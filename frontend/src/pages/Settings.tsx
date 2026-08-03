import { useEffect, useState, type ReactNode } from "react"
import { Link } from "react-router-dom"
import { Settings2, ShieldAlert, Eye, MonitorPlay, Save, BrainCog, ArrowUpRight } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Separator } from "@/components/ui/separator"
import { Badge } from "@/components/ui/badge"
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api, type SettingsUpdateInput, type SettingsView } from "@/lib/api"

export function Settings() {
  const toast = useToast()
  const settings = useAsync(() => api.settings.get(), [])
  const [draft, setDraft] = useState<SettingsView | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (settings.data) setDraft(settings.data)
  }, [settings.data])

  async function save(patch: SettingsUpdateInput) {
    if (!draft) return
    setSaving(true)
    try {
      const updated = await api.settings.update(patch)
      setDraft(updated)
      toast.push("Settings saved", "success")
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to save settings", "error")
    } finally {
      setSaving(false)
    }
  }

  if (settings.loading || !draft) {
    return <p className="text-sm text-[var(--color-text-muted)]">Loading settings…</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-text-muted)]">Configuration</p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Settings</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Changes here apply to this backend process only — secrets stay in the server's .env.
          </p>
        </div>
        <Badge variant={draft.environment === "production" ? "green" : "amber"}>{draft.environment}</Badge>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlert className="size-4 text-[var(--color-signal-red)]" />
            Wallet approval policy
          </CardTitle>
          <CardDescription>
            Every wallet popup is human-approved by default. Auto-approval only ever applies to allowlisted
            contracts under the USD cap below.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <Row
            label="Require manual approval"
            description="Off only enables auto-approval for allowlisted contracts under the cap — it never removes human review outside that policy."
          >
            <Switch
              checked={draft.wallet_require_manual_approval}
              onCheckedChange={(v) => setDraft({ ...draft, wallet_require_manual_approval: v })}
            />
          </Row>

          <Separator />

          <Row label="Max auto-approve value (USD)" description="0 means manual approval is always required.">
            <Input
              type="number"
              min={0}
              step="0.01"
              className="w-32"
              value={draft.wallet_max_auto_approve_value_usd}
              onChange={(e) =>
                setDraft({ ...draft, wallet_max_auto_approve_value_usd: Number(e.target.value) })
              }
            />
          </Row>

          <Separator />

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="allowlist">Allowlisted contracts</Label>
            <Input
              id="allowlist"
              placeholder="0xabc…, 0xdef…"
              value={draft.wallet_allowlisted_contracts}
              onChange={(e) => setDraft({ ...draft, wallet_allowlisted_contracts: e.target.value })}
            />
            <p className="text-xs text-[var(--color-text-faint)]">Comma-separated contract addresses.</p>
          </div>

          <div className="flex justify-end">
            <Button
              size="sm"
              disabled={saving}
              onClick={() =>
                save({
                  wallet_require_manual_approval: draft.wallet_require_manual_approval,
                  wallet_max_auto_approve_value_usd: draft.wallet_max_auto_approve_value_usd,
                  wallet_allowlisted_contracts: draft.wallet_allowlisted_contracts,
                })
              }
            >
              <Save className="size-4" /> Save policy
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Eye className="size-4 text-[var(--color-signal-violet)]" />
            Perception
          </CardTitle>
          <CardDescription>Vision-LLM and OCR fallback for canvas-heavy or image-only pages.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <Row label="Vision fallback enabled">
            <Switch
              checked={draft.vision_enabled}
              onCheckedChange={(v) => setDraft({ ...draft, vision_enabled: v })}
            />
          </Row>
          <Separator />
          <Row label="OCR fallback enabled">
            <Switch checked={draft.ocr_enabled} onCheckedChange={(v) => setDraft({ ...draft, ocr_enabled: v })} />
          </Row>
          <Separator />
          <Row label="Min interactive elements before fallback triggers">
            <Input
              type="number"
              min={0}
              className="w-24"
              value={draft.vision_min_elements_threshold}
              onChange={(e) => setDraft({ ...draft, vision_min_elements_threshold: Number(e.target.value) })}
            />
          </Row>
          <div className="flex justify-end">
            <Button
              size="sm"
              variant="subtle"
              disabled={saving}
              onClick={() =>
                save({
                  vision_enabled: draft.vision_enabled,
                  ocr_enabled: draft.ocr_enabled,
                  vision_min_elements_threshold: draft.vision_min_elements_threshold,
                })
              }
            >
              <Save className="size-4" /> Save perception settings
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <MonitorPlay className="size-4 text-[var(--color-signal-cyan)]" />
            Live session
          </CardTitle>
          <CardDescription>Screenshot streaming shown on the Browser page.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <Row label="Live session enabled">
            <Switch
              checked={draft.live_session_enabled}
              onCheckedChange={(v) => setDraft({ ...draft, live_session_enabled: v })}
            />
          </Row>
          <Separator />
          <Row label="Capture interval (ms)">
            <Input
              type="number"
              min={100}
              className="w-28"
              value={draft.live_session_interval_ms}
              onChange={(e) => setDraft({ ...draft, live_session_interval_ms: Number(e.target.value) })}
            />
          </Row>
          <Separator />
          <Row label="JPEG quality">
            <Input
              type="number"
              min={1}
              max={100}
              className="w-24"
              value={draft.live_session_jpeg_quality}
              onChange={(e) => setDraft({ ...draft, live_session_jpeg_quality: Number(e.target.value) })}
            />
          </Row>
          <div className="flex justify-end">
            <Button
              size="sm"
              variant="subtle"
              disabled={saving}
              onClick={() =>
                save({
                  live_session_enabled: draft.live_session_enabled,
                  live_session_interval_ms: draft.live_session_interval_ms,
                  live_session_jpeg_quality: draft.live_session_jpeg_quality,
                })
              }
            >
              <Save className="size-4" /> Save live session settings
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BrainCog className="size-4 text-[var(--color-signal-cyan)]" />
            AI Models
          </CardTitle>
          <CardDescription>
            Provider switching, per-task routing rules, fallback chain and health live on the dedicated{" "}
            <Link to="/ai-models" className="text-[var(--color-signal-cyan)] underline-offset-2 hover:underline">
              AI Models page
            </Link>
            . The essentials are here too.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <Row
            label="Automatic smart routing"
            description="When on, each task is routed to the provider configured for its task type instead of the default provider below."
          >
            <Switch
              checked={draft.ai_smart_routing_enabled}
              onCheckedChange={(v) => setDraft({ ...draft, ai_smart_routing_enabled: v })}
            />
          </Row>
          <Separator />
          <Row label="Default provider" description={`Currently: ${draft.llm_provider}`}>
            <span className="font-mono text-xs text-[var(--color-text-muted)]">{draft.llm_provider}</span>
          </Row>
          <Separator />
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="fallback">Fallback provider</Label>
            <Input
              id="fallback"
              value={draft.ai_fallback_provider}
              onChange={(e) => setDraft({ ...draft, ai_fallback_provider: e.target.value })}
            />
            <p className="text-xs text-[var(--color-text-faint)]">
              Tried automatically when the active provider times out, errors, or is rate-limited.
            </p>
          </div>
          <div className="flex items-center justify-between">
            <Button variant="ghost" size="sm" asChild>
              <Link to="/ai-models">
                Open full AI Model Manager <ArrowUpRight className="size-3.5" />
              </Link>
            </Button>
            <Button
              size="sm"
              variant="subtle"
              disabled={saving}
              onClick={() =>
                save({
                  ai_smart_routing_enabled: draft.ai_smart_routing_enabled,
                  ai_fallback_provider: draft.ai_fallback_provider,
                })
              }
            >
              <Save className="size-4" /> Save AI model settings
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Settings2 className="size-4 text-[var(--color-text-muted)]" />
            Runtime info
          </CardTitle>
          <CardDescription>Read-only — set via the backend's .env.</CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
          <InfoField label="App" value={draft.app_name} />
          <InfoField label="Model override" value={draft.llm_model_override || "default"} />
          <InfoField label="Browser channel" value={draft.browser_channel} />
        </CardContent>
      </Card>
    </div>
  )
}

function Row({
  label,
  description,
  children,
}: {
  label: string
  description?: string
  children: ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        <p className="text-sm text-[var(--color-text)]">{label}</p>
        {description && <p className="mt-0.5 max-w-md text-xs text-[var(--color-text-faint)]">{description}</p>}
      </div>
      {children}
    </div>
  )
}

function InfoField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-[var(--color-text-muted)]">{label}</p>
      <p className="mt-0.5 font-mono text-sm text-[var(--color-text)]">{value}</p>
    </div>
  )
}
