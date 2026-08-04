// Typed client for the Nexus-Agent FastAPI backend.
// Base URL + bearer token come from Vite env vars (see .env.example) so the
// same build can point at localhost during dev or a deployed backend in prod.

export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
  "http://127.0.0.1:8000"

export const API_TOKEN: string = (import.meta.env.VITE_API_TOKEN as string | undefined) || ""

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = "ApiError"
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set("Content-Type", "application/json")
  if (API_TOKEN) headers.set("Authorization", `Bearer ${API_TOKEN}`)

  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new ApiError(res.status, text || res.statusText)
  }
  if (res.status === 204) return undefined as T
  const contentType = res.headers.get("content-type") || ""
  if (contentType.includes("application/json")) return (await res.json()) as T
  return (await res.blob()) as unknown as T
}

export function wsUrl(path: string): string {
  const httpBase = API_BASE_URL.replace(/^http/, "ws")
  const token = API_TOKEN ? `?token=${encodeURIComponent(API_TOKEN)}` : ""
  return `${httpBase}${path}${token}`
}

// ---------- Domain types ----------

export type TaskStatus =
  | "queued"
  | "planning"
  | "running"
  | "paused"
  | "succeeded"
  | "failed"
  | "cancelled"

export interface TaskSummary {
  id: string
  website: string
  goal: string
  wallet_label: string | null
  status: TaskStatus
  priority: number
  retry_count: number
  created_at: string
  scheduled_for: string | null
}

export interface TaskStep {
  index: number
  action: string
  target: string
  success: boolean | null
}

export interface TaskDetail {
  id: string
  website: string
  goal: string
  status: TaskStatus
  steps: TaskStep[]
  error?: string
}

export interface CreateTaskInput {
  website: string
  goal: string
  wallet_label?: string
  notes?: string
  priority?: number
  scheduled_for?: string
}

export interface MemoryResult {
  [key: string]: unknown
}

// ---------- Memory Improvements ----------

export type MemoryCategory =
  | "conversation"
  | "skills"
  | "browser"
  | "coding"
  | "profiles"
  | "tasks"
  | "general"

export interface MemoryEntryRecord {
  id: string
  kind: string
  category: MemoryCategory
  website: string | null
  content: string
  metadata: Record<string, unknown>
  confidence: number
  importance: number
  effective_importance: number
  access_count: number
  last_accessed_at: string | null
  archived: boolean
  archived_at: string | null
  expires_at: string | null
  merged_count: number
  created_at: string | null
}

export interface MemoryAnalytics {
  total: number
  active: number
  archived: number
  by_category: Record<string, number>
  by_kind: Record<string, number>
  average_importance: number
  expiring_soon: number
  duplicate_group_count: number
  duplicate_entry_count: number
  top_recalled: { id: string; content: string; access_count: number; category: string }[]
  most_important: { id: string; content: string; effective_importance: number; category: string }[]
  growth_last_14_days: Record<string, number>
}

export interface MemoryListParams {
  category?: string
  kind?: string
  q?: string
  sort?: "importance" | "recent" | "access"
  include_archived?: boolean
  limit?: number
}

export interface Report {
  id: string
  task_id: string
  status: string
  summary: string
  execution_seconds: number
  tx_hashes: string[]
  screenshots: string[]
  created_at: string
}

export interface WalletRecord {
  id: string
  label: string
  address: string | null
  provider: string
  network: string | null
}

export interface RegisterWalletInput {
  label: string
  address?: string
  provider?: string
  network?: string
}

// ---------- Multi Wallet Manager ----------
// Metadata only, always -- there is no field here for a seed phrase or
// private key. Import flows that accept one use it only in-memory on the
// backend to derive an address; nothing secret ever comes back over this API.

export type WalletStatus = "active" | "inactive" | "locked" | "unknown"

export interface WalletMeta {
  id: string
  label: string
  address: string | null
  wallet_type: string
  network: string | null
  status: WalletStatus
  tags: string[]
  notes: string | null
  group_id: string | null
  is_active: boolean
  last_used_at: string | null
  created_at: string
}

export type ImportMethod = "seed_phrase" | "private_key" | "browser_profile" | "address"

export interface ImportWalletInput {
  label: string
  method: ImportMethod
  address?: string
  private_key?: string
  seed_phrase?: string
  wallet_type?: string
  network?: string
  tags?: string[]
  notes?: string
  group_id?: string
}

export interface UpdateWalletInput {
  label?: string
  network?: string
  tags?: string[]
  notes?: string
  status?: WalletStatus
  group_id?: string
  wallet_type?: string
}

export interface WalletGroup {
  id: string
  name: string
  description: string | null
}

export interface WalletActivityEntry {
  id: string
  wallet_id: string
  event_type: string
  description: string
  metadata: Record<string, unknown>
  created_at: string
}

