// @vitest-environment node
import { describe, expect, it } from "vitest";
import { GET } from "./route";

describe("asset-free OAuth callback relay", () => {
  it("clears the query before its authenticated POST and sends no external assets or referrer", async () => {
    const response = await GET(); const html = await response.text();
    expect(response.headers.get("Cache-Control")).toContain("no-store");
    expect(response.headers.get("Referrer-Policy")).toBe("no-referrer");
    expect(response.headers.get("Content-Security-Policy")).toContain("connect-src 'self'");
    expect(html).not.toMatch(/<script[^>]*src=|<img|<link/);
    const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)?.[1];
    expect(script).toBeTruthy();
    expect(() => new Function(script!)).not.toThrow();
    expect(script!.indexOf("history.replaceState")).toBeLessThan(script!.indexOf("fetch("));
    expect(script).toContain("credentials:'include'");
    expect(script).toContain("referrerPolicy:'no-referrer'");
  });
});
