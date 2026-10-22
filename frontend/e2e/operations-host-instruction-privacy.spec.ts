import { expect, test } from "@playwright/test";
import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

// All review/model endpoints are intercepted. This exercises the real page without adopting host text.
for (const outcome of ["success", "failure"] as const) {
  test(`private preview cannot survive a live session switch with delayed ${outcome}`, async ({ page }) => {
    await addAuthCookie(page); await mockAuthenticatedApis(page);
    let sessionOwner: string | null = E2E_USER_ID;
    let release!: () => void; let requested!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    const previewRequested = new Promise<void>(resolve => { requested = resolve; });
    let approveCount = 0;
    await page.route("**/api/auth/status", route => route.fulfill({ json: sessionOwner
      ? { authenticated: true, user: { id: sessionOwner, role: "admin" } } : { authenticated: false } }));
    await page.route("**/api/python-proxy/api/local-agent/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/status")) return route.fulfill({ json: { enabled: true, running: true, model: "gpt-5.6-sol", allowed_roots: ["/fixture"], max_concurrency: 1, queued: 0, limits: {}, supports_task_routes: true, supported_providers: ["codex-cli"], default_route: { provider: "codex-cli", model: "gpt-5.6-sol", effort: "high" } } });
      if (path.endsWith("/tasks")) return route.fulfill({ json: { tasks: [] } });
      if (path.endsWith("/instruction-reviews/preview")) {
        requested(); await held;
        return route.fulfill(outcome === "failure" ? { status: 403, json: { detail: "old private error" } } : { json: {
          owner: E2E_USER_ID, nonce: "b".repeat(32), task_id: "a".repeat(32), cli_version: "0.159.2",
          task_fingerprint: "c".repeat(64), review_digest: "d".repeat(64), expires_at: Date.now() / 1000 + 300, execution_expires_at: Date.now() / 1000 + 420,
          sources: [{ path: "/fixture/AGENTS.md", exists: true, selected: true, size_bytes: 24, sha256: "e".repeat(64), text: "OWNER_A_PRIVATE_FIXTURE" }],
        } });
      }
      if (path.endsWith("/approve")) approveCount++;
      return route.fulfill({ status: 404, json: { detail: "fixture only" } });
    });
    await page.goto("/operations?tab=work");
    const panel = page.getByTestId("operations-local-tasks");
    await panel.getByLabel("作業フォルダー").fill("/fixture"); await panel.getByLabel("目的", { exact: true }).fill("Read fixture");
    await panel.getByRole("button", { name: "ホスト指示の全文を確認" }).click(); await previewRequested;
    sessionOwner = outcome === "success" ? "22222222-2222-4222-8222-222222222222" : null;
    release();
    await expect(panel.getByRole("alert")).toContainText("ログイン中の所有者を確認できません");
    await expect(panel.getByText("OWNER_A_PRIVATE_FIXTURE")).toHaveCount(0);
    await expect(panel.getByRole("checkbox")).toHaveCount(0);
    expect(approveCount).toBe(0);
  });
}