// ---------- Identity & Profile Manager ----------
// Metadata only, always -- mirrors WalletMeta's scope boundary. No password,
// seed phrase, or private key is ever stored here. Cookies/local storage/
// session storage/extensions live on disk in the profile's own Chrome
// profile directory (see backend/identity/fs.py) -- this only tracks
// *where* that directory is and the last-known auth status per service.

export type ProfileStatus = "ready" | "in_use" | "needs_login" | "disabled" | "error"

export interface SessionStatus {
  gmail: boolean | null
  x: boolean | null
  discord: boolean | null
}

export interface ProfileMeta {
  id: string
  name: string
  chrome_profile_dir: string
  wallet_label: string | null
  gmail_account: string | null
  x_account: string | null
  discord_account: string | null
  extensions: string[]
  notes: string | null
  tags: string[]
  status: ProfileStatus
  enabled: boolean
  is_active: boolean
  sessions: SessionStatus
  last_session_check_at: string | null
  last_used_at: string | null
  created_at: string
}

export type ProfileRecord = ProfileMeta

export interface CreateProfileInput {
  name: string
  wallet_label?: string
  gmail_account?: string
  x_account?: string
  discord_account?: string
  extensions?: string[]
  notes?: string
  tags?: string[]
}

export interface UpdateProfileInput {
  name?: string
  wallet_label?: string
  gmail_account?: string
  x_account?: string
  discord_account?: string
  extensions?: string[]
  notes?: string
  tags?: string[]
  status?: ProfileStatus
}

export interface ImportProfileInput {
  name: string
  wallet_label?: string
  gmail_account?: string
  x_account?: string
  discord_account?: string
  extensions?: string[]
  notes?: string
  tags?: string[]
}

export interface ProfileActivityEntry {
  id: string
  profile_id: string
  event_type: string
  description: string
  metadata: Record<string, unknown>
  created_at: string
}

export interface ProfileFilesystemInfo {
  exists: boolean
  cookies: { present: boolean; size_bytes?: number }
  local_storage: { present: boolean }
  session_storage: { present: boolean }
  extensions: string[]
}

export interface ProfileSessionCheckResult {
  [service: string]: { authenticated: boolean | null; detail?: string }
}

export interface WalletLiveStatus extends WalletMeta {
  live: {
    connected: boolean | null
    locked_or_disconnected?: boolean | null
    network: string | null
    selected_address?: string | null
    reason?: string
  }
}

export interface WalletBalance {
  address: string
  network: string
  wei: number
  native: number
}

// ---------- Hot Signer (direct RPC native transfer, burner wallets) ----------
// Deliberately separate from the WalletBalance/WalletMeta scope above: this
// never touches the browser-extension flow, and the backend only ever
// surfaces the derived address -- the private key stays server-side.
export interface HotSignerStatus {
  enabled: boolean
  address: string | null
  max_native_value: number | null
}

export interface HotSignerSendInput {
  chain: string
  to_address: string
  amount: number
  wallet_id?: string
}

export interface HotSignerSendResult {
  tx_hash: string
  chain: string
  from_address: string
  to_address: string
  amount_native: number
}

export interface PendingWalletRequest {
  pending: boolean
  type?: "connection" | "transaction" | "signature" | "unknown"
  popup_id?: string
  snippet?: string
  reason?: string
}

export interface BrowserStatus {
  active: boolean
  error?: string
  task_id?: string
  url?: string
  title?: string
  viewers?: number
}

export interface LogsResponse {
  lines: string[]
  file: string
  total_lines?: number
}

export interface PluginInfo {
  name: string
  version: string
  description: string
  enabled: boolean
  error: string | null
}

// ---------- MCP Core ----------

export interface ConnectorInfo {
  name: string
  version: string
  description: string
  tags: string[]
  enabled: boolean
  status: string
  error: string | null
  config: Record<string, unknown>
  tool_count: number
}

export interface ToolInfo {
  connector: string
  name: string
  description: string
  input_schema: Record<string, unknown>
}

export interface ToolCallResult {
  ok: boolean
  connector: string
  tool: string
  output: unknown
  error: string | null
  latency_ms: number
  meta: Record<string, unknown>
}

export interface RoutedTool {
  connector: string
  tool_name: string
  score: number
}

export interface McpHealthComponent {
  status: string
  detail: string
}

export interface SocialConnectorStatus {
  connector: string
  service: string
  connection_status: string
  session_status: string
  account: string | null
  last_used_at: number | null
}

