export type StorySaveState = "saved" | "saving" | "dirty" | "failed";

export function saveStatusLabel(status: StorySaveState): string {
  return status === "saving"
    ? "保存中"
    : status === "dirty"
      ? "未保存の変更"
      : status === "failed"
        ? "保存に失敗"
        : "保存済み";
}
