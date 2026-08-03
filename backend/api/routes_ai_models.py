"""
AI Model Manager API.

Manual switching, smart-routing configuration, fallback/priority
configuration, temporary overrides, health, and connection testing for the
multi-provider LLM layer (backend/planner/model_manager.py).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.auth import require_auth
from backend.config.settings import LLMProvider
from backend.planner.model_manager import TaskType, model_manager, parse_provider_name

router = APIRouter(prefix="/api/ai-models", tags=["ai-models"], dependencies=[Depends(require_auth)])


def _provider(value: str) -> LLMProvider:
    try:
        return LLMProvider(value)
    except ValueError:
        parsed = parse_provider_name(value)
        if parsed:
            return parsed
        raise HTTPException(status_code=400, detail=f"Unknown provider '{value}'") from None


class SwitchRequest(BaseModel):
    provider: str
    model: Optional[str] = None


class RoutingModeRequest(BaseModel):
    mode: str  # "manual" | "auto"


class RoutingRuleUpdate(BaseModel):
    task_type: str
    provider: str


class RoutingRulesBulkUpdate(BaseModel):
    rules: dict[str, str]


class FallbackRequest(BaseModel):
    provider: str


class PriorityRequest(BaseModel):
    providers: list[str]


class ProviderToggleRequest(BaseModel):
    provider: str


class OverrideRequest(BaseModel):
    provider: str
    model: Optional[str] = None
    reason: Optional[str] = ""


@router.get("")
async def get_ai_models():
    """Full dashboard/settings view: current provider/model, routing mode,
    fallback, priority, routing rules, active override, and per-provider
    availability + health."""
    return model_manager.to_view()


@router.get("/health")
async def get_health():
    return model_manager.health_snapshot()


@router.get("/routing-rules")
async def get_routing_rules():
    return {k.value: v.value for k, v in model_manager.routing_rules.items()}


@router.put("/routing-rules")
async def update_routing_rules(req: RoutingRulesBulkUpdate):
    rules: dict[TaskType, LLMProvider] = {}
    for task_key, provider_val in req.rules.items():
        try:
            task_type = TaskType(task_key)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown task_type '{task_key}'") from None
        rules[task_type] = _provider(provider_val)
    model_manager.set_routing_rules(rules)
    return {k.value: v.value for k, v in model_manager.routing_rules.items()}


@router.post("/routing-rules/one")
async def update_routing_rule(req: RoutingRuleUpdate):
    try:
        task_type = TaskType(req.task_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown task_type '{req.task_type}'") from None
    model_manager.set_routing_rule(task_type, _provider(req.provider))
    return {"task_type": task_type.value, "provider": model_manager.routing_rules[task_type].value}


@router.post("/switch")
async def switch_provider(req: SwitchRequest):
    model_manager.switch_provider(_provider(req.provider), req.model)
    return model_manager.to_view()


@router.post("/routing-mode")
async def set_routing_mode(req: RoutingModeRequest):
    if req.mode not in ("manual", "auto"):
        raise HTTPException(status_code=400, detail="mode must be 'manual' or 'auto'")
    model_manager.enable_auto_routing(req.mode == "auto")
    return model_manager.to_view()


@router.post("/fallback")
async def set_fallback(req: FallbackRequest):
    model_manager.set_fallback_provider(_provider(req.provider))
    return model_manager.to_view()


@router.post("/priority")
async def set_priority(req: PriorityRequest):
    model_manager.set_provider_priority([_provider(p) for p in req.providers])
    return model_manager.to_view()


@router.post("/enable")
async def enable_provider(req: ProviderToggleRequest):
    model_manager.enable_provider(_provider(req.provider))
    return model_manager.to_view()


@router.post("/disable")
async def disable_provider(req: ProviderToggleRequest):
    model_manager.disable_provider(_provider(req.provider))
    return model_manager.to_view()


@router.post("/override")
async def set_override(req: OverrideRequest):
    model_manager.use_temporarily(_provider(req.provider), req.model, req.reason or "")
    return model_manager.to_view()


@router.delete("/override")
async def clear_override():
    model_manager.clear_override()
    return model_manager.to_view()


@router.post("/test/{provider}")
async def test_connection(provider: str):
    return await model_manager.test_connection(_provider(provider))
