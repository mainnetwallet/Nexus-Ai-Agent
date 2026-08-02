# Nexus-Agent

An autonomous, **generic** browser agent: point it at any website + a goal + a wallet
label, and it perceives the page, plans the next action with an LLM, executes it via
Playwright, verifies the result, and repeats — with persistent memory of what has
worked before, and full remote control from Telegram.

**No website-specific code anywhere.** The planner reasons only from what's currently
visible on the page (text + interactive elements), matching the "generic website
engine" requirement: it never hardcodes selectors or logic for any particular site.

## Status

Phase 1 (working, tested backbone) is complete. **Phase 2 is in progress.** Building
the full spec — a multi-week production system — in one pass isn't realistic, so
Phase 2 is being delivered incrementally, one feature at a time, each with its own
tests and docs update. See "Phase 2 progress" below for what's done and what's next.

### Phase 2 progress

| # | Feature | Status |
|---|---------|--------|
| 1 | Browser Vision (vision-LLM fallback) | ✅ done |
| 2 | OCR (Tesseract fallback) | ✅ done |
| 3 | Live Browser Session | ✅ done |
| 4 | React Dashboard | ✅ done |
| 5 | WebSocket live updates | ✅ done — task progress (`/api/tasks/ws/live`), browser frames (`/api/browser/ws/live`), live logs (`/api/logs/ws/live`), live plugin events (`/api/plugins/ws/live`) |
| 6 | Task Scheduler (pause/resume/retry/cancel/priority/history, per-task pause, `scheduled_for`) | ✅ done |
| 7 | Settings page | ✅ done |
| 8 | AI model switching | ⏳ not started |
| 9 | Chrome Profile Manager | ⏳ not started |
| 10 | Plugin System | ✅ done (dynamic loading, registry, lifecycle hooks, veto-only wallet-popup hook, live event stream) |
| 11 | Browser crash recovery (detect/restore session, resume interrupted tasks) | ⏳ not started |
| 12 | Memory improvements (explicit failed-workflow save, reuse ranking) | ⏳ not started |
| 13 | AI Decision Engine (dedicated reasoning module: perceive/decide/verify/recover) | ✅ done |

See `CHANGELOG.md` for details on what shipped in each step, and
`docs/ARCHITECTURE.md` for a data-flow-level writeup of the WebSocket layer,
Task Scheduler, and AI Decision Engine, including a repo-wide
security/performance/duplication review pass done between items 2 and 3.

### AI Decision Engine (new)

`backend/planner/decision_engine.py` extracts the reasoning `AgentLoop` used
to do inline into its own `DecisionEngine` class:

- **`perceive(snapshot, goal)`** — reads the DOM snapshot, falling back to
  the existing vision/OCR path when it's too sparse (unchanged behavior,
  just relocated so it's independently testable).
- **`decide(...)`** — sends the perception to the planner LLM, returns a
  typed `Decision` (`action/target/value/reasoning/confidence`).
- **`verify(url_before, url_after, action, success)`** — logs whether the
  action had an observable effect. Purely observational; `AgentLoop`'s
  existing stall-count failure threshold is unchanged.
- **`recovery_hint(...)`** — when an action failed or the page stalled for
  2+ steps, produces advisory text folded into the *next* `decide()` call's
  prompt, so the planner LLM (not a hardcoded retry policy) decides how to
  recover — keeping the "no site-specific logic" invariant intact.
- Every decision/verification is logged via the standard `logging` module,
  which is what feeds the new live logs stream below — no separate storage
  surface needed for "logs reasoning".
- `AgentLoop`'s constructor, `StepResult`/`TaskOutcome` shapes, and
  `llm`/`vision` attributes are unchanged — this was a pure extraction, not
  a redesign. See `docs/ARCHITECTURE.md` for the full writeup.
- Tests: `backend/tests/test_decision_engine.py` (9 tests).

### Live Logs + Plugin Events (new)

Rounding out the WebSocket layer (item 5):

- **`WS /api/logs/ws/live`** — sends the last 50 log lines on connect, then
  streams every new formatted line as it's emitted anywhere in the backend
  process, via a `WebSocketLogBroadcastHandler(logging.Handler)` attached to
  the root logger. The existing polling `GET /api/logs` is untouched.
