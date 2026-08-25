import { expect, test, type Locator, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

type ActiveFocusSnapshot = {
  tag: string | null;
  role: string | null;
  text: string;
  highlighted: boolean;
  isBody: boolean;
  isHtml: boolean;
  isConnected: boolean;
  isCombobox: boolean;
  isDialog: boolean;
  activeIsVisibleOption: boolean;
  optionCount: number;
};

const OPEN_FOCUS_POLL_MS = 100;

const REQUIRED_SELECTS = [
  "Provider",
  "Model",
  "推論・LLMモード",
  "Agent Team",
  "Execution Profile",
  "キャラクター",
] as const;

type SettingsSelectLabel = (typeof REQUIRED_SELECTS)[number];

async function mockChatSettingsRuntime(page: Page) {
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
  await page.route("**/api/python-proxy/character/**", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    const pathname = new URL(route.request().url()).pathname;
    const slug = decodeURIComponent(pathname.split("/").pop() || "");
    await route.fulfill({
      json: { ok: true, character_slug: slug },
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
          {
            provider: "openai",
            model: "gpt-5.6-luna",
            label: "GPT-5.6 Luna",
            reasoning_effort_options: ["medium", "high"],
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
              {
                id: "gpt-5.6-luna",
                label: "GPT-5.6 Luna",
                reasoning_effort_options: ["medium", "high"],
              },
            ],
          },
          {
            id: "anthropic",
            label: "Anthropic",
            models: [
              {
                id: "claude-sonnet",
                label: "Claude Sonnet",
                reasoning_effort_options: ["low", "medium", "high"],
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
        profiles: [
          {
            profile_id: "coding",
            display_name: "Coding",
            enabled: true,
          },
          {
            profile_id: "research",
            display_name: "Research",
            enabled: true,
          },
        ],
        effective_main: null,
      },
    });
  });
  await page.route("**/api/python-proxy/agent-team/config", async (route) => {
    await route.fulfill({
      json: {
        teams: {
          writers: {
            team_id: "writers",
            name: "Writers",
            enabled: true,
            execution_profiles: [
              { profile_id: "coding", name: "Coding", enabled: true },
              { profile_id: "research", name: "Research", enabled: true },
            ],
          },
        },
      },
    });
  });
}

async function openChatSettings(page: Page) {
  await page.goto("/chat");
  const control = page.getByTestId("chat-model-control");
  await expect(control).toBeVisible({ timeout: 15_000 });
  await expect(control).toContainText("openai / gpt-5.6-sol");
  await page.keyboard.press("Control+Shift+m");
  await expect(page.getByText("Chat settings", { exact: true })).toBeVisible();
}

async function visibleSettingsLabels(page: Page) {
  const settings = page.locator("[data-slot='popover-content']").filter({
    hasText: "Chat settings",
  });
  const labels = await settings
    .locator("[data-chat-settings-item]:not([disabled])")
    .evaluateAll((elements) =>
      elements.map(
        (element) =>
          element.getAttribute("aria-label") ||
          element.getAttribute("data-chat-settings-item") ||
          "",
      ),
    );
  return labels.filter(Boolean);
}

async function readActiveFocus(page: Page): Promise<ActiveFocusSnapshot> {
  return page.evaluate(() => {
    const active = document.activeElement as HTMLElement | null;
    const visibleOptions = [
      ...document.querySelectorAll<HTMLElement>('[role="option"]'),
    ].filter((element) => {
      if (!element.isConnected) return false;
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    });
    return {
      tag: active?.tagName ?? null,
      role: active?.getAttribute("role") ?? null,
      text: (active?.innerText ?? "").replace(/\s+/g, " ").trim(),
      highlighted: Boolean(active?.hasAttribute("data-highlighted")),
      isBody: active === document.body,
      isHtml: active === document.documentElement,
      isConnected: Boolean(active?.isConnected),
      isCombobox: active?.getAttribute("role") === "combobox",
      isDialog: active?.getAttribute("role") === "dialog",
      activeIsVisibleOption: Boolean(
        active &&
          active.getAttribute("role") === "option" &&
          active.isConnected &&
          visibleOptions.includes(active),
      ),
      optionCount: visibleOptions.length,
    };
  });
}

function expectFocusOnOpenOption(focus: ActiveFocusSnapshot, label: string) {
  const detail = `${label}: ${JSON.stringify(focus)}`;
  expect(focus.isBody, `focus fell to body after opening ${detail}`).toBe(
    false,
  );
  expect(focus.isHtml, `focus fell to <html> after opening ${detail}`).toBe(
    false,
  );
  expect(
    focus.isConnected,
    `activeElement detached after opening ${detail}`,
  ).toBe(true);
  expect(focus.isDialog, `focus stayed on Popover dialog ${detail}`).toBe(
    false,
  );
  expect(focus.isCombobox, `focus stayed on trigger ${detail}`).toBe(false);
  expect(focus.role, `activeElement is not an option ${detail}`).toBe("option");
  expect(
    focus.activeIsVisibleOption,
    `activeElement is not a visible option of the open list ${detail}`,
  ).toBe(true);
  expect(focus.highlighted, `open option is not highlighted ${detail}`).toBe(
    true,
  );
}

async function waitForOpenList(page: Page, trigger: Locator) {
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("option").first()).toBeVisible();
}

