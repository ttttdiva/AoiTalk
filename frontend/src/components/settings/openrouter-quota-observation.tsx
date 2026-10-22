"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCcw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { pyFetch } from "./llm-model-section-types";

type Local = {
  configured_limit: number;
  safety_margin: number;
  effective_cap: number;
  used: number;
  reserved: number;
};

type Observed = {
  status: "unverified" | "observed" | "stale" | "failed";
  used: number | null;
  limit: number | null;
  remaining: number | null;
  observed_at: string | null;
  failure: string;
};

type QuotaView = { local: Local | null; provider_observed?: Observed };

const statusLabels: Record<string, string> = {
  unverified: "未確認（まだ取得していません）",
  observed: "取得済み",
  stale: "古い観測値（再取得してください）",
  failed: "取得失敗",
};

const failureLabels: Record<string, string> = {
  missing_key: "APIキーが未設定または無効です",
  unsupported_endpoint: "公式OpenRouter以外の接続先のため実測できません（キーは送信していません）",
  redirect_rejected: "接続先がリダイレクトしたため中止しました",
  http_401: "APIキーが無効です（401）",
  http_429: "Providerのレート制限です（429）",
  timeout: "タイムアウトしました",
  network_error: "接続に失敗しました",
  invalid_response: "応答の形式が不正です",
};

const STALE_AFTER_MS = 15 * 60 * 1000;
const CLOCK_TICK_MS = 30 * 1000;

// 取得済みキャッシュは、再取得なしでも15分超またはUTC日跨ぎでstaleにする（表示のみ・Provider通信なし）。
export function effectiveStatus(observed: Observed | undefined, nowMs: number): Observed["status"] {
  const status = observed?.status ?? "unverified";
  if (status !== "observed" || !observed?.observed_at) return status;
  const at = Date.parse(observed.observed_at);
  if (!Number.isFinite(at)) return "stale";
  const crossedUtcDay = new Date(at).toISOString().slice(0, 10) !== new Date(nowMs).toISOString().slice(0, 10);
  return crossedUtcDay || nowMs - at > STALE_AFTER_MS ? "stale" : "observed";
}

const fmt = (value: number) => new Intl.NumberFormat("ja-JP").format(value);

export function OpenRouterQuotaObservation() {
  const [view, setView] = useState<QuotaView | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), CLOCK_TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    let active = true;
    pyFetch<QuotaView>("/free-team/openrouter/quota")
      .then((next) => active && setView(next))
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      setView(await pyFetch<QuotaView>("/free-team/openrouter/quota/refresh", { method: "POST" }));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Provider実測の取得に失敗しました");
    } finally {
      setRefreshing(false);
    }
  }, []);

  const local = view?.local ?? null;
  const observed = view?.provider_observed;
  const status = effectiveStatus(observed, nowMs);
  const hasValues = observed?.limit != null && observed.used != null && observed.remaining != null;

  return (
    <section className="space-y-2 rounded border p-2 text-xs" data-testid="openrouter-quota-observation" aria-label="OpenRouter無料枠の実測">
      <div className="flex items-center justify-between gap-2">
        <div className="font-medium">OpenRouter 無料枠（日次リクエスト・UTC）</div>
        <Button size="sm" variant="outline" disabled={refreshing} onClick={() => void refresh()}>
          {refreshing ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCcw className="h-3 w-3" />}
          実測を取得
        </Button>
      </div>
      {local && (
        <div data-testid="openrouter-local-quota" className="grid gap-1 sm:grid-cols-5">
          <div><span className="block text-[10px] text-muted-foreground">設定上限</span>{fmt(local.configured_limit)}</div>
          <div><span className="block text-[10px] text-muted-foreground">安全量</span>{fmt(local.safety_margin)}</div>
          <div><span className="block text-[10px] text-muted-foreground">実効cap</span>{fmt(local.effective_cap)}</div>
          <div><span className="block text-[10px] text-muted-foreground">ローカル使用済み</span>{fmt(local.used)}</div>
          <div><span className="block text-[10px] text-muted-foreground">ローカル予約中</span>{fmt(local.reserved)}</div>
        </div>
      )}
      <div data-testid="openrouter-provider-observed" role="status" className="space-y-1">
        <div>Provider実測: {statusLabels[status] ?? status}</div>
        {hasValues && (
          <div className="grid gap-1 sm:grid-cols-4">
            <div><span className="block text-[10px] text-muted-foreground">Provider使用済み</span>{fmt(observed.used as number)}</div>
            <div><span className="block text-[10px] text-muted-foreground">Provider上限</span>{fmt(observed.limit as number)}</div>
            <div><span className="block text-[10px] text-muted-foreground">Provider残量</span>{fmt(observed.remaining as number)}</div>
            <div><span className="block text-[10px] text-muted-foreground">観測時刻</span>{observed.observed_at}</div>
          </div>
        )}
        {status === "failed" && <div>{failureLabels[observed?.failure ?? ""] ?? observed?.failure}（数値は表示しません）</div>}
        <p className="text-muted-foreground">実測値は表示専用で、上限・予算・ルーティングは変更しません。契約Tierや無料利用資格の確認ではありません。</p>
      </div>
    </section>
  );
}
