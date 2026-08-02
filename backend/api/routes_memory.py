from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.app_state import state
from backend.api.auth import require_auth

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(require_auth)])


@router.get("/search")
async def search_memory(q: str, top_k: int = 5):
    results = await state.memory.recall_similar_workflows(website="", goal=q, top_k=top_k)
    return {"results": results}