- **`WS /api/plugins/ws/live`** — streams plugin lifecycle events
  (enabled/disabled/reloaded) and hook-dispatch events (task start/step/
  finish, wallet-popup decisions including the plugin veto outcome). Backed
  by a new optional `event_fn` on `PluginRegistry` (default `None` — every
  existing construction call and test is unaffected).
- Tests: `backend/tests/test_logs_ws.py` (4 tests),
  `backend/tests/test_plugin_events.py` (5 tests).

### Browser Vision + OCR (new)

`backend/vision/` adds a perception fallback for canvas-heavy, image-only, or
otherwise DOM-sparse pages:

- **`backend/vision/ocr.py`** — `OCREngine`, a Tesseract-based text extractor over
  the page screenshot. Degrades gracefully (returns `available=False`, never raises
  into the agent loop) if the `tesseract` binary isn't installed.
- **`backend/vision/vision_engine.py`** — `VisionAnalyzer`, which sends the
  screenshot to a vision-capable LLM (same provider/model family already configured
  for planning — Anthropic/OpenAI/Gemini/OpenRouter) and asks it to describe
  actionable elements in the same shape the planner already consumes.
- **`backend/planner/agent_loop.py`** — now checks `interactive_elements` count
  after each DOM snapshot; if it's below `VISION_MIN_ELEMENTS_THRESHOLD` (default 3)
  and `VISION_ENABLED=true`, it runs the OCR + vision fallback and merges the result
  into the snapshot before asking the planner LLM to decide the next action. No
  change to behavior on ordinary DOM-rich pages — this is purely additive.
- New settings: `VISION_ENABLED`, `VISION_MIN_ELEMENTS_THRESHOLD`,
  `VISION_MODEL_OVERRIDE`, `OCR_ENABLED`, `OCR_LANG`, `OCR_MAX_CHARS` (see
  `.env.example`).
- Docker image now installs the `tesseract-ocr` system package.

### Plugin Framework (new)

`backend/plugins/` lets you extend Nexus-Agent without touching core modules:

- **`backend/plugins/base.py`** — `NexusPlugin` base class. Subclass it and override
  only the hooks you need: `on_load`/`on_unload` (lifecycle) and
  `on_task_start`/`on_step`/`on_task_finish`/`on_wallet_popup` (observers into the
  agent loop and wallet approval flow).
- **`backend/plugins/registry.py`** — `PluginRegistry` discovers every `*.py` file
  under `plugins_dir` (default `backend/plugins/installed/`), imports it, and
  expects exactly one `NexusPlugin` subclass per file. Handles enable/disable/reload
  and dispatches hooks to every enabled plugin with per-plugin error isolation — a
  plugin that raises never crashes the task loop or other plugins.
- **Security boundary (deliberate, matching the wallet-key scope boundary above):**
  there is no "install plugin from string/upload" API. Only files already on disk get
  loaded — turning plugin management into a code-upload endpoint would make the
  service a remote-code-execution target. `on_wallet_popup` can veto an approval
  (turn approve → reject) but can never turn a reject into an approve, and no plugin
  hook ever receives a private key or seed phrase (those never exist as Python values
  outside `backend/wallet/import_utils.py` in the first place).
- **`backend/plugins/installed/task_logger.py`** — reference plugin, enabled by
  default, that appends one JSON line per task-lifecycle event to
  `data/plugin_task_log.jsonl`.
- New API: `GET /api/plugins`, `POST /api/plugins/rescan`,
  `POST /api/plugins/{name}/enable|disable|reload`.
- New settings: `PLUGINS_ENABLED`, `PLUGINS_DIR`.
- New dashboard page: **Plugins** — lists discovered plugins with an enable/disable
  toggle and a reload button per plugin, plus a rescan-disk action.
- Tests: `backend/tests/test_plugins.py` (11 tests — discovery, enable/disable,
  reload picking up on-disk changes, error isolation for a broken hook, malformed
  plugin files, and the wallet-popup veto path). Full suite: `57 passed`.

### Live Browser Session (new)

`backend/browser/live_session.py` adds real-time visibility into whatever website
the agent is currently operating on, without adding any new browser instance or
touching `BrowserEngine`/`TaskQueueService` behavior:

