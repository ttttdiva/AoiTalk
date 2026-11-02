import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { getTransferFixtureBundleFile } from "./fixtures/transfer-fixture-paths.cjs";

const FIXTURE_ORIGIN = "http://restart-fixture.test";

type RestartFixtureOptions = {
  heldPost?: number;
  accepted?: boolean;
};

let restartBundle: string;

test.beforeAll(async () => {
  restartBundle = await readFile(getTransferFixtureBundleFile("admin-restart-entry.js"), "utf8");
});

function fixturePage(): string {
  return `<!doctype html>
    <html lang="ja"><meta charset="utf-8"><title>Restart fixture</title>
    <main>
      <button type="button" data-restart-once>Request restart</button>
      <button type="button" data-restart-twice>Request restart twice</button>
      <button type="button" data-replace-screen>Open next screen</button>
      <p data-new-screen hidden>Fixture next screen</p>
    </main>
    <script type="module" src="/restart.js"></script></html>`;
}

async function installRestartFixture(page: Page, options: RestartFixtureOptions = {}) {
  let restartPosts = 0;
  let healthRequests = 0;
  let releaseHeldPost: (() => void) | null = null;
  let markHeldPostStarted!: () => void;
  const heldPostStarted = new Promise<void>((resolve) => {
    markHeldPostStarted = resolve;
  });

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.hostname !== "restart-fixture.test") {
      await route.abort("blockedbyclient");
      return;
    }
    if (url.pathname === "/") {
      await route.fulfill({ contentType: "text/html; charset=utf-8", body: fixturePage() });
      return;
    }
    if (url.pathname === "/restart.js") {
      await route.fulfill({ contentType: "text/javascript; charset=utf-8", body: restartBundle });
      return;
    }
    if (url.pathname === "/api/python-proxy/health") {
      healthRequests += 1;
      await route.fulfill({ json: { status: "ok", boot_id: "fixture-boot-old" } });
      return;
    }
    if (url.pathname === "/api/python-proxy/admin/restart") {
      restartPosts += 1;
      if (options.heldPost === restartPosts) {
        markHeldPostStarted();
        await new Promise<void>((resolve) => {
          releaseHeldPost = resolve;
        });
      }
      if (options.accepted) {
        await route.fulfill({ status: 200, json: { accepted: true } });
      } else {
        await route.fulfill({ status: 409, json: { error: "operation_busy" } });
      }
      return;
    }
    await route.fulfill({ status: 404, contentType: "text/plain", body: "fixture route not found" });
  });

  await page.goto(FIXTURE_ORIGIN);
  return {
    get restartPosts() { return restartPosts; },
    get healthRequests() { return healthRequests; },
    heldPostStarted,
    releaseHeldPost() { releaseHeldPost?.(); },
  };
}

test.describe("restart拒否overlayの実ブラウザfixture", () => {
  test("busy拒否からCloseで戻り、追加POSTを発生させない", async ({ page }) => {
    const fixture = await installRestartFixture(page);
    await page.getByRole("button", { name: "Request restart", exact: true }).click();

    const dialog = page.getByRole("dialog", { name: "管理サーバーの再起動" });
    await expect(dialog.getByRole("status")).toContainText("会話またはエージェント処理中");
    await dialog.getByRole("button", { name: "閉じる" }).click();

    await expect(dialog).toHaveCount(0);
    expect(fixture.restartPosts).toBe(1);
    expect(fixture.healthRequests).toBe(1);
  });

  test("retry二重click中のold responseは次画面を覆わず、POSTは一件だけ", async ({ page }) => {
    const fixture = await installRestartFixture(page, { heldPost: 2 });
    await page.getByRole("button", { name: "Request restart", exact: true }).click();

    const dialog = page.getByRole("dialog", { name: "管理サーバーの再起動" });
    await expect(dialog.getByRole("button", { name: "再起動を再試行" })).toBeVisible();
    await dialog.getByRole("button", { name: "再起動を再試行" }).click();
    await fixture.heldPostStarted;

    await page.evaluate(() => {
      const retry = document.querySelector<HTMLButtonElement>("[data-restart-retry]");
      retry?.click();
      retry?.click();
      document.querySelector<HTMLButtonElement>("[data-restart-twice]")?.click();
    });
    expect(fixture.restartPosts).toBe(2);

    await page.evaluate(() => {
      document.querySelector<HTMLButtonElement>("[data-replace-screen]")?.click();
    });
    await expect(page.getByText("Fixture next screen")).toBeVisible();
    fixture.releaseHeldPost();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByText("Fixture next screen")).toBeVisible();
    expect(fixture.restartPosts).toBe(2);
  });

  test("timeout後にCloseでき、再試行は発生しない", async ({ page }) => {
    await page.clock.install({ time: new Date("2026-10-26T00:00:00Z") });
    const fixture = await installRestartFixture(page, { accepted: true });
    await page.getByRole("button", { name: "Request restart", exact: true }).click();

    const dialog = page.getByRole("dialog", { name: "管理サーバーの再起動" });
    await expect(dialog.getByRole("status")).toContainText("サービスの起動完了を確認しています");
    await page.clock.runFor(300_000);
    await expect(dialog.getByRole("status")).toContainText("300秒以内に復帰を確認できませんでした");
    await dialog.getByRole("button", { name: "閉じる" }).click();

    await expect(dialog).toHaveCount(0);
    expect(fixture.restartPosts).toBe(1);
    expect(fixture.healthRequests).toBeGreaterThan(1);
  });
});
