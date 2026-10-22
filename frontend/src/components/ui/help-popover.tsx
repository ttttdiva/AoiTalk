"use client"

import * as React from "react"
import { CircleHelp } from "lucide-react"

import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"

type HelpPopoverProps = {
  /** 説明の対象名。読み上げ名は「{label}の説明」になる。 */
  label: string
  /** 開いたときに表示する静的な補足説明。 */
  children: React.ReactNode
  className?: string
  contentClassName?: string
  side?: React.ComponentProps<typeof PopoverContent>["side"]
  align?: React.ComponentProps<typeof PopoverContent>["align"]
}

/**
 * 見出し・機能名・入力項目の横に置く、クリック／タップ型の「？」ヘルプ。
 *
 * 常時表示していた静的な補足説明の置き場。ユーザー作成の本文、実エラー、
 * 入力検証、処理中／失敗状態、操作直前の課金・外部送信・不可逆操作の警告は
 * ここへ移さず、通常表示のまま残す。
 */
function HelpPopover({
  label,
  children,
  className,
  contentClassName,
  side = "bottom",
  align = "start",
}: HelpPopoverProps) {
  const stopPropagation = (event: React.SyntheticEvent) => {
    event.stopPropagation()
  }

  return (
    <Popover>
      <PopoverTrigger
        type="button"
        aria-label={`${label}の説明`}
        data-slot="help-popover-trigger"
        onClick={stopPropagation}
        onDoubleClick={stopPropagation}
        className={cn(
          "relative inline-flex size-5 shrink-0 cursor-pointer items-center justify-center rounded-full align-middle text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring data-popup-open:text-foreground",
          // 見た目は小さく保ちつつ、タッチ操作に十分なクリック領域を確保する。
          "before:absolute before:-inset-2 before:content-['']",
          className
        )}
      >
        <CircleHelp className="size-4" aria-hidden="true" />
      </PopoverTrigger>
      <PopoverContent
        side={side}
        align={align}
        collisionPadding={8}
        aria-label={`${label}の説明`}
        onClick={stopPropagation}
        onDoubleClick={stopPropagation}
        className={cn(
          "max-h-[min(24rem,var(--available-height))] w-72 max-w-[calc(100vw-1rem)] overflow-y-auto",
          contentClassName
        )}
      >
        <PopoverDescription
          render={<div />}
          className="text-sm leading-relaxed text-popover-foreground [&_p+p]:mt-1.5"
        >
          {children}
        </PopoverDescription>
      </PopoverContent>
    </Popover>
  )
}

export { HelpPopover }
export type { HelpPopoverProps }
