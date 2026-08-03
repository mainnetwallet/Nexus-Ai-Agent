"""
Configuration Manager.

Export/import/backup/restore for the subset of runtime settings that are
safe to persist and share (the same non-secret surface already exposed by
backend/api/routes_settings.py). Secrets (API keys, tokens, auth token)
are never included in an export -- restoring a config file only ever
touches the same safe fields routes_settings.update_settings() accepts.

Backups are timestamped JSON snapshots written under DATA_DIR/config_backups
so a bad import can always be rolled back.
"""
from __future__ import annotations

import datetime as dt
import json
from enum import Enum
from pathlib import Path
from typing import Any

from backend.config.settings import DATA_DIR, settings

BACKUP_DIR = DATA_DIR / "config_backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Keys that are safe to export/import/backup -- mirrors SettingsView /
# SettingsUpdateRequest in backend/api/routes_settings.py. Never includes
# api_auth_token, telegram_bot_token, or any *_api_key field.
EXPORTABLE_FIELDS = [
    "environment",
    "debug",
    "llm_provider",
    "llm_model_override",
    "browser_channel",
    "browser_headless",
    "browser_slow_mo_ms",
    "browser_default_timeout_ms",
    "wallet_require_manual_approval",
    "wallet_max_auto_approve_value_usd",
    "wallet_allowlisted_contracts",
    "vision_enabled",
    "vision_min_elements_threshold",
    "ocr_enabled",
    "ocr_lang",
    "live_session_enabled",
    "live_session_interval_ms",
    "live_session_jpeg_quality",
]


class ConfigManager:
    @staticmethod
    def export_settings() -> dict[str, Any]:
        data: dict[str, Any] = {}
        for field_name in EXPORTABLE_FIELDS:
            value = getattr(settings, field_name)
            data[field_name] = value.value if hasattr(value, "value") else value
        return {
            "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "app_name": settings.app_name,
            "settings": data,
        }

    @staticmethod
    def import_settings(payload: dict[str, Any]) -> dict[str, Any]:
        """
        Applies an exported settings payload to the in-memory settings
        object (same scope/lifetime as routes_settings.update_settings --
        not persisted to .env). Unknown/non-exportable keys are ignored
        rather than rejected, so older/newer export files stay compatible.

        Enum-typed fields (llm_provider, browser_channel) are re-parsed
        through their enum class rather than assigned as raw strings --
        pydantic BaseSettings does not re-validate plain attribute
        assignment, so assigning a bare string there would silently leave
        the field non-enum for the rest of the process.
        """
        incoming = payload.get("settings", payload)
        applied: dict[str, Any] = {}
        field_types = type(settings).model_fields
        for field_name in EXPORTABLE_FIELDS:
            if field_name not in incoming:
                continue
            value = incoming[field_name]
            annotation = field_types[field_name].annotation if field_name in field_types else None
            if isinstance(annotation, type) and issubclass(annotation, Enum) and not isinstance(value, Enum):
                value = annotation(value)
            setattr(settings, field_name, value)
            applied[field_name] = incoming[field_name]
        return applied

    @classmethod
    def backup(cls) -> Path:
        snapshot = cls.export_settings()
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = BACKUP_DIR / f"config_{timestamp}.json"
        path.write_text(json.dumps(snapshot, indent=2))
        return path

    @staticmethod
    def list_backups() -> list[dict[str, Any]]:
        backups = sorted(BACKUP_DIR.glob("config_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"filename": p.name, "created_at": p.stat().st_mtime} for p in backups]

    @classmethod
    def restore(cls, filename: str) -> dict[str, Any]:
        path = BACKUP_DIR / filename
        if not path.exists() or path.parent != BACKUP_DIR:
            raise FileNotFoundError(f"backup not found: {filename}")
        payload = json.loads(path.read_text())
        return cls.import_settings(payload)
