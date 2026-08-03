import { useCallback, useEffect, useRef, useState, type DependencyList } from "react"

interface AsyncState<T> {
  data: T | null
  error: string | null
  loading: boolean
}

/**
 * Runs `fn` on mount (and whenever `deps` change), tracking loading/error/data.
 * Returns a `refetch` function to re-run it manually (e.g. after a mutation
 * or on a poll interval).
 */
export function useAsync<T>(fn: () => Promise<T>, deps: DependencyList = []) {
  const [state, setState] = useState<AsyncState<T>>({ data: null, error: null, loading: true })
  const fnRef = useRef(fn)
  fnRef.current = fn

  const refetch = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null }))
    try {
      const data = await fnRef.current()
      setState({ data, error: null, loading: false })
    } catch (err) {
      setState({ data: null, error: err instanceof Error ? err.message : String(err), loading: false })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    refetch()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { ...state, refetch }
}
