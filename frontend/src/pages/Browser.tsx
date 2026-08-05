import { useEffect, useRef, useState } from "react"
import { MonitorPlay, Users, Globe } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { useAsync } from "@/hooks/use-async"
import { api } from "@/lib/api"

export function Browser() {
  const status = useAsync(() => api.browser.status(), [])
  const [imgUrl, setImgUrl] = useState<string | null>(null)
  const [staleAt, setStaleAt] = useState<number | null>(null)
  const objectUrlRef = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let ws: WebSocket | null = null

    function connectWs() {
      try {
        const url = api.wsUrl("/api/browser/ws/live")
        ws = new WebSocket(url)

        ws.onmessage = (event) => {
          if (cancelled) return
          try {
            const data = JSON.parse(event.data)
            if (data.image_base64) {
              setImgUrl(`data:${data.mime_type || "image/jpeg"};base64,${data.image_base64}`)
              setStaleAt(data.captured_at ? data.captured_at * 1000 : Date.now())
            }
          } catch {
            // Ignore non-json or malformed frames
          }
        }

        ws.onerror = () => {
          // Fallback HTTP poll if WS encounters error
        }

        ws.onclose = () => {
          if (!cancelled) {
            setTimeout(connectWs, 3000)
          }
        }
      } catch {
        // Fallback
      }
    }

    connectWs()

    // Secondary HTTP fallback poll just in case WS is unavailable
    async function fallbackPoll() {
      try {
        const blob = await api.browser.screenshotBlob()
        if (cancelled) return
        if (blob) {
          const url = URL.createObjectURL(blob)
          if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
          objectUrlRef.current = url
          setImgUrl(url)
          setStaleAt(Date.now())
        }
      } catch {
        // Quiet fallback
      }
    }

    const fallbackId = setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        fallbackPoll()
      }
    }, 2000)

    return () => {
      cancelled = true
      clearInterval(fallbackId)
      if (ws) ws.close()
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    }
  }, [])

  const active = status.data?.active ?? false

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-cyan)]">Live view</p>
        <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Live Browser</h1>
        <p className="mt-1 text-sm text-[var(--color-text-muted)]">
          Read-only observability into whatever page the agent is currently on. No remote control here.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <MonitorPlay className="size-4 text-[var(--color-signal-cyan)]" />
              Live screenshot
            </CardTitle>
            <Badge variant={active ? "green" : "neutral"}>{active ? "live" : "idle"}</Badge>
          </CardHeader>
          <CardContent>
            <div className="relative w-full overflow-hidden rounded-md border border-[var(--color-border)] bg-black" style={{ minHeight: "420px", maxHeight: "75vh" }}>
              {imgUrl ? (
                <img src={imgUrl} alt="Live agent browser view" className="h-full w-full object-contain" style={{ maxHeight: "75vh" }} />
              ) : (
                <div className="flex h-full w-full items-center justify-center">
                  <p className="text-sm text-[var(--color-text-faint)]">
                    {status.loading ? "Connecting…" : "No frame captured yet"}
                  </p>
                </div>
              )}
              {active && (
                <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-[var(--color-signal-cyan)]/60 scan-line" />
              )}
            </div>
            {staleAt && (
              <p className="mt-2 font-mono text-xs text-[var(--color-text-faint)]">
                last frame {new Date(staleAt).toLocaleTimeString()}
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Session status</CardTitle>
            <CardDescription>Polled every 15s</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {status.error && <p className="text-sm text-[var(--color-signal-red)]">{status.error}</p>}

            <InfoRow icon={Globe} label="URL" value={status.data?.url ?? "—"} mono />
            <InfoRow icon={MonitorPlay} label="Title" value={status.data?.title ?? "—"} />
            <InfoRow icon={Users} label="Viewers" value={String(status.data?.viewers ?? 0)} />
            {status.data?.task_id && <InfoRow icon={Globe} label="Task" value={status.data.task_id} mono />}
          </CardContent>
        </Card>
      </div>
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
