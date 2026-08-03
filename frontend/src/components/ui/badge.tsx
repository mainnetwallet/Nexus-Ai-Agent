import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wide",
  {
    variants: {
      variant: {
        neutral: "border-[var(--color-border-strong)] bg-[var(--color-surface)] text-[var(--color-text-muted)]",
        amber: "border-transparent bg-[var(--color-signal-amber-dim)] text-[var(--color-signal-amber)]",
        cyan: "border-transparent bg-[var(--color-signal-cyan-dim)] text-[var(--color-signal-cyan)]",
        green: "border-transparent bg-[var(--color-signal-green-dim)] text-[var(--color-signal-green)]",
        red: "border-transparent bg-[var(--color-signal-red-dim)] text-[var(--color-signal-red)]",
        violet: "border-transparent bg-[#9b8cf226] text-[var(--color-signal-violet)]",
      },
    },
    defaultVariants: { variant: "neutral" },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant, className }))} {...props} />
}
