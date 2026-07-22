import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("設定セクションの初期表示", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
  });

  test("主要な設定本文を閉じて表示し、見出し操作で展開できる", async ({ page }) => {
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
      await route.fallback();
    });
    await page.goto("/settings");
    expect(yomiStatusRequests).toBe(0);

    const disclosureCard = page
      .locator('button[aria-expanded]')
      .filter({ hasText: "誤読リスク検出" })
      .locator('xpath=ancestor::*[@data-slot="card"][1]');
    const existingCompactCard = page
      .locator('[data-slot="card-title"]')
      .filter({ hasText: "埋め込みカード" })
      .locator('xpath=ancestor::*[@data-slot="card"][1]');
    const existingCompactTitle = existingCompactCard.locator('[data-slot="card-title"]');
    const disclosureBox = await disclosureCard.boundingBox();
    const existingCompactBox = await existingCompactCard.boundingBox();
    const disclosureTriggerBox = await disclosureCard
      .locator('button[aria-expanded]')
      .boundingBox();
    const disclosureTitleBox = await disclosureCard
      .locator('button[aria-expanded] > span')
      .boundingBox();
    const existingCompactTitleBox = await existingCompactTitle.boundingBox();
    expect(disclosureBox?.height).toBe(existingCompactBox?.height);
    expect(disclosureTriggerBox?.height).toBe(
      disclosureBox ? disclosureBox.height - 2 : undefined,
    );
    expect(disclosureTitleBox?.x).toBe(existingCompactTitleBox?.x);

    const cases = [
      {
        title: "よく使う設定",
        body: page.getByRole("button", { name: "編集", exact: true }),
      },
      {
        title: "誤読リスク検出",
        body: page.getByText("誤読候補を検出します。読みの推測や原文の書換えは行いません。"),
      },
      {
        title: "共通読み辞書",
        body: page.getByPlaceholder("表記（例: 魔王魂）"),
      },
      {
        title: "未解決の誤読候補",
        body: page.getByText("候補テスト", { exact: true }),
      },
      {
        title: "タスク通知",
        body: page.getByText("新規タスクの通知をデフォルトONにする"),
      },
      {
        title: "ショートカット",
        body: page.getByText("Alt+Shift+R でバックエンドを即時再起動"),
      },
      {
        title: "外部AoiTalkサーバー接続",
        body: page.getByText("Enterpriseサーバーへの自動接続"),
      },
      {
        title: "ツール権限",
        body: page.getByText("ファイル直接ツール"),
      },
      {
        title: "MCP",
        body: page.getByText("MCPを有効化"),
      },
    ];

    for (const item of cases) {
      const trigger = page
        .locator('button[aria-expanded]')
        .filter({ hasText: item.title });
      await expect(trigger).toHaveAttribute("aria-expanded", "false");
      await expect(item.body).toHaveCount(0);
      await trigger.click();
      await expect(trigger).toHaveAttribute("aria-expanded", "true");
      await expect(item.body).toBeVisible();
      if (item.title === "誤読リスク検出") {
        expect(yomiStatusRequests).toBe(1);
        await expect(page.getByText("検出しきい値: 0.70")).toBeVisible();
      }
      await trigger.click();
      await expect(item.body).toHaveCount(0);
    }
  });
});
