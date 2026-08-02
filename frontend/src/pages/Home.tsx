import { Link } from "react-router-dom"
import { Activity, ListChecks, FileBarChart2, MonitorPlay, ArrowUpRight } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { useAsync } from "@/hooks/use-async"
import { api, type TaskStatus } from "@/lib/api"

const STATUS_VARIANT: Record<TaskStatus, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  queued: "neutral",
  planning: "violet",
  running: "cyan",
  paused: "amber",
  succeeded: "green",
  failed: "red",
  cancelled: "neutral",
}

export function Home() {
  const tasks = useAsync(() => api.tasks.list(), [])
  const reports = useAsync(() => api.reports.list(), [])
  const browserStatus = useAsync(() => api.browser.status(), [])

  const running = tasks.data?.filter((t) => t.status === "running" || t.status === "planning").length ?? 0
  const queued = tasks.data?.filter((t) => t.status === "queued").length ?? 0
  const succeeded = tasks.data?.filter((t) => t.status === "succeeded").length ?? 0
  const failed = tasks.data?.filter((t) => t.status === "failed").length ?? 0

  return (
    <div className="flex flex-col gap-8">
      <div>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-amber)]">
          Mission control
        </p>
        <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Nexus-Agent overview</h1>
        <p className="mt-1 text-sm text-[var(--color-text-muted)]">
          A live read of what the agent is doing right now, and where to jump in.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Running" value={running} tone="cyan" icon={Activity} loading={tasks.loading} />
        <StatCard label="Queued" value={queued} tone="amber" icon={ListChecks} loading={tasks.loading} />
        <StatCard label="Succeeded" value={succeeded} tone="green" icon={FileBarChart2} loading={tasks.loading} />
        <StatCard label="Failed" value={failed} tone="red" icon={FileBarChart2} loading={tasks.loading} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <div>
              <CardTitle>Recent tasks</CardTitle>
              <CardDescription>Latest items in the task queue</CardDescription>
            </div>
            <Link to="/tasks" className="flex items-center gap-1 text-xs text-[var(--color-signal-cyan)] hover:underline">
              View all <ArrowUpRight className="size-3" />
            </Link>
          </CardHeader>
          <CardContent>
            {tasks.loading && <p className="text-sm text-[var(--color-text-muted)]">Loading tasks…</p>}
            {tasks.error && <p className="text-sm text-[var(--color-signal-red)]">{tasks.error}</p>}
            {!tasks.loading && !tasks.error && tasks.data?.length === 0 && (
              <EmptyRow message="No tasks yet — create one from the Tasks page." />
            )}
            <div className="flex flex-col divide-y divide-[var(--color-border)]">
              {tasks.data?.slice(0, 6).map((t) => (
                <div key={t.id} className="flex items-center justify-between py-2.5">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-[var(--color-text)]">{t.goal}</p>
                    <p className="truncate font-mono text-xs text-[var(--color-text-faint)]">{t.website}</p>
                  </div>
                  <Badge variant={STATUS_VARIANT[t.status]}>{t.status}</Badge>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <MonitorPlay className="size-4 text-[var(--color-signal-cyan)]" />
              Live browser
            </CardTitle>
            <CardDescription>What the agent is looking at</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {browserStatus.loading && <p className="text-sm text-[var(--color-text-muted)]">Checking session…</p>}
            {browserStatus.data && (
              <>
                <div className="flex items-center gap-2">
                  <span
                    className={`size-2 rounded-full ${
                      browserStatus.data.active ? "bg-[var(--color-signal-green)] live-pulse" : "bg-[var(--color-text-faint)]"
                    }`}
                  />
                  <span className="text-sm text-[var(--color-text)]">
                    {browserStatus.data.active ? "Session active" : "Idle"}
                  </span>
                </div>
                {browserStatus.data.url && (
                  <p className="truncate font-mono text-xs text-[var(--color-text-muted)]">{browserStatus.data.url}</p>
                )}
              </>
            )}
            <Link
              to="/browser"
              className="mt-1 flex items-center gap-1 text-xs text-[var(--color-signal-cyan)] hover:underline"
            >
              Open live view <ArrowUpRight className="size-3" />
            </Link>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent reports</CardTitle>
          <CardDescription>Execution outcomes from finished tasks</CardDescription>
        </CardHeader>
        <CardContent>
          {reports.loading && <p className="text-sm text-[var(--color-text-muted)]">Loading reports…</p>}
          {!reports.loading && reports.data?.length === 0 && <EmptyRow message="No reports yet." />}
          <div className="flex flex-col divide-y divide-[var(--color-border)]">
            {reports.data?.slice(0, 5).map((r) => (
              <div key={r.id} className="flex items-center justify-between py-2.5">
                <p className="truncate text-sm text-[var(--color-text)]">{r.summary}</p>
                <span className="font-mono text-xs text-[var(--color-text-faint)]">
                  {r.execution_seconds.toFixed(1)}s
                </span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function StatCard({
  label,
  value,
  tone,
  icon: Icon,
  loading,
}: {
  label: string
  value: number
  tone: "cyan" | "amber" | "green" | "red"
  icon: typeof Activity
  loading: boolean
}) {
  const toneColor = `var(--color-signal-${tone})`
  return (
    <Card>
      <CardContent className="flex items-center justify-between pt-5">
        <div>
          <p className="text-xs uppercase tracking-wide text-[var(--color-text-muted)]">{label}</p>
          <p className="mt-1 font-mono text-2xl font-semibold" style={{ color: toneColor }}>
            {loading ? "–" : value}
          </p>
        </div>
        <Icon className="size-5" style={{ color: toneColor, opacity: 0.6 }} />
      </CardContent>
    </Card>
  )
}

function EmptyRow({ message }: { message: string }) {
  return <p className="py-4 text-sm text-[var(--color-text-faint)]">{message}</p>
}
