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
