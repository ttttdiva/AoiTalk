import { afterEach, describe, expect, it, vi } from "vitest";

import {
  mediaOperationsApi,
  type PersonaCreateInput,
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
