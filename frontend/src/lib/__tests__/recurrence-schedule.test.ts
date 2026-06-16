import { describe, expect, it } from "vitest";

import {
  applyOccurrenceDuration,
  computeNextRecurringScheduleAfter,
  getOccurrenceDurationMs,
} from "@/lib/recurrence-schedule";

const DAILY_CONFIG = {
  freq: "DAILY",
  interval: 1,
  byDay: [],
  skipWeekend: false,
  skipHoliday: false,
  endCount: null,
  endDate: null,
};

describe("recurrence-schedule", () => {
  it("同一日時の start/end はゼロ継続時間として保持する", () => {
    const start = new Date(2026, 5, 15, 0, 0, 0);
    const durationMs = getOccurrenceDurationMs(start, start);

    expect(durationMs).toBe(0);
    expect(applyOccurrenceDuration(start, durationMs)?.getTime()).toBe(
      start.getTime(),
    );
  });

  it("同日終日繰り返しの次回分でも endAt を null にしない", () => {
    const result = computeNextRecurringScheduleAfter({
      currentStartAt: new Date(2026, 5, 15, 0, 0, 0),
      currentEndAt: new Date(2026, 5, 15, 0, 0, 0),
      config: DAILY_CONFIG,
      after: new Date(2026, 5, 15, 0, 0, 0),
    });

    expect(result?.startAt).toEqual(new Date(2026, 5, 16, 0, 0, 0));
    expect(result?.endAt).toEqual(new Date(2026, 5, 16, 0, 0, 0));
  });

  it("終了日時がない場合は endAt を生成しない", () => {
    const result = computeNextRecurringScheduleAfter({
      currentStartAt: new Date(2026, 5, 15, 0, 0, 0),
      currentEndAt: null,
      config: DAILY_CONFIG,
      after: new Date(2026, 5, 15, 0, 0, 0),
    });

    expect(result?.startAt).toEqual(new Date(2026, 5, 16, 0, 0, 0));
    expect(result?.endAt).toBeNull();
  });
});
