import type { Task } from "../../types/api";

export type TaskStatusOption = {
  value: string;
  label: string;
  color: string;
  icon: string;
};

export const TASK_STATUS_OPTIONS: readonly TaskStatusOption[] = [
  {
    value: "open",
    label: "未着手",
    color: "#a6adc8",
    icon: "circle-outline",
  },
  {
    value: "in_progress",
    label: "進行中",
    color: "#f38ba8",
    icon: "progress-clock",
  },
  {
    value: "on_hold",
    label: "保留",
    color: "#f5c2e7",
    icon: "pause-circle-outline",
  },
  {
    value: "review",
    label: "レビュー待ち",
    color: "#89dceb",
    icon: "file-check-outline",
  },
  {
    value: "closed",
    label: "完了",
    color: "#a6e3a1",
    icon: "check-circle",
  },
];

/**
 * 選択肢からは外したが、既存データの表示には必要なステータス。
 *
 * Web側の選択肢（open/in_progress/on_hold/review/closed）へ揃えるため
 * 「取消」は選べなくした。ただし過去に付いた `cancelled` を表示すると
 * ラベルもアイコンも解決できず生の英字になるため、表示用にだけ残す。
 */
const LEGACY_TASK_STATUS_OPTIONS: readonly TaskStatusOption[] = [
  {
    value: "cancelled",
    label: "取消",
    color: "#a6adc8",
    icon: "close-circle-outline",
  },
];

export const TASK_STATUS_SHORTCUT_KEYS: Readonly<Record<string, string>> = {
  c: "closed",
  s: "in_progress",
  h: "on_hold",
  r: "review",
  x: "open",
};

export function getTaskStatusOption(
  status: string,
): TaskStatusOption | undefined {
  return (
    TASK_STATUS_OPTIONS.find((option) => option.value === status) ??
    LEGACY_TASK_STATUS_OPTIONS.find((option) => option.value === status)
  );
}

const CREATED_AT_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$/;

/**
 * created_at を比較可能な epoch ms へ変換する。
 *
 * `Date.parse` を直接使わないのは、同じタスクでも供給元によって表記が変わるため。
 * サーバーは `datetime.utcnow()` の TZ 指定なし（UTC実体）、端末生成分は `Z` 付きで
 * 保存されうるが、`Date.parse` は TZ 指定なしを端末ローカル時刻として解釈する。
 * その結果、同じタスクがローカル読み出しとサーバー応答で最大±TZ分ずれて評価され、
 * リロードのたびに一覧の並びが入れ替わっていた。ここでは TZ 指定なしを UTC として
 * 扱い、どちらの表記でも同じ値になるよう正規化する。小数秒も桁数に依存しない。
 */
function toComparableCreatedAt(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const match = value.trim().match(CREATED_AT_PATTERN);
  if (!match) return null;
  const [, year, month, day, hour, minute, second, fraction, offset] = match;
  let time = Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second ?? "0"),
    fraction ? Math.floor(Number(`0.${fraction}`) * 1000) : 0,
  );
  if (offset && offset !== "Z") {
    const sign = offset.startsWith("-") ? -1 : 1;
    const digits = offset.slice(1).replace(":", "");
    time -= sign * (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2))) * 60_000;
  }
  return Number.isFinite(time) ? time : null;
}

/**
 * 作成日時が有効なタスクを newest-first で返す。
 * invalid/null は末尾へ置く。
 *
 * 同時刻・invalid同士の順序は id で決める。取得元配列のindexで決めると、
 * ローカル読み出しとサーバー応答で供給順が違うぶんだけ表示順が揺れる。
 */
export function sortTasksNewestFirst<T extends Pick<Task, "id" | "created_at">>(
  tasks: readonly T[],
): T[] {
  return tasks
    .map((task) => ({ task, timestamp: toComparableCreatedAt(task.created_at) }))
    .sort((left, right) => {
      if (left.timestamp !== null && right.timestamp !== null) {
        return (
          right.timestamp - left.timestamp ||
          String(left.task.id).localeCompare(String(right.task.id))
        );
      }
      if (left.timestamp !== null) return -1;
      if (right.timestamp !== null) return 1;
      return String(left.task.id).localeCompare(String(right.task.id));
    })
    .map(({ task }) => task);
}

