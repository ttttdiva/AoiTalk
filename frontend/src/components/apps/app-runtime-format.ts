import { formatTimestamp } from "@/lib/agent-run-timeline-format";
import type { AppRuntimeRun } from "@/lib/apps-api";

const STATUS_LABELS: Record<string, string> = {
  queued: "待機中",
  dispatching: "配送中",
  accepted: "受理",
  starting: "起動中",
  running: "実行中",
  stopping: "停止中",
  succeeded: "成功",
  failed: "失敗",
  cancelled: "キャンセル",
  blocked: "ブロック",
  unknown: "状態不明",
};

const MODE_LABELS: Record<string, string> = {
  finite: "完了型",
  service: "サービス",
  desktop: "デスクトップ",
};

const READINESS_LABELS: Record<string, string> = {
  unknown: "不明",
  not_required: "準備確認不要",
  ready: "準備完了",
  pending: "準備確認中",
  failed: "準備失敗",
  unavailable: "利用不可",
  not_ready: "未準備",
};

const PROCESS_LABELS: Record<string, string> = {
  unknown: "不明",
  not_started: "未起動",
  starting: "起動中",
  running: "実行中",
  stopped: "停止済み",
  exited: "終了",
  succeeded: "成功終了",
  failed: "失敗終了",
  cancelled: "キャンセル終了",
};

export function runtimeStatusLabel(status?: string | null): string {
  if (!status) return STATUS_LABELS.unknown;
  return STATUS_LABELS[status] || status;
}

export function runtimeStatusExplanation(status?: string | null): string | null {
  switch (status) {
    case "unknown":
      return "実行状態を確認できないため、自動監視を停止しました。再読込で再確認できます。";
    case "blocked":
      return "実行はブロックされました。実行Targetの準備状態と権限を確認してください。";
    case "accepted":
      return "実行要求は受理されました。Targetの起動成功とは別の状態です。";
    case "starting":
      return "実行Targetを起動しています。起動成功は process status で確認します。";
    default:
      return null;
  }
}

export function formatRuntimeStatus(
  run: Pick<AppRuntimeRun, "status" | "mode" | "readiness_status" | "process_status" | "exit_code">,
): string {
  const parts = [runtimeStatusLabel(run.status)];
  if (run.mode) parts.push(`mode: ${MODE_LABELS[run.mode] || run.mode}`);
  if (run.readiness_status) parts.push(`readiness: ${READINESS_LABELS[run.readiness_status] || run.readiness_status}`);
  if (run.process_status) parts.push(`process: ${PROCESS_LABELS[run.process_status] || run.process_status}`);
  if (run.exit_code !== null && run.exit_code !== undefined) parts.push(`終了コード ${run.exit_code}`);
  return parts.join(" · ");
}

export function formatRuntimeTimestamp(value?: string | null): string {
  return formatTimestamp(value) || "—";
}

