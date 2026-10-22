"use client";

import { AlertTriangle, CheckCircle2, CircleDashed, Code2, Download, FileArchive, HelpCircle, ImageIcon, XCircle } from "lucide-react";
import {
  type DevelopmentArtifactRef,
  type DevelopmentCheck,
  type DevelopmentResult,
  type EngagementOpportunity,
  type SpecialistAssessment,
} from "@/lib/operations-api";
import { withBasePath } from "@/lib/base-path";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { cn } from "@/lib/utils";

/** Authenticated (session-cookie) download URL through the Python proxy. */
export function operationsArtifactContentUrl(artifactId: string): string {
  return withBasePath(`/api/python-proxy/operations/artifacts/${encodeURIComponent(artifactId)}/content`);
}

export const DEVELOPMENT_WORKER_KINDS = ["web_development", "app_development"] as const;

export function isDevelopmentKind(value: string | null | undefined): boolean {
  const kind = String(value || "").toLowerCase();
  return kind === "web_development" || kind === "app_development" || kind === "code";
}

export function specialistNameForKind(value: string | null | undefined): string {
  const kind = String(value || "").toLowerCase();
  if (kind === "web_development") return "Web制作担当";
  if (kind === "app_development" || kind === "code") return "アプリ開発担当";
  return "CrowdWorks担当";
}

export function assignedAgentName(opportunity: EngagementOpportunity): string {
  return (
    opportunity.assigned_agent_name
    || opportunity.development?.agent?.display_name
    || opportunity.specialist_assessment?.recommended_agent?.display_name
    || specialistNameForKind(opportunity.worker_kind)
  );
}

export function feasibilityLabel(value: string | null | undefined): string {
  const labels: Record<string, string> = { feasible: "実現可能", conditional: "条件付きで可能", infeasible: "実現困難" };
  return labels[String(value || "").toLowerCase()] ?? "専門評価待ち";
}

export function environmentLabel(value: string | null | undefined): string {
  const labels: Record<string, string> = { ready: "用意できる", partial: "一部のみ", unavailable: "用意できない" };
  return labels[String(value || "").toLowerCase()] ?? "未評価";
}

export function developmentStatusLabel(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    running: "開発中",
    ready_for_review: "最終レビュー待ち",
    verification_failed: "検証失敗・要対応",
    blocked: "停止・要対応",
    uncertain: "結果不明・要照合",
  };
  return labels[String(value || "").toLowerCase()] ?? "開発待ち";
}

function formatYen(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value).toLocaleString("ja-JP")}円` : "—";
}

function formatHours(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 10) / 10}h` : "—";
}

function formatBytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 102.4) / 10} KB`;
  return `${Math.round(value / 104857.6) / 10} MB`;
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border bg-background/60 p-2">
      <p className="text-[10px] text-muted-foreground">{label}</p>
      <p className="mt-1 text-sm font-semibold">{value}</p>
    </div>
  );
}

function ListBlock({ title, items, empty, tone = "default" }: { title: string; items?: string[] | null; empty: string; tone?: "default" | "warning" }) {
  const values = (items ?? []).filter(Boolean);
  return (
    <div className={cn("rounded-md border p-2 text-xs", tone === "warning" && values.length ? "border-amber-500/40 bg-amber-500/5" : "")}>
      <p className="font-semibold">{title}</p>
      {values.length ? (
        <ul className="mt-1 list-disc space-y-0.5 pl-4 leading-5">
          {values.map((item, index) => <li key={`${title}-${index}`}>{item}</li>)}
        </ul>
      ) : (
        <p className="mt-1 text-muted-foreground">{empty}</p>
      )}
    </div>
  );
}

export function SpecialistAssessmentPanel({ opportunity }: { opportunity: EngagementOpportunity }) {
  const assessment: SpecialistAssessment | null | undefined = opportunity.specialist_assessment;
  const specialist = assignedAgentName(opportunity);
  return (
    <div className="space-y-2 rounded-md border border-primary/25 bg-background/60 p-3" data-testid="specialist-assessment">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs font-semibold"><Code2 className="size-3.5 text-primary" /> <span className="inline-flex items-center gap-1">{specialist}の応募前実現性評価<HelpPopover label={`${specialist}の応募前実現性評価`}>Web制作・アプリ開発案件は、応募案を作る前に{specialist}が実現性・技術・不足要件を評価します。評価が終わるまで応募は提案されません。</HelpPopover></span></p>
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-[11px] font-medium",
            assessment?.feasibility === "feasible" && "border-emerald-500/40 text-emerald-700 dark:text-emerald-300",
            assessment?.feasibility === "conditional" && "border-amber-500/40 text-amber-700 dark:text-amber-300",
            assessment?.feasibility === "infeasible" && "border-destructive/40 text-destructive",
          )}
          data-feasibility={assessment?.feasibility ?? "pending"}
        >
          {feasibilityLabel(assessment?.feasibility)}
        </span>
      </div>
      {assessment ? (
        <>
          <p className="text-xs leading-5"><span className="font-semibold">作るもの: </span>{assessment.deliverable_summary || "—"}</p>
          {assessment.feasibility_reason ? <p className="text-[11px] leading-5 text-muted-foreground">判断理由: {assessment.feasibility_reason}</p> : null}
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="想定技術" value={assessment.tech_stack?.length ? assessment.tech_stack.join(" / ") : "未定"} />
            <Metric label="専門担当の想定工数" value={formatHours(assessment.estimated_effort_hours)} />
            <Metric label="想定コスト" value={formatYen(assessment.estimated_cost)} />
            <Metric label="開発・検証環境" value={environmentLabel(assessment.environment_ready)} />
            <Metric label="期待利益（専門見積り）" value={formatYen(assessment.expected_profit)} />
            <Metric label="利益 / 時（専門見積り）" value={`${formatYen(assessment.expected_profit_per_hour)}/h`} />
            <Metric label="人間作業時間" value={typeof assessment.expected_human_minutes === "number" ? `約${Math.round(assessment.expected_human_minutes)}分` : "未評価"} />
            <Metric label="専門担当の確度" value={typeof assessment.confidence === "number" ? `${Math.round(assessment.confidence * 100)}%` : "—"} />
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <ListBlock title="開発開始を阻む要件" items={assessment.development_blockers} empty="特記事項なし" tone="warning" />
            <ListBlock title="必要な顧客提供物" items={assessment.client_inputs_required} empty="特記事項なし" tone="warning" />
            <ListBlock title="未確定要件" items={assessment.unresolved_requirements} empty="特記事項なし" tone="warning" />
            <ListBlock title="主なリスク" items={assessment.risks} empty="特記事項なし" />
            <ListBlock title="人間が必要になる作業" items={assessment.human_tasks} empty="特記事項なし" />
          </div>
          {assessment.environment_notes?.length ? <ListBlock title="環境メモ" items={assessment.environment_notes} empty="" /> : null}
        </>
      ) : null}
    </div>
  );
}

function CheckIcon({ status }: { status: string }) {
  if (status === "passed") return <CheckCircle2 className="size-3.5 text-emerald-600" />;
  if (status === "failed" || status === "error") return <XCircle className="size-3.5 text-destructive" />;
  if (status === "timeout") return <HelpCircle className="size-3.5 text-amber-600" />;
  return <CircleDashed className="size-3.5 text-muted-foreground" />;
}

function checkStatusLabel(status: string): string {
  const labels: Record<string, string> = { passed: "確認済み", failed: "失敗", timeout: "結果不明", not_available: "未検証", skipped: "未実施", error: "実行エラー" };
  return labels[status] ?? status;
}

function artifactRoleLabel(role: string): string {
  const labels: Record<string, string> = {
    source_archive: "ソース一式",
    build_archive: "ビルド成果物",
    readme: "README",
    validation_report: "検証レポート",
    validation_report_json: "検証レポート(JSON)",
    verification_logs: "ビルド・テストログ",
    screenshots: "プレビュー",
  };
  return labels[role] ?? role;
}

function ArtifactLink({ role, artifact }: { role: string; artifact: DevelopmentArtifactRef }) {
  return (
    <a
      href={operationsArtifactContentUrl(artifact.id)}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center justify-between gap-2 rounded-md border border-border/70 bg-background/60 px-2.5 py-2 text-xs hover:border-primary/40 hover:bg-muted/40"
      data-testid={`development-artifact-${role}`}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <FileArchive className="size-3.5 shrink-0 text-primary" />
        <span className="font-medium">{artifactRoleLabel(role)}</span>
        <span className="truncate text-muted-foreground">{artifact.filename}</span>
      </span>
      <span className="flex shrink-0 items-center gap-1 text-muted-foreground">{formatBytes(artifact.size_bytes)} <Download className="size-3" /></span>
    </a>
  );
}

export function DevelopmentReviewPanel({ development }: { development: DevelopmentResult }) {
  const status = String(development.status || "");
  const checks: DevelopmentCheck[] = development.checks ?? [];
  const artifacts = development.artifacts ?? {};
  const screenshots = Array.isArray(artifacts.screenshots) ? artifacts.screenshots : [];
  const fileArtifacts = Object.entries(artifacts).filter(
    (entry): entry is [string, DevelopmentArtifactRef] => entry[0] !== "screenshots" && !!entry[1] && !Array.isArray(entry[1]),
  );
  const ready = status === "ready_for_review";
  const reason = development.blocking_reason || development.uncertain_reason;
  const failedChecks = checks.filter((check) => ["failed", "error", "timeout"].includes(check.status));
  const unverifiedChecks = checks.filter((check) => ["not_available", "skipped"].includes(check.status));
  const verificationSummary = failedChecks.length
    ? "独立検証に失敗しました"
    : unverifiedChecks.length
      ? "一部の検証は未実施・未検証です"
      : checks.length
        ? "実行された独立検証はすべて成功しました"
        : "独立検証はまだ実行されていません";
  return (
    <Card size="sm" className={cn(ready ? "border-emerald-500/40" : status === "running" ? "border-primary/30" : "border-amber-500/40")} data-testid="development-review">
      <CardHeader className="border-b border-border/70">
        <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
          <Code2 className="size-4 text-primary" />
          <span className="inline-flex items-center gap-1">
            開発結果・最終レビュー
            <HelpPopover label="開発結果・最終レビュー">
              顧客案件ごとの独立ワークスペースで開発しました。
            </HelpPopover>
          </span>
          <span className="rounded-full border px-2 py-0.5 text-[11px] font-medium" data-development-status={status}>{developmentStatusLabel(status)}</span>
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          {development.agent?.display_name || "専門Agent"}が開発しました。{verificationSummary}。最終納品・本番公開は自動では行いません。内容を確認してから人間が納品してください。
        </p>
      </CardHeader>
      <CardContent className="space-y-3 pt-3">
        {reason ? (
          <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-600" />
            <span>{status === "uncertain" ? "結果が確定できないため自動再実行していません: " : "停止理由: "}{reason}</span>
          </div>
        ) : null}
        {development.requirements?.status === "awaiting_client_input" ? (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs">
            <p className="font-semibold">必須要件が未解消のため開発を開始していません</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {(development.requirements.items ?? []).map((item) => <li key={item}>{item}</li>)}
            </ul>
          </div>
        ) : null}
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="担当Agent" value={development.agent?.display_name || "—"} />
          <Metric label="実行方法" value={`${development.runner?.runner || "—"}${development.runner?.model ? ` / ${development.runner.model}` : ""}`} />
          <Metric label="プロジェクト種別" value={development.project_type || "—"} />
          <Metric label="ワークスペース" value={development.workspace?.name ? `${development.workspace.name}${development.workspace.isolated_git ? "（独立git）" : ""}` : "—"} />
        </div>
        <div className="rounded-md border bg-background/60 p-3 text-xs leading-5">
          <p className="font-semibold">何を作ったか</p>
          <p className="mt-1 whitespace-pre-wrap">{development.agent_report?.summary || "エージェントの作業報告はありません。ソースと検証結果を確認してください。"}</p>
        </div>
        <div>
          <p className="mb-1 text-xs font-semibold">AoiTalkによる独立検証（ビルド・テスト・表示）</p>
          {checks.length ? (
            <ul className="grid gap-1 sm:grid-cols-2" aria-label="開発検証結果">
              {checks.map((check) => (
                <li key={check.id} className="flex items-start gap-1.5 rounded-md border border-border/60 px-2 py-1.5 text-xs" data-check-status={check.status}>
                  <CheckIcon status={check.status} />
                  <span className="min-w-0">
                    <span className="font-medium">{check.label}</span>
                    <span className="ml-1 text-muted-foreground">{checkStatusLabel(check.status)}{check.required ? "・必須" : ""}</span>
                    {check.detail ? <span className="block truncate text-[10px] text-muted-foreground" title={check.detail}>{check.detail}</span> : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-muted-foreground">まだ検証結果はありません。</p>}
        </div>
        {screenshots.length ? (
          <div>
            <p className="mb-1 flex items-center gap-1 text-xs font-semibold"><ImageIcon className="size-3.5" /> プレビュー</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {screenshots.map((shot) => (
                <a key={shot.id} href={operationsArtifactContentUrl(shot.id)} target="_blank" rel="noopener noreferrer" className="block overflow-hidden rounded-md border bg-muted/30">
                  {/* eslint-disable-next-line @next/next/no-img-element -- authenticated artifact bytes, not a static asset */}
                  <img src={operationsArtifactContentUrl(shot.id)} alt={`${shot.viewport === "mobile" ? "モバイル" : "PC"}幅プレビュー`} className="max-h-72 w-full object-cover object-top" loading="lazy" />
                  <span className="block px-2 py-1 text-[10px] text-muted-foreground">{shot.viewport === "mobile" ? "モバイル幅 390px" : "PC幅 1366px"}</span>
                </a>
              ))}
            </div>
          </div>
        ) : null}
        <div>
          <p className="mb-1 text-xs font-semibold">成果物（Artifact）</p>
          {fileArtifacts.length ? (
            <div className="grid gap-1.5 sm:grid-cols-2">
              {fileArtifacts.map(([role, artifact]) => <ArtifactLink key={role} role={role} artifact={artifact} />)}
            </div>
          ) : <p className="text-xs text-muted-foreground">登録済みの成果物はありません。</p>}
        </div>
        <div className="grid gap-2 sm:grid-cols-3">
          <ListBlock title="顧客へ渡す予定のファイル" items={development.deliverables} empty="まだありません" />
          <ListBlock title="未検証項目" items={development.unverified} empty="なし" tone="warning" />
          <ListBlock title="既知の問題" items={development.known_issues} empty="なし" tone="warning" />
        </div>
      </CardContent>
    </Card>
  );
}
