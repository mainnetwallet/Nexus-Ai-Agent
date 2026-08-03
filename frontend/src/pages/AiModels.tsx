import { useState } from "react"
import { BrainCog, CheckCircle2, Loader2, RefreshCw, XCircle, Zap } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useAsync } from "@/hooks/use-async"
import { api, type AiTaskType } from "@/lib/api"

const TASK_TYPES: { value: AiTaskType; label: string }[] = [
  { value: "coding", label: "Coding" },
  { value: "browser_automation", label: "Browser Automation" },
  { value: "planning", label: "Planning" },
  { value: "vision", label: "Vision" },
  { value: "long_context", label: "Long Context" },
  { value: "fast_response", label: "Fast Response" },
  { value: "general_chat", label: "General Chat" },
  { value: "research", label: "Research" },
  { value: "reasoning", label: "Reasoning" },
  { value: "low_cost", label: "Low Cost" },
]

const STATUS_VARIANT: Record<string, "neutral" | "green" | "amber" | "red"> = {
  healthy: "green",
  degraded: "amber",
  down: "red",
  unknown: "neutral",
}

function providerLabel(id: string): string {
  return id
    .split("_")
    .map((w) => (w.length <= 3 ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)))
    .join(" ")
}

export function AiModels() {
  const view = useAsync(() => api.aiModels.get(), [])
  const [testing, setTesting] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)

  const data = view.data

  const runMutation = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await fn()
      await view.refetch()
    } catch (err) {
      // surfaced inline below via view.error on next refetch failure; for
      // mutation-specific errors just log to console, the refetch above
      // still keeps the page in a consistent state.
      console.error(err)
    } finally {
      setBusy(false)
    }
  }

  const testConnection = async (provider: string) => {
    setTesting(provider)
    try {
      const res = await api.aiModels.testConnection(provider)
      setTestResult((prev) => ({
        ...prev,
        [provider]: res.ok ? `ok (${res.latency_ms?.toFixed(0)}ms)` : res.error || "failed",
      }))
      await view.refetch()
    } catch (err) {
      setTestResult((prev) => ({ ...prev, [provider]: err instanceof Error ? err.message : String(err) }))
    } finally {
      setTesting(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-cyan)]">
            AI Model Manager
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">AI Models</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Switch providers, configure smart routing and fallback, and monitor health across every
            connected LLM provider.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => view.refetch()} disabled={view.loading}>
          <RefreshCw className={`size-3.5 ${view.loading ? "animate-spin" : ""}`} /> Refresh
        </Button>
      </div>

      {view.error && <p className="text-sm text-[var(--color-signal-red)]">{view.error}</p>}

      {data && (
        <>
          {/* Current state + routing mode */}
          <Card>
            <CardContent className="flex flex-col gap-4 pt-5">
              <div className="flex items-center gap-2">
                <BrainCog className="size-4 text-[var(--color-signal-cyan)]" />
                <h2 className="text-sm font-semibold text-[var(--color-text)]">Active Configuration</h2>
              </div>

              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Metric label="Current Provider" value={providerLabel(data.current_provider)} />
                <Metric label="Current Model" value={data.current_model || "(default)"} />
                <Metric label="Routing Mode" value={data.routing_mode === "auto" ? "Automatic" : "Manual"} />
                <Metric label="Fallback Provider" value={providerLabel(data.fallback_provider)} />
              </div>

              {data.temporary_override && (
                <div className="flex items-center gap-2 rounded-md border border-[var(--color-signal-amber)]/40 bg-[var(--color-signal-amber-dim)] px-3 py-2 text-xs text-[var(--color-signal-amber)]">
                  <Zap className="size-3.5" />
                  Temporary override active: {providerLabel(data.temporary_override.provider)}
                  {data.temporary_override.reason ? ` — ${data.temporary_override.reason}` : ""}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="ml-auto h-6 px-2 text-xs"
                    disabled={busy}
                    onClick={() => runMutation(() => api.aiModels.clearOverride())}
                  >
                    Clear
                  </Button>
                </div>
              )}

              <div className="flex flex-wrap items-center gap-6 border-t border-[var(--color-border)] pt-4">
                <div className="flex items-center gap-2">
                  <Switch
                    checked={data.routing_mode === "auto"}
                    disabled={busy}
                    onCheckedChange={(checked) =>
                      runMutation(() => api.aiModels.setRoutingMode(checked ? "auto" : "manual"))
                    }
                  />
                  <span className="text-sm text-[var(--color-text)]">Automatic Smart Routing</span>
                </div>

                <div className="flex items-center gap-2">
                  <span className="text-sm text-[var(--color-text-muted)]">Default provider</span>
                  <Select
                    value={data.current_provider}
                    onValueChange={(val) => runMutation(() => api.aiModels.switch(val))}
                  >
                    <SelectTrigger className="h-8 w-44">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {data.providers.map((p) => (
                        <SelectItem key={p.provider} value={p.provider}>
                          {providerLabel(p.provider)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="flex items-center gap-2">
                  <span className="text-sm text-[var(--color-text-muted)]">Fallback provider</span>
                  <Select
                    value={data.fallback_provider}
                    onValueChange={(val) => runMutation(() => api.aiModels.setFallback(val))}
                  >
                    <SelectTrigger className="h-8 w-44">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {data.providers.map((p) => (
                        <SelectItem key={p.provider} value={p.provider}>
                          {providerLabel(p.provider)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Routing rules */}
          <Card>
            <CardContent className="flex flex-col gap-4 pt-5">
              <div className="flex items-center gap-2">
                <Zap className="size-4 text-[var(--color-signal-violet)]" />
                <h2 className="text-sm font-semibold text-[var(--color-text)]">Smart Routing Rules</h2>
                <Badge variant={data.routing_mode === "auto" ? "green" : "neutral"}>
                  {data.routing_mode === "auto" ? "active" : "inactive"}
                </Badge>
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {TASK_TYPES.map((t) => (
                  <div
                    key={t.value}
                    className="flex items-center justify-between gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2"
                  >
                    <span className="text-sm text-[var(--color-text)]">{t.label}</span>
                    <Select
                      value={data.routing_rules[t.value] || data.current_provider}
                      onValueChange={(val) => runMutation(() => api.aiModels.setRoutingRule(t.value, val))}
                    >
                      <SelectTrigger className="h-7 w-36 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {data.providers.map((p) => (
                          <SelectItem key={p.provider} value={p.provider}>
                            {providerLabel(p.provider)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          {/* Provider list + health */}
          <Card>
            <CardContent className="flex flex-col gap-4 pt-5">
              <div className="flex items-center gap-2">
                <CheckCircle2 className="size-4 text-[var(--color-signal-green)]" />
                <h2 className="text-sm font-semibold text-[var(--color-text)]">Providers &amp; Health</h2>
              </div>
              <div className="flex flex-col gap-2">
                {data.providers.map((p) => (
                  <div
                    key={p.provider}
                    className="flex flex-wrap items-center gap-3 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2"
                  >
                    <span className="w-36 shrink-0 font-mono text-xs text-[var(--color-text)]">
                      {providerLabel(p.provider)}
                    </span>
                    <Badge variant={p.has_api_key ? "cyan" : "neutral"}>
                      {p.has_api_key ? "key configured" : "no key"}
                    </Badge>
                    <Badge variant={STATUS_VARIANT[p.health.status] ?? "neutral"}>{p.health.status}</Badge>
                    <span className="text-xs text-[var(--color-text-faint)]">
                      {p.health.total_requests > 0
                        ? `${(p.health.availability * 100).toFixed(0)}% avail · ${p.health.latency_ms ?? "—"}ms`
                        : "no calls yet"}
                    </span>

                    <div className="ml-auto flex items-center gap-2">
                      {testResult[p.provider] && (
                        <span className="text-xs text-[var(--color-text-faint)]">{testResult[p.provider]}</span>
                      )}
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!p.has_api_key || testing === p.provider}
                        onClick={() => testConnection(p.provider)}
                      >
                        {testing === p.provider ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <CheckCircle2 className="size-3.5" />
                        )}
                        Test
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        onClick={() =>
                          runMutation(() =>
                            p.enabled ? api.aiModels.disable(p.provider) : api.aiModels.enable(p.provider)
                          )
                        }
                      >
                        {p.enabled ? <XCircle className="size-3.5" /> : <CheckCircle2 className="size-3.5" />}
                        {p.enabled ? "Disable" : "Enable"}
                      </Button>
                      <Button
                        variant="subtle"
                        size="sm"
                        disabled={busy}
                        onClick={() => runMutation(() => api.aiModels.setOverride(p.provider, undefined, "dashboard: one-off"))}
                      >
                        Use once
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2">
      <span className="text-[11px] uppercase tracking-wide text-[var(--color-text-faint)]">{label}</span>
      <span className="truncate font-mono text-sm text-[var(--color-text)]" title={value}>
        {value}
      </span>
    </div>
  )
}
