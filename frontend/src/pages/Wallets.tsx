import { useMemo, useState, type FormEvent } from "react"
import { Plus, Search, Wallet2, Trash2, Download, RefreshCw, ShieldCheck, Zap, Send } from "lucide-react"
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
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { useAsync } from "@/hooks/use-async"
import { useToast } from "@/components/toast-provider"
import { api, type WalletMeta, type WalletStatus, type ImportMethod } from "@/lib/api"

const STATUS_VARIANT: Record<WalletStatus, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  active: "green",
  inactive: "neutral",
  locked: "amber",
  unknown: "neutral",
}

export function Wallets() {
  const wallets = useAsync(() => api.wallets.listMeta(), [])
  const groups = useAsync(() => api.wallets.groups.list(), [])
  const [search, setSearch] = useState("")
  const [groupFilter, setGroupFilter] = useState<string>("")
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const toast = useToast()

  const filtered = useMemo(() => {
    let list = wallets.data ?? []
    if (search.trim()) {
      const needle = search.trim().toLowerCase()
      list = list.filter((w) => w.label.toLowerCase().includes(needle) || (w.address ?? "").toLowerCase().includes(needle))
    }
    if (groupFilter) list = list.filter((w) => w.group_id === groupFilter)
    return list
  }, [wallets.data, search, groupFilter])

  const selected = wallets.data?.find((w) => w.id === selectedId) ?? filtered[0] ?? null

  async function handleExport() {
    try {
      const meta = await api.wallets.exportMeta()
      const blob = new Blob([JSON.stringify(meta, null, 2)], { type: "application/json" })
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = "wallets-metadata.json"
      a.click()
      URL.revokeObjectURL(url)
      toast.push("Exported wallet metadata (no secrets included)", "success")
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Export failed", "error")
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-amber)]">
            Multi Wallet Manager
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Wallets</h1>
          <p className="mt-1 max-w-xl text-sm text-[var(--color-text-muted)]">
            Metadata for your wallets — labels, addresses, networks, tags. Seed phrases and private keys are
            used once to derive an address and are never stored here; signing always happens in your wallet
            extension.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="subtle" onClick={handleExport}>
            <Download className="size-4" /> Export metadata
          </Button>
          <Dialog open={importOpen} onOpenChange={setImportOpen}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="size-4" /> Import wallet
              </Button>
            </DialogTrigger>
            <DialogContent>
              <ImportWalletForm
                groups={groups.data ?? []}
                onImported={() => {
                  setImportOpen(false)
                  wallets.refetch()
                }}
              />
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <div className="grid grid-cols-[1fr_360px] gap-6">
        <div className="flex flex-col gap-4">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--color-text-faint)]" />
              <Input
                placeholder="Search label or address…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8"
              />
            </div>
            <Select value={groupFilter} onValueChange={setGroupFilter}>
              <SelectTrigger className="w-48">
                <SelectValue placeholder="All groups" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="">All groups</SelectItem>
                {groups.data?.map((g) => (
                  <SelectItem key={g.id} value={g.id}>
                    {g.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <Card>
            <CardContent className="pt-5">
              {wallets.loading && <p className="text-sm text-[var(--color-text-muted)]">Loading wallets…</p>}
              {wallets.error && <p className="text-sm text-[var(--color-signal-red)]">{wallets.error}</p>}
              {!wallets.loading && filtered.length === 0 && (
                <p className="py-6 text-center text-sm text-[var(--color-text-faint)]">
                  No wallets match. Import one to get started.
                </p>
              )}
              <div className="flex flex-col divide-y divide-[var(--color-border)]">
                {filtered.map((w) => (
                  <button
                    key={w.id}
                    onClick={() => setSelectedId(w.id)}
                    className={`flex items-center justify-between gap-4 py-3 text-left transition-colors ${
                      selected?.id === w.id ? "opacity-100" : "opacity-80 hover:opacity-100"
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="truncate text-sm font-medium text-[var(--color-text)]">{w.label}</p>
                        {w.is_active && <Badge variant="cyan">active</Badge>}
                      </div>
                      <p className="truncate font-mono text-xs text-[var(--color-text-faint)]">
                        {w.address ?? "no address on file"}
                      </p>
                      {w.tags.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {w.tags.map((t) => (
                            <span
                              key={t}
                              className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-text-faint)]"
                            >
                              {t}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-right">
                      <span className="font-mono text-xs text-[var(--color-text-faint)]">{w.network ?? "—"}</span>
                      <Badge variant={STATUS_VARIANT[w.status]}>{w.status}</Badge>
                    </div>
                  </button>
                ))}
              </div>
            </CardContent>
          </Card>

          <HotSignerCard />
        </div>

        <div>{selected ? <WalletDetails wallet={selected} onChanged={() => wallets.refetch()} /> : null}</div>
      </div>
    </div>
  )
}

function WalletDetails({ wallet, onChanged }: { wallet: WalletMeta; onChanged: () => void }) {
  const toast = useToast()
  const status = useAsync(() => api.wallets.status(wallet.id), [wallet.id])
  const balance = useAsync(
    () => (wallet.address && wallet.network ? api.wallets.balance(wallet.id) : Promise.resolve(null)),
    [wallet.id]
  )
  const activity = useAsync(() => api.wallets.activity(wallet.id, 20), [wallet.id])
  const [busy, setBusy] = useState(false)

  async function handleSelectActive() {
    setBusy(true)
    try {
      await api.wallets.selectActive(wallet.id)
      toast.push(`${wallet.label} is now the active wallet`, "success")
      onChanged()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to select wallet", "error")
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    if (!confirm(`Remove ${wallet.label} from Nexus-Agent? This only deletes local metadata.`)) return
    setBusy(true)
    try {
      await api.wallets.remove(wallet.id)
      toast.push("Wallet removed", "success")
      onChanged()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to remove wallet", "error")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-5">
        <div className="flex items-center gap-2">
          <Wallet2 className="size-4 text-[var(--color-signal-amber)]" />
          <p className="text-sm font-semibold text-[var(--color-text)]">{wallet.label}</p>
        </div>
        <p className="break-all font-mono text-xs text-[var(--color-text-faint)]">{wallet.address ?? "—"}</p>

        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={handleSelectActive} disabled={busy || wallet.is_active}>
            {wallet.is_active ? "Active wallet" : "Set active"}
          </Button>
          <Button size="sm" variant="subtle" onClick={() => status.refetch()}>
            <RefreshCw className="size-3.5" /> Refresh status
          </Button>
          <Button size="sm" variant="danger" onClick={handleDelete} disabled={busy}>
            <Trash2 className="size-3.5" /> Delete
          </Button>
        </div>

        <Tabs defaultValue="status">
          <TabsList>
            <TabsTrigger value="status">Status</TabsTrigger>
            <TabsTrigger value="activity">Activity</TabsTrigger>
          </TabsList>

          <TabsContent value="status">
            <div className="flex flex-col gap-2 pt-3 text-sm">
              <Row label="Network" value={wallet.network ?? "—"} />
              <Row label="Type" value={wallet.wallet_type} />
              <Row
                label="Connected"
                value={
                  status.data?.live.connected === null || status.data?.live.connected === undefined
                    ? "unknown (no active browser session)"
                    : status.data.live.connected
                      ? "yes"
                      : "no"
                }
              />
              <Row
                label="Balance"
                value={
                  balance.loading
                    ? "loading…"
                    : balance.data
                      ? `${balance.data.native.toFixed(4)} native`
                      : "unavailable"
                }
              />
              {wallet.last_used_at && <Row label="Last used" value={new Date(wallet.last_used_at).toLocaleString()} />}
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

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[var(--color-text-faint)]">{label}</span>
      <span className="font-mono text-xs text-[var(--color-text)]">{value}</span>
    </div>
  )
}

const CHAINS = ["ethereum", "polygon", "arbitrum", "optimism", "base", "bsc"]

function HotSignerCard() {
  const status = useAsync(() => api.wallets.hotSigner.status(), [])
  const toast = useToast()
  const [chain, setChain] = useState("base")
  const [toAddress, setToAddress] = useState("")
  const [amount, setAmount] = useState("")
  const [sending, setSending] = useState(false)
  const [lastTx, setLastTx] = useState<string | null>(null)

  const enabled = status.data?.enabled ?? false

  async function handleSend(e: FormEvent) {
    e.preventDefault()
    const amountNum = parseFloat(amount)
    if (!toAddress.trim() || !amountNum || amountNum <= 0) return
    if (
      !confirm(
        `Send ${amountNum} native token on ${chain} to ${toAddress.trim()}? This broadcasts immediately — no approval popup.`
      )
    )
      return
    setSending(true)
    setLastTx(null)
    try {
      const result = await api.wallets.hotSigner.send({ chain, to_address: toAddress.trim(), amount: amountNum })
      setLastTx(result.tx_hash)
      toast.push(`Sent — tx ${result.tx_hash.slice(0, 10)}…`, "success")
      setToAddress("")
      setAmount("")
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Send failed", "error")
    } finally {
      setSending(false)
    }
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Zap className="size-4 text-[var(--color-signal-amber)]" />
            <p className="text-sm font-semibold text-[var(--color-text)]">Hot Signer</p>
          </div>
          <Badge variant={enabled ? "green" : "neutral"}>{enabled ? "enabled" : "disabled"}</Badge>
        </div>

        <p className="text-xs text-[var(--color-text-faint)]">
          Direct RPC native-token transfer — signs and broadcasts immediately, no browser-extension approval popup.
          Separate from the wallet flow above; only ever point this at a burner/bot wallet.
        </p>

        {status.loading && <p className="text-xs text-[var(--color-text-muted)]">Checking status…</p>}

        {!status.loading && !enabled && (
          <p className="rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-xs text-[var(--color-text-faint)]">
            Disabled. Set <code>HOT_SIGNER_ENABLED=true</code> and <code>HOT_SIGNER_PRIVATE_KEY</code> in the
            backend&apos;s environment to turn this on.
          </p>
        )}

        {enabled && status.data?.address && (
          <p className="break-all font-mono text-xs text-[var(--color-text-faint)]">
            signer: {status.data.address}
            {status.data.max_native_value ? ` · cap: ${status.data.max_native_value}/tx` : ""}
          </p>
        )}

        <form onSubmit={handleSend} className="flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="hs-chain">Chain</Label>
              <Select value={chain} onValueChange={setChain}>
                <SelectTrigger id="hs-chain" disabled={!enabled}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CHAINS.map((c) => (
                    <SelectItem key={c} value={c}>
                      {c}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="hs-amount">Amount</Label>
              <Input
                id="hs-amount"
                type="number"
                step="any"
                min="0"
                placeholder="0.05"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                disabled={!enabled}
              />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="hs-to">Destination address</Label>
            <Input
              id="hs-to"
              placeholder="0x…"
              value={toAddress}
              onChange={(e) => setToAddress(e.target.value)}
              disabled={!enabled}
            />
          </div>
          <Button type="submit" size="sm" disabled={!enabled || sending}>
            <Send className="size-3.5" /> {sending ? "Sending…" : "Send"}
          </Button>
        </form>

        {lastTx && <p className="break-all font-mono text-[10px] text-[var(--color-text-faint)]">tx: {lastTx}</p>}
      </CardContent>
    </Card>
  )
}

function ImportWalletForm({
  groups,
  onImported,
}: {
  groups: { id: string; name: string }[]
  onImported: () => void
}) {
  const toast = useToast()
  const [method, setMethod] = useState<ImportMethod>("private_key")
  const [label, setLabel] = useState("")
  const [privateKey, setPrivateKey] = useState("")
  const [seedPhrase, setSeedPhrase] = useState("")
  const [network, setNetwork] = useState("")
  const [tags, setTags] = useState("")
  const [groupId, setGroupId] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!label.trim()) return
    setSubmitting(true)
    try {
      await api.wallets.import({
        label: label.trim(),
        method,
        private_key: privateKey.trim() || undefined,
        seed_phrase: seedPhrase.trim() || undefined,
        network: network || undefined,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        group_id: groupId || undefined,
      })
      toast.push("Wallet imported", "success")
      setLabel("")
      setPrivateKey("")
      setSeedPhrase("")
      onImported()
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Import failed", "error")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <DialogHeader>
        <DialogTitle>Import wallet</DialogTitle>
        <DialogDescription>
          A seed phrase or private key is used once, in memory, to derive the address — it is never written to
          disk. Actual transaction signing always happens in your own MetaMask/Rabby extension.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="label">Wallet name</Label>
          <Input id="label" value={label} onChange={(e) => setLabel(e.target.value)} required />
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="method">Import using</Label>
          <Select value={method} onValueChange={(v) => setMethod(v as ImportMethod)}>
            <SelectTrigger id="method">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="private_key">Private key</SelectItem>
              <SelectItem value="seed_phrase">Seed phrase</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {method === "private_key" && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pk">Private key</Label>
            <Input
              id="pk"
              type="password"
              placeholder="0x…"
              value={privateKey}
              onChange={(e) => setPrivateKey(e.target.value)}
            />
            <p className="flex items-center gap-1 text-xs text-[var(--color-text-faint)]">
              <ShieldCheck className="size-3" /> Used once to derive the address, then discarded — never stored.
            </p>
          </div>
        )}

        {method === "seed_phrase" && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="seed">Seed phrase</Label>
            <Textarea id="seed" placeholder="12 or 24 words" value={seedPhrase} onChange={(e) => setSeedPhrase(e.target.value)} />
            <p className="flex items-center gap-1 text-xs text-[var(--color-text-faint)]">
              <ShieldCheck className="size-3" /> Used once to derive the address, then discarded — never stored.
            </p>
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="network">Network</Label>
          <Select value={network} onValueChange={setNetwork}>
            <SelectTrigger id="network">
              <SelectValue placeholder="Unspecified" />
            </SelectTrigger>
            <SelectContent>
              {["ethereum", "polygon", "arbitrum", "optimism", "base", "bsc"].map((n) => (
                <SelectItem key={n} value={n}>
                  {n}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tags">Tags (comma separated)</Label>
          <Input id="tags" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="daily, high-value" />
        </div>

        {groups.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="group">Group</Label>
            <Select value={groupId} onValueChange={setGroupId}>
              <SelectTrigger id="group">
                <SelectValue placeholder="None" />
              </SelectTrigger>
              <SelectContent>
                {groups.map((g) => (
                  <SelectItem key={g.id} value={g.id}>
                    {g.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
      </div>

      <DialogFooter>
        <Button type="submit" disabled={submitting}>
          {submitting ? "Importing…" : "Import"}
        </Button>
      </DialogFooter>
    </form>
  )
}
