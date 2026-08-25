"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  DollarSign,
  Hash,
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Gift,
  Loader2,
  RefreshCw,
  Calendar,
} from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";
import {
  usageApi,
  type FreeTierResponse,
  type PricingCatalogStatus,
  type TokenUsageAgentRow,
  type TokenUsageDailyPoint,
  type TokenUsageProjectRow,
  type TokenUsageSummary,
  type TokenUsageTotals,
  type TokenUsageUserRow,
  type UsageCostMetrics,
} from "@/lib/ecc-api";
import { pyFetch } from "./llm-model-section-types";

type BreakdownTab = "model" | "project" | "agent" | "user";
type DetailPeriod = "today" | "week" | "month" | "custom";
type BreakdownRequestKind = "daily" | "total" | BreakdownTab;

type BreakdownFetchOptions = {
  /** 日別・合計を取得するか。初回の本日はダッシュボードから復元できる。 */
  includeBase?: boolean;
  /** 取得する内訳タブ。タブ切り替え時はこの1本だけ取得する。 */
  tab?: BreakdownTab;
};

const DETAIL_PERIOD_OPTIONS: Array<{
  value: Exclude<DetailPeriod, "custom">;
  label: string;
}> = [
  { value: "today", label: "本日" },
  { value: "week", label: "1週間" },
  { value: "month", label: "1か月" },
];

/** コスト欄を「—」表示にする pricing status */
const NO_BILLING_STATUSES = new Set(["unknown", "subscription", "local"]);

const EMPTY_COST = "—";

/** 8桁表示でも 0 になる極小値の下限 */
const SMALLEST_DISPLAYABLE = "0.00000001";

function trimTrailingZeros(value: string): string {
  if (!value.includes(".")) return value;
  return value.replace(/0+$/, "").replace(/\.$/, "");
}

/**
 * 小数桁数を min〜max の範囲に収めて描画する。
 * max 桁で丸めたあと末尾の 0 を削り、min 桁に満たなければ 0 で埋める。
 */
function renderDigits(value: number, min: number, max: number): string {
  const trimmed = trimTrailingZeros(value.toFixed(max));
  const [intPart, fracPart = ""] = trimmed.split(".");
  if (fracPart.length >= min) return trimmed;
  return `${intPart}.${fracPart.padEnd(min, "0")}`;
}

/**
 * 金額を桁落ちさせずに表示する。
 * - pricing status が unknown / subscription / local、または値が無い場合は「—」
 * - 0 は `$0.00`
 * - 1 以上は 2〜4 桁、0.0001 以上は 4〜6 桁、それ未満は 8 桁
 *   （固定 4 桁だと極小コストが `$0.0000` に潰れるため）
 */
export function formatCost(
  cost: number | null | undefined,
  pricingStatus?: string,
): string {
  if (pricingStatus && NO_BILLING_STATUSES.has(pricingStatus))
    return EMPTY_COST;
  if (cost === null || cost === undefined) return EMPTY_COST;
  if (typeof cost !== "number" || !Number.isFinite(cost)) return EMPTY_COST;
  if (cost === 0) return "$0.00";

  const sign = cost < 0 ? "-" : "";
  const abs = Math.abs(cost);

  let digits: string;
  if (abs >= 1) {
    digits = renderDigits(abs, 2, 4);
  } else if (abs >= 0.0001) {
    digits = renderDigits(abs, 4, 6);
  } else {
    digits = abs.toFixed(8);
  }

  // 極小すぎて 8 桁でも 0 になる場合は、0 と誤読されないよう下限表記にする。
  if (Number(digits) === 0) return `${sign}<$${SMALLEST_DISPLAYABLE}`;
  return `${sign}$${digits}`;
}

function formatTokens(tokens: number): string {
  return tokens.toLocaleString("ja-JP");
}

function formatPercent(value: number | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return EMPTY_COST;
  return `${value.toFixed(1)}%`;
}

function formatDate(dateStr: string): string {
  const [year, month, day] = dateStr.split("-").map(Number);
  if (year && month && day) return `${month}/${day}`;
  return dateStr;
}

function formatDateRangeLabel(start: string, end: string): string {
  if (!start || !end) return "期間未指定";
  if (start === end) return formatDate(start);
  return `${formatDate(start)}〜${formatDate(end)}`;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "未取得";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("ja-JP");
}

type CalendarDateParts = { year: number; month: number; day: number };

const JST_DATE_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function getJstDateParts(value: Date): CalendarDateParts {
  const parts = JST_DATE_FORMATTER.formatToParts(value);
  const getPart = (type: "year" | "month" | "day") =>
    Number(parts.find((part) => part.type === type)?.value ?? 0);
  return {
    year: getPart("year"),
    month: getPart("month"),
    day: getPart("day"),
  };
}

function shiftCalendarDate(
  date: CalendarDateParts,
  days: number,
): CalendarDateParts {
  const shifted = new Date(Date.UTC(date.year, date.month - 1, date.day));
  shifted.setUTCDate(shifted.getUTCDate() + days);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  };
}

function formatCalendarDate(date: CalendarDateParts): string {
  return `${date.year}-${String(date.month).padStart(2, "0")}-${String(
    date.day,
  ).padStart(2, "0")}`;
}

export function getDateRangeForPeriod(
  period: Exclude<DetailPeriod, "custom">,
  now = new Date(),
): { start: string; end: string } {
  const endDate = getJstDateParts(now);
  let startDate = endDate;
  if (period === "week") {
    startDate = shiftCalendarDate(endDate, -6);
  } else if (period === "month") {
    startDate = { ...endDate, day: 1 };
  }
  return {
    start: formatCalendarDate(startDate),
    end: formatCalendarDate(endDate),
  };
}

type PricingStatusMeta = { label: string; hint?: string };

const PRICING_STATUS_META: Record<string, PricingStatusMeta> = {
  priced: { label: "定価計算", hint: "料金カタログの単価で計算した金額です。" },
  provider_reported: {
    label: "プロバイダ報告額",
    hint: "プロバイダが返した実費をそのまま表示しています。",
  },
  free_incentive: {
    label: "無料枠適用",
    hint: "データ共有インセンティブの無料枠で相殺されています。",
  },
  subscription: {
    label: "サブスクリプション / クォータ制",
    hint: "定額プラン・クォータ制のためリクエスト単位の従量課金はありません。",
  },
  local: {
    label: "API従量課金なし",
    hint: "ローカル実行のため API 課金はありません。電力・GPU・機器費用は未算入です。",
  },
  unknown: {
    label: "料金未登録",
    hint: "料金カタログに単価が無いモデルです。金額を集計できません。",
  },
  mixed: {
    label: "混在",
    hint: "期間内に複数の料金状態が混在しています。",
  },
};

function pricingStatusMeta(
  status: string | undefined,
): PricingStatusMeta | null {
  if (!status) return null;
  return PRICING_STATUS_META[status] ?? { label: status };
}

const FREE_TIER_GROUP_LABELS: Record<string, string> = {
  "1m": "1M グループ",
  "10m": "10M グループ",
};

const USAGE_TIER_OPTIONS = [
  { value: "tier_1_2", label: "Tier 1-2（250K / 2.5M）" },
  { value: "tier_3_plus", label: "Tier 3 以上（1M / 10M）" },
];