async function expectOpenListKeyboardReady(page: Page, label: string) {
  // Do not synthesize Arrow to create option focus. That hid the bug on
  // unfixed builds. Base UI FloatingFocusManager enqueues option focus one
  // frame after the list is visible; poll at most that window. Unfixed
  // stays on the trigger for the whole interval.
  await expect
    .poll(async () => (await readActiveFocus(page)).activeIsVisibleOption, {
      timeout: OPEN_FOCUS_POLL_MS,
      intervals: [16],
    })
    .toBe(true);
  const opened = await readActiveFocus(page);
  expectFocusOnOpenOption(opened, label);
  expect(
    opened.optionCount,
    `${label}: need 2+ options so ArrowDown can move ${JSON.stringify(opened)}`,
  ).toBeGreaterThan(1);
  const optionTexts = (await page.getByRole("option").allInnerTexts()).map(
    (text) => text.replace(/\s+/g, " ").trim(),
  );
  const currentIndex = optionTexts.indexOf(opened.text);
  const moveKey =
    currentIndex >= 0 && currentIndex < optionTexts.length - 1
      ? "ArrowDown"
      : "ArrowUp";
  await page.keyboard.press(moveKey);
  const moved = await readActiveFocus(page);
  expectFocusOnOpenOption(moved, `${label} after ${moveKey}`);
  expect(
    moved.text,
    `${label}: ${moveKey} did not move from "${opened.text}" ${JSON.stringify(moved)}`,
  ).not.toBe(opened.text);
  return moved;
}

async function closeInnerIfOpen(page: Page) {
  if ((await page.getByRole("option").count()) === 0) return;
  await page.keyboard.press("Escape");
  await expect(page.getByRole("option")).toHaveCount(0);
}

async function ensureWritersTeamForMultipleProfiles(page: Page) {
  // Auto only exposes Execution Profile=None. Pick Writers first so the
  // profile list has None + Coding + Research before the contract runs.
  const team = page.getByRole("combobox", { name: "Agent Team" });
  const profile = page.getByRole("combobox", { name: "Execution Profile" });
  await expect(team).toBeVisible();
  await expect(team).toBeEnabled();
  await expect(profile).toBeVisible();
  await expect(profile).toBeEnabled();
  if ((await team.innerText()).includes("Writers")) return;

  await closeInnerIfOpen(page);
  await team.click();
  await expect(page.getByRole("option", { name: "Writers" })).toBeVisible();
  await page.getByRole("option", { name: "Writers" }).click();
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(team).toContainText("Writers");
  await expect(profile).toBeEnabled();
}

async function prepareSettingsSelect(page: Page, label: SettingsSelectLabel) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await mockChatSettingsRuntime(page);
  await openChatSettings(page);

  const labels = await visibleSettingsLabels(page);
  for (const required of REQUIRED_SELECTS) {
    expect(labels, `missing required Select "${required}"`).toContain(required);
  }
  if (label === "Execution Profile") {
    await ensureWritersTeamForMultipleProfiles(page);
  }
}

