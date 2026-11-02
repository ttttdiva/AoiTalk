"use client";

import { useId, useMemo, useState, useSyncExternalStore } from "react";
import { AppSelect } from "@/components/ui/app-select";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";
import type { TokenUsageModelDailyPoint } from "@/lib/ecc-api";

export type TokenTrendMetric = {
  total_tokens: number;
  list_cost: number;
  estimated_billed_cost: number;
  request_count: number;
};

export type TokenTrendPoint = {
  date: string;
  [key: string]: string | number | TokenTrendMetric | null;
};

export type TokenTrendSeries = {
  id: string;
  identity: string;
  valueKey: string;
  metricKey: string;
  label: string;
  color: string;
  dashArray: string;
};

export type TokenTrendChart = {
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

const TOKEN_TREND_DASH_ARRAYS = [
  "",
  "7 3",
  "2 3",
  "9 3 2 3",
  "5 2 1 2",
  "12 3",
  "3 2 3 5",
  "8 2 2 2 2 2",
  "1 3",
  "10 2 2 2",
  "4 2 1 2 1 2",
  "14 4 2 4",
];

function subscribeToViewportResize(onChange: () => void) {
  if (typeof window === "undefined") return () => {};
  window.addEventListener("resize", onChange);
  return () => window.removeEventListener("resize", onChange);
}

function isNarrowViewport(): boolean {
  return typeof window !== "undefined" && window.innerWidth <= 480;
}

function getServerViewportSnapshot(): boolean {
  return false;
}

type SeriesAccumulator = {
  identity: string;
  label: string;
  byDate: Map<string, TokenTrendMetric>;
};

function finiteNumber(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

function normalizeModelIdentity(row: TokenUsageModelDailyPoint) {
  const provider = row.provider?.trim() ?? "";
  const model = row.model?.trim() || "不明なモデル";
  const resolvedModel = row.resolved_model?.trim() || null;
  const distinctResolvedModel =
    resolvedModel && resolvedModel !== model ? resolvedModel : null;
  const identity = JSON.stringify([
    provider || null,
    model,
    distinctResolvedModel,
  ]);
  const label = [
    provider,
    distinctResolvedModel ? `${model} → ${distinctResolvedModel}` : model,
  ]
    .filter(Boolean)
    .join(" / ");

  return { identity, label };
}

/**
 * 一日・一モデルに入力行を集計する。無料枠グループは集計キーに含めず、
 * 日付が欠けたモデルは null（線の切れ目）、実測 0 は 0 として保持する。
 */
export function buildTokenTrendChart(
  rows: TokenUsageModelDailyPoint[],
  aggregateDates: string[] = [],
): TokenTrendChart {
  const seriesByIdentity = new Map<string, SeriesAccumulator>();
  const dates = new Set(aggregateDates.filter(Boolean));

  for (const row of rows) {
    if (!row.date) continue;
    dates.add(row.date);

    const { identity, label } = normalizeModelIdentity(row);
    const series =
      seriesByIdentity.get(identity) ??
      (() => {
        const created = { identity, label, byDate: new Map() };
        seriesByIdentity.set(identity, created);
        return created;
      })();

    const metric: TokenTrendMetric = {
      total_tokens: finiteNumber(row.total_tokens),
      list_cost: finiteNumber(row.list_cost ?? row.total_cost),
      estimated_billed_cost: finiteNumber(
        row.estimated_billed_cost ?? row.list_cost ?? row.total_cost,
      ),
      request_count: finiteNumber(row.request_count),
    };
    const existing = series.byDate.get(row.date);
    if (existing) {
      existing.total_tokens += metric.total_tokens;
      existing.list_cost += metric.list_cost;
      existing.estimated_billed_cost += metric.estimated_billed_cost;
      existing.request_count += metric.request_count;
    } else {
      series.byDate.set(row.date, metric);
    }
  }

  const orderedModels = [...seriesByIdentity.values()].sort(
    (a, b) => a.label.localeCompare(b.label) || a.identity.localeCompare(b.identity),
  );
  const duplicateLabels = new Set(
    orderedModels
      .map((series) => series.label)
      .filter((label, index, labels) => labels.indexOf(label) !== index),
  );
  const series: TokenTrendSeries[] = orderedModels.map((item, index) => ({
    id: `token-trend-${index}`,
    identity: item.identity,
    valueKey: `tokens-${index}`,
    metricKey: `metrics-${index}`,
    label: duplicateLabels.has(item.label)
      ? `${item.label} · ${item.identity}`
      : item.label,
    color: TOKEN_TREND_COLORS[index % TOKEN_TREND_COLORS.length],
    dashArray:
      TOKEN_TREND_DASH_ARRAYS[index % TOKEN_TREND_DASH_ARRAYS.length],
  }));

  const data = [...dates]
    .sort((a, b) => a.localeCompare(b))
    .map((date) => {
      const point: TokenTrendPoint = { date };
      for (const chartSeries of series) {
        const metric = seriesByIdentity
          .get(chartSeries.identity)
          ?.byDate.get(date);
        point[chartSeries.valueKey] = metric?.total_tokens ?? null;
        point[chartSeries.metricKey] = metric ?? null;
      }
      return point;
    });

  return { data, series };
}

type TokenTrendTooltipProps = Partial<TooltipContentProps<number, string>> & {
  applyFreeTier: boolean;
  chart: TokenTrendChart;
  formatCostValue: (cost: number) => string;
};

function formatTokens(tokens: number): string {
  return tokens.toLocaleString("ja-JP");
}

function formatDate(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return year && month && day ? `${month}/${day}` : date;
}

function TokenTrendTooltip({
  active,
  payload,
  label,
  applyFreeTier,
  chart,
  formatCostValue,
}: TokenTrendTooltipProps) {
  const point = payload?.[0]?.payload as TokenTrendPoint | undefined;
  if (!active || !point) return null;

  return (
    <div
      className="w-[min(24rem,calc(100vw-3.5rem))] max-h-[40vh] overflow-hidden rounded-lg border bg-popover text-xs text-popover-foreground shadow-md"
      onMouseMove={(event) => event.stopPropagation()}
      onTouchMove={(event) => event.stopPropagation()}
    >
      <p className="mb-1 px-3 pt-2 font-semibold">
        {formatDate(String(label ?? point.date))}
      </p>
      <div
        className="max-h-[calc(40vh-2rem)] overflow-y-auto px-3 pb-2"
        data-testid="cost-dashboard-token-trend-tooltip-models"
      >
        <div className="space-y-1.5">
          {chart.series.map((series) => {
            const metric = point[series.metricKey] as
              | TokenTrendMetric
              | null
              | undefined;
            return (
              <div
                className="border-t pt-1.5 first:border-t-0 first:pt-0"
                key={series.id}
              >
                <div className="flex items-start gap-2">
                  <span
                    aria-hidden="true"
                    className="mt-1.5 size-2 shrink-0 rounded-full"
                    style={{ backgroundColor: series.color }}
                  />
                  <span className="min-w-0 break-all font-medium">
                    {series.label}
                  </span>
                  <span className="ml-auto shrink-0 text-right font-medium">
                    {metric ? formatTokens(metric.total_tokens) : "データなし"}
                  </span>
                </div>
                {metric && (
                  <div className="ml-4 mt-0.5 grid grid-cols-2 gap-x-4 gap-y-0.5 text-[11px]">
                    <span className="text-muted-foreground">
                      {applyFreeTier ? "推定請求額" : "定価換算"}
                    </span>
                    <span className="text-right">
                      {formatCostValue(
                        applyFreeTier
                          ? metric.estimated_billed_cost
                          : metric.list_cost,
                      )}
                    </span>
                    <span className="text-muted-foreground">リクエスト</span>
                    <span className="text-right">
                      {formatTokens(metric.request_count)}
                    </span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export function TokenTrendChartView({
  chart,
  applyFreeTier,
  formatCostValue,
}: {
  chart: TokenTrendChart;
  applyFreeTier: boolean;
  formatCostValue: (cost: number) => string;
}) {
  const dateSelectId = useId();
  const latestDate = chart.data.at(-1)?.date ?? "";
  const [requestedDate, setRequestedDate] = useState(latestDate);
  const narrowViewport = useSyncExternalStore(
    subscribeToViewportResize,
    isNarrowViewport,
    getServerViewportSnapshot,
  );
  const selectedDate = chart.data.some(
    (point) => point.date === requestedDate,
  )
    ? requestedDate
    : latestDate;

  const selectedPoint = useMemo(
    () => chart.data.find((point) => point.date === selectedDate),
    [chart.data, selectedDate],
  );

  return (
    <div className="min-w-0 space-y-2 rounded-lg border p-3">
      <h4 className="text-xs font-semibold text-muted-foreground">
        モデル別トークン推移
      </h4>
      <div
        aria-label="モデル別トークン推移の折れ線グラフ"
        className="h-56 min-w-0 w-full"
        data-testid="cost-dashboard-token-trend-chart"
        role="group"
      >
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            accessibilityLayer
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
              shared
              filterNull={false}
              position={narrowViewport ? { x: 0 } : undefined}
              wrapperStyle={{ pointerEvents: "auto" }}
              content={
                <TokenTrendTooltip
                  applyFreeTier={applyFreeTier}
                  chart={chart}
                  formatCostValue={formatCostValue}
                />
              }
              cursor={{
                stroke: "var(--primary)",
                strokeDasharray: "4 4",
                strokeOpacity: 0.65,
              }}
            />
            {chart.series.map((series) => (
              <Line
                key={series.id}
                type="monotone"
                dataKey={series.valueKey}
                name={series.label}
                stroke={series.color}
                strokeDasharray={series.dashArray || undefined}
                strokeWidth={2}
                dot={{ r: 2, fill: series.color }}
                activeDot={{ r: 5, fill: series.color }}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <ul
        aria-label="モデル系列"
        className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
      >
        {chart.series.map((series) => (
          <li
            key={series.id}
            className="flex w-full min-w-0 max-w-full items-start gap-1 sm:w-fit"
          >
            <svg
              aria-hidden="true"
              className="mt-1 shrink-0"
              height="8"
              width="24"
              viewBox="0 0 24 8"
            >
              <line
                x1="0"
                x2="24"
                y1="4"
                y2="4"
                stroke={series.color}
                strokeDasharray={series.dashArray || undefined}
                strokeWidth="2"
              />
            </svg>
            <span className="min-w-0 break-all">{series.label}</span>
          </li>
        ))}
      </ul>
      <details className="border-t pt-2 text-xs">
        <summary className="cursor-pointer select-none font-medium">
          日付を選んで数値を確認
        </summary>
        <div className="mt-2 space-y-2">
          <AppSelect
            id={dateSelectId}
            aria-label="日別データの日付"
            className="h-8 max-w-full"
            value={selectedDate}
            onValueChange={setRequestedDate}
          >
            {chart.data.map((point) => (
              <option key={point.date} value={point.date}>
                {point.date}
              </option>
            ))}
          </AppSelect>
          <ul
            aria-label={`${selectedDate} のモデル別トークン数`}
            aria-live="polite"
            className="space-y-1"
          >
            {chart.series.map((series) => {
              const metric = selectedPoint?.[series.metricKey] as
                | TokenTrendMetric
                | null
                | undefined;
              return (
                <li
                  key={series.id}
                  className="flex items-start justify-between gap-3"
                >
                  <span className="min-w-0 break-all">{series.label}</span>
                  <span className="shrink-0 text-right">
                    {metric ? `${formatTokens(metric.total_tokens)} トークン` : "データなし"}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      </details>
    </div>
  );
}
