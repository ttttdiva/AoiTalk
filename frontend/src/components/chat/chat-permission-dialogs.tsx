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

export type ExternalModelMaskingStatus =
  | "MASKING_APPLIED"
  | "MASKING_UNNECESSARY";

/**
 * The v2 external-send review request.  These values are all server-owned
 * except for the final payload maintained by the dialog draft.  Keep the
 * wire-facing security fields explicit here so callers cannot accidentally
 * fall back to the old prompt/redacted_prompt contract.
 */
export type ExternalModelPromptRequest = {
  sessionId: string;
  requestId: string;
  contractVersion: 2;
  reviewNonce: string;
  bindingDigest: string;
  action: string;
  transport: string;
  destination: string;
  provider: string;
  tool: string;
  model: string;
  originalPayload: string;
  candidatePayload: string;
  maskingStatus: ExternalModelMaskingStatus;
  description: string;
  redactionFindings: { category: string; placeholder: string }[];
  notify: boolean;
  sourceKind?: string;
  riskLevel?: string;
  semanticStatus?: string;
  warning?: string;
};

/** Parsed, session-independent portion of an external-send review request. */
export type ExternalModelPromptRequestData = Omit<
  ExternalModelPromptRequest,
  "sessionId" | "requestId"
>;

export type ExternalModelPromptResponseInput = {
  requestId: string;
  approved: boolean;
  finalPayload: string;
  reviewNonce: string;
  bindingDigest: string;
  targetSessionId?: string | null;
};

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * Parse only the v2 external-send review contract.
 *
 * In particular, the candidate is never inferred from the original payload
 * (or from any of the legacy prompt/redacted_prompt fields).  Invalid data is
 * rejected as a whole so a malformed request can never reach an editable
 * approval control.
 */
export function parseExternalModelPromptRequestData(
  data: unknown,
): ExternalModelPromptRequestData | null {
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return null;
  }
  const record = data as Record<string, unknown>;
  if (record.contract_version !== 2) return null;

  const requiredNonEmptyMetadata = [
    "request_id",
    "review_nonce",
    "binding_digest",
    "action",
    "transport",
    "destination",
  ] as const;
  if (
    requiredNonEmptyMetadata.some((key) => !isNonEmptyString(record[key]))
  ) {
    return null;
  }
  if (
    !isNonEmptyString(record.original_payload) ||
    !isNonEmptyString(record.candidate_payload)
  ) {
    return null;
  }
  const labelString = (key: "provider" | "tool" | "model"): string | null => {
    // The metadata labels may be omitted or be empty strings, but an
    // explicitly supplied null/undefined/value of another type is malformed
    // and must not be silently coerced into a display placeholder.
    if (!(key in record)) return "";
    return typeof record[key] === "string" ? record[key] : null;
  };
  const provider = labelString("provider");
  const tool = labelString("tool");
  const model = labelString("model");
  if (provider === null || tool === null || model === null) return null;
  if (
    record.masking_status !== "MASKING_APPLIED" &&
    record.masking_status !== "MASKING_UNNECESSARY"
  ) {
    return null;
  }
  if (!Array.isArray(record.redaction_findings)) return null;
  if ("description" in record && typeof record.description !== "string") {
    return null;
  }
  if ("notify" in record && typeof record.notify !== "boolean") {
    return null;
  }
  for (const key of [
    "source_kind",
    "risk_level",
    "semantic_status",
    "warning",
  ]) {
    if (key in record && typeof record[key] !== "string") return null;
  }

  const redactionFindings: { category: string; placeholder: string }[] = [];
  for (const item of record.redaction_findings) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) {
      return null;
    }
    const finding = item as Record<string, unknown>;
    if (
      !isNonEmptyString(finding.category) ||
      !isNonEmptyString(finding.placeholder)
    ) {
      return null;
    }
    redactionFindings.push({
      category: finding.category,
      placeholder: finding.placeholder,
    });
  }

  // Human-readable metadata is intentionally non-authoritative.  Keep it
  // type-safe while allowing the server to omit optional presentation fields.
  const optionalString = (key: string): string | undefined =>
    typeof record[key] === "string" ? (record[key] as string) : undefined;

  return {
    contractVersion: 2,
    reviewNonce: record.review_nonce as string,
    bindingDigest: record.binding_digest as string,
    action: record.action as string,
    transport: record.transport as string,
    destination: record.destination as string,
    provider,
    tool,
    model,
    originalPayload: record.original_payload,
    candidatePayload: record.candidate_payload,
    maskingStatus: record.masking_status,
    description:
      typeof record.description === "string"
        ? record.description
        : "外部モデルへ送信する内容を確認してください",
    redactionFindings,
    notify: record.notify !== false,
    sourceKind: optionalString("source_kind"),
    riskLevel: optionalString("risk_level"),
    semanticStatus: optionalString("semantic_status"),
    warning: optionalString("warning"),
  };
}

