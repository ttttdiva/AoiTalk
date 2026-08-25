import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test("ChatGPTの人間確認と接続障害を別の状態として表示する", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  let loginCheckCount = 0;

  await page.route("**/api/python-proxy/**", async (route) => {
    const url = new URL(route.request().url());

    if (url.pathname === "/api/python-proxy/llm/models") {
      await route.fulfill({
        json: {
          current: { provider: "openai", model: "gpt-5.5" },
          providers: [
            {
              id: "openai",
              label: "OpenAI",
              configured_model: "gpt-5.5",
              supports_custom_model: true,
              source: "static",
              models: [
                {
                  id: "gpt-5.5",
                  label: "GPT-5.5",
                  reasoning_effort_options: [
                    "none",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                  ],
                },
              ],
            },
          ],
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/settings") {
      await route.fulfill({
        json: {
          settings: {
            agent_team: {
              orchestration_mode: "director",
              delegation_enabled: true,
            },
            chatgpt_web: {
              profile_dir: "cache/chatgpt",
              response_timeout_seconds: 900,
              max_rounds_per_turn: 20,
            },
          },
        },
      });
      return;
    }

    if (
      url.pathname === "/api/python-proxy/chatgpt-web/status" &&
      route.request().method() === "GET"
    ) {
      await route.fulfill({
        json: {
          busy: false,
          playwright_available: true,
        },
      });
      return;
    }

    if (
      url.pathname === "/api/python-proxy/chatgpt-web/check-login" &&
      route.request().method() === "POST"
    ) {
      loginCheckCount += 1;
      if (loginCheckCount > 1) {
        await route.fulfill({
          status: 503,
          json: { detail: "ChatGPTへの接続に失敗しました。" },
        });
        return;
      }
      await route.fulfill({
        json: {
          busy: false,
          playwright_available: true,
          logged_in: false,
          needs_human: true,
          message: "ChatGPTの人間確認画面が表示されています。",
        },
      });
      return;
    }

    await route.fallback();
  });

  await page.goto("/settings");
  await page.getByText("言語モデル", { exact: true }).click();
  await page.getByRole("button", { name: "接続を確認" }).click();

  await expect(page.getByRole("status")).toHaveText("確認が必要");
  await expect(page.getByRole("status")).toHaveAttribute(
    "data-variant",
    "outline",
  );
  await expect(
    page.getByText("ChatGPTの人間確認画面が表示されています。"),
  ).toBeVisible();
  await expect(page.getByText("接続エラー", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "接続を確認" }).click();

  await expect(page.getByRole("status")).toHaveText("接続エラー");
  await expect(page.getByRole("status")).toHaveAttribute(
    "data-variant",
    "destructive",
  );
  await expect(
    page.getByText("ChatGPTへの接続に失敗しました。"),
  ).toBeVisible();
});
