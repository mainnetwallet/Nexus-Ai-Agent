import { useState, type FormEvent } from "react"
import { BrainCircuit, Search } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useToast } from "@/components/toast-provider"
import { api, type MemoryResult } from "@/lib/api"

export function Memory() {
  const toast = useToast()
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

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-violet)]">Recall</p>
        <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">Memory</h1>
        <p className="mt-1 text-sm text-[var(--color-text-muted)]">
          Semantic search over past workflows — "the last time I did something like this."
        </p>
      </div>

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

      <Card>
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
    </div>
  )
}
