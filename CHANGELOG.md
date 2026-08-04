# Changelog

All notable changes to Nexus-Agent are documented here. Phase 2 is being delivered
incrementally, one feature at a time; each entry below corresponds to one delivered
increment with passing tests.

## [Unreleased] - Hot Signer: direct RPC native-token transfer from Chat (burner wallets)

New, deliberately separate opt-in path alongside the existing browser-extension
wallet flow. `WalletManager`/`WalletRegistry` never touch a private key by design
(human approves every popup in the user's own extension); `HotSigner` is the
opposite tradeoff — the backend holds a private key in memory (env var only) and
signs + broadcasts a native transfer itself via JSON-RPC, no approval step. Intended
only for burner/bot wallets. Disabled by default.

### Added
- `backend/wallet/hot_signer.py` — `HotSigner.send_native(chain, to_address, amount)`:
  builds a plain native-transfer tx (nonce + gas price via JSON-RPC, `eth_account`
  signing, `eth_sendRawTransaction` broadcast), validates chain/address/amount,
  enforces an optional per-tx cap, and logs the send to `WalletRegistry.record_activity`
  (address/amount/chain/tx hash only — never the key). `get_hot_signer_address()`
  helper for reading the configured signer's address without exposing the key.
- `backend/config/settings.py` / `.env.example` — `HOT_SIGNER_ENABLED`,
  `HOT_SIGNER_PRIVATE_KEY`, `HOT_SIGNER_MAX_NATIVE_VALUE`.
- `backend/api/app_state.py` / `backend/main.py` — `state.hot_signer`, initialized
  at startup wired to the existing `WalletRegistry` for activity logging.
- `backend/api/routes_wallet.py` — `POST /api/wallets/hot-signer/send`.
- `backend/planner/chat_engine.py` — `CLASSIFIER_SYSTEM_PROMPT` gained
  `wallet_action=send_native` (+ `send_chain`/`send_to_address`/`send_amount`
  fields, with Bengali/Banglish phrasing examples) and a new
  `ChatEngine._handle_send_native` handler dispatched from
  `_handle_wallet_command`. No confirmation turn — the message is parsed and
  the transfer executes immediately, gated only by the hot signer's own
  enable flag and per-tx cap.
- `backend/tests/test_hot_signer.py` — disabled-state, unsupported-chain,
  invalid-address, over-cap, and a fully mocked successful-send test
  (asserts RPC call order, returned tx hash, and the activity-log write).

### Notes
- This does not change `wallet/manager.py` or `wallet/registry.py`'s existing
  no-key-material guarantee; `HotSigner` is a new, independent module.
- README's "Security notes" section documents the opt-in/burner-wallet caveat.
- Dashboard: **Wallets** page gained a "Hot Signer" card
  (`frontend/src/pages/Wallets.tsx`) showing enabled/disabled status + derived
  address (never the key), plus a chain/amount/destination send form that
  broadcasts immediately on submit (with a confirm() dialog since there's no
  server-side approval step). New `GET /api/wallets/hot-signer/status` route
  and `api.wallets.hotSigner.{status,send}` client methods
  (`frontend/src/lib/api.ts`). `backend/tests/test_routes_hot_signer.py`
  covers both endpoints.

## [Unreleased] - Single Task Control (pause/resume/cancel one task from Chat/Telegram/Dashboard/REST API)

`TaskQueueService.pause_task/resume_task/cancel` (backend/planner/task_queue.py)
and the corresponding REST endpoints (`POST /api/tasks/{id}/pause|resume|cancel`,
backend/api/routes_tasks.py) already existed and the Dashboard's Tasks page
already had working buttons wired to them. What was missing was the same
control from Chat and Telegram — both only supported pausing/resuming the
*entire* agent worker, with no way to target one specific task, and no
"cancel" concept at all in either. This entry closes that gap by extending
the existing agent-command dispatch in both, instead of adding a parallel
task-control code path.

### Changed
- `backend/planner/chat_engine.py` — `CLASSIFIER_SYSTEM_PROMPT` gained a
  `task_id` field and guidance for three new `agent_command` actions:
  `pause_task`, `resume_task`, `cancel_task`. `_handle_agent_command` now
  routes these to `TaskQueueService.pause_task/resume_task/cancel` instead
  of the global `queue.pause()/resume()`. When no `task_id` is given,
  `pause_task`/`cancel_task` target whichever task is currently running
  (`queue.current_task_id`); `resume_task` targets the first paused task.
  Plain `"pause"`/`"resume"`/`"stop"` (no task named) are untouched and
  still control the global worker exactly as before — this was verified
  with a dedicated backward-compatibility test.
- `backend/telegram/bot.py` — `/pause` and `/resume` now accept an optional
  `<task_id>` argument (no arg = unchanged global behavior); added a new
  `/cancel [task_id]` command. `INTENT_SYSTEM_PROMPT` (the free-text NLU
  classifier) gained `pause_task`/`resume_task`/`cancel_task` intents plus
  a `task_id` field, which `on_free_text` delegates to the same ChatEngine
  logic above via a synthetic chat message — one place (ChatEngine) still
  owns the actual task-control decision, Telegram just routes to it.
- `backend/tests/test_chat_engine.py`, `backend/tests/test_telegram_bot.py`
  — added coverage for: pausing/resuming/cancelling by explicit id,
  defaulting to the currently-running task, the "no task running" and
  "unknown task id" error replies, and the bare-`"pause"`-still-global
  regression test.

### Fixed (unrelated pre-existing test bugs, found while validating this change)
- `backend/tests/test_llm_client.py::test_anthropic_vision_request_embeds_image`
  didn't set `settings.anthropic_api_key` before calling `_build_anthropic`,
  so it failed in any environment without a real key configured. Now
  monkeypatches a test key, matching the sibling text-request test right
  above it.
- `backend/tests/test_llm_client.py` — `LLMClient._complete`'s sticky-fallback
  cache (`_sticky_fallback_model`, a process-global dict) was leaking state
  between tests: `test_rate_limited_primary_model_falls_back_and_succeeds`
  left `LLMProvider.GEMINI` pointed at `"gemini-flash-backup"`, which then
  made `test_non_rate_limit_error_raises_immediately_without_fallback` try
  the backup model before the primary, failing its assertion. Added an
  autouse fixture that clears the cache before and after every test in
  the file.

### Not changed
- No new modules, no database schema change, no new Task states. Task
  state tracking (`TaskStatus.RUNNING/PAUSED/SUCCEEDED/FAILED/CANCELLED`,
  backend/database/models.py) and pause-preserves-progress behavior
  (`TaskQueueService._run_task`'s `wait_if_paused`, which blocks between
  steps and resumes from the next recorded `TaskStep`) were already
  correct and untouched.

## [Unreleased] - AI Model Manager integration fix (route every core LLM call through ModelManager)

The AI Model Manager feature below (manual switching, smart routing,
cross-provider fallback, temporary overrides) was fully implemented but
only actually reachable from the Chat `ai_model` command, Settings, and
the AI Models dashboard/API. The agent's real execution paths —
`agent_loop`, `decision_engine`, `vision_engine`, `teach` (Teach Mode),
the Telegram bot, and `chat_engine`'s classifier/reply calls —
instantiated `LLMClient()` directly and read `settings.llm_provider`
only, with no cross-provider fallback (only same-provider, same-model
429 retry). In other words: if the single configured provider's key was
missing, invalid, or rate-limited, the agent broke even when other
provider keys were configured and healthy, and switching providers via
Settings/AI Models/chat had no effect on what those paths actually used.

This entry closes that gap. No architecture change, no new modules —
every core path now defaults to the existing `model_manager` singleton
instead of constructing its own `LLMClient()`.

### Changed
- `backend/planner/agent_loop.py`, `backend/planner/decision_engine.py`,
  `backend/vision/vision_engine.py`, `backend/skills/teach.py`,
  `backend/telegram/bot.py`, `backend/planner/chat_engine.py` — default
  `self.llm` is now the `model_manager` singleton
  (`backend/planner/model_manager.py`) instead of `LLMClient()`. Explicit
  `llm=` injection (used throughout the test suite) is untouched, so
  tests, tools, and any other direct `LLMClient` consumer keep working
  exactly as before.
- `decision_engine.py`'s planning call, `teach.py`'s three step/skill/
  correction parses, the Telegram bot's intent classifier, and
  `chat_engine.py`'s classifier + reply calls now pass an explicit
  `task_type` (`BROWSER_AUTOMATION`, `PLANNING`, `FAST_RESPONSE`, or
  `GENERAL_CHAT`) so Smart Routing (when enabled) actually picks a
  task-appropriate provider for each of these call sites, not just for
  `vision_engine` (which already hardcoded `TaskType.VISION`).
- `backend/planner/llm_client.py` — `complete_text()`/`complete_json()`/
  `complete_json_with_image()` now accept (and ignore) an optional
  `task_type` keyword arg, so any call site or test double can use the
  exact same call signature against a raw `LLMClient` or the
  `ModelManager` interchangeably. `LLMClient` remains a single, fixed
  provider implementation — it does not route or fall back itself.
- `backend/tests/test_agent_loop.py`, `backend/tests/test_decision_engine.py`
  — `FakeLLM.complete_json()` now accepts the `task_type` kwarg.

### Result
- **Manual model switching** (`/api/ai-models/switch`, Settings, chat
  `"switch to Claude"`) now affects every core path — agent runs,
  Teach Mode, Telegram, and chat — not just new calls made through the
  API layer.
- **Smart routing** (task-type -> provider) is live for browser
  automation, planning, vision, fast-response classification, and
  general chat, wherever those task types actually occur in the code.
- **Cross-provider fallback** is live everywhere: however many provider
  API keys are configured in `.env`, that's how many providers
  participate in the fallback chain for every call site above — 1 key
  configured means only that provider is used, N keys means automatic
  fallback across all N on timeout/HTTP error/rate-limit/bad response.
- **Temporary overrides** (`use_temporarily()`, chat `"use Groq for this
  task only"`) now apply to the very next call regardless of which
  module makes it, since every path resolves through the same
  `model_manager` singleton.

### Verified
- Backend boots clean (`uvicorn backend.main:app`) with no import errors
  from the new wiring; no circular imports introduced.
- Frontend: `tsc -b && vite build` clean, `vite` dev server boots and
  serves `200`.
- Full backend suite: **365 passed**, 0 failed.

## [Unreleased] - AI Model Manager (multi-provider LLM switching, smart routing, fallback)

Extends the existing single-provider `LLMClient` (backend/planner/llm_client.py)
with a full multi-provider AI Model Manager: manual switching, automatic
task-type routing, per-provider health monitoring, cross-provider fallback,
and one-off temporary overrides — reachable from Chat, Settings, the new
AI Models dashboard page, and the REST API.

### Added
- `backend/planner/model_manager.py` — `ModelManager` (process singleton
  `model_manager`): manual `switch_provider()`/`set_default_provider()`,
  smart routing (`TaskType` enum -> provider, configurable via
  `set_routing_rule()`/`set_routing_rules()`), `use_temporarily()` one-off
  overrides that auto-clear after a single resolved call, per-provider
  `ProviderHealth` tracking (status, latency, availability, last
  success/error, rate-limit window), `fallback_chain()` +
  `complete_text()`/`complete_json()`/`complete_json_with_image()` that
  transparently retry across providers on timeout/HTTP error/rate-limit/
  bad response, `test_connection()`, and JSON state persisted to
  `data/ai_model_manager.json` (same pattern as `config_manager.py`).
  Also exports `parse_provider_name()`/`parse_task_type()` for free-text
  chat commands.
- `backend/config/settings.py` — `LLMProvider` enum grew from 4 to 20
  providers (added xAI, Moonshot, Qwen, Zhipu, Groq, Cerebras, Cohere,
  Hugging Face, NVIDIA NIM, SambaNova, Together AI, Fireworks AI,
  DeepInfra, Mistral AI, Replicate, AI21 Labs), one API-key field per new
  provider, plus `ai_smart_routing_enabled`, `ai_fallback_provider`,
  `ai_provider_priority`, `ai_disabled_providers`.
- `backend/planner/llm_client.py` — generic `_build_openai_compatible()`
  builder + `OPENAI_COMPATIBLE_PROVIDERS` table shared by the 16 new
  providers that speak the OpenAI chat/completions shape; `DEFAULT_MODELS`
  extended with a default model per new provider. Anthropic/OpenAI/
  OpenRouter/Gemini builders are untouched.
- `backend/api/routes_ai_models.py` — `GET/PUT /api/ai-models*`: full view,
  health, routing-mode, routing-rules (bulk + single), switch, fallback,
  priority, enable/disable, override (set/clear), and
  `POST /api/ai-models/test/{provider}`.
- `backend/api/routes_settings.py` — `SettingsView`/`SettingsUpdateRequest`
  grew `ai_smart_routing_enabled` and `ai_fallback_provider`; provider/
  model/routing/fallback updates are routed through `ModelManager` so
  Settings and the AI Models page stay in sync.
- `backend/planner/chat_engine.py` — new `ai_model` classifier category
  and `_handle_ai_model_command()`: "switch to Claude", "set Gemini as
  default", "use automatic routing", "use Claude for coding", "use Groq
  for this task only", "show current provider/model/health/routing".
- `frontend/src/pages/AiModels.tsx` — new dashboard page: current
  provider/model/routing mode/fallback, a routing-mode toggle, default/
  fallback provider selects, a routing-rule grid (one select per task
  type), and a provider list with health badges, enable/disable, test
  connection, and "use once" (temporary override) actions.
- `frontend/src/pages/Settings.tsx` — new "AI Models" card (smart-routing
  toggle, fallback provider, link to the full AI Models page).
- `frontend/src/lib/api.ts` — `api.aiModels.*` client, `AiModelManagerView`/
  `AiProviderInfo`/`ProviderHealth`/`AiTaskType` types; `SettingsView`
  grew the two AI Model Manager fields.
- `.env.example` — one API-key line per new provider plus the four
  `AI_*` AI Model Manager settings.
- `backend/tests/test_model_manager.py` (24 tests), additions to
  `backend/tests/test_llm_client.py` (3 tests), plus HTTP-level coverage in
  `backend/tests/test_routes_ai_models.py` (12 tests),
  `backend/tests/test_routes_settings.py` (4 tests), and
  `backend/tests/test_chat_engine_ai_models.py` (7 tests) — covering
  routing resolution, fallback-chain construction, health tracking,
  cross-provider fallback on `complete_text`, override auto-clearing,
  free-text provider/task-type parsing, every `/api/ai-models/*` endpoint,
  the `/api/settings` <-> `ModelManager` integration, and every chat
  `ai_model` command.

### Notes
- Backward compatible: `LLMClient(provider=None)` still reads
  `settings.llm_provider` directly and every existing call site (chat
  classifier, planner, vision) is unchanged. Full suite: **365 passed**
  (315 pre-existing + 50 new).
- Replicate's OpenAI-compatible endpoint only covers a subset of its
  models — pick a Replicate model known to support it, or route that
  task to a different provider.

## [Unreleased] - Social MCP connectors (X, Discord, Gmail)

Adds three new MCP connectors — X, Discord, Gmail — built on a new shared
base, `backend/mcp/connectors/social_base.py`. Unlike `github.py`, none of
these use a REST/OAuth client or store any API key/bot token/password:
every tool drives the same live, already-authenticated `BrowserEngine`
session a task/profile already has open (the same session the
Identity/Profile Manager's `SessionDetector` probes), so a connector never
fills in a login form on the user's behalf. If a session isn't
authenticated, every tool raises `SessionRequiredError` telling the caller
to log in manually.

### Added
- `backend/mcp/connectors/social_base.py` — `SocialMCPConnector` base
  class: lazy `engine_provider` resolution (always reflects whichever
  profile's session is currently live), `_ensure_session()` /
  `_detect_state()` wrapping `SessionDetector`, `require_confirm()` (the
  shared confirmation gate for irreversible outward-facing actions), and
  `status_snapshot()` for the dashboard (connection status, session
  status, account label, last-used timestamp).
- `backend/mcp/connectors/x_connector.py` — `detect_login_state`,
  `read_profile`, `read_notifications`, `draft_post`, `publish_post`,
  `reply`. `publish_post`/`reply` require `confirm=true`.
- `backend/mcp/connectors/discord_connector.py` — `detect_login_state`,
  `list_servers`, `list_channels`, `read_channel`, `send_message`,
  `reply`, `upload_file`.
- `backend/mcp/connectors/gmail_connector.py` — `detect_login_state`,
  `read_inbox`, `search_emails`, `draft_email`, `send_email`, `reply`.
  `send_email`/`reply` require `confirm=true`.
- `backend/mcp/connectors/__init__.py` — all three registered in
  `BUILTIN_CONNECTORS` (now 7 entries) plus a new `SOCIAL_CONNECTOR_NAMES`
  tuple.
- `backend/mcp/manager.py` — `default_enabled`/`default_config` wiring for
  x/discord/gmail, `wire_browser_engine_provider()` generalized to also
  wire every social connector (not just `browser`), and a new
  `social_status()` aggregating all three connectors' `status_snapshot()`.
- `backend/config/settings.py` — `mcp_x_enabled` / `mcp_discord_enabled` /
  `mcp_gmail_enabled` (default `True`) plus display-only account label
  settings (`mcp_x_account`, `mcp_discord_account`, `mcp_gmail_account`;
  never credentials).
- `backend/api/routes_mcp.py` — `GET /api/mcp/social-status`.
- `frontend/src/lib/api.ts` — `SocialConnectorStatus` type and
  `api.mcp.socialStatus()`.
- `frontend/src/pages/Mcp.tsx` — `SocialConnectorsPanel` showing
  connection/session status, account, and last-used per connector, plus
  config summaries for x/discord/gmail.
### Notes
- Backward compatible: filesystem/terminal/browser/github connectors and
  their existing tests are unchanged. Full suite: 315 passed.
- Discord's `send_message`/`reply`/`upload_file` are marked `destructive`
  (dashboard-visible) but, unlike X/Gmail, are not gated behind
  `require_confirm()` — sending a chat message is treated as reversible
  enough (delete-able in Discord itself) not to need an extra
  confirmation round-trip.

## [Unreleased] - Identity & Profile Manager

Adds `backend/identity/` — the Identity & Profile Manager: named, reusable
Chrome browser profiles (each with its own on-disk, persistent Chrome
user-data directory), wallet/Gmail/X/Discord account linking, best-effort
login-state detection, and full `TaskQueueService` integration so a task
can run as a specific profile and reuse that identity's cookies, local
storage, session storage, and extensions instead of starting logged out.
Built across four sessions (backend models + `backend/identity/` core in
sessions 1-2, the dashboard page and REST API alongside them, then two
sessions of test coverage); this entry documents the whole feature.

### Added
- `backend/identity/fs.py` — `ProfileFilesystem`: create/delete/clone a
  profile's Chrome user-data directory, plus a read-only `inspect()`
  summary (cookies presence/size, local/session storage presence,
  extension ids) for the dashboard.
- `backend/identity/detector.py` — `SessionDetector` /
  `SUPPORTED_SERVICES` (`gmail`, `x`, `discord`): read-only login
  detection via each service's own "am I logged in" redirect, never
  touching a password field.
- `backend/identity/registry.py` — `ProfileRegistry`: CRUD, search/tag/
  `enabled_only` filtering, clone (metadata + full Chrome profile
  directory copy), metadata-only export/import (strips `id` and
  `chrome_profile_dir`, no credentials), enable/disable,
  `select_active_profile`'s single-active-profile invariant, and a
  per-profile activity log.
- `backend/identity/manager.py` — `ProfileManager`: the
  `load_for_task()` / `check_sessions()` / `release()` facade
  `TaskQueueService` calls, composing `ProfileRegistry` +
  `SessionDetector` (same shape as `MCPManager` composing
  registry/discovery/router).
- `backend/planner/task_queue.py` — `TaskQueueService(profiles=...)` and
  `enqueue(..., profile_label=...)`; `_run_task` resolves the profile
  before launching the browser (fail-fast on a bad reference — `Task` →
  `FAILED` with a `Report` row, no browser ever started), wires the
  resolved `chrome_profile_dir` into `BrowserEngine`'s `user_data_dir`,
  and computes `effective_wallet_label` (explicit `task.wallet_label`
  wins, otherwise the loaded profile's `wallet_label`). A `profile_label`
  with no `ProfileManager` configured is accepted but ignored — full
  backward compatibility.
- `backend/api/routes_profiles.py` — `/api/profiles` REST surface: CRUD,
  `/active`, export/import, clone/rename, enable/disable/select,
  sessions status + manual re-check (409 with no active browser),
  filesystem inspection, activity log, and supported-services metadata.
- Dashboard: new **Profiles** page — create/list/search/filter, clone/
  export/import, enable/disable/select-active, session status, and
  filesystem inspection.

### Added — Tests
- `backend/tests/test_profile_registry.py` (25 tests) — CRUD, search/tag/
  enabled filtering, clone, export/import round-trip, enable/disable/
  select-active invariant, session-check recording, activity log.
- `backend/tests/test_profile_fs.py` (8 tests) — create/delete/clone/
  inspect on-disk behavior.
- `backend/tests/test_profile_manager.py` (7 tests) — `load_for_task`/
  `check_sessions`/`release`.
- `backend/tests/test_task_queue_profile.py` (5 tests) — `profile_label`
  persistence on the `Task` row; `effective_wallet_label` fallback
  verified both ways (profile wins, then task wins) by monkeypatching
  `BrowserEngine.start` to a no-op and `AgentLoop` to a fake that
  captures the `wallet_label` argument `.run()` was called with; the
  profile-not-found early-failure path (`Task` → `FAILED`, a `Report` row
  created, `BrowserEngine.start` monkeypatched to raise if called,
  proving the early return happens before any browser launch); and the
  `profiles=None` backward-compatibility path (a `profile_label` is
  accepted but ignored, task still runs to `SUCCEEDED`).
- `backend/tests/test_routes_profiles.py` (9 tests) — FastAPI app +
  `ASGITransport` + `AsyncClient` pattern (matching
  `test_routes_agent.py`), the full `/api/profiles` route surface
  including the 404s for an unknown id and the 409 for
  `/sessions/check` with no active browser.
- `backend/tests/conftest.py` — a new autouse fixture forcing open auth
  mode for the whole test session, added alongside the profile test
  suites.

### Verified
- Backend: recreated the venv from a clean `requirements.txt` install and
  ran `pytest backend/tests -q` — **312 passed, 0 failed** (54 of those
  from the Identity & Profile Manager's five test files above).
- Frontend: not touched in the sessions that added test coverage: no
  `npm ci && npm run build && npx oxlint src` re-run was needed for
  those; the dashboard `Profiles` page and `api.ts` profiles client were
  built and verified (`tsc -b && vite build` clean, `oxlint src` clean)
  in the earlier session that added them.
- Backward compatibility: a task with no `profile_label` runs exactly as
  before; a `profile_label` set with no `ProfileManager` configured is
  accepted but ignored rather than erroring.

### Documentation
- `README.md` — new "Identity & Profile Manager (new)" section (module
  breakdown matching the Skill Learning System / MCP Core sections), a
  Phase 2 progress table row (item 9, now ✅ done), a `backend/identity/`
  repo-layout entry, and a **Profiles** dashboard-page bullet.

## [Unreleased] - MCP Core test coverage + validation pass

A test-coverage-and-validation pass over the existing MCP Core
(`backend/mcp/` — base/registry/router/discovery/manager/client, the
filesystem/terminal/browser/github connectors, and their Chat/Telegram/
AgentLoop/Skills/Memory/dashboard integration points). No architecture
changes; one dead-code removal, one real bug fix (the `mcp_enabled` master
switch), and one small extension point added to `MemoryStore` to make it
testable without network access.

### Added — Tests
- `backend/tests/test_mcp_registry.py` — enable/disable/configure
  persistence (JSON file + in-memory record), isolated failure-on-connect,
  JSON reload via a second `MCPRegistry` pointed at the same `data_dir`,
  secret redaction in `list_connectors()`.
- `backend/tests/test_mcp_router.py` — explicit connector+tool hints bypass
  scoring, a connector hint alone still scores within that connector, the
  keyword pass fires correctly, no match below `min_score` returns `None`,
  an empty candidate list returns `None`.
- `backend/tests/test_mcp_manager.py` — `route_and_call` end-to-end against
  a fake in-memory connector, `call()` against a never-enabled connector
  returns `ok=False` (not an exception), an `MCPToolError` from
  `call_tool()` is isolated into a failed `ToolCallResult`, `on_call` fires
  exactly once per call (via both `call()` and `route_and_call()`) with the
  full `(connector, tool, arguments, result)` tuple, an exception inside
  `on_call` never propagates out of `call()`, and (new, see "Fixed" below)
  three tests covering the `mcp_enabled` master switch.
- `backend/tests/test_mcp_filesystem_connector.py` — path traversal
  (relative `../..` and absolute `/etc/passwd`) outside configured roots is
  rejected, a full write/read/search/list/delete round-trip inside a
  `tmp_path` root, append mode, delete-a-directory rejected, unknown tool
  rejected.
- `backend/tests/test_mcp_terminal_connector.py` — the double-gating
  behavior (`connect()` checks both a config flag captured once at
  construction and the same flag re-checked live) is exercised directly:
  disabled-by-default with no `enabled` key, `enabled=False` explicit,
  `enabled=True` at construction connects, default allow-list used when
  none configured, allow-list rejection, five shell-metacharacter
  rejection cases (`; | \` $ &`), empty command rejected, a real
  allow-listed `echo hi` runs with `exit_code 0`, an allow-listed but
  nonexistent binary raises "executable not found".
- `backend/tests/test_mcp_browser_connector.py` — `fetch_url` strips
  script/style content correctly, `extract_text=False` returns raw HTML,
  `get_page_links` extracts href+text pairs, `fetch_url` requires a
  non-empty url, `current_page_snapshot` raises distinct `MCPToolError`s
  for "no engine provider wired" vs "no active session", and returns the
  right snapshot fields with a fake engine provider.
- `backend/tests/test_mcp_github_connector.py` — a recording subclass
  captures `(method, path, params, json_body)` instead of hitting the
  network; verifies `get_repository`/`list_issues`/`create_issue`/
  `list_pull_requests`/`get_file_contents` build the right request shape,
  config defaults vs explicit-arg overrides, required-field validation,
  and `health_check()`'s "degraded: no token configured" vs "ok" states.
- `backend/tests/test_chat_engine_mcp.py` — the `mcp` chat category
  dispatches to `route_and_call()` with the right `(request_text,
  connector_hint)`, falls back to raw text when the intent has no
  `mcp_query`, formats a failed result as `"[connector.tool] failed: <e>"`,
  returns a "couldn't figure out which tool" message on no match, and
  returns the "MCP Core isn't enabled" fallback when `state.mcp` or
  `state` itself is `None`.
- `backend/tests/test_memory_store_mcp.py` — `save_tool_call()` writes a
  `MemoryEntry` with `kind="mcp_call"` and the right metadata for both a
  successful and a failed `ToolCallResult`, plus a plain object with no
  `ok`/`output`/`error` attributes to confirm the `getattr(..., None)`
  fallback doesn't raise.
- `backend/tests/test_agent_loop_mcp_tool.py` — an explicit
  `"connector.tool"` target calls `mcp.call()` directly (never
  `route_and_call`), free text calls `route_and_call()`, a no-match returns
  a clear failure note, `mcp=None` returns a clear "no MCPManager
  configured" note, a failed result's error appears in the note exactly as
  `mcp[connector.tool]: <e>`, invalid-JSON arguments fall back to `{}`, and
  `StepAction.MCP_TOOL` routes through `AgentLoop._execute_action`
  end-to-end.
- `backend/tests/test_telegram_mcp_command.py` — `/mcp` with no args sends
  `"list my mcp connectors"` through `chat_engine.send_message`; `/mcp`
  with args joins and passes them through; `auth_required` still applies
  (unauthorized users get "Not authorized." and `send_message` is never
  awaited).

### Fixed
- `backend/mcp/manager.py` — `settings.mcp_enabled` (the documented master
  switch for MCP Core) had no effect: `MCPManager.start()` checked
  `getattr(self, "_enabled_gate", True)`, but nothing ever set
  `_enabled_gate`, so the `getattr` default of `True` always won and
  `MCP_ENABLED=false` did nothing — MCP Core still started every connector.
  `__init__` now sets `_enabled_gate = True` explicitly, and
  `from_settings()` sets it from `bool(getattr(settings, "mcp_enabled",
  True))`. `state.mcp` remains a real object either way (chosen over
  skipping its construction entirely, to keep every existing `if state.mcp:`
  /  `getattr(app_state, "mcp", None)` check working unchanged) — when
  disabled, `start()` now just no-ops and logs that it did, so the `/mcp`
  API and dashboard still respond, reporting every connector disconnected.
  Covered by three new tests in `test_mcp_manager.py`.
- `backend/mcp/client.py` — removed an unused `field` import from
  `dataclasses` (`ClientStats`'s fields all have plain defaults). Verified
  via an AST-based unused-import scan across all of `backend/mcp/` — no
  other dead imports, debug prints, TODOs, or breakpoints found.

### Changed
- `backend/memory/store.py` — `MemoryStore.__init__` now accepts an
  optional `embedding_function` parameter (default `None`, unchanged
  production behavior: chromadb's built-in ONNX MiniLM function). Exists
  so tests (or any deployment without egress to chroma's model bucket)
  can inject a lightweight, network-free stand-in instead of triggering a
  model download on first `upsert()` — used by the new
  `test_memory_store_mcp.py` fixture.

### Verified
- Backend: `pytest backend/tests` — **258 passed**, 0 failed (176
  pre-existing + 79 new MCP tests + 3 new `mcp_enabled` regression tests),
  run against the real dependencies (not just `py_compile` + manual
  review, as in the prior session).
- Frontend: `tsc -b && vite build` clean (no type errors); `oxlint src`
  clean (0 errors, the same 1 pre-existing informational warning in
  `toast-provider.tsx` noted in the Skill Learning System entry below,
  unrelated to MCP). Reviewed `Mcp.tsx`, `api.ts`'s `mcp` client, `App.tsx`,
  and `AppShell.tsx` for dead/duplicate code — none found.

### Documentation
- `README.md` — new "MCP Core (new)" section (module-by-module breakdown,
  matching the style of the Skill Learning System section above it), a
  Phase 2 progress table row, a `backend/mcp/` repo-layout entry, and an
  **MCP** dashboard-page bullet.
- `.env.example` — the `MCP_*` settings block (added in the prior session)
  is present and accurate; its `MCP_ENABLED` comment updated to reflect
  that the master switch now actually gates `start()` instead of being a
  documented no-op.

## [Unreleased] - Skill Learning System production-readiness review

A validation-and-hardening pass over the existing Skill Learning System
(`backend/skills/`) — no architecture changes, no new features. Confirmed the
Skill Library, matcher, runner, Teach Mode, versioning, import/export, and
every integration point (Chat, Telegram, Planner/`TaskQueueService`, API,
dashboard) are wired correctly and don't regress `AgentLoop`, `BrowserEngine`,
`PluginRegistry`, `TaskQueueService`'s pause/resume/retry, or `MemoryStore`.

### Added — Tests
- `backend/tests/test_skill_runner.py` (9 tests) — `SkillRunner` had zero
  direct test coverage; added tests for full-workflow replay with variable
  substitution, caller-supplied variable overrides, first-failure
  short-circuiting (and the resulting fallback-to-planning summary),
  empty-workflow handling, navigation failure, a `navigate`-type workflow
  step using `value` as the URL, unknown actions, running with no
  `website_hint`, and step-level exception safety.

### Fixed — Telegram
- `backend/telegram/bot.py::on_free_text` — the `"unknown"` intent (per
  `INTENT_SYSTEM_PROMPT`: reserved for genuinely gibberish/empty input) was
  being routed into the same LLM-backed `ChatEngine` conversational path as
  `"chat"`, instead of the plain help-hint reply the code's own
  classification contract promises. This was silently swallowing gibberish
  input into a live LLM call it was never meant to reach, and made
  `test_on_free_text_unknown_intent_falls_back_to_help_hint` dependent on
  live network access to `api.anthropic.com` (previously noted in this file,
  under v1.2, as a known pre-existing failure in network-isolated
  environments). `"chat"` and `"unknown"` are now routed separately;
  `"unknown"` (and any other unrecognized intent value) gets the deterministic
  `"Not sure what you want — try /help."` reply with no outbound call.

### Verified
- Backend: `pytest backend/tests` — **176 passed**, 0 failed (167 pre-existing
  + 9 new), including the full `test_skills.py` (43) and new
  `test_skill_runner.py` (9) suites, and the now-deterministic
  `test_telegram_bot.py`.
- Frontend: `tsc -b && vite build` clean (no type errors), `oxlint src` clean
  (0 errors, 1 pre-existing informational warning in
  `toast-provider.tsx`, unrelated to Skills).
- Reviewed for backward compatibility: `SkillService`/`SkillMatcher` are
  `Optional` everywhere they're threaded through (`TaskQueueService`,
  `ChatEngine`, `routes_skills.py`'s `_library()`/`_teach()`), so
  `SKILLS_ENABLED=false` fully restores pre-Skill-System behavior with no
  code path affected.

### Documentation
- `README.md` — added a "Skill Learning System" section (module-by-module
  breakdown, matching the style of the other Phase 2 feature writeups), a
  Phase 2 progress table row, a `backend/skills/` repo-layout entry, and a
  **Skills** dashboard-page bullet.
- `.env.example` — added `SKILLS_ENABLED` / `SKILLS_MATCH_MIN_SCORE`, which
  `backend/config/settings.py` already defined but weren't documented there.

## [Unreleased] - v1.2 Conversational AI Chat

Turns Nexus-Agent into something you can just talk to, on the dashboard and over
Telegram, without touching the existing architecture: task execution, agent
lifecycle, and browser observation are still owned entirely by
`TaskQueueService`, `AgentRuntime`, and `LiveSessionManager`. This adds exactly
one new composing layer on top of them.

### Added — Chat engine and persistence
- `backend/database/models.py` — new `ChatSession` / `ChatMessage` tables.
  Sessions persist across restarts and store only lightweight continuity state
  (`last_task_id`, `last_error`); "current" task/browser/website/action is read
  live from `AgentRuntime` / `LiveSessionManager` at answer time, never
  duplicated, so it can't go stale.
- `backend/planner/chat_engine.py` — new `ChatEngine`: classifies every message
  into `conversation | question | browser_command | agent_command | task |
  settings | system_request` via one LLM call, then dispatches to the existing
  module that already owns that behavior. Only messages that actually describe
  new work become tasks. Covers "open <url>", "search for X", "summarize this
  page", "take a screenshot", "show browser", "pause/resume/stop", "continue
  the previous task", "check my current task", "explain why you failed",
  "explain your last action", "what happened today", plus general
  conversation/questions with short persistent history for multi-turn context.

### Added — API and dashboard
- `backend/api/routes_chat.py` — `GET/POST /api/chat/sessions`, `GET/POST/DELETE
  /api/chat/sessions/{id}/messages`, `GET /api/chat/sessions/{id}/export`. Same
  bearer-auth dependency as every other router.
- Frontend: new **AI Chat** page (`frontend/src/pages/Chat.tsx`) — conversation
  panel with category badges, plus a live sidebar (current task, live browser
  screenshot polling, quick stats), reusing the existing `api.agent.status()`
  and `api.browser.screenshotBlob()` endpoints. Added to the sidebar nav and
  `api.chat.*` typed client methods.

### Changed — Telegram
- `backend/telegram/bot.py` — the natural-language fallback (`_handle_chat`,
  reached when a message doesn't match a fast slash command or structured
  intent) now delegates to the same `ChatEngine`, keyed by
  `telegram:<chat_id>`. Telegram conversations get persistent, DB-backed
  history (previously an in-memory dict cleared on restart) and the full
  task/agent-command/browser-command/system-request taxonomy above, not just
  small talk. All existing slash commands (`/task /pause /resume /stop
  /restart /report /...`) and the existing structured `on_free_text` intent
  routing are unchanged and still pass their existing tests unmodified.

### Verified
- Backend: 123/123 new-and-existing tests passing (`pytest backend/tests`), up
  from 115 — 8 new tests in `backend/tests/test_chat_engine.py` plus all
  pre-existing suites green, including `test_telegram_bot.py`'s existing
  structured-intent and fallback tests. (One pre-existing test,
  `test_on_free_text_unknown_intent_falls_back_to_help_hint`, requires live
  network access to `api.anthropic.com` and fails the same way on the
  unmodified repo in a network-isolated environment — confirmed via `git
  stash` before making any changes; unrelated to this change.)
- Frontend: `tsc -b` clean, `vite build` succeeds (1896 modules, no errors),
  `oxlint` clean (0 errors, 1 pre-existing informational warning unrelated to
  this change).

## [Unreleased] - v1.1 System Monitoring & Telegram AI Chat

Extends the existing agent with an operational layer (health/diagnostics/resources/
config backup/build info) and upgrades the Telegram bot into a full conversational
interface. No architectural changes — every addition composes existing modules
(`TaskQueueService`, `AgentRuntime`, `MemoryStore`, `PluginRegistry`, `LLMClient`)
rather than re-implementing them.

### Added — System Monitoring
- `backend/monitoring/health.py` — `HealthMonitor`: aggregated health check across
  backend, database, browser, memory, AI provider, Telegram, and WebSocket layer.
  Each check is isolated (one failing component never blocks the others) and timed.
- `backend/monitoring/diagnostics.py` — `DiagnosticsService`: deeper on-demand
  environment check (Playwright installed, AI API key present, DB reachable,
  plugins loadable, memory store initialized, required env vars set), with both
  a structured JSON report and a human-readable text report.
- `backend/monitoring/resources.py` — `ResourceMonitor`: CPU%, process/system RAM,
  an estimate of Chromium child-process memory, task queue depth, and active task
  count. Uses `psutil` when available and degrades gracefully (returns `null`
  metrics, never raises) when it isn't.
- `backend/config/config_manager.py` — `ConfigManager`: export/import/backup/restore
  for the same non-secret settings surface `routes_settings.py` already exposes.
  Secrets (API keys, tokens) are never included. Backups are timestamped JSON
  snapshots under `data/config_backups/`.
- `backend/integrations/github_info.py` — reads local git metadata (commit, branch,
  dirty flag, nearest tag as version, remote repo URL) for build/version info with
  no network call and no token required.
- `backend/api/routes_system.py` — new `/api/system/*` routes composing all of the
  above: `GET /health`, `GET /diagnostics` (+ `/diagnostics/text`), `GET /resources`,
  `GET /version`, `GET/POST /config/export|import|backup|backups|restore`.
- Frontend: new `System` page (`frontend/src/pages/System.tsx`) rendering health,
  diagnostics, resources, and build info, with a one-click config backup button.
  Added to the sidebar nav and `api.system.*` typed client methods.
- 9 new backend tests (`backend/tests/test_system_routes.py`), including an explicit
  assertion that config export never leaks secret fields.

### Added — Telegram AI Chat
- `backend/telegram/bot.py`: added `/health`, `/diagnostics`, `/resources`, and
  `/restart` commands.
- Expanded the natural-language intent schema (`INTENT_SYSTEM_PROMPT`) to cover
  `health`, `diagnostics`, `resources`, `restart`, `tasks`, `report`, and
  `browser_status` in addition to the existing task/pause/resume/stop intents, with
  guidance examples so free-form phrasing ("how's everything doing?", "restart the
  agent") routes correctly.
- `/status`, `/report`, `/tasks`, and `/browser` now return real live data (agent
  runtime status, recent task list, recent reports, live browser URL/title) instead
  of pointing the user at the REST API.
- `/pause`, `/resume`, and `/stop` now route through `AgentRuntime` when the bot is
  constructed with the full `AppState` (`NexusTelegramBot(queue, app_state=state)`),
  giving Telegram the same single Start/Stop/Pause/Resume surface as the dashboard.
  Falls back to direct `TaskQueueService` calls when `app_state` isn't provided, so
  existing callers that only pass `queue` are unaffected.
- 13 new backend tests (`backend/tests/test_telegram_bot.py`) covering the live-data
  helpers, NL intent routing (including LLM-failure and unknown-intent fallbacks),
  and graceful degradation when `app_state` is `None`.

### Fixed
- `ConfigManager.import_settings` was assigning raw strings onto enum-typed settings
  fields (`browser_channel`, `llm_provider`). Pydantic `BaseSettings` does not
  re-validate plain attribute assignment, so a config export→import round-trip
  silently corrupted the shared `settings` singleton for the rest of the process
  (caught by the full test suite, not the isolated new tests — a reminder to always
  run the whole suite, not just the new file). Now re-parses enum fields through
  their enum class before assigning.

### Verified
- Backend: 115/115 tests passing (`pytest backend/tests`), up from 102.
- Frontend: `tsc -b` clean, `vite build` succeeds (1895 modules, no errors), `oxlint`
  clean (0 errors, 1 pre-existing informational warning unrelated to this change).

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
