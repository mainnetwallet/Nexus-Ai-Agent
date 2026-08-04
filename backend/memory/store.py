"""
Memory subsystem: SQLite for structured records, ChromaDB for semantic
similarity search over past workflows so the agent can recall "the last time
I did something like this" without any site-specific hardcoding.

Memory Improvements layered on top of the original store below -- every
pre-existing method keeps its original signature and behavior, so nothing
that already calls MemoryStore needs to change:

  - Importance Scoring  -- compute_base_importance() at write time,
                           effective_importance() (recency + access decay)
                           at read/rank time.
  - Memory Categories   -- MemoryCategory (Conversation/Skills/Browser/
                           Coding/Profiles/Tasks/General), inferred from
                           `kind` unless a caller passes metadata["category"].
  - Duplicate Detection -- exact-hash de-dup folds repeats into the existing
                           row automatically at write time; find_duplicate_groups
                           / merge_duplicates handle pre-existing duplicates
                           (hash-exact and, when available, semantic-near via
                           the same ChromaDB collection already used for recall).
  - Forget / Archive    -- archive_memory/unarchive_memory (reversible, hidden
                           from default listing) and forget_memory (permanent
                           delete from SQLite + ChromaDB).
  - Expiration Policy   -- run_expiration_sweep() archives/forgets aged,
                           low-importance memories; start()/stop() run it on
                           a background timer while the backend is up.
  - Memory Analytics    -- get_analytics() powers the Memory dashboard's
                           statistics panel.

All of this reuses the existing SQLite MemoryEntry table and ChromaDB
`nexus_workflows` collection -- no new storage engine, no new vector store.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import re
from typing import Any, Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import func, select

from backend.config.settings import settings
from backend.database.models import MemoryCategory, MemoryEntry
from backend.database.session import get_session

logger = logging.getLogger("nexus.memory")

# ------------------------------------------------------------------ #
# Categories, importance scoring, duplicate hashing -- pure helpers,
# unit-testable without touching the DB or ChromaDB.
# ------------------------------------------------------------------ #

# `kind` -> default MemoryCategory. An explicit metadata["category"] on any
# save_* call always wins over this; it's only the fallback.
_KIND_CATEGORY: dict[str, MemoryCategory] = {
    "workflow": MemoryCategory.BROWSER,
    "failure": MemoryCategory.BROWSER,
    "preference": MemoryCategory.PROFILES,
    "mcp_call": MemoryCategory.CODING,
    "conversation": MemoryCategory.CONVERSATION,
    "chat": MemoryCategory.CONVERSATION,
    "skill": MemoryCategory.SKILLS,
    "task": MemoryCategory.TASKS,
    "profile": MemoryCategory.PROFILES,
    "code": MemoryCategory.CODING,
    "coding": MemoryCategory.CODING,
    "browser": MemoryCategory.BROWSER,
}

# Base weight per `kind` folded into the importance score at write time --
# e.g. a saved preference or a successful workflow tends to matter more to
# future recall than a routine tool call or a failed one-off step.
_KIND_BASE_WEIGHT: dict[str, float] = {
    "workflow": 0.65,
    "failure": 0.35,
    "preference": 0.75,
    "mcp_call": 0.4,
    "conversation": 0.45,
    "skill": 0.7,
    "task": 0.55,
}

_RECENCY_HALF_LIFE_DAYS = 60.0
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_for_hash(text_: str) -> str:
    return _WHITESPACE_RE.sub(" ", text_.strip().lower())


def _aware(moment: dt.datetime) -> dt.datetime:
    """SQLite/aiosqlite hands back naive datetimes even for columns declared
    DateTime(timezone=True); every comparison against a timezone-aware `now`
    needs this normalization first, or Python raises on `<`/`<=`."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.timezone.utc)


def content_hash(text_: str) -> str:
    """Deterministic fingerprint of a memory's normalized content, used to
    detect exact/near-verbatim duplicates cheaply (no embedding needed)."""
    return hashlib.sha256(_normalize_for_hash(text_).encode("utf-8")).hexdigest()


