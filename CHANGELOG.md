# Changelog

All notable changes to Nexus-Agent are documented here. Phase 2 is being delivered
incrementally, one feature at a time; each entry below corresponds to one delivered
increment with passing tests.

## [1.0.0] - v1.0 Production Hardening Pass

Full repository review ahead of the first stable release. No architectural
changes, no new features, no breaking changes — verification plus targeted
fixes only.

### Verified
- All 43 backend modules import cleanly (no circular imports, no broken imports).
- Full backend test suite passes: 75/75 (`pytest backend/tests`).
- Frontend builds cleanly: `tsc -b && vite build` (1893 modules, no errors).
- Frontend lint clean: `oxlint` (0 errors, 1 pre-existing informational warning).
- No hardcoded secrets/keys/tokens in source (`backend`, `frontend/src`).
- Every `backend/api/routes_*.py` module enforces `require_auth`; no
  unauthenticated route groups.
- CORS defaults verified safe (empty + non-debug = no origins allowed).
- Docker image Playwright version (`v1.47.0-jammy`) matches
  `requirements.txt` (`playwright==1.47.0`).
- All `requirements.txt` entries confirmed in use, including indirect ones
  (`aiosqlite` via the `sqlite+aiosqlite://` SQLAlchemy driver string,
  `python-dotenv` via pydantic-settings' `env_file=".env"`).

### Fixed
- `backend/browser/engine.py`, `backend/browser/live_session.py`: several
  `except Exception: pass/continue` blocks silently discarded errors with no
  trace. Now logged at debug level (unchanged control flow, adds
  observability for production debugging) — settle/wait timeouts, per-strategy
  click/type fallback attempts, live-frame title reads, and websocket/poll
  shutdown cleanup.
- Removed 5 unused imports (`typing.Callable` in `database/session.py`,
  `SYSTEM_PROMPT` in `planner/agent_loop.py`, `StepAction`/`NexusPlugin`/
  `PluginContext` in test files) and 2 unused unpacked test variables in
  `backend/tests/test_llm_client.py`, flagged by `ruff`.
- Added `pytest.ini` pinning `asyncio_default_fixture_loop_scope = function`,
  removing a pytest-asyncio deprecation warning on every test run.

### Noted, not changed
- `backend/plugins/registry.py` uses `compile()`/`exec()` to load plugin
  files instead of `importlib` — this is intentional (see CHANGELOG entry
  under Phase 2 Plugin Framework for the mtime-cache reasoning) and is the
  mechanism the plugin system depends on, not a defect.

## [Unreleased] - Phase 2

### Added — Autonomous Agent Runtime (Phase 2, item 11)
- `backend/planner/agent_runtime.py`: new `AgentRuntime` — a single Start/Stop/
  Pause/Resume lifecycle for the agent as a whole (distinct from
  `TaskQueueService`'s existing per-task pause/resume), composing the existing
  `TaskQueueService`, `BrowserEngine`, `AgentLoop`, and `LiveSessionManager`
  rather than re-implementing any of them.
  - `start()` recovers tasks interrupted by an unclean shutdown, then starts
    (or resumes) `TaskQueueService`'s background worker loop.
  - `stop()` cancels the in-flight task (if any) and pauses the worker loop.
  - `pause()`/`resume()` pause/resume the worker loop and the in-flight task together.
  - `status()` returns persisted status, current task/action/target/reasoning,
    and runtime statistics (tasks completed/failed, steps executed, recoveries
    performed).
- `backend/database/models.py`: new `AgentRuntimeState` (singleton row,
  `id="singleton"`) and `AgentRuntimeStatus` enum (`stopped`/`starting`/
  `running`/`paused`/`stopping`) — persists agent status across process restarts.
- Startup/session recovery: any `Task` left in `PLANNING`/`RUNNING`/`PAUSED`
  status by an unclean shutdown is requeued as `QUEUED` on `AgentRuntime.start()`,
  since no live browser or asyncio task can still be backing it in a fresh
  process. Counted in `recoveries_performed`.
- Browser crash recovery: `TaskQueueService._run_task`'s crash handler now
  retries a crashed task up to its existing `max_retries` (same policy as a
  normal failed outcome) before marking it `FAILED`, instead of giving up
  after a single crash. A fresh `BrowserEngine` is launched on the retry.
- `backend/planner/task_queue.py`: `TaskQueueService` gained an optional
  `activity_fn` hook (async callable(dict), events: `task_start`/`step`/
  `task_finish`/`task_crash`) used by `AgentRuntime` to maintain a live
  "current action" view. Purely additive — `notify_fn` and plugin dispatch
  are unchanged.
- `backend/api/routes_agent.py`: new `/api/agent` routes (same bearer-auth
  dependency as every other router):
  - `POST /api/agent/start` / `/stop` / `/pause` / `/resume`
  - `GET /api/agent/status` — merges `AgentRuntime.status()` with the existing
    live browser session (`state.live_session`) and active wallet
    (`state.wallet_registry.get_active_wallet()`), rather than duplicating
    either data source.
  - `WS /api/agent/ws/live` — pushes each structured activity event as it happens.
- `backend/main.py`: `AgentRuntime` is created and `start()`-ed automatically
  in the lifespan (background execution from process boot), replacing the
  previous direct `state.queue.start_worker()` call.
- Frontend: new **Agent** dashboard page (`frontend/src/pages/Agent.tsx`) —
  Start/Stop/Pause/Resume controls, agent status badge, current task/action,
  AI reasoning summary, browser state, active wallet, and runtime statistics.
  Wired into `App.tsx` routing and the `AppShell` sidebar nav. New `api.agent`
  client methods and `AgentStatus`/`AgentQueueStatus`/`AgentBrowserState`/
  `AgentActiveWallet` types in `frontend/src/lib/api.ts`.
- Tests: `backend/tests/test_agent_runtime.py` (13 tests — status defaults,
  start/stop/pause/resume transitions, in-flight task pause/resume/cancel,
  interrupted-task recovery, activity-driven statistics, broadcast callback)
  and `backend/tests/test_routes_agent.py` (5 tests — full HTTP surface,
  uninitialized-runtime error responses). Full suite: 93/93 passing.
- No redesign: every existing module (`AgentLoop`, `TaskQueueService`,
  `BrowserEngine`, `LiveSessionManager`, `WalletManager`/`WalletRegistry`,
  `DecisionEngine`) is unchanged in behavior and reused as-is; this feature
  only adds a supervising layer and the two small, additive hooks described above.

### Added — Plugin Framework (Phase 2, item 10)
- `backend/plugins/base.py`: new `NexusPlugin` base class with no-op-default hooks —
  `on_load`/`on_unload` (lifecycle) and `on_task_start`/`on_step`/`on_task_finish`/
  `on_wallet_popup` (observers). `PluginContext` passed to `on_load` exposes the
  shared `MemoryStore`, a `notify_fn`, and the plugin's own config dict.
- `backend/plugins/registry.py`: new `PluginRegistry` —
  - `discover()` scans `plugins_dir` for `*.py` files, imports each via `compile()`
    + `exec()` directly (not `importlib`'s default `SourceFileLoader`, whose
    `__pycache__` bytecode cache is keyed on source mtime and can serve stale code
    on fast successive edits within the same mtime tick — this matters for
    `reload()`), and requires exactly one `NexusPlugin` subclass per file, recording
    an `error` on the plugin's record instead of raising if a file is malformed
    (zero or 2+ subclasses, or an import-time exception).
  - `enable(name)`/`disable(name)`/`reload(name)`/`load_all()`/`unload_all()` manage
    lifecycle; `list_plugins()` returns `{name, version, description, enabled, error}`
    per plugin.
  - `dispatch_task_start/step/task_finish`: awaits each enabled plugin's hook in
    turn; a raised exception is caught, logged, and does not disable the plugin or
    stop dispatch to the rest (`_isolated()`).
  - `dispatch_wallet_popup`: same isolation, but the return value matters — any
    enabled plugin returning `False` flips the final decision to reject. A plugin
    can never turn an existing reject into an approve.
  - No upload/install-from-string endpoint anywhere — only files already present
    under `plugins_dir` are ever imported, matching the key-material scope boundary
    already documented in `backend/wallet/import_utils.py`.
- `backend/planner/agent_loop.py`: `AgentLoop` takes optional `task_id` and
  `plugin_registry` params; dispatches `on_task_start` once at the top of `run()`,
  `on_step` after each executed step, and `on_task_finish` right before returning.
- `backend/planner/task_queue.py`: `TaskQueueService` takes an optional
  `plugin_registry` and threads `task_id`/`plugin_registry` into each task's
  `AgentLoop`.
- `backend/wallet/manager.py`: `WalletManager.__init__` takes an optional
  `plugin_registry`; `handle_pending_popup` takes an optional `task_id` and, after
  the policy/human decision is made, runs `dispatch_wallet_popup` and applies a veto
  (`reason="vetoed by plugin"`) before clicking Approve/Reject.
- `backend/api/routes_plugins.py`: new `GET /api/plugins`, `POST /api/plugins/rescan`,
  `POST /api/plugins/{name}/enable|disable|reload`, all behind `require_auth`.
- `backend/config/settings.py`: new `plugins_enabled` (default `true`) and
  `plugins_dir` (default `backend/plugins/installed/`) settings.
- `backend/main.py`: lifespan now creates a `PluginRegistry`, calls `load_all()` if
  `plugins_enabled`, wires it into `state.wallet` and the `TaskQueueService`, and
  calls `unload_all()` on shutdown.
- `backend/plugins/installed/task_logger.py`: reference plugin (enabled by default)
  appending one JSON line per task-lifecycle event to `data/plugin_task_log.jsonl` —
  doubles as documentation-by-example for plugin authors.
- `frontend/src/lib/api.ts`: new `PluginInfo` type and `api.plugins.{list,rescan,
  enable,disable,reload}`.
- `frontend/src/pages/Plugins.tsx`: new dashboard page — lists discovered plugins
  with an enable/disable `Switch`, a per-plugin `Reload` button, and a `Rescan disk`
  action; surfaces a plugin's `error` (e.g. malformed file) inline instead of hiding
  it. Added to nav (`AppShell.tsx`) and routing (`App.tsx`). Verified with
  `tsc --noEmit`, `npm run build`, and `oxlint` — all clean (`0 errors`, one
  pre-existing warning in an unrelated file).
- Tests: `backend/tests/test_plugins.py` (11 tests — discovery + enable, auto-enable
  via `load_all`, `on_unload` on disable, unknown-plugin no-ops, dispatch reaching
  only enabled plugins, a broken hook staying isolated without disabling the plugin,
  `reload()` picking up on-disk changes, malformed-file error recording for both
  zero-subclass and two-subclass modules, and the wallet-popup veto path including a
  full `WalletManager.handle_pending_popup` run against a fake browser engine). Full
  suite: `57 passed` (`pytest backend/tests -q`).

### Added — Task Scheduler (Phase 2, item 6): per-task pause, deferred scheduling, retry
- `backend/planner/task_queue.py`:
  - `TaskQueueService` now tracks a per-task `asyncio.Event` (`_task_pause_events`),
    created when a task starts running and discarded when it finishes. `pause_task(id)`
    / `resume_task(id)` clear/set it and return `False` if the task isn't currently
    running (no-op, not an error) — distinct from `pause()`/`resume()`, which
    pause/resume the whole worker (no new tasks start; the in-flight one keeps going).
  - `cancel(id)` now also sets the task's pause event if it has one, so cancelling a
    paused task unblocks it immediately instead of leaving it waiting forever for a
    `resume` that will never come.
  - New `retry(id)`: re-queues a `FAILED` or `CANCELLED` task (resets `retry_count` to
    0, sets status back to `QUEUED`), returns `False` for any other status or an
    unknown id.
  - New `queue_status()`: `{worker_paused, active_task_id, paused_task_ids}`.
  - `_pop_next()` now filters on `Task.scheduled_for` (`NULL` or `<= now`) — this
    column existed on the `Task` model already but was never read, so a
    `scheduled_for` in the future had no effect; a queued task with a future
    `scheduled_for` is now correctly skipped until it's due.
  - `enqueue()` takes an optional `scheduled_for: datetime`.
- `backend/planner/agent_loop.py`: `AgentLoop` takes an optional `wait_if_paused`
  (async callable, awaited once per step before the cancel check). `TaskQueueService`
  wires this to the per-task pause event and flips the task's DB status to `PAUSED`
  / back to `RUNNING` around the wait, matching the existing `TaskStatus.PAUSED` enum
  value that was defined but previously unused anywhere in the codebase.
- `backend/api/routes_tasks.py`: new endpoints, all behind the existing `require_auth`
  bearer dependency —
  - `POST /api/tasks/{id}/cancel|pause|resume|retry`
  - `GET /api/tasks/queue/status`, `POST /api/tasks/queue/pause|resume`
  - `POST /api/tasks` and `GET /api/tasks` now accept/return `scheduled_for`.
  - Route ordering matters here: `/queue/*` is registered before `/{task_id}/*` so a
    request to e.g. `/api/tasks/queue/pause` can't be swallowed by the `{task_id}`
    path parameter (verified both via `pytest` and a manual `TestClient` smoke run).
- `frontend/src/lib/api.ts`: `scheduled_for` added to `TaskSummary`/`CreateTaskInput`;
  new `api.tasks.{cancel,pause,resume,retry,queueStatus,pauseQueue,resumeQueue}`.
- `frontend/src/pages/Tasks.tsx`: each task row now shows pause/resume/cancel/retry
  icon buttons appropriate to its current status, plus a queue-wide pause/resume
  toggle in the page header. Verified with `tsc --noEmit`, `npm run build`, and
  `oxlint` — all clean.
- Tests: `backend/tests/test_task_queue.py` (9 tests — scheduled_for persistence and
  filtering, pause/resume unblocking a waiting task, pause/resume no-op on an unknown
  task id, cancel unblocking a paused task, retry success/rejection cases) and two new
  cases in `backend/tests/test_agent_loop.py` (wait_if_paused called once per step;
  a task paused-then-cancelled stops on resume without executing another action).
  Full suite: `46 passed` (`pytest backend/tests -q`).

### Fixed — `GET /api/tasks/{id}` always raised `MissingGreenlet` once a task had any steps
- `backend/api/routes_tasks.py`: `get_task` used `session.get(Task, task_id)` and then
  iterated `task.steps` — an implicit lazy-load, which SQLAlchemy's async ORM never
  supports via plain attribute access (it requires an explicit eager-load option or
  the `AsyncAttrs` mixin, neither of which this codebase used). This endpoint had
  never actually worked for any task once its `steps` relationship needed loading;
  found while manually exercising the new task-control endpoints above with a real
  `TestClient` run, not by static review. Fixed by querying with
  `select(Task).where(...).options(selectinload(Task.steps))` instead. Same fix
  applies for free to any future field on `Task` that needs a relationship — audited
  the rest of `api/`, `planner/`, and `telegram/` for the same lazy-attribute pattern
  and this was the only occurrence.
- Regression coverage: `backend/tests/test_routes_tasks.py` (9 new tests, mounting
  only `routes_tasks.router` against a real `TaskQueueService` — no worker started,
  no real browser/LLM/Telegram/ChromaDB involved) — create-then-get returns `steps:
  []` instead of a 500, unknown-id lookups, list/scheduled_for round-trip, and the
  new control endpoints' success/no-op/not-found paths.

### Added — React Dashboard + Settings page (Phase 2, items 4 and 7)
- `frontend/`: new Vite + React + TypeScript + Tailwind v4 dashboard, styled
  with shadcn-pattern components (Radix primitives + `class-variance-authority`
  + `tailwind-merge`, hand-rolled rather than via the shadcn CLI since this
  environment has no network access to `ui.shadcn.com`). Dark "ops console"
  visual style (`frontend/src/index.css` design tokens) distinct from generic
  AI-default palettes.
- Seven pages, each wired to a real backend endpoint via a single typed client
  (`frontend/src/lib/api.ts`):
  - **Home** (`src/pages/Home.tsx`) — task-status counts, recent tasks, recent
    reports, live browser-session status.
  - **Browser** (`src/pages/Browser.tsx`) — polls `GET /api/browser/screenshot`
    as an authenticated blob (not a plain `<img src>`, since the endpoint
    requires a bearer token) and `GET /api/browser/status`. Read-only, matching
    the backend route's own read-only contract.
  - **Tasks** (`src/pages/Tasks.tsx`) — lists `GET /api/tasks`; "New task"
    dialog posts to `POST /api/tasks` with an optional wallet label sourced
    from `GET /api/wallets`.
  - **Memory** (`src/pages/Memory.tsx`) — semantic search via
    `GET /api/memory/search`.
  - **Reports** (`src/pages/Reports.tsx`) — `GET /api/reports`: duration, tx
    hashes, screenshot counts.
  - **Logs** (`src/pages/Logs.tsx`) — live-polls the new `GET /api/logs`
    endpoint, with level-colored lines, a text filter, and pause/resume.
  - **Settings** (`src/pages/Settings.tsx`) — reads/patches the new
    `GET`/`PATCH /api/settings` endpoints: wallet approval policy (manual
    approval toggle, USD auto-approve cap, allowlisted contracts), vision/OCR
    fallback, live-session tuning. Secrets are never shown (see below).
- `backend/api/routes_logs.py`: new `GET /api/logs?lines=N` — tails
  `logs/nexus.log` (default 200 lines). Read-only, no write/delete surface.
- `backend/api/routes_settings.py`: new `GET /api/settings` (safe-to-display
  config subset) and `PATCH /api/settings` (partial update). Deliberately
  excludes `api_auth_token`, all LLM provider API keys, and
  `telegram_bot_token` from both the response model and the update model —
  those stay in `.env` only. Updates are in-memory for the current process
  only (not written back to `.env`), so a restart reverts to `.env` values;
  this keeps port/DB-path/secret changes out of dashboard scope on purpose.
- `backend/main.py`: registers both new routers behind the same
  `require_auth` bearer-token dependency as every other route.
- `frontend/.env.example`: `VITE_API_BASE_URL`, `VITE_API_TOKEN` (must match
  the backend's `API_AUTH_TOKEN`).

### Added — Live Browser Session (Phase 2, item 3)
- `backend/browser/live_session.py`: `LiveSessionManager` — observes whatever
  `BrowserEngine` `TaskQueueService` currently has active and periodically
  captures a JPEG screenshot of its page, broadcasting it to connected WebSocket
  clients. It never creates, owns, or controls a browser itself — purely
  read-only, so it cannot change agent behavior. Handles the no-active-task case
  (reports `active: false`, poll loop idles) and transient failures (mid-navigation
  screenshot errors, no active page) without raising.
- `backend/planner/task_queue.py`: `TaskQueueService` now exposes
  `current_engine` / `current_task_id` (both `None` when no task is running a
  browser), set right after a task's `BrowserEngine.start()` and cleared before
  `BrowserEngine.stop()` in `_run_task`'s `finally` block. Purely additive — no
  existing method signatures or behavior changed.
- `backend/api/routes_browser.py`: new `/api/browser` router (same
  `require_auth` bearer-token dependency as the other routers), registered in
  `backend/main.py`:
  - `GET /api/browser/status` — active flag, owning task id, current URL/title,
    connected client count, frame count, last-frame timestamp, stream interval,
    last error.
  - `GET /api/browser/screenshot` — latest frame as a raw JPEG (`204` if none
    captured yet, `503` if the live session failed to initialize).
  - `WS /api/browser/ws/live` — streams a JSON frame
    (`type: "frame"`, base64 JPEG + url/title/task_id/captured_at) on every
    capture, `{"type": "idle"}` when the active task's browser closes, and sends
    the most recent frame immediately on connect if one exists.
- `backend/api/app_state.py`: added `state.live_session` slot alongside the
  existing `memory`/`wallet`/`queue` singletons.
- `backend/main.py`: creates and starts `LiveSessionManager` in the `lifespan`
  right after the task queue worker starts, and stops it (closing all connected
  WebSocket clients) during shutdown alongside the Telegram bot teardown.
- `backend/config/settings.py` / `.env.example`: new settings —
  `LIVE_SESSION_ENABLED` (default `true`), `LIVE_SESSION_INTERVAL_MS` (default
  `1000`), `LIVE_SESSION_JPEG_QUALITY` (default `60`).
- Tests: `backend/tests/test_live_session.py` (11 tests, all against fakes that
  mirror `BrowserEngine`/`Page`'s actual method signatures — no real Playwright
  browser required) — idle vs. active status, frame capture updates status +
  latest screenshot, broadcast to connected clients, immediate frame delivery to
  a newly-registered client (and correctly sending nothing if no frame exists
  yet), graceful handling of "no active page" and screenshot failures, dead-client
  cleanup during broadcast, and poll-loop start/stop lifecycle. Full suite after
  this change: `26 passed` (`pytest backend/tests -q`).
- Verified end-to-end with a live FastAPI app (via `TestClient`, `DEBUG=true`,
  no auth token): `/api/health`, `/api/browser/status` (idle), `/api/browser/screenshot`
  (`204` with no frame yet), and a real WebSocket connect/accept/disconnect
  cycle against `/api/browser/ws/live` all behave as expected. A real Playwright
  Chromium launch could not be exercised in this environment (browser binary
  download is blocked by the sandbox's network allowlist), so the screenshot
  capture path itself is covered by the fake-`Page`/`BrowserEngine` unit tests
  above, which match Playwright's real `page.screenshot(type=, quality=)` /
  `page.title()` / `page.url` surface exactly.

### Fixed — Repo review: security, performance, correctness (no behavior/API changes except as noted)
- **Security — timing-safe auth**: `backend/api/auth.py` compared the bearer token with
  `!=`. Replaced with `hmac.compare_digest` (constant-time). Also logs one warning at
  first use if `API_AUTH_TOKEN` is unset outside `debug` mode, instead of silently
  running open.
- **Security — Telegram bot auth gap**: only `cmd_start`, `cmd_task`, and `on_free_text`
  checked `_is_authorized()`. Every other command — `pause`, `resume`, `stop`, `report`,
  `logs` (log file contents), `screenshot` (can include wallet popup contents),
  `memory`, `settings`, `tasks`, `browser`, `status` — had **no auth check**, so anyone
  who could message the bot could control it or read logs/screenshots even with
  `TELEGRAM_ALLOWED_USER_IDS` configured. Fixed by adding an `@auth_required` decorator
  applied to all 15 handlers (also removes the previous per-handler duplication of the
  check).
- **Security — CORS**: was hardcoded to `["*"]` in debug / `[]` otherwise, which would
  have silently blocked the upcoming React dashboard in production. Added
  `CORS_ALLOWED_ORIGINS` setting (comma-separated); empty still defaults to the same
  `*`-in-debug / closed-otherwise behavior, so existing deployments are unaffected.
- **Correctness — cancellation didn't cancel**: `TaskQueueService.cancel(task_id)` only
  added the id to a set that was checked *after* the agent loop already finished on its
  own, so `/stop`-ing a specific in-flight task never actually stopped it, only
  relabeled the report once it ended naturally. `AgentLoop` now takes an optional
  `should_cancel` callback checked once per step (default `None`, so existing callers
  are unaffected); `TaskQueueService` wires it to the cancelled-ids set and clears the
  id once consumed (previously the set grew unbounded).
- **Performance — blocking the event loop**: `MemoryStore` called ChromaDB's synchronous
  client directly inside `async def` methods; embedding + upsert/query calls blocked
  the entire FastAPI event loop (server responses, WebSocket broadcasts, other tasks)
  for their full duration. Wrapped in `asyncio.to_thread`. No signature changes.
- **Duplication — `llm_client.py`**: the 8 provider call methods (4 text + 4 vision)
  were near-identical. Refactored into one dispatch → post → extract pipeline shared by
  `complete_json` and `complete_json_with_image`, with one "build request" method per
  provider family (Anthropic / OpenAI+OpenRouter / Gemini). Public method signatures
  and behavior are unchanged; added `backend/tests/test_llm_client.py` (4 tests) to
  lock in per-provider request shape.
- **Duplication — API routes**: `routes_tasks.py`, `routes_reports.py`, and
  `routes_wallet.py` each repeated the same session/select/scalars boilerplate. Added
  `list_all()` to `backend/database/session.py` and switched all three `list_*`
  endpoints to use it. JSON response shape is byte-for-byte unchanged;
  `list_wallets` explicitly keeps its original uncapped query (`limit=None`).
- **Minor**: hoisted two duplicated local `import re` in `wallet/manager.py` to module
  level.
- Full suite after this pass: `15 passed` (`pytest backend/tests -q`).

### Added — Browser Vision + OCR fallback (Phase 2, item 1-2)
- `backend/vision/ocr.py`: `OCREngine` — async Tesseract OCR wrapper (text + word
  boxes with confidence), degrades gracefully to `available=False` when the
  `tesseract` binary is not installed instead of raising.
- `backend/vision/vision_engine.py`: `VisionAnalyzer` — combines OCR output with an
  optional vision-LLM read of the page screenshot, returning elements in the same
  shape the planner already consumes (`merge_into_elements`). Supports Anthropic,
  OpenAI, Gemini, and OpenRouter (matches existing `LLMProvider` set).
- `backend/planner/llm_client.py`: added `complete_json_with_image` plus one
  provider-specific multimodal call per provider (`_call_anthropic_vision`,
  `_call_openai_vision`, `_call_gemini_vision`, `_call_openrouter_vision`). Existing
  text-only methods are unchanged.
- `backend/planner/agent_loop.py`: `AgentLoop` now takes an optional `vision`
  parameter (defaults to a real `VisionAnalyzer`). After each DOM snapshot, if fewer
  than `VISION_MIN_ELEMENTS_THRESHOLD` interactive elements were found, it runs the
  OCR + vision fallback and merges the result into the snapshot before the planner
  LLM decides the next action. No behavior change on ordinary DOM-rich pages.
- `backend/config/settings.py`: new settings — `vision_enabled`,
  `vision_min_elements_threshold`, `vision_model_override`, `ocr_enabled`,
  `ocr_lang`, `ocr_max_chars`.
- `requirements.txt`: added `pytesseract==0.3.13`, `Pillow==10.4.0`.
- `docker/Dockerfile.backend`: installs the `tesseract-ocr` system package.
- `.env.example`: documented the new vision/OCR variables.
- Tests: `backend/tests/test_ocr.py` (3 tests — successful extraction, graceful
  degradation when tesseract is missing, missing-file handling) and
  `backend/tests/test_vision.py` (5 tests — threshold triggering, disabled flag,
  merge behavior, and graceful handling of a vision-LLM failure). All mock
  pytesseract/PIL/the LLM client, so they run without a real Tesseract install or
  API keys. Full suite: `9 passed` (`pytest backend/tests -q`).

### Added — Dedicated AI Decision Engine (Phase 2, v1.0 core infrastructure)
- `backend/planner/decision_engine.py`: new `DecisionEngine` class, extracted from
  logic that previously lived inline in `agent_loop.py`. Owns:
  - `perceive(snapshot, goal)` — runs the existing vision/OCR fallback when the DOM
    snapshot comes back too sparse, enriching the snapshot in place (unchanged
    behavior, just relocated).
  - `decide(...)` — builds the planner prompt (same `SYSTEM_PROMPT`, now re-exported
    from `agent_loop` for backward compatibility) and returns a typed `Decision`
    dataclass instead of a raw dict.
  - `verify(url_before, url_after, action, success)` — new: logs whether the
    previous action had an observable effect (`VerificationResult`), feeding the
    live logs stream. Purely observational, does not change control flow.
  - `recovery_hint(action, target, success, stall_count)` — new: produces short
    advisory text (e.g. "previous action failed, consider scrolling / a different
    element description") that gets folded into the *next* `decide()` call's
    prompt when an action failed or the page stalled for 2+ steps. Advisory only —
    `AgentLoop`'s own stall-count-based failure threshold (4 steps) is unchanged.
- `backend/planner/agent_loop.py`: `AgentLoop` now delegates perception/decision to
  `self.decision_engine` (a `DecisionEngine` built from the same `llm`/`vision`
  instances passed to `AgentLoop`, so existing callers/tests that construct
  `AgentLoop(llm=FakeLLM(...))` see identical behavior). Constructor signature,
  `StepResult`/`TaskOutcome` shapes, and `AgentLoop.llm`/`AgentLoop.vision` are
  unchanged — no breaking changes for `task_queue.py` or the Telegram bot.
- Tests: `backend/tests/test_decision_engine.py` (9 tests — decide/verify/
  recovery_hint/perceive in isolation, LLM-failure handling, recovery context
  folded into the next prompt).

### Added — WebSocket layer completion (Phase 2, v1.0 core infrastructure)
Live browser status (`/api/browser/ws/live`) and live task updates
(`/api/tasks/ws/live`) already existed (see Live Browser Session entry above and
the original task-queue delivery); this increment adds the two remaining streams:
- `backend/api/routes_logs.py`: new `WS /api/logs/ws/live` — sends the last 50 lines
  already on disk on connect, then streams every new formatted log line as it's
  emitted anywhere in the backend process (planner, decision engine, task queue,
  plugins, wallet, ...). New `WebSocketLogBroadcastHandler(logging.Handler)` bridges
  stdlib `logging` (sync, called from any thread) to the async broadcast via
  `loop.call_soon_threadsafe`. Attached to the root logger in `backend/main.py`'s
  lifespan, on top of the existing `FileHandler`/`StreamHandler` — purely additive,
  the polling `GET /api/logs` endpoint is untouched.
- `backend/api/routes_plugins.py`: new `WS /api/plugins/ws/live` — streams plugin
  lifecycle events (`plugin_enabled`, `plugin_disabled`, `plugin_reloaded`,
  `plugin_reload_failed`) and hook-dispatch events (`task_start`, `task_step`,
  `task_finish`, `wallet_popup`, the last including `initial_decision` and
  `final_decision` so a viewer can see a plugin veto happen live).
- `backend/plugins/registry.py`: `PluginRegistry` takes a new optional `event_fn`
  keyword (async callable, JSON string in) alongside the existing
  `memory`/`notify_fn`/`config`. Defaults to `None`, which is a complete no-op —
  every existing construction call (`PluginRegistry(plugins_dir=..., memory=...,
  notify_fn=...)` in `main.py` and every test in `test_plugins.py`) is unaffected.
  Broadcast failures are isolated the same way plugin hooks already are (a raising
  `event_fn` cannot break `enable`/`disable`/`reload`/dispatch).
- `backend/main.py`: wires `PluginRegistry(event_fn=_broadcast_plugin_event)` and
  attaches/detaches `WebSocketLogBroadcastHandler` in the lifespan.
- Tests: `backend/tests/test_logs_ws.py` (4 tests) and
  `backend/tests/test_plugin_events.py` (5 tests) — broadcast fan-out, dead-client
  cleanup, the logging-to-WS bridge, and that a missing/broken `event_fn` never
  breaks plugin lifecycle or dispatch.

### Confirmed (no change) — Task Scheduler (Phase 2, item 6)
Reviewed `backend/planner/task_queue.py` against the v1.0 core-infrastructure
requirements (persistent queue, priority, pause/resume/retry/cancel, background
execution): already fully implemented against the SQLite `Task` table with no gaps.
Left as-is per "do not redesign / do not replace working modules" — see
`docs/ARCHITECTURE.md` for the full data-flow writeup added in this increment.

Full suite after this pass: `75 passed` (`pytest backend/tests -q`, up from `57`).
`npm run build` in `frontend/` still succeeds unchanged (no frontend files touched
in this increment).

## [Phase 1] - prior to this changelog

Working, tested backbone: generic Playwright browser engine, LLM-driven agent loop
(Anthropic/OpenAI/Gemini/OpenRouter), SQLite + ChromaDB memory, non-custodial wallet
approval automation, priority task queue, Telegram bot with full command set, FastAPI
REST + WebSocket layer, Docker/compose, initial pytest suite. See README "What's
implemented and working" for the full list.
