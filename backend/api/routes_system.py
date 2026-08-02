"""
System API.

Consolidates the operational surfaces added on top of the existing agent:
health dashboard, diagnostics report, resource monitor, configuration
export/import/backup/restore, and build/version info. Everything here is
read-mostly and composes existing services (HealthMonitor, DiagnosticsService,
ResourceMonitor, ConfigManager, github_info) rather than owning any new
state of its own.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.app_state import state
from backend.api.auth import require_auth
from backend.config.config_manager import ConfigManager
from backend.integrations.github_info import get_build_info
from backend.monitoring.diagnostics import DiagnosticsService
from backend.monitoring.health import HealthMonitor
from backend.monitoring.resources import ResourceMonitor

router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------- #
# Health
# ---------------------------------------------------------------------- #
@router.get("/health")
async def system_health():
    monitor = HealthMonitor(state)
    report = await monitor.check_all()
    return report.to_dict()


# ---------------------------------------------------------------------- #
# Diagnostics
# ---------------------------------------------------------------------- #
@router.get("/diagnostics")
async def system_diagnostics():
    service = DiagnosticsService(state)
    report = await service.run()
    return report.to_dict()


@router.get("/diagnostics/text")
async def system_diagnostics_text():
    service = DiagnosticsService(state)
    report = await service.run()
    return {"report": report.to_text()}


# ---------------------------------------------------------------------- #
# Resources
# ---------------------------------------------------------------------- #
@router.get("/resources")
async def system_resources():
    monitor = ResourceMonitor(state)
    snapshot = await monitor.async_snapshot()
    return snapshot.to_dict()


# ---------------------------------------------------------------------- #
# Configuration manager
# ---------------------------------------------------------------------- #
class ImportConfigRequest(BaseModel):
    settings: dict


class RestoreConfigRequest(BaseModel):
    filename: str


@router.get("/config/export")
async def export_config():
    return ConfigManager.export_settings()


@router.post("/config/import")
async def import_config(req: ImportConfigRequest):
    applied = ConfigManager.import_settings({"settings": req.settings})
    return {"applied": applied}


@router.post("/config/backup")
async def backup_config():
    path = ConfigManager.backup()
    return {"filename": path.name}


@router.get("/config/backups")
async def list_config_backups():
    return {"backups": ConfigManager.list_backups()}


@router.post("/config/restore")
async def restore_config(req: RestoreConfigRequest):
    try:
        applied = ConfigManager.restore(req.filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"applied": applied}


# ---------------------------------------------------------------------- #
# Version / build info
# ---------------------------------------------------------------------- #
@router.get("/version")
async def system_version():
    return get_build_info().to_dict()
