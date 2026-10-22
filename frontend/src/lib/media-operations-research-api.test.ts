import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  mediaResearchApi,
} from "@/lib/media-operations-research-api";


function response(
  body: unknown,
): Response {
  return new Response(
    JSON.stringify(body),
    {
      status: 200,
      headers: {
        "content-type": "application/json",
      },
    },
  );
}

describe("mediaResearchApi", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("requests bounded ContentItem pages without changing legacy program filtering", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response([]));
    vi.stubGlobal("fetch", fetchMock);
    await mediaResearchApi.listContentItems("program-id", { limit: 100, offset: 100 });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/python-proxy/operations/media/content-items?editorial_program_id=program-id&limit=100&offset=100");
    await mediaResearchApi.listContentItems("program-id");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/python-proxy/operations/media/content-items?editorial_program_id=program-id");
  });

  it("sends an original brief without synthesizing research and retains the raw text", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ id: "original-item" }));
    vi.stubGlobal("fetch", fetchMock);
    const brief = "  人の原文\r\n確認は未実施。\n  ";
    await mediaResearchApi.createContentItem({
      editorial_program_id: "program-id", input_kind: "user_authored",
      title: "Original lesson", brief, persona_revision_id: "revision-id",
    }, "original-key");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/python-proxy/operations/media/content-items");
    expect(new Headers(init.headers).get("idempotency-key")).toBe("original-key");
    expect(JSON.parse(String(init.body))).toEqual({
      editorial_program_id: "program-id", input_kind: "user_authored",
      title: "Original lesson", brief, persona_revision_id: "revision-id",
    });
  });

  it("starts a pinned ResearchRun with idempotency", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        response({
          id: "run-id",
        }),
      );

    vi.stubGlobal(
      "fetch",
      fetchMock,
    );

    await mediaResearchApi.startRun(
      {
        research_routine_id: "routine-id",
        routine_version: 3,
        focus_note: null,
      },
      "run-key",
    );

    const [url, init] =
      fetchMock.mock.calls[0];

    expect(url).toBe(
      "/api/python-proxy/operations/media/research-runs",
    );
    expect(init.method).toBe("POST");

    const headers =
      new Headers(init.headers);

    expect(
      headers.get("idempotency-key"),
    ).toBe("run-key");

    expect(
      JSON.parse(
        String(init.body),
      ),
    ).toEqual({
      research_routine_id: "routine-id",
      routine_version: 3,
      focus_note: null,
    });
  });

  it("sends evidence as part of the immutable Finding create", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        response({
          id: "finding-id",
        }),
      );

    vi.stubGlobal(
      "fetch",
      fetchMock,
    );

    await mediaResearchApi.appendFinding(
      "run-id",
      {
        kind: "fact",
        statement: "statement",
        evidence: [
          {
            type: "url",
            url: "https://example.invalid/source",
            label: null,
            note: null,
          },
        ],
      },
      "finding-key",
    );

    expect(
      fetchMock.mock.calls[0][0],
    ).toBe(
      "/api/python-proxy/operations/media/research-runs/run-id/findings",
    );

    expect(
      JSON.parse(
        String(
          fetchMock.mock.calls[0][1]
            .body,
        ),
      ),
    ).toEqual({
      kind: "fact",
      statement: "statement",
      evidence: [
        {
          type: "url",
          url: "https://example.invalid/source",
          label: null,
          note: null,
        },
      ],
    });
  });

  it("creates ContentItem with explicit Finding IDs only", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        response({
          id: "item-id",
        }),
      );

    vi.stubGlobal(
      "fetch",
      fetchMock,
    );

    await mediaResearchApi.createContentItem(
      {
        editorial_program_id: "program-id",
        title: "title",
        brief: "brief",
        finding_ids: [
          "finding-id",
        ],
      },
      "content-key",
    );

    expect(
      JSON.parse(
        String(
          fetchMock.mock.calls[0][1]
            .body,
        ),
      ),
    ).toEqual({
      editorial_program_id: "program-id",
      title: "title",
      brief: "brief",
      finding_ids: [
        "finding-id",
      ],
    });
  });
});
