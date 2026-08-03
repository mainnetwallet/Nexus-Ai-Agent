import { useState } from "react"
import { Plug, AlertTriangle, Search, Users, MessageSquare, Mail } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api } from "@/lib/api"
import type { RoutedTool, SocialConnectorStatus } from "@/lib/api"

const SOCIAL_ICON: Record<string, typeof Users> = {
  x: Users,
  discord: MessageSquare,
  gmail: Mail,
}

const SESSION_BADGE: Record<string, "green" | "red" | "amber" | "neutral"> = {
  connected: "green",
  login_required: "amber",
  session_expired: "red",
  unknown: "neutral",
}

function formatLastUsed(ts: number | null): string {
  if (!ts) return "never"
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function SocialConnectorsPanel() {
  const status = useAsync(() => api.mcp.socialStatus(), [])
  const entries = Object.values(status.data?.connectors ?? {}) as SocialConnectorStatus[]

  if (status.error || entries.length === 0) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Users className="size-4 text-[var(--color-signal-cyan)]" />
          Social connectors
        </CardTitle>
        <CardDescription>
          X, Discord, and Gmail automate the profile's live authenticated browser session --
          no API keys or stored passwords. Session status reflects that profile's current login state.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {entries.map((c) => {
          const Icon = SOCIAL_ICON[c.service] ?? Plug
          return (
            <div key={c.connector} className="flex items-center justify-between gap-4 rounded-md border border-[var(--color-border)] p-3">
              <div className="flex items-center gap-3">
                <Icon className="size-4 text-[var(--color-signal-violet)]" />
                <div>
                  <p className="text-sm font-medium capitalize text-[var(--color-text)]">{c.service}</p>
                  <p className="text-xs text-[var(--color-text-muted)]">{c.account ?? "no account configured"}</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant={STATUS_BADGE[c.connection_status] ?? "neutral"}>{c.connection_status}</Badge>
                <Badge variant={SESSION_BADGE[c.session_status] ?? "neutral"}>{c.session_status.replace("_", " ")}</Badge>
                <span className="text-xs text-[var(--color-text-muted)]">last used {formatLastUsed(c.last_used_at)}</span>
              </div>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

const STATUS_BADGE: Record<string, "green" | "red" | "amber" | "neutral"> = {
  connected: "green",
  ok: "green",
  error: "red",
  disconnected: "neutral",
  disabled: "neutral",
  connecting: "amber",
}

function ConnectorConfigSummary({ name, config }: { name: string; config: Record<string, unknown> }) {
  if (name === "filesystem") {
    const roots = (config.roots as string[] | undefined) ?? []
    return <p className="text-xs text-[var(--color-text-muted)]">roots: {roots.length ? roots.join(", ") : "(none configured)"}</p>
  }
  if (name === "terminal") {
    const allowlist = (config.commands_allowlist as string[] | undefined) ?? []
    return (
      <p className="text-xs text-[var(--color-text-muted)]">
        allow-listed commands: {allowlist.length ? allowlist.join(", ") : "(none — terminal is opt-in and off by default for safety)"}
      </p>
    )
  }
  if (name === "github") {
    const hasToken = Boolean(config.token)
    return <p className="text-xs text-[var(--color-text-muted)]">token configured: {hasToken ? "yes" : "no"}</p>
  }
  if (name === "x" || name === "discord" || name === "gmail") {
    const account = config.account as string | undefined
    return (
      <p className="text-xs text-[var(--color-text-muted)]">
        account: {account || "(none configured -- reuses whichever profile's session is active)"}
      </p>
    )
  }
  return null
}

export function Mcp() {
  const toast = useToast()
  const connectors = useAsync(() => api.mcp.connectors(), [])
  const [busy, setBusy] = useState<string | null>(null)
  const [routeText, setRouteText] = useState("")
  const [routing, setRouting] = useState(false)
  const [routeResult, setRouteResult] = useState<{ matched: boolean; route: RoutedTool | null } | null>(null)

  async function toggle(name: string, currentlyEnabled: boolean) {
    setBusy(name)
    try {
      if (currentlyEnabled) {
        await api.mcp.disable(name)
        toast.push(`${name} disabled`, "success")
      } else {
        await api.mcp.enable(name)
        toast.push(`${name} enabled`, "success")
      }
      await connectors.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : `Failed to toggle ${name}`, "error")
    } finally {
      setBusy(null)
    }
  }

  async function testRoute() {
    if (!routeText.trim()) return
    setRouting(true)
    try {
      const result = await api.mcp.route(routeText)
      setRouteResult(result)
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Routing test failed", "error")
    } finally {
      setRouting(false)
    }
  }

  const list = connectors.data?.connectors ?? []

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-text-muted)]">Extensibility</p>
        <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">MCP Core</h1>
        <p className="mt-1 text-sm text-[var(--color-text-muted)]">
          Connectors let Chat, the Planner, and taught Skills reach outside the live browser session —
          filesystem, terminal, off-page web, and GitHub. The terminal connector is opt-in and disabled by
          default for safety.
        </p>
      </div>

      {connectors.error && (
        <Card>
          <CardContent className="flex items-center gap-2 pt-5 text-sm text-[var(--color-signal-red)]">
            <AlertTriangle className="size-4" />
            {connectors.error}
          </CardContent>
        </Card>
      )}

      <SocialConnectorsPanel />

      <div className="flex flex-col gap-3">
        {list.map((c) => (
          <Card key={c.name}>
            <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Plug className="size-4 text-[var(--color-signal-violet)]" />
                  {c.name}
                  <Badge variant="neutral">v{c.version}</Badge>
                  <Badge variant={STATUS_BADGE[c.status] ?? "neutral"}>{c.status}</Badge>
                  <Badge variant="neutral">{c.tool_count} tool{c.tool_count === 1 ? "" : "s"}</Badge>
                </CardTitle>
                {c.description && <CardDescription className="mt-1">{c.description}</CardDescription>}
                {c.error && <p className="mt-1 text-xs text-[var(--color-signal-red)]">{c.error}</p>}
                <div className="mt-2">
                  <ConnectorConfigSummary name={c.name} config={c.config} />
                </div>
              </div>
              <Switch
                checked={c.enabled}
                disabled={busy === c.name}
                onCheckedChange={() => toggle(c.name, c.enabled)}
              />
            </CardHeader>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Search className="size-4 text-[var(--color-signal-cyan)]" />
            Test routing
          </CardTitle>
          <CardDescription>
            See which connector and tool free text would be routed to, without executing it.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex gap-2">
            <Input
              placeholder='e.g. "list files in /data" or "check github issues on org/repo"'
              value={routeText}
              onChange={(e) => setRouteText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && testRoute()}
            />
            <Button variant="subtle" size="sm" onClick={testRoute} disabled={routing}>
              {routing ? "Routing…" : "Route"}
            </Button>
          </div>
          {routeResult && (
            <p className="text-sm text-[var(--color-text-muted)]">
              {routeResult.matched && routeResult.route
                ? `Would call ${routeResult.route.connector}.${routeResult.route.tool_name} (score ${routeResult.route.score.toFixed(2)})`
                : "No connector matched confidently enough."}
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
