// @vitest-environment node
import { describe, expect, it } from "vitest";
import { GET } from "./route";

describe("asset-free OAuth callback relay", () => {
  it("clears the query before its authenticated POST and sends no external assets or referrer", async () => {
    const response = await GET(); const html = await response.text();
    expect(response.headers.get("Cache-Control")).toContain("no-store");
    expect(response.headers.get("Referrer-Policy")).toBe("no-referrer");
    const csp = response.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toMatch(/style-src 'nonce-[0-9a-f-]+'/);
    expect(html).not.toMatch(/<script[^>]*src=|<img|<link|url\(/);
    const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)?.[1];
    expect(script).toBeTruthy();
    expect(() => new Function(script!)).not.toThrow();
    expect(script!.indexOf("history.replaceState")).toBeLessThan(script!.indexOf("fetch("));
    expect(script).toContain("credentials:'include'");
    expect(script).toContain("referrerPolicy:'no-referrer'");
  });

  it("returns to the workspace at once and leaves the result for a toast", async () => {
    const script = (await (await GET()).text()).match(/<script[^>]*>([\s\S]*?)<\/script>/)?.[1] ?? "";
    expect(script).toContain('"aoitalk-media-oauth-result-v1"');
    const finish = script.slice(script.indexOf("const finish"), script.indexOf("};", script.indexOf("const finish")));
    expect(finish.indexOf("sessionStorage.setItem")).toBeGreaterThan(-1);
    expect(finish.indexOf("sessionStorage.setItem")).toBeLessThan(finish.indexOf("location.replace(link.href)"));
    // The result never carries the provider code or state.
    expect(finish).not.toMatch(/\bcode\b|\bstate\b/);
  });
});
