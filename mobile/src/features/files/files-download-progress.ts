import type { FilesDownloadProgress } from "../../lib/files-download";

export type FilesDownloadPhase =
  | "preparing"
  | "picker"
  | "receiving"
  | "creating"
  | "copying"
  | "sharing"
  | "completed";

export type FilesDownloadViewState = {
  entryPath: string;
  entryName: string;
  locationIdentity: string;
  phase: FilesDownloadPhase;
  receivedBytes?: number;
  totalBytes?: number;
  percent?: number;
};

export function formatDownloadBytes(bytes: number | undefined): string {
  if (bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function downloadPhaseLabel(phase: FilesDownloadPhase): string {
  switch (phase) {
    case "picker":
      return "保存先を選択中";
    case "receiving":
      return "受信中";
    case "creating":
      return "保存ファイルを準備中";
    case "copying":
      return "端末に保存中";
    case "sharing":
      return "共有シートを開いています";
    case "completed":
      return "ダウンロード完了";
    case "preparing":
    default:
      return "ダウンロード準備中";
  }
}

export function downloadStateForEvent(
  previous: FilesDownloadViewState,
  event: FilesDownloadProgress,
): FilesDownloadViewState {
  let phase: FilesDownloadPhase;
  switch (event.operation) {
    case "picker":
      phase = "picker";
      break;
    case "receive":
      phase = "receiving";
      break;
    case "create":
      phase = "creating";
      break;
    case "copy":
      phase = event.state === "completed" ? "completed" : "copying";
      break;
    case "share":
      phase = event.state === "completed" ? "completed" : "sharing";
      break;
  }

  // Receive events are snapshots: an omitted total invalidates earlier values.
  const receivedBytes = event.operation === "receive" ? event.receivedBytes : previous.receivedBytes;
  const totalBytes = event.operation === "receive" ? event.totalBytes : previous.totalBytes;
  const percent = event.operation === "receive" ? event.percent : previous.percent;
  return { ...previous, phase, receivedBytes, totalBytes, percent };
}
