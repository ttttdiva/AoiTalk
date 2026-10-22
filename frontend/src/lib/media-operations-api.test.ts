import { afterEach, describe, expect, it, vi } from "vitest";

import {
  mediaOperationsApi,
  MediaOperationsApiError,
  type PersonaCreateInput,
  type RevenueCostRecordInput,
  type MediaCostEntryInput,
} from "@/lib/media-operations-api";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      "content-type": "application/json",
    },
  });
}

function emptySlots() {
  return Array.from({ length: 9 }, (_, index) => ({
    slot: index + 1,
    persona_id: null,
    persona: null,
  }));
}

describe("mediaOperationsApi", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts nullable costs with evidence and an idempotency key, then reads uncached history", async () => {
    const input: RevenueCostRecordInput = {
      currency: "USD", source: "manual", basis: "settled", fee_amount: 10,
      production_cost_amount: 0, human_cost_amount: null, human_minutes: null,
      missing_reasons: { human_cost_amount: "未確定", human_minutes: "未計測" },
      coverage_complete: false, coverage_note: "人的費用は未確認",
      evidence: [{ kind: "revenue_event", ref: "event/id", note: "手数料の明細" }],
      correction_of_id: "previous-cost",
    };
    const fetchMock = vi.fn().mockResolvedValueOnce(response({ id: "new-cost" })).mockResolvedValueOnce(response({ items: [{ id: "new-cost" }], effective: { id: "new-cost" }, has_more: false }));
    vi.stubGlobal("fetch", fetchMock);
    await mediaOperationsApi.createRevenueCostRecord("event/id", input, "cost-key");
    const readback = await mediaOperationsApi.getRevenueCostRecords("event/id");
    expect(readback.effective?.id).toBe("new-cost");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/python-proxy/operations/media/revenue-events/event%2Fid/cost-records");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("idempotency-key")).toBe("cost-key");
    expect(JSON.parse(init.body)).toEqual(input);
    expect(fetchMock.mock.calls[1]).toEqual([url, expect.objectContaining({ cache: "no-store", credentials: "include" })]);
  });

  it("preserves a correction conflict as a typed API error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "latest cost changed" }), { status: 409, headers: { "content-type": "application/json" } })));
    await expect(mediaOperationsApi.createRevenueCostRecord("event-1", {
      currency: "USD", source: "manual", basis: "settled", fee_amount: 10,
      production_cost_amount: 20, human_cost_amount: 5, human_minutes: 30,
      missing_reasons: {}, coverage_complete: true, coverage_note: "確認済み",
      evidence: [{ kind: "revenue_event", ref: "event-1", note: "明細確認" }], correction_of_id: "old-cost",
    }, "correction-key")).rejects.toEqual(expect.objectContaining({ status: 409, detail: "latest cost changed", name: "MediaOperationsApiError" }));
    expect(new MediaOperationsApiError("conflict", { status: 409 })).toBeInstanceOf(Error);
  });

  it("scopes independent costs to the Persona and supports append, history, and whole-entry allocation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response([]));
    vi.stubGlobal("fetch", fetchMock);
    await mediaOperationsApi.listCostEntries("persona/id", "project/id", 100);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/python-proxy/operations/media/cost-entries?persona_ref=persona%2Fid&limit=100&offset=100&project_id=project%2Fid");
    const input: MediaCostEntryInput = {
      persona_ref: "persona/id", project_id: "project/id", content_ref: null,
      currency: "USD", source: "manual", basis: "estimated",
      window_start: "2026-10-01T00:00:00Z", window_end: "2026-10-31T23:59:00Z",
      fee_amount: 0, production_cost_amount: 20, human_cost_amount: null, human_minutes: 30,
      missing_reasons: { human_cost_amount: "未確定" }, coverage_complete: false, coverage_note: "売上前の制作費用",
      evidence: [{ kind: "record", ref: "persona/id", note: "制作メモ" }], correction_of_id: null,
    };
    await mediaOperationsApi.createCostEntry(input, "entry-key");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/python-proxy/operations/media/cost-entries");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual(input);
    expect(new Headers(fetchMock.mock.calls[1][1].headers).get("idempotency-key")).toBe("entry-key");
    await mediaOperationsApi.getCostEntryHistory("entry/id");
    expect(fetchMock.mock.calls[2][0]).toBe("/api/python-proxy/operations/media/cost-entries/entry%2Fid/history");
    await mediaOperationsApi.allocateCostEntry("event/id", "revision/id", { correction_of_id: "old-cost" }, "allocate-key");
    expect(fetchMock.mock.calls[3][0]).toBe("/api/python-proxy/operations/media/revenue-events/event%2Fid/cost-entries/revision%2Fid/allocate");
    expect(fetchMock.mock.calls[3][1].method).toBe("POST");
    expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({ correction_of_id: "old-cost" });
    expect(new Headers(fetchMock.mock.calls[3][1].headers).get("idempotency-key")).toBe("allocate-key");
  });

  it("reads the fixed nine-slot intake through the Python proxy", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      response({
        project_id: null,
        slots: emptySlots(),
      }),
    );

    vi.stubGlobal("fetch", fetchMock);

    const result = await mediaOperationsApi.getPersonaIntake();

    expect(result.slots).toHaveLength(9);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const [url, init] = fetchMock.mock.calls[0];

    expect(url).toBe("/api/python-proxy/operations/media/persona-intake");
    expect(init).toEqual(
      expect.objectContaining({
        cache: "no-store",
        credentials: "include",
      }),
    );
  });

  it("sends explicit Persona input and Idempotency-Key", async () => {
    const detail = {
      id: "persona-id",
      owner_user_id: "owner-id",
      project_id: null,
      parent_brand_ref: null,
      state: "draft" as const,
      create_hash: "a".repeat(64),
      created_by: "owner-id",
      created_at: "2026-09-01T00:00:00",
      current_revision: {
        id: "revision-id",
        persona_id: "persona-id",
        owner_user_id: "owner-id",
        project_id: null,
        version: 1,
        display_name: "test-persona",
        summary: null,
        voice: null,
        audience: null,
        adult_policy: null,
        allowed_subjects: [],
        creative_direction: null,
        default_language: null,
        disclosure_policy: null,
        image_production_policy: {},
        ip_policy: null,
        kpi_objectives: [],
        locale: null,
        monetization_policy: {},
        niche: null,
        positioning: null,
        prohibited_subjects: [],
        public_aliases: [],
        research_policy: {},
        sensitive_policy: null,
        timezone: null,
        video_production_policy: {},
        visual_identity: {},
        platforms: ["x"],
        content_pillars: [],
        content_hash: "b".repeat(64),
        created_by: "owner-id",
        created_at: "2026-09-01T00:00:00",
      },
      revisions: [],
      revision_history_truncated: false,
    };

    const fetchMock = vi.fn().mockResolvedValue(response(detail));

    vi.stubGlobal("fetch", fetchMock);

    const input: PersonaCreateInput = {
      intake_slot: 1,
      display_name: "test-persona",
      summary: null,
      voice: null,
      audience: null,
      platforms: ["x"],
      content_pillars: [],
    };

    await mediaOperationsApi.createPersona(input, "idempotency-test");

    const [url, init] = fetchMock.mock.calls[0];

    expect(url).toBe("/api/python-proxy/operations/media/personas");
    expect(init.method).toBe("POST");

    const headers = new Headers(init.headers);
    expect(headers.get("idempotency-key")).toBe("idempotency-test");

    expect(JSON.parse(String(init.body))).toEqual(input);
  });

  it("supports typed Character search pagination", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response([]));
    vi.stubGlobal("fetch", fetchMock);

    await mediaOperationsApi.listCharacters({
      search: "  Aoi  ",
      limit: 100,
      offset: 100,
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/python-proxy/operations/media/characters?search=Aoi&limit=100&offset=100",
    );
  });

  it("sends sparse Character PATCH fields with all revision tokens", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ id: "character-id" }));
    vi.stubGlobal("fetch", fetchMock);

    await mediaOperationsApi.patchCharacter(
      "character-id",
      {
        expected_revision_id: "revision-id",
        expected_revision_version: 2,
        expected_revision_content_hash: "a".repeat(64),
        display_name: "Aoi",
        summary: null,
        content_pillars: [],
      },
      "patch-key",
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/python-proxy/operations/media/characters/character-id");
    expect(init.method).toBe("PATCH");
    expect(new Headers(init.headers).get("idempotency-key")).toBe("patch-key");
    expect(JSON.parse(String(init.body))).toEqual({
      expected_revision_id: "revision-id",
      expected_revision_version: 2,
      expected_revision_content_hash: "a".repeat(64),
      display_name: "Aoi",
      summary: null,
      content_pillars: [],
    });
  });
});
