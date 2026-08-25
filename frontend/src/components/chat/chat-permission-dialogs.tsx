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

/** 許可の適用範囲。`once` は今回だけ、`session` はこのセッション中ずっと。 */
export type ToolPermissionScope = "once" | "session";

export type ToolPermissionRequest = {
  sessionId: string;
  requestId: string;
  toolName: string;
  description: string;
  toolArgs: Record<string, unknown>;
  /** バックエンドが提示する選択肢。`session` を含むときだけ継続許可を出せる。 */
  scopeOptions: ToolPermissionScope[];
};

export type ExternalModelPromptRequest = {
  sessionId: string;
  requestId: string;
  provider: string;
  model: string;
  description: string;
  prompt: string;
  redactedPrompt: string;
  redactionFindings: { category: string; placeholder: string }[];
  notify: boolean;
  sourceKind?: string;
  riskLevel?: string;
  semanticStatus?: string;
  warning?: string;
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
      <DialogContent showCloseButton={false} size="3xl">
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
            {(request.sourceKind || request.riskLevel || request.semanticStatus) && (
              <div className="flex flex-wrap gap-2 text-[10px] text-muted-foreground">
                {request.sourceKind && <span className="rounded border px-2 py-1">source: {request.sourceKind}</span>}
                {request.riskLevel && <span className="rounded border px-2 py-1">risk: {request.riskLevel}</span>}
                {request.semanticStatus && <span className="rounded border px-2 py-1">semantic: {request.semanticStatus}</span>}
              </div>
            )}
            {request.warning && (
              <div role="alert" className="rounded border border-amber-500/50 bg-amber-500/10 p-2 text-xs">
                {request.warning}
              </div>
            )}
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
  onDecision: (approved: boolean, scope?: ToolPermissionScope) => void;
};

/**
 * ツール実行の確認ダイアログ。
 * 「許可 / このセッション中は許可 / 拒否」を選べる。継続許可は同種の操作
 * （同じコマンドのプログラム名、同じ対象パスなど）にだけ適用される。
 */
export function ToolPermissionDialog({
  request,
  onDecision,
}: ToolPermissionDialogProps) {
  const allowSession = request?.scopeOptions?.includes("session") ?? false;

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
            拒否
          </Button>
          {allowSession && (
            <Button
              variant="secondary"
              onClick={() => onDecision(true, "session")}
            >
              このセッション中は許可
            </Button>
          )}
          <Button onClick={() => onDecision(true, "once")}>許可</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export type AskUserQuestionRequest = {
  sessionId: string;
  requestId: string;
  question: string;
  inputType: string;
  choices: string[];
  allowMultiple: boolean;
  allowFreeText: boolean;
  revision: number;
};

export type PlanApprovalRequest = {
  sessionId: string;
  requestId: string;
  planText: string;
  summary: string;
  revision: number;
};

type AskUserQuestionDialogProps = {
  request: AskUserQuestionRequest | null;
  draft: string;
  selectedChoices: string[];
  onDraftChange: (value: string) => void;
  onSelectedChoicesChange: (value: string[]) => void;
  onSubmit: () => void;
  onCancel: () => void;
};

export function AskUserQuestionDialog({
  request,
  draft,
  selectedChoices,
  onDraftChange,
  onSelectedChoicesChange,
  onSubmit,
  onCancel,
}: AskUserQuestionDialogProps) {
  const inputType = request?.inputType ?? "free_text";
  const choices = request?.choices ?? [];
  const showChoices = choices.length > 0 && inputType !== "free_text";

  return (
    <Dialog
      open={request != null}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <DialogContent showCloseButton={false} size="xl">
        <DialogHeader>
          <DialogTitle>確認が必要です</DialogTitle>
          <DialogDescription>{request?.question}</DialogDescription>
        </DialogHeader>
        {showChoices && (
          <div className="space-y-2">
            {choices.map((choice) => {
              const checked = selectedChoices.includes(choice);
              return (
                <label
                  key={choice}
                  className="flex cursor-pointer items-center gap-2 rounded border px-3 py-2 text-sm"
                >
                  <input
                    type={request?.allowMultiple ? "checkbox" : "radio"}
                    checked={checked}
                    onChange={() => {
                      if (request?.allowMultiple) {
                        onSelectedChoicesChange(
                          checked
                            ? selectedChoices.filter((item) => item !== choice)
                            : [...selectedChoices, choice],
                        );
                      } else {
                        onSelectedChoicesChange([choice]);
                      }
                    }}
                  />
                  <span>{choice}</span>
                </label>
              );
            })}
          </div>
        )}
        {(inputType === "free_text" ||
          inputType === "choices_with_free_text" ||
          request?.allowFreeText) && (
          <textarea
            autoFocus
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            className="min-h-24 w-full rounded-md border border-input bg-background p-3 text-sm"
            placeholder="回答を入力"
          />
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            キャンセル
          </Button>
          <Button onClick={onSubmit}>送信</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type PlanApprovalDialogProps = {
  request: PlanApprovalRequest | null;
  draft: string;
  feedbackDraft: string;
  onDraftChange: (value: string) => void;
  onFeedbackDraftChange: (value: string) => void;
  onApprove: () => void;
  onFeedback: () => void;
  onCancel: () => void;
};

export function PlanApprovalDialog({
  request,
  draft,
  feedbackDraft,
  onDraftChange,
  onFeedbackDraftChange,
  onApprove,
  onFeedback,
  onCancel,
}: PlanApprovalDialogProps) {
  return (
    <Dialog
      open={request != null}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <DialogContent showCloseButton={false} size="3xl">
        <DialogHeader>
          <DialogTitle>計画の承認</DialogTitle>
          <DialogDescription>
            {request?.summary || "実行前に計画を確認してください。"}
          </DialogDescription>
        </DialogHeader>
        <textarea
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          className="min-h-56 w-full rounded-md border border-input bg-background p-3 text-sm"
        />
        <div className="space-y-2">
          <span className="text-xs font-medium text-muted-foreground">
            フィードバック（計画継続時）
          </span>
          <textarea
            value={feedbackDraft}
            onChange={(event) => onFeedbackDraftChange(event.target.value)}
            className="min-h-20 w-full rounded-md border border-input bg-background p-3 text-sm"
            placeholder="修正してほしい点があれば入力"
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            キャンセル
          </Button>
          <Button variant="secondary" onClick={onFeedback}>
            フィードバックして再計画
          </Button>
          <Button onClick={onApprove}>承認して実行</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
