import { afterEach, describe, expect, it, vi } from "vitest";
import { measureTaskApi } from "./task-api-measure";
describe("Task API measurement", () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
  it("does not log fast success", async () => {
    vi.useFakeTimers(); const log = vi.spyOn(console, "info").mockImplementation(() => {});
    const response = new Response(null, { status: 200 });
    expect(await measureTaskApi("list", async () => response)).toBe(response); expect(log).not.toHaveBeenCalled();
  });
  it("logs slow responses with status", async () => {
    vi.useFakeTimers(); const log = vi.spyOn(console, "info").mockImplementation(() => {});
    await measureTaskApi("detail", async () => { vi.advanceTimersByTime(1000); return new Response(null, { status: 200 }); });
    expect(log).toHaveBeenCalledOnce(); expect(log).toHaveBeenCalledWith("Task API", expect.objectContaining({ operation: "detail", elapsed: 1000, status: 200, timestamp: expect.any(String) }));
  });
  it("logs errors and preserves responses and exceptions", async () => {
    vi.useFakeTimers(); const log = vi.spyOn(console, "info").mockImplementation(() => {});
    const response = new Response(null, { status: 403 }); expect(await measureTaskApi("create", async () => response)).toBe(response);
    expect(log).toHaveBeenCalledWith("Task API", expect.objectContaining({ status: 403 })); log.mockClear();
    const error = new TypeError("private input"); await expect(measureTaskApi("create", async () => { throw error; })).rejects.toBe(error);
    expect(log).toHaveBeenCalledOnce(); expect(log).toHaveBeenCalledWith("Task API", expect.objectContaining({ status: 500, errorName: "TypeError" })); expect(JSON.stringify(log.mock.calls)).not.toContain("private input");
  });
});