/** 表示対象コスト。無料枠反映 ON なら推定請求額、OFF なら定価換算を返す。 */
function pickCost(
  row: (UsageCostMetrics & { total_cost?: number }) | null | undefined,
  applyFreeTier: boolean,
): number {
  if (!row) return 0;
  const listCost = row.list_cost ?? row.total_cost ?? 0;
  if (!applyFreeTier) return listCost;
  return row.estimated_billed_cost ?? listCost;
}

type CostTrendPoint = TokenUsageDailyPoint & { cost: number };
type TrendMetric = "cost" | "tokens";

type TokenTrendMetric = {
  total_tokens: number;
  list_cost: number;
  estimated_billed_cost: number;
  request_count: number;
};

type TokenTrendPoint = {
  date: string;
  [key: string]: unknown;
};

type TokenTrendSeries = {
  id: string;
  identity: string;
  valueKey: string;
  metricKey: string;
  label: string;
  color: string;
};

type TokenTrendChart = {
  id: string;
  title: string;
  quotaLimit?: number;
  data: TokenTrendPoint[];
  series: TokenTrendSeries[];
};

const TOKEN_TREND_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

type CostTrendTooltipProps = Partial<TooltipContentProps<number, string>> & {
  applyFreeTier: boolean;
};

function CostTrendTooltip({
  active,
  payload,
  label,
  applyFreeTier,
}: CostTrendTooltipProps) {
  const point = payload?.[0]?.payload as CostTrendPoint | undefined;
  if (!active || !point) return null;

  return (
    <div className="rounded-lg border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md">
      <p className="mb-1 font-semibold">
        {formatDate(String(label ?? point.date))}
      </p>
      <div className="space-y-0.5">
        <div className="flex items-center justify-between gap-6">
          <span className="text-muted-foreground">
            {applyFreeTier ? "推定請求額" : "定価換算"}
          </span>
          <span className="font-medium">{formatCost(point.cost)}</span>
        </div>
        <div className="flex items-center justify-between gap-6">
          <span className="text-muted-foreground">トークン</span>
          <span>{formatTokens(point.total_tokens)}</span>
        </div>
        <div className="flex items-center justify-between gap-6">
          <span className="text-muted-foreground">リクエスト</span>
          <span>{point.request_count.toLocaleString("ja-JP")}</span>
        </div>
        <div className="mt-1 border-t pt-1 text-[11px] text-muted-foreground">
          定価 {formatCost(point.list_cost ?? point.total_cost)} / 推定請求額{" "}
          {formatCost(
            point.estimated_billed_cost ?? point.list_cost ?? point.total_cost,
          )}
        </div>
      </div>
    </div>
  );
}

type TokenTrendTooltipProps = Partial<TooltipContentProps<number, string>> & {
  applyFreeTier: boolean;
  chart: TokenTrendChart;
};

