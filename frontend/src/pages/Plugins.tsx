import { useState } from "react"
import { Blocks, RefreshCw, AlertTriangle, ScanSearch } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Badge } from "@/components/ui/badge"
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api } from "@/lib/api"

export function Plugins() {
  const toast = useToast()
  const plugins = useAsync(() => api.plugins.list(), [])
  const [busy, setBusy] = useState<string | null>(null)
  const [rescanning, setRescanning] = useState(false)

  async function toggle(name: string, currentlyEnabled: boolean) {
    setBusy(name)
    try {
      if (currentlyEnabled) {
        await api.plugins.disable(name)
        toast.push(`${name} disabled`, "success")
      } else {
        await api.plugins.enable(name)
        toast.push(`${name} enabled`, "success")
      }
      await plugins.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : `Failed to toggle ${name}`, "error")
    } finally {
      setBusy(null)
    }
  }

  async function reload(name: string) {
    setBusy(name)
    try {
      await api.plugins.reload(name)
      toast.push(`${name} reloaded`, "success")
      await plugins.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : `Failed to reload ${name}`, "error")
    } finally {
      setBusy(null)
    }
  }

  async function rescan() {
    setRescanning(true)
    try {
      const result = await api.plugins.rescan()
      toast.push(
        result.discovered.length ? `Discovered: ${result.discovered.join(", ")}` : "No new plugins found",
        "success"
      )
      await plugins.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Rescan failed", "error")
    } finally {
      setRescanning(false)
    }
  }

  const list = plugins.data?.plugins ?? []

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-text-muted)]">Extensibility</p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Plugins</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Plugins observe the task lifecycle and wallet-approval decisions. They can log, notify, or veto a
            wallet approval — never approve one, and never see key material. Files are only ever loaded from{" "}
            <code className="font-mono text-[12px]">backend/plugins/installed/</code> on disk, not uploaded here.
          </p>
        </div>
        <Button variant="subtle" size="sm" onClick={rescan} disabled={rescanning}>
          <ScanSearch className="size-4" />
          {rescanning ? "Scanning…" : "Rescan disk"}
        </Button>
      </div>

      {plugins.error && (
        <Card>
          <CardContent className="flex items-center gap-2 pt-5 text-sm text-[var(--color-signal-red)]">
            <AlertTriangle className="size-4" />
            {plugins.error}
          </CardContent>
        </Card>
      )}

      {!plugins.error && !plugins.loading && list.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
            <Blocks className="size-6 text-[var(--color-text-faint)]" />
            <p className="text-sm text-[var(--color-text-faint)]">
              No plugins found under <code className="font-mono text-[12px]">backend/plugins/installed/</code>.
            </p>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-3">
        {list.map((p) => (
          <Card key={p.name}>
            <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Blocks className="size-4 text-[var(--color-signal-violet)]" />
                  {p.name}
                  <Badge variant="neutral">v{p.version}</Badge>
                  {p.error && <Badge variant="red">error</Badge>}
                </CardTitle>
                {p.description && <CardDescription className="mt-1">{p.description}</CardDescription>}
                {p.error && <p className="mt-1 text-xs text-[var(--color-signal-red)]">{p.error}</p>}
              </div>
              <div className="flex items-center gap-3">
                <Button
                  variant="subtle"
                  size="sm"
                  onClick={() => reload(p.name)}
                  disabled={busy === p.name || !!p.error}
                >
                  <RefreshCw className="size-3.5" />
                  Reload
                </Button>
                <Switch
                  checked={p.enabled}
                  disabled={busy === p.name || !!p.error}
                  onCheckedChange={() => toggle(p.name, p.enabled)}
                />
              </div>
            </CardHeader>
          </Card>
        ))}
      </div>
    </div>
  )
}
