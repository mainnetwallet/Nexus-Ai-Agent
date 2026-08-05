"""
Skill Library API.

CRUD + versioning + import/export/share + pending "save as skill?"
suggestions for the Skill Learning System (backend/skills/). Mirrors the
style of backend/api/routes_plugins.py and routes_tasks.py.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.app_state import state
from backend.api.auth import require_auth
from backend.database.models import SkillSource

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=[Depends(require_auth)])


def _library():
    if state.skills is None:
        raise HTTPException(status_code=503, detail="Skill library not initialized (skills_enabled=false?)")
    return state.skills


def _teach():
    if state.teach is None:
        raise HTTPException(status_code=503, detail="Teach Mode not initialized (skills_enabled=false?)")
    return state.teach


# ---------------------------------------------------------------- #
# Request bodies
# ---------------------------------------------------------------- #
class SkillVariable(BaseModel):
    name: str
    description: str = ""
    default: str = ""


class SkillStep(BaseModel):
    action: str
    target: str = ""
    value: str = ""
    description: str = ""


class CreateSkillRequest(BaseModel):
    name: str
    description: str = ""
    category: str = "general"
    trigger: str = ""
    variables: list[SkillVariable] = []
    workflow: list[SkillStep] = []
    success_condition: Optional[str] = None
    required_plugins: list[str] = []
    required_browser: Optional[str] = None
    website_hint: Optional[str] = None
    enabled: bool = True


class UpdateSkillRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    trigger: Optional[str] = None
    variables: Optional[list[SkillVariable]] = None
    workflow: Optional[list[SkillStep]] = None
    success_condition: Optional[str] = None
    required_plugins: Optional[list[str]] = None
    required_browser: Optional[str] = None
    website_hint: Optional[str] = None
    enabled: Optional[bool] = None
    change_note: str = "edited"


class RenameRequest(BaseModel):
    name: str


class DuplicateRequest(BaseModel):
    name: Optional[str] = None


class ImportSkillRequest(BaseModel):
    payload: Optional[dict[str, Any]] = None
    share_code: Optional[str] = None


class RecordedWorkflowRequest(BaseModel):
    name: str
    steps: list[SkillStep]
    description: str = ""
    category: str = "general"
    trigger: str = ""


class RollbackRequest(BaseModel):
    version: int


class LearnFromTextRequest(BaseModel):
    text: str


class TeachStartRequest(BaseModel):
    name: str = ""
    trigger: str = ""
    website_hint: str = ""


class TeachStepRequest(BaseModel):
    text: str


class TeachFinishRequest(BaseModel):
    name: Optional[str] = None
    description: str = ""
    category: str = "general"
    trigger: str = ""


class CorrectionRequest(BaseModel):
    skill_id: str
    step_index: int
    instruction: str


class ImportUrlRequest(BaseModel):
    url: str


# ---------------------------------------------------------------- #
# CRUD
# ---------------------------------------------------------------- #
@router.get("")
async def list_skills(category: Optional[str] = None, enabled_only: bool = False, search: Optional[str] = None):
    return await _library().list(category=category, enabled_only=enabled_only, search=search)


@router.post("")
async def create_skill(req: CreateSkillRequest):
    return await _library().create(
        name=req.name,
        description=req.description,
        category=req.category,
        trigger=req.trigger,
        variables=[v.model_dump() for v in req.variables],
        workflow=[s.model_dump() for s in req.workflow],
        success_condition=req.success_condition,
        required_plugins=req.required_plugins,
        required_browser=req.required_browser,
        website_hint=req.website_hint,
        source=SkillSource.MANUAL,
        enabled=req.enabled,
    )


@router.get("/pending")
async def list_pending():
    """Skills the agent just learned by completing a task successfully,
    awaiting a yes/no on whether to save them -- shown as prompts on the
    dashboard Skills page, in addition to being answerable via chat."""
    return _library().list_pending()


@router.post("/pending/{task_id}/confirm")
async def confirm_pending(task_id: str):
    skill = await _library().confirm_pending(task_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="No pending suggestion for that task")
    return skill


@router.post("/pending/{task_id}/discard")
async def discard_pending(task_id: str):
    ok = _library().discard_pending(task_id)
    return {"ok": ok}


@router.get("/{skill_id}")
async def get_skill(skill_id: str):
    skill = await _library().get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.patch("/{skill_id}")
async def update_skill(skill_id: str, req: UpdateSkillRequest):
    patch = req.model_dump(exclude={"change_note"}, exclude_none=True)
    if "variables" in patch:
        patch["variables"] = [v if isinstance(v, dict) else v.model_dump() for v in patch["variables"]]
    if "workflow" in patch:
        patch["workflow"] = [s if isinstance(s, dict) else s.model_dump() for s in patch["workflow"]]
    skill = await _library().update(skill_id, patch, change_note=req.change_note)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str):
    ok = await _library().delete(skill_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"ok": True}


@router.post("/{skill_id}/rename")
async def rename_skill(skill_id: str, req: RenameRequest):
    skill = await _library().rename(skill_id, req.name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.post("/{skill_id}/duplicate")
async def duplicate_skill(skill_id: str, req: DuplicateRequest):
    skill = await _library().duplicate(skill_id, req.name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.post("/{skill_id}/enable")
async def enable_skill(skill_id: str):
    skill = await _library().set_enabled(skill_id, True)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.post("/{skill_id}/disable")
async def disable_skill(skill_id: str):
    skill = await _library().set_enabled(skill_id, False)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


# ---------------------------------------------------------------- #
# Version history
# ---------------------------------------------------------------- #
@router.get("/{skill_id}/versions")
async def list_versions(skill_id: str):
    return await _library().versions(skill_id)


@router.post("/{skill_id}/rollback")
async def rollback_skill(skill_id: str, req: RollbackRequest):
    skill = await _library().rollback(skill_id, req.version)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill or version not found")
    return skill


# ---------------------------------------------------------------- #
# Import / export / share
# ---------------------------------------------------------------- #
@router.get("/{skill_id}/export")
async def export_skill(skill_id: str):
    payload = await _library().export_skill(skill_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return payload


@router.get("/{skill_id}/share")
async def share_skill(skill_id: str):
    code = await _library().share_code(skill_id)
    if code is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"share_code": code}


@router.post("/import")
async def import_skill(req: ImportSkillRequest):
    if req.share_code:
        try:
            return await _library().import_from_code(req.share_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.payload:
        return await _library().import_skill(req.payload, source=SkillSource.IMPORTED)
    raise HTTPException(status_code=400, detail="Provide either 'payload' or 'share_code'")


@router.post("/import/recorded-workflow")
async def import_recorded_workflow(req: RecordedWorkflowRequest):
    """Learn from a recorded workflow: a plain list of steps, e.g. exported
    from another tool or hand-written, with no export envelope required."""
    return await _library().import_recorded_workflow(
        name=req.name,
        steps=[s.model_dump() for s in req.steps],
        description=req.description,
        category=req.category,
        trigger=req.trigger,
    )


@router.post("/import/from-task/{task_id}")
async def import_from_task_history(task_id: str, req: RecordedWorkflowRequest | None = None):
    """Learn from a browser demonstration / prior run: rebuilds a skill's
    workflow from that task's persisted TaskStep rows (backend.database.
    models.TaskStep), regardless of whether it's still pending confirmation."""
    from sqlalchemy import select

    from backend.database.models import Task, TaskStep
    from backend.database.session import get_session

    async with get_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        result = await session.execute(select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.index))
        steps = list(result.scalars().all())

    if not steps:
        raise HTTPException(status_code=400, detail="No recorded steps for that task")

    name = (req.name if req and req.name else task.goal[:80])
    return await _library().import_recorded_workflow(
        name=name,
        steps=[{"action": s.action, "target": s.target_description, "value": s.value or "", "description": s.reasoning or ""} for s in steps],
        description=(req.description if req else "") or f"Recorded from a task on {task.website}: {task.goal}",
        category=(req.category if req else "general"),
        trigger=(req.trigger if req else task.goal),
    )


