import { afterEach, describe, expect, it, vi } from "vitest";
import { mediaBrandPortfolioApi, portfolioReadOptions } from "./media-brand-portfolio-api";
afterEach(() => vi.unstubAllGlobals());

describe("portfolio read recovery", () => {
  it("bounds automatic retries to transport and server errors", () => {
    for (const status of [0, 500, 502, 503, 504]) expect(portfolioReadOptions.shouldRetryOnError({ status })).toBe(true);
    for (const status of [401, 403, 404, 409, 422, 429]) expect(portfolioReadOptions.shouldRetryOnError({ status })).toBe(false);
    expect(portfolioReadOptions.shouldRetryOnError(new TypeError("network failure"))).toBe(true);
    expect(portfolioReadOptions.shouldRetryOnError(new Error("invalid body"))).toBe(false);
    expect(portfolioReadOptions.errorRetryCount).toBe(3);
    expect(portfolioReadOptions.keepPreviousData).toBe(false);
  });
  it("rejects malformed 200 responses instead of exposing an empty or foreign setup", async () => {
    for (const body of [{}, { owner_user_id: "owner", status: "ready", source: { sha256: "a".repeat(64) }, brands: [{ definition: { brand_key: "brand", display_name: "Brand" }, project_id: "project", persona: { id: "persona" } }] }]) {
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
      await expect(mediaBrandPortfolioApi.get()).rejects.toMatchObject({ status: 502 });
    }
  });
});
