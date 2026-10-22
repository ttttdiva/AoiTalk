import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("設定セクションの初期表示", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
  });

  test("主要な設定本文を閉じて表示し、見出し操作で展開できる", async ({
    page,
  }) => {
    let yomiStatusRequests = 0;
    await page.route("**/api/python-proxy/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/python-proxy/settings") {
        await route.fulfill({
          json: {
            settings: {
              agents: {
                filesystem: { enabled: true },
                project_management: { enabled: false },
                mcp: { enabled: true },
              },
              tts: {
                yomi_linter: {
                  enabled: true,
                  confidence_threshold: 0.7,
                  model_id: "test/yomi-linter",
                  device: "cpu",
                  quantization: "int8",
                  log_detections: true,
                },
              },
            },
          },
        });
        return;
      }
      if (url.pathname === "/api/python-proxy/tts/yomi-linter/status") {
        yomiStatusRequests += 1;
        await route.fulfill({
          json: { model_loaded: true, download_status: "ready" },
        });
        return;
      }
      if (url.pathname === "/api/python-proxy/tts/yomi-dictionary") {
        await route.fulfill({ json: { items: [] } });
        return;
      }
      if (url.pathname === "/api/python-proxy/tts/yomi-candidates") {
        await route.fulfill({
          json: {
            items: [
              {
                id: "candidate-1",
                detected_text: "候補テスト",
                confidence: 0.9,
                tts_engine: "voicevox",
                original_text: "候補テストを含む原文",
                occurrence_count: 1,
              },
            ],
          },
        });
        return;
      }
      if (url.pathname === "/api/python-proxy/runtime/features") {
        await route.fulfill({
          json: {
            features: {},
            application_features: { entertainment: true },
          },
        });
        return;
      }
      await route.fallback();
    });
    await page.goto("/settings");
    expect(yomiStatusRequests).toBe(0);
    await expect(
      page
        .getByRole("navigation", { name: "設定カテゴリ" })
        .locator('[data-settings-target="admin"]'),
    ).toBeVisible();
    await expect(
      page.locator(
        '[data-settings-disclosure="true"][data-settings-target="pet"]',
      ),
    ).toBeVisible();

    const cases = [
      {
        targetId: "yomi-linter",
        title: "誤読リスク検出",
        body: page.getByText(
          "誤読候補を検出します。読みの推測や原文の書換えは行いません。",
        ),
      },
      {
        targetId: "yomi-dictionary",
        title: "共通読み辞書",
        body: page.getByPlaceholder("表記（例: 魔王魂）"),
      },
      {
        targetId: "yomi-candidates",
        title: "未解決の誤読候補",
        body: page.getByText("候補テスト", { exact: true }),
      },
      {
        targetId: "editor-settings",
        title: "埋め込みカード",
        body: page.getByText("URL の既定表示", { exact: true }),
      },
      {
        targetId: "pet",
        title: "画面のペット",
        body: page.getByRole("region", { name: "ペットの設定", exact: true }),
      },
      {
        targetId: "task-notifications",
        title: "タスク通知",
        body: page.getByText("新規タスクの通知をデフォルトONにする"),
      },
      {
        targetId: "restart-shortcut",
        title: "ショートカット",
        body: page.getByText("Alt+Shift+R でバックエンドを即時再起動"),
      },
      {
        targetId: "remote-server",
        title: "外部AoiTalkサーバー接続",
        body: page.getByText("Enterpriseサーバーへの自動接続"),
      },
      {
        targetId: "tool-permissions",
        title: "ツール権限",
        body: page.getByText("ファイル直接ツール"),
      },
      {
        targetId: "mcp",
        title: "MCP",
        body: page.getByText("MCPを有効化"),
      },
    ];

    for (const item of cases) {
      const trigger = page.locator(
        `[data-settings-target-activation="${item.targetId}"], [data-settings-disclosure="true"][data-settings-target="${item.targetId}"] button[aria-expanded]`,
      );
      await expect(trigger).toHaveAttribute("aria-expanded", "false");
      await expect(item.body).toHaveCount(0);
      await trigger.click({ force: true });
      await expect(trigger).toHaveAttribute("aria-expanded", "true");
      await expect(item.body).toBeVisible();
      if (item.title === "誤読リスク検出") {
        expect(yomiStatusRequests).toBe(1);
        await expect(page.getByText("検出しきい値: 0.70")).toBeVisible();
      }
      await trigger.click({ force: true });
      await expect(item.body).toHaveCount(0);
    }
  });
});
