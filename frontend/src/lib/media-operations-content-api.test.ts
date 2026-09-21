import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mediaContentApi } from "@/lib/media-operations-content-api";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const payload = {
  type: "x_post" as const,
  text: "hello",
};

describe("mediaContentApi", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates a typed variant with explicit idempotency and no raw JSON editor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ id: "variant-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await mediaContentApi.createVariant(
      {
        content_item_id: "item-1",
        persona_revision_id: "persona-revision-1",
        platform_account_id: "account-1",
        platform: "x",
        payload,
        generation_output_refs: [],
        source_evidence: [{ type: "url", url: "https://example.test", label: null, note: null }],
      },
      "variant-key",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/python-proxy/operations/media/content-variants",
      expect.objectContaining({ method: "POST" }),
    );
    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init.headers).get("idempotency-key")).toBe("variant-key");
    expect(JSON.parse(String(init.body))).toMatchObject({
      content_item_id: "item-1",
      platform: "x",
      payload,
    });
    expect(JSON.stringify(JSON.parse(String(init.body)))).not.toContain("credential");
  });

  it("pins QA and Rights assessments to an immutable revision", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ id: "assessment-1" }));
    vi.stubGlobal("fetch", fetchMock);

    await mediaContentApi.recordQa(
      {
        variant_revision_id: "revision-1",
        policy_revision_id: "policy-1",
        policy_revision_hash: "a".repeat(64),
        result: "review_required",
        checks: [{ code: "alt text", status: "not_run", mandatory: true, message: null }],
        findings: [],
        evidence: [{ type: "url", url: "https://example.test/evidence", label: null, note: null }],
      },
      "qa-key",
    );
    await mediaContentApi.recordRights(
      {
        variant_revision_id: "revision-1",
        result: "blocked",
        checks: [{ code: "license", status: "failed", mandatory: true, message: "missing license" }],
        findings: [{ code: "license", severity: "error", message: "missing license" }],
        evidence: [],
      },
      "rights-key",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/python-proxy/operations/media/content-variant-revisions/revision-1/qa",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/python-proxy/operations/media/content-variant-revisions/revision-1/rights",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual(
      expect.objectContaining({ result: "review_required" }),
    );
  });
});
