"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  DollarSign,
  Hash,
  Activity,
  ChevronDown,
  ChevronUp,
  Loader2,
  RefreshCw,
  Calendar,
} from "lucide-react";
import {
  usageApi,
  type TokenUsageSummary,
} from "@/lib/ecc-api";
import { formatLocalDate } from "@/lib/date-time";

type BreakdownTab = "model" | "project" | "agent";

function formatCost(cost: number): string {
  return `$${cost.toFixed(4)}`;
}

function formatTokens(tokens: number): string {
  return tokens.toLocaleString("ja-JP");
}

function formatDate(dateStr: string): string {
  try {
    const d = new Date(dateStr);
    return `${d.getMonth() + 1}/${d.getDate()}`;
  } catch {
    return dateStr;
  }
}

function getDefaultDateRange(): { start: string; end: string } {
  const now = new Date();
  const end = formatLocalDate(now);
  const start = new Date(now);
  start.setDate(start.getDate() - 29);
  return { start: formatLocalDate(start), end };
}

export function CostDashboardSection() {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [dashboard, setDashboard] = useState<TokenUsageSummary | null>(null);
  const [breakdownTab, setBreakdownTab] = useState<BreakdownTab>("model");

  // 日付範囲フィルタ
  const defaults = useMemo(() => getDefaultDateRange(), []);
  const [dateFrom, setDateFrom] = useState(defaults.start);
  const [dateTo, setDateTo] = useState(defaults.end);

  // 詳細データ
  const [modelBreakdown, setModelBreakdown] = useState<
    TokenUsageSummary["model_breakdown"]
  >([]);
  const [projectBreakdown, setProjectBreakdown] = useState<
    Array<{
      project_id: string;
      project_name: string;
      total_cost: number;
      total_tokens: number;
      request_count: number;
    }>
  >([]);
  const [agentBreakdown, setAgentBreakdown] = useState<
    Array<{
      agent_id: string;
      agent_name: string;
      total_cost: number;
      total_tokens: number;
      request_count: number;
    }>
  >([]);

  const fetchDashboard = useCallback(async () => {
    setLoading(true);
    try {
      const data = await usageApi.getDashboard();
      setDashboard(data);
    } catch (err) {
      console.error("コストダッシュボード取得失敗:", err);
      setDashboard(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchBreakdown = useCallback(async () => {
    if (!dateFrom || !dateTo) return;
    try {
      const results = await Promise.allSettled([
        usageApi.getByModel(dateFrom, dateTo),
        usageApi.getByProject(dateFrom, dateTo),
        usageApi.getByAgent(dateFrom, dateTo),
      ]);
      if (results[0].status === "fulfilled") setModelBreakdown(results[0].value);
      if (results[1].status === "fulfilled") setProjectBreakdown(results[1].value);
      if (results[2].status === "fulfilled") setAgentBreakdown(results[2].value);
    } catch (err) {
      console.error("内訳データ取得失敗:", err);
    }
  }, [dateFrom, dateTo]);

  const handleToggle = useCallback(() => {
    if (!expanded && !dashboard) {
      fetchDashboard();
      fetchBreakdown();
    }
    setExpanded((v) => !v);
  }, [expanded, dashboard, fetchDashboard, fetchBreakdown]);

  const handleRefresh = useCallback(() => {
    fetchDashboard();
    fetchBreakdown();
  }, [fetchDashboard, fetchBreakdown]);

  // 日付変更時に内訳を再取得
  useEffect(() => {
    if (expanded && dateFrom && dateTo) {
      fetchBreakdown();
    }
  }, [expanded, dateFrom, dateTo, fetchBreakdown]);

  // 7日間チャートの最大コスト
  const maxDailyCost = useMemo(() => {
    if (!dashboard?.daily_trend.length) return 0;
    return Math.max(...dashboard.daily_trend.map((d) => d.total_cost));
  }, [dashboard]);

  // モデル別内訳をコスト降順にソート
  const sortedModelBreakdown = useMemo(
    () => [...modelBreakdown].sort((a, b) => b.total_cost - a.total_cost),
    [modelBreakdown],
  );

  const sortedProjectBreakdown = useMemo(
    () => [...projectBreakdown].sort((a, b) => b.total_cost - a.total_cost),
    [projectBreakdown],
  );

  const sortedAgentBreakdown = useMemo(
    () => [...agentBreakdown].sort((a, b) => b.total_cost - a.total_cost),
    [agentBreakdown],
  );

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={handleToggle}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <DollarSign className="size-4" />
            トークン使用量 / コストダッシュボード
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
              {/* サマリーカード */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div className="rounded-lg border p-3">
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                    <DollarSign className="size-3" />
                    本日のコスト
                  </div>
                  <p className="text-xl font-bold">
                    {formatCost(dashboard.today.total_cost)}
                  </p>
                </div>
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

              {/* 7日間コスト推移 (CSS Bar Chart) */}
              {dashboard.daily_trend.length > 0 && (
                <div className="space-y-2">
                  <h3 className="text-xs font-semibold text-muted-foreground">
                    7日間コスト推移
                  </h3>
                  <div className="flex items-end gap-1 h-32">
                    {dashboard.daily_trend.slice(-7).map((day) => {
                      const pct =
                        maxDailyCost > 0
                          ? (day.total_cost / maxDailyCost) * 100
                          : 0;
                      return (
                        <div
                          key={day.date}
                          className="flex-1 flex flex-col items-center gap-1"
                        >
                          <span className="text-[9px] text-muted-foreground">
                            {formatCost(day.total_cost)}
                          </span>
                          <div className="w-full flex-1 flex items-end">
                            <div
                              className="w-full rounded-t bg-primary transition-all hover:bg-primary/80"
                              style={{
                                height: `${Math.max(pct, 2)}%`,
                                minHeight: "2px",
                              }}
                              title={`${formatDate(day.date)}: ${formatCost(day.total_cost)} / ${formatTokens(day.total_tokens)} tokens`}
                            />
                          </div>
                          <span className="text-[10px] text-muted-foreground">
                            {formatDate(day.date)}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* 日付範囲フィルタ */}
              <div className="flex flex-wrap items-center gap-2">
                <Calendar className="size-3.5 text-muted-foreground" />
                <Input
                  type="date"
                  value={dateFrom}
                  onChange={(e) => setDateFrom(e.target.value)}
                  className="w-36 h-8 text-xs"
                />
                <span className="text-xs text-muted-foreground">~</span>
                <Input
                  type="date"
                  value={dateTo}
                  onChange={(e) => setDateTo(e.target.value)}
                  className="w-36 h-8 text-xs"
                />
                <Button variant="ghost" size="sm" onClick={handleRefresh}>
                  <RefreshCw className="size-3" />
                </Button>
              </div>

              {/* 内訳タブ */}
              <Tabs
                value={breakdownTab}
                onValueChange={(v) => setBreakdownTab(v as BreakdownTab)}
              >
                <TabsList>
                  <TabsTrigger value="model">モデル別</TabsTrigger>
                  <TabsTrigger value="project">プロジェクト別</TabsTrigger>
                  <TabsTrigger value="agent">エージェント別</TabsTrigger>
                </TabsList>
              </Tabs>

              {/* 内訳テーブル */}
              <div className="max-h-80 overflow-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      {breakdownTab === "model" && (
                        <>
                          <th className="py-1.5 pr-2 font-medium">プロバイダ</th>
                          <th className="py-1.5 pr-2 font-medium">モデル</th>
                        </>
                      )}
                      {breakdownTab === "project" && (
                        <th className="py-1.5 pr-2 font-medium">プロジェクト</th>
                      )}
                      {breakdownTab === "agent" && (
                        <th className="py-1.5 pr-2 font-medium">エージェント</th>
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
                        sortedModelBreakdown.map((row, i) => (
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
                            <td className="py-1.5 pr-2 text-right">
                              {formatTokens(row.total_tokens)}
                            </td>
                            <td className="py-1.5 pr-2 text-right font-medium">
                              {formatCost(row.total_cost)}
                            </td>
                            <td className="py-1.5 text-right">
                              {row.request_count.toLocaleString("ja-JP")}
                            </td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td
                            colSpan={5}
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
                              {row.project_name || row.project_id}
                            </td>
                            <td className="py-1.5 pr-2 text-right">
                              {formatTokens(row.total_tokens)}
                            </td>
                            <td className="py-1.5 pr-2 text-right font-medium">
                              {formatCost(row.total_cost)}
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
                              {row.agent_name || row.agent_id}
                            </td>
                            <td className="py-1.5 pr-2 text-right">
                              {formatTokens(row.total_tokens)}
                            </td>
                            <td className="py-1.5 pr-2 text-right font-medium">
                              {formatCost(row.total_cost)}
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
