import { describe, expect, it } from "vitest";
import { appHref, canonicalAppHref } from "@/lib/apps-link";

const appId = "123e4567-e89b-12d3-a456-426614174000";

describe("canonical App links", () => {
  it("builds a relative App URL with an optional Project query", () => {
    expect(appHref(appId)).toBe(`/apps/${appId}`);
    expect(appHref(appId, "project one")).toBe(`/apps/${appId}?project_id=project%20one`);
  });

  it("accepts only the canonical App path and project query", () => {
    expect(canonicalAppHref(`/apps/${appId}`)).toBe(`/apps/${appId}`);
    expect(canonicalAppHref(`/at/apps/${appId}`)).toBe(`/apps/${appId}`);
    expect(canonicalAppHref(`/apps/${appId}?project_id=project-1`)).toBe(`/apps/${appId}?project_id=project-1`);
    expect(canonicalAppHref(`/apps/${appId}?next=https://evil.test`)).toBeNull();
    expect(canonicalAppHref(`/apps/not-an-app`)).toBeNull();
    expect(canonicalAppHref(`https://example.test/apps/${appId}`)).toBeNull();
  });
});
