import * as React from "react"
import * as SwitchPrimitive from "@radix-ui/react-switch"
import { cn } from "@/lib/utils"

export const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      "peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-[var(--color-border-strong)] bg-[var(--color-bg)] transition-colors data-[state=checked]:bg-[var(--color-signal-cyan-dim)] data-[state=checked]:border-[var(--color-signal-cyan)] disabled:cursor-not-allowed disabled:opacity-40",
      className
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb
      className={cn(
        "pointer-events-none block h-3.5 w-3.5 translate-x-0.5 rounded-full bg-[var(--color-text-faint)] transition-transform data-[state=checked]:translate-x-4 data-[state=checked]:bg-[var(--color-signal-cyan)]"
      )}
    />
  </SwitchPrimitive.Root>
))
Switch.displayName = "Switch"
