"use client";

import { useMemo, useState } from "react";
import { diffLines } from "diff";
import useSWR from "swr";
import {
  AlertTriangle,
  Check,
  Clock3,
  GitCompare,
  History,
  Loader2,
  RefreshCw,
  Save,
  Undo2,
  X,
} from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useConfirm } from "@/hooks/use-confirm";
import {
  SkillContentReviewFields,
  type SkillReviewContent,
} from "@/components/skills/skill-recording-review";
import {
  proposalRevisionToken,
  skillLearningApi,
  SkillLearningApiError,
  type SkillProposal,
  type SkillProposalContent,
  type SkillProposalHistoryEntry,
  type SkillProposalStatus,
  type SkillUsageReceipt,
} from "@/lib/skill-learning";

const STATUS_LABELS: Record<SkillProposalStatus, string> = {
  pending: "保留中",
  applied: "適用済み",
  rejected: "拒否済み",
  stale: "陳腐化",
};

const OPERATION_LABELS = {
  create: "作成",
  update: "更新",
} as const;

const SCOPE_LABELS = {
  global: "グローバル",
  project: "プロジェクト",
} as const;

function statusVariant(
  status: SkillProposalStatus,
): "default" | "secondary" | "destructive" | "outline" {
  if (status === "stale") return "destructive";
  if (status === "applied") return "secondary";
  return "outline";
}

function dateTime(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ja-JP");
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function normalizeContent(
  value: Partial<SkillProposalContent> | null | undefined,
  targetName: string,
): SkillProposalContent {
  const parameters = asRecord(value?.parameters) ?? {};
  return {
    name: targetName,
    description: String(value?.description ?? ""),
    prompt_template: String(value?.prompt_template ?? ""),
    trigger_mode: String(value?.trigger_mode ?? "both"),
    aliases: stringList(value?.aliases),
    bound_tools: stringList(value?.bound_tools),
    examples: stringList(value?.examples),
    tags: stringList(value?.tags),
    parameters,
  };
}

function parseCommaList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseLineList(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

type ParametersParseResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; message: string };

function parseParameters(value: string): ParametersParseResult {
  try {
    const parsed = JSON.parse(value || "{}");
    const record = asRecord(parsed);
    if (!record) {
      return {
        ok: false,
        message: "parameters は JSON オブジェクトで入力してください。",
      };
    }
    return { ok: true, value: record };
  } catch {
    return {
      ok: false,
      message: "parameters の JSON が正しくありません。",
    };
  }
}

function boundedText(value: unknown, maxLength = 160): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const text = String(value).trim();
  if (!text) return null;
  if (text.includes("[REDACTED_SECRET]")) {
    return "機密情報はサーバーで非表示化されています";
  }
  return text.length <= maxLength
    ? text
    : `${text.slice(0, maxLength - 1)}…`;
}

interface SafeEvidenceRow {
  label: string;
  value: string;
}

function addSafeRecordRows(
  rows: SafeEvidenceRow[],
  prefix: string,
  value: unknown,
  allowedKeys: readonly string[],
) {
  if (rows.length >= 8) return;
  const record = asRecord(value);
  if (!record) return;

  for (const key of allowedKeys) {
    if (rows.length >= 8) return;
    const text = boundedText(record[key]);
    if (text) {
      rows.push({ label: `${prefix}${key}`, value: text });
    }
  }
}

function safeProposalEvidence(proposal: SkillProposal): SafeEvidenceRow[] {
  const rows: SafeEvidenceRow[] = [];
  const visited = new Set<object>();

  const visit = (value: unknown, depth: number) => {
    if (depth > 4 || rows.length >= 8) return;

    if (Array.isArray(value)) {
      for (const item of value.slice(0, 8)) {
        addSafeRecordRows(
          rows,
          "evidence.",
          item,
          ["type", "id", "label", "summary", "reason"] as const,
        );
      }
      return;
    }

    const record = asRecord(value);
    if (!record || visited.has(record)) return;
    visited.add(record);

    if ("source" in record) {
      addSafeRecordRows(
        rows,
        "source.",
        record.source,
        [
          "source_type",
          "type",
          "label",
          "title",
          "summary",
          "reason",
          "surface",
          "action",
        ] as const,
      );
      visit(record.source, depth + 1);
    }
    if ("evidence" in record) {
      visit(record.evidence, depth + 1);
    }
    if ("revision" in record) {
      addSafeRecordRows(
        rows,
        "revision.",
        record.revision,
        [
          "source_type",
          "type",
          "summary",
          "reason",
          "surface",
          "action",
        ] as const,
      );
      visit(record.revision, depth + 1);
    }
    if ("previous" in record) {
      visit(record.previous, depth + 1);
    }
  };

  visit(proposal.provenance, 0);

  if (proposal.receipt_id && rows.length < 8) {
    rows.push({
      label: "evidence.usage_receipt",
      value: proposal.receipt_id,
    });
  }
  return rows.slice(0, 8);
}

