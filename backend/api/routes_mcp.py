"""
MCP Core management API.

Connectors are fixed, first-party classes (backend/mcp/connectors/) --
there is no "install a connector" endpoint, matching routes_plugins.py's
no-remote-code-execution stance for plugins.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.app_state import state
from backend.api.auth import require_auth

router = APIRouter(prefix="/api/mcp", tags=["mcp"], dependencies=[Depends(require_auth)])


def _manager():
    if state.mcp is None:
        raise HTTPException(status_code=503, detail="MCP manager not initialized")
    return state.mcp


class ConfigureRequest(BaseModel):
    config: dict[str, Any] = {}


class CallRequest(BaseModel):
    connector: str
    tool: str
    arguments: dict[str, Any] = {}
    timeout: Optional[float] = None


class RouteRequest(BaseModel):
    text: str
    connector_hint: Optional[str] = None


@router.get("/connectors")
async def list_connectors():
    return {"connectors": _manager().list_connectors()}


@router.post("/connectors/{name}/enable")
async def enable_connector(name: str):
    ok = await _manager().enable(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not enable connector '{name}' (unknown or failed to connect)")
    return {"name": name, "enabled": True}


@router.post("/connectors/{name}/disable")
async def disable_connector(name: str):
    ok = await _manager().disable(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not disable connector '{name}' (unknown)")
    return {"name": name, "enabled": False}


@router.post("/connectors/{name}/configure")
async def configure_connector(name: str, payload: ConfigureRequest):
    ok = await _manager().configure(name, payload.config)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not configure connector '{name}' (unknown)")
    return {"name": name, "configured": True}


@router.get("/tools")
async def list_tools(connector: Optional[str] = None):
    return {"tools": _manager().list_tools(connector)}


@router.post("/call")
async def call_tool(payload: CallRequest):
    result = await _manager().call(payload.connector, payload.tool, payload.arguments, timeout=payload.timeout)
    return result.to_dict()


@router.post("/route")
async def route_only(payload: RouteRequest):
    """Test routing without executing: given free text, return which
    connector+tool the router would pick."""
    routed = _manager().router.route(payload.text, connector_hint=payload.connector_hint, min_score=_manager().router_min_score)
    if routed is None:
        return {"matched": False, "route": None}
    return {"matched": True, "route": routed.to_dict()}


@router.get("/health")
async def health():
    return await _manager().health()


@router.get("/social-status")
async def social_status():
    """Connection Status + Session Status + Account Information + Last Used
    for the X/Discord/Gmail connectors, used by the dashboard's social
    connectors panel (frontend/src/pages/Mcp.tsx)."""
    return {"connectors": await _manager().social_status()}
