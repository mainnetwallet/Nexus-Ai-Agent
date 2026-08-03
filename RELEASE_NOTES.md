# Nexus-Agent (Unreleased) — Single Task Control (pause/resume/cancel one task, everywhere)

Pausing/resuming/cancelling one specific task (not the whole agent) already
worked from the Dashboard and REST API. Chat and Telegram could only
pause/resume the entire worker, and neither had a "cancel one task" concept
at all. This release adds that, reusing the task-queue methods and REST
endpoints that already existed rather than building a second control path.

## What changed

- Chat: say "pause task", "resume task", or "cancel task" to control
  whichever task is currently running, or add an id — "pause task
  <task_id>" — to target a specific one. Plain "pause"/"resume" (no task
  named) still control the whole agent worker exactly as before.
- Telegram: `/pause [task_id]`, `/resume [task_id]`, and a new
  `/cancel [task_id]` command, plus the same phrasing works as free text.
- Dashboard and REST API (`POST /api/tasks/{id}/pause|resume|cancel`) are
  unchanged — they already supported this and now share the exact same
  underlying task-queue logic as Chat and Telegram.
- Pausing a task stops it after its current step and preserves progress;
  resuming continues from the next step rather than starting over. This
  behavior already existed and needed no changes.

## Also fixed

Two pre-existing test bugs found while validating this change, unrelated
to task control: a vision-request test that didn't set a test API key,
and a cross-test state leak in the LLM client's rate-limit fallback cache
that made an unrelated test's outcome depend on run order. Full backend
suite: 381 passed. Frontend build (`tsc -b && vite build`) is clean.

## Result

- Same single-task pause/resume/cancel behavior is now available from
  Chat, Telegram, Dashboard, and the REST API — one source of truth
  (`TaskQueueService`) behind all four.

# Nexus-Agent (Unreleased) — AI Model Manager integration fix

The AI Model Manager (manual switching, smart routing, cross-provider
fallback, temporary overrides — see the entry below) was built as a
standalone layer but several core execution paths bypassed it and
called `LLMClient()` directly: `agent_loop`, `decision_engine`,
`vision_engine`, Teach Mode (`teach.py`), the Telegram bot, and
`chat_engine`'s classifier/reply calls. Those paths only ever used the
single provider in `settings.llm_provider`, with no cross-provider
fallback — so a missing/invalid/rate-limited key on that one provider
broke the agent even with other valid keys configured, and switching
providers from Settings/AI Models/chat didn't change what these paths
actually used.

## What changed

- All six core call sites now default to the existing `model_manager`
  singleton instead of constructing their own `LLMClient()`. This is a
  wiring change only — no new modules, no architecture change.
  `LLMClient` stays exactly what it was: a single-provider HTTP
  implementation that `ModelManager` dispatches to.
- The planning call in `decision_engine.py`, the three parse calls in
  `teach.py`, the Telegram intent classifier, and `chat_engine.py`'s
  classifier + reply calls now pass an explicit `task_type` so Smart
  Routing has something to route on at every call site, not just vision.
- `LLMClient.complete_text/complete_json/complete_json_with_image` grew
  an optional, ignored `task_type` kwarg so the call signature is
  identical whether `self.llm` is a raw `LLMClient` (tests, explicit
  injection) or the `ModelManager` singleton.

## Result

- Manual switching, smart routing, cross-provider fallback, and
  temporary "use this provider once" overrides now apply uniformly
  across agent runs, Teach Mode, Telegram, and chat — not just the
  Settings/AI Models API surface.
- Fallback scope is exactly as many providers as have a configured
  API key in `.env`: one key configured -> that provider only; several
  keys configured -> automatic fallback across all of them, everywhere
  in the codebase.

## Compatibility

Fully backward compatible. Any code that explicitly injects its own
`llm=LLMClient(...)` (all of the existing test suite does this) is
completely unaffected — the change only touches each module's
*default* when no `llm` is passed in.

## Verified

- Backend: `uvicorn backend.main:app` boots clean, no import errors,
  no circular imports.
