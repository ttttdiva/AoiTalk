"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { HelpPopover } from "@/components/ui/help-popover";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  MediaOperationsApiError,
  mediaOperationsApi,
  type MediaStockItem,
} from "@/lib/media-operations-api";
import { withoutTestFixtures } from "@/lib/operations-test-data";
import { workspaceHref } from "@/lib/persona-workspace-model";
import { TestDataToggle } from "@/components/operations/test-data-toggle";

const STOCK_STATUSES: Record<MediaStockItem["stock_status"], string> = {
  adopted: "採用済み",
  awaiting_adoption: "採用待ち",
  needs_review: "要再確認",
};

const BLOCKER_LABELS: Record<string, string> = {
  account_binding_missing: "公開先アカウントが未接続です（ストック採用では確認不要）",
  persona_account_binding_missing_or_mismatch: "公開先アカウントと人格の紐付けがありません（ストック採用では確認不要）",
  analysis_evidence_missing: "fact / signal の URL または計測値参照がありません",
  content_variant_missing: "媒体別の内容がありません",
  content_variant_limit_exceeded: "媒体別の内容が上限を超えています",
  content_variant_scope_mismatch: "媒体別の内容の所有者またはProjectを確認できません",
  content_item_revision_mismatch: "媒体別の内容が現在の企画revisionに結び付いていません",
  content_item_hash_changed: "企画のハッシュが採用時から変わりました",
  revision_missing: "媒体別内容のrevisionがありません",
  revision_hash_invalid: "媒体別内容のrevision hashを検証できません",
  generation_output_manifest_invalid: "選択した生成物の参照を検証できません",
  generation_output_payload_invalid: "内容に含まれる生成物参照が不正です",
  generation_output_unpinned: "内容で使う生成物がrevisionに固定されていません",
  generation_output_missing: "選択した生成物が見つかりません",
  generation_output_scope_mismatch: "選択した生成物の所有者またはProjectが一致しません",
  generation_output_hash_mismatch: "選択した生成物のhashが一致しません",
  qa_missing: "QAが未記録です",
  qa_revision_mismatch: "QAの対象revisionが現在の内容と異なります",
  qa_hash_invalid: "QA結果のhashを検証できません",
  qa_variant_mismatch: "QA結果の媒体別内容が一致しません",
  qa_failed: "QAに未解決項目があります",
  qa_review_required: "QAに再確認が必要です",
  rights_missing: "権利確認が未記録です",
  rights_revision_mismatch: "権利確認の対象revisionが現在の内容と異なります",
  rights_hash_invalid: "権利確認結果のhashを検証できません",
  rights_variant_mismatch: "権利確認の媒体別内容が一致しません",
  rights_blocked: "権利確認に未解決項目があります",
  rights_review_required: "権利確認に再確認が必要です",
  stock_snapshot_changed: "採用後にrevision・QA・権利確認または根拠が変わりました",
  stock_state_changed: "採用状態またはversionが記録と一致しません",
  stock_adoption_record_missing: "ready状態ですが採用記録がありません",
  x_api_alt_text_unsupported: "X API では画像の代替テキストを付けられません",
};

export function blockerLabel(blocker: string): string {
  return BLOCKER_LABELS[blocker] ?? `確認が必要です（${blocker}）`;
}

/** The production stage that resolves a stock blocker. */
export function blockerStage(blocker: string): "brief" | "generation" | "qa" {
  if (blocker.startsWith("generation_output_")) return "generation";
  if (blocker === "analysis_evidence_missing" || blocker.startsWith("content_item_")) return "brief";
  return "qa";
}

/** One read serves the persona's product list and its next actions. */
export function useStockItems() {
  return useSWR("media-content-stock", mediaOperationsApi.listStockItems, {
    shouldRetryOnError: false,
    revalidateOnFocus: false,
  });
}

/** Stock items of one persona Project (or all), QA / acceptance fixtures hidden unless asked. */
export function visibleStockItems(items: readonly MediaStockItem[] | undefined, projectId?: string, includeTest = false): MediaStockItem[] {
  return withoutTestFixtures(
    (items ?? []).filter(item => !projectId || item.project_id === projectId),
    item => [item.title, item.brand_name],
    includeTest,
  );
}

export function stockItemHref(personaId: string, item: MediaStockItem): string {
  const blockers = item.blockers ?? [];
  return `${workspaceHref(personaId, blockers.length ? blockerStage(blockers[0]) : "brief")}&content=${encodeURIComponent(item.id)}`;
}

function adoptionError(error: unknown): string {
  if (error instanceof MediaOperationsApiError && error.status === 409) {
    return "企画や確認結果が更新されています。一覧を再取得して内容を確認してください。";
  }
  if (error instanceof MediaOperationsApiError && error.status === 403) {
    return "この操作は本人の認証でのみ実行できます。ログイン状態を確認してください。";
  }
  return "ストック状態を更新できませんでした。一覧を再取得してから再試行してください。";
}

function newIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `stock-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function ContentStockPanel({ projectId, personaId }: { projectId?: string; personaId?: string }) {
  const stock = useStockItems();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [includeTest, setIncludeTest] = useState(false);
  const items = visibleStockItems(stock.data, projectId, includeTest);
  const hiddenTestCount = includeTest ? 0 : visibleStockItems(stock.data, projectId, true).length - items.length;

  async function updateItem(item: MediaStockItem, action: "adopt" | "withdraw") {
    setBusyId(item.id);
    setErrorMessage(null);
    try {
      await mediaOperationsApi.changeStockAdoption(
        item.id,
        action,
        item.content_hash,
        item.version,
        newIdempotencyKey(),
      );
      await stock.mutate();
    } catch (error) {
      setErrorMessage(adoptionError(error));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section aria-label="制作物" className="min-w-0 rounded-xl border bg-card p-4" data-testid="content-stock-panel">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-1 text-sm font-semibold">
          制作物
          <HelpPopover label="制作物">
            保存済みの企画と、媒体別内容の確認状況です。ストック採用は内部の準備状態を記録するだけで、生成・予約・公開・送信は行いません。採用後に内容、QA、権利確認または根拠が変わると再確認になります。
          </HelpPopover>
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <TestDataToggle hiddenCount={hiddenTestCount} checked={includeTest} onChange={setIncludeTest} />
          <Button type="button" variant="ghost" size="sm" disabled={stock.isLoading} onClick={() => void stock.mutate()}>
            一覧を更新
          </Button>
        </div>
      </header>

      {stock.error ? (
        <p role="alert" className="mt-3 rounded-md border border-destructive/30 px-3 py-2 text-sm text-destructive">
          制作物の一覧を取得できませんでした。一覧を更新してください。
        </p>
      ) : null}
      {errorMessage ? (
        <p role="alert" className="mt-3 rounded-md border border-destructive/30 px-3 py-2 text-sm text-destructive">
          {errorMessage}
        </p>
      ) : null}
      {stock.isLoading ? <p role="status" className="mt-3 text-sm">制作物を確認中…</p> : null}
      {!stock.isLoading && !stock.error && items.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">保存済みの企画はまだありません。</p>
      ) : null}

      {items.length ? (
        <ul className="mt-3 divide-y rounded-lg border">
          {items.map((item) => {
            const pending = busyId === item.id;
            const blockers = item.blockers ?? [];
            const findings = item.findings ?? [];
            const variants = item.variants ?? [];
            const canAdopt = item.status === "draft" && item.blockers !== undefined && blockers.length === 0;
            const canWithdraw = item.status === "ready";
            return (
              <li key={item.id} className="min-w-0 space-y-1.5 p-3" data-stock-item={item.id}>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    {!projectId ? <p className="text-xs text-muted-foreground">{item.brand_name}</p> : null}
                    <h3 className="break-words text-sm font-medium">{item.title}</h3>
                    <p className="text-xs text-muted-foreground">
                      {STOCK_STATUSES[item.stock_status]}
                      {variants.map(variant => ` · ${variant.platform}: QA ${variant.qa_result ?? "未記録"} / 権利 ${variant.rights_result ?? "未記録"}`).join("")}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-2">
                    {personaId ? <Link className={buttonVariants({ variant: "outline", size: "sm" })} href={stockItemHref(personaId, item)}>開く</Link> : null}
                    {canAdopt ? (
                      <Button type="button" size="sm" disabled={pending} onClick={() => void updateItem(item, "adopt")}>
                        {pending ? "更新中…" : "制作ストックに採用"}
                      </Button>
                    ) : null}
                    {canWithdraw ? (
                      <Button type="button" variant="outline" size="sm" disabled={pending} onClick={() => void updateItem(item, "withdraw")}>
                        {pending ? "更新中…" : "ストックから取り下げ"}
                      </Button>
                    ) : null}
                  </div>
                </div>
                {blockers.length ? (
                  <p className="text-xs">
                    <span className="font-medium text-amber-700 dark:text-amber-300">次: </span>{blockerLabel(blockers[0])}
                    {blockers.length > 1 ? <span className="text-muted-foreground">（ほか {blockers.length - 1} 件）</span> : null}
                  </p>
                ) : null}
                {blockers.length > 1 || findings.length ? (
                  <details className="text-xs text-muted-foreground">
                    <summary className="cursor-pointer">詳細</summary>
                    {blockers.length > 1 ? (
                      <ul className="mt-1 list-disc space-y-0.5 pl-4">
                        {blockers.slice(1).map((blocker, index) => <li key={`${blocker}-${index}`}>{blockerLabel(blocker)}</li>)}
                      </ul>
                    ) : null}
                    {findings.length ? (
                      <ul className="mt-1 space-y-0.5">
                        {findings.map((finding) => (
                          <li key={finding.id} data-testid={`stock-finding-${finding.id}`}>
                            <span className="font-medium text-foreground">
                              {finding.kind === "fact" ? "事実" : finding.kind === "signal" ? "兆候" : "仮説"}
                            </span>
                            {" · "}{finding.statement}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </details>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