function maskedProjectId(projectId?: string | null): string {
  if (!projectId) return "ID なし";
  if (projectId.length <= 12) return projectId;
  return `${projectId.slice(0, 8)}…${projectId.slice(-4)}`;
}

function formatContentForDiff(
  content: SkillProposalContent | null,
): string {
  if (!content) {
    return "(target did not exist)\n";
  }
  return [
    `name: ${content.name}`,
    `description: ${content.description}`,
    `trigger_mode: ${content.trigger_mode}`,
    `aliases: ${content.aliases.join(", ")}`,
    `bound_tools: ${content.bound_tools.join(", ")}`,
    `examples: ${content.examples.join(" | ")}`,
    `tags: ${content.tags.join(", ")}`,
    "parameters:",
    JSON.stringify(content.parameters, null, 2),
    "prompt_template:",
    content.prompt_template,
    "",
  ].join("\n");
}

function currentHash(proposal: SkillProposal): string | null {
  if (proposal.status === "applied" && !proposal.rolled_back_at) {
    return proposal.applied_hash ?? null;
  }
  if (proposal.rolled_back_at) {
    return proposal.base_hash ?? null;
  }
  if (proposal.status === "stale") {
    const observed = [...(proposal.history ?? [])]
      .reverse()
      .find((entry) => entry.observed_hash)?.observed_hash;
    if (observed !== undefined) return observed ?? null;
  }
  const observed = [...(proposal.history ?? [])]
    .reverse()
    .find((entry) => entry.observed_hash)?.observed_hash;
  return observed ?? proposal.base_hash ?? null;
}

function currentVersion(
  proposal: SkillProposal,
  hash: string | null,
): string | null {
  if (!hash) return null;
  if (
    hash === proposal.applied_hash &&
    proposal.status === "applied" &&
    !proposal.rolled_back_at
  ) {
    return proposal.applied_version ?? `sha256:${hash}`;
  }
  if (hash === proposal.base_hash) {
    return proposal.base_version ?? `sha256:${hash}`;
  }
  return `sha256:${hash}`;
}

function historyDetail(entry: SkillProposalHistoryEntry): string | null {
  const details = asRecord(entry.details);
  if (!details) return null;
  for (const key of ["reason", "expected_hash", "restored_base_hash"] as const) {
    const value = boundedText(details[key], 220);
    if (value) return `${key}: ${value}`;
  }
  return null;
}

function HashRow({
  label,
  hash,
  version,
}: {
  label: string;
  hash: string;
  version: string;
}) {
  return (
    <div className="grid gap-1 border-t py-2 first:border-t-0 sm:grid-cols-[8rem_1fr]">
      <div className="text-xs font-medium">{label}</div>
      <div className="min-w-0 space-y-1 text-[11px]">
        <div className="break-all font-mono">
          <span className="text-muted-foreground">hash: </span>
          {hash}
        </div>
        <div className="break-all font-mono">
          <span className="text-muted-foreground">version: </span>
          {version}
        </div>
      </div>
    </div>
  );
}