def infer_category(kind: str, metadata: dict[str, Any] | None) -> str:
    """Resolve the MemoryCategory a new entry should be filed under."""
    metadata = metadata or {}
    explicit = metadata.get("category")
    if explicit:
        try:
            return MemoryCategory(str(explicit).lower()).value
        except ValueError:
            pass
    return _KIND_CATEGORY.get(kind, MemoryCategory.GENERAL).value


def compute_base_importance(kind: str, confidence: float, metadata: dict[str, Any] | None = None) -> float:
    """Stable importance component captured once at write time: a blend of
    the caller-supplied confidence and a `kind`-specific base weight, nudged
    by outcome status when present. Kept separate from effective_importance
    (below) so re-ranking by recency/access never requires rewriting history."""
    base = _KIND_BASE_WEIGHT.get(kind, 0.5)
    score = (confidence * 0.6) + (base * 0.4)
    metadata = metadata or {}
    if metadata.get("status") == "succeeded":
        score += 0.1
    elif metadata.get("status") == "failed":
        score -= 0.1
    return max(0.0, min(1.0, score))


def effective_importance(
    *,
    importance: float,
    access_count: int,
    created_at: dt.datetime,
    last_accessed_at: dt.datetime | None,
    now: dt.datetime | None = None,
) -> float:
    """Dynamic score used for ranking, analytics, and expiration: the stable
    base `importance`, decayed by time since the memory was last touched and
    boosted by how often it's been recalled -- so a frequently-used memory
    stays near the top as it ages, while a stale, never-revisited one fades."""
    now = now or dt.datetime.now(dt.timezone.utc)
    last_touch = _aware(last_accessed_at or created_at)
    age_days = max(0.0, (now - last_touch).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
    access_boost = min(access_count, 20) * 0.01
    score = importance * (0.4 + 0.6 * decay) + access_boost
    return max(0.0, min(1.0, score))


def _entry_to_dict(entry: MemoryEntry, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    return {
        "id": entry.id,
        "kind": entry.kind,
        "category": entry.category,
        "website": entry.website,
        "content": entry.content,
        "metadata": entry.metadata_json,
        "confidence": entry.confidence,
        "importance": round(entry.importance, 4),
        "effective_importance": round(
            effective_importance(
                importance=entry.importance,
                access_count=entry.access_count,
                created_at=entry.created_at,
                last_accessed_at=entry.last_accessed_at,
                now=now,
            ),
            4,
        ),
        "access_count": entry.access_count,
        "last_accessed_at": entry.last_accessed_at.isoformat() if entry.last_accessed_at else None,
        "archived": entry.archived,
        "archived_at": entry.archived_at.isoformat() if entry.archived_at else None,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        "merged_count": entry.merged_count,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


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
        self._expiration_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    # Background expiration loop
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Start the periodic expiration sweep (Memory Improvements: mirrors
        the LiveSessionManager/TaskQueueService start()/stop() task pattern
        used elsewhere in the backend). Safe to call more than once."""
        if not settings.memory_expiration_enabled:
            return
        if self._expiration_task is None or self._expiration_task.done():
            self._expiration_task = asyncio.create_task(self._expiration_loop())

    async def backfill_legacy_entries(self) -> int:
        """One-time data backfill for rows written before the Memory
        Improvements columns existed (content_hash is the tell -- it's only
        ever null on a pre-migration row, since every write path here always
        sets it). Computes category/importance/content_hash for them so
        older installs get ranking, categorization, and dedup for their
        existing history too, not just new writes. Safe to call every
        startup: a fully backfilled store finds zero matching rows."""
        async with get_session() as session:
            stmt = select(MemoryEntry).where(MemoryEntry.content_hash.is_(None))
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                row.content_hash = content_hash(row.content)
                row.category = infer_category(row.kind, row.metadata_json)
                row.importance = compute_base_importance(row.kind, row.confidence, row.metadata_json)
        if rows:
            logger.info("Backfilled Memory Improvements fields on %d legacy memory entries", len(rows))
        return len(rows)

    async def stop(self) -> None:
        if self._expiration_task:
            self._expiration_task.cancel()
            try:
                await self._expiration_task
            except asyncio.CancelledError:
                pass
            self._expiration_task = None

    async def _expiration_loop(self) -> None:
        interval = max(1, settings.memory_expiration_check_interval_hours) * 3600
        while True:
            try:
                result = await self.run_expiration_sweep()
                if result["archived"] or result["forgotten"]:
                    logger.info(
                        "Memory expiration sweep: archived=%s forgotten=%s",
                        result["archived"],
                        result["forgotten"],
                    )
            except Exception:
                logger.exception("Memory expiration sweep failed")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ #
    # Shared insert / duplicate-fold / chroma upsert path
    # ------------------------------------------------------------------ #
    async def _find_exact_duplicate(self, session, kind: str, chash: str) -> MemoryEntry | None:
        stmt = (
            select(MemoryEntry)
            .where(MemoryEntry.kind == kind, MemoryEntry.content_hash == chash, MemoryEntry.archived == False)  # noqa: E712
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    async def _persist_entry(
        self,
        *,
        kind: str,
        content: str,
        website: str | None,
        metadata: dict[str, Any],
        confidence: float,
        chroma_document: str,
        chroma_metadata: dict[str, Any],
    ) -> str:
        """Insert a new MemoryEntry, or -- if an exact-content duplicate of
        the same kind already exists and isn't archived -- fold this write
        into it instead (bump access_count/merged_count, keep the higher
        confidence, refresh last_accessed_at). Returns the entry id either
        way. This is the Duplicate Memory Detection guardrail applied at
        write time, on top of the maintenance-scan API below for anything
        already in the store before this existed."""
        chash = content_hash(content)
        category = infer_category(kind, metadata)
        importance = compute_base_importance(kind, confidence, metadata)
        now = dt.datetime.now(dt.timezone.utc)

        async with get_session() as session:
            existing = await self._find_exact_duplicate(session, kind, chash)
            if existing is not None:
                existing.access_count += 1
                existing.merged_count += 1
                existing.last_accessed_at = now
                existing.confidence = max(existing.confidence, confidence)
                existing.importance = max(existing.importance, importance)
                existing.metadata_json = {**existing.metadata_json, **metadata}
                entry_id = existing.id
                await session.flush()
            else:
                entry = MemoryEntry(
                    kind=kind,
                    website=website,
                    content=content,
                    metadata_json=metadata,
                    confidence=confidence,
                    category=category,
                    importance=importance,
                    content_hash=chash,
                    expires_at=self._compute_expiry(now, importance),
                )
                session.add(entry)
                await session.flush()
                entry_id = entry.id

        # chromadb's client is synchronous; embedding + upsert can take real
        # wall-clock time, so run it in a worker thread instead of blocking
        # the event loop (unchanged from the original implementation).
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[entry_id],
            documents=[chroma_document],
            metadatas=[{**chroma_metadata, "category": category}],
        )
        return entry_id

    @staticmethod
    def _compute_expiry(created_at: dt.datetime, importance: float) -> dt.datetime | None:
        """Only pre-flag entries that start out low-importance; anything
        else is left with no expiry until the sweep re-evaluates it against
        its *effective* (decayed) importance, which changes over time."""
        if importance < settings.memory_low_importance_threshold:
            return created_at + dt.timedelta(days=settings.memory_expiration_days)
        return None

    # ------------------------------------------------------------------ #
    # Original public API -- signatures and external behavior unchanged
    # ------------------------------------------------------------------ #
    async def save_workflow_outcome(self, website: str, goal: str, outcome: Any) -> None:
        summary = self._summarize(website, goal, outcome)
        confidence = 0.8 if outcome.status == "succeeded" else 0.2
        kind = "workflow" if outcome.status == "succeeded" else "failure"

        await self._persist_entry(
            kind=kind,
            content=summary,
            website=website,
            metadata={"goal": goal, "status": outcome.status, "step_count": len(outcome.steps)},
            confidence=confidence,
            chroma_document=summary,
            chroma_metadata={"website": website, "goal": goal, "status": outcome.status, "confidence": confidence},
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
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        for doc, meta in zip(docs, metas):
            out.append({"summary": doc, "confidence": meta.get("confidence", 0.5), "status": meta.get("status")})

        # Recall counts as "use" -- feeds Importance Scoring's access_count
        # boost. Best-effort: a bump failure never blocks returning results.
        if ids:
            try:
                await self._bump_access(ids)
            except Exception:
                logger.exception("Failed to record memory access from recall")
        return out

    async def save_preference(self, key: str, value: str) -> None:
        content = f"{key}={value}"
        await self._persist_entry(
            kind="preference",
            content=content,
            website=None,
            metadata={"key": key},
            confidence=0.5,
            chroma_document=f"user preference: {key} = {value}",
            chroma_metadata={"kind": "preference", "key": key},
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
        await self._persist_entry(
            kind="mcp_call",
            content=content,
            website=None,
            metadata={"connector": connector, "tool": tool, "arguments": arguments, "ok": ok},
            confidence=0.6 if ok else 0.3,
            chroma_document=content,
            chroma_metadata={"kind": "mcp_call", "connector": connector, "tool": tool},
        )

    @staticmethod
    def _summarize(website: str, goal: str, outcome: Any) -> str:
        step_summaries = "; ".join(f"{s.action}->{s.target}" for s in outcome.steps[-8:])
        return (
            f"Task on {website} with goal '{goal}' ended with status={outcome.status}. "
            f"{outcome.summary} Last steps: {step_summaries}"
        )

    # ------------------------------------------------------------------ #
    # Memory Improvements -- listing, access tracking
    # ------------------------------------------------------------------ #
    async def _bump_access(self, entry_ids: list[str]) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            stmt = select(MemoryEntry).where(MemoryEntry.id.in_(entry_ids))
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                row.access_count += 1
                row.last_accessed_at = now

    async def list_memories(
        self,
        *,
        category: str | None = None,
        kind: str | None = None,
        include_archived: bool = False,
        query: str | None = None,
        sort: str = "importance",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Reuses the existing SQLite MemoryEntry table -- no new storage.
        `sort` is "importance" (effective_importance desc, i.e. Automatic
        Importance Ranking), "recent" (created_at desc), or "access" (most
        recalled first)."""
        async with get_session() as session:
            stmt = select(MemoryEntry)
            if not include_archived:
                stmt = stmt.where(MemoryEntry.archived == False)  # noqa: E712
            if category:
                stmt = stmt.where(MemoryEntry.category == category)
            if kind:
                stmt = stmt.where(MemoryEntry.kind == kind)
            if query:
                needle = f"%{query.lower()}%"
                stmt = stmt.where(func.lower(MemoryEntry.content).like(needle))
            stmt = stmt.order_by(MemoryEntry.created_at.desc()).limit(max(1, min(limit, 2000)))
            rows = (await session.execute(stmt)).scalars().all()

        now = dt.datetime.now(dt.timezone.utc)
        entries = [_entry_to_dict(r, now) for r in rows]
        if sort == "recent":
            entries.sort(key=lambda e: e["created_at"] or "", reverse=True)
        elif sort == "access":
            entries.sort(key=lambda e: e["access_count"], reverse=True)
        else:  # "importance" -- Automatic Importance Ranking
            entries.sort(key=lambda e: e["effective_importance"], reverse=True)
        return entries

    async def get_memory(self, entry_id: str) -> dict[str, Any] | None:
        async with get_session() as session:
            entry = await session.get(MemoryEntry, entry_id)
            return _entry_to_dict(entry) if entry else None

    # ------------------------------------------------------------------ #
    # Memory Improvements -- Archive / Forget
    # ------------------------------------------------------------------ #
    async def archive_memory(self, entry_id: str) -> bool:
        async with get_session() as session:
            entry = await session.get(MemoryEntry, entry_id)
            if entry is None:
                return False
            entry.archived = True
            entry.archived_at = dt.datetime.now(dt.timezone.utc)
            return True

    async def unarchive_memory(self, entry_id: str) -> bool:
        async with get_session() as session:
            entry = await session.get(MemoryEntry, entry_id)
            if entry is None:
                return False
            entry.archived = False
            entry.archived_at = None
            return True

    async def forget_memory(self, entry_id: str) -> bool:
        """Permanent delete -- SQLite row and its ChromaDB embedding both go.
        Unlike archive_memory, this cannot be undone."""
        async with get_session() as session:
            entry = await session.get(MemoryEntry, entry_id)
            if entry is None:
                return False
            await session.delete(entry)
        try:
            await asyncio.to_thread(self._collection.delete, ids=[entry_id])
        except Exception:
            logger.exception("Failed to remove forgotten memory %s from chroma", entry_id)
        return True

    async def bulk_archive(self, entry_ids: list[str]) -> int:
        count = 0
        for entry_id in entry_ids:
            if await self.archive_memory(entry_id):
                count += 1
        return count

    async def bulk_forget(self, entry_ids: list[str]) -> int:
        count = 0
        for entry_id in entry_ids:
            if await self.forget_memory(entry_id):
                count += 1
        return count

    # ------------------------------------------------------------------ #
    # Memory Improvements -- Duplicate Detection (maintenance scan/merge)
    # ------------------------------------------------------------------ #
    async def find_duplicate_groups(self, semantic_threshold: float = 0.12) -> list[list[dict[str, Any]]]:
        """Groups of >=2 active memories considered duplicates of each
        other, for review/merge in the dashboard. Two signals, unioned:
          1. Exact content-hash match (catches anything written before the
             write-time dedup guard existed, or with matching content but
             different kinds).
          2. Semantic near-duplicates via the existing ChromaDB collection
             (catches paraphrases of the same fact/preference/workflow).
        """
        limit = settings.memory_duplicate_scan_limit
        async with get_session() as session:
            stmt = (
                select(MemoryEntry)
                .where(MemoryEntry.archived == False)  # noqa: E712
                .order_by(MemoryEntry.created_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()

        by_id = {r.id: r for r in rows}
        parent: dict[str, str] = {r.id: r.id for r in rows}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Signal 1: exact hash groups
        by_hash: dict[str, list[str]] = {}
        for r in rows:
            if r.content_hash:
                by_hash.setdefault(r.content_hash, []).append(r.id)
        for ids in by_hash.values():
            for other in ids[1:]:
                union(ids[0], other)

        # Signal 2: semantic near-duplicates via chroma (best-effort; a
        # failure here still leaves the exact-hash groups intact).
        if rows:
            try:
                for r in rows:
                    result = await asyncio.to_thread(
                        self._collection.query, query_texts=[r.content], n_results=4
                    )
                    ids = result.get("ids", [[]])[0]
                    distances = result.get("distances", [[]])[0]
                    for other_id, distance in zip(ids, distances):
                        if other_id != r.id and other_id in by_id and distance <= semantic_threshold:
                            union(r.id, other_id)
            except Exception:
                logger.exception("Semantic duplicate scan failed; falling back to exact-hash groups only")

        groups: dict[str, list[str]] = {}
        for entry_id in by_id:
            root = find(entry_id)
            groups.setdefault(root, []).append(entry_id)

        now = dt.datetime.now(dt.timezone.utc)
        return [
            sorted(
                (_entry_to_dict(by_id[i], now) for i in ids),
                key=lambda e: e["effective_importance"],
                reverse=True,
            )
            for ids in groups.values()
            if len(ids) > 1
        ]

    async def merge_duplicates(self, entry_ids: list[str], keep_id: str | None = None) -> dict[str, Any]:
        """Consolidate a duplicate group into one canonical entry: the
        highest-confidence/most-recently-touched row by default, or
        `keep_id` if given. Access counts are summed onto the survivor and
        every other row is permanently removed (SQLite + chroma)."""
        if len(entry_ids) < 2:
            raise ValueError("merge_duplicates needs at least 2 entry ids")

        async with get_session() as session:
            stmt = select(MemoryEntry).where(MemoryEntry.id.in_(entry_ids))
            rows = (await session.execute(stmt)).scalars().all()
            if len(rows) < 2:
                raise ValueError("fewer than 2 of the given ids were found")

            survivor = next((r for r in rows if r.id == keep_id), None) if keep_id else None
            if survivor is None:
                survivor = max(rows, key=lambda r: (r.confidence, r.created_at))

            total_access = sum(r.access_count for r in rows)
            merged_extra = sum(r.merged_count for r in rows if r.id != survivor.id) + (len(rows) - 1)
            survivor.access_count = total_access
            survivor.merged_count += merged_extra
            survivor.confidence = max(r.confidence for r in rows)
            survivor.importance = max(r.importance for r in rows)
            survivor.last_accessed_at = dt.datetime.now(dt.timezone.utc)

            removed_ids = [r.id for r in rows if r.id != survivor.id]
            for r in rows:
                if r.id != survivor.id:
                    await session.delete(r)
            survivor_id = survivor.id

        if removed_ids:
            try:
                await asyncio.to_thread(self._collection.delete, ids=removed_ids)
            except Exception:
                logger.exception("Failed to remove merged duplicate embeddings from chroma")

        merged = await self.get_memory(survivor_id)
        return {"kept_id": survivor_id, "removed_ids": removed_ids, "entry": merged}

    # ------------------------------------------------------------------ #
    # Memory Improvements -- Expiration Policy
    # ------------------------------------------------------------------ #
    async def run_expiration_sweep(self) -> dict[str, int]:
        """Finds active memories that are both aged past
        `memory_expiration_days` and low-value (effective_importance below
        `memory_low_importance_threshold`), then archives or permanently
        forgets them per `memory_expire_action`. Safe to call anytime,
        including manually via the API, in addition to the background loop."""
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(days=settings.memory_expiration_days)

        async with get_session() as session:
            stmt = select(MemoryEntry).where(MemoryEntry.archived == False)  # noqa: E712
            rows = (await session.execute(stmt)).scalars().all()
            eligible_ids = [
                r.id
                for r in rows
                if _aware(r.created_at) <= cutoff
                and effective_importance(
                    importance=r.importance,
                    access_count=r.access_count,
                    created_at=r.created_at,
                    last_accessed_at=r.last_accessed_at,
                    now=now,
                )
                < settings.memory_low_importance_threshold
            ]

        if not eligible_ids:
            return {"archived": 0, "forgotten": 0}

        if settings.memory_expire_action == "forget":
            count = await self.bulk_forget(eligible_ids)
            return {"archived": 0, "forgotten": count}
        count = await self.bulk_archive(eligible_ids)
        return {"archived": count, "forgotten": 0}

    # ------------------------------------------------------------------ #
    # Memory Improvements -- Analytics
    # ------------------------------------------------------------------ #
    async def get_analytics(self) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        async with get_session() as session:
            all_rows = (await session.execute(select(MemoryEntry))).scalars().all()

        active = [r for r in all_rows if not r.archived]
        archived = [r for r in all_rows if r.archived]

        by_category: dict[str, int] = {}
        for r in active:
            by_category[r.category] = by_category.get(r.category, 0) + 1

        by_kind: dict[str, int] = {}
        for r in active:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1

        effective_scores = [
            effective_importance(
                importance=r.importance,
                access_count=r.access_count,
                created_at=r.created_at,
                last_accessed_at=r.last_accessed_at,
                now=now,
            )
            for r in active
        ]
        avg_importance = round(sum(effective_scores) / len(effective_scores), 4) if effective_scores else 0.0

        cutoff = now - dt.timedelta(days=settings.memory_expiration_days)
        expiring_soon = sum(
            1
            for r, score in zip(active, effective_scores)
            if _aware(r.created_at) <= cutoff and score < settings.memory_low_importance_threshold
        )

        top_recalled = sorted(active, key=lambda r: r.access_count, reverse=True)[:5]
        most_important = sorted(
            zip(active, effective_scores), key=lambda pair: pair[1], reverse=True
        )[:5]

        # growth over the last 14 days, bucketed by day (for a small trend chart)
        growth: dict[str, int] = {}
        growth_cutoff = now - dt.timedelta(days=14)
        for r in all_rows:
            if _aware(r.created_at) >= growth_cutoff:
                day = r.created_at.date().isoformat()
                growth[day] = growth.get(day, 0) + 1

        duplicate_groups = await self.find_duplicate_groups()

        return {
            "total": len(all_rows),
            "active": len(active),
            "archived": len(archived),
            "by_category": by_category,
            "by_kind": by_kind,
            "average_importance": avg_importance,
            "expiring_soon": expiring_soon,
            "duplicate_group_count": len(duplicate_groups),
            "duplicate_entry_count": sum(len(g) for g in duplicate_groups),
            "top_recalled": [
                {"id": r.id, "content": r.content[:160], "access_count": r.access_count, "category": r.category}
                for r in top_recalled
            ],
            "most_important": [
                {"id": r.id, "content": r.content[:160], "effective_importance": round(score, 4), "category": r.category}
                for r, score in most_important
            ],
            "growth_last_14_days": dict(sorted(growth.items())),
        }
