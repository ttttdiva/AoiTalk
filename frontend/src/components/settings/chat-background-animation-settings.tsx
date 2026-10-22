"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";
import { Label } from "@/components/ui/label";
import { useChatBackgroundPreference } from "@/lib/chat-background-preference";
import { Waves } from "lucide-react";

export function ChatBackgroundAnimationSettings() {
  const { enabled, hydrated, saveError, setEnabled } =
    useChatBackgroundPreference();

  return (
    <Card
      size="sm"
      className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0"
    >
      <CardHeader className="flex flex-row items-center gap-1 p-0 !px-0">
        <div className="flex min-w-0 flex-1 items-center px-3 py-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Waves className="size-4" aria-hidden="true" />
            チャット背景アニメーション
          </CardTitle>
        </div>
        <HelpPopover label="チャット背景アニメーション" className="mr-2">
          チャットの背面に低コントラストの波模様を表示します。端末単位で保存され、動きを減らす設定が有効な場合は停止します。
        </HelpPopover>
      </CardHeader>
      <CardContent className="space-y-3 px-3 pb-3 pt-1">
        {!hydrated ? (
          <p className="text-xs text-muted-foreground" role="status">
            設定を読み込み中…
          </p>
        ) : (
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <Checkbox
                id="chat-background-animation-enabled"
                checked={enabled === true}
                onCheckedChange={(checked) => setEnabled(checked === true)}
              />
              <Label
                htmlFor="chat-background-animation-enabled"
                className="cursor-pointer text-xs"
              >
                波模様を表示する
              </Label>
            </div>
            <span className="shrink-0 text-xs text-muted-foreground">
              {enabled ? "ON" : "OFF"}
            </span>
          </div>
        )}
        {saveError && (
          <p role="alert" className="text-xs text-destructive">
            {saveError}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
