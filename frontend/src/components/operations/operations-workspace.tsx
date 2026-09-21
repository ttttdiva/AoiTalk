"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  CheckCircle2,
  Clipboard,
  ClipboardCheck,
  FileUp,
  History,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Upload,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import {
  operationsApi,
  type ApplicationDraft,
  type CreateConnectionInput,
  type EngagementOpportunity,
  type OperationsAction,
  type OperationsArtifact,
  type OperationsAttempt,
  type OperationsConnection,
  type OpportunityDetail,
  type OpportunityEvaluation,
  type UpdateConnectionInput,
} from "@/lib/operations-api";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import {
  OPERATIONS_ALL_SECTIONS,
  OPERATIONS_SECTION_IDS,
  OPERATIONS_SECTIONS,
  OperationsWorkspaceNavigation,
  type OperationsSection,
} from "@/components/operations/operations-workspace-navigation";
import { MediaPersonaPanel } from "@/components/operations/media/persona-panel";
import { MediaResearchPanel } from "@/components/operations/media/research-panel";
import { MediaAutomationPanel } from "@/components/operations/media/automation-panel";
import { MediaGenerationPanel } from "@/components/operations/media/generation-panel";
import { MediaContentVariantPanel } from "@/components/operations/media/content-variant-panel";
import { MediaCalendarPanel } from "@/components/operations/media/calendar-panel";
import { MediaResultsPanel } from "@/components/operations/media/results-panel";
import { OperationsCommandCenter } from "@/components/operations/operations-command-center";
import { EmployeeWorkspace } from "@/components/operations/agents/employee-workspace";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { AppSelect } from "@/components/ui/app-select";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type ConnectionForm = {
  provider_key: string;
  display_name: string;
  remote_account_ref: string;
  project_id: string;
};

type EvaluationForm = {
  fit: string;
  estimated_effort_hours: string;
  estimated_cost: string;
  estimated_revenue: string;
  summary: string;
  risks: string;
  missing_requirements: string;
  evidence_refs: string;
};

type DraftForm = {
  message: string;
  offered_price: string;
  currency: string;
  delivery_estimate: string;
  artifact_version_ids: string;
};

