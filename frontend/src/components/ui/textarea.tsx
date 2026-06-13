import * as React from "react"

import { cn } from "@/lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex field-sizing-content min-h-16 w-full rounded-lg border border-input bg-white/45 px-2.5 py-2 text-base text-foreground shadow-[inset_0_1px_rgba(255,255,255,0.62)] transition-colors outline-none backdrop-blur-xl placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 dark:bg-input/30 dark:shadow-[inset_0_1px_rgba(255,255,255,0.12)] md:text-sm",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }
