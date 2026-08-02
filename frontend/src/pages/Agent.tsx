import { useState } from "react"
import {
  Bot,
  Play,
  Square,
  Pause,
  RotateCcw,
  Globe,
  Wallet as WalletIcon,
  Brain,
  Activity,
  CheckCircle2,
  XCircle,
  RefreshCw,
} from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useAsync } from "@/hooks/use-async"
import { api, type AgentRuntimeStatusValue } from "@/lib/api"

const STATUS_VARIANT: Record<AgentRuntimeStatusValue, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  stopped: "neutral",
  starting: "violet",
  running: "green",
  paused: "amber",
  stopping: "violet",
}

function formatUptime(seconds: number): string {
  if (!seconds || seconds <= 0) return "—"
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export function Agent() {
  const agent = useAsync(() => api.agent.status(), [])
  const [actionPending, setActionPending] = useState<string | null>(null)

  async function runAction(name: string, fn: () => Promise<unknown>) {
    setActionPending(name)
    try {
      await fn()
      await agent.refetch()
    } finally {
      setActionPending(null)
    }
  }

  const data = agent.data
  const status = data?.status ?? "stopped"
  const isRunning = status === "running"
  const isPaused = status === "paused"
  const isStopped = status === "stopped"

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-amber)]">
            Autonomous runtime
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Agent</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Start, pause, or stop the continuously running agent, and watch what it's doing right now.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={() => agent.refetch()} disabled={agent.loading}>
          <RefreshCw className={`size-4 ${agent.loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      {agent.error && <p className="text-sm text-[var(--color-signal-red)]">{agent.error}</p>}
      {data?.error && <p className="text-sm text-[var(--color-signal-red)]">{data.error}</p>}

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Bot className="size-4 text-[var(--color-signal-amber)]" />
            Agent status
          </CardTitle>
          <Badge variant={STATUS_VARIANT[status]}>{status}</Badge>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={!isStopped || actionPending !== null}
              onClick={() => runAction("start", api.agent.start)}
            >
              <Play className="size-4" />
              {actionPending === "start" ? "Starting…" : "Start"}
            </Button>
            <Button
              variant="subtle"
              size="sm"
              disabled={!isRunning || actionPending !== null}
              onClick={() => runAction("pause", api.agent.pause)}
            >
              <Pause className="size-4" />
              {actionPending === "pause" ? "Pausing…" : "Pause"}
            </Button>
            <Button
              variant="subtle"
              size="sm"
              disabled={!isPaused || actionPending !== null}
              onClick={() => runAction("resume", api.agent.resume)}
            >
              <Play className="size-4" />
              {actionPending === "resume" ? "Resuming…" : "Resume"}
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={isStopped || actionPending !== null}
              onClick={() => runAction("stop", api.agent.stop)}
            >
              <Square className="size-4" />
              {actionPending === "stop" ? "Stopping…" : "Stop"}
            </Button>
          </div>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <InfoStat label="Uptime" value={formatUptime(data?.uptime_seconds ?? 0)} />
            <InfoStat label="Worker" value={data?.queue.worker_paused ? "paused" : "active"} />
            <InfoStat label="Recoveries" value={String(data?.recoveries_performed ?? 0)} />
            <InfoStat
              label="Last heartbeat"
              value={data?.last_heartbeat_at ? new Date(data.last_heartbeat_at).toLocaleTimeString() : "—"}
            />
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="size-4 text-[var(--color-signal-cyan)]" />
              Current task
            </CardTitle>
            <CardDescription>What the agent is working on right now</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {data?.current_task_id ? (
              <>
                <InfoRow icon={Globe} label="Task" value={data.current_task_id} mono />
                <InfoRow icon={Globe} label="Website" value={data.current_website ?? "—"} mono />
                <InfoRow icon={Bot} label="Action" value={data.current_action ?? "—"} />
                <InfoRow icon={Bot} label="Target" value={data.current_target ?? "—"} />
              </>
            ) : (
              <p className="text-sm text-[var(--color-text-faint)]">No task currently in flight.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Brain className="size-4 text-[var(--color-signal-violet)]" />
              AI reasoning summary
            </CardTitle>
            <CardDescription>The planner's own account of its last decision</CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-[var(--color-text)]">
              {data?.current_reasoning || "No reasoning recorded yet."}
            </p>
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Globe className="size-4 text-[var(--color-signal-cyan)]" />
              Browser state
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <span
                className={`size-2 rounded-full ${
                  data?.browser.active ? "bg-[var(--color-signal-green)] live-pulse" : "bg-[var(--color-text-faint)]"
                }`}
              />
              <span className="text-sm text-[var(--color-text)]">{data?.browser.active ? "Active" : "Idle"}</span>
            </div>
            <InfoRow icon={Globe} label="URL" value={data?.browser.url || "—"} mono />
            <InfoRow icon={Globe} label="Title" value={data?.browser.title || "—"} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <WalletIcon className="size-4 text-[var(--color-signal-amber)]" />
              Active wallet
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {data?.active_wallet ? (
              <>
                <InfoRow icon={WalletIcon} label="Label" value={data.active_wallet.label} />
                <InfoRow icon={WalletIcon} label="Address" value={data.active_wallet.address ?? "—"} mono />
                <InfoRow icon={WalletIcon} label="Network" value={data.active_wallet.network ?? "—"} />
              </>
            ) : (
              <p className="text-sm text-[var(--color-text-faint)]">No wallet selected.</p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Runtime statistics</CardTitle>
          <CardDescription>Cumulative counters since the agent last started</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <StatCard label="Completed" value={data?.tasks_completed ?? 0} icon={CheckCircle2} tone="green" />
            <StatCard label="Failed" value={data?.tasks_failed ?? 0} icon={XCircle} tone="red" />
            <StatCard label="Steps executed" value={data?.steps_executed ?? 0} icon={Activity} tone="cyan" />
            <StatCard label="Recoveries" value={data?.recoveries_performed ?? 0} icon={RotateCcw} tone="amber" />
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function InfoRow({
  icon: Icon,
  label,
  value,
  mono,
}: {
  icon: typeof Globe
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-start gap-2.5">
      <Icon className="mt-0.5 size-4 shrink-0 text-[var(--color-text-faint)]" />
      <div className="min-w-0">
        <p className="text-xs uppercase tracking-wide text-[var(--color-text-muted)]">{label}</p>
        <p className={`truncate text-sm text-[var(--color-text)] ${mono ? "font-mono" : ""}`}>{value}</p>
      </div>
    </div>
  )
}

function InfoStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-[var(--color-text-muted)]">{label}</p>
      <p className="mt-1 font-mono text-sm text-[var(--color-text)]">{value}</p>
    </div>
  )
}

function StatCard({
  label,
  value,
  icon: Icon,
  tone,
}: {
  label: string
  value: number
  icon: typeof Activity
  tone: "cyan" | "amber" | "green" | "red"
}) {
  const toneColor = `var(--color-signal-${tone})`
  return (
    <div className="flex items-center justify-between rounded-md border border-[var(--color-border)] px-4 py-3">
      <div>
        <p className="text-xs uppercase tracking-wide text-[var(--color-text-muted)]">{label}</p>
        <p className="mt-1 font-mono text-xl font-semibold" style={{ color: toneColor }}>
          {value}
        </p>
      </div>
      <Icon className="size-5" style={{ color: toneColor, opacity: 0.6 }} />
    </div>
  )
}
