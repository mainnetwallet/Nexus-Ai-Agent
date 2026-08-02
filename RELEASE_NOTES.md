# Nexus-Agent v1.1.0 (Unreleased) — System Monitoring & Telegram AI Chat

Adds an operational layer on top of the v1.0 agent — health dashboard, on-demand
diagnostics, resource monitor, configuration backup/restore, and build/version
info — and upgrades the Telegram bot from a fixed command set into a full
conversational interface. No breaking changes to the API, schema, or plugin
interface.

## What's new

- **Health Dashboard** (`GET /api/system/health`, System page in the UI):
  live status of backend, database, browser, memory store, AI provider,
  Telegram, and the WebSocket layer.
- **Diagnostics** (`GET /api/system/diagnostics`): on-demand environment check
  covering Playwright, the AI API key, database connectivity, plugin
  discovery, memory store initialization, and required env vars — with both
  a JSON and a plain-text report.
- **Resource Monitor** (`GET /api/system/resources`): CPU, process/system RAM,
  an estimate of browser (Chromium) memory, queue depth, and active task count.
- **Configuration Manager** (`/api/system/config/*`): export, import, backup,
  and restore for the same non-secret settings already editable from the
  dashboard — secrets are never included.
- **Build info** (`GET /api/system/version`): current commit, branch, and
  nearest tag, read from local git metadata.
- **Telegram AI chat**: `/health`, `/diagnostics`, `/resources`, and
  `/restart` commands, plus a much broader natural-language vocabulary — the
  bot now understands things like "how's everything doing?", "restart the
  agent", or "give me a report" without needing the exact command syntax.
  `/status`, `/report`, `/tasks`, and `/browser` now answer with real data
  instead of pointing you at the REST API.

## Release checklist (all green)

- [x] Backend tests — 115/115 passing (`pytest backend/tests`)
- [x] Frontend build — `tsc -b && vite build` succeeds, no type errors
- [x] Frontend lint — `oxlint` clean (0 errors)
- [x] Config export/import verified to never include secret fields
      (explicit test assertion)

## Upgrade notes

None required. `psutil` was added to `requirements.txt` for the resource
monitor — run `pip install -r requirements.txt` (or rebuild the Docker image)
before deploying this version.

---

# Nexus-Agent v1.0.0 — Release Notes

Nexus-Agent's first stable release. This release is a hardening pass on top
of the existing Phase 2 feature set — no new features, no architecture
changes, no breaking changes to the API or plugin interface.

## What's in v1.0

Full-stack AI browser agent: FastAPI backend + React/Vite frontend, driving
a Playwright browser via an LLM planner/decision loop, with vision (OCR +
vision-model fallback), a persistent memory store, a wallet manager with
plugin-based popup handling, a plugin framework (Discord/Telegram/etc.),
scheduled/queued task execution with pause/resume/retry/cancel, a live
WebSocket browser viewer, and a Telegram bot front-end.

## Release checklist (all green)

- [x] Backend imports cleanly — 43/43 modules, no circular or broken imports
- [x] Backend tests — 75/75 passing (`pytest backend/tests`)
- [x] Frontend build — `tsc -b && vite build` succeeds, no type errors
- [x] Frontend lint — `oxlint` clean (0 errors)
- [x] Security review — no hardcoded secrets, every REST route group behind
      `require_auth`, CORS defaults closed unless explicitly configured
- [x] Dependency audit — every entry in `requirements.txt` and
      `frontend/package.json` confirmed in use
- [x] Docker image / pinned dependency versions consistent (Playwright)

## Fixed since the initial push

- Several exception handlers that silently discarded errors now log them at
  debug level, so failures during browser automation and live-session
  cleanup are visible in production logs instead of disappearing.
- Removed unused imports and unused test variables flagged by `ruff`.
- Removed a recurring pytest-asyncio deprecation warning.

See `CHANGELOG.md` for the full itemized history, including all Phase 2
feature work this release builds on.

## Upgrade notes

None. This release has no config, schema, or API changes — deploy exactly as
described in `README.md`.
