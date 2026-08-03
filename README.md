# Nexus-Agent

An autonomous, **generic** browser agent: point it at any website + a goal + a wallet
label, and it perceives the page, plans the next action with an LLM, executes it via
Playwright, verifies the result, and repeats — with persistent memory of what has
worked before, and full remote control from Telegram.

**No website-specific code anywhere.** The planner reasons only from what's currently
visible on the page (text + interactive elements), matching the "generic website
engine" requirement: it never hardcodes selectors or logic for any particular site.


## আগে থেকে Repo Clone করা থাকলে (Update)

```bash
cd Nexus-Ai-Agent
git pull origin main
```
Pull-এর পর server restart করুন। প্রয়োজনে `pip install -r requirements.txt` / `npm install`।

শুধু frontend: `cd frontend && git pull origin main && npm run dev`


## এক Command-এ Backend + Frontend দুটোই চালান

**Windows:**
```powershell
cd Nexus-Ai-Agent
.\scripts\dev.ps1
```

**Linux / Mac:**
```bash
cd Nexus-Ai-Agent
chmod +x scripts/dev.sh
./scripts/dev.sh
```
শুধু frontend log এই window-এ দেখাবে। Backend log hide করা থাকে, `logs/backend.log`-এ যায়। `Ctrl+C`-তে দুটোই বন্ধ হবে। (আগে থেকে `.venv` ও `npm install` করা থাকতে হবে — নিচের step ৩ দেখুন।)

**`logs/backend.log`-এ `No module named uvicorn` error থাকলে**, dependencies install করা নেই — এটা চালান:
```powershell
cd Nexus-Ai-Agent
.venv\Scripts\activate
pip install -r requirements.txt
```
তারপর আবার `.\scripts\dev.ps1` চালান।


## একদম Simple Guide (৫ Step)

**১) Clone**
```bash
git clone https://github.com/mainnetwallet/Nexus-Ai-Agent.git
cd Nexus-Ai-Agent
```

**২) `.env` বানান**
```bash
cp .env.example .env
```
`.env`-এ `ANTHROPIC_API_KEY` আর `API_AUTH_TOKEN` বসান।

**৩) Install**
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chrome
```

**৪) Run**
```bash
python -m uvicorn backend.main:app --reload
```
→ `http://127.0.0.1:8000`, agent auto-active।

**৫) Task দিন**
```bash
curl -X POST http://127.0.0.1:8000/api/tasks \
  -H "Authorization: Bearer YOUR_API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"website": "https://example.com", "goal": "create an account", "wallet_label": "Wallet-01"}'
```

Dashboard (optional): `cd frontend && npm install && npm run dev` → `http://localhost:5173`


## Status

Phase 1 (working, tested backbone) সম্পূর্ণ। **Phase 2 চলছে** — ধাপে ধাপে, প্রতিটা feature-এর সাথে test আর doc আপডেট নিয়ে।

| # | Feature | অবস্থা |
|---|---------|--------|
| 1 | Browser Vision (vision-LLM fallback) | ✅ |
| 2 | OCR (Tesseract fallback) | ✅ |
| 3 | Live Browser Session | ✅ |
| 4 | React Dashboard | ✅ |
| 5 | WebSocket live updates (tasks, browser, logs, plugins) | ✅ |
| 6 | Task Scheduler (pause/resume/retry/cancel/priority) | ✅ |
| 7 | Settings page | ✅ |
| 8 | AI model switching | ✅ |
| 9 | Chrome Profile Manager | ✅ |
| 10 | Plugin System | ✅ |
| 11 | Browser crash recovery | ⏳ |
| 12 | Memory improvements | ⏳ |
| 13 | AI Decision Engine | ✅ |
| 14 | Skill Learning System | ✅ |
| 15 | MCP Core | ✅ |
| 16 | Social MCP connectors (X, Discord, Gmail) | ✅ |
| 17 | AI Model Manager (multi-provider, smart routing, fallback) | ✅ |

### মূল Feature গুলো এক নজরে

- **AI Model Manager** — 20 provider (Anthropic, OpenAI, Gemini, OpenRouter,
  xAI/Grok, Moonshot/Kimi, Qwen, Zhipu/GLM, Groq, Cerebras, Cohere,
  Hugging Face, NVIDIA NIM, SambaNova, Together AI, Fireworks AI,
  DeepInfra, Mistral AI, Replicate, AI21) এক জায়গা থেকে manage করা যায়:
  manual switch (`switch to Claude` লিখলেই Chat-এ হয়ে যায়), task-type
  ভিত্তিক automatic smart routing, provider health/latency monitoring,
  timeout/rate-limit-এ automatic cross-provider fallback, আর "use Gemini
  for this task only" টাইপ temporary override। দেখুন dashboard-এর
  **AI Models** page অথবা `GET/POST /api/ai-models/*`।
  এখন এটাই **একমাত্র entry point** সব AI request-এর জন্য — agent loop,
  decision engine, vision, Teach Mode, Telegram bot, chat, সবকিছু এর
  মধ্য দিয়ে যায়, তাই manual switching/smart routing/fallback/temporary
  override সব জায়গায় কাজ করে, শুধু Settings/dashboard থেকে না।
  `.env`-এ যতগুলো provider-এর key দেওয়া থাকবে, fallback ঠিক ততগুলোর
  মধ্যেই হবে (১টা key → ওই ১টাই, N টা key → N টার মধ্যে automatic
  fallback) — কোথাও hardcoded single-provider bypass নেই।

