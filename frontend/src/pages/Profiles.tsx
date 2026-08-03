import { useMemo, useState, type FormEvent, type ReactNode } from "react"
import {
  Plus,
  Search,
  UserCircle,
  Trash2,
  Download,
  Upload,
  RefreshCw,
  Copy,
  Pencil,
  Mail,
  AtSign,
  MessageSquare,
} from "lucide-react"
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
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api, type ProfileMeta, type ProfileStatus } from "@/lib/api"

const STATUS_VARIANT: Record<ProfileStatus, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  ready: "green",
  in_use: "cyan",
  needs_login: "amber",
  disabled: "neutral",
  error: "red",
}

function SessionDot({ label, ok }: { label: string; ok: boolean | null }) {
  const color =
    ok === null ? "bg-[var(--color-text-faint)]" : ok ? "bg-[var(--color-signal-green)]" : "bg-[var(--color-signal-red)]"
  return (
    <span className="flex items-center gap-1 font-mono text-[10px] text-[var(--color-text-faint)]">
      <span className={`size-1.5 rounded-full ${color}`} />
      {label}
    </span>
  )
}

export function Profiles() {
  const profiles = useAsync(() => api.profiles.list(), [])
  const [search, setSearch] = useState("")
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const toast = useToast()

  const filtered = useMemo(() => {
    let list = profiles.data ?? []
    if (search.trim()) {
      const needle = search.trim().toLowerCase()
      list = list.filter(
        (p) =>
          p.name.toLowerCase().includes(needle) ||
          (p.gmail_account ?? "").toLowerCase().includes(needle) ||
          (p.x_account ?? "").toLowerCase().includes(needle) ||
          (p.discord_account ?? "").toLowerCase().includes(needle)
      )
    }
    return list
  }, [profiles.data, search])

  const selected = profiles.data?.find((p) => p.id === selectedId) ?? filtered[0] ?? null

  async function handleExport(id: string, name: string) {
    try {
      const meta = await api.profiles.export(id)
      const blob = new Blob([JSON.stringify(meta, null, 2)], { type: "application/json" })
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = `profile-${name}.json`
      a.click()
      URL.revokeObjectURL(url)
      toast.push("Exported profile metadata (no cookies or credentials included)", "success")
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Export failed", "error")
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-amber)]">
            Identity & Profile Manager
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Chrome Profiles</h1>
          <p className="mt-1 max-w-xl text-sm text-[var(--color-text-muted)]">
            Each profile is one complete online identity — a wallet, a Chrome profile directory, and its
            Gmail/X/Discord accounts. Cookies, local storage, and extensions live on disk in the profile's own
            Chrome directory and are never duplicated here.
          </p>
        </div>
        <div className="flex gap-2">
          <Dialog open={importOpen} onOpenChange={setImportOpen}>
            <DialogTrigger asChild>
              <Button variant="subtle">
                <Upload className="size-4" /> Import
              </Button>
            </DialogTrigger>
            <DialogContent>
              <ImportProfileForm
                onImported={() => {
                  setImportOpen(false)
                  profiles.refetch()
                }}
              />
            </DialogContent>
          </Dialog>
          <Dialog open={createOpen} onOpenChange={setCreateOpen}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="size-4" /> New profile
              </Button>
            </DialogTrigger>
            <DialogContent>
              <CreateProfileForm
                onCreated={() => {
                  setCreateOpen(false)
                  profiles.refetch()
                }}
              />
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <div className="grid grid-cols-[1fr_380px] gap-6">
        <div className="flex flex-col gap-4">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--color-text-faint)]" />
            <Input
              placeholder="Search name, Gmail, X, or Discord account…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8"
            />
          </div>

          <Card>
            <CardContent className="pt-5">
              {profiles.loading && <p className="text-sm text-[var(--color-text-muted)]">Loading profiles…</p>}
              {profiles.error && <p className="text-sm text-[var(--color-signal-red)]">{profiles.error}</p>}
              {!profiles.loading && filtered.length === 0 && (
                <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">
                  No profiles match. Create one to get started.
                </p>
              )}
              <div className="flex flex-col divide-y divide-[var(--color-border)]">
                {filtered.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => setSelectedId(p.id)}
                    className={`flex items-center justify-between gap-4 py-3 text-left transition-colors ${
                      selected?.id === p.id ? "opacity-100" : "opacity-80 hover:opacity-100"
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-medium text-[var(--color-text)]">{p.name}</p>
                        {p.is_active && <Badge variant="cyan">active</Badge>}
                      </div>
                      <p className="truncate font-mono text-xs text-[var(--color-text-faint)]">
                        {p.wallet_label ?? "no wallet linked"}
                      </p>
                      <div className="mt-1 flex flex-wrap gap-2">
                        <SessionDot label="gmail" ok={p.sessions.gmail} />
                        <SessionDot label="x" ok={p.sessions.x} />
                        <SessionDot label="discord" ok={p.sessions.discord} />
                      </div>
                    </div>
                    <div className="flex items-center gap-2 text-right">
                      <Badge variant={STATUS_VARIANT[p.status]}>{p.status.replace("_", " ")}</Badge>
                    </div>
                  </button>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>

        <div>
          {selected ? (
            <ProfileDetails profile={selected} onChanged={() => profiles.refetch()} onExport={handleExport} />
          ) : null}
        </div>
      </div>
    </div>
  )
}

function ProfileDetails({
  profile,
  onChanged,
  onExport,
}: {
  profile: ProfileMeta
  onChanged: () => void
  onExport: (id: string, name: string) => void
}) {
  const toast = useToast()
  const filesystem = useAsync(() => api.profiles.filesystem(profile.id), [profile.id])
  const activity = useAsync(() => api.profiles.activity(profile.id, 20), [profile.id])
  const [busy, setBusy] = useState(false)
  const [cloneOpen, setCloneOpen] = useState(false)
  const [renameOpen, setRenameOpen] = useState(false)

  async function handleSelectActive() {
    setBusy(true)
    try {
      await api.profiles.select(profile.id)
      toast.push(`${profile.name} is now the active profile`, "success")
      onChanged()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to select profile", "error")
    } finally {
      setBusy(false)
    }
  }

  async function handleToggleEnabled() {
    setBusy(true)
    try {
      if (profile.enabled) {
        await api.profiles.disable(profile.id)
        toast.push(`${profile.name} disabled`, "info")
      } else {
        await api.profiles.enable(profile.id)
        toast.push(`${profile.name} enabled`, "success")
      }
      onChanged()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to update profile", "error")
    } finally {
      setBusy(false)
    }
  }

  async function handleCheckSessions() {
    setBusy(true)
    try {
      await api.profiles.checkSessionsNow(profile.id)
      toast.push("Session check complete", "success")
      onChanged()
    } catch (err) {
      toast.push(
        err instanceof Error ? err.message : "Couldn't check sessions — run a task with this profile first",
        "error"
      )
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    if (!confirm(`Delete ${profile.name}? This removes its metadata and Chrome profile directory.`)) return
    setBusy(true)
    try {
      await api.profiles.remove(profile.id)
      toast.push("Profile deleted", "success")
      onChanged()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to delete profile", "error")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-5">
        <div className="flex items-center gap-2">
          <UserCircle className="size-4 text-[var(--color-signal-amber)]" />
          <p className="text-sm font-semibold text-[var(--color-text)]">{profile.name}</p>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={handleSelectActive} disabled={busy || profile.is_active || !profile.enabled}>
            {profile.is_active ? "Active profile" : "Set active"}
          </Button>
          <Button size="sm" variant="subtle" onClick={handleToggleEnabled} disabled={busy}>
            {profile.enabled ? "Disable" : "Enable"}
          </Button>
          <Button size="sm" variant="subtle" onClick={handleCheckSessions} disabled={busy}>
            <RefreshCw className="size-3.5" /> Check sessions
          </Button>
        </div>
        <div className="flex flex-wrap gap-2">
          <Dialog open={cloneOpen} onOpenChange={setCloneOpen}>
            <DialogTrigger asChild>
              <Button size="sm" variant="subtle">
                <Copy className="size-3.5" /> Clone
              </Button>
            </DialogTrigger>
            <DialogContent>
              <CloneRenameForm
                title="Clone profile"
                description="Copies this profile's metadata and its full Chrome profile directory — cookies, local storage, and extensions included — so the clone starts already signed in."
                confirmLabel="Clone"
                onSubmit={async (newName) => {
                  await api.profiles.clone(profile.id, newName)
                  toast.push("Profile cloned", "success")
                  setCloneOpen(false)
                  onChanged()
                }}
              />
            </DialogContent>
          </Dialog>
          <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
            <DialogTrigger asChild>
              <Button size="sm" variant="subtle">
                <Pencil className="size-3.5" /> Rename
              </Button>
            </DialogTrigger>
            <DialogContent>
              <CloneRenameForm
                title="Rename profile"
                description="Update this profile's display name. Nothing on disk changes."
                confirmLabel="Rename"
                initialValue={profile.name}
                onSubmit={async (newName) => {
                  await api.profiles.rename(profile.id, newName)
                  toast.push("Profile renamed", "success")
                  setRenameOpen(false)
                  onChanged()
                }}
              />
            </DialogContent>
          </Dialog>
          <Button size="sm" variant="subtle" onClick={() => onExport(profile.id, profile.name)}>
            <Download className="size-3.5" /> Export
          </Button>
          <Button size="sm" variant="danger" onClick={handleDelete} disabled={busy}>
            <Trash2 className="size-3.5" /> Delete
          </Button>
        </div>

        <Tabs defaultValue="status">
          <TabsList>
            <TabsTrigger value="status">Status</TabsTrigger>
            <TabsTrigger value="filesystem">Chrome profile</TabsTrigger>
            <TabsTrigger value="activity">Activity</TabsTrigger>
          </TabsList>

          <TabsContent value="status">
            <div className="flex flex-col gap-2 pt-3 text-sm">
              <Row label="Wallet" value={profile.wallet_label ?? "—"} />
              <Row
                label="Gmail"
                value={profile.gmail_account ?? "—"}
                icon={<Mail className="size-3" />}
                sessionOk={profile.sessions.gmail}
              />
              <Row
                label="X"
                value={profile.x_account ?? "—"}
                icon={<AtSign className="size-3" />}
                sessionOk={profile.sessions.x}
              />
              <Row
                label="Discord"
                value={profile.discord_account ?? "—"}
                icon={<MessageSquare className="size-3" />}
                sessionOk={profile.sessions.discord}
              />
              {profile.last_session_check_at && (
                <Row label="Last session check" value={new Date(profile.last_session_check_at).toLocaleString()} />
              )}
              {profile.last_used_at && <Row label="Last used" value={new Date(profile.last_used_at).toLocaleString()} />}
              {profile.tags.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {profile.tags.map((t) => (
                    <span
                      key={t}
                      className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-text-faint)]"
                    >
                      {t}
                    </span>
                  ))}
                </div>
              )}
              {profile.notes && <p className="pt-1 text-xs text-[var(--color-text-faint)]">{profile.notes}</p>}
            </div>
          </TabsContent>

          <TabsContent value="filesystem">
            <div className="flex flex-col gap-2 pt-3 text-sm">
              {filesystem.loading && <p className="text-xs text-[var(--color-text-muted)]">Loading…</p>}
              {filesystem.data && (
                <>
                  <Row label="Directory exists" value={filesystem.data.exists ? "yes" : "no"} />
                  <Row
                    label="Cookies"
                    value={
                      filesystem.data.cookies.present
                        ? `present (${filesystem.data.cookies.size_bytes ?? 0} bytes)`
                        : "not present"
                    }
                  />
                  <Row label="Local storage" value={filesystem.data.local_storage.present ? "present" : "not present"} />
                  <Row label="Session storage" value={filesystem.data.session_storage.present ? "present" : "not present"} />
                  <div className="flex items-center justify-between">
                    <span className="text-[var(--color-text-faint)]">Extensions</span>
                    <span className="font-mono text-xs text-[var(--color-text)]">
                      {filesystem.data.extensions.length ? filesystem.data.extensions.join(", ") : "none"}
                    </span>
                  </div>
                </>
              )}
            </div>
          </TabsContent>

          <TabsContent value="activity">
            <div className="flex flex-col gap-2 pt-3">
              {activity.loading && <p className="text-xs text-[var(--color-text-muted)]">Loading…</p>}
              {activity.data?.length === 0 && (
                <p className="text-xs text-[var(--color-text-faint)]">No activity recorded yet.</p>
              )}
              {activity.data?.map((a) => (
                <div key={a.id} className="border-b border-[var(--color-border)] pb-2 last:border-0">
                  <p className="text-xs text-[var(--color-text)]">{a.description}</p>
                  <p className="font-mono text-[10px] text-[var(--color-text-faint)]">
                    {a.event_type} · {new Date(a.created_at).toLocaleString()}
                  </p>
                </div>
              ))}
            </div>
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  )
}

function Row({
  label,
  value,
  icon,
  sessionOk,
}: {
  label: string
  value: string
  icon?: ReactNode
  sessionOk?: boolean | null
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="flex items-center gap-1 text-[var(--color-text-faint)]">
        {icon}
        {label}
      </span>
      <span className="flex items-center gap-1.5">
        <span className="font-mono text-xs text-[var(--color-text)]">{value}</span>
        {sessionOk !== undefined && sessionOk !== null && (
          <span
            className={`size-1.5 rounded-full ${sessionOk ? "bg-[var(--color-signal-green)]" : "bg-[var(--color-signal-red)]"}`}
          />
        )}
      </span>
    </div>
  )
}

function CreateProfileForm({ onCreated }: { onCreated: () => void }) {
  const toast = useToast()
  const [name, setName] = useState("")
  const [walletLabel, setWalletLabel] = useState("")
  const [gmail, setGmail] = useState("")
  const [x, setX] = useState("")
  const [discord, setDiscord] = useState("")
  const [tags, setTags] = useState("")
  const [notes, setNotes] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    setSubmitting(true)
    try {
      await api.profiles.create({
        name: name.trim(),
        wallet_label: walletLabel.trim() || undefined,
        gmail_account: gmail.trim() || undefined,
        x_account: x.trim() || undefined,
        discord_account: discord.trim() || undefined,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        notes: notes.trim() || undefined,
      })
      toast.push("Profile created", "success")
      onCreated()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to create profile", "error")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <DialogHeader>
        <DialogTitle>New profile</DialogTitle>
        <DialogDescription>
          Creates a fresh Chrome profile directory and metadata row. Gmail/X/Discord accounts are labels only —
          you'll sign in manually in that profile's browser session.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="p-name">Profile name</Label>
          <Input id="p-name" value={name} onChange={(e) => setName(e.target.value)} required />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="p-wallet">Wallet label</Label>
          <Input id="p-wallet" value={walletLabel} onChange={(e) => setWalletLabel(e.target.value)} placeholder="optional" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="p-gmail">Gmail account</Label>
          <Input id="p-gmail" value={gmail} onChange={(e) => setGmail(e.target.value)} placeholder="optional" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="p-x">X account</Label>
          <Input id="p-x" value={x} onChange={(e) => setX(e.target.value)} placeholder="optional" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="p-discord">Discord account</Label>
          <Input id="p-discord" value={discord} onChange={(e) => setDiscord(e.target.value)} placeholder="optional" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="p-tags">Tags (comma separated)</Label>
          <Input id="p-tags" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="daily, high-value" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="p-notes">Notes</Label>
          <Textarea id="p-notes" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="optional" />
        </div>
      </div>

      <DialogFooter>
        <Button type="submit" disabled={submitting}>
          {submitting ? "Creating…" : "Create profile"}
        </Button>
      </DialogFooter>
    </form>
  )
}

function ImportProfileForm({ onImported }: { onImported: () => void }) {
  const toast = useToast()
  const [json, setJson] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    try {
      const parsed = JSON.parse(json)
      if (!parsed.name) throw new Error("Import data must include a name")
      await api.profiles.import(parsed)
      toast.push("Profile imported", "success")
      onImported()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Import failed — check the JSON", "error")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <DialogHeader>
        <DialogTitle>Import profile</DialogTitle>
        <DialogDescription>
          Paste metadata exported from another profile (via Export). This creates a new profile row and Chrome
          profile directory — it does not carry over cookies or session state from the source.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="p-import-json">Profile JSON</Label>
        <Textarea
          id="p-import-json"
          value={json}
          onChange={(e) => setJson(e.target.value)}
          placeholder='{"name": "Profile-02", "wallet_label": "..."}'
          rows={8}
          required
        />
      </div>

      <DialogFooter>
        <Button type="submit" disabled={submitting}>
          {submitting ? "Importing…" : "Import"}
        </Button>
      </DialogFooter>
    </form>
  )
}

function CloneRenameForm({
  title,
  description,
  confirmLabel,
  initialValue = "",
  onSubmit,
}: {
  title: string
  description: string
  confirmLabel: string
  initialValue?: string
  onSubmit: (newName: string) => Promise<void>
}) {
  const toast = useToast()
  const [newName, setNewName] = useState(initialValue)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!newName.trim()) return
    setSubmitting(true)
    try {
      await onSubmit(newName.trim())
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "error")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="p-new-name">New name</Label>
        <Input id="p-new-name" value={newName} onChange={(e) => setNewName(e.target.value)} required />
      </div>

      <DialogFooter>
        <Button type="submit" disabled={submitting}>
          {submitting ? "Working…" : confirmLabel}
        </Button>
      </DialogFooter>
    </form>
  )
}
