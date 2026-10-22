"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
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
  operationsSectionFromQuery,
  usesOperationsMap,
  OperationsWorkspaceNavigation,
  type OperationsSection,
} from "@/components/operations/operations-workspace-navigation";
import { PersonaWorkspace } from "@/components/operations/media/persona-workspace";
import { MediaPortfolioSummary } from "@/components/operations/media/media-portfolio-summary";
import { MediaPersonaPanel } from "@/components/operations/media/persona-panel";
import { MediaResearchPanel } from "@/components/operations/media/research-panel";
import { MediaBootstrapImportPanel } from "@/components/operations/media/media-bootstrap-import";
import { MediaAutomationPanel } from "@/components/operations/media/automation-panel";
import { MediaGenerationPanel } from "@/components/operations/media/generation-panel";
import { MediaContentVariantPanel } from "@/components/operations/media/content-variant-panel";
import { MediaCalendarPanel } from "@/components/operations/media/calendar-panel";
import { MediaResultsPanel } from "@/components/operations/media/results-panel";
import { ActionPackagePanel } from "@/components/operations/action-package-panel";
import { ActionReceiptPanel } from "@/components/operations/action-receipt-panel";
import { OperationsOverview } from "@/components/operations/operations-overview";
import { OperationsCommandCenter } from "@/components/operations/operations-command-center";
import { EmployeeWorkspace } from "@/components/operations/agents/employee-workspace";
import { OperationsLocalTaskPanel } from "@/components/operations/local-task-panel";
import {
  DevelopmentReviewPanel,
  SpecialistAssessmentPanel,
  assignedAgentName,
  developmentStatusLabel,
  feasibilityLabel,
  isDevelopmentKind,
} from "@/components/operations/crowdworks-development-panels";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { AppSelect } from "@/components/ui/app-select";
import { Textarea } from "@/components/ui/textarea";
import { HelpPopover } from "@/components/ui/help-popover";
import { cn } from "@/lib/utils";

type ConnectionForm = {
  provider_key: string;
  display_name: string;
  remote_account_ref: string;
  project_id: string;
};

type ConnectionProjectOption = {
  id: string;
  name: string;
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

function formatYen(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value).toLocaleString("ja-JP")}円`
    : "—";
}

function workflowLabel(stage: string | null | undefined): string {
  const labels: Record<string, string> = {
    INGESTED: "取得済み",
    EVALUATED: "評価済み",
    APPLICATION_PROPOSED: "判断待ち",
    APPLICATION_APPROVED: "応募承認済み",
    APPLICATION_REJECTED: "見送り",
    APPLICATION_SUBMITTED: "応募済み",
    CONTRACT_ACCEPTED: "受注済み",
    READY_FOR_REVIEW: "最終レビュー待ち",
  };
  return labels[String(stage || "").toUpperCase()] ?? (stage || "処理中");
}

function fitLabel(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    high: "高",
    medium: "中",
    low: "低",
  };
  const key = String(value || "").toLowerCase();
  return labels[key] ?? (value || "—");
}

function formatHours(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value * 10) / 10}h`
    : "—";
}

function workerKindLabel(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    writing: "文章作成",
    research: "調査・整理",
    data_entry: "データ入力・整形",
    web_development: "Web制作",
    app_development: "アプリ開発",
    code: "アプリ開発",
    general: "一般作業",
  };
  return labels[String(value || "").toLowerCase()] ?? "一般作業";
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

type CrowdWorksPolicyConstraints = {
  scan_interval_minutes?: number | null;
  max_opportunities_per_scan?: number | null;
  max_applications_per_scan?: number | null;
  min_expected_profit?: number | null;
  min_profit_per_hour?: number | null;
};

type CrowdWorksBootstrapStatus = {
  configured: boolean;
  started: boolean;
  browser_ready: boolean;
  blocked_reason?: string | null;
  runtime_readiness?: CrowdWorksLaneReadiness | null;
  specialist_readiness?: Record<string, CrowdWorksLaneReadiness | null | undefined> | null;
  connection?: { id?: string | null; project_id?: string | null } | null;
  policy?: {
    state?: string | null;
    current_revision?: {
      authorization_mode?: string | null;
      constraints?: CrowdWorksPolicyConstraints | null;
    } | null;
  } | null;
};

type CrowdWorksLaneReadiness = {
  ready?: boolean | null;
  blocked_reason?: string | null;
  execution_boundary?: string | null;
};

const CROWDWORKS_LANE_LABELS: Record<string, string> = {
  lead: "案件探索・応募",
  web_development: "Web開発",
  app_development: "アプリ開発",
};

const CROWDWORKS_REASON_LABELS: Record<string, string> = {
  agent_missing: "担当Agentが未作成です",
  agent_inactive: "担当Agentが停止中です",
  agent_revision_missing: "担当Agentの設定版がありません",
  capability_denied: "必要な権限が付与されていません",
  agent_route_development_provider_unsupported: "開発用モデル経路が未対応です",
  agent_route_provider_unsupported: "モデル経路が未対応です",
  isolated_execution_required: "隔離された実行環境が必要です",
  crowdworks_browser_session_unavailable: "Edge拡張の接続待ちです",
};

function crowdWorksReasonLabel(reason: string): string {
  if (CROWDWORKS_REASON_LABELS[reason]) return CROWDWORKS_REASON_LABELS[reason];
  if (reason.startsWith("agent_route_")) return "モデル経路の設定を確認してください";
  return "開始条件を確認してください";
}

function crowdWorksReasonHref(reason: string): string {
  return reason === "crowdworks_browser_session_unavailable" ? "/operations?tab=connections" : "/operations?tab=agents";
}

type CrowdWorksLaneView = { key: string; label: string; state: "ready" | "blocked" | "unknown"; reason: string | null };

function crowdWorksLaneViews(status: CrowdWorksBootstrapStatus | null): CrowdWorksLaneView[] {
  if (!status) return [];
  const lanes: Array<[string, CrowdWorksLaneReadiness | null | undefined]> = [
    ["lead", status.runtime_readiness],
    ...["web_development", "app_development"].map((kind): [string, CrowdWorksLaneReadiness | null | undefined] => [kind, status.specialist_readiness?.[kind]]),
  ];
  return lanes.map(([key, readiness]) => {
    const label = CROWDWORKS_LANE_LABELS[key] ?? key;
    if (!readiness || typeof readiness.ready !== "boolean") return { key, label, state: "unknown", reason: null };
    if (readiness.ready) return { key, label, state: "ready", reason: null };
    return { key, label, state: "blocked", reason: readiness.blocked_reason ?? "unknown" };
  });
}

function CrowdWorksLaneReadiness({ status }: { status: CrowdWorksBootstrapStatus | null }) {
  const lanes = crowdWorksLaneViews(status);
  if (lanes.length === 0) return null;
  const browserReason = status?.browser_ready === false && status.blocked_reason === "crowdworks_browser_session_unavailable" ? status.blocked_reason : null;
  return (
    <ul className="grid gap-1.5 text-[11px] sm:grid-cols-3" data-testid="crowdworks-lane-readiness" aria-label="担当レーン別の準備状況">
      {lanes.map((lane) => (
        <li key={lane.key} className="rounded-md border border-border/70 bg-muted/20 px-2.5 py-2" data-lane={lane.key} data-lane-state={lane.state}>
          <p className="font-semibold">{lane.label}: {lane.state === "ready" ? "準備完了" : lane.state === "blocked" ? "要対応" : "状態不明"}</p>
          {lane.reason ? (
            <p className="mt-0.5 text-muted-foreground">
              {crowdWorksReasonLabel(lane.reason)}{" "}
              <Link className="text-primary underline" href={crowdWorksReasonHref(lane.reason)}>設定を開く</Link>
            </p>
          ) : null}
        </li>
      ))}
      {browserReason ? (
        <li className="sm:col-span-3 text-muted-foreground">
          {crowdWorksReasonLabel(browserReason)}{" "}
          <Link className="text-primary underline" href={crowdWorksReasonHref(browserReason)}>設定を開く</Link>
        </li>
      ) : null}
    </ul>
  );
}