export interface SettingsView {
  app_name: string
  environment: string
  debug: boolean
  llm_provider: string
  llm_model_override: string
  ai_smart_routing_enabled: boolean
  ai_fallback_provider: string
  browser_channel: string
  browser_headless: boolean
  browser_slow_mo_ms: number
  browser_default_timeout_ms: number
  wallet_require_manual_approval: boolean
  wallet_max_auto_approve_value_usd: number
  wallet_allowlisted_contracts: string
  vision_enabled: boolean
  vision_min_elements_threshold: number
  ocr_enabled: boolean
  ocr_lang: string
  live_session_enabled: boolean
  live_session_interval_ms: number
  live_session_jpeg_quality: number
}

export type SettingsUpdateInput = Partial<
  Omit<SettingsView, "app_name" | "environment" | "debug" | "browser_channel">
>

export interface ProviderHealth {
  status: "unknown" | "healthy" | "degraded" | "down"
  connection_status: "untested" | "connected" | "failed"
  latency_ms: number | null
  last_success_at: string | null
  last_error: string | null
  last_error_at: string | null
  total_requests: number
  total_failures: number
  rate_limited_until: string | null
  availability: number
}

export interface AiProviderInfo {
  provider: string
  default_model: string
  has_api_key: boolean
  enabled: boolean
  health: ProviderHealth
}

export interface AiModelManagerView {
  current_provider: string
  current_model: string
  routing_mode: "manual" | "auto"
  fallback_provider: string
  provider_priority: string[]
  disabled_providers: string[]
  routing_rules: Record<string, string>
  temporary_override: { provider: string; model: string | null; reason: string } | null
  providers: AiProviderInfo[]
}

export type AiTaskType =
  | "coding"
  | "browser_automation"
  | "planning"
  | "vision"
  | "long_context"
  | "fast_response"
  | "general_chat"
  | "research"
  | "reasoning"
  | "low_cost"

export interface HealthResponse {
  status: string
  app: string
}

// ---------- Agent Runtime ----------

export type AgentRuntimeStatusValue = "stopped" | "starting" | "running" | "paused" | "stopping"

export interface AgentQueueStatus {
  worker_paused: boolean
  active_task_id: string | null
  paused_task_ids: string[]
}

export interface AgentBrowserState {
  active: boolean
  url: string
  title: string
}

export interface AgentActiveWallet {
  id: string
  label: string
  address: string | null
  wallet_type: string
  network: string | null
  status: string
}

export interface AgentStatus {
  status: AgentRuntimeStatusValue
  started_at: string | null
  stopped_at: string | null
  current_task_id: string | null
  current_website: string | null
  current_action: string | null
  current_target: string | null
  current_reasoning: string | null
  tasks_completed: number
  tasks_failed: number
  steps_executed: number
  recoveries_performed: number
  last_heartbeat_at: string | null
  uptime_seconds: number
  queue: AgentQueueStatus
  browser: AgentBrowserState
  active_wallet: AgentActiveWallet | null
  error?: string
}

// ---------- System (health / diagnostics / resources / config / version) ----------

export type ComponentHealthStatus = "ok" | "degraded" | "down" | "unknown"

export interface ComponentHealth {
  name: string
  status: ComponentHealthStatus
  detail: string
  latency_ms: number | null
}

export interface HealthReport {
  overall: ComponentHealthStatus
  checked_at: number
  components: ComponentHealth[]
}

export interface DiagnosticCheck {
  name: string
  passed: boolean
  detail: string
}

export interface DiagnosticReport {
  generated_at: number
  python_version: string
  platform: string
  passed: boolean
  checks: DiagnosticCheck[]
}

export interface ResourceSnapshot {
  taken_at: number
  cpu_percent: number | null
  process_rss_mb: number | null
  system_memory_percent: number | null
  system_memory_available_mb: number | null
  browser_memory_mb: number | null
  queue_size: number
  active_tasks: number
  psutil_available: boolean
}

export interface BuildInfo {
  version: string
  commit: string
  commit_short: string
  branch: string
  commit_date: string
  dirty: boolean
  repo: string
}

export interface ConfigBackup {
  filename: string
  created_at: number
}

// ---------- AI Chat ----------

export type ChatCategory =
  | "conversation"
  | "question"
  | "browser_command"
  | "agent_command"
  | "task"
  | "settings"
  | "system_request"

export type ChatRole = "user" | "assistant" | "system"

export interface ChatSession {
  id: string
  channel: string
  title: string | null
  last_task_id: string | null
  last_error: string | null
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  category: ChatCategory | null
  meta: Record<string, unknown>
  created_at: string
}

export interface SendChatMessageResult {
  session_id: string
  reply: string
  category: ChatCategory
  action: string
  meta: Record<string, unknown>
}

// ---------- Skill Library / Teach Mode ----------

export type SkillSource =
  | "natural_language"
  | "teach_mode"
  | "browser_demonstration"
  | "recorded_workflow"
  | "task_outcome"
  | "correction"
  | "imported"
  | "manual"