# ---------------------------------------------------------------- #
# Natural-language authoring & corrections
# ---------------------------------------------------------------- #
@router.post("/learn")
async def learn_from_text(req: LearnFromTextRequest):
    """Learn a skill from one natural-language description end-to-end.
    If the text is a URL that a skill provider can handle (e.g. GitHub),
    auto-routes to the URL-based import pipeline instead."""
    text = req.text.strip()

    # Auto-detect URLs and route to the URL import pipeline
    if text.startswith(("http://", "https://")):
        from backend.skills.providers.registry import get_registry
        registry = get_registry()
        if registry.can_handle(text):
            result = await _library().import_from_url(text)
            return {"created": True, "source": "url", "import_result": result}

    draft = await _teach().parse_skill_from_text(text)
    if not draft.get("workflow"):
        return {"created": False, "draft": draft, "reason": "Could not extract concrete steps from that description."}
    skill = await _library().create(
        name=draft["name"] or text[:60],
        description=draft.get("description", ""),
        category=draft.get("category", "general"),
        trigger=draft.get("trigger", ""),
        variables=draft.get("variables") or [],
        workflow=draft.get("workflow") or [],
        website_hint=draft.get("website_hint"),
        source=SkillSource.NATURAL_LANGUAGE,
    )
    return {"created": True, "skill": skill}