type CompleteForm = {
  outcome: "succeeded" | "failed" | "uncertain";
  result_summary: string;
  remote_resource_id: string;
  remote_url: string;
  remote_status: string;
  evidence_note: string;
  confirmation_level: string;
  evidence_artifact_ids: string;
};

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `operations-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function asOptionalNumber(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : null;
}

function splitLines(value: string): string[] {
  return value
    .split(/[,\n]/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

function readableError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "Operations APIでエラーが発生しました";
}

function statusLabel(status: string | null | undefined): string {
  const value = status?.trim();
  if (!value) return "未設定";
  const labels: Record<string, string> = {
    pending: "保留",
    draft: "下書き",
    proposed: "提案",
    approved: "承認済み",
    rejected: "却下",
    approval_invalidated: "承認無効",
    running: "実行中",
    attempting: "実行中",
    succeeded: "成功",
    failed: "失敗",
    uncertain: "要照合",
    reconciled: "照合済み",
  };
  return labels[value.toLowerCase()] ?? value;
}

function StatusPill({ status }: { status: string | null | undefined }) {
  return (
    <span
      className="inline-flex items-center rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
      data-operation-status={status ?? "unknown"}
    >
      {statusLabel(status)}
    </span>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
      {readableError(error)}
    </div>
  );
}

function FieldLabel({ htmlFor, children }: { htmlFor: string; children: React.ReactNode }) {
  return (
    <label htmlFor={htmlFor} className="text-xs font-medium text-foreground">
      {children}
    </label>
  );
}

function ConnectionPanel({
  connections,
  loading,
  error,
  idempotencyKey,
  onReload,
  onSaved,
}: {
  connections: OperationsConnection[];
  loading: boolean;
  error: unknown;
  idempotencyKey: string;
  onReload: () => void;
  onSaved: (connection: OperationsConnection) => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingVersion, setEditingVersion] = useState<number>(1);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<ConnectionForm>({
    provider_key: "crowdworks",
    display_name: "CrowdWorks",
    remote_account_ref: "",
    project_id: "",
  });
  const [formError, setFormError] = useState<unknown>(null);

  const beginEdit = (connection?: OperationsConnection) => {
    if (!connection) {
      setEditingId(null);
      setEditingVersion(1);
      setForm({
        provider_key: "crowdworks",
        display_name: "CrowdWorks",
        remote_account_ref: "",
        project_id: "",
      });
      return;
    }
    setEditingId(connection.id);
    setEditingVersion(connection.version ?? 1);
    setForm({
      provider_key: connection.provider_key || "crowdworks",
      display_name: connection.display_name || "CrowdWorks",
      remote_account_ref: connection.remote_account_ref ?? "",
      project_id: connection.project_id ?? "",
    });
  };

  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!form.display_name.trim()) {
      setFormError(new Error("表示名を入力してください"));
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const providerKey = form.provider_key.trim() || "crowdworks";
      const displayName = form.display_name.trim();
      const remoteAccountRef = form.remote_account_ref.trim() || null;
      const result = editingId
        ? await operationsApi.updateConnection(editingId, {
          provider_key: providerKey,
          display_name: displayName,
          remote_account_ref: remoteAccountRef,
          expected_version: editingVersion,
        } satisfies UpdateConnectionInput, idempotencyKey)
        : await operationsApi.createConnection({
          provider_key: providerKey,
          display_name: displayName,
          remote_account_ref: remoteAccountRef,
          project_id: form.project_id.trim() || null,
        } satisfies CreateConnectionInput, idempotencyKey);
      onSaved(result);
      setEditingId(null);
      beginEdit();
      toast.success(editingId ? "Connectionを更新しました" : "Connectionを作成しました");
    } catch (saveError) {
      setFormError(saveError);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4" data-testid="operations-connections-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">Connections</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            CrowdWorksなど、外部サービスの接続先を登録します。認証情報はこの画面に入力・保存しません。
          </p>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={onReload} disabled={loading}>
          <RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 更新
        </Button>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(19rem,24rem)]">
        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">登録済みのConnections</CardTitle>
            <CardDescription>接続先は案件の作成時に選択できます。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 pt-1">
            <ErrorNotice error={error} />
            {loading && !connections.length ? (
              <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div>
            ) : connections.length ? (
              connections.map((connection) => (
                <div key={connection.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/70 bg-background/40 px-3 py-2.5">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="truncate text-sm font-medium">{connection.display_name}</span>
                      <StatusPill status={connection.auth_status} />
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {connection.provider_key || "provider"}
                      {connection.remote_account_ref ? ` · ${connection.remote_account_ref}` : ""}
                    </p>
                  </div>
                  <Button type="button" variant="ghost" size="sm" onClick={() => beginEdit(connection)}>
                    <Pencil className="size-3.5" /> 編集
                  </Button>
                </div>
              ))
            ) : (
              <EmptyState>まだConnectionがありません。右のフォームからCrowdWorksを登録してください。</EmptyState>
            )}
          </CardContent>
        </Card>

        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">{editingId ? "Connectionを編集" : "Manual Connectionを追加"}</CardTitle>
            <CardDescription>資格情報を扱わず、接続先の識別情報だけを登録します。</CardDescription>
          </CardHeader>
          <CardContent className="pt-1">
            <form className="space-y-3" onSubmit={save} aria-label="CrowdWorks connection form">
              <div className="space-y-1.5">
                <FieldLabel htmlFor="operations-provider-key">Provider</FieldLabel>
                <Input id="operations-provider-key" value={form.provider_key} onChange={(event) => setForm((current) => ({ ...current, provider_key: event.target.value }))} placeholder="crowdworks" />
              </div>
              <div className="space-y-1.5">
                <FieldLabel htmlFor="operations-connection-name">表示名</FieldLabel>
                <Input id="operations-connection-name" value={form.display_name} onChange={(event) => setForm((current) => ({ ...current, display_name: event.target.value }))} placeholder="CrowdWorks" required />
              </div>
              <div className="space-y-1.5">
                <FieldLabel htmlFor="operations-remote-account">Remote account reference（任意）</FieldLabel>
                <Input id="operations-remote-account" value={form.remote_account_ref} onChange={(event) => setForm((current) => ({ ...current, remote_account_ref: event.target.value }))} placeholder="表示用のアカウント識別子" />
              </div>
              <div className="space-y-1.5">
                <FieldLabel htmlFor="operations-connection-project">Project ID（作成時のみ・変更不可）</FieldLabel>
                <Input id="operations-connection-project" value={form.project_id} onChange={(event) => setForm((current) => ({ ...current, project_id: event.target.value }))} placeholder="project-id" disabled={Boolean(editingId)} />
              </div>
              <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-4 text-amber-800 dark:text-amber-200">
                認証情報・パスワード・トークンの入力欄はありません。実行時の資格情報はサーバー側の安全な設定を利用します。
              </p>
              <ErrorNotice error={formError} />
              <div className="flex justify-end gap-2">
                {editingId && <Button type="button" variant="ghost" size="sm" onClick={() => beginEdit()}>キャンセル</Button>}
                <Button type="submit" size="sm" disabled={saving}>
                  {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
                  {editingId ? "更新" : "登録"}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function OpportunityDetailPanel({
  detail,
  connections,
  idempotencyKey,
  onRefresh,
  onActionCreated,
  onDetailChanged,
}: {
  detail: OpportunityDetail;
  connections: OperationsConnection[];
  idempotencyKey: string;
  onRefresh: () => void;
  onActionCreated: (action: OperationsAction) => void;
  onDetailChanged: (detail: OpportunityDetail) => void;
}) {
  const [evaluationForm, setEvaluationForm] = useState<EvaluationForm>({ fit: "", estimated_effort_hours: "", estimated_cost: "", estimated_revenue: "", summary: "", risks: "", missing_requirements: "", evidence_refs: "" });
  const [draftForm, setDraftForm] = useState<DraftForm>({ message: "", offered_price: "", currency: "JPY", delivery_estimate: "", artifact_version_ids: "" });
  const [actionConnectionId, setActionConnectionId] = useState(detail.connection_id ?? connections[0]?.id ?? "");
  const [saving, setSaving] = useState<"evaluation" | "draft" | "action" | "artifact" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [artifactFile, setArtifactFile] = useState<File | null>(null);
  const [uploadedArtifacts, setUploadedArtifacts] = useState<OperationsArtifact[]>([]);

  useEffect(() => {
    if (!actionConnectionId && connections[0]?.id) setActionConnectionId(connections[0].id);
  }, [actionConnectionId, connections]);

  const createEvaluation = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving("evaluation");
    setError(null);
    try {
      const evaluation = await operationsApi.createEvaluation(detail.id, {
        fit: evaluationForm.fit.trim() || null,
        estimated_effort_hours: asOptionalNumber(evaluationForm.estimated_effort_hours),
        estimated_cost: asOptionalNumber(evaluationForm.estimated_cost),
        estimated_revenue: asOptionalNumber(evaluationForm.estimated_revenue),
        summary: evaluationForm.summary.trim() || null,
        risks: splitLines(evaluationForm.risks),
        missing_requirements: splitLines(evaluationForm.missing_requirements),
        evidence_refs: splitLines(evaluationForm.evidence_refs),
      }, idempotencyKey);
      onDetailChanged({ ...detail, evaluations: [...detail.evaluations, evaluation] });
      setEvaluationForm((current) => ({ ...current, summary: "", risks: "", missing_requirements: "", evidence_refs: "" }));
      toast.success("Evaluation versionを作成しました");
    } catch (createError) {
      setError(createError);
    } finally {
      setSaving(null);
    }
  };

  const createDraft = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!draftForm.message.trim()) {
      setError(new Error("Draft messageを入力してください"));
      return;
    }
    setSaving("draft");
    setError(null);
    try {
      const draft = await operationsApi.createDraft(detail.id, {
        message: draftForm.message.trim(),
        offered_price: asOptionalNumber(draftForm.offered_price),
        currency: draftForm.currency.trim() || null,
        delivery_estimate: draftForm.delivery_estimate.trim() || null,
        artifact_version_ids: splitLines(draftForm.artifact_version_ids),
      }, idempotencyKey);
      onDetailChanged({ ...detail, drafts: [...detail.drafts, draft] });
      setDraftForm((current) => ({ ...current, message: "", offered_price: "", delivery_estimate: "", artifact_version_ids: "" }));
      toast.success("Application Draft versionを作成しました");
    } catch (createError) {
      setError(createError);
    } finally {
      setSaving(null);
    }
  };

  const createAction = async (draft: ApplicationDraft) => {
    if (!actionConnectionId) {
      setError(new Error("Actionに使うConnectionを選択してください"));
      return;
    }
    setSaving("action");
    setError(null);
    try {
      const action = await operationsApi.createAction({ connection_id: actionConnectionId, application_draft_id: draft.id, idempotency_key: idempotencyKey }, idempotencyKey);
      onActionCreated(action);
      toast.success("Actionを作成しました。承認前にcanonical payloadを確認してください");
    } catch (createError) {
      setError(createError);
    } finally {
      setSaving(null);
    }
  };

  const uploadArtifact = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!artifactFile) {
      setError(new Error("アップロードするファイルを選択してください"));
      return;
    }
    setSaving("artifact");
    setError(null);
    try {
      const artifact = await operationsApi.uploadArtifact({ file: artifactFile, opportunity_id: detail.id, label: artifactFile.name }, idempotencyKey);
      setUploadedArtifacts((current) => [artifact, ...current.filter((item) => item.id !== artifact.id)]);
      setDraftForm((current) => {
        const ids = splitLines(current.artifact_version_ids);
        if (!ids.includes(artifact.id)) ids.push(artifact.id);
        return { ...current, artifact_version_ids: ids.join(", ") };
      });
      setArtifactFile(null);
      toast.success("Artifactをアップロードしました");
      onRefresh();
    } catch (uploadError) {
      setError(uploadError);
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className="space-y-4" data-testid="operations-opportunity-detail">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-base font-semibold">{detail.title || "Untitled opportunity"}</h3>
            <StatusPill status={detail.status} />
          </div>
          <p className="mt-1 text-xs text-muted-foreground">Opportunity ID: <code>{detail.id}</code></p>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={onRefresh}>
          <RefreshCw className="size-3.5" /> Timeline / detailを再読込
        </Button>
      </div>

      <Card size="sm">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="text-sm">Source（untrusted）</CardTitle>
          <CardDescription>外部サイトの内容は表示用データとして扱い、リンク先を自動実行しません。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 pt-1">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">Source URL</p>
              <p className="mt-1 break-all rounded-md border border-border/60 bg-muted/30 px-2.5 py-2 font-mono text-xs">{detail.source_url || "—"}</p>
            </div>
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground">Source hash</p>
              <p className="mt-1 break-all rounded-md border border-border/60 bg-muted/30 px-2.5 py-2 font-mono text-xs">{detail.source_hash || "—"}</p>
            </div>
          </div>
          <pre className="max-h-52 overflow-auto whitespace-pre-wrap rounded-md border border-border/60 bg-background/60 p-3 text-xs leading-5">{detail.source_text || "Source textはありません"}</pre>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">Evaluation versions</CardTitle>
            <CardDescription>適合度・見積り・リスクを版管理します。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 pt-1">
            {detail.evaluations.length ? detail.evaluations.map((evaluation: OpportunityEvaluation, index) => (
              <div key={evaluation.id || `${evaluation.version}-${index}`} className="rounded-md border border-border/70 bg-background/40 p-3 text-xs">
                <div className="flex items-center justify-between gap-2"><span className="font-semibold">v{evaluation.version ?? index + 1}</span><span className="text-muted-foreground">{evaluation.fit || "fit未評価"}</span></div>
                <p className="mt-2 whitespace-pre-wrap leading-5">{evaluation.summary || "概要なし"}</p>
                <div className="mt-2 grid gap-1 text-muted-foreground sm:grid-cols-3"><span>工数: {evaluation.estimated_effort_hours ?? "—"}h</span><span>費用: {evaluation.estimated_cost ?? "—"}</span><span>売上: {evaluation.estimated_revenue ?? "—"}</span></div>
                {(evaluation.risks?.length || evaluation.missing_requirements?.length) ? <div className="mt-2 space-y-1 text-muted-foreground"><p>Risks: {evaluation.risks?.join("、") || "—"}</p><p>Missing: {evaluation.missing_requirements?.join("、") || "—"}</p></div> : null}
              </div>
            )) : <EmptyState>Evaluation versionはまだありません。</EmptyState>}
            <form className="space-y-2 rounded-md border border-border/60 bg-muted/20 p-3" onSubmit={createEvaluation} aria-label="Create evaluation form">
              <div className="grid gap-2 sm:grid-cols-2">
                <Input aria-label="Fit" value={evaluationForm.fit} onChange={(event) => setEvaluationForm((current) => ({ ...current, fit: event.target.value }))} placeholder="fit（high / medium / low）" />
                <Input aria-label="Estimated effort hours" inputMode="decimal" value={evaluationForm.estimated_effort_hours} onChange={(event) => setEvaluationForm((current) => ({ ...current, estimated_effort_hours: event.target.value }))} placeholder="工数 (hours)" />
                <Input aria-label="Estimated cost" inputMode="decimal" value={evaluationForm.estimated_cost} onChange={(event) => setEvaluationForm((current) => ({ ...current, estimated_cost: event.target.value }))} placeholder="費用" />
                <Input aria-label="Estimated revenue" inputMode="decimal" value={evaluationForm.estimated_revenue} onChange={(event) => setEvaluationForm((current) => ({ ...current, estimated_revenue: event.target.value }))} placeholder="売上見込み" />
              </div>
              <Textarea aria-label="Evaluation summary" value={evaluationForm.summary} onChange={(event) => setEvaluationForm((current) => ({ ...current, summary: event.target.value }))} placeholder="評価サマリー" rows={2} />
              <div className="grid gap-2 sm:grid-cols-3"><Input aria-label="Risks" value={evaluationForm.risks} onChange={(event) => setEvaluationForm((current) => ({ ...current, risks: event.target.value }))} placeholder="Risks（カンマ区切り）" /><Input aria-label="Missing requirements" value={evaluationForm.missing_requirements} onChange={(event) => setEvaluationForm((current) => ({ ...current, missing_requirements: event.target.value }))} placeholder="Missing requirements" /><Input aria-label="Evidence refs" value={evaluationForm.evidence_refs} onChange={(event) => setEvaluationForm((current) => ({ ...current, evidence_refs: event.target.value }))} placeholder="Evidence refs" /></div>
              <div className="flex justify-end"><Button type="submit" size="sm" disabled={saving !== null}><Plus className="size-3.5" /> Evaluationを追加</Button></div>
            </form>
          </CardContent>
        </Card>

        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">Application Draft versions</CardTitle>
            <CardDescription>承認前に内容を編集・比較し、Actionへ昇格します。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 pt-1">
            {detail.drafts.length ? detail.drafts.map((draft: ApplicationDraft, index) => (
              <div key={draft.id || `${draft.version}-${index}`} className="rounded-md border border-border/70 bg-background/40 p-3 text-xs">
                <div className="flex items-center justify-between gap-2"><span className="font-semibold">v{draft.version ?? index + 1}</span><span className="text-muted-foreground">{draft.offered_price ?? "—"} {draft.currency || ""}</span></div>
                <p className="mt-2 whitespace-pre-wrap leading-5">{draft.message || "Draft messageなし"}</p>
                <p className="mt-2 text-muted-foreground">Delivery: {draft.delivery_estimate || "—"}</p>
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2"><span className="font-mono text-[10px] text-muted-foreground">{draft.id}</span><Button type="button" size="xs" onClick={() => void createAction(draft)} disabled={saving !== null || !draft.id}><ShieldCheck className="size-3" /> Actionを作成</Button></div>
              </div>
            )) : <EmptyState>Application Draft versionはまだありません。</EmptyState>}
            <form className="space-y-2 rounded-md border border-border/60 bg-muted/20 p-3" onSubmit={createDraft} aria-label="Create application draft form">
              <Textarea aria-label="Draft message" value={draftForm.message} onChange={(event) => setDraftForm((current) => ({ ...current, message: event.target.value }))} placeholder="応募文 / 提案文" rows={3} required />
              <div className="grid gap-2 sm:grid-cols-3"><Input aria-label="Offered price" inputMode="decimal" value={draftForm.offered_price} onChange={(event) => setDraftForm((current) => ({ ...current, offered_price: event.target.value }))} placeholder="価格" /><Input aria-label="Currency" value={draftForm.currency} onChange={(event) => setDraftForm((current) => ({ ...current, currency: event.target.value }))} placeholder="JPY" /><Input aria-label="Delivery estimate" value={draftForm.delivery_estimate} onChange={(event) => setDraftForm((current) => ({ ...current, delivery_estimate: event.target.value }))} placeholder="納期見込み" /></div>
              <Input aria-label="Artifact version IDs" value={draftForm.artifact_version_ids} onChange={(event) => setDraftForm((current) => ({ ...current, artifact_version_ids: event.target.value }))} placeholder="Artifact version IDs（カンマ区切り）" />
              <div className="flex justify-end"><Button type="submit" size="sm" disabled={saving !== null}><Plus className="size-3.5" /> Draftを追加</Button></div>
            </form>
          </CardContent>
        </Card>
      </div>

      <Card size="sm">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="flex items-center gap-1.5 text-sm"><FileUp className="size-4 text-primary" /> Artifact versions（任意）</CardTitle>
          <CardDescription>ファイルを添付すると、Draftのartifact_version_idsへ紐づけて参照できます。</CardDescription>
        </CardHeader>
        <CardContent className="pt-1">
          <form className="flex flex-wrap items-center gap-2" onSubmit={uploadArtifact} aria-label="Upload artifact form">
            <label className="inline-flex min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground hover:bg-muted/40">
              <Upload className="size-3.5 shrink-0" />
              <span className="truncate">{artifactFile?.name || "ファイルを選択"}</span>
              <input type="file" className="sr-only" onChange={(event) => setArtifactFile(event.target.files?.[0] ?? null)} />
            </label>
            <Button type="submit" size="sm" variant="outline" disabled={saving !== null || !artifactFile}><Upload className="size-3.5" /> Upload</Button>
          </form>
          {uploadedArtifacts.length ? <div className="mt-3 space-y-2" aria-label="Uploaded artifact versions">{uploadedArtifacts.map((artifact) => <div key={artifact.id} className="rounded-md border border-border/60 bg-muted/20 p-2 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{artifact.filename || "Artifact"}</span><span>{artifact.size_bytes ?? "—"} bytes</span></div><code className="mt-1 block break-all text-[10px] text-muted-foreground">ID {artifact.id} · SHA-256 {artifact.sha256 || "—"}</code></div>)}</div> : null}
          <div className="mt-3 flex flex-wrap items-center gap-2"><label htmlFor="operations-action-connection" className="text-xs text-muted-foreground">Action用Connection</label><AppSelect id="operations-action-connection" className="h-8 min-w-48 rounded-md border border-input bg-card px-2 text-xs" value={actionConnectionId} onChange={(event) => setActionConnectionId(event.target.value)}><option value="">Connectionを選択</option>{connections.map((connection) => <option key={connection.id} value={connection.id}>{connection.display_name}</option>)}</AppSelect></div>
          <ErrorNotice error={error} />
        </CardContent>
      </Card>
    </div>
  );
}

function OpportunitiesPanel({
  connections,
  idempotencyKey,
  onActionCreated,
}: {
  connections: OperationsConnection[];
  idempotencyKey: string;
  onActionCreated: (action: OperationsAction) => void;
}) {
  const [opportunities, setOpportunities] = useState<EngagementOpportunity[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<OpportunityDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [formError, setFormError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [title, setTitle] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const selectedConnection = connections.find((connection) => connection.id === connectionId) ?? null;

  const loadOpportunities = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await operationsApi.listOpportunities();
      setOpportunities(result);
      setSelectedId((current) => current && result.some((item) => item.id === current) ? current : result[0]?.id ?? null);
    } catch (loadError) {
      setError(loadError);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    setDetailLoading(true);
    setFormError(null);
    try {
      setDetail(await operationsApi.getOpportunity(id));
    } catch (loadError) {
      setFormError(loadError);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => { void loadOpportunities(); }, [loadOpportunities]);
  useEffect(() => { if (selectedId) void loadDetail(selectedId); else setDetail(null); }, [loadDetail, selectedId]);

  const ingest = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!sourceUrl.trim() || !sourceText.trim()) {
      setFormError(new Error("Source URLとSource textを入力してください"));
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const opportunity = await operationsApi.createOpportunity({ connection_id: selectedConnection?.id ?? null, project_id: selectedConnection?.project_id ?? null, source_url: sourceUrl.trim(), source_text: sourceText, title: title.trim() || "Imported opportunity" }, idempotencyKey);
      setOpportunities((current) => [opportunity, ...current]);
      setSelectedId(opportunity.id);
      setSourceUrl("");
      setSourceText("");
      setTitle("");
      toast.success("Opportunityを取り込みました");
    } catch (createError) {
      setFormError(createError);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4" data-testid="operations-opportunities-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-lg font-semibold tracking-tight">Opportunities</h2><p className="mt-1 text-sm text-muted-foreground">外部案件のURLと本文を取り込み、Evaluation / Draft / Actionを版管理します。</p></div>
        <Button type="button" variant="outline" size="sm" onClick={() => void loadOpportunities()} disabled={loading}><RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 更新</Button>
      </div>
      <Card size="sm">
        <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Sourceを取り込む</CardTitle><CardDescription>URLラベルは信頼せず、取得済みのSource textを保存します。</CardDescription></CardHeader>
        <CardContent className="pt-1"><form className="grid gap-2 lg:grid-cols-[minmax(11rem,0.7fr)_minmax(11rem,0.7fr)_minmax(0,1.8fr)_auto]" onSubmit={ingest} aria-label="Ingest opportunity form"><Input aria-label="Opportunity title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="タイトル（任意）" /><Input aria-label="Source URL" type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://…" required /><Textarea aria-label="Source text" value={sourceText} onChange={(event) => setSourceText(event.target.value)} placeholder="案件本文を貼り付け" rows={1} required /><Button type="submit" disabled={saving}><Plus className="size-3.5" /> Ingest</Button></form><div className="mt-2 flex flex-wrap items-center gap-2"><label htmlFor="operations-opportunity-connection" className="text-xs text-muted-foreground">Connection（任意）</label><AppSelect id="operations-opportunity-connection" className="h-8 min-w-48 rounded-md border border-input bg-card px-2 text-xs" value={connectionId} onChange={(event) => setConnectionId(event.target.value)}><option value="">Connectionを選択</option>{connections.map((connection) => <option key={connection.id} value={connection.id}>{connection.display_name}</option>)}</AppSelect></div><ErrorNotice error={formError} /></CardContent>
      </Card>

      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(15rem,22rem)_minmax(0,1fr)]">
        <Card size="sm" className="min-w-0">
          <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">取り込み済み</CardTitle><CardDescription>{opportunities.length}件</CardDescription></CardHeader>
          <CardContent className="space-y-1.5 pt-1">{loading && !opportunities.length ? <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div> : opportunities.length ? opportunities.map((opportunity) => <button key={opportunity.id} type="button" onClick={() => setSelectedId(opportunity.id)} className={cn("group relative w-full rounded-md border-l-2 px-3 py-2.5 text-left transition-colors", selectedId === opportunity.id ? "border-primary bg-primary/5" : "border-transparent hover:border-border hover:bg-muted/40")} aria-current={selectedId === opportunity.id ? "page" : undefined}><div className="flex items-center justify-between gap-2"><span className="truncate text-sm font-medium">{opportunity.title || "Untitled opportunity"}</span><StatusPill status={opportunity.status} /></div><span className="mt-1 block truncate font-mono text-[10px] text-muted-foreground">{opportunity.source_url || opportunity.id}</span></button>) : <EmptyState>Opportunityはまだありません。</EmptyState>}</CardContent>
        </Card>
        <Card size="sm" className="min-w-0"><CardContent className="relative pt-4">{detail && detail.id === selectedId ? <><OpportunityDetailPanel key={detail.id} detail={detail} connections={connections} idempotencyKey={idempotencyKey} onRefresh={() => selectedId && void loadDetail(selectedId)} onActionCreated={onActionCreated} onDetailChanged={setDetail} />{detailLoading ? <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-[inherit] bg-background/60" data-testid="operations-opportunity-detail-loading-overlay"><div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Detailを読み込み中…</div></div> : null}</> : detailLoading ? <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Detailを読み込み中…</div> : <EmptyState>左のOpportunityを選択してください。</EmptyState>}<ErrorNotice error={error} /></CardContent></Card>
      </div>
    </div>
  );
}

function ActionDetailPanel({
  action,
  idempotencyKey,
  onChanged,
  onReload,
}: {
  action: OperationsAction;
  idempotencyKey: string;
  onChanged: (action: OperationsAction) => void;
  onReload: () => void;
}) {
  const [saving, setSaving] = useState<"decision" | "revise" | "attempt" | "complete" | "reconcile" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [decisionReason, setDecisionReason] = useState("");
  const [revisionDraftId, setRevisionDraftId] = useState("");
  const [attempt, setAttempt] = useState<OperationsAttempt | null>(action.attempts?.[0] ?? null);
  const [completeForm, setCompleteForm] = useState<CompleteForm>({ outcome: "succeeded", result_summary: "", remote_resource_id: "", remote_url: "", remote_status: "", evidence_note: "", confirmation_level: "human_confirmed", evidence_artifact_ids: "" });
  const [reconcileResolution, setReconcileResolution] = useState("succeeded");
  const [reconcileEvidence, setReconcileEvidence] = useState("");
  const [reconcileEvidenceArtifactIds, setReconcileEvidenceArtifactIds] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setAttempt(action.attempts?.[0] ?? null);
  }, [action.attempts]);

  const expectedVersion = action.version ?? action.action_version ?? 1;
  const actionStatus = action.status?.toLowerCase() ?? "";
  const approved = actionStatus === "approved";
  const canApprove = ["proposed", "rejected", "failed"].includes(actionStatus);
  const canReject = ["proposed", "approved"].includes(actionStatus);
  const canRevise = ["proposed", "approved", "rejected", "failed"].includes(actionStatus);
  const frozenSourceUrl = action.source_url ?? (typeof action.payload?.source_url === "string" ? action.payload.source_url : null);
  const frozenSourceHash = action.source_hash ?? action.source_snapshot_hash ?? (typeof action.payload?.source_snapshot_hash === "string" ? action.payload.source_snapshot_hash : null);
  const approvalInvalidated = Boolean(
    action.status?.toLowerCase() === "approval_invalidated" ||
      action.approvals?.some((approval) => approval.decision === "invalidated") ||
      action.timeline?.some((event) => {
        const count = event.payload?.invalidated_approval_count;
        return event.event_type === "action.revised" && typeof count === "number" && count > 0;
      }),
  );
  const attemptInProgress = attempt?.status === "running" || attempt?.status === "started";
  const needsReconcile = action.status?.toLowerCase() === "uncertain" || attempt?.outcome === "uncertain" || attempt?.status === "uncertain";

  const mutateAction = async (operation: "approve" | "reject") => {
    setSaving("decision");
    setError(null);
    try {
      const result = operation === "approve" ? await operationsApi.approveAction(action.id, { expected_version: expectedVersion, reason: decisionReason.trim() || null }, idempotencyKey) : await operationsApi.rejectAction(action.id, { expected_version: expectedVersion, reason: decisionReason.trim() || null }, idempotencyKey);
      onChanged(result);
      toast.success(operation === "approve" ? "Actionを承認しました" : "Actionを却下しました");
    } catch (decisionError) {
      setError(decisionError);
    } finally {
      setSaving(null);
    }
  };

  const revise = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!revisionDraftId.trim()) {
      setError(new Error("新しいApplication Draft IDを入力してください"));
      return;
    }
    setSaving("revise");
    setError(null);
    try {
      const result = await operationsApi.reviseAction(action.id, { application_draft_id: revisionDraftId.trim(), expected_version: expectedVersion }, idempotencyKey);
      onChanged(result);
      toast.warning("Actionを改訂しました。以前のApprovalは無効化されています");
      setRevisionDraftId("");
    } catch (reviseError) {
      setError(reviseError);
    } finally {
      setSaving(null);
    }
  };

  const startAttempt = async () => {
    setSaving("attempt");
    setError(null);
    try {
      const result = await operationsApi.startAttempt(action.id, { expected_version: expectedVersion }, idempotencyKey);
      setAttempt(result);
      onChanged({ ...action, attempts: [result, ...(action.attempts ?? [])], status: "attempting", version: expectedVersion + 1 });
      toast.success("Manual attemptを開始しました");
    } catch (attemptError) {
      setError(attemptError);
    } finally {
      setSaving(null);
    }
  };

  const completeAttempt = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!attempt?.id) return;
    if (completeForm.outcome === "uncertain" && !completeForm.evidence_note.trim() && !splitLines(completeForm.evidence_artifact_ids).length) {
      setError(new Error("uncertainで完了するにはevidence noteまたはartifact IDが必要です"));
      return;
    }
    setSaving("complete");
    setError(null);
    try {
      const result = await operationsApi.completeAttempt(action.id, attempt.id, { expected_version: expectedVersion, outcome: completeForm.outcome, result_summary: completeForm.result_summary.trim() || null, remote_resource_id: completeForm.remote_resource_id.trim() || null, remote_url: completeForm.remote_url.trim() || null, remote_status: completeForm.remote_status.trim() || null, evidence_note: completeForm.evidence_note.trim() || null, confirmation_level: completeForm.confirmation_level.trim() || null, evidence_artifact_ids: splitLines(completeForm.evidence_artifact_ids) }, idempotencyKey);
      onChanged(result);
      setAttempt(result.attempts?.[0] ?? attempt);
      onReload();
      toast.success(`Attemptを${completeForm.outcome}で完了しました`);
    } catch (completeError) {
      setError(completeError);
    } finally {
      setSaving(null);
    }
  };

  const reconcile = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!reconcileEvidence.trim()) {
      setError(new Error("照合エビデンスを入力してください"));
      return;
    }
    setSaving("reconcile");
    setError(null);
    try {
      const result = await operationsApi.reconcileAction(action.id, { expected_version: expectedVersion, outcome: reconcileResolution, evidence_note: reconcileEvidence.trim(), evidence_artifact_ids: splitLines(reconcileEvidenceArtifactIds) }, idempotencyKey);
      onChanged(result);
      setReconcileEvidence("");
      setReconcileEvidenceArtifactIds("");
      toast.success("不確実なAttemptを照合しました");
    } catch (reconcileError) {
      setError(reconcileError);
    } finally {
      setSaving(null);
    }
  };

  const copyFrozenPayload = async () => {
    const value = JSON.stringify({ payload: action.payload ?? {}, source_url: frozenSourceUrl, source_snapshot_hash: frozenSourceHash, payload_hash: action.payload_hash ?? null, artifact_hashes: action.artifact_hashes ?? [] }, null, 2);
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setError(new Error("クリップボードへのコピーに失敗しました"));
    }
  };

  return (
    <div className="space-y-4" data-testid="operations-action-detail">
      <div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h3 className="truncate text-base font-semibold">Action <code className="font-mono text-xs font-normal">{action.id}</code></h3><StatusPill status={action.status} /></div><p className="mt-1 text-xs text-muted-foreground">version {expectedVersion} · action version {action.action_version ?? "—"}</p></div><Button type="button" size="sm" variant="outline" onClick={onReload}><RefreshCw className="size-3.5" /> Timelineを再読込</Button></div>

      <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><ClipboardCheck className="size-4 text-primary" /> Exact canonical payload</CardTitle><CardDescription>承認対象のpayloadとhashを確認してください。承認後はこの内容が凍結されます。</CardDescription></CardHeader><CardContent className="space-y-3 pt-1"><pre data-testid="canonical-payload" className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md border border-border/70 bg-background/60 p-3 font-mono text-xs leading-5">{JSON.stringify(action.payload ?? {}, null, 2)}</pre><div className="grid gap-2 sm:grid-cols-2"><div className="rounded-md border border-border/60 bg-muted/20 p-2"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">payload hash</p><code className="mt-1 block break-all text-xs">{action.payload_hash || "—"}</code></div><div className="rounded-md border border-border/60 bg-muted/20 p-2"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">artifact hashes</p><code className="mt-1 block break-all text-xs">{action.artifact_hashes?.join(", ") || "—"}</code></div></div><div className="flex flex-wrap items-center gap-2"><span className="text-xs text-muted-foreground">Frozen source URL: <span className="font-mono">{frozenSourceUrl || "—"}</span></span><span className="text-xs text-muted-foreground">Source hash: <span className="font-mono">{frozenSourceHash || "—"}</span></span>{approved && <Button type="button" size="sm" variant="outline" onClick={() => void copyFrozenPayload()}>{copied ? <Check className="size-3.5" /> : <Clipboard className="size-3.5" />}{copied ? "コピーしました" : "Frozen payload + source URLをコピー"}</Button>}</div></CardContent></Card>

      <div className="grid gap-4 xl:grid-cols-2"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Approval</CardTitle><CardDescription>Decisionはexpected versionを含む明示的な操作です。</CardDescription></CardHeader><CardContent className="space-y-3 pt-1"><Textarea aria-label="Approval reason" value={decisionReason} onChange={(event) => setDecisionReason(event.target.value)} placeholder="理由（任意）" rows={2} /><div className="flex flex-wrap gap-2"><Button type="button" size="sm" onClick={() => void mutateAction("approve")} disabled={saving !== null || !canApprove}><CheckCircle2 className="size-3.5" /> Approve</Button><Button type="button" size="sm" variant="destructive" onClick={() => void mutateAction("reject")} disabled={saving !== null || !canReject}><XCircle className="size-3.5" /> Reject</Button></div>{approved ? <p className="rounded-md border border-emerald-500/30 bg-emerald-500/5 px-2.5 py-2 text-xs text-emerald-800 dark:text-emerald-200">承認済み。Frozen payloadをコピーしてからManual attemptを開始できます。</p> : null}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Revise action</CardTitle><CardDescription>新しいDraftへ改訂すると、既存のApprovalは無効化されます。</CardDescription></CardHeader><CardContent className="pt-1"><form className="space-y-2" onSubmit={revise}><Input aria-label="New application draft ID" value={revisionDraftId} onChange={(event) => setRevisionDraftId(event.target.value)} placeholder="new application draft ID" required /><Button type="submit" size="sm" variant="outline" disabled={saving !== null || !canRevise}><Pencil className="size-3.5" /> Revise</Button></form>{approvalInvalidated ? <p className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-xs text-amber-800 dark:text-amber-200">Approval invalidated — 改訂後のpayloadを再承認してください。</p> : null}</CardContent></Card></div>

      <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Manual attempt</CardTitle><CardDescription>承認後に開始し、結果を succeeded / failed / uncertain で記録します。</CardDescription></CardHeader><CardContent className="space-y-3 pt-1">{attempt ? <div className="rounded-md border border-border/70 bg-background/40 p-3 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-mono">Attempt {attempt.id}</span><StatusPill status={attempt.outcome || attempt.status} /></div><p className="mt-1 text-muted-foreground">started {attempt.started_at || "—"} · completed {attempt.completed_at || "—"}</p>{attempt.evidence_note ? <p className="mt-2 whitespace-pre-wrap">Evidence: {attempt.evidence_note}</p> : null}</div> : <p className="text-sm text-muted-foreground">Attemptはまだありません。</p>}{approved && !attemptInProgress && <Button type="button" size="sm" onClick={() => void startAttempt()} disabled={saving !== null}><Plus className="size-3.5" /> Manual attemptを開始</Button>}{attemptInProgress && <form className="space-y-2 rounded-md border border-border/60 bg-muted/20 p-3" onSubmit={completeAttempt} aria-label="Complete manual attempt form"><div className="grid gap-2 sm:grid-cols-2"><AppSelect aria-label="Attempt outcome" className="h-8 rounded-md border border-input bg-card px-2 text-xs" value={completeForm.outcome} onChange={(event) => setCompleteForm((current) => ({ ...current, outcome: event.target.value as CompleteForm["outcome"] }))}><option value="succeeded">succeeded</option><option value="failed">failed</option><option value="uncertain">uncertain</option></AppSelect><AppSelect aria-label="Confirmation level" value={completeForm.confirmation_level} onChange={(event) => setCompleteForm((current) => ({ ...current, confirmation_level: event.target.value }))}><option value="human_confirmed">human_confirmed</option><option value="provider_confirmed">provider_confirmed</option></AppSelect></div><Textarea aria-label="Result summary" value={completeForm.result_summary} onChange={(event) => setCompleteForm((current) => ({ ...current, result_summary: event.target.value }))} placeholder="結果サマリー" rows={2} /><div className="grid gap-2 sm:grid-cols-3"><Input aria-label="Remote resource ID" value={completeForm.remote_resource_id} onChange={(event) => setCompleteForm((current) => ({ ...current, remote_resource_id: event.target.value }))} placeholder="remote resource ID" /><Input aria-label="Remote URL" value={completeForm.remote_url} onChange={(event) => setCompleteForm((current) => ({ ...current, remote_url: event.target.value }))} placeholder="remote URL" /><Input aria-label="Remote status" value={completeForm.remote_status} onChange={(event) => setCompleteForm((current) => ({ ...current, remote_status: event.target.value }))} placeholder="remote status" /></div><Textarea aria-label="Attempt evidence note" value={completeForm.evidence_note} onChange={(event) => setCompleteForm((current) => ({ ...current, evidence_note: event.target.value }))} placeholder="Evidence note" rows={2} /><Input aria-label="Evidence artifact IDs" value={completeForm.evidence_artifact_ids} onChange={(event) => setCompleteForm((current) => ({ ...current, evidence_artifact_ids: event.target.value }))} placeholder="Evidence artifact IDs（任意・カンマ区切り）" /><Button type="submit" size="sm" disabled={saving !== null}><Save className="size-3.5" /> Attemptを完了</Button></form>}{needsReconcile && <form className="space-y-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-3" onSubmit={reconcile} aria-label="Reconcile uncertain attempt form"><p className="text-xs font-medium text-amber-800 dark:text-amber-200">Uncertain outcomeを照合</p><div className="grid gap-2 sm:grid-cols-[12rem_minmax(0,1fr)]"><AppSelect aria-label="Reconcile resolution" className="h-8 rounded-md border border-input bg-card px-2 text-xs" value={reconcileResolution} onChange={(event) => setReconcileResolution(event.target.value)}><option value="succeeded">succeeded</option><option value="failed">failed</option></AppSelect><Textarea aria-label="Reconcile evidence" value={reconcileEvidence} onChange={(event) => setReconcileEvidence(event.target.value)} placeholder="照合エビデンス（必須）" rows={2} required /></div><Input aria-label="Reconcile evidence artifact IDs" value={reconcileEvidenceArtifactIds} onChange={(event) => setReconcileEvidenceArtifactIds(event.target.value)} placeholder="Evidence artifact IDs（任意・カンマ区切り）" /><Button type="submit" size="sm" variant="outline" disabled={saving !== null}><History className="size-3.5" /> Reconcile</Button></form>}{error ? <ErrorNotice error={error} /> : null}</CardContent></Card>

      <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Timeline</CardTitle><CardDescription>作成・改訂・承認・Attemptのイベントを再読込できます。</CardDescription></CardHeader><CardContent className="pt-1">{action.timeline?.length ? <ol className="space-y-2 border-l border-border pl-4">{action.timeline.map((entry, index) => <li key={entry.id || `${entry.created_at}-${index}`} className="relative text-xs"><span className="absolute -left-[1.18rem] top-1.5 size-2 rounded-full border border-primary bg-background" /><div className="flex flex-wrap justify-between gap-2"><span className="font-medium">{entry.event_type || entry.type || "event"}</span><time className="text-muted-foreground">{entry.created_at || "—"}</time></div>{entry.detail ? <p className="mt-0.5 whitespace-pre-wrap text-muted-foreground">{entry.detail}</p> : null}</li>)}</ol> : <EmptyState>Timeline eventはまだありません。</EmptyState>}</CardContent></Card>
    </div>
  );
}

function ActionsPanel({ initialActionId, reloadToken, idempotencyKey }: { initialActionId: string | null; reloadToken: number; idempotencyKey: string }) {
  const [actions, setActions] = useState<OperationsAction[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initialActionId);
  const [detail, setDetail] = useState<OperationsAction | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const loadActions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await operationsApi.listActions();
      setActions(result);
      setSelectedId((current) => {
        const desired = initialActionId || current;
        return desired && result.some((item) => item.id === desired) ? desired : result[0]?.id ?? null;
      });
    } catch (loadError) {
      setError(loadError);
    } finally {
      setLoading(false);
    }
  }, [initialActionId]);

  const loadDetail = useCallback(async (id: string) => {
    setDetailLoading(true);
    try {
      setDetail(await operationsApi.getAction(id));
    } catch (loadError) {
      setError(loadError);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const applyActionChange = useCallback((next: OperationsAction) => {
    setDetail(next);
    setActions((current) => current.map((item) => item.id === next.id ? next : item));
  }, []);

  useEffect(() => { void loadActions(); }, [loadActions, reloadToken]);
  useEffect(() => { if (selectedId) void loadDetail(selectedId); else setDetail(null); }, [loadDetail, selectedId]);

  return (
    <div className="space-y-4" data-testid="operations-actions-panel">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold tracking-tight">Approvals / Actions</h2><p className="mt-1 text-sm text-muted-foreground">Canonical payloadを確認し、承認・改訂・手動実行・不確実結果の照合を行います。</p></div><Button type="button" variant="outline" size="sm" onClick={() => void loadActions()} disabled={loading}><RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> Timeline / actionsを更新</Button></div>
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(15rem,22rem)_minmax(0,1fr)]"><Card size="sm" className="min-w-0"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Action一覧</CardTitle><CardDescription>{actions.length}件 · 明示的なidempotency keyで更新</CardDescription></CardHeader><CardContent className="space-y-1.5 pt-1">{loading && !actions.length ? <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div> : actions.length ? actions.map((action) => <button key={action.id} type="button" className={cn("group relative w-full rounded-md border-l-2 px-3 py-2.5 text-left transition-colors", selectedId === action.id ? "border-primary bg-primary/5" : "border-transparent hover:border-border hover:bg-muted/40")} aria-current={selectedId === action.id ? "page" : undefined} onClick={() => setSelectedId(action.id)}><div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-xs">{action.id}</span><StatusPill status={action.status} /></div><p className="mt-1 text-[10px] text-muted-foreground">v{action.version ?? action.action_version ?? "—"} · draft {action.application_draft_id || "—"}</p></button>) : <EmptyState>Actionはまだありません。OpportunitiesのDraftから作成できます。</EmptyState>}<ErrorNotice error={error} /></CardContent></Card><Card size="sm" className="min-w-0"><CardContent className="pt-4">{detailLoading ? <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Action detailを読み込み中…</div> : detail ? <ActionDetailPanel action={detail} idempotencyKey={idempotencyKey} onChanged={applyActionChange} onReload={() => selectedId && void loadDetail(selectedId)} /> : <EmptyState>左のActionを選択してください。</EmptyState>}</CardContent></Card></div>
    </div>
  );
}

export function OperationsWorkspace() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const runtime = useOptionalRuntimeContext();
  const companyRuntimeFeatures = runtime?.runtimeFeatures?.application_features;
  const companyRuntimeEnabled =
    companyRuntimeFeatures?.virtual_company === true &&
    companyRuntimeFeatures?.autonomous_agent_runtime === true;

  const requestedSection =
    searchParams?.get("tab") as OperationsSection | null;

  const isKnownSection = (value: OperationsSection | null): value is OperationsSection =>
    Boolean(value && OPERATIONS_SECTION_IDS.includes(value));

  const [section, setSection] = useState<OperationsSection>(
    isKnownSection(requestedSection)
      ? requestedSection
      : "overview",
  );

  const [idempotencyKey, setIdempotencyKey] =
    useState(newIdempotencyKey);
  const [connections, setConnections] =
    useState<OperationsConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] =
    useState(true);
  const [connectionsError, setConnectionsError] =
    useState<unknown>(null);
  const [focusedActionId, setFocusedActionId] =
    useState<string | null>(null);
  const [actionsReloadToken, setActionsReloadToken] =
    useState(0);

  useEffect(() => {
    const next =
      isKnownSection(requestedSection)
        ? requestedSection
        : "overview";

    setSection(next);
  }, [requestedSection]);

  const loadConnections = useCallback(async () => {
    setConnectionsLoading(true);
    setConnectionsError(null);

    try {
      setConnections(
        await operationsApi.listConnections(),
      );
    } catch (loadError) {
      setConnectionsError(loadError);
    } finally {
      setConnectionsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConnections();
  }, [loadConnections]);

  const selectSection = useCallback(
    (next: OperationsSection) => {
      setSection(next);
      const currentQuery =
        searchParams && typeof searchParams.toString === "function"
          ? searchParams.toString()
          : "";
      const params = new URLSearchParams(
        currentQuery === "[object Object]" ? "" : currentQuery,
      );
      if (next === "overview") params.delete("tab");
      else params.set("tab", next);
      const query = params.toString();
      router.replace(`/operations${query ? `?${query}` : ""}`, { scroll: false });
    },
    [router, searchParams],
  );

  const navigation = useMemo(
    () => (
      <aside
        className="ao-workspace-nav-panel"
        data-shell-slot="workspace-navigation"
        data-workspace="operations"
      >
        <div className="flex min-h-0 flex-1 flex-col bg-card/70">
          <div className="border-b border-border px-4 pb-4 pt-4">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-sm font-semibold tracking-tight text-foreground">
                Operations
              </h2>

              <span className="rounded-[4px] border border-primary/35 bg-primary/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.14em] text-primary">
                {section === "overview" || section === "agents" || section === "work" || section === "activity"
                  ? "Command"
                  : section === "personas" || section === "research" || section === "generation" || section === "pipeline" || section === "calendar" || section === "results"
                  ? "Media"
                  : "Manual"}
              </span>
            </div>

            <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
              {section === "overview" || section === "agents" || section === "work" || section === "activity"
                ? "会社全体のAgent・Work・Activityを確認"
                : section === "personas" || section === "research" || section === "generation" || section === "pipeline" || section === "calendar" || section === "results"
                ? "発信人格と媒体情報を管理"
                : "外部案件の安全な実行ワークフロー"}
            </p>
          </div>

          <OperationsWorkspaceNavigation
            activeSection={section}
            onSelect={selectSection}
            companyRuntimeEnabled={companyRuntimeEnabled}
          />

          <div className="mt-auto border-t border-border px-4 py-3">
            <p className="text-[11px] leading-4 text-muted-foreground">
              {section === "overview" || section === "agents" || section === "work" || section === "activity"
                ? section === "agents" ? "AI社員の職務・所属・自動化を管理します。" : "Canonical ledgerのread-only projectionです。"
                : section === "personas" || section === "research" || section === "generation" || section === "pipeline" || section === "calendar" || section === "results"
                ? "Characterは入力された内容だけを保存し、Revision履歴を上書きしません。"
                : "Credentialsは入力せず、承認済みpayloadだけを手動実行します。"}
            </p>
          </div>
        </div>
      </aside>
    ),
    [companyRuntimeEnabled, section, selectSection],
  );

  useWorkspaceShellRegistration({
    id: "operations-workspace",
    routeKey: pathname || "/operations",
    workspaceNavigation: navigation,
    priority: 30,
  });

  const handleActionCreated = (
    action: OperationsAction,
  ) => {
    setFocusedActionId(action.id || null);
    setActionsReloadToken((value) => value + 1);
    setIdempotencyKey(newIdempotencyKey());
    selectSection("actions");
  };

  return (
    <div
      className="flex min-h-full min-w-0 w-full flex-col overflow-hidden bg-background"
      data-testid="operations-workspace"
    >
      <div className="border-b border-border bg-card/40 px-4 py-3 lg:hidden">
        <OperationsWorkspaceNavigation
          activeSection={section}
          onSelect={selectSection}
          companyRuntimeEnabled={companyRuntimeEnabled}
          compact
        />
      </div>

      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1440px] space-y-5 p-4 pb-12 sm:p-6">
          {section === "overview" || section === "agents" || section === "work" || section === "activity" ? null : section === "personas" ? (
            <header>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                Media Operations
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">
                Personas
              </h1>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                発信人格（Persona / Brand）・対象読者・文体・利用プラットフォームを管理します。
              </p>
            </header>
          ) : section === "research" ? (
            <header>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                Media Operations
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">
                Research
              </h1>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                ResearchRoutine → Run snapshot → Evidence付きFinding → Editorial traceを管理します。
              </p>
            </header>
          ) : section === "automation" ? (
            <header>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                Media Operations
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">
                Automation
              </h1>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                AoiTalkがTheme・Research・Concept・承認・Generation Studio実行をdurableにオーケストレーションします。
              </p>
            </header>
          ) : section === "generation" ? (
            <header>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                Media Operations
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">
                Creative / Generation
              </h1>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                PersonaとCreative Recipeのexact revisionを固定し、画像生成のRunとOutput provenanceを確認します。
              </p>
            </header>
          ) : section === "pipeline" ? (
            <header>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                Media Operations / Pipeline
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">
                Content Pipeline
              </h1>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                ContentItemからPlatform別のtyped variantを作り、immutable revision・QA・Rights・Readinessを確認します。
              </p>
            </header>
          ) : section === "calendar" ? (
            <header>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                Media Operations / Calendar
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">カレンダー</h1>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                調査・編集・生成・公開・レビューの予定と、人手で次に行うことを確認します。
              </p>
            </header>
          ) : section === "results" ? (
            <header>
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                Media Operations / Results
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">結果</h1>
              <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                Metrics・Experiment・Revenue・学習提案を、保存済みEvidenceに紐づけて確認します。
              </p>
            </header>
          ) : (
            <header className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">
                  Operations workspace
                </p>
                <h1 className="mt-1 text-2xl font-semibold tracking-tight">
                  Engagement Operations
                </h1>
                <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
                  Connections → Opportunities → Approval / Actionを一つの監査可能な流れで扱います。
                </p>
              </div>

              <div className="flex min-w-[18rem] flex-col items-stretch gap-1.5 sm:items-end">
                <label
                  htmlFor="operations-idempotency-key"
                  className="text-[11px] font-medium text-muted-foreground"
                >
                  Idempotency key（明示）
                </label>

                <div className="flex w-full max-w-md gap-1.5">
                  <Input
                    id="operations-idempotency-key"
                    value={idempotencyKey}
                    onChange={(event) =>
                      setIdempotencyKey(
                        event.target.value,
                      )
                    }
                    aria-describedby="operations-idempotency-help"
                    className="font-mono text-xs"
                  />

                  <Button
                    type="button"
                    size="icon-sm"
                    variant="outline"
                    title="新しいidempotency keyを生成"
                    aria-label="新しいidempotency keyを生成"
                    onClick={() =>
                      setIdempotencyKey(
                        newIdempotencyKey(),
                      )
                    }
                  >
                    <RefreshCw className="size-3.5" />
                  </Button>
                </div>

                <p
                  id="operations-idempotency-help"
                  className="text-[10px] text-muted-foreground"
                >
                  同じキーで再送すると重複作成を避けられます。
                </p>
              </div>
            </header>
          )}

          <div
            role="tablist"
            aria-label="Operations sections"
            className="flex min-w-0 gap-1 overflow-x-auto rounded-lg border border-border bg-muted/30 p-1"
          >
            {OPERATIONS_SECTIONS.map(
              ({ id, label, icon: Icon }) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={section === id}
                  aria-controls={`operations-panel-${id}`}
                  onClick={() => selectSection(id)}
                  className={cn(
                    "inline-flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                    section === id
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
                  )}
                >
                  <Icon
                    className="size-3.5"
                    aria-hidden="true"
                  />
                  {label}
                </button>
              ),
            )}
          </div>

          <div
            id={`operations-panel-${section}`}
            role="tabpanel"
            aria-label={
              OPERATIONS_ALL_SECTIONS.find(
                (item) => item.id === section,
              )?.label
            }
          >
            {section === "agents" ? <EmployeeWorkspace /> : null}
            {section === "overview" || section === "work" || section === "activity" ? (
              <OperationsCommandCenter view={section} />
            ) : null}

            {section === "personas" ? (
              <MediaPersonaPanel />
            ) : null}

            {section === "research" ? (
              <MediaResearchPanel />
            ) : null}

            {section === "automation" ? (
              <MediaAutomationPanel />
            ) : null}

            {section === "generation" ? (
              <MediaGenerationPanel />
            ) : null}

            {section === "pipeline" ? (
              <MediaContentVariantPanel />
            ) : null}

            {section === "calendar" ? (
              <MediaCalendarPanel />
            ) : null}

            {section === "results" ? (
              <MediaResultsPanel />
            ) : null}

            {section === "connections" ? (
              <ConnectionPanel
                connections={connections}
                loading={connectionsLoading}
                error={connectionsError}
                idempotencyKey={idempotencyKey}
                onReload={() =>
                  void loadConnections()
                }
                onSaved={(connection) =>
                  setConnections((current) => {
                    const index =
                      current.findIndex(
                        (item) =>
                          item.id === connection.id,
                      );

                    if (index < 0) {
                      return [
                        connection,
                        ...current,
                      ];
                    }

                    const next = [...current];
                    next[index] = connection;
                    return next;
                  })
                }
              />
            ) : null}

            {section === "opportunities" ? (
              <OpportunitiesPanel
                connections={connections}
                idempotencyKey={idempotencyKey}
                onActionCreated={
                  handleActionCreated
                }
              />
            ) : null}

            {section === "actions" ? (
              <ActionsPanel
                initialActionId={focusedActionId}
                reloadToken={actionsReloadToken}
                idempotencyKey={idempotencyKey}
              />
            ) : null}
          </div>
        </div>
      </main>
    </div>
  );
}
