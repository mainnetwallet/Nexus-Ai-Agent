# Nexus-Agent Architecture

This doc covers how the pieces fit together, with emphasis on the three
"core infrastructure" subsystems completed for v1.0: the WebSocket layer,
the Task Scheduler, and the AI Decision Engine. It's a companion to the
per-feature detail in `README.md` and `CHANGELOG.md`, not a replacement for
either -- read those for what shipped when and why.

## High-level data flow

```
POST /api/tasks (website, goal, wallet_label)
        |
        v
  Task row (SQLite, status=queued) -----------------+
        |                                            |
        v                                            |
  TaskQueueService._worker_loop()                    |
   - pops highest-priority due task                  |  GET /api/tasks
   - opens a BrowserEngine (Playwright)               |  GET /api/tasks/{id}
   - builds an AgentLoop for it                       |  WS  /api/tasks/ws/live
        |                                            |
        v                                            |
  AgentLoop.run()  <--- delegates to --->  DecisionEngine
   - navigate                                - perceive() (DOM + vision/OCR fallback)
   - recall similar workflows (memory)        - decide()  (LLM -> Decision)
   - loop: perceive -> decide -> execute      - verify()  (did the URL change?)
     -> verify -> (recovery hint) -> repeat   - recovery_hint() (folded into next decide())
        |
        +--> WalletManager.handle_pending_popup() on "wallet_popup" action
        +--> PluginRegistry.dispatch_* at task_start/step/task_finish/wallet_popup
        +--> MemoryStore.save_workflow_outcome() at the end
        |
        v
  Report row (status, summary, screenshots)
```

Four live views sit alongside this loop, all behind the same
`require_auth` bearer-token dependency as the REST routes:

| Stream | Route | Source |
|---|---|---|
| Task lifecycle | `WS /api/tasks/ws/live` | `TaskQueueService`'s `notify_fn` -> `routes_tasks.broadcast()` |
| Browser frames | `WS /api/browser/ws/live` | `LiveSessionManager` polling `TaskQueueService.current_engine` |
| Backend logs | `WS /api/logs/ws/live` | `WebSocketLogBroadcastHandler` attached to the root logger |
| Plugin events | `WS /api/plugins/ws/live` | `PluginRegistry`'s `event_fn` -> `routes_plugins.broadcast()` |

## AI Decision Engine (`backend/planner/decision_engine.py`)

`AgentLoop` used to build the LLM prompt and run the vision fallback inline.
That reasoning is now a separate `DecisionEngine`, constructed by
`AgentLoop.__init__` from the same `llm`/`vision` instances it's given (so
tests that pass a fake LLM into `AgentLoop` are unaffected):

- **`perceive(snapshot, goal)`** -- reads the DOM snapshot `BrowserEngine`
  already captured; if it came back too sparse (canvas UI, image-only page),
  runs the existing OCR + vision-LLM fallback and merges the result in. No
  change from the prior inline version other than being callable/testable on
  its own.
- **`decide(goal, wallet_label, notes, snapshot, prior_context,
  recovery_context)`** -- sends the perception to the planner LLM and parses
  its strict-JSON response into a `Decision` dataclass
  (`action/target/value/reasoning/confidence`). Logs the full decision via
  `logging.getLogger("nexus.decision_engine")`.
- **`verify(url_before, url_after, action, success)`** -- after `AgentLoop`
  executes the action, checks whether the URL actually changed and logs a
  `VerificationResult`. This is observational: `AgentLoop`'s own stall
  counter (four consecutive no-change steps => `failed`) still drives the
  actual failure decision, unchanged from before this refactor.
- **`recovery_hint(action, target, success, stall_count)`** -- when the
  previous action failed, or the page has stalled for 2+ steps, returns a
  short instruction that gets folded into the *next* `decide()` call's
  prompt (a `RECOVERY: ...` block). This is the "handles recovery" piece:
  it's advisory context for the LLM's next decision, not a hardcoded
  retry/backoff policy, which keeps the "no site-specific logic" invariant
  intact -- the recovery strategy the LLM picks (scroll, wait, try a
  different element, give up as blocked) is still reasoned per-page.

Every `decide`/`verify`/`recovery_hint` call logs through the standard
`logging` module, which is what feeds `WS /api/logs/ws/live` -- there's no
separate "decision log" storage surface, by design, to avoid adding a new
persistence path for something that's already visible in logs.

## Task Scheduler (`backend/planner/task_queue.py`)

Reviewed for v1.0 and found already complete; nothing in this increment
changed it. For reference, what it provides:

- **Persistence**: every task is a row in the SQLite `Task` table
  (`backend/database/models.py`), not an in-memory-only queue -- a restart
  loses in-flight browser state but not the queue itself (`queued`/`paused`
  tasks are picked back up by `_worker_loop`'s next poll).
- **Priority**: `_pop_next()` orders by `Task.priority DESC, Task.created_at
  ASC`, and honors `scheduled_for` (deferred tasks aren't eligible until
  their time arrives).
- **Pause/resume**: both queue-wide (`pause()`/`resume()`, an
  `asyncio.Event` the worker loop waits on) and per-task
  (`pause_task()`/`resume_task()`, a per-task `asyncio.Event` threaded into
  `AgentLoop` as `wait_if_paused`).
- **Retry/cancel**: `retry()` re-queues a `failed`/`cancelled` task and
  resets its retry counter; `cancel()` marks a task cancelled and, if it's
  currently paused, wakes it so `AgentLoop.should_cancel()` is observed
  immediately instead of hanging until a resume.
- **Background execution**: `start_worker()` launches `_worker_loop()` as an
  `asyncio.Task` that runs for the life of the process, driving one task at
  a time (one `BrowserEngine` per in-flight task).

## Plugin event stream (new in this increment)

`PluginRegistry` takes an optional `event_fn: Callable[[str], Awaitable[None]]`
(default `None`). When set, every lifecycle change
(`plugin_enabled`/`plugin_disabled`/`plugin_reloaded`/`plugin_reload_failed`)
and every hook dispatch (`task_start`/`task_step`/`task_finish`/
`wallet_popup`) calls `self._emit(event_type, **fields)`, which JSON-encodes
the event and awaits `event_fn`. Failures in `event_fn` are caught and
logged -- same isolation guarantee `_isolated()` already gives plugin hooks,
so a broken WebSocket broadcast can never take down task execution.
`backend/main.py` wires this to `routes_plugins.broadcast()`, which fans the
JSON string out to every client connected to `WS /api/plugins/ws/live`.

## Non-goals for this increment

Per the "core infrastructure only" scope: no changes to the browser engine,
wallet manager, memory store, vision/OCR modules, Telegram bot, or the
frontend dashboard. `frontend/src/lib/api.ts` does not yet have typed
clients for `/api/logs/ws/live` or `/api/plugins/ws/live` -- the Logs and
Plugins dashboard pages still use their original polling-based views. Wiring
the dashboard to these two new streams is a reasonable next increment but
was out of scope here (frontend was reviewed, not redesigned).
