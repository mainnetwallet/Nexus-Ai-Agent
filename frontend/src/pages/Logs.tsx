import { useEffect, useRef, useState } from "react"
import { ScrollText, Pause, Play } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useAsync } from "@/hooks/use-async"
import { api } from "@/lib/api"

const LEVEL_COLOR: Record<string, string> = {
  ERROR: "var(--color-signal-red)",
  WARNING: "var(--color-signal-amber)",
  INFO: "var(--color-text-muted)",
  DEBUG: "var(--color-text-faint)",
}

function levelOf(line: string): string {
  for (const level of Object.keys(LEVEL_COLOR)) {
    if (line.includes(` ${level} `)) return level
  }
  return "INFO"
}

export function Logs() {
  const logs = useAsync(() => api.logs.tail(300), [])
  const [live, setLive] = useState(true)
  const [filter, setFilter] = useState("")
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!live) return
    const id = setInterval(() => logs.refetch(), 3000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" })
  }, [logs.data])

  const lines = (logs.data?.lines ?? []).filter((l) => l.toLowerCase().includes(filter.toLowerCase()))

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-text-muted)]">Console</p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Logs</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Tail of {logs.data?.file ?? "the backend log file"}.
          </p>
        </div>
        <Button variant="subtle" size="sm" onClick={() => setLive((v) => !v)}>
          {live ? <Pause className="size-4" /> : <Play className="size-4" />}
          {live ? "Pause" : "Resume"}
        </Button>
      </div>

      <Input placeholder="Filter logs…" value={filter} onChange={(e) => setFilter(e.target.value)} />

      <Card>
        <CardContent className="pt-5">
          {logs.error && <p className="text-sm text-[var(--color-signal-red)]">{logs.error}</p>}
          {!logs.error && lines.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-10 text-center">
              <ScrollText className="size-6 text-[var(--color-text-faint)]" />
              <p className="text-sm text-[var(--color-text-faint)]">
                {logs.loading ? "Loading logs…" : "No log lines match."}
              </p>
            </div>
          )}
          <div className="max-h-[60vh] overflow-y-auto rounded-md border border-[var(--color-border)] bg-[#060a12]">
            <pre className="p-3 font-mono text-[12px] leading-relaxed">
              {lines.map((line, i) => (
                <div key={i} style={{ color: LEVEL_COLOR[levelOf(line)] }} className="whitespace-pre-wrap break-words">
                  {line}
                </div>
              ))}
              <div ref={bottomRef} />
            </pre>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