function CrowdWorksQuickStart({ onReady }: { onReady: () => void }) {
  const [status, setStatus] = useState<CrowdWorksBootstrapStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const leadOnly = status?.started === true
    && crowdWorksLaneViews(status).some((lane) => lane.key !== "lead" && lane.state !== "ready");
  const policyRevision = status?.policy?.current_revision ?? null;
  const policy = policyRevision?.constraints ?? null;
  const policyValue = (value: number | null | undefined, suffix = "") =>
    typeof value === "number" && Number.isFinite(value) ? `${value.toLocaleString("ja-JP")}${suffix}` : "—";

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/python-proxy/marketplace/crowdworks/status", {
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`CrowdWorks自動化の状態を取得できませんでした (${response.status})`);
      const payload = await response.json() as { result?: CrowdWorksBootstrapStatus };
      setStatus(payload.result ?? null);
    } catch (loadError) {
      setError(loadError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { const task = window.setTimeout(() => { void load(); }, 0); return () => window.clearTimeout(task); }, [load]);

  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      const response = await fetch("/api/python-proxy/marketplace/crowdworks/bootstrap", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
      });
      if (!response.ok) throw new Error(`CrowdWorks自動化を開始できませんでした (${response.status})`);
      const payload = await response.json() as { result?: CrowdWorksBootstrapStatus };
      const next = payload.result ?? null;
      setStatus(next);
      onReady();
      if (next?.started) {
        const partial = crowdWorksLaneViews(next).some((lane) => lane.key !== "lead" && lane.state !== "ready");
        if (partial) toast.warning("探索・応募は開始しました。開発レーンは準備未完了または状態不明です");
        else toast.success("CrowdWorks自動化を開始しました");
      } else if (next?.blocked_reason === "crowdworks_browser_session_unavailable") {
        toast.warning("設定は完了しました。PC接続とEdge拡張を接続すると自動開始できます");
      } else if (next?.blocked_reason) {
        toast.warning(`CrowdWorks自動化の設定は完了しました。${crowdWorksReasonLabel(next.blocked_reason)}`);
      } else {
        toast.warning("CrowdWorks自動化の設定は完了しましたが、開始条件を確認してください");
      }
    } catch (startError) {
      setError(startError);
    } finally {
      setStarting(false);
    }
  };

  return (
    <Card size="sm" data-testid="crowdworks-quick-start">
      <CardHeader className="border-b border-border/70">
        <CardTitle className="inline-flex items-center gap-1 text-sm">
          CrowdWorks 自動化
          <HelpPopover label="CrowdWorks 自動化">
            Agent・Connection・応募Policyを自動セットアップします。CrowdWorksのID/パスワードは保存せず、ログイン済みEdgeを使います。
          </HelpPopover>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 pt-3">
        {loading ? <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 状態を確認中…</div> : (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <StatusPill status={status?.started ? "running" : status?.configured ? "draft" : "pending"} />
            <span>{status?.started ? (leadOnly ? "探索・応募のみ稼働中" : "自動稼働中") : status?.configured ? "セットアップ済み" : "未セットアップ"}</span>
            <span className="text-muted-foreground">{status?.browser_ready ? "Edge接続済み" : "Edge接続待ち"}</span>
          </div>
        )}
        {!loading ? <CrowdWorksLaneReadiness status={status} /> : null}
        <div className="rounded-md border border-border/70 bg-muted/20 p-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p className="inline-flex items-center gap-1 text-xs font-semibold">
                現在の探索 / 応募Policy
                <HelpPopover label="現在の探索 / 応募Policy">
                  一回限りの案件一覧ではなく、このPolicyの間隔でCrowdWorksを継続探索します。値は現在のPolicy Revisionから表示しています。
                </HelpPopover>
              </p>
            </div>
            {status?.policy?.state ? <StatusPill status={status.policy.state} /> : null}
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            <div className="rounded-md bg-background/70 p-2"><p className="text-[10px] text-muted-foreground">Scan interval</p><p className="mt-0.5 text-xs font-semibold">{policyValue(policy?.scan_interval_minutes, "分")}</p></div>
            <div className="rounded-md bg-background/70 p-2"><p className="text-[10px] text-muted-foreground">1回の最大取得</p><p className="mt-0.5 text-xs font-semibold">{policyValue(policy?.max_opportunities_per_scan, "件")}</p></div>
            <div className="rounded-md bg-background/70 p-2"><p className="text-[10px] text-muted-foreground">期待利益しきい値</p><p className="mt-0.5 text-xs font-semibold">{typeof policy?.min_expected_profit === "number" ? formatYen(policy.min_expected_profit) : "—"}</p></div>
            <div className="rounded-md bg-background/70 p-2"><p className="text-[10px] text-muted-foreground">利益 / 時しきい値</p><p className="mt-0.5 text-xs font-semibold">{typeof policy?.min_profit_per_hour === "number" ? `${formatYen(policy.min_profit_per_hour)}/h` : "—"}</p></div>
            <div className="rounded-md bg-background/70 p-2"><p className="text-[10px] text-muted-foreground">応募候補上限</p><p className="mt-0.5 text-xs font-semibold">{policyValue(policy?.max_applications_per_scan, "件")}</p></div>
          </div>
          <div className="mt-3 grid gap-1.5 text-[11px] text-muted-foreground sm:grid-cols-2">
            <p>• ブラウザ: {status?.browser_ready ? "Edge接続済み（CrowdWorksへのログインが必要）" : "Edgeの接続待ち"}</p>
            <p>• 実応募: {policyRevision?.authorization_mode === "human_approval" ? "人間承認が必須（自動実応募しない）" : policyRevision?.authorization_mode ? `Policy: ${policyRevision.authorization_mode}` : "Policy未取得"}</p>
            <p>• 承認後: 実応募 → 契約監視 → 受注後AI作業 → READY_FOR_REVIEW</p>
            <p>• 最終納品: 自動実行しない</p>
          </div>
        </div>
        {status?.configured && status?.connection && !status.connection.project_id ? (
          <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-4 text-amber-800 dark:text-amber-200">
            CrowdWorks自動化のConnectionはProject未所属（global）です。所属Projectは作成後に変更できません。Project単位のConnectionが必要なら下のフォームから既存Projectを選んで追加できます。AgentのSpace所属はAgent画面から設定できます。
          </p>
        ) : null}
        <ErrorNotice error={error} />
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" onClick={() => void start()} disabled={starting || loading || status?.started === true}>
            {starting ? <Loader2 className="size-3.5 animate-spin" /> : <Bot className="size-3.5" />}
            {status?.started ? (leadOnly ? "探索・応募のみ稼働中" : "自動稼働中") : status?.configured ? "接続確認して開始" : "自動セットアップして開始"}
          </Button>
          <Button type="button" size="sm" variant="outline" onClick={() => void load()} disabled={starting || loading}>
            <RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 状態を更新
          </Button>
        </div>
      </CardContent>
    </Card>
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
  const [projectOptions, setProjectOptions] = useState<ConnectionProjectOption[]>([]);

  useEffect(() => {
    let cancelled = false;
    const loadProjects = async () => {
      try {
        const response = await fetch("/api/python-proxy/operations/command-center?limit=500", {
          credentials: "include",
          cache: "no-store",
          headers: { accept: "application/json" },
        });
        if (!response.ok) return;
        const payload = await response.json() as {
          projects?: Array<{ id?: string | null; name?: string | null }>;
          result?: { projects?: Array<{ id?: string | null; name?: string | null }> } | null;
        };
        const projects = payload.result?.projects ?? payload.projects;
        if (cancelled || !Array.isArray(projects)) return;
        const next = projects
          .filter((item): item is { id: string; name?: string | null } => typeof item?.id === "string" && Boolean(item.id))
          .map((item) => ({ id: item.id, name: item.name?.trim() || item.id }));
        setProjectOptions(next);
      } catch {
        // Project selection is a convenience; Connection CRUD remains usable if the projection is unavailable.
      }
    };
    void loadProjects();
    return () => { cancelled = true; };
  }, []);

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
          <h2 className="inline-flex items-center gap-1 text-lg font-semibold tracking-tight">
            Connections
            <HelpPopover label="Connections">
              CrowdWorksなど、外部サービスの接続先を登録します。認証情報はこの画面に入力・保存しません。
            </HelpPopover>
          </h2>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={onReload} disabled={loading}>
          <RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 更新
        </Button>
      </div>

      <CrowdWorksQuickStart onReady={onReload} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(19rem,24rem)]">
        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="inline-flex items-center gap-1 text-sm">登録済みのConnections<HelpPopover label="登録済みのConnections">接続先は案件の作成時に選択できます。</HelpPopover></CardTitle>
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
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      Project: {projectOptions.find((project) => project.id === connection.project_id)?.name || connection.project_id || "未所属（global）"}
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
            <CardTitle className="inline-flex items-center gap-1 text-sm">
              {editingId ? "Connectionを編集" : "Manual Connectionを追加"}
              <HelpPopover label={editingId ? "Connectionを編集" : "Manual Connectionを追加"}>
                資格情報を扱わず、接続先の識別情報だけを登録します。
              </HelpPopover>
            </CardTitle>
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
                <FieldLabel htmlFor="operations-connection-project">所属Project（任意・作成時のみ）</FieldLabel>
                {projectOptions.length ? (
                  <AppSelect
                    id="operations-connection-project"
                    value={form.project_id}
                    onChange={(event) => setForm((current) => ({ ...current, project_id: event.target.value }))}
                    disabled={Boolean(editingId)}
                  >
                    <option value="">未所属（global）</option>
                    {projectOptions.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                    {form.project_id && !projectOptions.some((project) => project.id === form.project_id) ? <option value={form.project_id}>{form.project_id}</option> : null}
                  </AppSelect>
                ) : (
                  <Input id="operations-connection-project" value={form.project_id} onChange={(event) => setForm((current) => ({ ...current, project_id: event.target.value }))} placeholder="project-id" disabled={Boolean(editingId)} />
                )}
                <p className="text-[10px] leading-4 text-muted-foreground">既存Projectへ紐づける場合だけ選択します。scopeの安全性を保つため作成後は変更できません。</p>
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
  const [decisionSaving, setDecisionSaving] = useState(false);
  const [requirementsSaving, setRequirementsSaving] = useState(false);

  const latestEvaluation = useMemo(() => detail.evaluations.reduce<OpportunityEvaluation | null>((latest, item) => {
    if (!latest) return item;
    return (item.version ?? 0) >= (latest.version ?? 0) ? item : latest;
  }, null), [detail.evaluations]);
  const decisionAction = useMemo(() => {
    const actions = detail.actions ?? [];
    return actions.find((item) => item.status === "proposed")
      ?? actions.find((item) => ["approved", "attempting", "running", "succeeded", "uncertain"].includes(item.status ?? ""))
      ?? actions[0]
      ?? null;
  }, [detail.actions]);
  const latestDraft = useMemo(() => detail.drafts.reduce<ApplicationDraft | null>((latest, item) => {
    if (!latest) return item;
    return (item.version ?? 0) >= (latest.version ?? 0) ? item : latest;
  }, null), [detail.drafts]);
  // An Action freezes the approved content; a newer draft does not revise it.
  const decisionDraftId = decisionAction?.application_draft_id ?? decisionAction?.payload?.application_draft_id;
  const decisionDraft = decisionAction
    ? detail.drafts.find((item) => item.id === decisionDraftId) ?? null
    : latestDraft;
  const application = decisionAction?.payload ?? decisionDraft;
  const applicationMessage = typeof application?.message === "string" ? application.message : null;
  const applicationPrice = typeof application?.offered_price === "number" ? application.offered_price : null;
  const applicationCurrency = typeof application?.currency === "string" ? application.currency : "JPY";
  const applicationDelivery = typeof application?.delivery_estimate === "string" ? application.delivery_estimate : null;
  const expectedProfit = detail.expected_profit ?? (
    typeof latestEvaluation?.estimated_revenue === "number"
      ? latestEvaluation.estimated_revenue - (latestEvaluation.estimated_cost ?? 0)
      : null
  );
  const expectedProfitPerHour = detail.expected_profit_per_hour ?? (
    typeof expectedProfit === "number" && (latestEvaluation?.estimated_effort_hours ?? 0) > 0
      ? expectedProfit / Number(latestEvaluation?.estimated_effort_hours)
      : null
  );
  const estimatedEffortHours = detail.estimated_effort_hours ?? latestEvaluation?.estimated_effort_hours ?? null;
  const humanWorkLabel = typeof detail.expected_human_minutes === "number"
    ? detail.expected_human_minutes > 0
      ? `あり・約${Math.round(detail.expected_human_minutes)}分`
      : "原則不要"
    : "未評価";
  const confidenceLabel = typeof detail.confidence === "number"
    ? `${Math.round(detail.confidence * 100)}%`
    : "未評価";
  const blockingRequirements = detail.development_blocking_requirements ?? [];

  const decideCrowdWorks = async (decision: "approve" | "reject") => {
    if (!decisionAction?.id) return;
    setDecisionSaving(true);
    setError(null);
    try {
      const expectedVersion = decisionAction.version ?? decisionAction.action_version ?? 1;
      const updated = decision === "approve"
        ? await operationsApi.approveAction(decisionAction.id, { expected_version: expectedVersion, reason: "CrowdWorks案件への実応募を承認" }, newIdempotencyKey())
        : await operationsApi.rejectAction(decisionAction.id, { expected_version: expectedVersion, reason: "今回は見送る" }, newIdempotencyKey());
      const actions = (detail.actions ?? []).map((item) => item.id === updated.id ? updated : item);
      onDetailChanged({ ...detail, actions, external_action_status: updated.status });
      if (decision === "approve") onActionCreated(updated);
      toast.success(decision === "approve" ? "応募を承認しました。CrowdWorks担当が応募送信→契約監視→受注後作業→READY_FOR_REVIEWまで進みます" : "この案件は見送りました");
    } catch (decisionError) {
      setError(decisionError);
    } finally {
      setDecisionSaving(false);
    }
  };

  const resolveDevelopmentRequirements = async () => {
    setRequirementsSaving(true);
    setError(null);
    try {
      const updated = await operationsApi.resolveDevelopmentRequirements(detail.id, {
        resolved: true,
        note: "人間が受注後の必須要件・提供物を確認済み",
      });
      onDetailChanged({ ...detail, ...updated, development_requirements_resolved: true });
      toast.success("必須要件を確認済みとして記録しました。次回の開発laneで開始できます");
    } catch (resolveError) {
      setError(resolveError);
    } finally {
      setRequirementsSaving(false);
    }
  };

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

      {detail.source_url?.includes("crowdworks.jp") ? (
        <Card size="sm" className="border-primary/30 bg-primary/[0.03]">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="flex items-center gap-1.5 text-sm"><Bot className="size-4 text-primary" /> <span className="inline-flex items-center gap-1">CrowdWorks担当の判断材料<HelpPopover label="CrowdWorks担当の判断材料">案件取得・採算評価・応募案作成まではAIが準備します。仕事内容・採算・リスク・応募条件を確認し、進めるか見送るかだけ判断してください。</HelpPopover></span></CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 pt-3">
            <div className="rounded-md border bg-background/60 p-3">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">仕事内容</p>
              <p className="mt-1 line-clamp-4 whitespace-pre-wrap text-xs leading-5">{detail.source_text || detail.title || "案件本文を取得できていません"}</p>
            </div>

            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">案件種別 / 担当予定Agent</p><p className="mt-1 text-sm font-semibold">{workerKindLabel(detail.worker_kind)} / {assignedAgentName(detail)}</p></div>
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">想定工数</p><p className="mt-1 text-sm font-semibold">{formatHours(estimatedEffortHours)}</p></div>
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">{detail.compensation_type === "hourly" ? "時間単価" : "報酬"}</p><p className="mt-1 text-sm font-semibold">{formatYen(detail.compensation_yen)}</p></div>
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">人間作業</p><p className="mt-1 text-sm font-semibold">{humanWorkLabel}</p></div>
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">期待利益</p><p className="mt-1 text-sm font-semibold">{formatYen(expectedProfit)}</p></div>
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">利益 / 時</p><p className="mt-1 text-sm font-semibold">{formatYen(expectedProfitPerHour)}/h</p></div>
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">fit / confidence</p><p className="mt-1 text-sm font-semibold">{fitLabel(latestEvaluation?.fit ?? detail.fit)} / {confidenceLabel}</p></div>
              <div className="rounded-md border bg-background/60 p-2"><p className="text-[10px] text-muted-foreground">進捗</p><p className="mt-1 text-sm font-semibold">{workflowLabel(detail.workflow_stage)}</p></div>
            </div>

            {isDevelopmentKind(detail.worker_kind) ? <SpecialistAssessmentPanel opportunity={detail} /> : null}

            {isDevelopmentKind(detail.worker_kind) && blockingRequirements.length > 0 && !detail.development_requirements_resolved ? (
              <Card size="sm" className="border-amber-500/40 bg-amber-500/[0.04]" data-testid="development-requirements-gate">
                <CardHeader className="border-b border-amber-500/20">
                  <CardTitle className="text-sm">開発開始前の必須確認</CardTitle>
                  <p className="text-sm text-muted-foreground">開発開始前に解消すべき入力・要件が残っているため、AIはコードエージェントを開始しません。</p>
                </CardHeader>
                <CardContent className="space-y-2 pt-3">
                  <ul className="list-disc space-y-1 pl-5 text-xs leading-5">
                    {blockingRequirements.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                  <Button type="button" size="sm" disabled={requirementsSaving} onClick={() => void resolveDevelopmentRequirements()}>
                    {requirementsSaving ? "記録中…" : "要件を確認済みとして開発を許可"}
                  </Button>
                </CardContent>
              </Card>
            ) : null}

            {latestEvaluation?.summary ? (
              <div className="rounded-md border bg-background/60 p-3 text-sm leading-6">
                <p className="mb-1 text-xs font-semibold text-muted-foreground">担当Agentの評価 / 実行方針</p>
                <p className="whitespace-pre-wrap">{latestEvaluation.summary}</p>
              </div>
            ) : null}

            <div className="grid gap-2 text-xs sm:grid-cols-2">
              <div className="rounded-md border p-2"><span className="font-semibold">リスク: </span>{latestEvaluation ? latestEvaluation.risks?.join("、") || "特記事項なし" : "未評価"}</div>
              <div className="rounded-md border p-2"><span className="font-semibold">不足条件: </span>{latestEvaluation ? latestEvaluation.missing_requirements?.join("、") || "特記事項なし" : "未評価"}</div>
            </div>

            <div className="rounded-md border bg-background/60 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-semibold">{decisionAction ? "承認対象の応募内容" : "応募案"}</p>
                <div className="flex flex-wrap gap-3 text-[11px] text-muted-foreground">
                  <span>提示価格: {applicationPrice != null ? `${applicationPrice.toLocaleString("ja-JP")}${applicationCurrency === "JPY" ? "円" : ` ${applicationCurrency}`}` : "未設定"}</span>
                  <span>納期案: {applicationDelivery || "未設定"}</span>
                </div>
              </div>
              <p className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-muted-foreground">{applicationMessage || "応募文はまだ作成されていません。"}</p>
            </div>

            {decisionAction?.status === "proposed" ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-primary/30 bg-background/80 p-3">
                <div className="max-w-3xl">
                  <p className="text-sm font-semibold">あなたの判断: 応募する / 見送る</p>
                  <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
                    「応募して進める」でこの応募を人間承認します。承認後は CrowdWorks実応募 → 契約監視 → 受注後AI作業 → READY_FOR_REVIEW まで進みます。最終納品は自動ではありません。
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button type="button" size="sm" variant="outline" disabled={decisionSaving} onClick={() => void decideCrowdWorks("reject")}><XCircle className="size-3.5" /> 今回は見送る</Button>
                  <Button type="button" size="sm" disabled={decisionSaving} onClick={() => void decideCrowdWorks("approve")}><CheckCircle2 className="size-3.5" /> 応募して進める</Button>
                </div>
              </div>
            ) : decisionAction ? (
              <div className="flex items-center gap-2 rounded-md border bg-background/60 p-3 text-sm">
                <StatusPill status={decisionAction.status} />
                <span>{decisionAction.status === "succeeded" ? (isDevelopmentKind(detail.worker_kind) ? `応募済み。受注後は${assignedAgentName(detail)}が独立ワークスペースで実開発・ビルド・テスト・表示確認を行い、READY_FOR_REVIEWで人間レビューを待ちます。最終納品は自動ではありません。` : "応募済み。契約監視と受注後作業を継続し、READY_FOR_REVIEWで人間レビューを待ちます。最終納品は自動ではありません。") : decisionAction.status === "approved" ? "進行決定済み。応募実行待ちです。" : decisionAction.status === "rejected" ? "今回は見送り済みです。" : "担当Agentが処理を継続しています。"}</span>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">採算条件を満たす候補は、応募案ができ次第ここで最終判断できます。</p>
            )}
            <ErrorNotice error={error} />
          </CardContent>
        </Card>
      ) : null}

      {detail.development ? <DevelopmentReviewPanel development={detail.development} /> : null}

      <Card size="sm">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="inline-flex items-center gap-1 text-sm">Source（untrusted）<HelpPopover label="Source（untrusted）">外部サイトの内容は表示用データとして扱い、リンク先を自動実行しません。</HelpPopover></CardTitle>
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
            <CardTitle className="inline-flex items-center gap-1 text-sm">Evaluation versions<HelpPopover label="Evaluation versions">適合度・見積り・リスクを版管理します。</HelpPopover></CardTitle>
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
            <CardTitle className="inline-flex items-center gap-1 text-sm">Application Draft versions<HelpPopover label="Application Draft versions">承認前に内容を編集・比較し、Actionへ昇格します。</HelpPopover></CardTitle>
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
          <CardTitle className="flex items-center gap-1.5 text-sm"><FileUp className="size-4 text-primary" /> <span className="inline-flex items-center gap-1">Artifact versions（任意）<HelpPopover label="Artifact versions（任意）">ファイルを添付すると、Draftのartifact_version_idsへ紐づけて参照できます。</HelpPopover></span></CardTitle>
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
  initialOpportunityId,
}: {
  connections: OperationsConnection[];
  idempotencyKey: string;
  onActionCreated: (action: OperationsAction) => void;
  initialOpportunityId?: string | null;
}) {
  const requestedOpportunityId = initialOpportunityId?.trim() || null;
  const [opportunities, setOpportunities] = useState<EngagementOpportunity[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(requestedOpportunityId);
  const [detail, setDetail] = useState<OpportunityDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [detailError, setDetailError] = useState<unknown>(null);
  const [formError, setFormError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  const [cardDecisionSaving, setCardDecisionSaving] = useState<string | null>(null);
  const requestedOpportunityIdRef = useRef(requestedOpportunityId);
  const opportunitiesRef = useRef(opportunities);
  const targetDetailRef = useRef<OpportunityDetail | null>(null);
  const opportunitiesRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [title, setTitle] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const selectedConnection = connections.find((connection) => connection.id === connectionId) ?? null;

  useEffect(() => {
    opportunitiesRef.current = opportunities;
  }, [opportunities]);

  // A deep link is an explicit target, even when the bounded list projection
  // does not include it. Reset stale detail state before resolving a new URL
  // so a previous card cannot flash while the target is loading.
  useEffect(() => {
    requestedOpportunityIdRef.current = requestedOpportunityId;
    targetDetailRef.current = null;
    ++detailRequestRef.current;
    setDetail(null);
    setDetailError(null);
    setSelectedId(requestedOpportunityId ?? opportunitiesRef.current[0]?.id ?? null);
  }, [requestedOpportunityId]);

  const boardOpportunities = useMemo(() => {
    // Items that need a human (final review, bid decision, blocked development)
    // stay on the board ahead of newly evaluated listings.
    const priority = (item: EngagementOpportunity) => {
      const stage = String(item.workflow_stage || "").toUpperCase();
      const development = String(item.development?.status || "");
      if (stage === "READY_FOR_REVIEW") return 0;
      if (["uncertain", "verification_failed", "blocked"].includes(development)) return 1;
      if (stage === "APPLICATION_PROPOSED" && (item.external_action_status || "proposed") === "proposed") return 2;
      if (["CONTRACT_ACCEPTED", "APPLICATION_SUBMITTED"].includes(stage) || development === "running") return 3;
      return 4;
    };
    return opportunities
      .filter((item) => item.source_url?.includes("crowdworks.jp") && typeof item.expected_profit === "number")
      .map((item, index) => ({ item, index }))
      .sort((a, b) => priority(a.item) - priority(b.item) || a.index - b.index)
      .map(({ item }) => item)
      .slice(0, 30);
  }, [opportunities]);
  const reviewReadyCount = useMemo(
    () => boardOpportunities.filter((item) => String(item.workflow_stage || "").toUpperCase() === "READY_FOR_REVIEW").length,
    [boardOpportunities],
  );
  const decisionReadyCount = useMemo(
    () => boardOpportunities.filter((item) => String(item.workflow_stage || "").toUpperCase() === "APPLICATION_PROPOSED"
      && (item.external_action_status || (item.external_action_id ? "proposed" : "")) === "proposed").length,
    [boardOpportunities],
  );

  const loadOpportunities = useCallback(async () => {
    const requestId = ++opportunitiesRequestRef.current;
    setLoading(true);
    setError(null);
    try {
      const result = await operationsApi.listOpportunities();
      const ranked = [...result].sort((a, b) => {
        const aCrowdWorks = a.source_url?.includes("crowdworks.jp") ? 1 : 0;
        const bCrowdWorks = b.source_url?.includes("crowdworks.jp") ? 1 : 0;
        if (aCrowdWorks !== bCrowdWorks) return bCrowdWorks - aCrowdWorks;
        const aScore = typeof a.expected_profit_per_hour === "number" ? a.expected_profit_per_hour : -Infinity;
        const bScore = typeof b.expected_profit_per_hour === "number" ? b.expected_profit_per_hour : -Infinity;
        if (aScore !== bScore) return bScore - aScore;
        return String(b.created_at ?? "").localeCompare(String(a.created_at ?? ""));
      });
      if (requestId !== opportunitiesRequestRef.current) return;
      const targetDetail = targetDetailRef.current;
      const nextOpportunities = targetDetail && requestedOpportunityIdRef.current === targetDetail.id
        ? ranked.some((item) => item.id === targetDetail.id)
          ? ranked.map((item) => item.id === targetDetail.id ? { ...item, ...targetDetail } : item)
          : [targetDetail, ...ranked]
        : ranked;
      setOpportunities(nextOpportunities);
      setSelectedId((current) => {
        const requestedId = requestedOpportunityIdRef.current;
        if (requestedId) return requestedId;
        return current && nextOpportunities.some((item) => item.id === current)
          ? current
          : nextOpportunities[0]?.id ?? null;
      });
    } catch (loadError) {
      if (requestId === opportunitiesRequestRef.current) setError(loadError);
    } finally {
      if (requestId === opportunitiesRequestRef.current) setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    const requestId = ++detailRequestRef.current;
    setDetailLoading(true);
    setDetailError(null);
    setFormError(null);
    try {
      const loadedDetail = await operationsApi.getOpportunity(id);
      if (requestId !== detailRequestRef.current) return;
      if (loadedDetail.id !== id) {
        throw new Error("Opportunity detail response did not match the requested ID");
      }
      setDetail(loadedDetail);
      if (requestedOpportunityIdRef.current === id) targetDetailRef.current = loadedDetail;
      setOpportunities((current) => {
        const index = current.findIndex((item) => item.id === id);
        if (index < 0) return [loadedDetail, ...current];
        const next = [...current];
        next[index] = { ...next[index], ...loadedDetail };
        return next;
      });
    } catch (loadError) {
      if (requestId === detailRequestRef.current) setDetailError(loadError);
    } finally {
      if (requestId === detailRequestRef.current) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadOpportunities();
    const timer = window.setInterval(() => { void loadOpportunities(); }, 15000);
    return () => window.clearInterval(timer);
  }, [loadOpportunities]);
  useEffect(() => {
    if (selectedId) {
      void loadDetail(selectedId);
      return;
    }
    ++detailRequestRef.current;
    setDetail(null);
    setDetailError(null);
    setDetailLoading(false);
  }, [loadDetail, selectedId]);

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

  const decideFromCard = async (opportunity: EngagementOpportunity, decision: "approve" | "reject") => {
    setCardDecisionSaving(opportunity.id);
    setError(null);
    try {
      const currentDetail = detail?.id === opportunity.id
        ? detail
        : await operationsApi.getOpportunity(opportunity.id);
      const action = (currentDetail.actions ?? []).find((item) => item.status === "proposed") ?? null;
      if (!action) {
        setSelectedId(opportunity.id);
        setDetail(currentDetail);
        toast.warning("この案件はまだ応募判断の準備ができていません。詳細を確認してください");
        return;
      }
      const expectedVersion = action.version ?? action.action_version ?? 1;
      const updated = decision === "approve"
        ? await operationsApi.approveAction(
          action.id,
          { expected_version: expectedVersion, reason: "案件ボードから実応募を承認" },
          newIdempotencyKey(),
        )
        : await operationsApi.rejectAction(
          action.id,
          { expected_version: expectedVersion, reason: "案件ボードから今回は見送る" },
          newIdempotencyKey(),
        );
      if (decision === "approve") onActionCreated(updated);
      const nextStatus = decision === "approve" ? "approved" : "rejected";
      setOpportunities((current) => current.map((item) => (
        item.id === opportunity.id ? { ...item, external_action_status: nextStatus } : item
      )));
      const nextDetail = {
        ...currentDetail,
        external_action_status: nextStatus,
        actions: (currentDetail.actions ?? []).map((item) => item.id === updated.id ? updated : item),
      };
      if (selectedId === opportunity.id) setDetail(nextDetail);
      toast.success(decision === "approve"
        ? "応募を承認しました。CrowdWorks担当が応募送信→契約監視→受注後作業→READY_FOR_REVIEWまで進みます"
        : "この案件は見送りました");
    } catch (decisionError) {
      setError(decisionError);
    } finally {
      setCardDecisionSaving(null);
    }
  };

  return (
    <div className="space-y-4" data-testid="operations-opportunities-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="inline-flex items-center gap-1 text-lg font-semibold tracking-tight">Opportunities<HelpPopover label="Opportunities">外部案件のURLと本文を取り込み、Evaluation / Draft / Actionを版管理します。</HelpPopover></h2></div>
        <Button type="button" variant="outline" size="sm" onClick={() => void loadOpportunities()} disabled={loading}><RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> 更新</Button>
      </div>
      <Card size="sm" data-testid="crowdworks-opportunity-board" className="overflow-hidden">
        <CardHeader className="border-b border-border/70">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2 text-base"><Bot className="size-4 text-primary" /> <span className="inline-flex items-center gap-1">CrowdWorks 案件ボード<HelpPopover label="CrowdWorks 案件ボード">継続探索で見つけた採算評価済み案件を利益効率順に並べています。応募を承認した案件だけ実応募→契約監視→受注後AI作業→READY_FOR_REVIEWへ進み、最終納品は自動ではありません。</HelpPopover></span></CardTitle>
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>評価済み {boardOpportunities.length}件</span>
              <span className="rounded-full border border-primary/30 bg-primary/10 px-2 py-1 font-medium text-primary">応募判断待ち {decisionReadyCount}件</span>
              {reviewReadyCount ? <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 font-medium text-emerald-700 dark:text-emerald-300">最終レビュー待ち {reviewReadyCount}件</span> : null}
            </div>
          </div>
        </CardHeader>
        <CardContent className="pt-3">
          {loading && !boardOpportunities.length ? (
            <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 案件を読み込み中…</div>
          ) : boardOpportunities.length ? (
            <div className="-mx-1 overflow-x-auto pb-2" aria-label="CrowdWorks案件一覧">
              <div className="flex min-w-max gap-3 px-1">
                {boardOpportunities.map((opportunity) => {
                  const stage = String(opportunity.workflow_stage || "").toUpperCase();
                  const actionStatus = String(opportunity.external_action_status || (opportunity.external_action_id ? "proposed" : "")).toLowerCase();
                  const ready = stage === "APPLICATION_PROPOSED" && actionStatus === "proposed";
                  const accepted = ["approved", "attempting", "running", "succeeded"].includes(actionStatus)
                    || ["APPLICATION_SUBMITTED", "CONTRACT_ACCEPTED", "READY_FOR_REVIEW"].includes(stage);
                  const rejected = actionStatus === "rejected";
                  const screeningStatus = String(opportunity.screening_status || "").toLowerCase();
                  const screeningReason = String(opportunity.screening_reason || "");
                  const summaryLines = [...(opportunity.card_summary_lines ?? [])].filter(Boolean).slice(0, 3);
                  const fallbackSummaryLines = [
                    "仕事内容: " + (opportunity.title || "案件内容を確認中"),
                    "AI実行: " + workerKindLabel(opportunity.worker_kind) + "（" + assignedAgentName(opportunity) + "） / " + formatHours(opportunity.estimated_effort_hours),
                    "判断材料: 適合度 " + fitLabel(opportunity.fit) + "・AI確度 " + (typeof opportunity.confidence === "number" ? String(Math.round(opportunity.confidence * 100)) + "%" : "—"),
                  ];
                  while (summaryLines.length < 3) summaryLines.push(fallbackSummaryLines[summaryLines.length]);
                  const developmentStatus = String(opportunity.development?.status || "");
                  const developmentKind = isDevelopmentKind(opportunity.worker_kind);
                  const cardStateLabel = ready
                    ? "応募判断待ち"
                    : developmentStatus
                      ? developmentStatusLabel(developmentStatus)
                    : stage === "READY_FOR_REVIEW"
                      ? "最終レビュー待ち"
                    : accepted
                      ? "進行中"
                      : rejected
                        ? "見送り"
                        : ["below_profit_threshold", "below_profit_per_hour"].includes(screeningStatus)
                          ? "採算条件未達"
                          : screeningStatus === "qualified_waiting_rank"
                            ? "次回候補"
                            : screeningStatus === "application_preparing"
                              ? "応募案作成中"
                              : screeningStatus === "evaluation_incomplete"
                                ? "再評価待ち"
                                : screeningStatus === "specialist_review_pending"
                                  ? "専門評価待ち"
                                  : screeningStatus === "specialist_declined"
                                    ? "専門担当が見送り推奨"
                                    : screeningStatus === "specialist_unavailable"
                                      ? "担当Agentなし"
                                : stage === "EVALUATED"
                                  ? "評価済み"
                                  : workflowLabel(opportunity.workflow_stage);
                  const confidence = typeof opportunity.confidence === "number" ? String(Math.round(opportunity.confidence * 100)) + "%" : "—";
                  return (
                    <article
                      key={opportunity.id}
                      className={cn(
                        "flex w-[22rem] shrink-0 flex-col rounded-xl border bg-card p-3 shadow-sm transition",
                        selectedId === opportunity.id ? "border-primary ring-1 ring-primary/30" : "border-border hover:border-primary/40 hover:shadow-md",
                      )}
                      data-testid={"opportunity-card-" + opportunity.id}
                    >
                      <button type="button" className="text-left" onClick={() => setSelectedId(opportunity.id)}>
                        <div className="flex items-start justify-between gap-2">
                          <h3 className="line-clamp-2 min-h-10 text-sm font-semibold leading-5">{opportunity.title || "Untitled opportunity"}</h3>
                          <StatusPill status={opportunity.status} />
                        </div>
                      </button>
                      {opportunity.source_url ? (
                        <a
                          href={opportunity.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="mt-1 block truncate text-[10px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                          title={opportunity.source_url}
                        >
                          {opportunity.source_url}
                        </a>
                      ) : (
                        <p className="mt-1 truncate text-[10px] text-muted-foreground">{opportunity.id}</p>
                      )}

                      <ul className="mt-3 min-h-[4.75rem] space-y-1 rounded-md border border-border/60 bg-muted/20 px-2.5 py-2 text-[11px] leading-5 text-muted-foreground">
                        {summaryLines.map((line, index) => (
                          <li key={opportunity.id + "-summary-" + index} className="flex min-w-0 gap-1.5" title={line}>
                            <span className="shrink-0 text-primary">•</span>
                            <span className="line-clamp-1">{line}</span>
                          </li>
                        ))}
                      </ul>

                      <div className="mt-3 grid grid-cols-3 gap-1.5 text-xs">
                        <div className="rounded-md bg-muted/45 p-2"><p className="text-[10px] text-muted-foreground">{opportunity.compensation_type === "hourly" ? "時間単価" : "報酬"}</p><p className="mt-0.5 font-semibold">{formatYen(opportunity.compensation_yen)}</p></div>
                        <div className="rounded-md bg-muted/45 p-2"><p className="text-[10px] text-muted-foreground">期待利益</p><p className="mt-0.5 font-semibold">{formatYen(opportunity.expected_profit)}</p></div>
                        <div className="rounded-md bg-muted/45 p-2"><p className="text-[10px] text-muted-foreground">利益/時</p><p className="mt-0.5 font-semibold">{formatYen(opportunity.expected_profit_per_hour)}</p></div>
                        <div className="rounded-md bg-muted/45 p-2"><p className="text-[10px] text-muted-foreground">想定工数</p><p className="mt-0.5 font-semibold">{formatHours(opportunity.estimated_effort_hours)}</p></div>
                        <div className="rounded-md bg-muted/45 p-2"><p className="text-[10px] text-muted-foreground">適合度</p><p className="mt-0.5 font-semibold">{fitLabel(opportunity.fit)}</p></div>
                        <div className="rounded-md bg-muted/45 p-2"><p className="text-[10px] text-muted-foreground">AI確度</p><p className="mt-0.5 font-semibold">{confidence}</p></div>
                      </div>

                      {developmentKind ? (
                        <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-primary/20 bg-primary/[0.03] px-2.5 py-1.5 text-[11px]" data-testid={"opportunity-specialist-" + opportunity.id}>
                          <span className="truncate"><span className="text-muted-foreground">担当予定: </span><span className="font-semibold">{assignedAgentName(opportunity)}</span></span>
                          <span className="shrink-0 text-muted-foreground">{feasibilityLabel(opportunity.specialist_assessment?.feasibility)}</span>
                        </div>
                      ) : null}

                      <div className="mt-2 min-h-10 rounded-md border border-border/60 bg-background/50 px-2.5 py-2">
                        <p className={cn("text-xs font-semibold", ready && "text-primary")}>{cardStateLabel}</p>
                        {screeningReason && !accepted && !rejected ? <p className="mt-0.5 line-clamp-1 text-[10px] text-muted-foreground" title={screeningReason}>{screeningReason}</p> : null}
                      </div>

                      <div className="mt-3 flex items-center justify-end gap-2 border-t border-border/70 pt-3">
                        {ready ? (
                          <>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              disabled={cardDecisionSaving !== null}
                              onClick={() => void decideFromCard(opportunity, "reject")}
                              aria-label={(opportunity.title || "案件") + "を今回は見送る"}
                            >
                              <XCircle className="size-3.5" />
                              今回は見送る
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              disabled={cardDecisionSaving !== null}
                              onClick={() => void decideFromCard(opportunity, "approve")}
                              aria-label={(opportunity.title || "案件") + "へ応募して進める"}
                              title="人間承認後、実応募→契約監視→受注後AI作業→READY_FOR_REVIEWまで進みます。最終納品は自動ではありません。"
                            >
                              {cardDecisionSaving === opportunity.id ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5" />}
                              応募して進める
                            </Button>
                          </>
                        ) : (
                          <Button type="button" size="sm" variant="outline" disabled className="min-w-[8.5rem]">
                            {cardStateLabel}
                          </Button>
                        )}
                      </div>
                    </article>
                  );
                })}
              </div>
            </div>
          ) : (
            <EmptyState>評価済みのCrowdWorks案件を準備しています。</EmptyState>
          )}
          <ErrorNotice error={error} />
        </CardContent>
      </Card>

      <Card size="sm" className="min-w-0">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="inline-flex items-center gap-1 text-sm">選択案件の詳細<HelpPopover label="選択案件の詳細">カードを選ぶと、担当エージェントの評価・リスク・応募案・進捗を確認できます。</HelpPopover></CardTitle>
        </CardHeader>
        <CardContent className="relative pt-4">
          {detail && detail.id === selectedId ? (
            <>
              <OpportunityDetailPanel key={detail.id} detail={detail} connections={connections} idempotencyKey={idempotencyKey} onRefresh={() => selectedId && void loadDetail(selectedId)} onActionCreated={onActionCreated} onDetailChanged={(next) => {
                setDetail(next);
                if (requestedOpportunityIdRef.current === next.id) targetDetailRef.current = next;
                setOpportunities((current) => current.map((item) => item.id === next.id ? { ...item, external_action_status: next.external_action_status } : item));
              }} />
              {detailError ? <div className="mt-3"><ErrorNotice error={detailError} /></div> : null}
              {detailLoading ? <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-[inherit] bg-background/60" data-testid="operations-opportunity-detail-loading-overlay"><div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Detailを読み込み中…</div></div> : null}
            </>
          ) : detailLoading ? (
            <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Detailを読み込み中…</div>
          ) : detailError && selectedId ? (
            <div className="space-y-3" data-testid="operations-opportunity-deeplink-error">
              <p className="break-all text-sm">Opportunity {selectedId} を表示できませんでした。</p>
              <ErrorNotice error={detailError} />
              <Button type="button" size="sm" variant="outline" onClick={() => void loadDetail(selectedId)}>再試行</Button>
            </div>
          ) : (
            <EmptyState>上の案件カードを選択してください。</EmptyState>
          )}
        </CardContent>
      </Card>

      <details className="rounded-lg border border-border bg-card/40">
        <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">管理用: 手動取り込み / 全取り込み一覧</summary>
        <div className="space-y-4 border-t border-border/70 p-4">
          <div>
            <h3 className="text-sm font-semibold">Sourceを手動で取り込む</h3>
            <p className="mt-1 text-xs text-muted-foreground">通常のCrowdWorks運用では使いません。外部案件を手動登録するときだけ使用します。</p>
          </div>
          <form className="grid gap-2 lg:grid-cols-[minmax(11rem,0.7fr)_minmax(11rem,0.7fr)_minmax(0,1.8fr)_auto]" onSubmit={ingest} aria-label="Ingest opportunity form">
            <Input aria-label="Opportunity title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="タイトル（任意）" />
            <Input aria-label="Source URL" type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://…" required />
            <Textarea aria-label="Source text" value={sourceText} onChange={(event) => setSourceText(event.target.value)} placeholder="案件本文を貼り付け" rows={1} required />
            <Button type="submit" disabled={saving}><Plus className="size-3.5" /> Ingest</Button>
          </form>
          <div className="flex flex-wrap items-center gap-2"><label htmlFor="operations-opportunity-connection" className="text-xs text-muted-foreground">Connection（任意）</label><AppSelect id="operations-opportunity-connection" className="h-8 min-w-48 rounded-md border border-input bg-card px-2 text-xs" value={connectionId} onChange={(event) => setConnectionId(event.target.value)}><option value="">Connectionを選択</option>{connections.map((connection) => <option key={connection.id} value={connection.id}>{connection.display_name}</option>)}</AppSelect></div>
          <ErrorNotice error={formError} />

          <div className="max-h-80 overflow-y-auto rounded-lg border border-border">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-card px-3 py-2 text-xs"><span className="font-medium">全取り込み済み</span><span className="text-muted-foreground">{opportunities.length}件</span></div>
            <div className="divide-y divide-border/60">
              {opportunities.map((opportunity) => (
                <button key={opportunity.id} type="button" onClick={() => setSelectedId(opportunity.id)} className={cn("block w-full px-3 py-2 text-left text-xs hover:bg-muted/40", selectedId === opportunity.id && "bg-primary/5")}>
                  <div className="flex items-center justify-between gap-2"><span className="truncate font-medium">{opportunity.title || "Untitled opportunity"}</span><span className="shrink-0 text-muted-foreground">{workflowLabel(opportunity.workflow_stage)}</span></div>
                </button>
              ))}
            </div>
          </div>
        </div>
      </details>
    </div>
  );
}

function ActionDetailPanel({
  action,
  idempotencyKey,
  readOnly,
  onChanged,
  onReload,
}: {
  action: OperationsAction;
  idempotencyKey: string;
  readOnly: boolean;
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
    if (readOnly) return;
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
    if (readOnly) return;
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
    if (readOnly) return;
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
    if (readOnly) return;
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
    if (readOnly) return;
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
      {readOnly ? <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground" data-testid="operations-action-read-only">閲覧のみ。承認・改訂・手動実行・結果の更新は無効になっています。</p> : null}
      <div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h3 className="truncate text-base font-semibold">Action <code className="font-mono text-xs font-normal">{action.id}</code></h3><StatusPill status={action.status} /></div><p className="mt-1 text-xs text-muted-foreground">version {expectedVersion} · action version {action.action_version ?? "—"}</p></div><Button type="button" size="sm" variant="outline" onClick={onReload}><RefreshCw className="size-3.5" /> Timelineを再読込</Button></div>

      <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><ClipboardCheck className="size-4 text-primary" /> <span className="inline-flex items-center gap-1">Exact canonical payload<HelpPopover label="Exact canonical payload">承認対象のpayloadとhashを確認してください。承認後はこの内容が凍結されます。</HelpPopover></span></CardTitle></CardHeader><CardContent className="space-y-3 pt-1"><pre data-testid="canonical-payload" className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md border border-border/70 bg-background/60 p-3 font-mono text-xs leading-5">{JSON.stringify(action.payload ?? {}, null, 2)}</pre><div className="grid gap-2 sm:grid-cols-2"><div className="rounded-md border border-border/60 bg-muted/20 p-2"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">payload hash</p><code className="mt-1 block break-all text-xs">{action.payload_hash || "—"}</code></div><div className="rounded-md border border-border/60 bg-muted/20 p-2"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">artifact hashes</p><code className="mt-1 block break-all text-xs">{action.artifact_hashes?.join(", ") || "—"}</code></div></div><div className="flex flex-wrap items-center gap-2"><span className="text-xs text-muted-foreground">Frozen source URL: <span className="font-mono">{frozenSourceUrl || "—"}</span></span><span className="text-xs text-muted-foreground">Source hash: <span className="font-mono">{frozenSourceHash || "—"}</span></span>{approved && <Button type="button" size="sm" variant="outline" onClick={() => void copyFrozenPayload()}>{copied ? <Check className="size-3.5" /> : <Clipboard className="size-3.5" />}{copied ? "コピーしました" : "Frozen payload + source URLをコピー"}</Button>}</div></CardContent></Card>

      <div className="grid gap-4 xl:grid-cols-2"><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Approval<HelpPopover label="Approval">Decisionはexpected versionを含む明示的な操作です。</HelpPopover></CardTitle></CardHeader><CardContent className="space-y-3 pt-1"><Textarea disabled={readOnly} aria-label="Approval reason" value={decisionReason} onChange={(event) => setDecisionReason(event.target.value)} placeholder="理由（任意）" rows={2} /><div className="flex flex-wrap gap-2"><Button type="button" size="sm" onClick={() => void mutateAction("approve")} disabled={readOnly || saving !== null || !canApprove}><CheckCircle2 className="size-3.5" /> Approve</Button><Button type="button" size="sm" variant="destructive" onClick={() => void mutateAction("reject")} disabled={readOnly || saving !== null || !canReject}><XCircle className="size-3.5" /> Reject</Button></div>{approved ? <p className="rounded-md border border-emerald-500/30 bg-emerald-500/5 px-2.5 py-2 text-xs text-emerald-800 dark:text-emerald-200">{readOnly ? "承認済み。Frozen payloadと実行履歴を閲覧できます。" : "承認済み。Frozen payloadをコピーしてからManual attemptを開始できます。"}</p> : null}</CardContent></Card><Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Revise action<HelpPopover label="Revise action">新しいDraftへ改訂すると、既存のApprovalは無効化されます。</HelpPopover></CardTitle></CardHeader><CardContent className="pt-1"><form className="space-y-2" onSubmit={revise}><Input disabled={readOnly} aria-label="New application draft ID" value={revisionDraftId} onChange={(event) => setRevisionDraftId(event.target.value)} placeholder="new application draft ID" required /><Button type="submit" size="sm" variant="outline" disabled={readOnly || saving !== null || !canRevise}><Pencil className="size-3.5" /> Revise</Button></form>{approvalInvalidated ? <p className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-2 text-xs text-amber-800 dark:text-amber-200">Approval invalidated — 改訂後のpayloadを再承認してください。</p> : null}</CardContent></Card></div>

      <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Manual attempt<HelpPopover label="Manual attempt">承認後に開始し、結果を succeeded / failed / uncertain で記録します。</HelpPopover></CardTitle></CardHeader><CardContent className="space-y-3 pt-1">{attempt ? <div className="rounded-md border border-border/70 bg-background/40 p-3 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-mono">Attempt {attempt.id}</span><StatusPill status={attempt.outcome || attempt.status} /></div><p className="mt-1 text-muted-foreground">started {attempt.started_at || "—"} · completed {attempt.completed_at || "—"}</p>{attempt.evidence_note ? <p className="mt-2 whitespace-pre-wrap">Evidence: {attempt.evidence_note}</p> : null}</div> : <p className="text-sm text-muted-foreground">Attemptはまだありません。</p>}{approved && !attemptInProgress && <Button type="button" size="sm" onClick={() => void startAttempt()} disabled={readOnly || saving !== null}><Plus className="size-3.5" /> Manual attemptを開始</Button>}{attemptInProgress && <form className="space-y-2 rounded-md border border-border/60 bg-muted/20 p-3" onSubmit={completeAttempt} aria-label="Complete manual attempt form"><div className="grid gap-2 sm:grid-cols-2"><AppSelect disabled={readOnly} aria-label="Attempt outcome" className="h-8 rounded-md border border-input bg-card px-2 text-xs" value={completeForm.outcome} onChange={(event) => setCompleteForm((current) => ({ ...current, outcome: event.target.value as CompleteForm["outcome"] }))}><option value="succeeded">succeeded</option><option value="failed">failed</option><option value="uncertain">uncertain</option></AppSelect><AppSelect disabled={readOnly} aria-label="Confirmation level" value={completeForm.confirmation_level} onChange={(event) => setCompleteForm((current) => ({ ...current, confirmation_level: event.target.value }))}><option value="human_confirmed">human_confirmed</option></AppSelect></div><Textarea disabled={readOnly} aria-label="Result summary" value={completeForm.result_summary} onChange={(event) => setCompleteForm((current) => ({ ...current, result_summary: event.target.value }))} placeholder="結果サマリー" rows={2} /><div className="grid gap-2 sm:grid-cols-3"><Input disabled={readOnly} aria-label="Remote resource ID" value={completeForm.remote_resource_id} onChange={(event) => setCompleteForm((current) => ({ ...current, remote_resource_id: event.target.value }))} placeholder="remote resource ID" /><Input disabled={readOnly} aria-label="Remote URL" value={completeForm.remote_url} onChange={(event) => setCompleteForm((current) => ({ ...current, remote_url: event.target.value }))} placeholder="remote URL" /><Input disabled={readOnly} aria-label="Remote status" value={completeForm.remote_status} onChange={(event) => setCompleteForm((current) => ({ ...current, remote_status: event.target.value }))} placeholder="remote status" /></div><Textarea disabled={readOnly} aria-label="Attempt evidence note" value={completeForm.evidence_note} onChange={(event) => setCompleteForm((current) => ({ ...current, evidence_note: event.target.value }))} placeholder="Evidence note" rows={2} /><Input disabled={readOnly} aria-label="Evidence artifact IDs" value={completeForm.evidence_artifact_ids} onChange={(event) => setCompleteForm((current) => ({ ...current, evidence_artifact_ids: event.target.value }))} placeholder="Evidence artifact IDs（任意・カンマ区切り）" /><Button type="submit" size="sm" disabled={readOnly || saving !== null}><Save className="size-3.5" /> Attemptを完了</Button></form>}{needsReconcile && <form className="space-y-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-3" onSubmit={reconcile} aria-label="Reconcile uncertain attempt form"><p className="text-xs font-medium text-amber-800 dark:text-amber-200">Uncertain outcomeを照合</p><div className="grid gap-2 sm:grid-cols-[12rem_minmax(0,1fr)]"><AppSelect disabled={readOnly} aria-label="Reconcile resolution" className="h-8 rounded-md border border-input bg-card px-2 text-xs" value={reconcileResolution} onChange={(event) => setReconcileResolution(event.target.value)}><option value="succeeded">succeeded</option><option value="failed">failed</option></AppSelect><Textarea disabled={readOnly} aria-label="Reconcile evidence" value={reconcileEvidence} onChange={(event) => setReconcileEvidence(event.target.value)} placeholder="照合エビデンス（必須）" rows={2} required /></div><Input disabled={readOnly} aria-label="Reconcile evidence artifact IDs" value={reconcileEvidenceArtifactIds} onChange={(event) => setReconcileEvidenceArtifactIds(event.target.value)} placeholder="Evidence artifact IDs（任意・カンマ区切り）" /><Button type="submit" size="sm" variant="outline" disabled={readOnly || saving !== null}><History className="size-3.5" /> Reconcile</Button></form>}{error ? <ErrorNotice error={error} /> : null}</CardContent></Card>

      <ActionPackagePanel action={action} />

      <ActionReceiptPanel action={action} />

      <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Timeline<HelpPopover label="Timeline">作成・改訂・承認・Attemptのイベントを再読込できます。</HelpPopover></CardTitle></CardHeader><CardContent className="pt-1">{action.timeline?.length ? <ol className="space-y-2 border-l border-border pl-4">{action.timeline.map((entry, index) => <li key={entry.id || `${entry.created_at}-${index}`} className="relative text-xs"><span className="absolute -left-[1.18rem] top-1.5 size-2 rounded-full border border-primary bg-background" /><div className="flex flex-wrap justify-between gap-2"><span className="font-medium">{entry.event_type || entry.type || "event"}</span><time className="text-muted-foreground">{entry.created_at || "—"}</time></div>{entry.detail ? <p className="mt-0.5 whitespace-pre-wrap text-muted-foreground">{entry.detail}</p> : null}</li>)}</ol> : <EmptyState>Timeline eventはまだありません。</EmptyState>}</CardContent></Card>
    </div>
  );
}

function ActionDetailLoader({ actionId, idempotencyKey, readOnly, onChanged }: { actionId: string; idempotencyKey: string; readOnly: boolean; onChanged: (action: OperationsAction) => void }) {
  const [reloadToken, setReloadToken] = useState(0);
  const [result, setResult] = useState<{ reloadToken: number; action: OperationsAction | null; error: unknown } | null>(null);
  const currentResult = result?.reloadToken === reloadToken ? result : null;

  useEffect(() => {
    let active = true;
    operationsApi.getAction(actionId).then(
      (action) => {
        if (active) setResult({ reloadToken, action, error: null });
      },
      (error: unknown) => {
        if (active) setResult({ reloadToken, action: null, error });
      },
    );
    return () => { active = false; };
  }, [actionId, reloadToken]);

  if (!currentResult) {
    return <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Action detailを読み込み中…</div>;
  }
  if (!currentResult.action) {
    return <div className="space-y-3"><p className="break-all text-sm">Action {actionId}を読み込めませんでした。</p><ErrorNotice error={currentResult.error} /><Button type="button" size="sm" variant="outline" onClick={() => setReloadToken((value) => value + 1)}>再試行</Button></div>;
  }
  return <ActionDetailPanel action={currentResult.action} idempotencyKey={idempotencyKey} readOnly={readOnly} onChanged={(next) => {
    setResult((current) => current?.reloadToken === reloadToken
      ? { reloadToken, action: next, error: null }
      : current);
    onChanged(next);
  }} onReload={() => setReloadToken((value) => value + 1)} />;
}

function ActionsPanel({ initialActionId, reloadToken, idempotencyKey, readOnly }: { initialActionId: string | null; reloadToken: number; idempotencyKey: string; readOnly: boolean }) {
  const [actions, setActions] = useState<OperationsAction[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initialActionId);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const loadActions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await operationsApi.listActions();
      setActions(result);
      // Explicit targets can be outside the first page of the action list.
      setSelectedId((current) => current ?? result[0]?.id ?? null);
    } catch (loadError) {
      setError(loadError);
    } finally {
      setLoading(false);
    }
  }, []);

  const applyActionChange = useCallback((next: OperationsAction) => {
    setActions((current) => current.map((item) => item.id === next.id ? next : item));
  }, []);

  useEffect(() => { void loadActions(); }, [loadActions, reloadToken]);

  return (
    <div className="space-y-4" data-testid="operations-actions-panel">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="inline-flex items-center gap-1 text-lg font-semibold tracking-tight">Approvals / Actions<HelpPopover label="Approvals / Actions">{readOnly ? "Canonical payloadとActionの履歴を閲覧します。" : "Canonical payloadを確認し、承認・改訂・手動実行・不確実結果の照合を行います。"}</HelpPopover></h2></div><Button type="button" variant="outline" size="sm" onClick={() => void loadActions()} disabled={loading}><RefreshCw className={cn("size-3.5", loading && "animate-spin")} /> Timeline / actionsを更新</Button></div>
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(15rem,22rem)_minmax(0,1fr)]"><Card size="sm" className="min-w-0"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Action一覧<span className="text-xs font-normal text-muted-foreground">{actions.length}件</span><HelpPopover label="Action一覧">明示的なidempotency keyで更新</HelpPopover></CardTitle></CardHeader><CardContent className="space-y-1.5 pt-1">{loading && !actions.length ? <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div> : actions.length ? actions.map((action) => <button key={action.id} type="button" className={cn("group relative w-full rounded-md border-l-2 px-3 py-2.5 text-left transition-colors", selectedId === action.id ? "border-primary bg-primary/5" : "border-transparent hover:border-border hover:bg-muted/40")} aria-current={selectedId === action.id ? "page" : undefined} onClick={() => setSelectedId(action.id)}><div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-xs">{action.id}</span><StatusPill status={action.status} /></div><p className="mt-1 text-[10px] text-muted-foreground">v{action.version ?? action.action_version ?? "—"} · draft {action.application_draft_id || "—"}</p></button>) : <EmptyState>Actionはまだありません。OpportunitiesのDraftから作成できます。</EmptyState>}<ErrorNotice error={error} /></CardContent></Card><Card size="sm" className="min-w-0"><CardContent className="pt-4">{selectedId ? <ActionDetailLoader key={selectedId} actionId={selectedId} idempotencyKey={idempotencyKey} readOnly={readOnly} onChanged={applyActionChange} /> : <EmptyState>左のActionを選択してください。</EmptyState>}</CardContent></Card></div>
    </div>
  );
}

export function OperationsWorkspace() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const navigationSearchParams = useRef(searchParams);
  // Shell registrations reuse equivalent navigation nodes, including their callbacks.
  useEffect(() => { navigationSearchParams.current = searchParams; }, [searchParams]);
  const runtime = useOptionalRuntimeContext();
  const companyRuntimeFeatures = runtime?.runtimeFeatures?.application_features;
  const companyRuntimeEnabled =
    companyRuntimeFeatures?.virtual_company === true &&
    companyRuntimeFeatures?.autonomous_agent_runtime === true;

  const routedSection = operationsSectionFromQuery(searchParams);
  const requestedActionId = searchParams?.get("action")?.trim() || null;
  const mapOverview = usesOperationsMap(searchParams);
  // A restrictive display hint only; server-side authorization remains canonical.
  const actionsReadOnly = searchParams?.get("read_only") === "1";

  const [section, setSection] = useState<OperationsSection>(routedSection);
  const fillsPanel = section === "overview" || section === "agents";

  const [idempotencyKey, setIdempotencyKey] =
    useState(newIdempotencyKey);
  const [connections, setConnections] =
    useState<OperationsConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] =
    useState(true);
  const [connectionsError, setConnectionsError] =
    useState<unknown>(null);
  const [actionFocus, setActionFocus] = useState({
    requestedActionId,
    focusedActionId: requestedActionId,
  });
  // Synchronize navigation before rendering a detail for the previous URL.
  if (actionFocus.requestedActionId !== requestedActionId) {
    setActionFocus({ requestedActionId, focusedActionId: requestedActionId });
  }
  const [actionsReloadToken, setActionsReloadToken] =
    useState(0);

  useEffect(() => {
    setSection(routedSection);
  }, [routedSection]);

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
    (next: OperationsSection, actionId?: string) => {
      setSection(next);
      const latestSearchParams = navigationSearchParams.current;
      const currentQuery =
        latestSearchParams && typeof latestSearchParams.toString === "function"
          ? latestSearchParams.toString()
          : "";
      const params = new URLSearchParams(
        currentQuery === "[object Object]" ? "" : currentQuery,
      );
      params.set("tab", next);
      if (actionId) params.set("action", actionId);
      const query = params.toString();
      router.replace(`/operations${query ? `?${query}` : ""}`, { scroll: false });
    },
    [router],
  );

  useWorkspaceShellRegistration({
    id: "operations-workspace",
    routeKey: pathname || "/operations",
    workspaceNavigation: (
      <aside className="flex h-full min-h-0 flex-col" data-workspace="operations">
        <OperationsWorkspaceNavigation
          activeSection={section}
          onSelect={selectSection}
          companyRuntimeEnabled={companyRuntimeEnabled}
        />
      </aside>
    ),
    priority: 30,
  });

  const handleActionCreated = (
    action: OperationsAction,
  ) => {
    setActionFocus({ requestedActionId, focusedActionId: action.id || null });
    setActionsReloadToken((value) => value + 1);
    setIdempotencyKey(newIdempotencyKey());
    selectSection("actions", action.id);
  };

  return (
    <div
      className={cn("flex min-w-0 w-full flex-col bg-background", fillsPanel ? "h-full min-h-0 overflow-hidden" : "min-h-full")}
      data-testid="operations-workspace"
    >
      <main className={cn("min-h-0 min-w-0 flex-1", fillsPanel ? "flex overflow-hidden" : "overflow-y-auto")}>
        <div className={fillsPanel ? `flex min-h-0 min-w-0 w-full flex-1 flex-col ${section === "agents" ? "p-4 sm:p-6" : ""}` : "w-full space-y-5 p-4 pb-12 sm:p-6"}>
          {section === "studio" || section === "overview" || section === "agents" || section === "work" || section === "activity" ? null : section === "personas" ? (
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
              <h1 className="mt-1 inline-flex items-center gap-1 text-2xl font-semibold tracking-tight">
                Content Pipeline
                <HelpPopover label="Content Pipeline">
                  ContentItemからPlatform別のtyped variantを作り、immutable revision・QA・Rights・Readinessを確認します。
                </HelpPopover>
              </h1>
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
                <h1 className="mt-1 inline-flex items-center gap-1 text-2xl font-semibold tracking-tight">
                  Engagement Operations
                  <HelpPopover label="Engagement Operations">
                    Connections → Opportunities → Approval / Actionを一つの監査可能な流れで扱います。
                  </HelpPopover>
                </h1>
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
            id={`operations-panel-${section}`}
            role="region"
            className={fillsPanel ? "flex min-h-0 flex-1 flex-col" : undefined}
            aria-label={
              OPERATIONS_ALL_SECTIONS.find(
                (item) => item.id === section,
              )?.label
            }
          >
            {section === "studio" ? <PersonaWorkspace /> : null}
            {section === "agents" ? <EmployeeWorkspace /> : null}
            {section === "overview" || section === "work" || section === "activity" ? (
              <>
                {section === "overview" && !mapOverview ? (
                  <OperationsOverview query={searchParams?.toString() ?? ""} onNavigate={href => router.push(href, { scroll: false })} />
                ) : (
                  <>
                    {section === "overview" ? <Link className="shrink-0 border-b border-border px-4 py-2 text-sm text-primary hover:underline" href={`/operations?${(() => { const query = new URLSearchParams(searchParams?.toString()); for (const key of ["overview_view", "graph_node", "graph_work", "execution_focus", "execution_node", "work_view"]) query.delete(key); query.set("tab", "overview"); return query.toString(); })()}`}>Operationsの状況へ戻る</Link> : null}
                    {section === "overview" ? <div className="shrink-0 px-3 py-2"><MediaPortfolioSummary compact /></div> : null}
                    <OperationsCommandCenter view={section} />
                  </>
                )}
                {section === "work" ? <OperationsLocalTaskPanel /> : null}
              </>
            ) : null}

            {section === "personas" ? (
              <MediaPersonaPanel initialPersonaId={searchParams?.get("persona") ?? undefined} />
            ) : null}

            {section === "research" ? (
              <div className="space-y-4"><MediaBootstrapImportPanel /><MediaResearchPanel /></div>
            ) : null}

            {section === "automation" ? (
              <MediaAutomationPanel />
            ) : null}

            {section === "generation" ? (
              <MediaGenerationPanel />
            ) : null}

            {section === "pipeline" ? (
              <MediaContentVariantPanel initialContentItemId={searchParams?.get("content_item_id") ?? undefined} initialOutputRef={searchParams?.get("output_ref") ?? undefined} />
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
                initialOpportunityId={searchParams?.get("opportunity_id") ?? undefined}
                onActionCreated={
                  handleActionCreated
                }
              />
            ) : null}

            {section === "actions" ? (
              <ActionsPanel
                key={actionFocus.focusedActionId}
                initialActionId={actionFocus.focusedActionId}
                readOnly={actionsReadOnly}
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