type ExternalModelPromptDialogProps = {
  request: ExternalModelPromptRequest | null;
  draft: string;
  onDraftChange: (value: string) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLTextAreaElement>) => void;
  onDecision: (approved: boolean) => void;
};

/**
 * 外部モデル送信の確認ダイアログ。
 * The original and candidate payloads are server-owned read-only evidence;
 * only the final payload draft can be edited and sent.
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
            <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
              <span>action: {request.action}</span>
              <span>transport: {request.transport}</span>
              <span>destination: {request.destination}</span>
              <span>provider: {request.provider || "-"}</span>
              <span>tool: {request.tool || "-"}</span>
              <span>model: {request.model || "-"}</span>
            </div>
            <div
              data-testid="external-model-masking-status"
              className="rounded border border-blue-500/40 bg-blue-500/10 p-2 text-xs"
            >
              {request.maskingStatus}
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
              <span className="text-xs font-medium">原文（送信前）</span>
              <pre
                data-testid="external-model-original-payload"
                className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs"
              >
                {request.originalPayload}
              </pre>
            </div>
            <div className="space-y-2">
              <span className="text-xs font-medium">秘匿候補（サーバー提示・編集不可）</span>
              <pre
                data-testid="external-model-candidate-payload"
                className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs"
              >
                {request.candidatePayload}
              </pre>
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
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-medium">最終送信内容</span>
                <span className="text-[10px] text-muted-foreground">
                  Enterで送信 / Shift+Enterで改行
                </span>
              </div>
              <textarea
                autoFocus
                aria-label="最終送信内容"
                value={draft}
                onChange={(event) => onDraftChange(event.target.value)}
                onKeyDown={onKeyDown}
                className="min-h-52 w-full rounded-md border border-input bg-background p-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              />
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
            送信
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

/** Server-owned material approval binding (digests only). */
export type PlanBindingDigest = {
  planId: string;
  revision: number;
  planDigest: string;
  actionDigest: string;
  contextSelectionHash: string;
  evidenceHashSetDigest: string;
  materialActionPreviewDigest: string;
};

export type MaterialActionPreviewAction = {
  index: number;
  tool: string;
  arguments: Record<string, unknown>;
  dynamicFields: string[];
};

export type MaterialActionPreview = {
  version: 1;
  actionCount: number;
  actions: MaterialActionPreviewAction[];
};

export type PlanApprovalRequest = {
  sessionId: string;
  requestId: string;
  planText: string;
  summary: string;
  revision: number;
  planRevision: number;
  planDigest: string;
  actionDigest: string;
  materialActionPreviewDigest: string;
  planBinding: PlanBindingDigest | null;
  materialActionPreview: MaterialActionPreview | null;
  /** Non-null when the server payload cannot be safely approved. */
  approvalValidationError: string | null;
};

type JsonObject = Record<string, unknown>;

