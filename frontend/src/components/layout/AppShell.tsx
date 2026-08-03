import { NavLink, Outlet } from "react-router-dom"
import {
  LayoutDashboard,
  MessageSquare,
  Bot,
  MonitorPlay,
  ListChecks,
  BrainCircuit,
  BrainCog,
  FileBarChart2,
  ScrollText,
  Settings2,
  Radio,
  Wallet,
  Blocks,
  HeartPulse,
  Sparkles,
  Plug,
  UserCircle,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { useBackendHealth } from "@/hooks/use-backend-health"

const NAV = [
  { to: "/", label: "Home", icon: LayoutDashboard, end: true },
  { to: "/chat", label: "AI Chat", icon: MessageSquare },
  { to: "/agent", label: "Agent", icon: Bot },
  { to: "/browser", label: "Live Browser", icon: MonitorPlay },
  { to: "/tasks", label: "Tasks", icon: ListChecks },
  { to: "/wallets", label: "Wallets", icon: Wallet },
  { to: "/profiles", label: "Chrome Profiles", icon: UserCircle },
  { to: "/memory", label: "Memory", icon: BrainCircuit },
  { to: "/reports", label: "Reports", icon: FileBarChart2 },
  { to: "/skills", label: "Skills", icon: Sparkles },
  { to: "/plugins", label: "Plugins", icon: Blocks },
  { to: "/mcp", label: "MCP", icon: Plug },
  { to: "/ai-models", label: "AI Models", icon: BrainCog },
  { to: "/system", label: "System", icon: HeartPulse },
  { to: "/logs", label: "Logs", icon: ScrollText },
  { to: "/settings", label: "Settings", icon: Settings2 },
]

export function AppShell() {
  const health = useBackendHealth()

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)]/60 backdrop-blur-sm">
        <div className="flex items-center gap-2 px-5 py-5">
          <div className="relative flex size-7 items-center justify-center rounded-md bg-[var(--color-signal-amber-dim)]">
            <Radio className="size-4 text-[var(--color-signal-amber)]" />
          </div>
          <div className="flex flex-col leading-none">
            <span className="font-mono text-[13px] font-semibold tracking-wide text-[var(--color-text)]">
              NEXUS
            </span>
            <span className="font-mono text-[10px] tracking-[0.2em] text-[var(--color-text-faint)]">
              AGENT CONSOLE
            </span>
          </div>
        </div>

        <nav className="flex flex-1 flex-col gap-0.5 px-3">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-[var(--color-signal-amber-dim)] text-[var(--color-signal-amber)]"
                    : "text-[var(--color-text-muted)] hover:bg-[var(--color-bg)] hover:text-[var(--color-text)]"
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="flex items-center gap-2 border-t border-[var(--color-border)] px-5 py-4">
          <span
            className={cn(
              "size-2 rounded-full",
              health === "ok" && "bg-[var(--color-signal-green)]",
              health === "down" && "bg-[var(--color-signal-red)]",
              health === "checking" && "bg-[var(--color-text-faint)]"
            )}
          />
          <span className="font-mono text-[11px] text-[var(--color-text-muted)]">
            {health === "ok" && "backend online"}
            {health === "down" && "backend unreachable"}
            {health === "checking" && "checking backend..."}
          </span>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl px-8 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
