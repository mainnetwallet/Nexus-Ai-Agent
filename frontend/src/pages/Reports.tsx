import { FileBarChart2, Clock, Hash, Image as ImageIcon } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { useAsync } from "@/hooks/use-async"
import { api } from "@/lib/api"

const STATUS_VARIANT: Record<string, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  succeeded: "green",
  failed: "red",
  running: "cyan",
  cancelled: "neutral",
}

export function Reports() {
  const reports = useAsync(() => api.reports.list(), [])

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-green)]">Outcomes</p>
        <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Reports</h1>
        <p className="mt-1 text-sm text-[var(--color-text-muted)]">
          What happened on each completed task — duration, transactions, and captured evidence.
        </p>
      </div>

      {reports.loading && <p className="text-sm text-[var(--color-text-muted)]">Loading reports…</p>}
      {reports.error && <p className="text-sm text-[var(--color-signal-red)]">{reports.error}</p>}
      {!reports.loading && reports.data?.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
            <FileBarChart2 className="size-6 text-[var(--color-text-faint)]" />
            <p className="text-sm text-[var(--color-text-faint)]">No reports yet — they appear once a task finishes.</p>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-3">
        {reports.data?.map((r) => (
          <Card key={r.id}>
            <CardContent className="flex flex-col gap-3 pt-5">
              <div className="flex items-start justify-between gap-4">
                <p className="text-sm text-[var(--color-text)]">{r.summary}</p>
                <Badge variant={STATUS_VARIANT[r.status] ?? "neutral"}>{r.status}</Badge>
              </div>
              <div className="flex flex-wrap items-center gap-4 text-xs text-[var(--color-text-faint)]">
                <span className="flex items-center gap-1 font-mono">
                  <Clock className="size-3" /> {r.execution_seconds.toFixed(1)}s
                </span>
                <span className="flex items-center gap-1 font-mono">
                  <Hash className="size-3" /> task {r.task_id.slice(0, 8)}
                </span>
                {r.tx_hashes.length > 0 && (
                  <span className="flex items-center gap-1 font-mono">
                    <Hash className="size-3" /> {r.tx_hashes.length} tx
                  </span>
                )}
                {r.screenshots.length > 0 && (
                  <span className="flex items-center gap-1 font-mono">
                    <ImageIcon className="size-3" /> {r.screenshots.length} screenshots
                  </span>
                )}
                <span className="ml-auto font-mono">{new Date(r.created_at).toLocaleString()}</span>
              </div>
              {r.tx_hashes.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {r.tx_hashes.map((tx) => (
                    <span
                      key={tx}
                      className="truncate rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 font-mono text-[11px] text-[var(--color-text-muted)]"
                    >
                      {tx}
                    </span>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
