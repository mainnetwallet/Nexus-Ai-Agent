import { useState, type FormEvent } from "react"
import { Plus, Globe, Wallet2, Pause, Play, X, RotateCcw } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
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
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api, type TaskStatus, type TaskSummary } from "@/lib/api"

const STATUS_VARIANT: Record<TaskStatus, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  queued: "neutral",
  planning: "violet",
  running: "cyan",
  paused: "amber",
  succeeded: "green",
  failed: "red",
  cancelled: "neutral",
}

export function Tasks() {
  const tasks = useAsync(() => api.tasks.list(), [])
  const wallets = useAsync(() => api.wallets.list(), [])
  const queueStatus = useAsync(() => api.tasks.queueStatus(), [])
  const [open, setOpen] = useState(false)
  const toast = useToast()

  async function toggleQueue() {
    try {
      if (queueStatus.data?.worker_paused) {
        await api.tasks.resumeQueue()
        toast.push("Queue resumed", "success")
      } else {
        await api.tasks.pauseQueue()
        toast.push("Queue paused", "success")
      }
      queueStatus.refetch()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to update queue", "error")
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-amber)]">Queue</p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Tasks</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Point the agent at a website and a goal. It plans, acts, and verifies on its own.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={toggleQueue}>
            {queueStatus.data?.worker_paused ? (
              <>
                <Play className="size-4" /> Resume queue
              </>
            ) : (
              <>
                <Pause className="size-4" /> Pause queue
              </>
            )}
          </Button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="size-4" /> New task
              </Button>
            </DialogTrigger>
            <DialogContent>
              <NewTaskForm
                walletOptions={wallets.data ?? []}
                onCreated={() => {
                  setOpen(false)
                  tasks.refetch()
                }}
              />
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <Card>
        <CardContent className="pt-5">
          {tasks.loading && <p className="text-sm text-[var(--color-text-muted)]">Loading tasks…</p>}
          {tasks.error && <p className="text-sm text-[var(--color-signal-red)]">{tasks.error}</p>}
          {!tasks.loading && tasks.data?.length === 0 && (
            <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">
              No tasks yet. Create one to get the agent moving.
            </p>
          )}

          <div className="flex flex-col divide-y divide-[var(--color-border)]">
            {tasks.data?.map((t) => (
              <TaskRow key={t.id} task={t} onChanged={() => { tasks.refetch(); queueStatus.refetch() }} />
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function TaskRow({ task: t, onChanged }: { task: TaskSummary; onChanged: () => void }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)

  async function run(action: () => Promise<{ error?: string }>) {
    setBusy(true)
    try {
      const result = await action()
      if (result.error) {
        toast.push(result.error, "error")
      } else {
        onChanged()
      }
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Action failed", "error")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-[var(--color-text)]">{t.goal}</p>
        <div className="mt-1 flex items-center gap-3">
          <span className="flex items-center gap-1 truncate font-mono text-xs text-[var(--color-text-faint)]">
            <Globe className="size-3" /> {t.website}
          </span>
          {t.wallet_label && (
            <span className="flex items-center gap-1 font-mono text-xs text-[var(--color-text-faint)]">
              <Wallet2 className="size-3" /> {t.wallet_label}
            </span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-3 text-right">
        <span className="font-mono text-xs text-[var(--color-text-faint)]">
          {new Date(t.created_at).toLocaleString()}
        </span>
        <Badge variant={STATUS_VARIANT[t.status]}>{t.status}</Badge>
        <div className="flex items-center gap-1">
          {t.status === "running" && (
            <Button variant="ghost" size="icon" disabled={busy} title="Pause" onClick={() => run(() => api.tasks.pause(t.id))}>
              <Pause className="size-4" />
            </Button>
          )}
          {t.status === "paused" && (
            <Button variant="ghost" size="icon" disabled={busy} title="Resume" onClick={() => run(() => api.tasks.resume(t.id))}>
              <Play className="size-4" />
            </Button>
          )}
          {(t.status === "running" || t.status === "planning" || t.status === "paused" || t.status === "queued") && (
            <Button variant="ghost" size="icon" disabled={busy} title="Cancel" onClick={() => run(() => api.tasks.cancel(t.id))}>
              <X className="size-4" />
            </Button>
          )}
          {(t.status === "failed" || t.status === "cancelled") && (
            <Button variant="ghost" size="icon" disabled={busy} title="Retry" onClick={() => run(() => api.tasks.retry(t.id))}>
              <RotateCcw className="size-4" />
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}

function NewTaskForm({
  walletOptions,
  onCreated,
}: {
  walletOptions: { id: string; label: string }[]
  onCreated: () => void
}) {
  const toast = useToast()
  const [website, setWebsite] = useState("")
  const [goal, setGoal] = useState("")
  const [walletLabel, setWalletLabel] = useState<string>("")
  const [notes, setNotes] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!website.trim() || !goal.trim()) return
    setSubmitting(true)
    try {
      await api.tasks.create({
        website: website.trim(),
        goal: goal.trim(),
        wallet_label: walletLabel || undefined,
        notes: notes.trim(),
      })
      toast.push("Task queued", "success")
      setWebsite("")
      setGoal("")
      setWalletLabel("")
      setNotes("")
      onCreated()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to create task", "error")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <DialogHeader>
        <DialogTitle>New task</DialogTitle>
        <DialogDescription>The planner reasons only from what's visible on the page — no site-specific logic.</DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="website">Website</Label>
          <Input
            id="website"
            placeholder="https://example.com"
            value={website}
            onChange={(e) => setWebsite(e.target.value)}
            required
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="goal">Goal</Label>
          <Textarea
            id="goal"
            placeholder="e.g. Claim the daily reward and log the resulting balance"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            required
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wallet">Wallet label (optional)</Label>
          <Select value={walletLabel} onValueChange={setWalletLabel}>
            <SelectTrigger id="wallet">
              <SelectValue placeholder="None" />
            </SelectTrigger>
            <SelectContent>
              {walletOptions.map((w) => (
                <SelectItem key={w.id} value={w.label}>
                  {w.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="notes">Notes (optional)</Label>
          <Textarea id="notes" placeholder="Anything the agent should know" value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>
      </div>

      <DialogFooter>
        <Button type="submit" disabled={submitting}>
          {submitting ? "Queuing…" : "Queue task"}
        </Button>
      </DialogFooter>
    </form>
  )
}