export interface SkillVariable {
  name: string
  description: string
  default: string
}

export interface SkillStep {
  action: string
  target: string
  value: string
  description: string
}

export interface Skill {
  id: string
  name: string
  description: string
  category: string
  trigger: string
  variables: SkillVariable[]
  workflow: SkillStep[]
  success_condition: string | null
  required_plugins: string[]
  required_browser: string | null
  website_hint: string | null
  success_rate: number
  usage_count: number
  last_used_at: string | null
  version: number
  enabled: boolean
  source: SkillSource
  created_at: string
  updated_at: string
}

export interface PendingSkill {
  task_id: string
  name: string
  description: string
  category: string
  trigger: string
  website_hint?: string | null
  workflow: SkillStep[]
  source_skill_id?: string
}

export interface SkillVersionEntry {
  version: number
  snapshot: Record<string, unknown>
  change_note: string | null
  created_at: string
}

export interface CreateSkillInput {
  name: string
  description?: string
  category?: string
  trigger?: string
  variables?: SkillVariable[]
  workflow?: SkillStep[]
  success_condition?: string | null
  required_plugins?: string[]
  required_browser?: string | null
  website_hint?: string | null
  enabled?: boolean
}

export type UpdateSkillInput = Partial<CreateSkillInput> & { change_note?: string }

export interface TeachDraft {
  session_id: string
  name: string
  description: string
  category: string
  trigger: string
  website_hint: string | null
  variables: SkillVariable[]
  steps: SkillStep[]
}

// ---------- Endpoints ----------

