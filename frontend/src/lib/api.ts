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

export interface SettingsView {
  app_name: string
  environment: string
  debug: boolean
  llm_provider: string
  llm_model_override: string
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
  Omit<SettingsView, "app_name" | "environment" | "debug" | "llm_provider" | "llm_model_override" | "browser_channel">
>

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
    queueStatus: () =>
      request<{ worker_paused: boolean; active_task_id: string | null; paused_task_ids: string[] }>(
        "/api/tasks/queue/status"
      ),
    pauseQueue: () => request<{ worker_paused: boolean }>("/api/tasks/queue/pause", { method: "POST" }),
    resumeQueue: () => request<{ worker_paused: boolean }>("/api/tasks/queue/resume", { method: "POST" }),
  },

  memory: {
    search: (q: string, topK = 5) =>
      request<{ results: MemoryResult[] }>(
        `/api/memory/search?q=${encodeURIComponent(q)}&top_k=${topK}`
      ),
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

    groups: {
      list: () => request<WalletGroup[]>("/api/wallets/groups"),
      create: (name: string, description?: string) =>
        request<WalletGroup>("/api/wallets/groups", { method: "POST", body: JSON.stringify({ name, description }) }),
      remove: (id: string) => request<{ ok: boolean }>(`/api/wallets/groups/${id}`, { method: "DELETE" }),
    },
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
  },

  settings: {
    get: () => request<SettingsView>("/api/settings"),
    update: (input: SettingsUpdateInput) =>
      request<SettingsView>("/api/settings", { method: "PATCH", body: JSON.stringify(input) }),
  },

  plugins: {
    list: () => request<{ plugins: PluginInfo[] }>("/api/plugins"),
    rescan: () => request<{ discovered: string[]; plugins: PluginInfo[] }>("/api/plugins/rescan", { method: "POST" }),
    enable: (name: string) => request<{ name: string; enabled: boolean }>(`/api/plugins/${name}/enable`, { method: "POST" }),
    disable: (name: string) => request<{ name: string; enabled: boolean }>(`/api/plugins/${name}/disable`, { method: "POST" }),
    reload: (name: string) => request<{ plugins: PluginInfo[] }>(`/api/plugins/${name}/reload`, { method: "POST" }),
  },
}
