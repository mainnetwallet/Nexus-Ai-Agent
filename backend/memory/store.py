"""
Memory subsystem: SQLite for structured records, ChromaDB for semantic
similarity search over past workflows so the agent can recall "the last time
I did something like this" without any site-specific hardcoding.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from backend.config.settings import settings
from backend.database.models import MemoryEntry
from backend.database.session import get_session

logger = logging.getLogger("nexus.memory")


class MemoryStore:
    def __init__(self, embedding_function: Any = None) -> None:
        # `embedding_function` is optional and defaults to chromadb's built-in
        # ONNX MiniLM function (unchanged production behavior). It exists as
        # a constructor param so tests/offline deployments without egress to
        # chroma's model bucket can inject a lightweight stand-in instead of
        # triggering a network download on first upsert.
        self._chroma_client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        collection_kwargs: dict[str, Any] = {}
        if embedding_function is not None:
            collection_kwargs["embedding_function"] = embedding_function
        self._collection = self._chroma_client.get_or_create_collection("nexus_workflows", **collection_kwargs)

    async def save_workflow_outcome(self, website: str, goal: str, outcome: Any) -> None:
        summary = self._summarize(website, goal, outcome)
        confidence = 0.8 if outcome.status == "succeeded" else 0.2
        kind = "workflow" if outcome.status == "succeeded" else "failure"

        async with get_session() as session:
            entry = MemoryEntry(
                kind=kind,
                website=website,
                content=summary,
                metadata_json={"goal": goal, "status": outcome.status, "step_count": len(outcome.steps)},
                confidence=confidence,
            )
            session.add(entry)
            await session.flush()
            entry_id = entry.id

        # chromadb's client is synchronous; embedding + upsert can take real
        # wall-clock time, so run it in a worker thread instead of blocking
        # the event loop (which would otherwise stall the FastAPI server and
        # every other in-flight task/websocket for the duration of the call).
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[entry_id],
            documents=[summary],
            metadatas=[{"website": website, "goal": goal, "status": outcome.status, "confidence": confidence}],
        )
        logger.info("Saved workflow memory for %s (%s)", website, outcome.status)

    async def recall_similar_workflows(self, website: str, goal: str, top_k: int = 3) -> list[dict[str, Any]]:
        query = f"website: {website} goal: {goal}"
        try:
            results = await asyncio.to_thread(self._collection.query, query_texts=[query], n_results=top_k)
        except Exception:
            logger.exception("Chroma query failed")
            return []

        out: list[dict[str, Any]] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        for doc, meta in zip(docs, metas):
            out.append({"summary": doc, "confidence": meta.get("confidence", 0.5), "status": meta.get("status")})
        return out

    async def save_preference(self, key: str, value: str) -> None:
        async with get_session() as session:
            entry = MemoryEntry(kind="preference", content=f"{key}={value}", metadata_json={"key": key})
            session.add(entry)
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[str(uuid.uuid4())],
            documents=[f"user preference: {key} = {value}"],
            metadatas=[{"kind": "preference", "key": key}],
        )

    async def save_tool_call(self, connector: str, tool: str, arguments: dict[str, Any], result: Any) -> None:
        """Records an MCP tool invocation (backend/mcp/manager.py) so it's
        recallable the same way workflow outcomes and preferences are.
        `result` is a backend.mcp.base.ToolCallResult (kept as Any here so
        this module has no import dependency on backend/mcp/)."""
        ok = getattr(result, "ok", None)
        output = getattr(result, "output", None)
        error = getattr(result, "error", None)
        content = f"{connector}.{tool}({arguments}) -> ok={ok} output={output} error={error}"
        async with get_session() as session:
            entry = MemoryEntry(
                kind="mcp_call",
                content=content,
                metadata_json={"connector": connector, "tool": tool, "arguments": arguments, "ok": ok},
            )
            session.add(entry)
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[str(uuid.uuid4())],
            documents=[content],
            metadatas=[{"kind": "mcp_call", "connector": connector, "tool": tool}],
        )

    @staticmethod
    def _summarize(website: str, goal: str, outcome: Any) -> str:
        step_summaries = "; ".join(f"{s.action}->{s.target}" for s in outcome.steps[-8:])
        return (
            f"Task on {website} with goal '{goal}' ended with status={outcome.status}. "
            f"{outcome.summary} Last steps: {step_summaries}"
        )
