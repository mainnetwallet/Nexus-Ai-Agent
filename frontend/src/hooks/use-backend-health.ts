import { useEffect, useState } from "react"
import { api } from "@/lib/api"

export type HealthState = "checking" | "ok" | "down"

export function useBackendHealth(pollMs = 15000): HealthState {
  const [health, setHealth] = useState<HealthState>("checking")

  useEffect(() => {
    let cancelled = false

    async function check() {
      try {
        await api.health()
        if (!cancelled) setHealth("ok")
      } catch {
        if (!cancelled) setHealth("down")
      }
    }

    check()
    const id = setInterval(check, pollMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [pollMs])

  return health
}
