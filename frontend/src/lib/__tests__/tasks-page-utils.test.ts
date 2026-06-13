import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildTaskDateUpdate,
  dateColor,
  formatDuration,
  getStatusShortcutTarget,
  hasNonMidnightTime,
  isDatePast,
  isFutureTask,
  isOverdue,
  isToday,
  parseTaskDateValue,
} from "@/lib/tasks-page-utils";
import type { Task } from "@/lib/task-api";

// 日付依存のテストはシステム時刻を固定する（2026-06-11 12:00 ローカル）
const NOW = new Date(2026, 5, 11, 12, 0, 0);

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "t1",
    title: "テスト",
    status: "open",
    all_day: false,
    ...overrides,
  } as Task;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("parseTaskDateValue", () => {
  it("ローカル日時文字列をローカル時刻として解釈する", () => {
    const d = parseTaskDateValue("2026-06-11T09:30");
    expect(d?.getHours()).toBe(9);
    expect(d?.getMinutes()).toBe(30);
  });

  it("null・不正値は null を返す", () => {
    expect(parseTaskDateValue(null)).toBeNull();
    expect(parseTaskDateValue("invalid")).toBeNull();
  });
});

describe("formatDuration", () => {
  it("0 以下は空文字", () => {
    expect(formatDuration(0)).toBe("");
    expect(formatDuration(-10)).toBe("");
  });

  it("時間と分を組み合わせて表示する", () => {
    expect(formatDuration(59)).toBe("0m");
    expect(formatDuration(60)).toBe("1m");
    expect(formatDuration(3600)).toBe("1h");
    expect(formatDuration(3660)).toBe("1h 1m");
  });
});

describe("isToday", () => {
  it("今日の日付は true", () => {
    expect(isToday("2026-06-11T23:59")).toBe(true);
    expect(isToday("2026-06-11")).toBe(true);
  });

  it("昨日・明日は false", () => {
    expect(isToday("2026-06-10T12:00")).toBe(false);
    expect(isToday("2026-06-12T00:00")).toBe(false);
  });
});

describe("isFutureTask", () => {
  it("start_at が明日以降なら true", () => {
    expect(isFutureTask(makeTask({ start_at: "2026-06-12T00:00" }))).toBe(true);
  });

  it("今日中・過去・未設定は false", () => {
    expect(isFutureTask(makeTask({ start_at: "2026-06-11T23:00" }))).toBe(
      false,
    );
    expect(isFutureTask(makeTask({ start_at: "2026-06-01T00:00" }))).toBe(
      false,
    );
    expect(isFutureTask(makeTask())).toBe(false);
  });
});

describe("isOverdue", () => {
  it("時刻付き期限が過ぎていれば true", () => {
    expect(isOverdue(makeTask({ end_at: "2026-06-11T11:59" }))).toBe(true);
    expect(isOverdue(makeTask({ end_at: "2026-06-11T12:01" }))).toBe(false);
  });

  it("終日・00:00 期限は日単位で判定する（当日中は false）", () => {
    expect(
      isOverdue(makeTask({ end_at: "2026-06-11T00:00", all_day: true })),
    ).toBe(false);
    expect(
      isOverdue(makeTask({ end_at: "2026-06-10T00:00", all_day: true })),
    ).toBe(true);
  });

  it("closed タスクと期限なしは false", () => {
    expect(
      isOverdue(makeTask({ end_at: "2026-06-01T00:00", status: "closed" })),
    ).toBe(false);
    expect(isOverdue(makeTask())).toBe(false);
  });
});

describe("isDatePast", () => {
  it("終日は日単位、時刻付きは分単位で過去判定する", () => {
    expect(isDatePast("2026-06-11", { all_day: true })).toBe(false);
    expect(isDatePast("2026-06-10", { all_day: true })).toBe(true);
    expect(isDatePast("2026-06-11T11:00", { all_day: false })).toBe(true);
    expect(isDatePast("2026-06-11T13:00", { all_day: false })).toBe(false);
  });
});

describe("dateColor", () => {
  const RED = "text-red-500 dark:text-red-400";

  it("Due Date: 過去=赤、今日=強調、未来=通常", () => {
    expect(dateColor("2026-06-10T10:00", makeTask(), "end")).toBe(RED);
    expect(dateColor("2026-06-11T18:00", makeTask(), "end")).not.toBe("");
    expect(dateColor("2026-07-01T10:00", makeTask(), "end")).toBe("");
  });

  it("Start Date: 過去 + Due 過去 = 赤、過去 + Due 未来 = 作業期間中の強調", () => {
    const overdueTask = makeTask({ end_at: "2026-06-10T10:00" });
    expect(dateColor("2026-06-09T10:00", overdueTask, "start")).toBe(RED);

    const ongoingTask = makeTask({ end_at: "2026-07-01T10:00" });
    const color = dateColor("2026-06-09T10:00", ongoingTask, "start");
    expect(color).not.toBe(RED);
    expect(color).not.toBe("");
  });

  it("closed タスクは常に通常色", () => {
    expect(
      dateColor("2026-06-01T10:00", makeTask({ status: "closed" }), "end"),
    ).toBe("");
  });
});

describe("hasNonMidnightTime", () => {
  it("00:00 以外の時刻を持つ場合のみ true", () => {
    expect(hasNonMidnightTime("2026-06-11T09:30")).toBe(true);
    expect(hasNonMidnightTime("2026-06-11T00:00")).toBe(false);
    expect(hasNonMidnightTime("2026-06-11")).toBe(false);
    expect(hasNonMidnightTime(null)).toBe(false);
  });
});

describe("buildTaskDateUpdate", () => {
  it("日付のみの指定は all_day=true、日付値はそのまま渡す", () => {
    const update = buildTaskDateUpdate(makeTask(), {
      start_at: "2026-06-11",
      end_at: "2026-06-12",
    });
    expect(update.all_day).toBe(true);
    expect(update.start_at).toBe("2026-06-11");
    expect(update.end_at).toBe("2026-06-12");
  });

  it("時刻を含む指定は all_day=false、秒を補完する", () => {
    const update = buildTaskDateUpdate(makeTask(), {
      start_at: "2026-06-11T09:30",
    });
    expect(update.all_day).toBe(false);
    expect(update.start_at).toBe("2026-06-11T09:30:00");
    expect(update).not.toHaveProperty("end_at");
  });

  it("変更されない既存日付も all_day 判定に含める", () => {
    const task = makeTask({ start_at: "2026-06-11T09:30:00", all_day: false });
    const update = buildTaskDateUpdate(task, { end_at: "2026-06-12" });
    expect(update.all_day).toBe(false);
    expect(update.end_at).toBe("2026-06-12");
  });

  it("日付の全削除は all_day=false で null を渡す", () => {
    const task = makeTask({ start_at: "2026-06-11T09:30:00" });
    const update = buildTaskDateUpdate(task, { start_at: null });
    expect(update.all_day).toBe(false);
    expect(update.start_at).toBeNull();
  });
});

describe("getStatusShortcutTarget", () => {
  it("定義済みキーをステータスへ解決する（大文字小文字を無視）", () => {
    expect(getStatusShortcutTarget("c")).toBe("closed");
    expect(getStatusShortcutTarget("S")).toBe("in_progress");
    expect(getStatusShortcutTarget("z")).toBeUndefined();
  });
});