function ProposalDiff({
  base,
  proposed,
}: {
  base: SkillProposalContent | null;
  proposed: SkillProposalContent;
}) {
  const parts = useMemo(
    () =>
      diffLines(
        formatContentForDiff(base),
        formatContentForDiff(proposed),
      ),
    [base, proposed],
  );

  return (
    <div
      data-testid="proposal-diff"
      className="max-h-72 overflow-auto rounded-md border bg-muted/20 p-2 font-mono text-[11px]"
    >
      {parts.map((part, partIndex) =>
        part.value.split("\n").map((line, lineIndex, lines) => {
          if (lineIndex === lines.length - 1 && line === "") return null;
          const prefix = part.added ? "+ " : part.removed ? "- " : "  ";
          return (
            <div
              key={`${partIndex}:${lineIndex}`}
              className={
                part.added
                  ? "bg-primary/5 text-primary"
                  : part.removed
                    ? "bg-destructive/10 text-destructive"
                    : "text-muted-foreground"
              }
            >
              {prefix}
              {line}
            </div>
          );
        }),
      )}
    </div>
  );
}

function UsageReceiptView({
  receipt,
  loading,
  error,
}: {
  receipt: SkillUsageReceipt | null;
  loading: boolean;
  error?: Error;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="size-3 animate-spin" />
        使用実績を確認中...
      </div>
    );
  }
  if (error) {
    return (
      <p className="text-xs text-destructive">
        使用実績を取得できませんでした。
      </p>
    );
  }
  if (!receipt) {
    return (
      <p className="text-xs text-muted-foreground">
        実使用レシートはありません。Skill の一覧表示・推薦・候補提示は使用実績として扱いません。
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1">
        <Badge variant="outline">{receipt.invocation_path}</Badge>
        <Badge
          variant={receipt.outcome === "error" ? "destructive" : "secondary"}
        >
          {receipt.outcome}
        </Badge>
        <Badge variant="outline">{receipt.skill_scope}</Badge>
      </div>
      <dl className="grid gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2">
        <div>
          <dt className="inline font-medium">Skill: </dt>
          <dd className="inline">{receipt.skill_name}</dd>
        </div>
        <div>
          <dt className="inline font-medium">実行日時: </dt>
          <dd className="inline">{dateTime(receipt.created_at)}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="inline font-medium">実行時 hash: </dt>
          <dd className="inline break-all font-mono">{receipt.skill_hash}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="inline font-medium">実行時 version: </dt>
          <dd className="inline break-all font-mono">{receipt.skill_version}</dd>
        </div>
        {receipt.message_id && (
          <div className="sm:col-span-2">
            <dt className="inline font-medium">trusted message: </dt>
            <dd className="inline break-all font-mono">{receipt.message_id}</dd>
          </div>
        )}
        {receipt.agent_run_id && (
          <div className="sm:col-span-2">
            <dt className="inline font-medium">AgentRun: </dt>
            <dd className="inline break-all font-mono">
              {receipt.agent_run_id}
            </dd>
          </div>
        )}
        {receipt.session_id && (
          <div className="sm:col-span-2">
            <dt className="inline font-medium">session: </dt>
            <dd className="inline break-all font-mono">{receipt.session_id}</dd>
          </div>
        )}
      </dl>
      <p className="text-[11px] text-muted-foreground">
        生の provenance / extra フィールドは UI に展開しません。上記はサーバーが検証した実呼び出し識別子だけです。
      </p>
    </div>
  );
}

