"""
Memory API.

Original endpoint (GET /api/memory/search) is unchanged. Everything else
here is the Memory Improvements surface -- listing/filtering by category,
importance-ranked browsing, archive/forget, duplicate scan/merge,
expiration, and analytics for the Memory dashboard -- all backed by the
same MemoryStore (SQLite MemoryEntry table + ChromaDB collection).

Route order matters here: literal paths (/search, /analytics, /duplicates,
/expire/run) are registered before the parameterized /{entry_id} routes so
they aren't swallowed by the catch-all.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.app_state import state
from backend.api.auth import require_auth

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(require_auth)])


def _store():
    if state.memory is None:
        raise HTTPException(status_code=503, detail="Memory store not initialized")
    return state.memory


# ---------------------------------------------------------------- #
# Request bodies
# ---------------------------------------------------------------- #
class BulkIdsRequest(BaseModel):
    ids: list[str]


class MergeDuplicatesRequest(BaseModel):
    ids: list[str]
    keep_id: Optional[str] = None


# ---------------------------------------------------------------- #
# Original endpoint -- unchanged path, params, and response shape
# ---------------------------------------------------------------- #
@router.get("/search")
async def search_memory(q: str, top_k: int = 5):
    results = await state.memory.recall_similar_workflows(website="", goal=q, top_k=top_k)
    return {"results": results}


# ---------------------------------------------------------------- #
# Listing / browsing (Categories + Automatic Importance Ranking)
# ---------------------------------------------------------------- #
@router.get("")
async def list_memories(
    category: Optional[str] = None,
    kind: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "importance",
    include_archived: bool = False,
    limit: int = 200,
):
    return {
        "memories": await _store().list_memories(
            category=category,
            kind=kind,
            query=q,
            sort=sort,
            include_archived=include_archived,
            limit=limit,
        )
    }


@router.get("/analytics")
async def memory_analytics():
    """Memory Statistics and Analytics for the Memory dashboard."""
    return await _store().get_analytics()


@router.get("/duplicates")
async def list_duplicate_groups():
    """Duplicate Memory Detection -- groups of memories the store considers
    duplicates of each other (exact-hash and semantic-near), for review."""
    return {"groups": await _store().find_duplicate_groups()}


@router.post("/duplicates/merge")
async def merge_duplicate_group(payload: MergeDuplicatesRequest):
    if len(payload.ids) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 ids to merge")
    try:
        return await _store().merge_duplicates(payload.ids, keep_id=payload.keep_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/expire/run")
async def run_expiration_now():
    """Manually trigger the Memory Expiration Policy sweep (in addition to
    the automatic background timer)."""
    return await _store().run_expiration_sweep()


@router.post("/bulk/archive")
async def bulk_archive_memories(payload: BulkIdsRequest):
    count = await _store().bulk_archive(payload.ids)
    return {"archived": count}


@router.post("/bulk/forget")
async def bulk_forget_memories(payload: BulkIdsRequest):
    count = await _store().bulk_forget(payload.ids)
    return {"forgotten": count}


@router.get("/{entry_id}")
async def get_memory(entry_id: str):
    entry = await _store().get_memory(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return entry


# ---------------------------------------------------------------- #
# Archive / Forget
# ---------------------------------------------------------------- #
@router.post("/{entry_id}/archive")
async def archive_memory(entry_id: str):
    ok = await _store().archive_memory(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": entry_id, "archived": True}


@router.post("/{entry_id}/unarchive")
async def unarchive_memory(entry_id: str):
    ok = await _store().unarchive_memory(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": entry_id, "archived": False}


@router.delete("/{entry_id}")
async def forget_memory(entry_id: str):
    """Permanently forget a memory (SQLite row + ChromaDB embedding)."""
    ok = await _store().forget_memory(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": entry_id, "forgotten": True}
