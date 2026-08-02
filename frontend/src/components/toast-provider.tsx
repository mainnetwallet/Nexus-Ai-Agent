import * as React from "react"
import { cn } from "@/lib/utils"
import { CheckCircle2, XCircle, Info } from "lucide-react"

type ToastKind = "success" | "error" | "info"
interface ToastItem {
  id: number
  message: string
  kind: ToastKind
}

interface ToastContextValue {
  push: (message: string, kind?: ToastKind) => void
}

const ToastContext = React.createContext<ToastContextValue | null>(null)

export function useToast() {
  const ctx = React.useContext(ToastContext)
  if (!ctx) throw new Error("useToast must be used within ToastProvider")
  return ctx
}

const ICONS: Record<ToastKind, React.ReactNode> = {
  success: <CheckCircle2 className="size-4 text-[var(--color-signal-green)]" />,
  error: <XCircle className="size-4 text-[var(--color-signal-red)]" />,
  info: <Info className="size-4 text-[var(--color-signal-cyan)]" />,
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([])

  const push = React.useCallback((message: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev, { id, message, kind }])
    setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id))
    }, 4000)
  }, [])

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              "flex items-center gap-2 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-4 py-2.5 text-sm text-[var(--color-text)] shadow-xl"
            )}
          >
            {ICONS[t.kind]}
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
