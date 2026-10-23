import type { ConflictKind, SyncV3Status } from "../../sync-v3/contract";

export type SyncTone = "ok" | "busy" | "info" | "warn" | "error";

export type SyncStatusPresentation = {
  label: string;
  tone: SyncTone;
  detail: string | null;
};

export function describeSyncStatus(status: SyncV3Status | null): SyncStatusPresentation {
  if (!status) return { label: "同期を準備中", tone: "info", detail: null };
  switch (status.overall) {
    case "synced":
      return { label: "同期済み", tone: "ok", detail: null };
    case "syncing":
      return { label: "同期中", tone: "busy", detail: null };
    case "pending": {
      const { queued, backoff, sent } = status.queue;
      return { label: `送信待ち ${queued + backoff + sent}件`, tone: "info", detail: null };
    }
    case "bootstrapping": {
      const { seen, total } = status.bootstrap;
      return {
        label: total === null ? `初回取得中 ${seen}件` : `初回取得中 ${seen}/${total}`,
        tone: "busy",
        detail: null,
      };
    }
    case "offline":
      return { label: "オフライン（端末に保存済み）", tone: "info", detail: null };
    case "auth_required":
      return { label: "再ログインが必要", tone: "warn", detail: null };
    case "disabled":
      return {
        label: "同期は一時停止中（データは端末に保持）",
        tone: "warn",
        detail: status.disabledReason,
      };
    case "migration_pending":
      return { label: "同期の移行確認が必要", tone: "warn", detail: null };
    case "degraded":
      return {
        label: "同期が遅れています",
        tone: "warn",
        detail:
          status.disabledReason ?? status.push.lastErrorCode ?? status.changes.lastErrorCode ?? status.bootstrap.lastErrorCode,
      };
  }
}

export function describeConflictBadge(count: number): string | null {
  return count > 0 ? `要確認 ${count}件` : null;
}

const KIND_LABELS: Record<ConflictKind, string> = {
  field_conflict: "同時編集",
  entity_deleted: "サーバーで削除済み",
  forbidden: "権限なし",
  invalid: "内容が受け付けられない",
  scope_unknown: "保存先不明",
  access_revoked: "共有が解除",
  migrated_unresolved: "旧版から引継ぎ",
};

export function kindLabel(kind: ConflictKind): string {
  return KIND_LABELS[kind];
}