async function openSelect(
  page: Page,
  trigger: Locator,
  openBy: "pointer" | "enter" | "arrowRight",
) {
  await closeInnerIfOpen(page);
  await expect(trigger).toBeVisible();
  await expect(trigger).toBeEnabled();
  if (openBy === "pointer") {
    await trigger.click();
    return;
  }
  await trigger.focus();
  await expect(trigger).toBeFocused();
  await page.keyboard.press(openBy === "enter" ? "Enter" : "ArrowRight");
}

async function assertEnterCommit(
  page: Page,
  trigger: Locator,
  label: SettingsSelectLabel,
  committed: ActiveFocusSnapshot,
) {
  expect(
    committed.text,
    `${label}: Enter commit needs a highlighted option label ${JSON.stringify(committed)}`,
  ).not.toBe("");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(trigger).toContainText(committed.text);

  if (label === "キャラクター") {
    // changeCharacter sets characterChanging and disables this trigger.
    // Existing outer-layer recovery then moves to the first enabled Select
    // (Provider). Stay inside Chat settings; the new value must remain on
    // this same trigger. The open → option focus → Arrow → Enter path is
    // what this case proves.
    await expect(page.getByText("Chat settings", { exact: true })).toBeVisible();
    await expect(trigger).toContainText(committed.text);
    await expect(trigger).toBeEnabled({ timeout: 5_000 });
    return;
  }

  await expect(trigger).toBeEnabled();
  await expect(trigger).toBeFocused();
}

test("Chat settings pointer click → focused option → ArrowDown → Enter commit (all 6 Selects)", async ({
  page,
}) => {
  for (const label of REQUIRED_SELECTS) {
    await prepareSettingsSelect(page, label);
    const trigger = page.getByRole("combobox", { name: label });
    await openSelect(page, trigger, "pointer");
    await waitForOpenList(page, trigger);
    const moved = await expectOpenListKeyboardReady(
      page,
      `${label} (pointer click → focused option → ArrowDown)`,
    );
    await assertEnterCommit(page, trigger, label, moved);
  }
});

test("Chat settings Enter open → focused option → ArrowDown → Enter commit (all 6 Selects)", async ({
  page,
}) => {
  for (const label of REQUIRED_SELECTS) {
    await prepareSettingsSelect(page, label);
    const trigger = page.getByRole("combobox", { name: label });
    await openSelect(page, trigger, "enter");
    await waitForOpenList(page, trigger);
    const moved = await expectOpenListKeyboardReady(
      page,
      `${label} (Enter open → focused option → ArrowDown)`,
    );
    await assertEnterCommit(page, trigger, label, moved);
  }
});

test("Chat settings ArrowRight open → focused option → ArrowDown (all 6 Selects)", async ({
  page,
}) => {
  for (const label of REQUIRED_SELECTS) {
    await prepareSettingsSelect(page, label);
    const trigger = page.getByRole("combobox", { name: label });
    await openSelect(page, trigger, "arrowRight");
    await waitForOpenList(page, trigger);
    await expectOpenListKeyboardReady(
      page,
      `${label} (ArrowRight open → focused option → ArrowDown)`,
    );
    await page.keyboard.press("ArrowLeft");
    await expect(page.getByRole("option")).toHaveCount(0);
    await expect(trigger).toBeFocused();
  }
});

test("Chat settings keyboard-only outer path returns to the composer", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await mockChatSettingsRuntime(page);
  await openChatSettings(page);

  const provider = page.getByRole("combobox", { name: "Provider" });
  const model = page.getByRole("combobox", { name: "Model" });
  const effort = page.getByRole("combobox", { name: "推論・LLMモード" });
  await expect(provider).toBeFocused();

  await page.keyboard.press("ArrowDown");
  await expect(model).toBeFocused();
  await page.keyboard.press("Enter");
  await waitForOpenList(page, model);
  await expectOpenListKeyboardReady(page, "Model (outer Enter)");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(model).toBeFocused();

  await page.keyboard.press("ArrowDown");
  await expect(effort).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByText("Chat settings", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("textbox").last()).toBeFocused();
});