- **AI Decision Engine** — perceive → decide → verify → recover চক্রে চলা আলাদা reasoning module।
- **Browser Vision + OCR** — canvas-heavy বা image-only পেজে vision-LLM ও Tesseract OCR দিয়ে fallback perception।
- **Plugin Framework** — core module না ছুঁয়ে নতুন hook/feature যোগ করার সিস্টেম।
- **Live Browser Session** — agent এখন কোন website-এ কী করছে, সেটা real-time দেখার সুবিধা।
- **Autonomous Agent Runtime** — backend চালু হলেই agent নিজে থেকে চলতে থাকে, continuous loop।
- **System Monitoring + Telegram AI Chat** — health monitor আর Telegram-এ agent-এর সাথে সরাসরি chat।
- **Conversational AI Chat** — শুধু task queue না, একটা পূর্ণ conversational agent হিসেবেও কাজ করে।
- **Skill Learning System** — একবার solve করা task থেকে reusable "skill" শিখে রাখে।
- **MCP Core** — filesystem/terminal/browser/github connector, Chat ও Planner-এর জন্য auto-routing সহ।
- **Social MCP connectors (X, Discord, Gmail)** — এগুলো কোনো API key/OAuth app বা bot token লাগে না; profile-এর already-logged-in browser session (Identity/Profile Manager যেটা manage করে) reuse করে DOM automation-এর মাধ্যমে কাজ করে। একটা connector কখনো নিজে থেকে username/password টাইপ করবে না — session আনঅথেন্টিকেটেড পেলে শুধু "please log in manually" বলে জানিয়ে দেয়। X-এর `publish_post`/`reply` এবং Gmail-এর `send_email`/`reply` — এই আউটবাউন্ড, irreversible action গুলো `confirm=true` ছাড়া চলবে না: draft আগে ইউজারকে দেখাতে হবে, approve করলে তবেই `confirm=true` দিয়ে আবার call করতে হয়। Status/session/account info দেখতে `GET /api/mcp/social-status`।
- **Identity & Profile Manager** — প্রতিটা automated identity-র জন্য আলাদা Chrome profile + wallet।

বিস্তারিত জানতে দেখুন `CHANGELOG.md` (প্রতিটা ধাপে কী শিপ হয়েছে) এবং `docs/ARCHITECTURE.md` (WebSocket layer, Task Scheduler, AI Decision Engine-এর data-flow লেভেলের লেখা)।

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



## Gemini (বা অন্য LLM) API সেট করবেন কিভাবে

`.env` ফাইলে দুইটা জিনিস set করতে হবে:
```
LLM_PROVIDER=gemini
GEMINI_API_KEY=আপনার_key
```
Key নেওয়ার জায়গা: https://aistudio.google.com/apikey

⚠️ যেকোনো API key শুধু `.env` ফাইলে বসান — কোথাও share বা paste করবেন না। Key leak হয়ে গেলে সাথে সাথে regenerate করে নিন।

`.env` change করার পর server restart করুন:
```bash
python -m uvicorn backend.main:app --reload
```

এছাড়াও `.env`-এ আরও 16টা provider-এর জন্য key বসানোর জায়গা আছে (Groq,
Cohere, Mistral, xAI, ইত্যাদি — পুরো লিস্ট `.env.example`-এ)। একবার key
বসিয়ে backend restart করলে সেই provider dashboard-এর **AI Models** page,
Settings page, বা Chat (`switch to groq`) — যেকোনো জায়গা থেকে বেছে নেওয়া
যায়, `.env` আবার touch না করেই।

## Telegram দিয়ে Agent-এর সাথে Chat করা (Full Process)

**১) Bot বানান**
Telegram-এ **@BotFather** খুঁজে `/newbot` command দিন, নাম ও username দিন (username-এর শেষে `bot` থাকতে হবে)। এতে একটা **token** পাবেন।

**২) নিজের User ID বের করুন**
Telegram-এ **@userinfobot** খুঁজে `/start` দিন — এটা আপনার numeric ID দিবে।

**৩) `.env`-এ বসান**
```
TELEGRAM_BOT_TOKEN=BotFather-এর_token
TELEGRAM_ALLOWED_USER_IDS=আপনার_user_id
```
(একাধিক ID হলে comma দিয়ে: `123,456`)

**৪) Server restart করুন, তারপর নিজের bot-এ গিয়ে `/start` দিন।**