- Frontend: `tsc -b && vite build` clean; `vite` dev server serves `200`.
- Full backend test suite: **365 passed**, 0 failed.

---

# Nexus-Agent (Unreleased) — AI Model Manager

Adds a production-ready **AI Model Manager** on top of the existing
`LLMClient`: manual provider switching, automatic smart routing by task
type, per-provider health monitoring, automatic cross-provider fallback,
and "use this provider for one task only" temporary overrides — usable
from Chat, Settings, a new AI Models dashboard page, and the REST API.

## What changed

- **20 providers, one interface**: `LLMProvider` now covers Anthropic,
  OpenAI, Gemini, OpenRouter, xAI (Grok), Moonshot (Kimi), Alibaba Qwen,
  Zhipu (GLM), Groq, Cerebras, Cohere, Hugging Face, NVIDIA NIM,
  SambaNova, Together AI, Fireworks AI, DeepInfra, Mistral AI, Replicate,
  and AI21 Labs. 16 of them share one generic OpenAI-compatible request
  builder in `llm_client.py`, so adding the next provider is a ~4-line
  change (enum member, API key field, `DEFAULT_MODELS` entry,
  `OPENAI_COMPATIBLE_PROVIDERS` entry).
- **`ModelManager`** (`backend/planner/model_manager.py`, singleton
  `model_manager`): the brain of the feature —
  - Manual switch/set-default (mirrors `settings.llm_provider` so every
    existing `LLMClient()` call site picks it up automatically).
  - Smart routing: a `TaskType` -> provider table (coding, browser
    automation, planning, vision, long context, fast response, general
    chat, research, reasoning, low cost), toggled on/off, fully
    reconfigurable, persisted to `data/ai_model_manager.json`.
  - Temporary override: "use Claude for this task only" wins over both
    manual and auto-routed resolution for exactly the next call, then
    clears itself.
  - Health: per-provider status/connection/latency/last success/last
    error/availability/rate-limit window, updated on every real call and
    on-demand via "Test Provider Connection".
  - Fallback: on timeout, HTTP error, rate limit, or an unparsable
    response, `complete_text()`/`complete_json()`/
    `complete_json_with_image()` retry through an ordered chain (explicit
    fallback provider, then priority list, then anything else available),
    skipping disabled providers and providers with no API key configured.
- **Chat commands**: "switch to Claude", "set Gemini as default", "use
  automatic routing", "use Claude for coding", "use Groq for this request
  only", "show current provider/model/health/routing" all work as
  natural-language chat messages, dispatched through a new `ai_model`
  classifier category in `chat_engine.py`.
- **REST API**: `GET /api/ai-models` (full view), `GET /api/ai-models/health`,
  `PUT /api/ai-models/routing-rules` (+ `/one`), `POST /api/ai-models/switch`,
  `/routing-mode`, `/fallback`, `/priority`, `/enable`, `/disable`,
  `/override` (+ `DELETE` to clear), and `/test/{provider}`.
- **Settings**: existing `GET/PATCH /api/settings` grew
  `ai_smart_routing_enabled` and `ai_fallback_provider`, routed through
  `ModelManager` so the two surfaces never drift.
- **Dashboard**: new **AI Models** page — current provider/model/routing
  mode/fallback at a glance, a routing-mode switch, default/fallback
  provider pickers, one select per task-type routing rule, and a provider
  table with API-key/health badges, enable/disable, connection testing,
  and a one-click "use once" temporary override. A compact card on the
  **Settings** page covers the essentials and links to the full page.
- **Tests**: 50 new tests across `test_model_manager.py`, additions to
  `test_llm_client.py`, `test_routes_ai_models.py` (every `/api/ai-models/*`
  endpoint), `test_routes_settings.py` (the `/api/settings` <->
  `ModelManager` integration), and `test_chat_engine_ai_models.py` (every
  chat `ai_model` command) — covering routing resolution, fallback-chain
  construction, health tracking, cross-provider fallback, override
  auto-clearing, and free-text provider/task-type parsing from chat
  messages. Full suite: **365 passed** (315 pre-existing + 50 new).
  Frontend: `tsc --noEmit`, `oxlint`, and `vite build` all clean.