export const api = {
  health: () => request<HealthResponse>("/api/health"),

  system: {
    health: () => request<HealthReport>("/api/system/health"),
    diagnostics: () => request<DiagnosticReport>("/api/system/diagnostics"),
    resources: () => request<ResourceSnapshot>("/api/system/resources"),
    version: () => request<BuildInfo>("/api/system/version"),
    exportConfig: () => request<{ exported_at: string; app_name: string; settings: Record<string, unknown> }>(
      "/api/system/config/export"
    ),
    backupConfig: () => request<{ filename: string }>("/api/system/config/backup", { method: "POST" }),
    listBackups: () => request<{ backups: ConfigBackup[] }>("/api/system/config/backups"),
    restoreConfig: (filename: string) =>
      request<{ applied: Record<string, unknown> }>("/api/system/config/restore", {
        method: "POST",
        body: JSON.stringify({ filename }),
      }),
  },

  agent: {
    status: () => request<AgentStatus>("/api/agent/status"),
    start: () => request<AgentStatus>("/api/agent/start", { method: "POST" }),
    stop: () => request<AgentStatus>("/api/agent/stop", { method: "POST" }),
    pause: () => request<AgentStatus>("/api/agent/pause", { method: "POST" }),
    resume: () => request<AgentStatus>("/api/agent/resume", { method: "POST" }),
  },

  chat: {
    listSessions: () => request<ChatSession[]>("/api/chat/sessions"),
    createSession: (channel = "dashboard") =>
      request<ChatSession>("/api/chat/sessions", { method: "POST", body: JSON.stringify({ channel }) }),
    getMessages: (sessionId: string) => request<ChatMessage[]>(`/api/chat/sessions/${sessionId}/messages`),
    sendMessage: (sessionId: string, text: string) =>
      request<SendChatMessageResult>(`/api/chat/sessions/${sessionId}/messages`, {
        method: "POST",
        body: JSON.stringify({ text }),
      }),
    clearMessages: (sessionId: string) =>
      request<{ ok: boolean }>(`/api/chat/sessions/${sessionId}/messages`, { method: "DELETE" }),
    exportMessages: (sessionId: string) =>
      request<{ session_id: string; messages: ChatMessage[] }>(`/api/chat/sessions/${sessionId}/export`),
  },

  tasks: {
    list: () => request<TaskSummary[]>("/api/tasks"),
    get: (id: string) => request<TaskDetail>(`/api/tasks/${id}`),
    create: (input: CreateTaskInput) =>
      request<{ id: string }>("/api/tasks", { method: "POST", body: JSON.stringify(input) }),
    cancel: (id: string) => request<{ id: string; cancel_requested?: boolean; error?: string }>(
      `/api/tasks/${id}/cancel`,
      { method: "POST" }
    ),
    pause: (id: string) =>
      request<{ id: string; paused?: boolean; error?: string }>(`/api/tasks/${id}/pause`, { method: "POST" }),
    resume: (id: string) =>
      request<{ id: string; paused?: boolean; error?: string }>(`/api/tasks/${id}/resume`, { method: "POST" }),
    retry: (id: string) =>
      request<{ id: string; requeued?: boolean; error?: string }>(`/api/tasks/${id}/retry`, { method: "POST" }),
    remove: (id: string) =>
      request<{ id: string; deleted?: boolean; error?: string }>(`/api/tasks/${id}`, { method: "DELETE" }),
    queueStatus: () =>
      request<{
        worker_paused: boolean
        active_task_id: string | null
        paused_task_ids: string[]
        running_tasks: { task_id: string; profile_id: string | null; website: string }[]
        concurrency: { active: number; max: number }
      }>("/api/tasks/queue/status"),
    pauseQueue: () => request<{ worker_paused: boolean }>("/api/tasks/queue/pause", { method: "POST" }),
    resumeQueue: () => request<{ worker_paused: boolean }>("/api/tasks/queue/resume", { method: "POST" }),
  },

  memory: {
    search: (q: string, topK = 5) =>
      request<{ results: MemoryResult[] }>(
        `/api/memory/search?q=${encodeURIComponent(q)}&top_k=${topK}`
      ),

    // Memory Improvements
    list: (params?: MemoryListParams) => {
      const qs = new URLSearchParams(
        Object.entries(params || {})
          .filter(([, v]) => v !== undefined && v !== "")
          .map(([k, v]) => [k, String(v)])
      ).toString()
      return request<{ memories: MemoryEntryRecord[] }>(`/api/memory${qs ? `?${qs}` : ""}`)
    },
    get: (id: string) => request<MemoryEntryRecord>(`/api/memory/${id}`),
    analytics: () => request<MemoryAnalytics>("/api/memory/analytics"),
    archive: (id: string) => request<{ id: string; archived: boolean }>(`/api/memory/${id}/archive`, { method: "POST" }),
    unarchive: (id: string) =>
      request<{ id: string; archived: boolean }>(`/api/memory/${id}/unarchive`, { method: "POST" }),
    forget: (id: string) => request<{ id: string; forgotten: boolean }>(`/api/memory/${id}`, { method: "DELETE" }),
    bulkArchive: (ids: string[]) =>
      request<{ archived: number }>("/api/memory/bulk/archive", { method: "POST", body: JSON.stringify({ ids }) }),
    bulkForget: (ids: string[]) =>
      request<{ forgotten: number }>("/api/memory/bulk/forget", { method: "POST", body: JSON.stringify({ ids }) }),
    duplicates: () => request<{ groups: MemoryEntryRecord[][] }>("/api/memory/duplicates"),
    mergeDuplicates: (ids: string[], keepId?: string) =>
      request<{ kept_id: string; removed_ids: string[]; entry: MemoryEntryRecord }>(
        "/api/memory/duplicates/merge",
        { method: "POST", body: JSON.stringify({ ids, keep_id: keepId }) }
      ),
    runExpiration: () => request<{ archived: number; forgotten: number }>("/api/memory/expire/run", { method: "POST" }),
  },

  reports: {
    list: () => request<Report[]>("/api/reports"),
  },

  wallets: {
    list: () => request<WalletRecord[]>("/api/wallets"),
    register: (input: RegisterWalletInput) =>
      request<{ id: string }>("/api/wallets", { method: "POST", body: JSON.stringify(input) }),

    // Multi Wallet Manager
    listMeta: (params?: { search?: string; group_id?: string; status?: string; tag?: string }) => {
      const qs = new URLSearchParams(
        Object.entries(params || {}).filter(([, v]) => v) as [string, string][]
      ).toString()
      return request<WalletMeta[]>(`/api/wallets${qs ? `?${qs}` : ""}`)
    },
    get: (id: string) => request<WalletMeta>(`/api/wallets/${id}`),
    import: (input: ImportWalletInput) =>
      request<WalletMeta>("/api/wallets/import", { method: "POST", body: JSON.stringify(input) }),
    update: (id: string, input: UpdateWalletInput) =>
      request<WalletMeta>(`/api/wallets/${id}`, { method: "PATCH", body: JSON.stringify(input) }),
    remove: (id: string) => request<{ ok: boolean }>(`/api/wallets/${id}`, { method: "DELETE" }),
    selectActive: (id: string) => request<WalletMeta>(`/api/wallets/${id}/select`, { method: "POST" }),
    getActive: () => request<WalletMeta | null>("/api/wallets/active"),
    status: (id: string) => request<WalletLiveStatus>(`/api/wallets/${id}/status`),
    balance: (id: string, network?: string) =>
      request<WalletBalance>(`/api/wallets/${id}/balance${network ? `?network=${network}` : ""}`),
    activity: (id?: string, limit = 50) =>
      request<WalletActivityEntry[]>(id ? `/api/wallets/${id}/activity?limit=${limit}` : `/api/wallets/activity?limit=${limit}`),
    exportMeta: (ids?: string[]) =>
      request<WalletMeta[]>(`/api/wallets/export${ids?.length ? `?ids=${ids.join(",")}` : ""}`),
    currentNetwork: () => request<{ network: string | null; reason?: string }>("/api/wallets/network/current"),
    switchNetwork: (id: string, network: string) =>
      request<{ ok: boolean; error?: string }>(`/api/wallets/${id}/network/switch`, {
        method: "POST",
        body: JSON.stringify({ network }),
      }),
    pendingRequest: () => request<PendingWalletRequest>("/api/wallets/requests/pending"),

    hotSigner: {
      status: () => request<HotSignerStatus>("/api/wallets/hot-signer/status"),
      send: (input: HotSignerSendInput) =>
        request<HotSignerSendResult>("/api/wallets/hot-signer/send", {
          method: "POST",
          body: JSON.stringify(input),
        }),
    },

    groups: {
      list: () => request<WalletGroup[]>("/api/wallets/groups"),
      create: (name: string, description?: string) =>
        request<WalletGroup>("/api/wallets/groups", { method: "POST", body: JSON.stringify({ name, description }) }),
      remove: (id: string) => request<{ ok: boolean }>(`/api/wallets/groups/${id}`, { method: "DELETE" }),
    },
  },

  profiles: {
    list: (params?: { search?: string; tag?: string; enabled_only?: boolean }) => {
      const qs = new URLSearchParams(
        Object.entries(params || {})
          .filter(([, v]) => v !== undefined && v !== "" && v !== false)
          .map(([k, v]) => [k, String(v)])
      ).toString()
      return request<ProfileMeta[]>(`/api/profiles${qs ? `?${qs}` : ""}`)
    },
    get: (id: string) => request<ProfileMeta>(`/api/profiles/${id}`),
    create: (input: CreateProfileInput) =>
      request<ProfileMeta>("/api/profiles", { method: "POST", body: JSON.stringify(input) }),
    update: (id: string, input: UpdateProfileInput) =>
      request<ProfileMeta>(`/api/profiles/${id}`, { method: "PATCH", body: JSON.stringify(input) }),
    remove: (id: string) => request<{ ok: boolean }>(`/api/profiles/${id}`, { method: "DELETE" }),
    clone: (id: string, newName: string) =>
      request<ProfileMeta>(`/api/profiles/${id}/clone`, { method: "POST", body: JSON.stringify({ new_name: newName }) }),
    openInChrome: (id: string) =>
      request<{ id: string; chrome_profile_dir: string; pid: number }>(`/api/profiles/${id}/open`, { method: "POST" }),
    rename: (id: string, newName: string) =>
      request<ProfileMeta>(`/api/profiles/${id}/rename`, { method: "POST", body: JSON.stringify({ new_name: newName }) }),
    import: (input: ImportProfileInput) =>
      request<ProfileMeta>("/api/profiles/import", { method: "POST", body: JSON.stringify(input) }),
    export: (id: string) => request<Record<string, unknown>>(`/api/profiles/${id}/export`),
    enable: (id: string) => request<ProfileMeta>(`/api/profiles/${id}/enable`, { method: "POST" }),
    disable: (id: string) => request<ProfileMeta>(`/api/profiles/${id}/disable`, { method: "POST" }),
    select: (id: string) => request<ProfileMeta>(`/api/profiles/${id}/select`, { method: "POST" }),
    getActive: () => request<ProfileMeta | null>("/api/profiles/active"),
    sessions: (id: string) => request<SessionStatus & { last_session_check_at: string | null }>(`/api/profiles/${id}/sessions`),
    checkSessionsNow: (id: string) => request<ProfileSessionCheckResult>(`/api/profiles/${id}/sessions/check`, { method: "POST" }),
    filesystem: (id: string) => request<ProfileFilesystemInfo>(`/api/profiles/${id}/filesystem`),
    activity: (id?: string, limit = 50) =>
      request<ProfileActivityEntry[]>(id ? `/api/profiles/${id}/activity?limit=${limit}` : `/api/profiles/activity?limit=${limit}`),
    supportedServices: () => request<{ services: string[] }>("/api/profiles/meta/supported-services"),
  },

  browser: {
    status: () => request<BrowserStatus>("/api/browser/status"),
    // Screenshot is a raw JPEG (or empty 204 if nothing captured yet), not
    // JSON, so it's fetched as a blob and turned into an object URL by the
    // caller rather than being requested as a plain <img src>.
    screenshotBlob: async (): Promise<Blob | null> => {
      const headers = new Headers()
      if (API_TOKEN) headers.set("Authorization", `Bearer ${API_TOKEN}`)
      const res = await fetch(`${API_BASE_URL}/api/browser/screenshot`, { headers })
      if (res.status === 204) return null
      if (!res.ok) throw new ApiError(res.status, res.statusText)
      return res.blob()
    },
  },

  logs: {
    tail: (lines = 200) => request<LogsResponse>(`/api/logs?lines=${lines}`),
    clear: () => request<{ cleared: boolean; file: string }>(`/api/logs`, { method: "DELETE" }),
  },

  settings: {
    get: () => request<SettingsView>("/api/settings"),
    update: (input: SettingsUpdateInput) =>
      request<SettingsView>("/api/settings", { method: "PATCH", body: JSON.stringify(input) }),
  },

  aiModels: {
    get: () => request<AiModelManagerView>("/api/ai-models"),
    health: () => request<Record<string, ProviderHealth>>("/api/ai-models/health"),
    switch: (provider: string, model?: string) =>
      request<AiModelManagerView>("/api/ai-models/switch", {
        method: "POST",
        body: JSON.stringify({ provider, model: model || undefined }),
      }),
    setRoutingMode: (mode: "manual" | "auto") =>
      request<AiModelManagerView>("/api/ai-models/routing-mode", { method: "POST", body: JSON.stringify({ mode }) }),
    setRoutingRule: (task_type: AiTaskType, provider: string) =>
      request<{ task_type: string; provider: string }>("/api/ai-models/routing-rules/one", {
        method: "POST",
        body: JSON.stringify({ task_type, provider }),
      }),
    setFallback: (provider: string) =>
      request<AiModelManagerView>("/api/ai-models/fallback", { method: "POST", body: JSON.stringify({ provider }) }),
    setPriority: (providers: string[]) =>
      request<AiModelManagerView>("/api/ai-models/priority", { method: "POST", body: JSON.stringify({ providers }) }),
    enable: (provider: string) =>
      request<AiModelManagerView>("/api/ai-models/enable", { method: "POST", body: JSON.stringify({ provider }) }),
    disable: (provider: string) =>
      request<AiModelManagerView>("/api/ai-models/disable", { method: "POST", body: JSON.stringify({ provider }) }),
    setOverride: (provider: string, model?: string, reason?: string) =>
      request<AiModelManagerView>("/api/ai-models/override", {
        method: "POST",
        body: JSON.stringify({ provider, model: model || undefined, reason: reason || "" }),
      }),
    clearOverride: () => request<AiModelManagerView>("/api/ai-models/override", { method: "DELETE" }),
    testConnection: (provider: string) =>
      request<{ provider: string; ok: boolean; latency_ms?: number; error?: string }>(
        `/api/ai-models/test/${provider}`,
        { method: "POST" }
      ),
  },

  plugins: {
    list: () => request<{ plugins: PluginInfo[] }>("/api/plugins"),
    rescan: () => request<{ discovered: string[]; plugins: PluginInfo[] }>("/api/plugins/rescan", { method: "POST" }),
    enable: (name: string) => request<{ name: string; enabled: boolean }>(`/api/plugins/${name}/enable`, { method: "POST" }),
    disable: (name: string) => request<{ name: string; enabled: boolean }>(`/api/plugins/${name}/disable`, { method: "POST" }),
    reload: (name: string) => request<{ plugins: PluginInfo[] }>(`/api/plugins/${name}/reload`, { method: "POST" }),
  },

  mcp: {
    connectors: () => request<{ connectors: ConnectorInfo[] }>("/api/mcp/connectors"),
    tools: (connector?: string) =>
      request<{ tools: ToolInfo[] }>(`/api/mcp/tools${connector ? `?connector=${connector}` : ""}`),
    health: () => request<Record<string, McpHealthComponent>>("/api/mcp/health"),
    socialStatus: () => request<{ connectors: Record<string, SocialConnectorStatus> }>("/api/mcp/social-status"),
    enable: (name: string) => request<{ name: string; enabled: boolean }>(`/api/mcp/connectors/${name}/enable`, { method: "POST" }),
    disable: (name: string) => request<{ name: string; enabled: boolean }>(`/api/mcp/connectors/${name}/disable`, { method: "POST" }),
    configure: (name: string, config: Record<string, unknown>) =>
      request<{ name: string; configured: boolean }>(`/api/mcp/connectors/${name}/configure`, {
        method: "POST",
        body: JSON.stringify({ config }),
      }),
    call: (connector: string, tool: string, args: Record<string, unknown> = {}, timeout?: number) =>
      request<ToolCallResult>("/api/mcp/call", {
        method: "POST",
        body: JSON.stringify({ connector, tool, arguments: args, timeout }),
      }),
    route: (text: string, connector_hint?: string) =>
      request<{ matched: boolean; route: RoutedTool | null }>("/api/mcp/route", {
        method: "POST",
        body: JSON.stringify({ text, connector_hint }),
      }),
  },

  skills: {
    list: (params?: { category?: string; enabled_only?: boolean; search?: string }) => {
      const qs = new URLSearchParams(
        Object.entries(params || {})
          .filter(([, v]) => v !== undefined && v !== "")
          .map(([k, v]) => [k, String(v)])
      ).toString()
      return request<Skill[]>(`/api/skills${qs ? `?${qs}` : ""}`)
    },
    get: (id: string) => request<Skill>(`/api/skills/${id}`),
    create: (input: CreateSkillInput) =>
      request<Skill>("/api/skills", { method: "POST", body: JSON.stringify(input) }),
    update: (id: string, input: UpdateSkillInput) =>
      request<Skill>(`/api/skills/${id}`, { method: "PATCH", body: JSON.stringify(input) }),
    remove: (id: string) => request<{ ok: boolean }>(`/api/skills/${id}`, { method: "DELETE" }),
    rename: (id: string, name: string) =>
      request<Skill>(`/api/skills/${id}/rename`, { method: "POST", body: JSON.stringify({ name }) }),
    duplicate: (id: string, name?: string) =>
      request<Skill>(`/api/skills/${id}/duplicate`, { method: "POST", body: JSON.stringify({ name }) }),
    enable: (id: string) => request<Skill>(`/api/skills/${id}/enable`, { method: "POST" }),
    disable: (id: string) => request<Skill>(`/api/skills/${id}/disable`, { method: "POST" }),

    versions: (id: string) => request<SkillVersionEntry[]>(`/api/skills/${id}/versions`),
    rollback: (id: string, version: number) =>
      request<Skill>(`/api/skills/${id}/rollback`, { method: "POST", body: JSON.stringify({ version }) }),

    exportSkill: (id: string) => request<Record<string, unknown>>(`/api/skills/${id}/export`),
    shareCode: (id: string) => request<{ share_code: string }>(`/api/skills/${id}/share`),
    import: (input: { payload?: Record<string, unknown>; share_code?: string }) =>
      request<Skill>("/api/skills/import", { method: "POST", body: JSON.stringify(input) }),
    importRecordedWorkflow: (input: {
      name: string
      steps: SkillStep[]
      description?: string
      category?: string
      trigger?: string
    }) => request<Skill>("/api/skills/import/recorded-workflow", { method: "POST", body: JSON.stringify(input) }),
    importFromTask: (taskId: string, input?: { name?: string; description?: string; category?: string; trigger?: string; steps?: SkillStep[] }) =>
      request<Skill>(`/api/skills/import/from-task/${taskId}`, {
        method: "POST",
        body: JSON.stringify(input ?? { name: "", steps: [] }),
      }),

    learnFromText: (text: string) =>
      request<{ created: boolean; skill?: Skill; draft?: Record<string, unknown>; reason?: string }>(
        "/api/skills/learn",
        { method: "POST", body: JSON.stringify({ text }) }
      ),
    correct: (input: { skill_id: string; step_index: number; instruction: string }) =>
      request<Skill>("/api/skills/correct", { method: "POST", body: JSON.stringify(input) }),

    pending: {
      list: () => request<PendingSkill[]>("/api/skills/pending"),
      confirm: (taskId: string) => request<Skill>(`/api/skills/pending/${taskId}/confirm`, { method: "POST" }),
      discard: (taskId: string) => request<{ ok: boolean }>(`/api/skills/pending/${taskId}/discard`, { method: "POST" }),
    },

    teach: {
      start: (sessionId: string, input: { name?: string; trigger?: string; website_hint?: string }) =>
        request<{ session_id: string; draft: TeachDraft }>(`/api/skills/teach/${sessionId}/start`, {
          method: "POST",
          body: JSON.stringify(input),
        }),
      step: (sessionId: string, text: string) =>
        request<{ step: SkillStep; draft: TeachDraft }>(`/api/skills/teach/${sessionId}/step`, {
          method: "POST",
          body: JSON.stringify({ text }),
        }),
      undo: (sessionId: string) => request<{ ok: boolean }>(`/api/skills/teach/${sessionId}/undo`, { method: "POST" }),
      cancel: (sessionId: string) => request<{ ok: boolean }>(`/api/skills/teach/${sessionId}/cancel`, { method: "POST" }),
      finish: (sessionId: string, input: { name?: string; description?: string; category?: string; trigger?: string }) =>
        request<Skill>(`/api/skills/teach/${sessionId}/finish`, { method: "POST", body: JSON.stringify(input) }),
      status: (sessionId: string) =>
        request<{ active: boolean; draft: TeachDraft | null }>(`/api/skills/teach/${sessionId}`),
    },
  },
}
