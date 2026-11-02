"use client";

import { useEffect, useState } from "react";

const IDLE_POLL_MS = 30_000;
const DRAINING_POLL_MS = 5_000;

type WaitingItem = {
  kind: string;
  reason?: string;
  source_type?: string;
  intent_key?: string;
  state?: string;
};

export type RestartDrainStatus = {
  state: "idle" | "draining" | "shutting_down";
  waiting_count: number;
  waiting_items: WaitingItem[];
  remaining_seconds: number | null;
};

const REASON_LABELS: Record<string, string> = {
  conversation_dispatch: "会話の送信",
  conversation_generation: "会話の応答生成",
  conversation_late_finalize: "会話の後処理",
  admin_restart_chat_active: "会話",
  local_agent_tasks: "ローカルエージェントタスク",
  gpu_admission_active: "GPU処理",
  db_dispatch_outbox: "会話の配信待ち",
  agent_work_active_claims: "エージェント処理",
  agent_work_owned_tasks: "エージェント処理",
  db_agent_work_active: "エージェント処理",
  busy_state_unknown: "状態を確認中",
  agent_work_non_preemptible: "中断できない処理",
};

function itemLabel(item: WaitingItem): string {
  if (item.kind === "non_preemptible") {
    return ["中断できない処理", item.reason, item.source_type].filter(Boolean).join(": ");
  }
  if (item.kind === "agent_work") {
    return [item.source_type, item.intent_key].filter(Boolean).join(": ") || "エージェント処理";
  }
  return REASON_LABELS[item.kind] ?? item.kind;
}

/** 表示する文言。予約が無ければ null。 */
export function describeRestartDrain(
  status: RestartDrainStatus | null,
): { label: string; title: string } | null {
  if (!status || status.state === "idle") return null;
  if (status.state === "shutting_down") {
    return { label: "まもなく再起動します", title: "処理が終わったため再起動を始めました" };
  }
  const minutes =
    status.remaining_seconds === null ? null : Math.ceil(status.remaining_seconds / 60);
  const label =
    `再起動待ち・処理中 ${status.waiting_count} 件` +
    (minutes === null ? "" : `（上限まで ${minutes} 分）`);
  const lines = status.waiting_items.map(itemLabel);
  return {
    label,
    title: [
      "新しいエージェント処理の開始を止めています。会話はそのまま使えます。中断してよい処理は再起動後に続きから再開します。",
      ...lines,
    ].join("\n"),
  };
}

/** 管理者向け: 再起動予約中だけ画面隅に小さく表示する。 */
export function RestartDrainIndicator() {
  const [status, setStatus] = useState<RestartDrainStatus | null>(null);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      let next: RestartDrainStatus | null = null;
      try {
        const response = await fetch("/api/python-proxy/admin/restart-drain", {
          cache: "no-store",
          credentials: "include",
        });
        // Non-admins (403) and older backends (404) never see the indicator.
        if ([401, 403, 404].includes(response.status)) return;
        if (response.ok) next = (await response.json()) as RestartDrainStatus;
      } catch {
        // 再起動中の切断は次の確認で扱う。
      }
      if (stopped) return;
      setStatus(next);
      timer = window.setTimeout(
        poll,
        next && next.state !== "idle" ? DRAINING_POLL_MS : IDLE_POLL_MS,
      );
    };
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, []);

  const view = describeRestartDrain(status);
  if (!view) return null;
  return (
    <div
      role="status"
      title={view.title}
      data-restart-drain={status?.state}
      className="fixed bottom-3 left-3 z-50 rounded-full border border-amber-500/40 bg-amber-50 px-3 py-1 text-xs text-amber-900 shadow-sm dark:bg-amber-950 dark:text-amber-100"
    >
      {view.label}
    </div>
  );
}
