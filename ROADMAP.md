# Roadmap

This roadmap tracks the direction of Nexus-Agent across releases. It is updated
after every release to reflect what shipped and to re-plan what's next.

Status legend: ✅ Done · 🚧 In Progress · ⏳ Planned · 🔭 Vision (not yet scoped in detail)

---

## v1.0 — Completed ✅

**Theme:** Stable, production-hardened first release of the full-stack autonomous browser agent.

### Goals
- Ship a working, tested, generic (no site-specific code) browser automation agent end to end.
- Establish the core architecture as the permanent baseline for future releases.

### Major Features
- FastAPI backend + React/Vite frontend, full-stack integration.
- Playwright-driven browser automation with an LLM planner/decision loop
  (`backend/planner`, `DecisionEngine`: perceive → decide → verify → recover).
- Vision fallback stack: Tesseract OCR + vision-LLM fallback (`backend/vision`).
- Persistent memory store for prior successful workflows (`backend/memory`, ChromaDB).
- Wallet manager with plugin-based popup handling (`backend/wallet`).
- Plugin framework with dynamic loading, registry, lifecycle hooks, and live event stream (`backend/plugins`).
- Task Scheduler: queued execution with pause/resume/retry/cancel/priority/history/scheduled-for (`backend/planner/task_queue.py`).
- Live WebSocket layer: task progress, browser frames, live logs, plugin events.
- Telegram bot front-end for remote control (`backend/telegram`).
- React dashboard: Settings page, live browser viewer.
- JWT auth enforced on every API route group.

### Improvements
- Full repository production-hardening pass: 43/43 backend modules import cleanly, 75/75 backend tests passing, clean frontend build/lint, no hardcoded secrets, closed-by-default CORS.
- Silent exception handlers converted to debug-level logging for production observability.
- Dependency audit: every `requirements.txt` / `package.json` entry confirmed in use.
- Docker image dependency versions (e.g. Playwright) verified consistent with `requirements.txt`.

### Breaking Changes
- None. v1.0 was a hardening pass on the existing Phase 2 feature set — no API, schema, or plugin-interface changes.

---

## v1.1 — Planned ⏳

**Theme:** Finish the remaining Phase 2 backlog and close out autonomous-operation gaps.

### Goals
- Complete the items from the Phase 2 plan that were not yet in v1.0.
- Make the agent runtime fully autonomous and self-recovering across restarts.

### Major Features
- **Autonomous Agent Runtime**: single Start/Stop/Pause/Resume lifecycle for the
  agent as a whole, with interrupted-task recovery on restart and browser-crash
  retry (building on `backend/planner/agent_runtime.py`), plus the `/api/agent`
  REST + WebSocket surface and the frontend Agent dashboard.
- **AI model switching**: allow the planner/decision engine to switch between
  configured LLM providers/models without a restart.
- **Chrome Profile Manager**: manage multiple persistent browser profiles
  (cookies/sessions) instead of a single implicit profile.
- **Memory improvements**: explicit failed-workflow save (not just successes)
  and a reuse-ranking mechanism so the planner prefers previously successful
  approaches over cold starts.

### Improvements
- Expand test coverage for the new agent runtime, model-switching, and memory paths.
- Update `docs/ARCHITECTURE.md` with the finalized autonomous-runtime data flow.

### Breaking Changes
- None expected. All items extend existing modules (`planner`, `wallet`, `memory`,
  `browser`) per the standing backward-compatibility rule.

---

## v1.2 — Planned ⏳

**Theme:** Reliability, performance, and operational maturity.

### Goals
- Harden the system for longer unattended runs and heavier task volumes.
- Improve visibility and control for operators running the agent in production.

### Major Features
- Expanded plugin catalog (additional notification/integration plugins beyond Telegram/Discord).
- Structured observability: metrics/tracing hooks around the decision loop and task queue, surfaced in the dashboard.
- Configurable retry/backoff and rate-limit policies for browser automation and LLM calls.
- Multi-task concurrency controls (safe parallel task execution where site/session isolation allows it).

### Improvements
- Performance profiling and optimization of the perceive/decide/verify loop.
- Expanded automated test suite (load/integration tests, not just unit tests).
- Documentation pass across `README.md`, `docs/`, and plugin-authoring guides.

### Breaking Changes
- None expected, pending final scoping. Any schema change required for concurrency
  controls will ship with a migration and be called out explicitly here before release.

---

## v2.0 — Vision 🔭

**Theme:** Multi-agent, extensible platform.

### Goals
- Evolve from a single-agent tool into a platform capable of coordinating multiple
  agents and richer integrations, while preserving the "no site-specific logic" core principle.

### Major Features (directional, not yet scoped)
- Multi-agent orchestration: run and coordinate multiple concurrent agent instances
  against different goals/sites from a single control plane.
- Plugin marketplace / distribution mechanism for community-built plugins.
- Expanded wallet/chain support and richer on-chain action verification.
- Advanced memory: cross-agent shared knowledge base of successful workflows.

### Improvements
- Revisit core architecture decisions only at this stage, with an explicit
  migration plan, given v1.x's freeze on architecture changes.

### Breaking Changes
- Likely, given the scope (multi-agent coordination may require schema and API
  changes). Any breaking change will be scoped, documented, and versioned
  according to semantic versioning before implementation begins.

---

## Maintenance Note

This roadmap is updated after every release:
- Move shipped items from "Planned" to "Completed" with an accurate feature list.
- Re-scope the next "Planned" version based on what actually shipped.
- Keep this file consistent with `CHANGELOG.md` and `RELEASE_NOTES.md`.
