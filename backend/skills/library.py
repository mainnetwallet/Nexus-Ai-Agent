"""
SkillService: the Skill Library.

Owns CRUD + version history for Skill rows (backend/database/models.py),
keeps a ChromaDB collection ("nexus_skills", same persistence directory as
backend/memory/store.py's "nexus_workflows") in sync for semantic matching,
and tracks two pieces of ephemeral state in memory:

- pending "save this as a skill?" suggestions, registered by
  TaskQueueService after a task succeeds (see backend/planner/task_queue.py)
  and resolved via chat ("save as skill" / "discard") or the dashboard
  Skills page.
- active Teach Mode drafts live in backend.skills.teach.TeachModeManager,
  not here, but both are constructed together in backend/main.py and share
  the same SkillService instance for the final `create()` call.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
from typing import Any, Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import select

from backend.config.settings import settings
from backend.database.models import Skill, SkillSource, SkillVersion
from backend.database.session import get_session

logger = logging.getLogger("nexus.skills")

# Fields that fully describe a skill's behavior (as opposed to bookkeeping
# like id/usage stats/timestamps) -- these are what gets snapshotted into
# SkillVersion and what export/import round-trip.
_PORTABLE_FIELDS = (
    "name",
    "description",
    "category",
    "trigger",
    "variables",
    "workflow",
    "success_condition",
    "required_plugins",
    "required_browser",
    "website_hint",
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class SkillService:
    def __init__(self) -> None:
        self._chroma_client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._chroma_client.get_or_create_collection("nexus_skills")

        # task_id -> draft skill dict, registered by TaskQueueService right
        # after a task succeeds. Ephemeral by design (process-lifetime only)
        # -- if the process restarts before the user answers, the prompt is
        # simply gone, same as an unread chat notification would be.
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_order: list[str] = []

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_dict(row: Skill) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "category": row.category,
            "trigger": row.trigger,
            "variables": row.variables or [],
            "workflow": row.workflow or [],
            "success_condition": row.success_condition,
            "required_plugins": row.required_plugins or [],
            "required_browser": row.required_browser,
            "website_hint": row.website_hint,
            "success_rate": row.success_rate,
            "usage_count": row.usage_count,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "version": row.version,
            "enabled": row.enabled,
            "source": row.source.value if hasattr(row.source, "value") else row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    async def create(
        self,
        *,
        name: str,
        description: str = "",
        category: str = "general",
        trigger: str = "",
        variables: Optional[list[dict]] = None,
        workflow: Optional[list[dict]] = None,
        success_condition: Optional[str] = None,
        required_plugins: Optional[list[str]] = None,
        required_browser: Optional[str] = None,
        website_hint: Optional[str] = None,
        source: SkillSource = SkillSource.MANUAL,
        enabled: bool = True,
    ) -> dict[str, Any]:
        async with get_session() as session:
            row = Skill(
                name=name.strip() or "Untitled skill",
                description=description or "",
                category=category or "general",
                trigger=trigger or "",
                variables=variables or [],
                workflow=workflow or [],
                success_condition=success_condition,
                required_plugins=required_plugins or [],
                required_browser=required_browser,
                website_hint=website_hint,
                source=source,
                enabled=enabled,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            skill_dict = self._to_dict(row)

            version_row = SkillVersion(
                skill_id=row.id,
                version=1,
                snapshot_json={k: skill_dict[k] for k in _PORTABLE_FIELDS},
                change_note=f"created via {source.value if hasattr(source, 'value') else source}",
            )
            session.add(version_row)

        await self._reindex(skill_dict)
        logger.info("Skill created: %s (%s)", skill_dict["name"], skill_dict["id"])
        return skill_dict

    async def get(self, skill_id: str) -> Optional[dict[str, Any]]:
        async with get_session() as session:
            row = await session.get(Skill, skill_id)
            return self._to_dict(row) if row else None

    async def list(
        self, category: Optional[str] = None, enabled_only: bool = False, search: Optional[str] = None
    ) -> list[dict[str, Any]]:
        async with get_session() as session:
            stmt = select(Skill).order_by(Skill.updated_at.desc())
            if category:
                stmt = stmt.where(Skill.category == category)
            if enabled_only:
                stmt = stmt.where(Skill.enabled.is_(True))
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        out = [self._to_dict(r) for r in rows]
        if search:
            needle = search.lower()
            out = [
                s
                for s in out
                if needle in s["name"].lower()
                or needle in (s["description"] or "").lower()
                or needle in (s["trigger"] or "").lower()
            ]
        return out

    async def update(
        self, skill_id: str, patch: dict[str, Any], change_note: str = "edited"
    ) -> Optional[dict[str, Any]]:
        """Applies `patch` (any subset of _PORTABLE_FIELDS, plus enabled/category),
        snapshots the pre-update state as the *new* version, and bumps
        Skill.version. Unknown keys in `patch` are ignored."""
        async with get_session() as session:
            row = await session.get(Skill, skill_id)
            if row is None:
                return None

            allowed = set(_PORTABLE_FIELDS) | {"enabled"}
            changed = False
            for key, value in patch.items():
                if key in allowed and getattr(row, key, object()) != value:
                    setattr(row, key, value)
                    changed = True

            if changed:
                row.version += 1
            await session.flush()
            await session.refresh(row)
            skill_dict = self._to_dict(row)

            if changed:
                session.add(
                    SkillVersion(
                        skill_id=row.id,
                        version=row.version,
                        snapshot_json={k: skill_dict[k] for k in _PORTABLE_FIELDS},
                        change_note=change_note,
                    )
                )

        await self._reindex(skill_dict)
        return skill_dict

    async def rename(self, skill_id: str, new_name: str) -> Optional[dict[str, Any]]:
        return await self.update(skill_id, {"name": new_name}, change_note=f"renamed to '{new_name}'")

    async def delete(self, skill_id: str) -> bool:
        async with get_session() as session:
            row = await session.get(Skill, skill_id)
            if row is None:
                return False
            await session.delete(row)
        try:
            self._collection.delete(ids=[skill_id])
        except Exception:
            logger.debug("Chroma delete for skill %s failed (may not have been indexed)", skill_id)
        self._pending = {tid: d for tid, d in self._pending.items() if d.get("source_skill_id") != skill_id}
        return True

    async def duplicate(self, skill_id: str, new_name: Optional[str] = None) -> Optional[dict[str, Any]]:
        original = await self.get(skill_id)
        if original is None:
            return None
        return await self.create(
            name=new_name or f"{original['name']} (copy)",
            description=original["description"],
            category=original["category"],
            trigger=original["trigger"],
            variables=original["variables"],
            workflow=original["workflow"],
            success_condition=original["success_condition"],
            required_plugins=original["required_plugins"],
            required_browser=original["required_browser"],
            website_hint=original["website_hint"],
            source=SkillSource.MANUAL,
            enabled=original["enabled"],
        )

    async def set_enabled(self, skill_id: str, enabled: bool) -> Optional[dict[str, Any]]:
        return await self.update(skill_id, {"enabled": enabled}, change_note=("enabled" if enabled else "disabled"))

    # ------------------------------------------------------------------ #
    # Usage tracking
    # ------------------------------------------------------------------ #
    async def record_usage(self, skill_id: str, success: bool) -> None:
        async with get_session() as session:
            row = await session.get(Skill, skill_id)
            if row is None:
                return
            prior_total = row.success_rate * row.usage_count
            row.usage_count += 1
            row.success_rate = (prior_total + (1.0 if success else 0.0)) / row.usage_count
            row.last_used_at = _now()

    # ------------------------------------------------------------------ #
    # Version history
    # ------------------------------------------------------------------ #
    async def versions(self, skill_id: str) -> list[dict[str, Any]]:
        async with get_session() as session:
            result = await session.execute(
                select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(SkillVersion.version.desc())
            )
            rows = list(result.scalars().all())
        return [
            {
                "id": v.id,
                "skill_id": v.skill_id,
                "version": v.version,
                "snapshot": v.snapshot_json,
                "change_note": v.change_note,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in rows
        ]

    async def rollback(self, skill_id: str, to_version: int) -> Optional[dict[str, Any]]:
        async with get_session() as session:
            result = await session.execute(
                select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version == to_version)
            )
            version_row = result.scalar_one_or_none()
        if version_row is None:
            return None
        return await self.update(skill_id, version_row.snapshot_json, change_note=f"rolled back to v{to_version}")

    # ------------------------------------------------------------------ #
    # Import / export / share
    # ------------------------------------------------------------------ #
    async def export_skill(self, skill_id: str) -> Optional[dict[str, Any]]:
        skill = await self.get(skill_id)
        if skill is None:
            return None
        return {"nexus_skill_export": 1, **{k: skill[k] for k in _PORTABLE_FIELDS}}

    async def share_code(self, skill_id: str) -> Optional[str]:
        """Base64-encoded export, small enough to paste into chat/Telegram
        or a text file -- the counterpart to import_from_code()."""
        payload = await self.export_skill(skill_id)
        if payload is None:
            return None
        return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    async def import_skill(
        self, payload: dict[str, Any], source: SkillSource = SkillSource.IMPORTED
    ) -> dict[str, Any]:
        """Accepts either a full export payload ({"nexus_skill_export": 1, ...})
        or a bare {"name", "workflow", ...} dict -- e.g. a "recorded
        workflow" JSON produced by some other tool -- and creates a new
        Skill from it. Missing fields default sensibly."""
        return await self.create(
            name=payload.get("name") or "Imported skill",
            description=payload.get("description", ""),
            category=payload.get("category", "general"),
            trigger=payload.get("trigger", ""),
            variables=payload.get("variables") or [],
            workflow=payload.get("workflow") or [],
            success_condition=payload.get("success_condition"),
            required_plugins=payload.get("required_plugins") or [],
            required_browser=payload.get("required_browser"),
            website_hint=payload.get("website_hint"),
            source=source,
        )

    async def import_from_code(self, code: str) -> dict[str, Any]:
        try:
            payload = json.loads(base64.urlsafe_b64decode(code.encode("ascii")).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid share code: {exc}") from exc
        return await self.import_skill(payload, source=SkillSource.IMPORTED)

    async def import_recorded_workflow(
        self, name: str, steps: list[dict[str, Any]], description: str = "", category: str = "general", trigger: str = ""
    ) -> dict[str, Any]:
        """Learn from a recorded workflow: a plain list of {action, target,
        value, description} step dicts (e.g. exported from a prior task's
        steps, or hand-written), with no export envelope required."""
        workflow = [
            {
                "action": s.get("action", "click"),
                "target": s.get("target", ""),
                "value": s.get("value", ""),
                "description": s.get("description", ""),
            }
            for s in steps
        ]
        return await self.create(
            name=name,
            description=description,
            category=category,
            trigger=trigger,
            workflow=workflow,
            source=SkillSource.RECORDED_WORKFLOW,
        )

    # ------------------------------------------------------------------ #
    # Pending "save as skill?" suggestions
    # ------------------------------------------------------------------ #
    def register_pending(self, task_id: str, draft: dict[str, Any]) -> None:
        self._pending[task_id] = draft
        self._pending_order.append(task_id)
        # Keep this bounded -- it's a UX convenience, not a durable queue.
        while len(self._pending_order) > 50:
            stale = self._pending_order.pop(0)
            self._pending.pop(stale, None)

    def list_pending(self) -> list[dict[str, Any]]:
        return [{"task_id": tid, **self._pending[tid]} for tid in self._pending_order if tid in self._pending]

    def get_pending(self, task_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        if task_id and task_id in self._pending:
            return {"task_id": task_id, **self._pending[task_id]}
        if self._pending_order:
            latest = self._pending_order[-1]
            return {"task_id": latest, **self._pending[latest]}
        return None

    def discard_pending(self, task_id: Optional[str] = None) -> bool:
        target = task_id
        if target is None and self._pending_order:
            target = self._pending_order[-1]
        if target is None or target not in self._pending:
            return False
        self._pending.pop(target, None)
        self._pending_order = [t for t in self._pending_order if t != target]
        return True

    async def confirm_pending(self, task_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        draft = self.get_pending(task_id)
        if draft is None:
            return None
        skill = await self.create(
            name=draft.get("name") or f"Skill from task {draft['task_id'][:8]}",
            description=draft.get("description", ""),
            category=draft.get("category", "general"),
            trigger=draft.get("trigger", ""),
            workflow=draft.get("workflow") or [],
            website_hint=draft.get("website_hint"),
            source=SkillSource.TASK_OUTCOME,
        )
        self.discard_pending(draft["task_id"])
        return skill

    # ------------------------------------------------------------------ #
    # Semantic index (used by backend.skills.matcher.SkillMatcher)
    # ------------------------------------------------------------------ #
    async def _reindex(self, skill: dict[str, Any]) -> None:
        if not skill.get("enabled", True):
            try:
                self._collection.delete(ids=[skill["id"]])
            except Exception:
                pass
            return
        doc = (
            f"skill: {skill['name']}. category: {skill['category']}. "
            f"triggers: {skill['trigger']}. description: {skill['description']}"
        )
        try:
            await asyncio.to_thread(
                self._collection.upsert,
                ids=[skill["id"]],
                documents=[doc],
                metadatas=[{"name": skill["name"], "category": skill["category"], "enabled": skill["enabled"]}],
            )
        except Exception:
            logger.exception("Failed to index skill %s for matching", skill["id"])

    async def semantic_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        try:
            results = await asyncio.to_thread(self._collection.query, query_texts=[query], n_results=top_k)
        except Exception:
            logger.exception("Skill semantic search failed")
            return []
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0] if results.get("distances") else [None] * len(ids)
        out = []
        for skill_id, distance in zip(ids, distances):
            # Chroma's default space is squared-L2 on normalized embeddings;
            # treat smaller distance as higher similarity, clamp to [0, 1].
            score = max(0.0, 1.0 - (distance / 2.0)) if distance is not None else 0.0
            out.append({"skill_id": skill_id, "score": score})
        return out

    async def rebuild_index(self) -> int:
        """Re-embeds every skill -- useful after a manual DB edit or migration."""
        skills = await self.list()
        for s in skills:
            await self._reindex(s)
        return len(skills)
