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
      { label: "Operations", path: "/operations", title: "Operations" },
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

  test("SpaceとProject選択中も狭幅とdesktopのヘッダーを保ち今日のメモを開ける", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);

    // Empty shared fixtures hide the real Space selector and miss its width.
    // Both Spaces need participating Projects to survive provider filtering.
    const spaces = [
      { id: "space-inbox", name: "Inbox", color: "#2563eb" },
      { id: "space-long", name: "長い名前のスペースでもヘッダーを圧迫しない", color: "#16a34a" },
    ];
    const projects = spaces.map((space) => ({
      id: `project-${space.id}`, name: `${space.name} Project`,
      space_id: space.id, is_participating: true, is_completed: false,
    }));
    await page.route("**/api/spaces", (route) => route.fulfill({ json: { spaces, total: spaces.length } }));
    await page.route("**/api/projects", (route) => route.fulfill({ json: { projects, total: projects.length } }));

    await page.route("**/api/python-proxy/docs/daily-captures?*", async (route) => {
      const day = new URL(route.request().url()).searchParams.get("day");
      await route.fulfill({ json: { day, docs_library_id: "personal-e2e", library_name: "Personal Library", day_node_id: null, items: [] } });
    });

    for (const width of [320, 375, 1280]) {
      await page.setViewportSize({ width, height: 720 });
      await page.goto("/chat");
      const spaceSelector = page.getByRole("combobox", { name: "スペース選択", exact: true });
      await expect(spaceSelector).toContainText("Inbox");
      if (width >= 768) {
        await expect(page.getByRole("combobox", { name: "プロジェクト選択", exact: true })).toContainText("Inbox Project");
      } else {
        await expect(page.getByRole("button", { name: "通知を開く" })).toBeVisible();
      }
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
      const assertHeaderFits = async () => {
        const metrics = await header.evaluate((element) => {
          const bounds = element.getBoundingClientRect();
          const titleElement = element.querySelector<HTMLElement>('[data-shell-region="workspace-title"]');
          const avatar = element.querySelector<HTMLElement>('[aria-label="ユーザーメニューを開く"]');
          const titleBounds = titleElement?.getBoundingClientRect();
          const avatarBounds = avatar?.getBoundingClientRect();
          return {
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
            titleWidth: titleBounds?.width ?? 0,
            avatarRight: avatarBounds?.right ?? Infinity,
            headerRight: bounds.right,
          };
        });
        expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
        // A nonzero, but unreadable sliver is not a useful workspace title.
        expect(metrics.titleWidth).toBeGreaterThanOrEqual(48);
        expect(metrics.avatarRight).toBeLessThanOrEqual(metrics.headerRight);
      };
      await assertHeaderFits();
      const selectedSpaceName = header.locator('[data-shell-region="selected-space-name"]');
      const inboxMetrics = await selectedSpaceName.evaluate((element) => ({
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        renderedWidth: element.getBoundingClientRect().width,
        display: getComputedStyle(element).display,
        font: getComputedStyle(element).font,
      }));
      expect(inboxMetrics.scrollWidth, `Inbox must remain readable at ${width}px: ${JSON.stringify(inboxMetrics)}`)
        .toBeLessThanOrEqual(inboxMetrics.clientWidth);
      await spaceSelector.click();
      await page.getByRole("option", { name: spaces[1].name, exact: true }).click();
      await expect(spaceSelector).toContainText(spaces[1].name);
      await assertHeaderFits();
      await spaceSelector.click();
      await page.getByRole("option", { name: "Inbox", exact: true }).click();
      await expect(spaceSelector).toContainText("Inbox");
      await assertHeaderFits();

      const openDaily = async () => {
        if (width < 768) {
          await expect(header.getByRole("button", { name: "今日のメモを開く" })).toBeHidden();
          await page.getByRole("button", { name: "ユーザーメニューを開く" }).click();
          await page.getByRole("menuitem", { name: "今日のメモを開く", exact: true }).click();
        } else {
          await header.getByRole("button", { name: "今日のメモを開く" }).click();
        }
      };
      await openDaily();
      const dailyDialog = page.getByRole("dialog", { name: "Today · 今日のメモ" });
      await expect(dailyDialog).toBeVisible();
      const dailyInput = dailyDialog.getByRole("textbox", { name: "今日のメモに追記" });
      await expect(dailyInput).toBeFocused();
      await dailyInput.fill(`mobile draft ${width}`);
      await dailyDialog.getByRole("button", { name: "Close", exact: true }).click();
      await expect(dailyDialog).toBeHidden();
      await openDaily();
      await expect(dailyInput).toHaveValue(`mobile draft ${width}`);
      await dailyInput.fill("");
      await dailyDialog.getByRole("button", { name: "Close", exact: true }).click();
      await page.keyboard.press("Control+Alt+D");
      await expect(dailyDialog).toBeVisible();
      await expect(dailyInput).toBeFocused();
      await dailyDialog.getByRole("button", { name: "Close", exact: true }).click();
      await assertHeaderFits();
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