type TaskOrderItem = Pick<Task, "id" | "created_at"> & {
  sort_order?: number | null;
};

/**
 * 一覧に表示する正規順を返す。
 *
 * `sort_order` を持つ行は repository が返す canonical order を尊重する。
 * 旧キャッシュなど sort_order が欠けた行だけは created_at の安定順へ
 * fallback する。表示側で created_at を第一キーにすると並べ替え結果を
 * 読み直し時に失うため、タスク一覧ではこの関数を使う。
 */
export function sortTasksCanonical<T extends TaskOrderItem>(
  tasks: readonly T[],
): T[] {
  const fallback = sortTasksNewestFirst(tasks);
  const fallbackIndex = new Map(
    fallback.map((task, index) => [String(task.id), index]),
  );

  return [...tasks].sort((left, right) => {
    const leftOrder =
      typeof left.sort_order === "number" && Number.isFinite(left.sort_order)
        ? left.sort_order
        : null;
    const rightOrder =
      typeof right.sort_order === "number" && Number.isFinite(right.sort_order)
        ? right.sort_order
        : null;

    if (leftOrder !== null && rightOrder !== null) {
      return (
        leftOrder - rightOrder ||
        String(left.id).localeCompare(String(right.id))
      );
    }
    if (leftOrder !== null) return -1;
    if (rightOrder !== null) return 1;
    return (
      (fallbackIndex.get(String(left.id)) ?? Number.MAX_SAFE_INTEGER) -
      (fallbackIndex.get(String(right.id)) ?? Number.MAX_SAFE_INTEGER)
    );
  });
}

export type ReorderVisibleTaskIdsOptions = {
  /** D&D開始時点の canonical 全体順。 */
  canonicalTaskIds: readonly string[];
  /** filter/search後に画面へ見えている順。 */
  visibleTaskIds: readonly string[];
  /** blockとして移動する選択済みtask IDs。 */
  selectedTaskIds: ReadonlySet<string> | readonly string[];
  /** visible配列の挿入位置。0なら先頭、lengthなら末尾。 */
  targetVisibleIndex: number;
};

/**
 * visible subsetだけを並べ替え、canonical full orderへ差し戻す。
 *
 * hidden taskは visible slot の間に残り、相対順も変わらない。複数選択は
 * visible order中のblockとして抜き出すため、block内部の順序を保持する。
 */
export function reorderVisibleTaskIds({
  canonicalTaskIds,
  visibleTaskIds,
  selectedTaskIds,
  targetVisibleIndex,
}: ReorderVisibleTaskIdsOptions): string[] {
  const selected =
    selectedTaskIds instanceof Set
      ? selectedTaskIds
      : new Set(selectedTaskIds);
  const visibleSet = new Set(visibleTaskIds);
  const visible = visibleTaskIds.filter((taskId) =>
    canonicalTaskIds.includes(taskId),
  );
  const moving = visible.filter((taskId) => selected.has(taskId));
  if (moving.length === 0 || visible.length < 2) return [...canonicalTaskIds];

  const remaining = visible.filter((taskId) => !selected.has(taskId));
  const clampedTarget = Math.max(
    0,
    Math.min(Math.trunc(targetVisibleIndex), visible.length),
  );
  const removedBeforeTarget = visible
    .slice(0, clampedTarget)
    .filter((taskId) => selected.has(taskId)).length;
  const insertionIndex = Math.max(
    0,
    Math.min(remaining.length, clampedTarget - removedBeforeTarget),
  );
  const nextVisible = [
    ...remaining.slice(0, insertionIndex),
    ...moving,
    ...remaining.slice(insertionIndex),
  ];

  const nextVisibleIterator = nextVisible[Symbol.iterator]();
  const next = canonicalTaskIds.map((taskId) =>
    visibleSet.has(taskId) ? nextVisibleIterator.next().value ?? taskId : taskId,
  );
  return next;
}

/**
 * D&Dの結果が元順と同じかを、重複や欠落を含めて安全に比較する。
 */
export function areTaskOrdersEqual(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

export type TaskPressAction = "navigate" | "toggle-selection" | "ignore";

export function resolveTaskPressAction(options: {
  wasLongPress: boolean;
  selectionMode: boolean;
}): TaskPressAction {
  if (options.wasLongPress) return "ignore";
  return options.selectionMode ? "toggle-selection" : "navigate";
}
