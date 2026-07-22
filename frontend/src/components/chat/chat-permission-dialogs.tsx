"use client";

import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

export type ToolPermissionRequest = {
  requestId: string;
  toolName: string;
  description: string;
  toolArgs: Record<string, unknown>;
};

export type ExternalModelPromptRequest = {
  requestId: string;
  provider: string;
  model: string;
  description: string;
  prompt: string;
  redactedPrompt: string;
  redactionFindings: { category: string; placeholder: string }[];
  notify: boolean;
};

type ExternalModelPromptDialogProps = {
  request: ExternalModelPromptRequest | null;
  draft: string;
  onDraftChange: (value: string) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLTextAreaElement>) => void;
  onDecision: (approved: boolean) => void;
};

/**
 * 外部モデル送信の確認ダイアログ。
 * `page.tsx` から JSX をそのまま切り出した表示専用コンポーネント（挙動不変）。
 */
export function ExternalModelPromptDialog({
  request,
  draft,
  onDraftChange,
  onKeyDown,
  onDecision,
}: ExternalModelPromptDialogProps) {
  return (
    <Dialog
      open={request != null}
      onOpenChange={(open) => {
        if (!open) onDecision(false);
      }}
    >
      <DialogContent showCloseButton={false} className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>外部モデル送信の確認</DialogTitle>
          <DialogDescription>{request?.description}</DialogDescription>
        </DialogHeader>
        {request && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{request.provider}</span>
              <span>/</span>
              <span>{request.model}</span>
            </div>
            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-medium">秘匿版プロンプト</span>
                <span className="text-[10px] text-muted-foreground">
                  Enterで送信 / Shift+Enterで改行
                </span>
              </div>
              <textarea
                autoFocus
                value={draft}
                onChange={(event) => onDraftChange(event.target.value)}
                onKeyDown={onKeyDown}
                className="min-h-52 w-full rounded-md border border-input bg-background p-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              />
            </div>
            {request.redactionFindings.length > 0 && (
              <div className="flex flex-wrap gap-2 text-[10px] text-muted-foreground">
                {request.redactionFindings.map((finding, index) => (
                  <span
                    key={`${finding.placeholder}-${index}`}
                    className="rounded border bg-muted/40 px-2 py-1"
                  >
                    {finding.category}: {finding.placeholder}
                  </span>
                ))}
              </div>
            )}
            <div className="space-y-2">
              <span className="text-xs font-medium">原文プロンプト</span>
              <pre className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs">
                {request.prompt}
              </pre>
            </div>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onDecision(false)}>
            キャンセル
          </Button>
          <Button
            onClick={() => onDecision(true)}
            disabled={!draft.trim()}
          >
            この内容で送信
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type ToolPermissionDialogProps = {
  request: ToolPermissionRequest | null;
  onDecision: (approved: boolean) => void;
};

/**
 * ツール実行の確認ダイアログ。
 * `page.tsx` から JSX をそのまま切り出した表示専用コンポーネント（挙動不変）。
 */
export function ToolPermissionDialog({
  request,
  onDecision,
}: ToolPermissionDialogProps) {
  return (
    <Dialog
      open={request != null}
      onOpenChange={(open) => {
        if (!open) onDecision(false);
      }}
    >
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>ツール実行の確認</DialogTitle>
          <DialogDescription>{request?.description}</DialogDescription>
        </DialogHeader>
        {request && (
          <div className="rounded-md border bg-muted/40 p-3 font-mono text-xs break-words">
            <div className="font-sans text-muted-foreground">
              {request.toolName}
            </div>
            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap">
              {JSON.stringify(request.toolArgs, null, 2)}
            </pre>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onDecision(false)}>
            いいえ
          </Button>
          <Button onClick={() => onDecision(true)}>はい</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
