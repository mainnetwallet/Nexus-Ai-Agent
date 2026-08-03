import { useMemo, useState, type FormEvent } from "react"
import {
  Sparkles,
  Search,
  Trash2,
  Copy,
  Share2,
  Download,
  CheckCircle2,
  XCircle,
  History,
  Wand2,
  ListOrdered,
} from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api, type Skill, type SkillSource } from "@/lib/api"

const SOURCE_VARIANT: Record<SkillSource, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  natural_language: "cyan",
  teach_mode: "violet",
  browser_demonstration: "violet",
  recorded_workflow: "amber",
  task_outcome: "green",
  correction: "amber",
  imported: "neutral",
  manual: "neutral",
}

const SOURCE_LABEL: Record<SkillSource, string> = {
  natural_language: "natural language",
  teach_mode: "teach mode",
  browser_demonstration: "browser demo",
  recorded_workflow: "recorded",
  task_outcome: "task outcome",
  correction: "correction",
  imported: "imported",
  manual: "manual",
}

export function Skills() {
  const toast = useToast()
  const skills = useAsync(() => api.skills.list(), [])
  const pending = useAsync(() => api.skills.pending.list(), [])
  const [search, setSearch] = useState("")
  const [categoryFilter, setCategoryFilter] = useState<string>("")
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [learnOpen, setLearnOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)

  const categories = useMemo(() => {
    const set = new Set((skills.data ?? []).map((s) => s.category).filter(Boolean))
    return Array.from(set)
  }, [skills.data])

  const filtered = useMemo(() => {
    let list = skills.data ?? []
    if (search.trim()) {
      const needle = search.trim().toLowerCase()
      list = list.filter(
        (s) => s.name.toLowerCase().includes(needle) || s.trigger.toLowerCase().includes(needle)
      )
    }
    if (categoryFilter) list = list.filter((s) => s.category === categoryFilter)
    return list
  }, [skills.data, search, categoryFilter])

  const selected = skills.data?.find((s) => s.id === selectedId) ?? filtered[0] ?? null
  const pendingList = pending.data ?? []

  async function refetchAll() {
    await Promise.all([skills.refetch(), pending.refetch()])
  }

  async function toggle(skill: Skill) {
    setBusy(skill.id)
    try {
      if (skill.enabled) {
        await api.skills.disable(skill.id)
        toast.push(`${skill.name} disabled`, "success")
      } else {
        await api.skills.enable(skill.id)
        toast.push(`${skill.name} enabled`, "success")
      }
      await skills.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : `Failed to toggle ${skill.name}`, "error")
    } finally {
      setBusy(null)
    }
  }

  async function remove(skill: Skill) {
    if (!confirm(`Delete the "${skill.name}" skill? This can't be undone.`)) return
    setBusy(skill.id)
    try {
      await api.skills.remove(skill.id)
      toast.push("Skill deleted", "success")
      if (selectedId === skill.id) setSelectedId(null)
      await skills.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to delete skill", "error")
    } finally {
      setBusy(null)
    }
  }

  async function duplicate(skill: Skill) {
    setBusy(skill.id)
    try {
      const copy = await api.skills.duplicate(skill.id)
      toast.push(`Duplicated as "${copy.name}"`, "success")
      await skills.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to duplicate skill", "error")
    } finally {
      setBusy(null)
    }
  }

  async function share(skill: Skill) {
    setBusy(skill.id)
    try {
      const { share_code } = await api.skills.shareCode(skill.id)
      await navigator.clipboard.writeText(share_code).catch(() => {})
      toast.push("Share code copied to clipboard", "success")
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to generate share code", "error")
    } finally {
      setBusy(null)
    }
  }

  async function exportSkill(skill: Skill) {
    try {
      const payload = await api.skills.exportSkill(skill.id)
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" })
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = `${skill.name.replace(/\s+/g, "_").toLowerCase()}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Export failed", "error")
    }
  }

  async function confirmPending(taskId: string) {
    setBusy(taskId)
    try {
      await api.skills.pending.confirm(taskId)
      toast.push("Skill saved", "success")
      await refetchAll()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to save skill", "error")
    } finally {
      setBusy(null)
    }
  }

  async function discardPending(taskId: string) {
    setBusy(taskId)
    try {
      await api.skills.pending.discard(taskId)
      await pending.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to discard suggestion", "error")
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-violet)]">
            Skill Learning System
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Skills</h1>
          <p className="mt-1 max-w-xl text-sm text-[var(--color-text-muted)]">
            Reusable workflows the agent can replay by trigger phrase. Learned from a description, Teach
            Mode (via chat or <code className="font-mono text-[12px]">/teach</code>), a recorded
            demonstration, or automatically after a successful task.
          </p>
        </div>
        <Dialog open={learnOpen} onOpenChange={setLearnOpen}>
          <DialogTrigger asChild>
            <Button>
              <Wand2 className="size-4" /> Learn from text
            </Button>
          </DialogTrigger>
          <DialogContent>
            <LearnFromTextForm
              onLearned={() => {
                setLearnOpen(false)
                skills.refetch()
              }}
            />
          </DialogContent>
        </Dialog>
      </div>

      {pendingList.length > 0 && (
        <Card>
          <CardContent className="flex flex-col gap-3 pt-5">
            <p className="text-sm font-semibold text-[var(--color-text)]">
              Pending suggestions <Badge variant="amber">{pendingList.length}</Badge>
            </p>
            <p className="text-xs text-[var(--color-text-muted)]">
              The agent just completed a task successfully and thinks this workflow is worth saving as a
              skill.
            </p>
            <div className="flex flex-col divide-y divide-[var(--color-border)]">
              {pendingList.map((p) => (
                <div key={p.task_id} className="flex items-center justify-between gap-4 py-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-[var(--color-text)]">{p.name}</p>
                    <p className="truncate text-xs text-[var(--color-text-faint)]">{p.description}</p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button
                      size="sm"
                      variant="subtle"
                      disabled={busy === p.task_id}
                      onClick={() => confirmPending(p.task_id)}
                    >
                      <CheckCircle2 className="size-3.5" /> Save
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy === p.task_id}
                      onClick={() => discardPending(p.task_id)}
                    >
                      <XCircle className="size-3.5" /> Discard
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-[1fr_380px] gap-6">
        <div className="flex flex-col gap-4">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--color-text-faint)]" />
              <Input
                placeholder="Search name or trigger…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8"
              />
            </div>
            <Select value={categoryFilter} onValueChange={setCategoryFilter}>
              <SelectTrigger className="w-48">
                <SelectValue placeholder="All categories" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="">All categories</SelectItem>
                {categories.map((c) => (
                  <SelectItem key={c} value={c}>
                    {c}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <Card>
            <CardContent className="pt-5">
              {skills.loading && <p className="text-sm text-[var(--color-text-muted)]">Loading skills…</p>}
              {skills.error && <p className="text-sm text-[var(--color-signal-red)]">{skills.error}</p>}
              {!skills.loading && filtered.length === 0 && (
                <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">
                  No skills yet. Teach one from chat, or use "Learn from text" above.
                </p>
              )}
              <div className="flex flex-col divide-y divide-[var(--color-border)]">
                {filtered.map((s) => (
                  <div key={s.id} className="flex items-center justify-between gap-3 py-3">
                    <button
                      onClick={() => setSelectedId(s.id)}
                      className={`min-w-0 flex-1 text-left transition-opacity ${
                        selected?.id === s.id ? "opacity-100" : "opacity-80 hover:opacity-100"
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-medium text-[var(--color-text)]">{s.name}</p>
                        <Badge variant={SOURCE_VARIANT[s.source]}>{SOURCE_LABEL[s.source]}</Badge>
                        {!s.enabled && <Badge variant="neutral">disabled</Badge>}
                      </div>
                      <p className="truncate font-mono text-xs text-[var(--color-text-faint)]">
                        {s.trigger.split("\n")[0] || "no trigger phrase set"}
                      </p>
                    </button>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className="font-mono text-xs text-[var(--color-text-faint)]">
                        {s.usage_count}× · {(s.success_rate * 100).toFixed(0)}%
                      </span>
                      <Switch
                        checked={s.enabled}
                        disabled={busy === s.id}
                        onCheckedChange={() => toggle(s)}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>

        <div>
          {selected ? (
            <SkillDetails
              skill={selected}
              busy={busy === selected.id}
              onDuplicate={() => duplicate(selected)}
              onShare={() => share(selected)}
              onExport={() => exportSkill(selected)}
              onDelete={() => remove(selected)}
              onChanged={() => skills.refetch()}
            />
          ) : (
            <Card>
              <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
                <Sparkles className="size-6 text-[var(--color-text-faint)]" />
                <p className="text-sm text-[var(--color-text-faint)]">Select a skill to see its workflow.</p>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

function SkillDetails({
  skill,
  busy,
  onDuplicate,
  onShare,
  onExport,
  onDelete,
  onChanged,
}: {
  skill: Skill
  busy: boolean
  onDuplicate: () => void
  onShare: () => void
  onExport: () => void
  onDelete: () => void
  onChanged: () => void
}) {
  const toast = useToast()
  const versions = useAsync(() => api.skills.versions(skill.id), [skill.id])
  const [rollingBack, setRollingBack] = useState<number | null>(null)

  async function rollback(version: number) {
    if (!confirm(`Roll back "${skill.name}" to version ${version}?`)) return
    setRollingBack(version)
    try {
      await api.skills.rollback(skill.id, version)
      toast.push(`Rolled back to version ${version}`, "success")
      onChanged()
      await versions.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Rollback failed", "error")
    } finally {
      setRollingBack(null)
    }
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-5">
        <div className="flex items-center gap-2">
          <Sparkles className="size-4 text-[var(--color-signal-violet)]" />
          <p className="text-sm font-semibold text-[var(--color-text)]">{skill.name}</p>
          <Badge variant="neutral">v{skill.version}</Badge>
        </div>
        {skill.description && <p className="text-xs text-[var(--color-text-muted)]">{skill.description}</p>}
        {skill.website_hint && (
          <p className="truncate font-mono text-xs text-[var(--color-text-faint)]">{skill.website_hint}</p>
        )}

        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="subtle" onClick={onDuplicate} disabled={busy}>
            <Copy className="size-3.5" /> Duplicate
          </Button>
          <Button size="sm" variant="subtle" onClick={onShare} disabled={busy}>
            <Share2 className="size-3.5" /> Share
          </Button>
          <Button size="sm" variant="subtle" onClick={onExport} disabled={busy}>
            <Download className="size-3.5" /> Export
          </Button>
          <Button size="sm" variant="danger" onClick={onDelete} disabled={busy}>
            <Trash2 className="size-3.5" /> Delete
          </Button>
        </div>

        <Tabs defaultValue="workflow">
          <TabsList>
            <TabsTrigger value="workflow">Workflow</TabsTrigger>
            <TabsTrigger value="triggers">Triggers</TabsTrigger>
            <TabsTrigger value="history">History</TabsTrigger>
          </TabsList>

          <TabsContent value="workflow">
            <div className="flex flex-col gap-2 pt-3">
              {skill.workflow.length === 0 && (
                <p className="text-xs text-[var(--color-text-faint)]">No steps recorded.</p>
              )}
              {skill.workflow.map((step, i) => (
                <div key={i} className="flex gap-2 border-b border-[var(--color-border)] pb-2 last:border-0">
                  <ListOrdered className="mt-0.5 size-3.5 shrink-0 text-[var(--color-text-faint)]" />
                  <div className="min-w-0">
                    <p className="text-xs text-[var(--color-text)]">
                      <span className="font-mono text-[11px] uppercase text-[var(--color-signal-cyan)]">
                        {step.action}
                      </span>{" "}
                      {step.target}
                      {step.value ? ` → ${step.value}` : ""}
                    </p>
                    {step.description && (
                      <p className="text-[11px] text-[var(--color-text-faint)]">{step.description}</p>
                    )}
                  </div>
                </div>
              ))}
              {skill.variables.length > 0 && (
                <div className="mt-2">
                  <p className="text-[11px] uppercase tracking-wide text-[var(--color-text-faint)]">Variables</p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {skill.variables.map((v) => (
                      <Badge key={v.name} variant="cyan">
                        {`{{${v.name}}}`}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </TabsContent>

          <TabsContent value="triggers">
            <div className="flex flex-col gap-1 pt-3">
              {skill.trigger
                .split("\n")
                .filter(Boolean)
                .map((t, i) => (
                  <p key={i} className="font-mono text-xs text-[var(--color-text)]">
                    {t}
                  </p>
                ))}
              {!skill.trigger && (
                <p className="text-xs text-[var(--color-text-faint)]">No trigger phrases set.</p>
              )}
            </div>
          </TabsContent>

          <TabsContent value="history">
            <div className="flex flex-col gap-2 pt-3">
              {versions.loading && <p className="text-xs text-[var(--color-text-muted)]">Loading…</p>}
              {versions.data?.length === 0 && (
                <p className="text-xs text-[var(--color-text-faint)]">No version history yet.</p>
              )}
              {versions.data?.map((v) => (
                <div key={v.version} className="flex items-center justify-between gap-2 border-b border-[var(--color-border)] pb-2 last:border-0">
                  <div className="min-w-0">
                    <p className="text-xs text-[var(--color-text)]">
                      v{v.version} · {v.change_note || "edited"}
                    </p>
                    <p className="font-mono text-[10px] text-[var(--color-text-faint)]">
                      {new Date(v.created_at).toLocaleString()}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={rollingBack === v.version || v.version === skill.version}
                    onClick={() => rollback(v.version)}
                  >
                    <History className="size-3.5" /> Restore
                  </Button>
                </div>
              ))}
            </div>
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  )
}

function LearnFromTextForm({ onLearned }: { onLearned: () => void }) {
  const toast = useToast()
  const [text, setText] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!text.trim()) return
    setSubmitting(true)
    try {
      const result = await api.skills.learnFromText(text.trim())
      if (result.created) {
        toast.push(`Learned "${result.skill?.name}"`, "success")
        setText("")
        onLearned()
      } else {
        toast.push(result.reason || "Couldn't extract concrete steps from that description", "error")
      }
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to learn skill", "error")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <DialogHeader>
        <DialogTitle>Learn a skill from text</DialogTitle>
        <DialogDescription>
          Describe a workflow in plain language — steps, the site it applies to, and what varies each time
          — and the agent will turn it into a replayable skill.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="skill-text">Description</Label>
        <Textarea
          id="skill-text"
          rows={6}
          placeholder='e.g. "To check the gas price, open etherscan.io/gastracker and read the standard gwei value."'
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </div>

      <DialogFooter>
        <Button type="submit" disabled={submitting}>
          {submitting ? "Learning…" : "Learn skill"}
        </Button>
      </DialogFooter>
    </form>
  )
}