- **`LiveSessionManager`** — a single instance (`state.live_session`, created and
  started in `main.py`'s lifespan) that polls `TaskQueueService.current_engine`
  (a new read-only attribute set/cleared around each task's browser lifecycle) on a
  fixed interval, captures a JPEG screenshot of its active page, and broadcasts it
  to connected WebSocket clients. When no task is running, `/api/browser/status`
  reports `active: false` and the poll loop simply idles — no extra browser is ever
  launched for this feature.
- **`backend/api/routes_browser.py`** — new `/api/browser` routes on the existing
  FastAPI app, same bearer-auth dependency as the other routers:
  - `GET /api/browser/status` — whether a browser is active, which task owns it,
    current URL/title, connected client count, last frame timestamp.
  - `GET /api/browser/screenshot` — the latest captured frame as a raw JPEG
    (`204` if nothing has been captured yet).
  - `WS /api/browser/ws/live` — push-based stream; emits a JSON frame
    (`{"type": "frame", "task_id", "url", "title", "captured_at", "mime_type",
    "image_base64"}`) on every capture, and `{"type": "idle"}` when the active
    task ends. On connect, it immediately sends the most recent frame (if any)
    so a viewer isn't stuck looking at a blank screen.
- New settings: `LIVE_SESSION_ENABLED`, `LIVE_SESSION_INTERVAL_MS`,
  `LIVE_SESSION_JPEG_QUALITY` (see `.env.example`).
- Tests: `backend/tests/test_live_session.py` (11 tests — status when idle/active,
  frame capture + broadcast, sending the existing frame to newly-registered
  clients, graceful handling of no-active-page/screenshot failures, dead-client
  cleanup, start/stop lifecycle).

### Autonomous Agent Runtime (new)

`backend/planner/agent_runtime.py` turns Nexus-Agent into a continuously running
agent with a single Start/Stop/Pause/Resume lifecycle for the agent as a whole
(distinct from `TaskQueueService`'s existing per-task pause/resume), persisted
across restarts, and self-healing after an unclean shutdown or browser crash.
It composes the existing `TaskQueueService`, `BrowserEngine`, `AgentLoop`, and
`LiveSessionManager` rather than re-implementing any of them:

- **`AgentRuntime`** (`state.agent`, created and auto-started in `main.py`'s
  lifespan) — `start()` / `stop()` / `pause()` / `resume()` drive
  `TaskQueueService`'s worker loop and current in-flight task together, and
  persist status to a new `AgentRuntimeState` singleton row so a dashboard
  reload (or a backend restart) still shows the last known state.
- **Recovery** — on `start()`, any task left in `PLANNING`/`RUNNING`/`PAUSED`
  status (an artifact of a process that died mid-task) is requeued as `QUEUED`
  automatically, since no live browser or asyncio task can still be backing it
  in a fresh process. `recoveries_performed` in the status view counts this.
- **Browser crash recovery** — `TaskQueueService._run_task`'s crash handler now
  retries a crashed task (browser crash, Playwright error, or anything else
  unexpected) up to its existing `max_retries` before marking it `FAILED`,
  instead of giving up after one crash. A fresh `BrowserEngine` is launched on
  the retry.
- **Live monitoring** — `TaskQueueService` gained an optional `activity_fn`
  hook (`task_start` / `step` / `task_finish` / `task_crash` events) that
  `AgentRuntime` uses to maintain "current action / target / reasoning" in
  real time, without touching `notify_fn` or the plugin dispatch that were
  already there.
- **`backend/api/routes_agent.py`** — new `/api/agent` routes, same
  bearer-auth dependency as every other router:
  - `POST /api/agent/start` / `/stop` / `/pause` / `/resume`
  - `GET /api/agent/status` — runtime status + current task/action/reasoning +
    runtime statistics, merged with the existing live browser session
    (`state.live_session`) and active wallet (`state.wallet_registry`) rather
    than duplicating either.
  - `WS /api/agent/ws/live` — pushes each structured activity event as it happens.
- Tests: `backend/tests/test_agent_runtime.py` (13 tests) and
  `backend/tests/test_routes_agent.py` (5 tests) — status transitions,
  recovery of interrupted tasks, activity-driven statistics, and the full
  start/stop/pause/resume HTTP surface.
- Dashboard: new **Agent** page (see "Frontend dashboard" below).

## What's implemented and working

- **`backend/browser/engine.py`** — Playwright wrapper: navigate, smart_click/type/
  wait/scroll, upload, popup/new-tab detection, DOM + accessibility-ish element
  extraction, screenshots, persistent Chrome profile support, Chrome/Edge channel
  selection.
- **`backend/planner/agent_loop.py`** — the think → analyze → plan → execute → verify →
  continue loop; orchestrates browser lifecycle, pause/cancel, plugin dispatch, and
  persistence, delegating perception/decision/verification/recovery to
  `backend/planner/decision_engine.py`'s `DecisionEngine` (see "AI Decision Engine"
  above). Stall detection, memory-informed context.
- **`backend/planner/decision_engine.py`** — dedicated reasoning module: reads
  DOM/vision state, asks the LLM for the next action, verifies the previous
  action's effect, and produces recovery guidance for the next decision (see
  "AI Decision Engine" above).
- **`backend/planner/llm_client.py`** — unified client for Anthropic / OpenAI / Gemini /
  OpenRouter, switchable via `.env`.
- **`backend/memory/store.py`** — SQLite (structured) + ChromaDB (semantic recall of
  prior workflows so the agent can draw on past experience with similar goals).
- **`backend/vision/`** — OCR + vision-LLM fallback perception for canvas-heavy or
  image-only pages (see "Browser Vision + OCR" below).
- **`backend/browser/live_session.py`** — real-time screenshot streaming of whatever
  page the agent is currently on, over REST + WebSocket (see "Live Browser Session"
  below).
- **`backend/wallet/manager.py`** — **non-custodial** wallet automation: it never reads,
  stores, or transmits seed phrases or private keys. It only decides whether to click
  Approve/Reject on your MetaMask/Rabby extension's own popup, under an explicit,
  configurable allow-policy (manual-approval-by-default, contract allowlist, USD value
  cap). All real signing happens inside your wallet extension.
- **`backend/planner/task_queue.py`** — priority queue with pause/resume/cancel/retry
  (both queue-wide and per-task), `scheduled_for` deferred scheduling, and persistent
  history via the `Task`/`Report` tables; drives the agent loop per task, writes reports.
- **`backend/planner/agent_runtime.py`** — Autonomous Agent Runtime: single Start/
  Stop/Pause/Resume lifecycle for the agent as a whole, persisted status, startup
  recovery of interrupted tasks, browser-crash retry (see "Autonomous Agent Runtime"
  above).
- **`backend/telegram/bot.py`** — all requested commands (`/start /help /status /task
  /browser /pause /resume /stop /report /logs /screenshot /memory /settings /tasks`)
  plus free-form natural-language routing ("pause the browser", "complete all tasks on
  https://... using Wallet-01").
- **`backend/api/`** — FastAPI REST + WebSocket layer (tasks, reports, memory search,
  wallet metadata registration, live logs, live plugin events), bearer-token auth.
- **`backend/database/models.py`** — Task/TaskStep/Report/WalletRecord/MemoryEntry/
  AgentRuntimeState SQLAlchemy models.
- Docker + docker-compose, `.env.example`, a passing unit test for the agent loop.

## Quick start

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY (or another provider), TELEGRAM_BOT_TOKEN, etc.

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chrome

python -m uvicorn backend.main:app --reload
```

Then either:
- Use the API: `POST /api/tasks {"website": "...", "goal": "...", "wallet_label": "Wallet-01"}`
  (optionally add `"priority": 5` or `"scheduled_for": "2026-01-01T00:00:00Z"` to defer it)
- Or, if `TELEGRAM_BOT_TOKEN` is set, message your bot: `/task https://example.com | create an account | Wallet-01`

Control a task once it's queued or running:
- `POST /api/tasks/{id}/pause` / `/resume` — pause or resume just that task (its browser
  session stays open; the agent loop blocks between steps until resumed)
- `POST /api/tasks/{id}/cancel` — cancel it (works even if it's currently paused)
- `POST /api/tasks/{id}/retry` — re-queue a `failed` or `cancelled` task, resetting its
  retry counter
- `GET /api/tasks/queue/status`, `POST /api/tasks/queue/pause` / `/resume` — pause or
  resume the whole worker (no new tasks start; the in-flight one keeps running)

While a task is running, watch it live:
- `GET /api/browser/status` — is a browser active, current URL/title
- `GET /api/browser/screenshot` — latest frame as a JPEG
- `WS /api/browser/ws/live` — push stream of frames as they're captured

The agent itself runs continuously in the background from the moment the
backend starts (auto-started in `main.py`'s lifespan). Control it as a whole:
- `POST /api/agent/start` / `/stop` / `/pause` / `/resume`
- `GET /api/agent/status` — status, current task/action/reasoning, browser
  state, active wallet, and runtime statistics in one call
- `WS /api/agent/ws/live` — push stream of activity events

Run tests:
```bash
pytest backend/tests -q
```

## Docker

```bash
docker compose up --build
```

## Security notes (please read)

- Wallet approvals default to **manual** (`WALLET_REQUIRE_MANUAL_APPROVAL=true`). Only
  relax this for specific, allowlisted contracts and a USD cap you're comfortable
  with, and only after you've watched the agent operate safely for a while.
- No seed phrase or private key ever passes through this codebase. If you later wire
  in a signing key (e.g. for a separate scripted/burner-wallet flow outside the
  browser), keep that entirely out of `wallet/manager.py`'s scope and encrypt it at
  rest — this module is deliberately UI-automation-only.
- Set `API_AUTH_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` before exposing this beyond
  localhost.

## Repo layout

```
backend/
  api/         REST + WebSocket routes, auth
  browser/     Playwright engine (generic, no site logic) + live session streaming
  planner/     LLM client, agent loop, task queue, autonomous agent runtime
  memory/      SQLite + ChromaDB store
  vision/      OCR + vision-LLM perception fallback
  wallet/      Non-custodial approval automation
  telegram/    Bot commands + NL routing
  database/    SQLAlchemy models + session
  tests/       Pytest suite
docker/        Dockerfile(s)
docs/          (reserved for architecture docs)
frontend/      React dashboard (Vite + TypeScript + Tailwind v4 + shadcn-style UI)
  src/lib/api.ts  -- typed client for every backend route
  src/pages/       -- Home, Agent, Browser, Tasks, Memory, Reports, Logs, Settings
```

## Frontend dashboard

A single-page dashboard that talks to the FastAPI backend over REST (and polls
the screenshot endpoint for the live view — no site-specific logic here either,
it just renders whatever the backend returns).

```bash
cd frontend
cp .env.example .env
# set VITE_API_BASE_URL and VITE_API_TOKEN to match the backend's host/port
# and API_AUTH_TOKEN

npm install
npm run dev      # http://localhost:5173, backend must already be running
npm run build    # production build -> frontend/dist
```

Pages:
- **Home** — live counts (running/queued/succeeded/failed), recent tasks, recent
  reports, and current browser-session status at a glance.
- **Agent** — Start/Stop/Pause/Resume the Autonomous Agent Runtime; shows agent
  status, current task/action, AI reasoning summary, browser state, active
  wallet, and runtime statistics (`GET /api/agent/status`, `POST /api/agent/
  start` / `/stop` / `/pause` / `/resume`).
- **Browser** — read-only live view: polls `GET /api/browser/screenshot` and
  shows `GET /api/browser/status` (URL, title, viewer count). No control surface.
- **Tasks** — lists `GET /api/tasks`, and a "New task" dialog that posts to
  `POST /api/tasks` (website, goal, optional wallet label from `GET /api/wallets`,
  notes).
- **Memory** — semantic search over past workflows via `GET /api/memory/search`.
- **Reports** — outcomes from `GET /api/reports`: duration, tx hashes, screenshot
  counts.
- **Logs** — tails `GET /api/logs` (new route, see below) with live polling,
  level-colored lines, and a text filter.
- **Settings** — reads/patches `GET`/`PATCH /api/settings` (new route, see
  below): wallet approval policy, vision/OCR fallback, live-session tuning.
  Never exposes API keys, the auth token, or the Telegram token.

Two backend routes were added to give the Logs and Settings pages something
real to call:
- `backend/api/routes_logs.py` — `GET /api/logs?lines=N`, tails
  `logs/nexus.log`. Read-only.
- `backend/api/routes_settings.py` — `GET /api/settings` (safe-to-display
  subset of config) and `PATCH /api/settings` (updates the running process's
  in-memory settings only; not persisted to `.env`, so a restart reverts to
  `.env` values). Secrets are never returned or accepted.

Both are registered in `backend/main.py` behind the same `require_auth` bearer
token as every other route.
