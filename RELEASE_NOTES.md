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