**৫) Task দেওয়ার Format**
```
/task <website> | <goal> | <wallet_label (optional)>
```
উদাহরণ:
```
/task https://example.com | ekta screenshot nao
```
`website` আর `goal`-এর মাঝে অবশ্যই `|` (pipe) থাকতে হবে, শুধু space দিলে হবে না।

**৬) অন্যান্য Command**
```
/status       → agent এখন কী করছে
/tasks        → সব task-এর list
/browser      → live browser-এর status
/screenshot   → এই মুহূর্তের screenshot
/logs         → recent logs
/pause /resume /stop /restart
/report       → শেষ হওয়া task-এর ফলাফল
```
Normal ভাষায় লিখলেও চলবে, যেমন: `"how's everything doing?"`, `"restart the agent"`।

## Agent সরাসরি Website-এ কী করছে তা Live দেখবেন কিভাবে

তিনটা উপায় আছে:

**১) Telegram Screenshot (snapshot, live না)**
```
/screenshot
```

**২) Frontend Dashboard — real-time Live View (recommended)**
```bash
cd frontend
npm install
npm run dev
```
তারপর browser-এ `http://localhost:5173` খুলে বাম দিকের **"Browser"** page-এ যান — প্রতি সেকেন্ডে refresh হওয়া live screenshot stream দেখা যাবে।

**৩) সরাসরি Chrome Window (সবচেয়ে direct)**
`.env`-এ `BROWSER_HEADLESS=false` থাকলে agent task চালানোর সময় আপনার PC-তে একটা আসল, চোখে দেখা যায় এমন Chrome window খুলে যায় — taskbar-এ সেটা দেখতে পাবেন, agent কী click/type করছে সরাসরি দেখা যাবে।


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
  planner/     LLM client (single-provider impl) + AI Model Manager (single entry point for every AI request: multi-provider switching/smart routing/cross-provider fallback/temporary overrides), agent loop, task queue, autonomous agent runtime
  memory/      SQLite + ChromaDB store
  vision/      OCR + vision-LLM perception fallback
  wallet/      Non-custodial approval automation
  skills/      Skill Library, matcher, runner, Teach Mode
  mcp/         MCP Core: registry/router/discovery/manager/client + connectors
    connectors/social_base.py  shared base for X/Discord/Gmail (browser-session driven, no API keys)
  identity/    Identity & Profile Manager: fs/detector/registry/manager
  telegram/    Bot commands + NL routing
  database/    SQLAlchemy models + session
  tests/       Pytest suite
docker/        Dockerfile(s)
docs/          (reserved for architecture docs)
frontend/      React dashboard (Vite + TypeScript + Tailwind v4 + shadcn-style UI)
  src/lib/api.ts  -- typed client for every backend route
  src/pages/       -- Home, Agent, Browser, Tasks, Memory, Reports, Logs, Settings, System, Skills, AI Models
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
- **System** — Health, Diagnostics, and Resource panels (`GET /api/system/
  health` / `/diagnostics` / `/resources`), build info (`GET /api/system/
  version`), and a one-click config backup (`POST /api/system/config/backup`).
- **Settings** — reads/patches `GET`/`PATCH /api/settings` (new route, see
  below): wallet approval policy, vision/OCR fallback, live-session tuning,
  and AI Model Manager basics (smart-routing toggle, fallback provider).
  Never exposes API keys, the auth token, or the Telegram token.
- **AI Models** — the full AI Model Manager control surface
  (`GET/POST /api/ai-models/*`): current provider/model/routing mode at a
  glance, default/fallback provider pickers, one routing-rule select per
  task type, and a provider table with API-key/health badges, enable/
  disable, "Test Provider Connection", and a one-click temporary override.
  Every switch/route/fallback/override change made here applies
  immediately to *every* AI call in the codebase (agent runs, decision
  engine, vision, Teach Mode, Telegram, chat) — there's no separate
  "admin-only" copy of the provider selection anymore.
- **Skills** — browse/search/filter learned skills (`GET /api/skills`),
  inspect a skill's workflow/triggers/version history with one-click
  rollback, duplicate/share/export/delete, a "Learn from text" dialog, and
  pending "save this as a skill?" prompts (`GET /api/skills/pending`).
- **MCP** — per-connector status and enable/disable toggle, tool counts,
  connector-specific config summaries, and a "test routing" panel
  (`GET /api/mcp/connectors`, `POST /api/mcp/route`).
- **Profiles** — create/list/search/filter Chrome browser profiles
  (`GET`/`POST /api/profiles`), clone/export/import (`POST /{id}/clone`,
  `GET /{id}/export`, `POST /import`), enable/disable/select-active
  (`POST /{id}/enable` / `/disable` / `/select`), per-service Gmail/X/
  Discord session status with a manual re-check (`GET /{id}/sessions`,
  `POST /{id}/sessions/check`), and a read-only filesystem inspector for
  the profile's on-disk Chrome directory (`GET /{id}/filesystem`).

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