function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isJsonValue(value: unknown, depth = 0): boolean {
  if (depth > 12) return false;
  if (value === null || typeof value === "string" || typeof value === "boolean" || typeof value === "number") {
    return typeof value !== "number" || Number.isFinite(value);
  }
  if (Array.isArray(value)) return value.every((item) => isJsonValue(item, depth + 1));
  if (isJsonObject(value)) {
    return Object.entries(value).every(([key, item]) => key.length > 0 && isJsonValue(item, depth + 1));
  }
  return false;
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

const PLAN_BINDING_KEYS = new Set([
  "plan_id",
  "revision",
  "plan_digest",
  "action_digest",
  "context_selection_hash",
  "evidence_hash_set_digest",
  "material_action_preview_digest",
]);
const MATERIAL_ACTION_PREVIEW_KEYS = new Set([
  "version",
  "action_count",
  "actions",
]);
const MATERIAL_ACTION_KEYS = new Set([
  "index",
  "tool",
  "arguments",
  "dynamic_fields",
]);
const MAX_MATERIAL_ACTIONS = 32;

/**
 * Parse the server-generated material action preview without compatibility
 * fallbacks. Invalid material data is retained as a non-approvable request so
 * the user can understand why approval is disabled and use feedback to ask
 * for a fresh plan.
 */
export function parsePlanApprovalRequestData(
  data: Record<string, unknown>,
): Omit<PlanApprovalRequest, "sessionId" | "requestId"> {
  const errors: string[] = [];
  const planText = typeof data.plan_text === "string" ? data.plan_text : "";
  const summary = typeof data.summary === "string" && data.summary.trim()
    ? data.summary
    : "実行前に計画を確認してください。";
  const revision = positiveInteger(data.revision) ? data.revision : 0;
  const planRevision = positiveInteger(data.plan_revision) ? data.plan_revision : 0;
  const planDigest = nonEmptyString(data.plan_digest) ? data.plan_digest.trim() : "";
  const actionDigest = nonEmptyString(data.action_digest) ? data.action_digest.trim() : "";
  const materialActionPreviewDigest = nonEmptyString(data.material_action_preview_digest)
    ? data.material_action_preview_digest.trim()
    : "";
  if (!revision) errors.push("承認リビジョンが不正です");
  if (!planRevision) errors.push("計画リビジョンがありません");
  if (revision && planRevision && revision !== planRevision) {
    errors.push("承認リビジョンと計画リビジョンが一致しません");
  }
  if (!planDigest) errors.push("plan_digestがありません");
  if (!actionDigest) errors.push("action_digestがありません");
  if (!materialActionPreviewDigest) errors.push("material_action_preview_digestがありません");

  let planBinding: PlanBindingDigest | null = null;
  if (!isJsonObject(data.plan_binding)) {
    errors.push("plan_bindingがありません");
  } else {
    const binding = data.plan_binding;
    if (Object.keys(binding).some((key) => !PLAN_BINDING_KEYS.has(key))) {
      errors.push("plan_bindingに許可されていない値があります");
    }
    const bindingRevision = positiveInteger(binding.revision) ? binding.revision : 0;
    const bindingPlanId = nonEmptyString(binding.plan_id) ? binding.plan_id.trim() : "";
    const bindingPlanDigest = nonEmptyString(binding.plan_digest) ? binding.plan_digest.trim() : "";
    const bindingActionDigest = nonEmptyString(binding.action_digest) ? binding.action_digest.trim() : "";
    const bindingContextHash = nonEmptyString(binding.context_selection_hash) ? binding.context_selection_hash.trim() : "";
    const bindingEvidenceHash = nonEmptyString(binding.evidence_hash_set_digest) ? binding.evidence_hash_set_digest.trim() : "";
    const bindingPreviewDigest = nonEmptyString(binding.material_action_preview_digest)
      ? binding.material_action_preview_digest.trim()
      : "";
    if (!bindingPlanId) errors.push("plan_binding.plan_idがありません");
    if (!bindingRevision) errors.push("plan_binding.revisionが不正です");
    if (!bindingPlanDigest) errors.push("plan_binding.plan_digestがありません");
    if (!bindingActionDigest) errors.push("plan_binding.action_digestがありません");
    if (!bindingContextHash) errors.push("plan_binding.context_selection_hashがありません");
    if (!bindingEvidenceHash) errors.push("plan_binding.evidence_hash_set_digestがありません");
    if (!bindingPreviewDigest) errors.push("plan_binding.material_action_preview_digestがありません");
    if (bindingRevision !== planRevision) errors.push("計画リビジョンとbindingが一致しません");
    if (bindingPlanDigest !== planDigest) errors.push("plan_digestとbindingが一致しません");
    if (bindingActionDigest !== actionDigest) errors.push("action_digestとbindingが一致しません");
    if (bindingPreviewDigest !== materialActionPreviewDigest) errors.push("preview digestとbindingが一致しません");
    planBinding = {
      planId: bindingPlanId,
      revision: bindingRevision,
      planDigest: bindingPlanDigest,
      actionDigest: bindingActionDigest,
      contextSelectionHash: bindingContextHash,
      evidenceHashSetDigest: bindingEvidenceHash,
      materialActionPreviewDigest: bindingPreviewDigest,
    };
  }

  let materialActionPreview: MaterialActionPreview | null = null;
  if (!isJsonObject(data.material_action_preview)) {
    errors.push("material_action_previewがありません");
  } else {
    const preview = data.material_action_preview;
    if (Object.keys(preview).some((key) => !MATERIAL_ACTION_PREVIEW_KEYS.has(key))) {
      errors.push("material_action_previewに許可されていない値があります");
    }
    const version = preview.version;
    const actionCount = preview.action_count;
    const rawActions = preview.actions;
    if (version !== 1) errors.push("material_action_previewのversionが不正です");
    if (!positiveInteger(actionCount)) errors.push("material_action_preview.action_countが不正です");
    if (positiveInteger(actionCount) && actionCount > MAX_MATERIAL_ACTIONS) {
      errors.push("material_action_previewの件数が上限を超えています");
    }
    if (!Array.isArray(rawActions) || rawActions.length === 0) {
      errors.push("material_action_preview.actionsがありません");
    } else if (positiveInteger(actionCount) && rawActions.length !== actionCount) {
      errors.push("action_countとpreviewの件数が一致しません");
    }
    const parsedActions: MaterialActionPreviewAction[] = [];
    if (Array.isArray(rawActions)) {
      rawActions.forEach((rawAction, index) => {
        if (!isJsonObject(rawAction)) {
          errors.push(`preview action ${index + 1}が不正です`);
          return;
        }
        if (Object.keys(rawAction).some((key) => !MATERIAL_ACTION_KEYS.has(key))) {
          errors.push(`preview action ${index + 1}に許可されていない値があります`);
        }
        const actionIndex = rawAction.index;
        const tool = nonEmptyString(rawAction.tool) ? rawAction.tool.trim() : "";
        const args = rawAction.arguments;
        const rawDynamicFields = rawAction.dynamic_fields;
        const dynamicFields = Array.isArray(rawDynamicFields)
          ? rawDynamicFields.filter((field): field is string => nonEmptyString(field))
          : [];
        const dynamicFieldsValid = Array.isArray(rawDynamicFields) &&
          dynamicFields.length === rawDynamicFields.length &&
          dynamicFields.every((field) => !field.includes(".") && !field.includes("[") && !field.includes("]"));
        if (actionIndex !== index) errors.push(`preview action ${index + 1}のindexが不正です`);
        if (!tool) errors.push(`preview action ${index + 1}のtoolがありません`);
        if (!isJsonObject(args) || !isJsonValue(args)) errors.push(`preview action ${index + 1}のargumentsが不正です`);
        if (!dynamicFieldsValid) errors.push(`preview action ${index + 1}のdynamic_fieldsが不正です`);
        parsedActions.push({
          index: typeof actionIndex === "number" ? actionIndex : index,
          tool,
          arguments: isJsonObject(args) ? args : {},
          dynamicFields,
        });
      });
    }
    if (version === 1 && positiveInteger(actionCount) && Array.isArray(rawActions)) {
      materialActionPreview = { version: 1, actionCount, actions: parsedActions };
    }
  }

  const approvalValidationError = errors.length > 0
    ? `承認対象を検証できないため承認できません。${errors.join("、")}。フィードバックして再計画してください。`
    : null;
  return {
    planText,
    summary,
    revision,
    planRevision,
    planDigest,
    actionDigest,
    materialActionPreviewDigest,
    planBinding,
    materialActionPreview,
    approvalValidationError,
  };
}

/** Build the only material approval response accepted by the UI. */
export function buildPlanApprovalResponsePayload(
  request: PlanApprovalRequest,
): Record<string, unknown> | null {
  if (request.approvalValidationError || !request.planBinding || !request.materialActionPreview) {
    return null;
  }
  const binding = request.planBinding;
  return {
    action: "approve",
    plan_text: request.planText,
    revision: request.revision,
    plan_binding: {
      plan_id: binding.planId,
      revision: binding.revision,
      plan_digest: binding.planDigest,
      action_digest: binding.actionDigest,
      context_selection_hash: binding.contextSelectionHash,
      evidence_hash_set_digest: binding.evidenceHashSetDigest,
      material_action_preview_digest: binding.materialActionPreviewDigest,
    },
    action_digest: request.actionDigest,
    material_action_preview_digest: request.materialActionPreviewDigest,
  };
}

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
  feedbackDraft: string;
  onFeedbackDraftChange: (value: string) => void;
  onApprove: () => void;
  onFeedback: () => void;
  onCancel: () => void;
};

