import { test, expect } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("ナビゲーション", () => {
  test("ルート（/）にアクセスすると /chat にリダイレクトされる", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/");
    await expect(page).toHaveURL(/\/chat/);
  });

  test("Global Railのナビリンクで各ページに遷移できる", async ({ page }) => {
    test.setTimeout(120_000);
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");

    const globalNavigation = page.getByRole("navigation", { name: "Workspace" });
    const tabLabels = await globalNavigation.getByRole("link").evaluateAll((links) =>
      links.map((link) => link.getAttribute("aria-label")),
    );
    expect(tabLabels.slice(0, 5)).toEqual([
      "チャット",
      "タスク",
      "カレンダー",
      "Docs",
      "Files",
    ]);
    await expect(globalNavigation.getByRole("link", { name: "Apps" })).toBeVisible();

    // Global RailのLinkがroute stateとworkspace titleを更新する。
    const navTabs = [
      { label: "タスク", path: "/tasks", title: "タスク" },
      { label: "カレンダー", path: "/calendar", title: "カレンダー" },
      { label: "Docs", path: "/docs", title: "Docs" },
      { label: "Files", path: "/filer", title: "Files" },
      { label: "プロジェクト", path: "/projects", title: "プロジェクト" },
      { label: "シナリオ", path: "/scenarios", title: "Story" },
      { label: "TRPG", path: "/trpg", title: "TRPG" },
      { label: "設定", path: "/settings", title: "設定" },
      { label: "チャット", path: "/chat", title: "チャット" },
    ];

    for (const { label, path, title } of navTabs) {
      const link = globalNavigation.getByRole("link", { name: label, exact: true });
      await expect(link).toBeVisible();
      await expect(link).toHaveAttribute("href", path);
      await link.click();
      await expect(
        page.locator('[data-shell-region="workspace-title"]'),
      ).toContainText(title, { timeout: 15_000 });
    }
  });

  test("Alt+4でDocsに遷移できる", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");

    await page.keyboard.press("Alt+4");
    await expect(page).toHaveURL(/\/docs/);
  });

  test("設定のチェックで任意タブを表示・非表示にできる", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);

    let navigationTabs = {
      scenarios: true,
      trpg: true,
    };
    await page.route("**/api/users/me/settings", async (route) => {
      if (route.request().method() === "PATCH") {
        const patch = route.request().postDataJSON() as {
          navigation_tabs?: Partial<typeof navigationTabs>;
        };
        navigationTabs = {
          ...navigationTabs,
          ...patch.navigation_tabs,
        };
      }
      await route.fulfill({
        json: { settings: { navigation_tabs: navigationTabs } },
      });
    });

    await page.goto("/settings");
    const globalNavigation = page.getByRole("navigation", { name: "Workspace" });
    const scenarioTab = globalNavigation.getByRole("link", { name: "シナリオ" });
    const trpgTab = globalNavigation.getByRole("link", { name: "TRPG" });
    await expect(scenarioTab).toBeVisible();
    await expect(trpgTab).toBeVisible();

    await page.getByRole("button", { name: "タブ表示" }).click();
    const scenarioCheckbox = page.getByRole("checkbox", {
      name: "シナリオタブを表示",
    });
    const trpgCheckbox = page.getByRole("checkbox", {
      name: "TRPGタブを表示",
    });
    await scenarioCheckbox.click();
    await expect(scenarioTab).toHaveCount(0);
    await expect(trpgTab).toBeVisible();

    await trpgCheckbox.click();
    await expect(trpgTab).toHaveCount(0);

    await page.goto("/settings");
    await page.getByRole("button", { name: "タブ表示" }).click();
    await page.getByRole("checkbox", { name: "シナリオタブを表示" }).click();
    await page.getByRole("checkbox", { name: "TRPGタブを表示" }).click();
    await expect(scenarioTab).toBeVisible();
    await expect(trpgTab).toBeVisible();
  });

  test("timer-changedイベントでヘッダータイマーが即時更新される", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");
    await page.getByRole("link", { name: "Docs" }).waitFor();
    await page.waitForTimeout(500);

    await page.evaluate(() => {
      const started = new Date(Date.now() - 3500);
      const pad = (value: number) => String(value).padStart(2, "0");
      const startedAt = `${started.getFullYear()}-${pad(started.getMonth() + 1)}-${pad(started.getDate())}T${pad(started.getHours())}:${pad(started.getMinutes())}:${pad(started.getSeconds())}`;
      window.dispatchEvent(
        new CustomEvent("timer-changed", {
          detail: {
            activeEntry: {
              id: "timer-e2e",
              task_id: "task-e2e",
              user_id: "user-1",
              source: "timer",
              started_at: startedAt,
              task_title: "E2E timer",
            },
          },
        }),
      );
    });

    await expect(page.locator("header")).toContainText("E2E timer");
    await expect(page.locator("header")).toContainText(/00:00:0[3-9]/);
  });

  test("320pxと375pxでアクティブタイマー表示中もヘッダーが横溢れせずタイトル幅を保つ", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);

    for (const width of [320, 375]) {
      await page.setViewportSize({ width, height: 720 });
      await page.goto("/chat");
      await expect(
        page.getByRole("button", { name: "通知を開く" }),
      ).toBeVisible();
      await page.evaluate(() => {
        window.dispatchEvent(
          new CustomEvent("timer-changed", {
            detail: {
              activeEntry: {
                id: "responsive-timer-e2e",
                task_id: "task-e2e",
                user_id: "user-1",
                source: "timer",
                started_at: new Date(Date.now() - 3500).toISOString(),
                task_title: "幅の長いタイマータスク名",
              },
            },
          }),
        );
      });

      const header = page.locator('[data-shell-region="global-context"]');
      const title = page.locator('[data-shell-region="workspace-title"]');
      await expect(page.locator('[data-shell-region="timer"]')).toBeVisible();
      await expect(title).toContainText("チャット");
      const metrics = await header.evaluate((element) => {
        const titleElement = element.querySelector<HTMLElement>(
          '[data-shell-region="workspace-title"]',
        );
        return {
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
          titleWidth: titleElement?.getBoundingClientRect().width ?? 0,
        };
      });
      expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
      expect(metrics.titleWidth).toBeGreaterThan(0);
    }
  });

  test("サイドバーにナビリンクが表示される", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");

    const globalNavigation = page.getByRole("navigation", { name: "Workspace" });
    const sidebarLinks = ["チャット", "タスク"];
    for (const label of sidebarLinks) {
      await expect(
        globalNavigation.getByRole("link", { name: label, exact: true }),
      ).toBeVisible();
    }
  });
});
