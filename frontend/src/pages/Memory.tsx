import { useMemo, useState, type FormEvent } from "react"
import {
  Archive,
  ArchiveRestore,
  BrainCircuit,
  Copy,
  Layers,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api, type MemoryCategory, type MemoryEntryRecord, type MemoryResult } from "@/lib/api"

const CATEGORY_LABEL: Record<MemoryCategory, string> = {
  conversation: "Conversation",
  skills: "Skills",
  browser: "Browser",
  coding: "Coding",
  profiles: "Profiles",
  tasks: "Tasks",
  general: "General",
}

const CATEGORY_VARIANT: Record<MemoryCategory, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  conversation: "cyan",
  skills: "violet",
  browser: "amber",
  coding: "green",
  profiles: "neutral",
  tasks: "red",
  general: "neutral",
}

function importanceVariant(score: number): "neutral" | "amber" | "cyan" | "green" | "red" | "violet" {
  if (score >= 0.7) return "green"
  if (score >= 0.4) return "amber"
  return "neutral"
}

function formatDate(iso: string | null) {
  if (!iso) return "—"
  return new Date(iso).toLocaleString()
}

export function Memory() {
  const toast = useToast()

  // ---- Search tab (original behavior, unchanged) ----
  const [query, setQuery] = useState("")
  const [results, setResults] = useState<MemoryResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searched, setSearched] = useState(false)

  async function handleSearch(e: FormEvent) {
    e.preventDefault()
    if (!query.trim()) return
    setSearching(true)
    try {
      const res = await api.memory.search(query.trim())
      setResults(res.results)
      setSearched(true)
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Memory search failed", "error")
    } finally {
      setSearching(false)
    }
  }

  // ---- Browse tab (categories + importance ranking + archive/forget) ----
  const [category, setCategory] = useState<string>("all")
  const [sort, setSort] = useState<"importance" | "recent" | "access">("importance")
  const [includeArchived, setIncludeArchived] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)

  const memories = useAsync(
    () =>
      api.memory.list({
        category: category === "all" ? undefined : category,
        sort,
        include_archived: includeArchived,
      }),
    [category, sort, includeArchived]
  )
  const memoryList = memories.data?.memories ?? []

  const analytics = useAsync(() => api.memory.analytics(), [])
  const duplicates = useAsync(() => api.memory.duplicates(), [])

  async function refreshAll() {
    await Promise.all([memories.refetch(), analytics.refetch(), duplicates.refetch()])
  }

  async function archive(entry: MemoryEntryRecord) {
    setBusyId(entry.id)
    try {
      await api.memory.archive(entry.id)
      toast.push("Memory archived", "success")
      await refreshAll()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to archive memory", "error")
    } finally {
      setBusyId(null)
    }
  }

  async function unarchive(entry: MemoryEntryRecord) {
    setBusyId(entry.id)
    try {
      await api.memory.unarchive(entry.id)
      toast.push("Memory restored", "success")
      await refreshAll()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to restore memory", "error")
    } finally {
      setBusyId(null)
    }
  }

  async function forget(entry: MemoryEntryRecord) {
    if (!confirm("Permanently forget this memory? This can't be undone.")) return
    setBusyId(entry.id)
    try {
      await api.memory.forget(entry.id)
      toast.push("Memory forgotten", "success")
      await refreshAll()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to forget memory", "error")
    } finally {
      setBusyId(null)
    }
  }

  async function mergeGroup(group: MemoryEntryRecord[]) {
    const keepId = group[0].id
    setBusyId(keepId)
    try {
      const res = await api.memory.mergeDuplicates(
        group.map((g) => g.id),
        keepId
      )
      toast.push(`Merged ${res.removed_ids.length} duplicate(s)`, "success")
      await refreshAll()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to merge duplicates", "error")
    } finally {
      setBusyId(null)
    }
  }

  async function runExpiration() {
    try {
      const res = await api.memory.runExpiration()
      toast.push(`Expiration sweep: archived ${res.archived}, forgot ${res.forgotten}`, "success")
      await refreshAll()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Expiration sweep failed", "error")
    }
  }

  const stats = analytics.data
  const categoryEntries = useMemo(
    () => Object.entries(stats?.by_category ?? {}) as [MemoryCategory, number][],
    [stats]
  )

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-violet)]">Recall</p>
        <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Memory</h1>
        <p className="mt-1 text-sm text-[var(--color-text-muted)]">
          Semantic search over past workflows, plus categorized browsing, importance ranking, duplicate
          cleanup, and analytics for everything the agent has remembered.
        </p>
      </div>

      <Tabs defaultValue="search">
        <TabsList>
          <TabsTrigger value="search">Search</TabsTrigger>
          <TabsTrigger value="browse">Browse</TabsTrigger>
          <TabsTrigger value="duplicates">Duplicates</TabsTrigger>
          <TabsTrigger value="analytics">Analytics</TabsTrigger>
        </TabsList>

        {/* ---------------- Search (unchanged) ---------------- */}
        <TabsContent value="search" className="mt-4">
          <form onSubmit={handleSearch} className="flex gap-2">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[var(--color-text-faint)]" />
              <Input
                className="pl-9"
                placeholder="Describe a goal, e.g. 'claim daily reward on a DeFi site'"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={searching}>
              {searching ? "Searching…" : "Search"}
            </Button>
          </form>

          <Card className="mt-4">
            <CardContent className="pt-5">
              {!searched && (
                <div className="flex flex-col items-center gap-2 py-10 text-center">
                  <BrainCircuit className="size-6 text-[var(--color-text-faint)]" />
                  <p className="text-sm text-[var(--color-text-faint)]">
                    Search recalls similar workflows the agent has run before, ranked by similarity.
                  </p>
                </div>
              )}

              {searched && results?.length === 0 && (
                <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">No matching memories found.</p>
              )}

              <div className="flex flex-col gap-3">
                {results?.map((r, i) => (
                  <div key={i} className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-4">
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs text-[var(--color-text-muted)]">
                      {JSON.stringify(r, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* ---------------- Browse (categories + importance + archive/forget) ---------------- */}
        <TabsContent value="browse" className="mt-4 flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-2">
            <Select value={category} onValueChange={setCategory}>
              <SelectTrigger className="w-44">
                <SelectValue placeholder="Category" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All categories</SelectItem>
                {(Object.keys(CATEGORY_LABEL) as MemoryCategory[]).map((c) => (
                  <SelectItem key={c} value={c}>
                    {CATEGORY_LABEL[c]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select value={sort} onValueChange={(v) => setSort(v as typeof sort)}>
              <SelectTrigger className="w-44">
                <SelectValue placeholder="Sort" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="importance">Most important</SelectItem>
                <SelectItem value="recent">Most recent</SelectItem>
                <SelectItem value="access">Most recalled</SelectItem>
              </SelectContent>
            </Select>

            <Button
              variant={includeArchived ? "default" : "subtle"}
              size="sm"
              onClick={() => setIncludeArchived((v) => !v)}
            >
              <Archive className="size-3.5" /> {includeArchived ? "Showing archived" : "Show archived"}
            </Button>

            <Button variant="ghost" size="sm" onClick={runExpiration} className="ml-auto">
              <Sparkles className="size-3.5" /> Run expiration sweep
            </Button>
          </div>

          <Card>
            <CardContent className="flex flex-col divide-y divide-[var(--color-border)] pt-5">
              {memories.loading && (
                <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">Loading…</p>
              )}
              {!memories.loading && memoryList.length === 0 && (
                <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">No memories found.</p>
              )}
              {memoryList.map((m) => (
                <div key={m.id} className="flex items-start justify-between gap-4 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="mb-1 flex flex-wrap items-center gap-1.5">
                      <Badge variant={CATEGORY_VARIANT[m.category] ?? "neutral"}>
                        {CATEGORY_LABEL[m.category] ?? m.category}
                      </Badge>
                      <Badge variant="neutral">{m.kind}</Badge>
                      <Badge variant={importanceVariant(m.effective_importance)}>
                        importance {m.effective_importance.toFixed(2)}
                      </Badge>
                      {m.access_count > 0 && <Badge variant="neutral">recalled ×{m.access_count}</Badge>}
                      {m.merged_count > 0 && <Badge variant="cyan">merged ×{m.merged_count}</Badge>}
                      {m.archived && <Badge variant="red">archived</Badge>}
                    </div>
                    <p className="truncate text-sm text-[var(--color-text)]" title={m.content}>
                      {m.content}
                    </p>
                    <p className="mt-0.5 text-xs text-[var(--color-text-faint)]">
                      created {formatDate(m.created_at)}
                      {m.last_accessed_at ? ` · last used ${formatDate(m.last_accessed_at)}` : ""}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {m.archived ? (
                      <Button size="sm" variant="subtle" disabled={busyId === m.id} onClick={() => unarchive(m)}>
                        <ArchiveRestore className="size-3.5" /> Restore
                      </Button>
                    ) : (
                      <Button size="sm" variant="ghost" disabled={busyId === m.id} onClick={() => archive(m)}>
                        <Archive className="size-3.5" /> Archive
                      </Button>
                    )}
                    <Button size="sm" variant="ghost" disabled={busyId === m.id} onClick={() => forget(m)}>
                      <Trash2 className="size-3.5 text-[var(--color-signal-red)]" /> Forget
                    </Button>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ---------------- Duplicates ---------------- */}
        <TabsContent value="duplicates" className="mt-4">
          <Card>
            <CardContent className="flex flex-col gap-4 pt-5">
              {duplicates.loading && (
                <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">Scanning…</p>
              )}
              {!duplicates.loading && (duplicates.data?.groups.length ?? 0) === 0 && (
                <div className="flex flex-col items-center gap-2 py-10 text-center">
                  <Layers className="size-6 text-[var(--color-text-faint)]" />
                  <p className="text-sm text-[var(--color-text-faint)]">No duplicate memories detected.</p>
                </div>
              )}
              {duplicates.data?.groups.map((group, i) => (
                <div key={i} className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-4">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-text-muted)]">
                      <Copy className="size-3.5" /> {group.length} similar memories
                    </p>
                    <Button size="sm" variant="subtle" disabled={busyId !== null} onClick={() => mergeGroup(group)}>
                      Merge into best
                    </Button>
                  </div>
                  <div className="flex flex-col gap-2">
                    {group.map((m) => (
                      <p key={m.id} className="truncate text-sm text-[var(--color-text-muted)]" title={m.content}>
                        {m.content}
                      </p>
                    ))}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ---------------- Analytics ---------------- */}
        <TabsContent value="analytics" className="mt-4 flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard label="Total memories" value={stats?.total ?? "—"} />
            <StatCard label="Active" value={stats?.active ?? "—"} />
            <StatCard label="Archived" value={stats?.archived ?? "—"} />
            <StatCard label="Avg. importance" value={stats ? stats.average_importance.toFixed(2) : "—"} />
            <StatCard label="Expiring soon" value={stats?.expiring_soon ?? "—"} />
            <StatCard label="Duplicate groups" value={stats?.duplicate_group_count ?? "—"} />
            <StatCard label="Duplicate entries" value={stats?.duplicate_entry_count ?? "—"} />
          </div>

          <Card>
            <CardContent className="pt-5">
              <p className="mb-3 text-sm font-semibold text-[var(--color-text)]">By category</p>
              <div className="flex flex-wrap gap-2">
                {categoryEntries.length === 0 && (
                  <p className="text-sm text-[var(--color-text-faint)]">No data yet.</p>
                )}
                {categoryEntries.map(([cat, count]) => (
                  <Badge key={cat} variant={CATEGORY_VARIANT[cat] ?? "neutral"}>
                    {CATEGORY_LABEL[cat] ?? cat}: {count}
                  </Badge>
                ))}
              </div>
            </CardContent>
          </Card>

          <div className="grid gap-3 sm:grid-cols-2">
            <Card>
              <CardContent className="pt-5">
                <p className="mb-3 text-sm font-semibold text-[var(--color-text)]">Most recalled</p>
                <div className="flex flex-col gap-2">
                  {(stats?.top_recalled ?? []).length === 0 && (
                    <p className="text-sm text-[var(--color-text-faint)]">No recalls yet.</p>
                  )}
                  {stats?.top_recalled.map((m) => (
                    <div key={m.id} className="flex items-center justify-between gap-2">
                      <p className="truncate text-sm text-[var(--color-text-muted)]" title={m.content}>
                        {m.content}
                      </p>
                      <Badge variant="neutral">×{m.access_count}</Badge>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardContent className="pt-5">
                <p className="mb-3 text-sm font-semibold text-[var(--color-text)]">Most important</p>
                <div className="flex flex-col gap-2">
                  {(stats?.most_important ?? []).length === 0 && (
                    <p className="text-sm text-[var(--color-text-faint)]">No data yet.</p>
                  )}
                  {stats?.most_important.map((m) => (
                    <div key={m.id} className="flex items-center justify-between gap-2">
                      <p className="truncate text-sm text-[var(--color-text-muted)]" title={m.content}>
                        {m.content}
                      </p>
                      <Badge variant={importanceVariant(m.effective_importance)}>
                        {m.effective_importance.toFixed(2)}
                      </Badge>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  )
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <Card>
      <CardContent className="pt-5">
        <p className="text-xs uppercase tracking-wide text-[var(--color-text-faint)]">{label}</p>
        <p className="mt-1 text-xl font-semibold text-[var(--color-text)]">{value}</p>
      </CardContent>
    </Card>
  )
}