## Compatibility

Fully additive — `LLMClient(provider=None)` still reads
`settings.llm_provider` exactly as before, so the planner, chat
classifier, and vision fallback needed zero changes. Existing
Anthropic/OpenAI/OpenRouter/Gemini request builders and their tests are
untouched; only the *dispatch* for the 16 new providers is new code.

## Known limitation

Replicate does not expose its OpenAI-compatible chat endpoint for every
model — pick a Replicate model that supports it, or route that task type
to a different provider via a routing rule.

---

# Nexus-Agent (Unreleased) — Social MCP connectors (X, Discord, Gmail)

Adds three new MCP connectors — **X**, **Discord**, **Gmail** — on a shared
new base, `backend/mcp/connectors/social_base.py`. None of them use a
REST/OAuth client or store an API key, bot token, or password: every tool
drives the same live, already-authenticated `BrowserEngine` session a
task/profile already has open (the same session the Identity/Profile
Manager's `SessionDetector` probes). If a session isn't authenticated, the
connector never fills in a login form — it raises `SessionRequiredError`
telling the caller to log in manually.

## What changed

- **Shared base** (`social_base.py`): lazy `engine_provider` resolution
  (always reflects whichever profile's session is currently live),
  `_ensure_session()`/`_detect_state()` wrapping `SessionDetector`,
  `require_confirm()` — the shared confirmation gate for irreversible,
  outward-facing actions — and `status_snapshot()` for the dashboard.
- **X connector**: `detect_login_state`, `read_profile`,
  `read_notifications`, `draft_post`, `publish_post`, `reply`.
  `publish_post`/`reply` require `confirm=true` — call `draft_post` first,
  show the user the exact text, only send once they approve.
- **Discord connector**: `detect_login_state`, `list_servers`,
  `list_channels`, `read_channel`, `send_message`, `reply`, `upload_file`.
- **Gmail connector**: `detect_login_state`, `read_inbox`,
  `search_emails`, `draft_email`, `send_email`, `reply`. `send_email`/
  `reply` require `confirm=true`, same pattern as X.
- **MCP manager/registry**: `BUILTIN_CONNECTORS` grew from 4 to 7 entries;
  new `SOCIAL_CONNECTOR_NAMES`; `wire_browser_engine_provider()`
  generalized to wire every social connector, not just `browser`; new
  `MCPManager.social_status()`.
- **Settings**: `mcp_x_enabled` / `mcp_discord_enabled` /
  `mcp_gmail_enabled` (default `True`) plus display-only account label
  settings — never credentials.
- **REST API**: new `GET /api/mcp/social-status`.
- **Dashboard**: new `SocialConnectorsPanel` on the MCP page (connection
  status, session status, account, last used) plus config summaries for
  x/discord/gmail.
- **Documentation**: added a "Social MCP connectors" bullet + status-table
  row to `README.md`, a repo-layout note, and this release-notes entry.

## Release checklist (all green)

- [x] Backend tests — 315/315 passing (`pytest backend/tests`)
- [x] Backend boots — verified via `TestClient`: all 7 connectors
      (filesystem/terminal/browser/github/x/discord/gmail) enable with no
      import errors; `GET /api/mcp/social-status` responds
- [x] Frontend build — `tsc -b && vite build` succeeds, no type errors
- [x] Backward compatibility — filesystem/terminal/browser/github
      connectors and their existing tests are unchanged

## Upgrade notes

No schema changes. Three new settings flags default to `True` (enabled),
so `x`/`discord`/`gmail` connectors start up automatically on existing
deployments — set `MCP_X_ENABLED=false` (etc.) to opt out. No new
required dependencies (reuses the existing `BrowserEngine`/Playwright
stack). `publish_post`, X's `reply`, `send_email`, and Gmail's `reply` all
require an explicit `confirm=true` argument — any existing caller that
was calling these blind will now get an `MCPToolError` until it's updated
to pass `confirm=true` after showing the user the draft.

---

# Nexus-Agent (Unreleased) — Identity & Profile Manager

Adds `backend/identity/` — the Identity & Profile Manager: named, reusable
Chrome browser profiles with their own persistent, on-disk user-data
directories, wallet/Gmail/X/Discord account linking, best-effort login-state
detection, and a `Profiles` dashboard page. A task can now run as a specific
profile and reuse that identity's cookies, local storage, session storage,
and extensions instead of starting logged out every run.

## What changed

- **Profile core** (`backend/identity/`): `ProfileFilesystem` (create/
  delete/clone/inspect a profile's Chrome directory), `SessionDetector`
  (read-only Gmail/X/Discord login detection), `ProfileRegistry` (CRUD,
  search/tag filtering, clone, metadata-only export/import,
  enable/disable, single-active-profile selection, activity log), and
  `ProfileManager` (the `load_for_task`/`check_sessions`/`release` facade).
- **Task queue integration**: `TaskQueueService` resolves a task's
  `profile_label` before launching the browser — a bad reference fails
  fast without starting `BrowserEngine` — and computes an
  `effective_wallet_label` that prefers an explicit task-level wallet over
  the profile's. Fully backward compatible: no `profile_label` (or no
  `ProfileManager` configured) behaves exactly as before.
- **REST API**: new `/api/profiles` surface (CRUD, clone/export/import,
  enable/disable/select, session status + manual re-check, filesystem
  inspection, activity log).
- **Dashboard**: new **Profiles** page.
- **Test coverage**: 5 new test files, 54 tests total
  (`test_profile_registry.py` 25, `test_profile_fs.py` 8,
  `test_profile_manager.py` 7, `test_task_queue_profile.py` 5,
  `test_routes_profiles.py` 9) — see `CHANGELOG.md` for the full
  breakdown.
- **Documentation**: added an "Identity & Profile Manager" section to
  `README.md` (module breakdown matching the existing Phase 2
  write-ups), a repo-layout entry, a Profiles dashboard-page bullet, and
  marked the Chrome Profile Manager row in the Phase 2 progress table done.

## Release checklist (all green)

- [x] Backend tests — 312/312 passing (`pytest backend/tests`), re-verified
      against a freshly recreated venv
- [x] Frontend build — `tsc -b && vite build` succeeds, no type errors
      (verified in the session that added the Profiles page; unchanged
      since)
- [x] Frontend lint — `oxlint src` clean (0 errors)
- [x] Backward compatibility — a task with no `profile_label` is
      unaffected; a `profile_label` with no `ProfileManager` configured is
      accepted but ignored rather than erroring

## Upgrade notes

None required. No schema-breaking changes (the `Task`/`ProfileRecord`/
`ProfileActivity` tables are additive), no new required dependencies, no
new settings — the Identity & Profile Manager is wired unconditionally and
has no `_ENABLED` flag to configure.

---

# Nexus-Agent (Unreleased) — MCP Core test coverage + validation pass

A test-coverage-and-validation pass focused on the existing MCP Core
(`backend/mcp/` — registry, router, discovery, manager, client, and the
filesystem/terminal/browser/github connectors — plus its Chat/Telegram/
AgentLoop/Skills/Memory/dashboard integrations). No new features, no
architecture changes. One real bug fix and one small testability
extension.

## What changed

- **Test coverage**: 11 new test files (`test_mcp_registry.py`,
  `test_mcp_router.py`, `test_mcp_manager.py`,
  `test_mcp_filesystem_connector.py`, `test_mcp_terminal_connector.py`,
  `test_mcp_browser_connector.py`, `test_mcp_github_connector.py`,
  `test_chat_engine_mcp.py`, `test_memory_store_mcp.py`,
  `test_agent_loop_mcp_tool.py`, `test_telegram_mcp_command.py`) covering
  connector sandboxing/safety, router scoring, manager routing and
  callback semantics, and every integration point MCP Core touches. See
  `CHANGELOG.md` for the full breakdown.
- **Bug fix — `mcp_enabled` master switch**: `MCP_ENABLED=false` previously
  had no effect (`MCPManager.start()`'s enable check always fell through to
  its default). Fixed so the switch actually gates connector startup;
  `state.mcp` still stays a real object either way, so the `/mcp` API and
  dashboard keep responding when disabled.
- **Dead code**: removed one unused import in `backend/mcp/client.py`,
  confirmed clean otherwise via an AST-based scan of `backend/mcp/`.
- **Testability**: `MemoryStore` now accepts an optional injectable
  `embedding_function`, so tests don't need network access to download
  chromadb's default embedding model.
- **Documentation**: added an "MCP Core" section to `README.md` (module
  breakdown matching the existing Phase 2 write-ups), a repo-layout entry,
  an MCP dashboard-page bullet, a Phase 2 progress table row, and updated
  the stale `MCP_ENABLED` comment in `.env.example`.

Everything else in MCP Core — the four connectors, routing, the `/mcp`
dashboard page, and the Chat/Telegram/`AgentLoop`/Skills/Memory
integrations — was reviewed and found already correct; no changes were
needed beyond the fix above.

## Release checklist (all green)

- [x] Backend tests — 258/258 passing (`pytest backend/tests`), up from 176,
      run against real dependencies (not just static review)
- [x] Frontend build — `tsc -b && vite build` succeeds, no type errors
- [x] Frontend lint — `oxlint src` clean (0 errors)
- [x] `mcp_enabled` master switch fixed and covered by regression tests
- [x] Reviewed `Mcp.tsx` and related frontend files — no dead/duplicate code

## Upgrade notes

None required. No schema changes, no new dependencies. `MemoryStore()`
continues to work with no arguments; the new `embedding_function` parameter
is optional and unused in production.

---

# Nexus-Agent (Unreleased) — Skill Learning System hardening pass

A validation-and-hardening pass focused entirely on the existing Skill
Learning System (`backend/skills/` — Skill Library, matcher, runner, Teach
Mode — plus its Chat/Telegram/Planner/API/dashboard integrations). No new
features, no architecture changes, no breaking changes to the API, schema,
or plugin interface.

## What changed

- **Test coverage**: `SkillRunner` (deterministic workflow replay against
  `BrowserEngine`) had no direct tests — added `backend/tests/
  test_skill_runner.py` (9 tests) covering replay, variable substitution/
  overrides, failure short-circuiting, navigation failure, unknown actions,
  and exception safety.
- **Telegram bug fix**: `on_free_text` was routing the `"unknown"` intent
  (reserved for gibberish/empty input) through the same LLM-backed chat path
  as `"chat"`, instead of the deterministic help-hint reply its own
  classification contract promises — this made one test dependent on live
  network access. Fixed; see `CHANGELOG.md` for detail.
- **Documentation**: added a "Skill Learning System" section to `README.md`
  (module breakdown matching the existing Phase 2 write-ups), a repo-layout
  entry, a Skills dashboard-page bullet, and documented the
  `SKILLS_ENABLED` / `SKILLS_MATCH_MIN_SCORE` settings in `.env.example`.

Everything else in the Skill Learning System — Skill Library CRUD/
versioning/import-export, the matcher's keyword+semantic passes, Teach Mode,
natural-language authoring, correction parsing, the full `/api/skills`
surface, and the Chat/Telegram/`TaskQueueService` integrations — was
reviewed and found already correct; no changes were needed.

## Release checklist (all green)

- [x] Backend tests — 176/176 passing (`pytest backend/tests`), up from 167
- [x] Frontend build — `tsc -b && vite build` succeeds, no type errors
- [x] Frontend lint — `oxlint src` clean (0 errors)
- [x] Backward compatibility — `SkillService`/`SkillMatcher` remain
      `Optional` at every integration point; `SKILLS_ENABLED=false` fully
      restores pre-Skill-System behavior

## Upgrade notes

None required. No schema changes, no new dependencies.

---

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