export function PlanApprovalDialog({
  request,
  feedbackDraft,
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
        <div className="space-y-3">
          <div className="space-y-1">
            <span className="text-xs font-medium text-muted-foreground">
              実行計画（読み取り専用）
            </span>
            <pre
              data-testid="plan-approval-plan-text"
              className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-input bg-muted/30 p-3 text-sm"
            >
              {request?.planText || "（計画本文なし）"}
            </pre>
          </div>
          <div className="space-y-2 rounded-md border border-input bg-muted/20 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium">
                実行対象アクション（{request?.materialActionPreview?.actionCount ?? 0}件）
              </span>
              {request?.materialActionPreview && (
                <span className="text-[10px] text-muted-foreground">
                  preview v{request.materialActionPreview.version}
                </span>
              )}
            </div>
            {request?.materialActionPreview?.actions.length ? (
              <ol
                data-testid="plan-approval-actions"
                className="max-h-80 space-y-3 overflow-auto"
              >
                {request.materialActionPreview.actions.map((action) => (
                  <li
                    key={`${action.index}-${action.tool}`}
                    className="rounded border border-border-subtle bg-background p-3"
                    data-testid={`plan-approval-action-${action.index}`}
                  >
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="font-semibold">#{action.index + 1}</span>
                      <code className="rounded bg-muted px-1.5 py-0.5 font-mono">
                        {action.tool}
                      </code>
                    </div>
                    <div className="mt-2 space-y-1">
                      <span className="text-[11px] font-medium text-muted-foreground">
                        arguments（読み取り専用）
                      </span>
                      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded border bg-muted/30 p-2 font-mono text-[11px]">
                        {JSON.stringify(action.arguments, null, 2)}
                      </pre>
                    </div>
                    <div className="mt-2 text-[11px] text-muted-foreground">
                      dynamic fields: {action.dynamicFields.length > 0
                        ? action.dynamicFields.join(", ")
                        : "なし"}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="text-xs text-muted-foreground">
                実行対象アクションを表示できません。
              </div>
            )}
          </div>
          <div
            data-testid="plan-approval-binding"
            className="grid gap-1 rounded-md border border-border-subtle bg-muted/20 p-3 text-[10px] text-muted-foreground sm:grid-cols-3"
          >
            <span>plan digest: {request?.planDigest || "（なし）"}</span>
            <span>action digest: {request?.actionDigest || "（なし）"}</span>
            <span>
              preview digest: {request?.materialActionPreviewDigest || "（なし）"}
            </span>
          </div>
        </div>
        {request?.approvalValidationError && (
          <div
            role="alert"
            data-testid="plan-approval-invalid"
            className="rounded border border-amber-500/50 bg-amber-500/10 p-3 text-xs"
          >
            {request.approvalValidationError}
          </div>
        )}
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
          <Button
            onClick={onApprove}
            disabled={request?.approvalValidationError != null}
            title={request?.approvalValidationError || undefined}
          >
            承認して実行
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