function TokenTrendTooltip({
  active,
  payload,
  label,
  applyFreeTier,
  chart,
}: TokenTrendTooltipProps) {
  const point = payload?.[0]?.payload as TokenTrendPoint | undefined;
  if (!active || !point) return null;

  const rows = chart.series
    .map((series) => {
      const metric = point[series.metricKey] as TokenTrendMetric | undefined;
      return metric && metric.total_tokens > 0 ? { series, metric } : null;
    })
    .filter(
      (row): row is { series: TokenTrendSeries; metric: TokenTrendMetric } =>
        row !== null,
    );

  if (rows.length === 0) return null;

  return (
    <div className="max-w-xs rounded-lg border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md">
      <p className="mb-1 font-semibold">
        {formatDate(String(label ?? point.date))}
      </p>
      <div className="space-y-2">
        {rows.map(({ series, metric }) => (
          <div key={series.id} className="space-y-0.5">
            <div className="flex items-center gap-1.5 font-medium">
              <span
                className="size-2 shrink-0 rounded-full"
                style={{ backgroundColor: series.color }}
              />
              <span className="truncate">{series.label}</span>
            </div>
            <div className="flex items-center justify-between gap-5">
              <span className="text-muted-foreground">トークン</span>
              <span>{formatTokens(metric.total_tokens)}</span>
            </div>
            <div className="flex items-center justify-between gap-5">
              <span className="text-muted-foreground">
                {applyFreeTier ? "推定請求額" : "定価換算"}
              </span>
              <span>
                {formatCost(
                  applyFreeTier
                    ? metric.estimated_billed_cost
                    : metric.list_cost,
                )}
              </span>
            </div>
            <div className="flex items-center justify-between gap-5">
              <span className="text-muted-foreground">リクエスト</span>
              <span>{metric.request_count.toLocaleString("ja-JP")}</span>
            </div>
            <div className="border-t pt-0.5 text-[11px] text-muted-foreground">
              定価 {formatCost(metric.list_cost)} / 推定請求額{" "}
              {formatCost(metric.estimated_billed_cost)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function TokenTrendChartView({
  chart,
  applyFreeTier,
}: {
  chart: TokenTrendChart;
  applyFreeTier: boolean;
}) {
  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-muted-foreground">
          {chart.title}
        </h4>
        {typeof chart.quotaLimit === "number" && (
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="inline-block w-5 border-t border-dashed border-chart-4" />
            無料枠 {formatTokens(chart.quotaLimit)} / 日
          </span>
        )}
      </div>
      <div
        className="h-56 w-full"
        data-testid={`cost-dashboard-token-trend-chart-${chart.id}`}
      >
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={chart.data}
            margin={{ top: 8, right: 12, bottom: 4, left: 8 }}
          >
            <CartesianGrid
              stroke="var(--border)"
              strokeDasharray="3 3"
              vertical={false}
            />
            <XAxis
              dataKey="date"
              tickFormatter={formatDate}
              tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={{ stroke: "var(--border)" }}
              minTickGap={18}
              interval="preserveStartEnd"
            />
            <YAxis
              tickFormatter={(value: number) => formatTokens(value)}
              tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={false}
              width={82}
              allowDecimals={false}
            />
            <Tooltip
              content={
                <TokenTrendTooltip
                  applyFreeTier={applyFreeTier}
                  chart={chart}
                />
              }
              cursor={{
                stroke: "var(--primary)",
                strokeDasharray: "4 4",
                strokeOpacity: 0.65,
              }}
            />
            {typeof chart.quotaLimit === "number" && (
              <ReferenceLine
                y={chart.quotaLimit}
                stroke="var(--chart-4)"
                strokeDasharray="6 4"
                strokeWidth={1.5}
              />
            )}
            {chart.series.map((series) => (
              <Line
                key={series.id}
                type="monotone"
                dataKey={series.valueKey}
                name={series.label}
                stroke={series.color}
                strokeWidth={2}
                dot={{ r: 2, fill: series.color }}
                activeDot={{ r: 5, fill: series.color }}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        {chart.series.map((series) => (
          <span key={series.id} className="flex items-center gap-1">
            <span
              className="size-2 rounded-full"
              style={{ backgroundColor: series.color }}
            />
            {series.label}
          </span>
        ))}
      </div>
    </div>
  );
}

export function CostDashboardSection({ isAdmin }: { isAdmin: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [dashboard, setDashboard] = useState<TokenUsageSummary | null>(null);
  const [breakdownTab, setBreakdownTab] = useState<BreakdownTab>("model");
  const [detailPeriod, setDetailPeriod] = useState<DetailPeriod>("today");

  // 無料枠を反映するか（ON=推定請求額 / OFF=定価換算）
  const [applyFreeTier, setApplyFreeTier] = useState(true);
  const [trendMetric, setTrendMetric] = useState<TrendMetric>("cost");

  // 料金カタログ / 無料枠の状態
  const [pricing, setPricing] = useState<PricingCatalogStatus | null>(null);
  const [freeTier, setFreeTier] = useState<FreeTierResponse | null>(null);
  const [refreshingPricing, setRefreshingPricing] = useState(false);
  const [pricingMessage, setPricingMessage] = useState<string | null>(null);

  // 管理者向け設定ドラフト
  const [incentiveEnabled, setIncentiveEnabled] = useState(false);
  const [usageTier, setUsageTier] = useState("tier_1_2");
  const [savingIncentive, setSavingIncentive] = useState(false);

  // 日付範囲フィルタ（JST のまま。無料枠の日界は UTC で別表示する）
  const defaults = useMemo(() => getDateRangeForPeriod("today"), []);
  const [dateFrom, setDateFrom] = useState(defaults.start);
  const [dateTo, setDateTo] = useState(defaults.end);

  // 詳細データ
  const [modelBreakdown, setModelBreakdown] = useState<
    TokenUsageSummary["model_breakdown"]
  >([]);
  const [projectBreakdown, setProjectBreakdown] = useState<
    TokenUsageProjectRow[]
  >([]);
  const [agentBreakdown, setAgentBreakdown] = useState<TokenUsageAgentRow[]>(
    [],
  );
  const [userBreakdown, setUserBreakdown] = useState<TokenUsageUserRow[]>([]);
  const [dailyBreakdown, setDailyBreakdown] = useState<TokenUsageDailyPoint[]>(
    [],
  );
  const [detailTotal, setDetailTotal] = useState<TokenUsageTotals | null>(null);
  const [baseBreakdownLoading, setBaseBreakdownLoading] = useState(false);
  const [tabBreakdownLoading, setTabBreakdownLoading] = useState(false);
  const breakdownLoading = baseBreakdownLoading || tabBreakdownLoading;
  const baseBreakdownRequestIdRef = useRef(0);
  const tabBreakdownRequestIdRef = useRef(0);
  const dashboardRequestIdRef = useRef(0);
  const pricingMetaRequestIdRef = useRef(0);

  const applyFreeTierMeta = useCallback(
    (tier: FreeTierResponse | null | undefined) => {
      if (!tier) return;
      setFreeTier(tier);
      setIncentiveEnabled(tier.enabled ?? false);
      setUsageTier(tier.tier ?? "tier_1_2");
    },
    [],
  );

  const fetchPricingMeta = useCallback(async () => {
    const requestId = ++pricingMetaRequestIdRef.current;
    const results = await Promise.allSettled([
      usageApi.getPricingStatus(),
      usageApi.getFreeTier(),
    ] as const);
    if (requestId !== pricingMetaRequestIdRef.current) return;
    if (results[0].status === "fulfilled") {
      setPricing(results[0].value?.pricing ?? null);
    }
    const tier =
      results[1].status === "fulfilled"
        ? results[1].value
        : results[0].status === "fulfilled"
          ? results[0].value?.free_tier
          : undefined;
    applyFreeTierMeta(tier);
  }, [applyFreeTierMeta]);

  const fetchDashboard = useCallback(async () => {
    const requestId = ++dashboardRequestIdRef.current;
    setLoading(true);
    try {
      const data = await usageApi.getDashboard();
      if (requestId !== dashboardRequestIdRef.current) return;
      ++pricingMetaRequestIdRef.current;
      setDashboard(data);
      if (data.pricing) setPricing(data.pricing);
      applyFreeTierMeta(data.free_tier);

      // 旧バックエンドとの互換。現行バックエンドはダッシュボードに
      // 料金状態と無料枠を同梱するため、通常の初回表示では追加取得しない。
      if (!data.pricing || !data.free_tier) void fetchPricingMeta();
    } catch (err) {
      if (requestId !== dashboardRequestIdRef.current) return;
      console.error("コストダッシュボード取得失敗:", err);
      setDashboard(null);
    } finally {
      if (requestId === dashboardRequestIdRef.current) setLoading(false);
    }
  }, [applyFreeTierMeta, fetchPricingMeta]);

  const fetchBreakdown = useCallback(
    async ({
      includeBase = true,
      tab = "model",
    }: BreakdownFetchOptions = {}) => {
      const baseRequestId = includeBase
        ? ++baseBreakdownRequestIdRef.current
        : baseBreakdownRequestIdRef.current;
      const tabRequestId = ++tabBreakdownRequestIdRef.current;
      if (!dateFrom || !dateTo || dateFrom > dateTo) {
        if (includeBase) setBaseBreakdownLoading(false);
        setTabBreakdownLoading(false);
        return;
      }
      if (includeBase) setBaseBreakdownLoading(true);
      setTabBreakdownLoading(true);
      try {
        const requests: Array<{
          kind: BreakdownRequestKind;
          promise: Promise<unknown>;
        }> = [];
        if (includeBase) {
          requests.push(
            { kind: "daily", promise: usageApi.getDaily(dateFrom, dateTo) },
            { kind: "total", promise: usageApi.getTotal(dateFrom, dateTo) },
          );
        }
        if (tab === "model") {
          requests.push({
            kind: "model",
            promise: usageApi.getByModel(dateFrom, dateTo),
          });
        } else if (tab === "project") {
          requests.push({
            kind: "project",
            promise: usageApi.getByProject(dateFrom, dateTo),
          });
        } else if (tab === "agent") {
          requests.push({
            kind: "agent",
            promise: usageApi.getByAgent(dateFrom, dateTo),
          });
        } else if (isAdmin) {
          requests.push({
            kind: "user",
            promise: usageApi.getByUser(dateFrom, dateTo),
          });
        }

        const results = await Promise.allSettled(
          requests.map((request) => request.promise),
        );
        results.forEach((result, index) => {
          if (result.status !== "fulfilled") return;
          const kind = requests[index]?.kind;
          const baseIsCurrent =
            !includeBase || baseRequestId === baseBreakdownRequestIdRef.current;
          const tabIsCurrent = tabRequestId === tabBreakdownRequestIdRef.current;
          if (
            (kind === "daily" || kind === "total")
              ? !baseIsCurrent
              : !tabIsCurrent
          ) {
            return;
          }
          switch (kind) {
            case "daily":
              setDailyBreakdown(result.value as TokenUsageDailyPoint[]);
              break;
            case "total":
              setDetailTotal(result.value as TokenUsageTotals);
              break;
            case "model":
              setModelBreakdown(
                result.value as TokenUsageSummary["model_breakdown"],
              );
              break;
            case "project":
              setProjectBreakdown(result.value as TokenUsageProjectRow[]);
              break;
            case "agent":
              setAgentBreakdown(result.value as TokenUsageAgentRow[]);
              break;
            case "user":
              setUserBreakdown(result.value as TokenUsageUserRow[]);
              break;
          }
        });
      } catch (err) {
        console.error("内訳データ取得失敗:", err);
      } finally {
        if (
          includeBase &&
          baseRequestId === baseBreakdownRequestIdRef.current
        )
          setBaseBreakdownLoading(false);
        if (tabRequestId === tabBreakdownRequestIdRef.current)
          setTabBreakdownLoading(false);
      }
    },
    [
      baseBreakdownRequestIdRef,
      dateFrom,
      dateTo,
      isAdmin,
      tabBreakdownRequestIdRef,
    ],
  );

  const selectDetailPeriod = useCallback(
    (period: Exclude<DetailPeriod, "custom">) => {
      const range = getDateRangeForPeriod(period);
      setDetailPeriod(period);
      setDailyBreakdown([]);
      setDetailTotal(null);
      setModelBreakdown([]);
      setProjectBreakdown([]);
      setAgentBreakdown([]);
      setUserBreakdown([]);
      setDateFrom(range.start);
      setDateTo(range.end);
    },
    [],
  );

  const handleDateChange = useCallback(
    (field: "from" | "to", value: string) => {
      setDetailPeriod("custom");
      setDailyBreakdown([]);
      setDetailTotal(null);
      setModelBreakdown([]);
      setProjectBreakdown([]);
      setAgentBreakdown([]);
      setUserBreakdown([]);
      if (field === "from") setDateFrom(value);
      else setDateTo(value);
    },
    [],
  );

  const handleToggle = useCallback(() => {
    if (!expanded && !dashboard) {
      fetchDashboard();
    }
    setExpanded((v) => !v);
  }, [expanded, dashboard, fetchDashboard]);

  const handleRefresh = useCallback(() => {
    fetchDashboard();
    fetchBreakdown({ includeBase: true, tab: breakdownTab });
  }, [breakdownTab, fetchDashboard, fetchBreakdown]);

  const handleRefreshPricing = useCallback(async () => {
    setRefreshingPricing(true);
    setPricingMessage(null);
    try {
      const res = await usageApi.refreshPricing();
      setPricingMessage(
        res?.success === false
          ? "料金表の更新に失敗しました（既存の料金を維持しています）"
          : "料金表を更新しました",
      );
      await fetchPricingMeta();
    } catch (err) {
      console.error("料金表更新失敗:", err);
      setPricingMessage(
        `料金表の更新に失敗しました: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setRefreshingPricing(false);
    }
  }, [fetchPricingMeta]);

  const handleSaveIncentive = useCallback(async () => {
    setSavingIncentive(true);
    setPricingMessage(null);
    try {
      // 設定保存は言語モデル設定と同じ config 更新 API（PATCH /api/settings）を使う。
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({
          key: "openai.data_sharing_incentive_enabled",
          value: incentiveEnabled,
        }),
      });
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "openai.usage_tier", value: usageTier }),
      });
      setPricingMessage("無料枠設定を保存しました");
      await fetchPricingMeta();
    } catch (err) {
      console.error("無料枠設定の保存失敗:", err);
      setPricingMessage(
        `無料枠設定の保存に失敗しました: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setSavingIncentive(false);
    }
  }, [incentiveEnabled, usageTier, fetchPricingMeta]);

  const detailRangeKeyRef = useRef<string | null>(null);
  const detailTabRef = useRef<BreakdownTab | null>(null);

  // 初回はダッシュボード本体から本日の合計・日別行を復元し、
  // 現在選択中の内訳だけを遅延取得する。期間変更後は基礎集計と
  // 選択中タブを取得し、タブ切り替え時はそのタブだけを取得する。
  useEffect(() => {
    if (!expanded || !dashboard) return;

    const rangeKey = `${dateFrom}:${dateTo}`;
    if (detailRangeKeyRef.current === rangeKey) return;
    detailRangeKeyRef.current = rangeKey;
    detailTabRef.current = breakdownTab;

    if (detailPeriod === "today" && dateFrom === dateTo) {
      const todayRow = dashboard.daily_trend.find((row) => row.date === dateTo);
      const initialToday = todayRow ?? {
        ...dashboard.today,
        date: dateTo,
      };
      setDailyBreakdown([
        initialToday,
      ]);
      setDetailTotal(initialToday);
      fetchBreakdown({ includeBase: false, tab: breakdownTab });
      return;
    }

    fetchBreakdown({ includeBase: true, tab: breakdownTab });
  }, [
    dashboard,
    dateFrom,
    dateTo,
    detailPeriod,
    expanded,
    breakdownTab,
    fetchBreakdown,
  ]);

  useEffect(() => {
    if (!expanded || !dashboard) return;
    if (detailTabRef.current === null) {
      detailTabRef.current = breakdownTab;
      return;
    }
    if (detailTabRef.current === breakdownTab) return;
    detailTabRef.current = breakdownTab;
    fetchBreakdown({ includeBase: false, tab: breakdownTab });
  }, [breakdownTab, dashboard, expanded, fetchBreakdown]);

  const dailyTrendData = useMemo<CostTrendPoint[]>(
    () =>
      (dashboard?.daily_trend ?? []).map((day) => ({
        ...day,
        cost: pickCost(day, applyFreeTier),
      })),
    [dashboard, applyFreeTier],
  );

  const tokenTrendCharts = useMemo<TokenTrendChart[]>(() => {
    const rows = dashboard?.daily_model_trend ?? [];
    if (rows.length === 0 || dailyTrendData.length === 0) return [];

    type SeriesAccumulator = {
      identity: string;
      groupKey: string;
      label: string;
      byDate: Map<string, TokenTrendMetric>;
    };

    const seriesByIdentity = new Map<string, SeriesAccumulator>();
    for (const row of rows) {
      const totalTokens = Number(row.total_tokens ?? 0);
      if (!Number.isFinite(totalTokens) || totalTokens <= 0) continue;

      const modelLabel =
        row.resolved_model && row.resolved_model !== row.model
          ? `${row.model} (${row.resolved_model})`
          : row.model;
      const label = row.provider
        ? `${row.provider} / ${modelLabel}`
        : modelLabel;
      const baseIdentity = `${row.provider}:${row.model}:${row.resolved_model ?? ""}`;
      const groupKey = row.free_incentive_group?.trim() || `model:${baseIdentity}`;
      const identity = `${baseIdentity}:${groupKey}`;
      const existing = seriesByIdentity.get(identity);
      const series =
        existing ??
        (() => {
          const created: SeriesAccumulator = {
            identity,
            groupKey,
            label,
            byDate: new Map(),
          };
          seriesByIdentity.set(identity, created);
          return created;
        })();
      const dateMetric = series.byDate.get(row.date);
      const metric: TokenTrendMetric = {
        total_tokens: totalTokens,
        list_cost: Number(row.list_cost ?? row.total_cost ?? 0) || 0,
        estimated_billed_cost:
          Number(row.estimated_billed_cost ?? row.list_cost ?? row.total_cost ?? 0) ||
          0,
        request_count: Number(row.request_count ?? 0) || 0,
      };
      if (dateMetric) {
        dateMetric.total_tokens += metric.total_tokens;
        dateMetric.list_cost += metric.list_cost;
        dateMetric.estimated_billed_cost += metric.estimated_billed_cost;
        dateMetric.request_count += metric.request_count;
      } else {
        series.byDate.set(row.date, metric);
      }
    }

    const groupedSeries = new Map<string, SeriesAccumulator[]>();
    for (const series of seriesByIdentity.values()) {
      const group = groupedSeries.get(series.groupKey) ?? [];
      group.push(series);
      groupedSeries.set(series.groupKey, group);
    }

    return [...groupedSeries.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([groupKey, group], chartIndex) => {
        const orderedGroup = [...group].sort((a, b) =>
          a.label.localeCompare(b.label),
        );
        const series = orderedGroup.map((item, seriesIndex) => ({
          id: `token-trend-${chartIndex}-${seriesIndex}`,
          identity: item.identity,
          valueKey: `tokens-${chartIndex}-${seriesIndex}`,
          metricKey: `metrics-${chartIndex}-${seriesIndex}`,
          label: item.label,
          color: TOKEN_TREND_COLORS[seriesIndex % TOKEN_TREND_COLORS.length],
        }));
        const data = dailyTrendData.map(({ date }) => {
          const point: TokenTrendPoint = { date };
          for (const chartSeries of series) {
            const source = seriesByIdentity.get(chartSeries.identity);
            const metric = source?.byDate.get(date);
            point[chartSeries.valueKey] = metric?.total_tokens ?? 0;
            point[chartSeries.metricKey] =
              metric ?? {
                total_tokens: 0,
                list_cost: 0,
                estimated_billed_cost: 0,
                request_count: 0,
              } satisfies TokenTrendMetric;
          }
          return point;
        });
        const quotaLimit = freeTier?.enabled
          ? freeTier.groups?.find((item) => item.group === groupKey)
              ?.limit_tokens
          : undefined;
        const title = FREE_TIER_GROUP_LABELS[groupKey]
          ? `${FREE_TIER_GROUP_LABELS[groupKey]}のモデル別トークン推移`
          : groupKey.startsWith("model:")
            ? `${orderedGroup[0]?.label ?? "モデル"}のトークン推移`
            : `${groupKey}のモデル別トークン推移`;
        return {
          id: `group-${chartIndex}`,
          title,
          quotaLimit,
          data,
          series,
        };
      });
  }, [dashboard, dailyTrendData, freeTier]);

  // モデル別内訳をコスト降順にソート
  const sortedModelBreakdown = useMemo(
    () =>
      [...modelBreakdown].sort(
        (a, b) => pickCost(b, applyFreeTier) - pickCost(a, applyFreeTier),
      ),
    [modelBreakdown, applyFreeTier],
  );

  const sortedProjectBreakdown = useMemo(
    () =>
      [...projectBreakdown].sort(
        (a, b) => pickCost(b, applyFreeTier) - pickCost(a, applyFreeTier),
      ),
    [projectBreakdown, applyFreeTier],
  );

  const sortedAgentBreakdown = useMemo(
    () =>
      [...agentBreakdown].sort(
        (a, b) => pickCost(b, applyFreeTier) - pickCost(a, applyFreeTier),
      ),
    [agentBreakdown, applyFreeTier],
  );
  const sortedUserBreakdown = useMemo(
    () => [...userBreakdown].sort((a, b) => b.total_tokens - a.total_tokens),
    [userBreakdown],
  );
  const sortedDailyBreakdown = useMemo(
    () => [...dailyBreakdown].sort((a, b) => a.date.localeCompare(b.date)),
    [dailyBreakdown],
  );

  const today = dashboard?.today;
  const detailTotals = detailTotal ?? (detailPeriod === "today" ? today : null);
  const detailPeriodLabel =
    detailPeriod === "today"
      ? "本日の詳細"
      : detailPeriod === "week"
        ? "1週間の詳細"
        : detailPeriod === "month"
          ? "1か月の詳細"
          : "指定期間の詳細";
  const detailRangeLabel = formatDateRangeLabel(dateFrom, dateTo);
  const pricingLastUpdated = useMemo(() => {
    const stamps = (pricing?.sources ?? [])
      .map((source) => source.last_success_at)
      .filter((value): value is string => Boolean(value))
      .sort();
    return stamps.length > 0 ? stamps[stamps.length - 1] : null;
  }, [pricing]);

  return (
    <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
      <CardHeader className="cursor-pointer select-none" onClick={handleToggle}>
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <DollarSign className="size-4" />
            トークン使用量 / コストダッシュボード
            <Badge variant="outline" className="text-[10px]">
              {isAdmin ? "全体" : "自分"}
            </Badge>
          </span>
          {expanded ? (
            <ChevronUp className="size-4" />
          ) : (
            <ChevronDown className="size-4" />
          )}
        </CardTitle>
      </CardHeader>

      {expanded && (
        <CardContent className="space-y-4">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              取得中...
            </div>
          ) : !dashboard ? (
            <p className="text-sm text-muted-foreground">
              データを取得できませんでした
            </p>
          ) : (
            <>
              {/* 無料枠反映トグル */}
              <div className="flex w-fit items-center gap-2 text-xs">
                <label className="flex items-center gap-2">
                  <Checkbox
                    checked={applyFreeTier}
                    onCheckedChange={(checked) =>
                      setApplyFreeTier(checked === true)
                    }
                  />
                  <span>無料枠を反映</span>
                </label>
                <span className="text-muted-foreground">
                  {applyFreeTier
                    ? "（推定請求額を表示中）"
                    : "（定価換算を表示中）"}
                </span>
              </div>

              {/* サマリーカード */}
              <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
                <button
                  type="button"
                  className={`rounded-lg border p-3 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    detailPeriod === "today"
                      ? "border-primary bg-primary/5 ring-1 ring-primary/20"
                      : ""
                  }`}
                  aria-pressed={detailPeriod === "today"}
                  aria-label="本日の詳細を表示"
                  onClick={() => selectDetailPeriod("today")}
                >
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                    <DollarSign className="size-3" />
                    本日のコスト
                  </div>
                  <p
                    className="text-xl font-bold"
                    data-testid="cost-dashboard-today-cost"
                  >
                    {formatCost(pickCost(today, applyFreeTier))}
                  </p>
                  <p className="mt-1 text-[10px] text-primary">
                    {detailPeriod === "today" ? "表示中" : "今日の詳細を見る"}
                  </p>
                </button>
                <button
                  type="button"
                  className={`rounded-lg border p-3 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    detailPeriod === "month"
                      ? "border-primary bg-primary/5 ring-1 ring-primary/20"
                      : ""
                  }`}
                  aria-pressed={detailPeriod === "month"}
                  aria-label="今月の詳細を表示"
                  onClick={() => selectDetailPeriod("month")}
                >
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                    <Calendar className="size-3" />
                    今月のコスト
                  </div>
                  <p
                    className="text-xl font-bold"
                    data-testid="cost-dashboard-monthly-cost"
                  >
                    {formatCost(
                      dashboard.monthly_total
                        ? pickCost(dashboard.monthly_total, applyFreeTier)
                        : undefined,
                    )}
                  </p>
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    JSTの当月合計
                  </p>
                  <p className="mt-1 text-[10px] text-primary">
                    {detailPeriod === "month" ? "表示中" : "今月の詳細を見る"}
                  </p>
                </button>
                <div className="rounded-lg border p-3">
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                    <Hash className="size-3" />
                    本日のトークン数
                  </div>
                  <p className="text-xl font-bold">
                    {formatTokens(dashboard.today.total_tokens)}
                  </p>
                </div>
                <div className="rounded-lg border p-3">
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                    <Activity className="size-3" />
                    本日のリクエスト数
                  </div>
                  <p className="text-xl font-bold">
                    {dashboard.today.request_count.toLocaleString("ja-JP")}
                  </p>
                </div>
              </div>

              {/* 定価 / 推定請求額 / 削減額 */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div className="rounded-lg border p-3">
                  <div className="text-xs text-muted-foreground mb-1">
                    定価換算 (list_cost)
                  </div>
                  <p
                    className="text-sm font-semibold"
                    data-testid="cost-dashboard-list-cost"
                  >
                    {formatCost(today?.list_cost ?? today?.total_cost)}
                  </p>
                </div>
                <div className="rounded-lg border p-3">
                  <div className="text-xs text-muted-foreground mb-1">
                    推定請求額 (estimated_billed_cost)
                  </div>
                  <p
                    className="text-sm font-semibold"
                    data-testid="cost-dashboard-billed-cost"
                  >
                    {formatCost(
                      today?.estimated_billed_cost ??
                        today?.list_cost ??
                        today?.total_cost,
                    )}
                  </p>
                </div>
                <div className="rounded-lg border p-3">
                  <div className="text-xs text-muted-foreground mb-1">
                    削減額 (savings)
                  </div>
                  <p
                    className="text-sm font-semibold"
                    data-testid="cost-dashboard-savings"
                  >
                    {formatCost(today?.savings ?? 0)}
                  </p>
                </div>
              </div>

              {/* 料金カバレッジ */}
              <div className="rounded-lg border p-3 space-y-1 text-xs">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <span className="text-muted-foreground">
                    料金未登録リクエスト数:{" "}
                    <span className="font-medium text-foreground">
                      {(today?.unpriced_request_count ?? 0).toLocaleString(
                        "ja-JP",
                      )}
                    </span>
                  </span>
                  <span className="text-muted-foreground">
                    料金カバー率:{" "}
                    <span className="font-medium text-foreground">
                      {formatPercent(today?.pricing_coverage_percent)}
                    </span>
                  </span>
                </div>
                {today?.is_partial && (
                  <div className="flex items-center gap-1.5 text-amber-600 dark:text-amber-500">
                    <AlertTriangle className="size-3.5" />
                    部分集計です。料金未登録のリクエストが含まれるため、実際の請求額より小さく出ます。
                  </div>
                )}
              </div>

              {/* 無料枠パネル */}
              <div className="rounded-lg border p-3 space-y-2">
                <div className="flex flex-wrap items-center gap-2 text-xs font-semibold">
                  <Gift className="size-3.5" />
                  OpenAI データ共有インセンティブ無料枠
                  <Badge variant="outline" className="text-[10px]">
                    {freeTier?.enabled ? "有効" : "無効"}
                  </Badge>
                  <span className="font-normal text-muted-foreground">
                    UTC 日付: {freeTier?.utc_date ?? "—"}
                  </span>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  OpenAI 無料枠の日界は UTC です（JST
                  の日付範囲フィルタとは境界が異なります）。
                </p>
                {freeTier?.groups && freeTier.groups.length > 0 ? (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {freeTier.groups.map((group) => (
                      <div
                        key={group.group}
                        className="rounded border p-2 text-xs space-y-0.5"
                      >
                        <div className="font-medium">
                          {FREE_TIER_GROUP_LABELS[group.group] ?? group.group}
                        </div>
                        <div className="text-muted-foreground">
                          本日使用量: {formatTokens(group.used_tokens ?? 0)}
                        </div>
                        <div className="text-muted-foreground">
                          上限: {formatTokens(group.limit_tokens ?? 0)}
                        </div>
                        <div className="text-muted-foreground">
                          残量: {formatTokens(group.remaining_tokens ?? 0)}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    無料枠の使用状況を取得できませんでした
                  </p>
                )}
              </div>

              {/* 料金カタログ状態 */}
              <div className="rounded-lg border p-3 space-y-2 text-xs">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <span className="text-muted-foreground">
                    料金カタログ版:{" "}
                    <span className="font-medium text-foreground">
                      {pricing?.catalog_version ?? "未取得"}
                    </span>
                  </span>
                  <span className="text-muted-foreground">
                    料金表最終更新日時:{" "}
                    <span className="font-medium text-foreground">
                      {formatTimestamp(pricingLastUpdated)}
                    </span>
                  </span>
                  {typeof pricing?.rule_count === "number" && (
                    <span className="text-muted-foreground">
                      ルール数:{" "}
                      <span className="font-medium text-foreground">
                        {pricing.rule_count.toLocaleString("ja-JP")}
                      </span>
                    </span>
                  )}
                </div>

                {isAdmin && (
                  <div className="space-y-2 border-t pt-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={refreshingPricing}
                        onClick={handleRefreshPricing}
                      >
                        {refreshingPricing ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <RefreshCw className="size-3" />
                        )}
                        料金表を更新
                      </Button>
                      <label className="flex items-center gap-2">
                        <Checkbox
                          checked={incentiveEnabled}
                          onCheckedChange={(checked) =>
                            setIncentiveEnabled(checked === true)
                          }
                        />
                        <span>データ共有インセンティブ</span>
                      </label>
                      <label className="flex items-center gap-2">
                        <span className="text-muted-foreground">
                          Usage Tier
                        </span>
                        <AppSelect
                          aria-label="Usage Tier"
                          value={usageTier}
                          onChange={(event) => setUsageTier(event.target.value)}
                          className="h-8 w-56 rounded-lg border border-input bg-transparent px-2.5 text-xs"
                        >
                          {USAGE_TIER_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </AppSelect>
                      </label>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={savingIncentive}
                        onClick={handleSaveIncentive}
                      >
                        {savingIncentive && (
                          <Loader2 className="size-3 animate-spin" />
                        )}
                        無料枠設定を保存
                      </Button>
                    </div>
                  </div>
                )}

                {pricingMessage && (
                  <p
                    className="text-xs text-muted-foreground"
                    data-testid="cost-dashboard-pricing-message"
                  >
                    {pricingMessage}
                  </p>
                )}
              </div>

              {/* 30日間の価格 / トークン推移 */}
              {dailyTrendData.length > 0 && (
                <div className="space-y-2" data-testid="cost-dashboard-trend">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <h3 className="text-xs font-semibold text-muted-foreground">
                        {trendMetric === "cost"
                          ? "30日間コスト推移"
                          : "30日間トークン推移"}
                      </h3>
                      {trendMetric === "tokens" && (
                        <p className="text-[11px] text-muted-foreground">
                          無料枠グループごとにモデル別表示
                        </p>
                      )}
                    </div>
                    <Tabs
                      value={trendMetric}
                      onValueChange={(value) =>
                        setTrendMetric(value as TrendMetric)
                      }
                    >
                      <TabsList className="h-8">
                        <TabsTrigger value="cost" className="h-7 px-2 text-xs">
                          価格
                        </TabsTrigger>
                        <TabsTrigger
                          value="tokens"
                          className="h-7 px-2 text-xs"
                        >
                          トークン
                        </TabsTrigger>
                      </TabsList>
                    </Tabs>
                  </div>

                  {trendMetric === "cost" ? (
                    <div
                      className="h-64 w-full"
                      data-testid="cost-dashboard-trend-chart"
                    >
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart
                          data={dailyTrendData}
                          margin={{ top: 8, right: 12, bottom: 4, left: 8 }}
                        >
                          <CartesianGrid
                            stroke="var(--border)"
                            strokeDasharray="3 3"
                            vertical={false}
                          />
                          <XAxis
                            dataKey="date"
                            tickFormatter={formatDate}
                            tick={{
                              fontSize: 10,
                              fill: "var(--muted-foreground)",
                            }}
                            tickLine={false}
                            axisLine={{ stroke: "var(--border)" }}
                            minTickGap={18}
                            interval="preserveStartEnd"
                          />
                          <YAxis
                            tickFormatter={(value: number) => formatCost(value)}
                            tick={{
                              fontSize: 10,
                              fill: "var(--muted-foreground)",
                            }}
                            tickLine={false}
                            axisLine={false}
                            width={70}
                            allowDecimals
                          />
                          <Tooltip
                            content={
                              <CostTrendTooltip
                                applyFreeTier={applyFreeTier}
                              />
                            }
                            cursor={{
                              stroke: "var(--primary)",
                              strokeDasharray: "4 4",
                              strokeOpacity: 0.65,
                            }}
                          />
                          <Line
                            type="monotone"
                            dataKey="cost"
                            name="コスト"
                            stroke="var(--primary)"
                            strokeWidth={2}
                            dot={{ r: 2, fill: "var(--primary)" }}
                            activeDot={{ r: 5, fill: "var(--primary)" }}
                            isAnimationActive={false}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  ) : tokenTrendCharts.length > 0 ? (
                    <div className="space-y-3">
                      {tokenTrendCharts.map((chart) => (
                        <TokenTrendChartView
                          key={chart.id}
                          chart={chart}
                          applyFreeTier={applyFreeTier}
                        />
                      ))}
                    </div>
                  ) : (
                    <p className="rounded-lg border p-4 text-xs text-muted-foreground">
                      モデル別の日次トークンデータがありません
                    </p>
                  )}
                </div>
              )}

              {/* 詳細期間のクイック選択 */}
              <div
                className="space-y-3 rounded-lg border bg-muted/20 p-3"
                data-testid="cost-dashboard-period-selector"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <h3 className="text-xs font-semibold">詳細の集計期間</h3>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      下の使用量と内訳を、見たい期間にすぐ切り替えられます。
                    </p>
                  </div>
                  <Badge variant="secondary" className="text-[10px]">
                    {detailPeriodLabel}
                  </Badge>
                </div>

                <div
                  className="flex flex-wrap gap-1"
                  role="group"
                  aria-label="詳細の集計期間"
                >
                  {DETAIL_PERIOD_OPTIONS.map((option) => (
                    <Button
                      key={option.value}
                      type="button"
                      variant={
                        detailPeriod === option.value ? "default" : "outline"
                      }
                      size="sm"
                      className="h-8 px-3 py-1 text-xs"
                      aria-pressed={detailPeriod === option.value}
                      onClick={() => selectDetailPeriod(option.value)}
                    >
                      {option.label}
                    </Button>
                  ))}
                  <Button
                    type="button"
                    variant={detailPeriod === "custom" ? "default" : "outline"}
                    size="sm"
                    className="h-8 px-3 py-1 text-xs"
                    aria-pressed={detailPeriod === "custom"}
                    onClick={() => setDetailPeriod("custom")}
                  >
                    指定
                  </Button>
                </div>

                <div className="flex flex-wrap items-end gap-2 border-t pt-3">
                  <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
                    開始日
                    <Input
                      type="date"
                      value={dateFrom}
                      onChange={(e) => handleDateChange("from", e.target.value)}
                      aria-label="詳細期間の開始日"
                      className="h-8 w-36 text-xs"
                    />
                  </label>
                  <span className="pb-1 text-xs text-muted-foreground">〜</span>
                  <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
                    終了日
                    <Input
                      type="date"
                      value={dateTo}
                      onChange={(e) => handleDateChange("to", e.target.value)}
                      aria-label="詳細期間の終了日"
                      className="h-8 w-36 text-xs"
                    />
                  </label>
                  <span className="pb-1 text-[11px] text-muted-foreground">
                    {detailRangeLabel}（JST）
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="mb-0.5"
                    aria-label="使用量を更新"
                    onClick={handleRefresh}
                  >
                    <RefreshCw className="size-3" />
                    更新
                  </Button>
                </div>
                {dateFrom > dateTo && (
                  <p className="text-xs text-destructive">
                    開始日は終了日以前に指定してください。
                  </p>
                )}
              </div>

              {/* 選択期間の合計 */}
              <div
                className="space-y-3 rounded-lg border p-3"
                data-testid="cost-dashboard-detail-summary"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div>
                    <h3 className="text-xs font-semibold">
                      {detailPeriodLabel}
                    </h3>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      {detailRangeLabel}（JST）
                    </p>
                  </div>
                  {breakdownLoading && (
                    <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      <Loader2 className="size-3 animate-spin" />
                      更新中...
                    </span>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <div className="rounded-md bg-muted/40 p-2">
                    <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      <Activity className="size-3" />
                      リクエスト数
                    </div>
                    <p
                      className="mt-1 text-lg font-bold"
                      data-testid="cost-dashboard-detail-request-count"
                    >
                      {detailTotals
                        ? detailTotals.request_count.toLocaleString("ja-JP")
                        : EMPTY_COST}
                    </p>
                  </div>
                  <div className="rounded-md bg-muted/40 p-2">
                    <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      <Hash className="size-3" />
                      トークン量
                    </div>
                    <p
                      className="mt-1 text-lg font-bold"
                      data-testid="cost-dashboard-detail-token-count"
                    >
                      {detailTotals
                        ? formatTokens(detailTotals.total_tokens)
                        : EMPTY_COST}
                    </p>
                  </div>
                  <div className="rounded-md bg-muted/40 p-2">
                    <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      <DollarSign className="size-3" />
                      コスト
                    </div>
                    <p
                      className="mt-1 text-lg font-bold"
                      data-testid="cost-dashboard-detail-cost"
                    >
                      {detailTotals
                        ? formatCost(pickCost(detailTotals, applyFreeTier))
                        : EMPTY_COST}
                    </p>
                  </div>
                  <div className="rounded-md bg-muted/40 p-2">
                    <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      <AlertTriangle className="size-3" />
                      料金未登録
                    </div>
                    <p className="mt-1 text-lg font-bold">
                      {detailTotals
                        ? (detailTotals.unpriced_request_count?.toLocaleString(
                            "ja-JP",
                          ) ?? "0")
                        : EMPTY_COST}
                    </p>
                  </div>
                </div>
              </div>

              {/* 日別の使用量 */}
              <div
                className="overflow-hidden rounded-lg border"
                data-testid="cost-dashboard-daily-breakdown"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2 border-b px-3 py-2.5">
                  <div>
                    <h3 className="text-xs font-semibold">日別の使用量</h3>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      {detailPeriod === "today"
                        ? "本日のトークン量とリクエスト数"
                        : `${detailRangeLabel}の日ごとの内訳`}
                    </p>
                  </div>
                  <span className="text-[11px] text-muted-foreground">JST</span>
                </div>
                <div className="max-h-64 overflow-auto">
                  <table className="w-full min-w-[420px] text-xs">
                    <thead>
                      <tr className="border-b text-left text-muted-foreground">
                        <th className="px-3 py-2 font-medium">日付</th>
                        <th className="px-3 py-2 text-right font-medium">
                          トークン量
                        </th>
                        <th className="px-3 py-2 text-right font-medium">
                          リクエスト数
                        </th>
                        <th className="px-3 py-2 text-right font-medium">
                          コスト
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {breakdownLoading && sortedDailyBreakdown.length === 0 ? (
                        <tr>
                          <td
                            colSpan={4}
                            className="py-5 text-center text-muted-foreground"
                          >
                            取得中...
                          </td>
                        </tr>
                      ) : sortedDailyBreakdown.length > 0 ? (
                        sortedDailyBreakdown.map((day) => (
                          <tr
                            key={day.date}
                            className="border-b border-border/50 last:border-b-0"
                            data-testid={`cost-dashboard-daily-row-${day.date}`}
                          >
                            <td className="px-3 py-2 font-medium">
                              {day.date}
                            </td>
                            <td className="px-3 py-2 text-right">
                              {formatTokens(day.total_tokens)}
                            </td>
                            <td className="px-3 py-2 text-right">
                              {day.request_count.toLocaleString("ja-JP")}
                            </td>
                            <td className="px-3 py-2 text-right font-medium">
                              {formatCost(pickCost(day, applyFreeTier))}
                            </td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td
                            colSpan={4}
                            className="py-5 text-center text-muted-foreground"
                          >
                            データがありません
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* 内訳タブ */}
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <h3 className="text-xs font-semibold">期間内訳</h3>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">
                    {detailRangeLabel}（JST）のモデル・プロジェクト別集計
                  </p>
                </div>
              </div>
              <Tabs
                value={breakdownTab}
                onValueChange={(v) => setBreakdownTab(v as BreakdownTab)}
              >
                <TabsList className="max-w-full overflow-x-auto">
                  <TabsTrigger value="model">モデル別</TabsTrigger>
                  <TabsTrigger value="project">プロジェクト別</TabsTrigger>
                  <TabsTrigger value="agent">エージェント別</TabsTrigger>
                  {isAdmin && (
                    <TabsTrigger value="user">ユーザー別</TabsTrigger>
                  )}
                </TabsList>
              </Tabs>

              {/* 内訳テーブル */}
              <div className="max-h-80 overflow-auto">
                <table className="w-full min-w-[560px] text-xs">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      {breakdownTab === "model" && (
                        <>
                          <th className="py-1.5 pr-2 font-medium">
                            プロバイダ
                          </th>
                          <th className="py-1.5 pr-2 font-medium">モデル</th>
                          <th className="py-1.5 pr-2 font-medium">料金区分</th>
                        </>
                      )}
                      {breakdownTab === "project" && (
                        <th className="py-1.5 pr-2 font-medium">
                          プロジェクト
                        </th>
                      )}
                      {breakdownTab === "agent" && (
                        <th className="py-1.5 pr-2 font-medium">
                          エージェント
                        </th>
                      )}
                      {breakdownTab === "user" && (
                        <th className="py-1.5 pr-2 font-medium">ユーザー</th>
                      )}
                      <th className="py-1.5 pr-2 font-medium text-right">
                        トークン数
                      </th>
                      <th className="py-1.5 pr-2 font-medium text-right">
                        コスト
                      </th>
                      <th className="py-1.5 font-medium text-right">
                        リクエスト数
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {breakdownTab === "model" &&
                      (sortedModelBreakdown.length > 0 ? (
                        sortedModelBreakdown.map((row, i) => {
                          const meta = pricingStatusMeta(row.pricing_status);
                          return (
                            <tr key={i} className="border-b border-border/50">
                              <td className="py-1.5 pr-2">
                                <Badge
                                  variant="outline"
                                  className="text-[10px]"
                                >
                                  {row.provider}
                                </Badge>
                              </td>
                              <td className="py-1.5 pr-2 font-mono">
                                {row.model}
                              </td>
                              <td className="py-1.5 pr-2">
                                {meta ? (
                                  <Badge
                                    variant="secondary"
                                    className="text-[10px]"
                                    title={meta.hint}
                                  >
                                    {meta.label}
                                  </Badge>
                                ) : (
                                  <span className="text-muted-foreground">
                                    {EMPTY_COST}
                                  </span>
                                )}
                              </td>
                              <td className="py-1.5 pr-2 text-right">
                                {formatTokens(row.total_tokens)}
                              </td>
                              <td className="py-1.5 pr-2 text-right font-medium">
                                {formatCost(
                                  pickCost(row, applyFreeTier),
                                  row.pricing_status,
                                )}
                              </td>
                              <td className="py-1.5 text-right">
                                {row.request_count.toLocaleString("ja-JP")}
                              </td>
                            </tr>
                          );
                        })
                      ) : (
                        <tr>
                          <td
                            colSpan={6}
                            className="py-4 text-center text-muted-foreground"
                          >
                            データがありません
                          </td>
                        </tr>
                      ))}

                    {breakdownTab === "project" &&
                      (sortedProjectBreakdown.length > 0 ? (
                        sortedProjectBreakdown.map((row, i) => (
                          <tr key={i} className="border-b border-border/50">
                            <td className="py-1.5 pr-2">
                              {row.project_name ||
                                (row.project_id === "none"
                                  ? "未設定"
                                  : row.project_id || "未設定")}
                            </td>
                            <td className="py-1.5 pr-2 text-right">
                              {formatTokens(row.total_tokens)}
                            </td>
                            <td className="py-1.5 pr-2 text-right font-medium">
                              {formatCost(pickCost(row, applyFreeTier))}
                            </td>
                            <td className="py-1.5 text-right">
                              {row.request_count.toLocaleString("ja-JP")}
                            </td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td
                            colSpan={4}
                            className="py-4 text-center text-muted-foreground"
                          >
                            データがありません
                          </td>
                        </tr>
                      ))}

                    {breakdownTab === "agent" &&
                      (sortedAgentBreakdown.length > 0 ? (
                        sortedAgentBreakdown.map((row, i) => (
                          <tr key={i} className="border-b border-border/50">
                            <td className="py-1.5 pr-2">
                              {row.agent_name || row.agent_id || "不明"}
                            </td>
                            <td className="py-1.5 pr-2 text-right">
                              {formatTokens(row.total_tokens)}
                            </td>
                            <td className="py-1.5 pr-2 text-right font-medium">
                              {formatCost(pickCost(row, applyFreeTier))}
                            </td>
                            <td className="py-1.5 text-right">
                              {row.request_count.toLocaleString("ja-JP")}
                            </td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td
                            colSpan={4}
                            className="py-4 text-center text-muted-foreground"
                          >
                            データがありません
                          </td>
                        </tr>
                      ))}
                    {breakdownTab === "user" &&
                      (sortedUserBreakdown.length > 0 ? (
                        sortedUserBreakdown.map((row) => (
                          <tr
                            key={row.user_id}
                            className="border-b border-border/50"
                          >
                            <td className="py-1.5 pr-2">
                              <div>{row.user_name || row.user_id}</div>
                              {row.user_name &&
                                row.user_name !== row.user_id && (
                                  <div className="font-mono text-[10px] text-muted-foreground">
                                    {row.user_id}
                                  </div>
                                )}
                            </td>
                            <td className="py-1.5 pr-2 text-right">
                              {formatTokens(row.total_tokens)}
                            </td>
                            <td className="py-1.5 pr-2 text-right font-medium">
                              {formatCost(pickCost(row, applyFreeTier))}
                            </td>
                            <td className="py-1.5 text-right">
                              {row.request_count.toLocaleString("ja-JP")}
                            </td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td
                            colSpan={4}
                            className="py-4 text-center text-muted-foreground"
                          >
                            データがありません
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}
