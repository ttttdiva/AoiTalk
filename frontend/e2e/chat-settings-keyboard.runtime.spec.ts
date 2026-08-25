import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test("Chat settings keyboard outer/inner transitions keep Portal focus deterministic", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  // The shared E2E API mock intentionally models an unavailable backend. Add
  // only the runtime payload needed to expose all three Chat settings fields.
  await page.route("**/api/python-proxy/health", async (route) => {
    await route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/python-proxy/characters", async (route) => {
    await route.fulfill({
      json: {
        current: "aoi",
        character_options: [
          { slug: "aoi", name: "Aoi" },
          { slug: "writer", name: "Writer" },
        ],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/engine", async (route) => {
    await route.fulfill({
      json: {
        provider: "openai",
        model: "gpt-5.6-sol",
        available: [
          {
            provider: "openai",
            model: "gpt-5.6-sol",
            label: "GPT-5.6 Sol",
            reasoning_effort_options: ["medium", "high", "xhigh"],
          },
        ],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/models", async (route) => {
    await route.fulfill({
      json: {
        current: { provider: "openai", model: "gpt-5.6-sol" },
        providers: [
          {
            id: "openai",
            label: "OpenAI",
            models: [
              {
                id: "gpt-5.6-sol",
                label: "GPT-5.6 Sol",
                reasoning_effort_options: ["medium", "high", "xhigh"],
              },
            ],
          },
        ],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/mode", async (route) => {
    await route.fulfill({
      json: {
        mode: "high",
        available_modes: ["medium", "high", "xhigh"],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/execution-profiles", async (route) => {
    await route.fulfill({
      json: {
        active_profile_id: "manual",
        profiles: [],
        effective_main: null,
      },
    });
  });
  await page.route("**/api/python-proxy/agent-team/config", async (route) => {
    await route.fulfill({ json: { teams: {} } });
  });

  await page.goto("/chat");
  const control = page.getByTestId("chat-model-control");
  await expect(control).toBeVisible({ timeout: 15_000 });
  await expect(control).toContainText("openai / gpt-5.6-sol");
  await page.keyboard.press("Control+Shift+m");

  const provider = page.getByRole("combobox", { name: "Provider" });
  const model = page.getByRole("combobox", { name: "Model" });
  const effort = page.getByRole("combobox", { name: "推論・LLMモード" });
  const agentTeam = page.getByRole("combobox", { name: "Agent Team" });
  const executionProfile = page.getByRole("combobox", { name: "Execution Profile" });
  const character = page.getByRole("combobox", { name: "キャラクター" });
  await expect(provider).toBeVisible();
  await expect(provider).toBeFocused();
  await expect(page.getByRole("option")).toHaveCount(0);

  await page.keyboard.press("ArrowDown");
  await expect(model).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(effort).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option", { name: "Medium" })).toBeVisible();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option")).toHaveCount(0);
  await page.keyboard.press("ArrowDown");
  await expect(agentTeam).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(executionProfile).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(character).toBeFocused();
  await expect(page.getByRole("option")).toHaveCount(0);
});

test("Chat settings keeps Melody reasoning unsupported and skips its status row", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  await page.route("**/api/python-proxy/health", async (route) => {
    await route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/python-proxy/characters", async (route) => {
    await route.fulfill({
      json: {
        current: "aoi",
        character_options: [
          { slug: "aoi", name: "Aoi" },
          { slug: "writer", name: "Writer" },
        ],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/engine", async (route) => {
    await route.fulfill({
      json: {
        provider: "openai_compatible_local",
        model: "melody1437-26b-a4b-v2.0",
        available: [
          {
            provider: "openai_compatible_local",
            model: "melody1437-26b-a4b-v2.0",
            label: "Melody1437-26B-A4B v2.0 Q8_0",
            supports_reasoning: false,
          },
        ],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/models", async (route) => {
    await route.fulfill({
      json: {
        current: {
          provider: "openai_compatible_local",
          model: "melody1437-26b-a4b-v2.0",
        },
        providers: [
          {
            id: "openai_compatible_local",
            label: "ローカルOpenAI互換サーバー",
            models: [
              {
                id: "melody1437-26b-a4b-v2.0",
                label: "Melody1437-26B-A4B v2.0 Q8_0",
                supports_reasoning: false,
              },
            ],
          },
        ],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/mode", async (route) => {
    await route.fulfill({
      json: {
        mode: "fast",
        available_modes: ["fast", "thinking"],
      },
    });
  });
  await page.route("**/api/python-proxy/llm/execution-profiles", async (route) => {
    await route.fulfill({
      json: {
        active_profile_id: "manual",
        profiles: [],
        effective_main: null,
      },
    });
  });
  await page.route("**/api/python-proxy/agent-team/config", async (route) => {
    await route.fulfill({ json: { teams: {} } });
  });

  await page.goto("/chat");
  const control = page.getByTestId("chat-model-control");
  await expect(control).toBeVisible({ timeout: 15_000 });
  await expect(control).toContainText("推論モード指定なし");
  await page.keyboard.press("Control+Shift+m");

  const provider = page.getByRole("combobox", { name: "Provider" });
  const model = page.getByRole("combobox", { name: "Model" });
  const agentTeam = page.getByRole("combobox", { name: "Agent Team" });
  await expect(provider).toBeVisible();
  await expect(provider).toBeFocused();
  await expect(page.getByTestId("chat-effort-unsupported")).toHaveText(
    "推論モード指定なし",
  );
  await expect(
    page.getByRole("combobox", { name: "推論・LLMモード" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("このモデルは推論effortの外部指定に対応していません。", {
      exact: true,
    }),
  ).toBeVisible();

  await page.keyboard.press("ArrowDown");
  await expect(model).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(agentTeam).toBeFocused();
});
