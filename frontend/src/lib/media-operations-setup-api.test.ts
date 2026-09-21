import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  mediaOperationsSetupApi,
} from "@/lib/media-operations-setup-api";


function response(
  body: unknown,
): Response {
  return new Response(
    JSON.stringify(body),
    {
      status: 200,
      headers: {
        "content-type":
          "application/json",
      },
    },
  );
}

describe(
  "mediaOperationsSetupApi",
  () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it("imports bulk draft with idempotency header", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValue(
          response({
            id: "draft-id",
          }),
        );

      vi.stubGlobal(
        "fetch",
        fetchMock,
      );

      await mediaOperationsSetupApi.importPersonaBulkDraft(
        {
          project_id: null,
          slots: [],
        } as never,
        "draft-key",
      );

      const [
        url,
        init,
      ] =
        fetchMock.mock.calls[0];

      expect(url).toBe(
        "/api/python-proxy/operations/media/persona-drafts",
      );
      expect(
        init.method,
      ).toBe("POST");

      const headers =
        new Headers(
          init.headers,
        );

      expect(
        headers.get(
          "idempotency-key",
        ),
      ).toBe("draft-key");
    });

    it("uses PUT for correction and POST for atomic apply", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValue(
          response({}),
        );

      vi.stubGlobal(
        "fetch",
        fetchMock,
      );

      await mediaOperationsSetupApi.correctPersonaBulkDraft(
        "draft-id",
        {
          expected_version: 3,
          slots: [],
        } as never,
      );

      await mediaOperationsSetupApi.applyPersonaBulkDraft(
        "draft-id",
        3,
        "apply-key",
      );

      expect(
        fetchMock.mock.calls[0][0],
      ).toBe(
        "/api/python-proxy/operations/media/persona-drafts/draft-id",
      );
      expect(
        fetchMock.mock.calls[0][1]
          .method,
      ).toBe("PUT");

      expect(
        fetchMock.mock.calls[1][0],
      ).toBe(
        "/api/python-proxy/operations/media/persona-drafts/draft-id/apply",
      );
      expect(
        fetchMock.mock.calls[1][1]
          .method,
      ).toBe("POST");

      const headers =
        new Headers(
          fetchMock.mock.calls[1][1]
            .headers,
        );

      expect(
        headers.get(
          "idempotency-key",
        ),
      ).toBe("apply-key");
    });

    it("creates PlatformAccount without any credential payload helper", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValue(
          response({
            id: "account-id",
          }),
        );

      vi.stubGlobal(
        "fetch",
        fetchMock,
      );

      await mediaOperationsSetupApi.createPlatformAccount(
        {
          platform: "x",
          account_ref: "test-ref",
          display_name:
            "Test account",
          project_id: null,
        },
        "account-key",
      );

      const body = JSON.parse(
        String(
          fetchMock.mock.calls[0][1]
            .body,
        ),
      );

      expect(body).toEqual({
        platform: "x",
        account_ref:
          "test-ref",
        display_name:
            "Test account",
        project_id: null,
      });

      expect(
        JSON.stringify(
          body,
        ),
      ).not.toContain(
        "credential_ref",
      );
    });

    it("uses the canonical OpenAPI file field for credential uploads", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        response({
          id: "credential-id",
        }),
      );
      vi.stubGlobal("fetch", fetchMock);

      const file = new File(
        ['{"access_token":"redacted-in-test"}'],
        "credential.json",
        { type: "application/json" },
      );
      await mediaOperationsSetupApi.createPlatformCredential(
        "account-id",
        {
          package: file,
          connection_type: "oauth",
        },
        "credential-key",
      );

      const form = fetchMock.mock.calls[0][1].body as FormData;
      expect(Array.from(form.keys())).toContain("file");
      expect(Array.from(form.keys())).not.toContain("package");
    });

    it("does not retain provider error bodies or secret-looking details", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ detail: "token=super-secret-value" }),
          {
            status: 422,
            headers: { "content-type": "application/json" },
          },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);

      await expect(
        mediaOperationsSetupApi.getPlatformCredential("account-id"),
      ).rejects.toMatchObject({
        status: 422,
        detail: undefined,
        body: undefined,
        message: "入力を確認してください。",
      });
    });
  },
);
