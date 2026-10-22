"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { usageApi, type TokenUsageDetail } from "@/lib/ecc-api";

type DetailState = { key: string; record?: TokenUsageDetail; error?: string };
const amount = (value: string | null) =>
  value === null ? "未計測" : `${value} USD`;

export function UsageRecordDetail() {
  const query = useSearchParams();
  const usageId = query?.get("usage_id") || "";
  const runId = query?.get("run_id") || "";
  const key = `${usageId}:${runId}`;
  const [state, setState] = useState<DetailState | null>(null);
  useEffect(() => {
    if (!usageId || !runId) return;
    let active = true;
    usageApi.getRecordDetail(usageId, runId).then(
      (record) => {
        if (!active) return;
        if (record.id !== usageId || record.agent_run_id !== runId) {
          setState({ key, error: "指定された費用記録を確認できません。" });
        } else setState({ key, record });
      },
      () => {
        if (active)
          setState({
            key,
            error: "費用記録を取得できません。権限と対象を確認してください。",
          });
      },
    );
    return () => {
      active = false;
    };
  }, [usageId, runId, key]);
  if (!usageId || !runId) return null;
  const current = state?.key === key ? state : null;
  const record = current?.record;
  return (
    <Card className="mb-4" aria-label="費用記録の詳細">
      <CardHeader>
        <CardTitle className="inline-flex items-center gap-1">
          費用記録の詳細
          <HelpPopover label="費用記録の詳細">定価見積りとproviderが返した実費を区別します。実費が返らない場合は未計測です。</HelpPopover>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {current?.error ? (
          <p role="alert">{current.error}</p>
        ) : !record ? (
          <p role="status">費用記録を読み込み中…</p>
        ) : (
          <>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-2 text-sm [&_dd]:break-all">
              <dt>Usage</dt>
              <dd>{record.id}</dd>
              <dt>Run</dt>
              <dd>{record.agent_run_id}</dd>
              <dt>Project</dt>
              <dd>{record.project_id || "なし"}</dd>
              <dt>記録所有者</dt>
              <dd>{record.user_id || "未記録"}</dd>
              <dt>担当</dt>
              <dd>{record.agent_name || "未記録"}</dd>
              <dt>Provider</dt>
              <dd>{record.provider}</dd>
              <dt>要求モデル</dt>
              <dd>{record.requested_model}</dd>
              <dt>実行モデル</dt>
              <dd>{record.resolved_model || "未記録"}</dd>
              <dt>トークン</dt>
              <dd>
                入力 {record.input_tokens} / 出力 {record.output_tokens} / 合計{" "}
                {record.total_tokens}
              </dd>
              <dt>記録時刻</dt>
              <dd>{record.created_at || "未記録"}</dd>
              <dt>料金判定</dt>
              <dd>
                {(
                  {
                    priced: "料金確定",
                    free_incentive: "無料枠対象",
                    unknown: "料金未確定",
                    local: "ローカル実行",
                    subscription: "サブスクリプション",
                  } as Record<string, string>
                )[record.pricing_status || ""] || "未確定"}
              </dd>
              <dt>定価見積り</dt>
              <dd>{amount(record.list_total_cost)}</dd>
              <dt>計測済み実費</dt>
              <dd>{amount(record.provider_reported_cost)}</dd>
            </dl>
          </>
        )}
      </CardContent>
    </Card>
  );
}
