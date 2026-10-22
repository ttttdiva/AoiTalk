import type { components } from "@/lib/api-types.gen";
import { apiFetch } from "@/components/docs/docs-utils";

export const OPEN_DAILY_CAPTURE_EVENT = "global-open-daily-capture";

export { docsTodayDate as dailyCaptureDay } from "@/lib/docs-daily-calendar";

export function isDailyCaptureShortcut(event: KeyboardEvent): boolean {
  return event.ctrlKey && event.altKey && !event.metaKey && !event.shiftKey
    && !event.repeat && !event.isComposing && event.keyCode !== 229
    && !event.getModifierState("AltGraph") && event.key.toLowerCase() === "d";
}

type DailyCaptureWire = components["schemas"]["DailyCaptureResponse"];
type DailyCaptureHistoryWire = components["schemas"]["DailyCaptureListResponse"];

export type DailyCapture = Omit<DailyCaptureWire, "changes"> & {
  changes: NonNullable<DailyCaptureWire["changes"]>;
};
export type DailyCaptureHistory = Omit<DailyCaptureHistoryWire, "items" | "docs_library_id" | "day_node_id"> & {
  items: DailyCapture[];
  docs_library_id: string | null;
  day_node_id: string | null;
};
export type DailyCaptureSubmission = components["schemas"]["DailyCaptureBody"];

const endpoint = "/api/python-proxy/docs/daily-captures";

function normalizeCapture(value: DailyCaptureWire): DailyCapture {
  return { ...value, changes: value.changes ?? [] };
}

export const dailyCaptureApi = {
  history: async (day: string, signal?: AbortSignal): Promise<DailyCaptureHistory> => {
    const value = await apiFetch<DailyCaptureHistoryWire>(`${endpoint}?day=${encodeURIComponent(day)}`, { signal });
    return { ...value, docs_library_id: value.docs_library_id ?? null, day_node_id: value.day_node_id ?? null, items: (value.items ?? []).map(normalizeCapture) };
  },
  save: async (submission: DailyCaptureSubmission): Promise<DailyCapture> => normalizeCapture(await apiFetch<DailyCaptureWire>(endpoint, {
    method: "POST", body: JSON.stringify(submission),
  })),
  retry: async (id: string): Promise<DailyCapture> => normalizeCapture(await apiFetch<DailyCaptureWire>(`${endpoint}/${encodeURIComponent(id)}/retry`, { method: "POST" })),
  undo: async (id: string): Promise<DailyCapture> => normalizeCapture(await apiFetch<DailyCaptureWire>(`${endpoint}/${encodeURIComponent(id)}/undo`, { method: "POST" })),
};