function SkillProposalDetail({
  proposal,
  receipt,
  receiptLoading,
  receiptError,
  onProposalChanged,
  onSkillChanged,
}: {
  proposal: SkillProposal;
  receipt: SkillUsageReceipt | null;
  receiptLoading: boolean;
  receiptError?: Error;
  onProposalChanged: (proposal: SkillProposal) => Promise<void>;
  onSkillChanged?: () => void | Promise<void>;
}) {
  const confirm = useConfirm();
  const persistedContent = normalizeContent(
    proposal.proposed_content,
    proposal.target_name,
  );
  const baseContent = proposal.base_snapshot
    ? normalizeContent(proposal.base_snapshot, proposal.target_name)
    : null;
  const [draft, setDraft] = useState<SkillProposalContent>(persistedContent);
  const [parametersText, setParametersText] = useState(
    JSON.stringify(persistedContent.parameters, null, 2),
  );
  const [rejectReason, setRejectReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const parameterResult = parseParameters(parametersText);
  const effectiveDraft = useMemo<SkillProposalContent>(() => {
    const parameters = parameterResult.ok
      ? parameterResult.value
      : { __invalid_json__: parametersText };
    if (proposal.target_scope === "project") {
      return {
        ...draft,
        name: proposal.target_name,
        aliases: [],
        examples: [],
        tags: [],
        parameters: {},
      };
    }
    return {
      ...draft,
      name: proposal.target_name,
      parameters,
    };
  }, [
    draft,
    parameterResult,
    parametersText,
    proposal.target_name,
    proposal.target_scope,
  ]);

  const persistedSerialized = JSON.stringify(persistedContent);
  const currentSerialized = parameterResult.ok
    ? JSON.stringify(effectiveDraft)
    : "__invalid__";
  const dirty = persistedSerialized !== currentSerialized;
  const editable = proposal.status === "pending" && busy === null;
  const evidenceRows = safeProposalEvidence(proposal);

  const observedHash = currentHash(proposal);
  const observedVersion = currentVersion(proposal, observedHash);

  const runMutation = async (
    key: string,
    action: () => Promise<{ proposal: SkillProposal }>,
    changesSkill = false,
  ) => {
    setBusy(key);
    setActionError(null);
    try {
      const result = await action();
      await onProposalChanged(result.proposal);
      if (changesSkill && onSkillChanged) {
        try {
          await onSkillChanged();
        } catch {
          // Proposal mutation already succeeded; list refresh can be retried manually.
        }
      }
    } catch (error) {
      if (error instanceof SkillLearningApiError && error.proposal) {
        try {
          await onProposalChanged(error.proposal);
        } catch {
          // Keep the safe mutation error visible even if list refresh fails.
        }
      }
      setActionError(
        error instanceof Error
          ? error.message
          : "Skill 提案の操作に失敗しました。",
      );
    } finally {
      setBusy(null);
    }
  };

  const handleSaveRevision = async () => {
    if (!parameterResult.ok) {
      setFieldError(parameterResult.message);
      return;
    }
    setFieldError(null);
    await runMutation("save", () =>
      skillLearningApi.reviseProposal(
        proposal.id,
        effectiveDraft,
        {
          source_type: "human_review",
          surface: "settings_skills",
          action: "proposal_revision",
        },
        proposalRevisionToken(proposal),
      ),
    );
  };

  const handleApply = async () => {
    if (dirty || !parameterResult.ok) return;
    if (
      !(await confirm({
        description: `Skill 提案「${proposal.target_name}」を明示的に適用しますか？`,
      }))
    ) {
      return;
    }
    await runMutation(
      "apply",
      () =>
        skillLearningApi.applyProposal(
          proposal.id,
          proposalRevisionToken(proposal),
        ),
      true,
    );
  };

  const handleReject = async () => {
    if (
      !(await confirm({
        description: `Skill 提案「${proposal.target_name}」を拒否しますか？`,
        destructive: true,
      }))
    ) {
      return;
    }
    await runMutation("reject", () =>
      skillLearningApi.rejectProposal(
        proposal.id,
        rejectReason,
        proposalRevisionToken(proposal),
      ),
    );
  };

  const handleRollback = async () => {
    if (
      !(await confirm({
        description: `適用済み Skill「${proposal.target_name}」を提案前の状態へロールバックしますか？`,
        destructive: true,
      }))
    ) {
      return;
    }
    await runMutation(
      "rollback",
      () =>
        skillLearningApi.rollbackProposal(
          proposal.id,
          proposalRevisionToken(proposal),
        ),
      true,
    );
  };

  const coreValue: SkillReviewContent = {
    name: draft.name,
    description: draft.description,
    prompt_template: draft.prompt_template,
    trigger_mode: draft.trigger_mode,
  };

  return (
    <div className="space-y-4">
      <div className="rounded-md border p-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={statusVariant(proposal.status)}>
            {STATUS_LABELS[proposal.status]} ({proposal.status})
          </Badge>
          <Badge variant="outline">
            {OPERATION_LABELS[proposal.operation]} ({proposal.operation})
          </Badge>
          <Badge variant="outline">
            {SCOPE_LABELS[proposal.target_scope]} ({proposal.target_scope})
          </Badge>
          <Badge variant="outline">{proposal.reason_type}</Badge>
          {proposal.rolled_back_at && (
            <Badge variant="secondary">ロールバック済み</Badge>
          )}
        </div>
        <div className="mt-2 space-y-1 text-[11px] text-muted-foreground">
          <p>
            対象:{" "}
            <span className="font-mono text-foreground">
              {proposal.target_path}
            </span>
          </p>
          {proposal.target_scope === "project" && (
            <p>
              Project:{" "}
              <span className="font-mono text-foreground">
                {maskedProjectId(proposal.project_id)}
              </span>
              {" — "}
              スコープは提案に固定され、操作時にバックエンドで ACL
              を再検証します。
            </p>
          )}
          <p>更新: {dateTime(proposal.updated_at)}</p>
        </div>
      </div>

      {proposal.status === "stale" && (
        <div className="flex items-start gap-2 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>
            canonical Skill が提案の基準版から変更されています。この stale
            提案から自動適用・自動ロールバックは行われません。
          </span>
        </div>
      )}

      <section className="space-y-1">
        <h4 className="text-xs font-semibold">Hash / version</h4>
        <div className="rounded-md border px-3">
          <HashRow
            label="Current（最終観測）"
            hash={observedHash ?? "不存在"}
            version={observedVersion ?? "不存在"}
          />
          <HashRow
            label="Base"
            hash={proposal.base_hash ?? "不存在"}
            version={proposal.base_version ?? "不存在"}
          />
          <HashRow
            label="Proposed"
            hash={proposal.applied_hash ?? "未適用のため canonical hash 未確定"}
            version={
              proposal.applied_version ??
              "明示的な apply で materialize された時点で確定"
            }
          />
        </div>
      </section>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold">提案内容</h4>
        <SkillContentReviewFields
          value={coreValue}
          onChange={(next) =>
            setDraft((current) => ({ ...current, ...next }))
          }
          readOnly={!editable}
          nameReadOnly
          promptLabel="スキル本文（prompt_template）"
        />

        <div className="space-y-1">
          <Label className="text-xs">紐づくツール（カンマ区切り）</Label>
          <Input
            value={draft.bound_tools.join(", ")}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                bound_tools: parseCommaList(event.target.value),
              }))
            }
            disabled={!editable}
            placeholder="tool-a, tool-b"
          />
        </div>

        {proposal.target_scope === "global" && (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label className="text-xs">エイリアス（カンマ区切り）</Label>
                <Input
                  value={draft.aliases.join(", ")}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      aliases: parseCommaList(event.target.value),
                    }))
                  }
                  disabled={!editable}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">タグ（カンマ区切り）</Label>
                <Input
                  value={draft.tags.join(", ")}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      tags: parseCommaList(event.target.value),
                    }))
                  }
                  disabled={!editable}
                />
              </div>
            </div>

            <div className="space-y-1">
              <Label className="text-xs">examples（1行1件）</Label>
              <Textarea
                value={draft.examples.join("\n")}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    examples: parseLineList(event.target.value),
                  }))
                }
                disabled={!editable}
                rows={3}
                className="font-mono text-xs"
              />
            </div>

            <div className="space-y-1">
              <Label className="text-xs">parameters（JSON object）</Label>
              <Textarea
                value={parametersText}
                onChange={(event) => {
                  setParametersText(event.target.value);
                  setFieldError(null);
                }}
                disabled={!editable}
                rows={6}
                className="font-mono text-xs"
              />
            </div>
          </>
        )}

        {fieldError && (
          <p className="rounded-md bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
            {fieldError}
          </p>
        )}

        {proposal.status === "pending" && (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-[11px] text-muted-foreground">
              編集内容は「提案編集を保存」で proposal
              に永続化されます。保存だけでは Skill に適用されません。
            </p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void handleSaveRevision()}
              disabled={!dirty || busy !== null}
            >
              {busy === "save" ? (
                <Loader2 className="mr-1 size-3 animate-spin" />
              ) : (
                <Save className="mr-1 size-3" />
              )}
              提案編集を保存
            </Button>
          </div>
        )}
      </section>

      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 text-xs font-semibold">
          <GitCompare className="size-3.5" />
          Base → Proposed diff
        </h4>
        <ProposalDiff base={baseContent} proposed={effectiveDraft} />
      </section>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold">根拠と出典</h4>
        {evidenceRows.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            UI に安全に表示できる bounded evidence はありません。
          </p>
        ) : (
          <dl className="rounded-md border px-3 py-2 text-[11px]">
            {evidenceRows.map((row, index) => (
              <div
                key={`${row.label}:${index}`}
                className="grid gap-1 border-t py-1.5 first:border-t-0 sm:grid-cols-[10rem_1fr]"
              >
                <dt className="font-medium">{row.label}</dt>
                <dd className="min-w-0 break-words">{row.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <section className="space-y-2 rounded-md border p-3">
        <h4 className="text-xs font-semibold">実使用レシート</h4>
        <UsageReceiptView
          receipt={receipt}
          loading={receiptLoading}
          error={receiptError}
        />
      </section>

      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 text-xs font-semibold">
          <History className="size-3.5" />
          履歴
        </h4>
        {proposal.history && proposal.history.length > 0 ? (
          <div className="max-h-52 space-y-1 overflow-auto rounded-md border p-2">
            {proposal.history.map((entry) => {
              const detail = historyDetail(entry);
              return (
                <div
                  key={entry.id}
                  className="rounded-md bg-muted/30 px-2 py-1.5 text-[11px]"
                >
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge variant="outline">#{entry.sequence}</Badge>
                    <span className="font-medium">{entry.event}</span>
                    <span className="text-muted-foreground">
                      {entry.from_status ?? "—"} → {entry.to_status ?? "—"}
                    </span>
                    <span className="ml-auto text-muted-foreground">
                      {dateTime(entry.created_at)}
                    </span>
                  </div>
                  {entry.observed_hash && (
                    <p className="mt-1 break-all font-mono text-muted-foreground">
                      observed: {entry.observed_hash}
                    </p>
                  )}
                  {entry.result_hash && (
                    <p className="break-all font-mono text-muted-foreground">
                      result: {entry.result_hash}
                    </p>
                  )}
                  {detail && (
                    <p className="mt-1 break-words text-muted-foreground">
                      {detail}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">履歴はありません。</p>
        )}
      </section>

      {actionError && (
        <div className="flex items-start gap-2 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>{actionError}</span>
        </div>
      )}

      {proposal.status === "pending" && (
        <div className="space-y-2 border-t pt-3">
          {dirty && (
            <p className="text-[11px] text-muted-foreground">
              未保存の編集があります。Apply の前に提案編集を保存してください。
            </p>
          )}
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="min-w-[14rem] flex-1 space-y-1">
              <Label className="text-xs">拒否理由（任意）</Label>
              <Input
                value={rejectReason}
                onChange={(event) => setRejectReason(event.target.value)}
                placeholder="レビューで拒否した理由"
                disabled={busy !== null}
              />
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => void handleReject()}
                disabled={busy !== null}
              >
                {busy === "reject" ? (
                  <Loader2 className="mr-1 size-3 animate-spin" />
                ) : (
                  <X className="mr-1 size-3" />
                )}
                拒否
              </Button>
              <Button
                size="sm"
                onClick={() => void handleApply()}
                disabled={dirty || !parameterResult.ok || busy !== null}
              >
                {busy === "apply" ? (
                  <Loader2 className="mr-1 size-3 animate-spin" />
                ) : (
                  <Check className="mr-1 size-3" />
                )}
                適用
              </Button>
            </div>
          </div>
        </div>
      )}

      {proposal.status === "applied" && !proposal.rolled_back_at && (
        <div className="flex justify-end border-t pt-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void handleRollback()}
            disabled={busy !== null}
          >
            {busy === "rollback" ? (
              <Loader2 className="mr-1 size-3 animate-spin" />
            ) : (
              <Undo2 className="mr-1 size-3" />
            )}
            ロールバック
          </Button>
        </div>
      )}
    </div>
  );
}

export function SkillProposalReview({
  onSkillChanged,
}: {
  onSkillChanged?: () => void | Promise<void>;
}) {
  const [statusFilter, setStatusFilter] = useState<
    "all" | SkillProposalStatus
  >("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const {
    data: listResponse,
    mutate: mutateList,
    isLoading: listLoading,
    error: listError,
  } = useSWR(
    `skill-proposals:${statusFilter}`,
    () =>
      skillLearningApi.listProposals({
        status: statusFilter === "all" ? undefined : statusFilter,
        limit: 100,
      }),
    { revalidateOnFocus: false },
  );
  const proposals = listResponse?.proposals ?? [];

  const {
    data: detailResponse,
    mutate: mutateDetail,
    isLoading: detailLoading,
    error: detailError,
  } = useSWR(
    selectedId ? `skill-proposal:${selectedId}` : null,
    () => skillLearningApi.getProposal(selectedId as string),
    { revalidateOnFocus: false },
  );
  const proposal = detailResponse?.proposal ?? null;

  const {
    data: receiptResponse,
    isLoading: receiptLoading,
    error: receiptError,
  } = useSWR(
    proposal?.receipt_id
      ? `skill-usage-receipt:${proposal.receipt_id}`
      : null,
    () => skillLearningApi.getUsageReceipt(proposal?.receipt_id as string),
    { revalidateOnFocus: false },
  );

  const handleProposalChanged = async (next: SkillProposal) => {
    await mutateDetail({ success: true, proposal: next }, false);
    await mutateList();
  };

  return (
    <section className="space-y-3" aria-label="Skill learning proposals">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium">学習提案</h3>
            {proposals.length > 0 && (
              <Badge variant="secondary" className="text-[10px]">
                {proposals.length}件
              </Badge>
            )}
          </div>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            自動適用は無効です。適用・拒否・ロールバックは明示操作でのみ実行します。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <AppSelect
            aria-label="提案ステータス"
            value={statusFilter}
            onChange={(event) =>
              setStatusFilter(
                event.target.value as "all" | SkillProposalStatus,
              )
            }
            size="sm"
          >
            <option value="all">すべて</option>
            <option value="pending">保留中</option>
            <option value="applied">適用済み</option>
            <option value="rejected">拒否済み</option>
            <option value="stale">陳腐化</option>
          </AppSelect>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void mutateList()}
            disabled={listLoading}
          >
            {listLoading ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <RefreshCw className="size-3" />
            )}
            <span className="ml-1">更新</span>
          </Button>
        </div>
      </div>

      {listError ? (
        <p className="rounded-md bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
          {listError instanceof Error
            ? listError.message
            : "Skill 提案を取得できませんでした。"}
        </p>
      ) : listLoading && !listResponse ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="size-3 animate-spin" />
          提案を取得中...
        </div>
      ) : proposals.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          該当する Skill 提案はありません。
        </p>
      ) : (
        <div className="max-h-72 space-y-2 overflow-auto">
          {proposals.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-label={`提案 ${item.target_name} を開く`}
              onClick={() => setSelectedId(item.id)}
              className="w-full rounded-md border p-2.5 text-left transition-colors hover:bg-muted/50"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {item.target_name}
                  </p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    <Badge
                      variant={statusVariant(item.status)}
                      className="text-[10px]"
                    >
                      {STATUS_LABELS[item.status]} ({item.status})
                    </Badge>
                    <Badge variant="outline" className="text-[10px]">
                      {OPERATION_LABELS[item.operation]} ({item.operation})
                    </Badge>
                    <Badge variant="outline" className="text-[10px]">
                      {SCOPE_LABELS[item.target_scope]}
                    </Badge>
                    {item.rolled_back_at && (
                      <Badge variant="secondary" className="text-[10px]">
                        rollback済み
                      </Badge>
                    )}
                  </div>
                </div>
                <span className="flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground">
                  <Clock3 className="size-3" />
                  {dateTime(item.updated_at)}
                </span>
              </div>
            </button>
          ))}
        </div>
      )}

      <Dialog
        open={selectedId !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedId(null);
        }}
      >
        <DialogContent size="lg" className="max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Skill 提案レビュー</DialogTitle>
          </DialogHeader>
          {detailError ? (
            <p className="rounded-md bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
              {detailError instanceof Error
                ? detailError.message
                : "Skill 提案の詳細を取得できませんでした。"}
            </p>
          ) : detailLoading || !proposal ? (
            <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              提案詳細を取得中...
            </div>
          ) : (
            <SkillProposalDetail
              key={`${proposal.id}:${proposal.updated_at ?? ""}:${proposal.status}:${proposal.rolled_back_at ?? ""}`}
              proposal={proposal}
              receipt={receiptResponse?.receipt ?? null}
              receiptLoading={receiptLoading}
              receiptError={
                receiptError instanceof Error ? receiptError : undefined
              }
              onProposalChanged={handleProposalChanged}
              onSkillChanged={onSkillChanged}
            />
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}