@router.post("/import-url")
async def import_from_url(req: ImportUrlRequest):
    """Import skills from a GitHub repository (or any supported URL).
    Clones the repository, analyzes it with LLM, extracts reusable skills,
    deduplicates against existing skills, and saves them to the library."""
    try:
        result = await _library().import_from_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.post("/correct")
async def correct_skill(req: CorrectionRequest):
    skill = await _library().get(req.skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    corrected_step = await _teach().parse_correction(req.instruction)
    workflow = list(skill["workflow"])
    if 0 <= req.step_index < len(workflow):
        workflow[req.step_index] = corrected_step
        note = f"corrected step {req.step_index}"
    else:
        workflow.append(corrected_step)
        note = "appended corrected step"
    updated = await _library().update(req.skill_id, {"workflow": workflow}, change_note=note)
    return updated


# ---------------------------------------------------------------- #
# Teach Mode
# ---------------------------------------------------------------- #
@router.post("/teach/{session_id}/start")
async def teach_start(session_id: str, req: TeachStartRequest):
    draft = _teach().start(session_id, name=req.name, trigger=req.trigger, website_hint=req.website_hint)
    return {"session_id": session_id, "draft": draft.__dict__}


@router.post("/teach/{session_id}/step")
async def teach_step(session_id: str, req: TeachStepRequest):
    step = await _teach().add_step_from_text(session_id, req.text)
    if step is None:
        raise HTTPException(status_code=400, detail="No active Teach Mode session for this id")
    return {"step": step, "draft": _teach().get_draft(session_id).__dict__}


@router.post("/teach/{session_id}/undo")
async def teach_undo(session_id: str):
    ok = _teach().undo_last_step(session_id)
    return {"ok": ok}


@router.post("/teach/{session_id}/cancel")
async def teach_cancel(session_id: str):
    ok = _teach().cancel(session_id)
    return {"ok": ok}


@router.post("/teach/{session_id}/finish")
async def teach_finish(session_id: str, req: TeachFinishRequest):
    draft = _teach().get_draft(session_id)
    if draft is None:
        raise HTTPException(status_code=400, detail="No active Teach Mode session for this id")

    _teach().finish(session_id)
    skill = await _library().create(
        name=req.name or draft.name or "Taught skill",
        description=req.description or draft.description,
        category=req.category or draft.category,
        trigger=req.trigger or draft.trigger,
        website_hint=draft.website_hint or None,
        variables=draft.variables,
        workflow=draft.steps,
        source=SkillSource.TEACH_MODE,
    )
    return skill


@router.get("/teach/{session_id}")
async def teach_status(session_id: str):
    draft = _teach().get_draft(session_id)
    return {"active": draft is not None, "draft": draft.__dict__ if draft else None}
