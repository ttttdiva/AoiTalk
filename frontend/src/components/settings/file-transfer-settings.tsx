"use client";

import { ArrowLeftRight } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";
import { Label } from "@/components/ui/label";
import { useFileTransferCompatibilityPreference } from "@/lib/file-transfer-preference";

export function FileTransferSettings() {
  const { enabled, hydrated, saveError, setEnabled } =
    useFileTransferCompatibilityPreference();

  return (
    <Card
      size="sm"
      className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0"
    >
      <CardHeader className="flex flex-row items-center gap-1 p-0 !px-0">
        <div className="flex min-w-0 flex-1 items-center px-3 py-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <ArrowLeftRight className="size-4" aria-hidden="true" />
            転送互換モード
          </CardTitle>
        </div>
        <HelpPopover label="転送互換モード" className="mr-2">
          接続環境に合わせてファイル転送の通信方式を切り替えます。ファイル名・形式・内容は保持します。設定はこのブラウザに保存されます。64MiBを超える転送は通常方式を利用します。
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
                id="file-transfer-compatibility-enabled"
                checked={enabled === true}
                onCheckedChange={(checked) => setEnabled(checked === true)}
              />
              <Label
                htmlFor="file-transfer-compatibility-enabled"
                className="cursor-pointer text-xs"
              >
                互換方式を利用する
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
