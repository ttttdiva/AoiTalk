import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

const MAIN_ROUTE = { provider: "mock", model: "mock-model" } as const;
const MESSAGE = "新規送信の楽観的表示確認";
const DEFAULTS_HOLD_MS = 1_500;
const TIMING_BUDGET_MS = 750;

const EMPTY_SETTINGS = {
  agent_team_selection: { mode: "auto", team_id: "", loaded_team_ids: [] },
  main_route: {},
  special_routing: {},
  execution_profile_id: "",
};

const CREATED_SESSION = {
  id: "optimistic-created-session",
  user_id: E2E_USER_ID,
  character_name: "aoi",
  title: "Optimistic chat",
  message_count: 0,
  is_active: true,
  is_group_chat: false,
  group_character_names: [],
};

type SendCounters = {
  defaults: number;
  defaultsFulfilled: number;
  creates: Array<Record<string, unknown>>;
  settingsPuts: Array<Record<string, unknown>>;
  dispatches: Array<Record<string, unknown>>;
};

type DefaultsGate = {
  promise: Promise<void>;
  release: () => void;
  started: Promise<number>;
  markStarted: (startedAt: number) => void;
  released: boolean;
};

function createDefaultsGate(): DefaultsGate {
  let release!: () => void;
  let resolveStarted!: (startedAt: number) => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  const started = new Promise<number>((resolve) => {
    resolveStarted = resolve;
  });

  return {
    promise,
    release,
    started,
    markStarted: resolveStarted,
    released: false,
  };
}

function conversationIdFromUrl(url: string): string {
  return new URL(url).pathname.match(/\/api\/conversations\/([^/]+)/)?.[1] ?? "";
}

function proxyConversationIdFromUrl(url: string): string {
  return (
    new URL(url).pathname.match(/\/api\/python-proxy\/conversations\/([^/]+)/)?.[1] ??
    ""
  );
}

async function installFixture(
  page: Page,
  defaultsGate: DefaultsGate,
): Promise<SendCounters> {
  const counters: SendCounters = {
    defaults: 0,
    defaultsFulfilled: 0,
    creates: [],
    settingsPuts: [],
    dispatches: [],
  };

  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => socket.close());

  await page.route("**/api/python-proxy/characters", async (route) => {
    await route.fulfill({ json: { characters: ["aoi"], current: "aoi" } });
  });
  await page.route("**/api/python-proxy/agent-team/config", async (route) => {
    await route.fulfill({ json: { teams: {} } });
  });
  await page.route("**/api/python-proxy/llm/execution-profiles", async (route) => {
    await route.fulfill({
      json: { active_profile_id: "manual", profiles: [], effective_main: MAIN_ROUTE },
    });
  });
  await page.route(
    "**/api/python-proxy/llm/new-chat-defaults",
    async (route) => {
      counters.defaults += 1;
      if (counters.defaults === 1) {
        defaultsGate.markStarted(performance.now());
      }
      await defaultsGate.promise;
      await route.fulfill({
        json: { last_used_main: MAIN_ROUTE, effective_main: MAIN_ROUTE },
      });
      counters.defaultsFulfilled += 1;
    },
  );
  await page.route(
    "**/api/python-proxy/llm/session-settings*",
    async (route) => {
      const method = route.request().method();
      if (method === "PUT") {
        const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
        counters.settingsPuts.push(body);
        const settings =
          body.settings && typeof body.settings === "object"
            ? (body.settings as Record<string, unknown>)
            : EMPTY_SETTINGS;
        const mainRoute =
          settings.main_route && typeof settings.main_route === "object"
            ? settings.main_route
            : MAIN_ROUTE;
        await route.fulfill({ json: { settings, effective_main: mainRoute } });
        return;
      }
      await route.fulfill({
        json: { settings: EMPTY_SETTINGS, effective_main: MAIN_ROUTE },
      });
    },
  );

  await page.route("**/api/conversations", async (route) => {
    const method = route.request().method();
    if (method === "POST") {
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      counters.creates.push(body);
      await route.fulfill({ json: { session: CREATED_SESSION } });
      return;
    }
    await route.fulfill({ json: { conversations: [CREATED_SESSION], total: 1 } });
  });
  await page.route("**/api/conversations/*/resume**", async (route) => {
    const id = conversationIdFromUrl(route.request().url());
    await route.fulfill({
      json: {
        session: { ...CREATED_SESSION, id },
        messages: [],
      },
    });
  });
  await page.route("**/api/conversations/*/messages**", async (route) => {
    await route.fulfill({ json: { messages: [] } });
  });
  await page.route(
    "**/api/python-proxy/conversations/*/generation/status",
    async (route) => {
      const id = proxyConversationIdFromUrl(route.request().url());
      await route.fulfill({
        json: { session_id: id, running: false, status: "idle" },
      });
    },
  );
  await page.route("**/api/conversations/*/related-tasks", async (route) => {
    await route.fulfill({ json: { tasks: [] } });
  });
  await page.route(
    "**/api/python-proxy/conversations/*/dispatch",
    async (route) => {
      if (route.request().method() !== "POST") {
        await route.fallback();
        return;
      }
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      counters.dispatches.push(body);
      const sessionId = proxyConversationIdFromUrl(route.request().url());
      await route.fulfill({
        json: {
          success: true,
          queued: false,
          session_id: sessionId,
          agent_run_id: "optimistic-run",
        },
      });
    },
  );

  return counters;
}

