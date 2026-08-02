import { useState } from "react"
import { Activity, Cpu, GitCommit, HeartPulse, RefreshCw, ShieldCheck } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useAsync } from "@/hooks/use-async"
import { api, type ComponentHealthStatus } from "@/lib/api"

const STATUS_VARIANT: Record<ComponentHealthStatus, "neutral" | "green" | "amber" | "red"> = {
  ok: "green",
  degraded: "amber",
  down: "red",
  unknown: "neutral",
}

export function System() {
  const health = useAsync(() => api.system.health(), [])
  const diagnostics = useAsync(() => api.system.diagnostics(), [])
  const resources = useAsync(() => api.system.resources(), [])
  const version = useAsync(() => api.system.version(), [])
  const [backupMsg, setBackupMsg] = useState<string | null>(null)

  const refreshAll = () => {
    health.refetch()
    diagnostics.refetch()
    resources.refetch()
  }

  const runBackup = async () => {
    setBackupMsg(null)
    try {
      const res = await api.system.backupConfig()
      setBackupMsg(`Saved backup: ${res.filename}`)
    } catch (err) {
      setBackupMsg(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-green)]">
            Operations
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">System</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Health, diagnostics, and resource usage across every subsystem — plus build info and
            configuration backups.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refreshAll}>
          <RefreshCw className="size-3.5" /> Refresh
        </Button>
      </div>

      {/* Health */}
      <Card>
        <CardContent className="flex flex-col gap-4 pt-5">
          <div className="flex items-center gap-2">
            <HeartPulse className="size-4 text-[var(--color-signal-green)]" />
            <h2 className="text-sm font-semibold text-[var(--color-text)]">Health</h2>
            {health.data && (
              <Badge variant={STATUS_VARIANT[health.data.overall]}>{health.data.overall}</Badge>
            )}
          </div>
          {health.loading && <p className="text-sm text-[var(--color-text-muted)]">Checking components…</p>}
          {health.error && <p className="text-sm text-[var(--color-signal-red)]">{health.error}</p>}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {health.data?.components.map((c) => (
              <div
                key={c.name}
                className="flex flex-col gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2"
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs text-[var(--color-text)]">{c.name}</span>
                  <Badge variant={STATUS_VARIANT[c.status]}>{c.status}</Badge>
                </div>
                <p className="truncate text-[11px] text-[var(--color-text-faint)]" title={c.detail}>
                  {c.detail}
                </p>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Diagnostics */}
      <Card>
        <CardContent className="flex flex-col gap-4 pt-5">
          <div className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-[var(--color-signal-cyan)]" />
            <h2 className="text-sm font-semibold text-[var(--color-text)]">Diagnostics</h2>
            {diagnostics.data && (
              <Badge variant={diagnostics.data.passed ? "green" : "red"}>
                {diagnostics.data.passed ? "pass" : "fail"}
              </Badge>
            )}
          </div>
          {diagnostics.loading && <p className="text-sm text-[var(--color-text-muted)]">Running checks…</p>}
          {diagnostics.error && <p className="text-sm text-[var(--color-signal-red)]">{diagnostics.error}</p>}
          <div className="flex flex-col gap-2">
            {diagnostics.data?.checks.map((c) => (
              <div key={c.name} className="flex items-center justify-between gap-3 text-sm">
                <span className="font-mono text-[var(--color-text)]">{c.name}</span>
                <span className="flex-1 truncate text-[var(--color-text-faint)]">{c.detail}</span>
                <Badge variant={c.passed ? "green" : "red"}>{c.passed ? "ok" : "fail"}</Badge>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Resources */}
      <Card>
        <CardContent className="flex flex-col gap-4 pt-5">
          <div className="flex items-center gap-2">
            <Cpu className="size-4 text-[var(--color-signal-violet)]" />
            <h2 className="text-sm font-semibold text-[var(--color-text)]">Resources</h2>
          </div>
          {resources.loading && <p className="text-sm text-[var(--color-text-muted)]">Sampling…</p>}
          {resources.error && <p className="text-sm text-[var(--color-signal-red)]">{resources.error}</p>}
          {resources.data && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              <Metric label="CPU" value={fmtPct(resources.data.cpu_percent)} />
              <Metric label="Process RAM" value={fmtMb(resources.data.process_rss_mb)} />
              <Metric label="System RAM" value={fmtPct(resources.data.system_memory_percent)} />
              <Metric label="Browser RAM" value={fmtMb(resources.data.browser_memory_mb)} />
              <Metric label="Queue / Active" value={`${resources.data.queue_size} / ${resources.data.active_tasks}`} />
            </div>
          )}
          {resources.data && !resources.data.psutil_available && (
            <p className="text-xs text-[var(--color-text-faint)]">
              psutil not available — CPU/RAM metrics are limited.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Build info + Config backup */}
      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardContent className="flex flex-col gap-3 pt-5">
            <div className="flex items-center gap-2">
              <GitCommit className="size-4 text-[var(--color-text-muted)]" />
              <h2 className="text-sm font-semibold text-[var(--color-text)]">Build</h2>
            </div>
            {version.data && (
              <div className="flex flex-col gap-1 font-mono text-xs text-[var(--color-text-muted)]">
                <span>version: {version.data.version}</span>
                <span>commit: {version.data.commit_short}{version.data.dirty ? " (dirty)" : ""}</span>
                <span>branch: {version.data.branch}</span>
                {version.data.repo !== "unknown" && <span>repo: {version.data.repo}</span>}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="flex flex-col gap-3 pt-5">
            <div className="flex items-center gap-2">
              <Activity className="size-4 text-[var(--color-text-muted)]" />
              <h2 className="text-sm font-semibold text-[var(--color-text)]">Configuration</h2>
            </div>
            <p className="text-xs text-[var(--color-text-faint)]">
              Back up the current (non-secret) settings, or export/restore from a snapshot.
            </p>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={runBackup}>
                Backup now
              </Button>
            </div>
            {backupMsg && <p className="text-xs text-[var(--color-text-muted)]">{backupMsg}</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2">
      <span className="text-[11px] uppercase tracking-wide text-[var(--color-text-faint)]">{label}</span>
      <span className="font-mono text-sm text-[var(--color-text)]">{value}</span>
    </div>
  )
}

function fmtPct(v: number | null): string {
  return v === null ? "n/a" : `${v.toFixed(1)}%`
}

function fmtMb(v: number | null): string {
  return v === null ? "n/a" : `${v.toFixed(0)} MB`
}
