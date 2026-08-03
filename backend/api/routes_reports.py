from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.auth import require_auth
from backend.database.models import Report
from backend.database.session import list_all

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_auth)])


@router.get("")
async def list_reports():
    reports = await list_all(Report, order_by=Report.created_at.desc(), limit=100)
    return [
        {
            "id": r.id,
            "task_id": r.task_id,
            "status": r.status,
            "summary": r.summary,
            "execution_seconds": r.execution_seconds,
            "tx_hashes": r.tx_hashes,
            "screenshots": r.screenshots,
            "created_at": r.created_at.isoformat(),
        }
        for r in reports
    ]