async function openNewChat(page: Page): Promise<ReturnType<Page["getByRole"]>> {
  const authStatus = page.waitForResponse(
    (response) =>
      response.url().includes("/api/auth/status") &&
      response.request().method() === "GET",
  );
  await page.goto("/chat");
  await authStatus;
  await page.waitForTimeout(50);
  const input = page.getByRole("textbox", { name: "メッセージ入力", exact: true });
  await expect(input).toBeVisible();
  return input;
}

test("新規送信はdefaults待機中にbubbleとcomposerを先に更新し、create/dispatchを一度だけ実行する", async ({
  page,
}) => {
  const defaultsGate = createDefaultsGate();

  const counters = await installFixture(page, defaultsGate);
  const input = await openNewChat(page);
  const defaultsStartedAt = await defaultsGate.started;

  try {
    await input.fill(MESSAGE);
    const clickAt = performance.now();
    await page.getByTitle("送信").dispatchEvent("click");

    const pendingBubble = page
      .locator('[data-chat-message-delivery-state="pending"]')
      .filter({ hasText: MESSAGE })
      .first();
    await expect(pendingBubble).toBeVisible({ timeout: TIMING_BUDGET_MS });
    const bubbleVisibleAt = performance.now();
    await expect(input).toHaveValue("", { timeout: TIMING_BUDGET_MS });
    const inputClearedAt = performance.now();
    const clickToBubbleMs = bubbleVisibleAt - clickAt;
    const clickToClearMs = inputClearedAt - clickAt;

    // These assertions record bounded click-to-render timings for the regression
    // while the defaults request is still unresolved.
    expect(clickToBubbleMs).toBeLessThan(TIMING_BUDGET_MS);
    expect(clickToClearMs).toBeLessThan(TIMING_BUDGET_MS);
    expect(defaultsStartedAt).toBeLessThanOrEqual(clickAt);
    expect(defaultsGate.released).toBe(false);
    expect(counters.defaults).toBeGreaterThan(0);
    expect(counters.defaultsFulfilled).toBe(0);
    expect(counters.creates).toHaveLength(0);
    expect(counters.dispatches).toHaveLength(0);

    const elapsedSinceClick = performance.now() - clickAt;
    if (elapsedSinceClick < DEFAULTS_HOLD_MS) {
      await page.waitForTimeout(Math.ceil(DEFAULTS_HOLD_MS - elapsedSinceClick) + 10);
    }
    const gateReleaseAt = performance.now();
    const unresolvedGateMs = gateReleaseAt - defaultsStartedAt;
    expect(gateReleaseAt - clickAt).toBeGreaterThanOrEqual(DEFAULTS_HOLD_MS);
    expect(defaultsGate.released).toBe(false);
    expect(counters.defaultsFulfilled).toBe(0);
    expect(counters.creates).toHaveLength(0);
    expect(counters.dispatches).toHaveLength(0);

    console.info(
      `[chat-send-optimistic] click->bubble=${clickToBubbleMs.toFixed(1)}ms ` +
        `click->clear=${clickToClearMs.toFixed(1)}ms ` +
        `defaults-gate-unresolved=${unresolvedGateMs.toFixed(1)}ms`,
    );

    defaultsGate.released = true;
    defaultsGate.release();

    await expect
      .poll(() => counters.defaultsFulfilled, { timeout: 15_000 })
      .toBeGreaterThan(0);
    await expect.poll(() => counters.creates.length, { timeout: 15_000 }).toBe(1);
    await expect.poll(() => counters.dispatches.length, { timeout: 15_000 }).toBe(1);
    expect(counters.creates[0]?.main_route).toEqual(MAIN_ROUTE);
    expect(counters.creates[0]?.initial_message).toMatchObject({
      content: MESSAGE,
    });
    expect(counters.dispatches[0]?.message).toBe(MESSAGE);
  } finally {
    if (!defaultsGate.released) {
      defaultsGate.released = true;
      defaultsGate.release();
    }
  }
});
