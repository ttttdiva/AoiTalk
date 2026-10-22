import { expect, test, type Page, type Route } from "@playwright/test";
import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

const MAIN_ROUTE = { provider: "mock", model: "mock-model" } as const;
const MESSAGE = "新規通常チャットの回復確認";

const EMPTY_SETTINGS = {
  agent_team_selection: { mode: "auto", team_id: "", loaded_team_ids: [] },
  main_route: {},
  special_routing: {},
  execution_profile_id: "",
};

const CREATED_SESSION = {
  id: "recovery-created-session",
  user_id: E2E_USER_ID,
  character_name: "aoi",
  title: "Recovery chat",
  message_count: 0,
  is_active: true,
  is_group_chat: false,
  group_character_names: [],
};

const EXISTING_SESSION = {
  id: "recovery-existing-session",
  user_id: E2E_USER_ID,
  character_name: "aoi",
  title: "Existing recovery chat",
  message_count: 1,
  is_active: true,
  is_group_chat: false,
  group_character_names: [],
};

const EXISTING_MESSAGE = {
  id: "recovery-existing-message",
  session_id: EXISTING_SESSION.id,
  role: "assistant",
  content: "切替先の会話を保持します",
  metadata: {},
  branch_index: 0,
  is_active_branch: true,
};

type NewChatDefaults = {
  last_used_main?: unknown;
  effective_main?: unknown;
};

type RecoveryCounters = {
  defaults: number;
  creates: Array<Record<string, unknown>>;
  settingsPuts: Array<Record<string, unknown>>;
  dispatches: Array<Record<string, unknown>>;
};

type RecoveryFixtureOptions = {
  defaults: (
    route: Route,
    counters: RecoveryCounters,
  ) => Promise<void>;
  holdCreate?: Promise<void>;
};

function sessionFor(id: string) {
  return id === CREATED_SESSION.id ? CREATED_SESSION : EXISTING_SESSION;
}

function conversationIdFromUrl(url: string): string {
  return new URL(url).pathname.match(/\/api\/conversations\/([^/]+)/)?.[1] ?? "";
}

function proxyConversationIdFromUrl(url: string): string {
  return new URL(url).pathname.match(/\/api\/python-proxy\/conversations\/([^/]+)/)?.[1] ?? "";
}

async function installRecoveryFixture(
  page: Page,
  options: RecoveryFixtureOptions,
): Promise<RecoveryCounters> {
  const counters: RecoveryCounters = {
    defaults: 0,
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
      await options.defaults(route, counters);
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
        await route.fulfill({
          json: { settings, effective_main: mainRoute },
        });
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
      if (options.holdCreate) await options.holdCreate;
      await route.fulfill({ json: { session: CREATED_SESSION } });
      return;
    }
    await route.fulfill({
      json: { conversations: [EXISTING_SESSION, CREATED_SESSION], total: 2 },
    });
  });

  await page.route("**/api/conversations/*/resume**", async (route) => {
    const id = conversationIdFromUrl(route.request().url());
    const session = sessionFor(id);
    await route.fulfill({
      json: {
        session,
        messages: id === EXISTING_SESSION.id ? [EXISTING_MESSAGE] : [],
      },
    });
  });
  await page.route("**/api/conversations/*/messages**", async (route) => {
    const id = conversationIdFromUrl(route.request().url());
    await route.fulfill({
      json: {
        messages: id === EXISTING_SESSION.id ? [EXISTING_MESSAGE] : [],
      },
    });
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
          agent_run_id: "recovery-run",
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
  // Let the authenticated user scope commit before a send captures its
  // draft/revision fence. This is especially important for the fresh
  // production artifact where the composer can paint before auth hydration.
  await page.waitForTimeout(50);
  const input = page.getByRole("textbox", { name: "メッセージ入力", exact: true });
  await expect(input).toBeVisible();
  return input;
}

async function expectAuthoritativeRouteError(page: Page) {
  const error = page
    .locator('[data-sonner-toast], [role="alert"]')
    .filter({ hasText: /Provider.*Model|モデル設定|last-used|確定できない|応答生成/ })
    .first();
  await expect(error).toBeVisible();
}

test("新規送信は初期defaultsの遅延中に待機し、create/dispatchを一度だけ実行する", async ({
  page,
}) => {
  let releaseDefaults!: () => void;
  const defaultsGate = new Promise<void>((resolve) => {
    releaseDefaults = resolve;
  });
  let defaultsStarted!: () => void;
  const defaultsStartedPromise = new Promise<void>((resolve) => {
    defaultsStarted = resolve;
  });

  const counters = await installRecoveryFixture(page, {
    defaults: async (route) => {
      defaultsStarted();
      await defaultsGate;
      await route.fulfill({
        json: { last_used_main: MAIN_ROUTE, effective_main: MAIN_ROUTE },
      });
    },
  });

  const input = await openNewChat(page);
  await defaultsStartedPromise;
  await input.fill(MESSAGE);
  await page.getByTitle("送信").dispatchEvent("click");

  await expect.poll(() => counters.creates.length).toBe(0);
  await expect.poll(() => counters.dispatches.length).toBe(0);

  releaseDefaults();
  await expect.poll(() => counters.creates.length, { timeout: 15_000 }).toBe(1);
  await expect.poll(() => counters.dispatches.length, { timeout: 15_000 }).toBe(1);
  expect(counters.creates[0]?.main_route).toEqual(MAIN_ROUTE);
  expect(counters.dispatches[0]?.message).toBe(MESSAGE);
});

test("defaultsの503後は下書きを保持し、再試行で新規送信を回復する", async ({ page }) => {
  let allowDefaults = false;
  const counters = await installRecoveryFixture(page, {
    defaults: async (route) => {
      if (!allowDefaults) {
        await route.fulfill({ status: 503, json: { detail: "defaults unavailable" } });
        return;
      }
      await route.fulfill({
        json: { last_used_main: MAIN_ROUTE, effective_main: MAIN_ROUTE },
      });
    },
  });

  const input = await openNewChat(page);
  await expect.poll(() => counters.defaults).toBeGreaterThan(0);
  await input.fill(MESSAGE);
  await page.getByTitle("送信").dispatchEvent("click");

  await expect.poll(() => counters.creates.length).toBe(0);
  await expect(input).toHaveValue(MESSAGE, { timeout: 10_000 });
  await expectAuthoritativeRouteError(page);

  allowDefaults = true;
  await page.getByTitle("送信").dispatchEvent("click");
  await expect.poll(() => counters.creates.length, { timeout: 15_000 }).toBe(1);
  await expect.poll(() => counters.dispatches.length, { timeout: 15_000 }).toBe(1);
  expect(counters.dispatches[0]?.message).toBe(MESSAGE);
});

test("200の空／不正なdefaultsは送信せず、入力保持と可視エラーを維持する", async ({
  browser,
  page,
}) => {
  const cases: NewChatDefaults[] = [
    {
      last_used_main: MAIN_ROUTE,
      effective_main: {},
    },
    {
      last_used_main: { provider: "mock" },
      effective_main: { provider: 42, model: null },
    },
  ];

  const firstCounters = await installRecoveryFixture(page, {
    defaults: async (route) => {
      await route.fulfill({ json: cases[0] });
    },
  });
  const firstInput = await openNewChat(page);
  await firstInput.fill(MESSAGE);
  await page.getByTitle("送信").dispatchEvent("click");
  await expect(firstInput).toHaveValue(MESSAGE, { timeout: 10_000 });
  await expect.poll(() => firstCounters.creates.length).toBe(0);
  await expect.poll(() => firstCounters.dispatches.length).toBe(0);
  await expectAuthoritativeRouteError(page);

  const origin = new URL(page.url()).origin;
  const context = await browser.newContext({ baseURL: origin });
  const secondPage = await context.newPage();
  try {
    const secondCounters = await installRecoveryFixture(secondPage, {
      defaults: async (route) => {
        await route.fulfill({ json: cases[1] });
      },
    });
    const secondInput = await openNewChat(secondPage);
    await secondInput.fill(MESSAGE);
    await secondPage.getByTitle("送信").dispatchEvent("click");
    await expect(secondInput).toHaveValue(MESSAGE, { timeout: 10_000 });
    await expect.poll(() => secondCounters.creates.length).toBe(0);
    await expect.poll(() => secondCounters.dispatches.length).toBe(0);
    await expectAuthoritativeRouteError(secondPage);
  } finally {
    await context.close();
  }
});

test("同tickの二重送信を作らず、セッション切替後の再読込を維持する", async ({
  page,
}) => {
  let releaseCreate!: () => void;
  const createGate = new Promise<void>((resolve) => {
    releaseCreate = resolve;
  });
  const counters = await installRecoveryFixture(page, {
    defaults: async (route) => {
      await route.fulfill({
        json: { last_used_main: MAIN_ROUTE, effective_main: MAIN_ROUTE },
      });
    },
    holdCreate: createGate,
  });

  const input = await openNewChat(page);
  await expect.poll(() => counters.defaults).toBeGreaterThan(0);
  await input.fill(MESSAGE);
  const send = page.getByTitle("送信");
  await send.dispatchEvent("click");
  await send.dispatchEvent("click");
  await expect.poll(() => counters.creates.length).toBeLessThanOrEqual(1);
  releaseCreate();
  await expect.poll(() => counters.creates.length, { timeout: 15_000 }).toBe(1);
  await expect.poll(() => counters.dispatches.length, { timeout: 15_000 }).toBe(1);

  await page.goto(`/chat?s=${EXISTING_SESSION.id}`);
  await expect(page.getByText(EXISTING_MESSAGE.content, { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText(EXISTING_MESSAGE.content, { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "メッセージ入力", exact: true })).toBeVisible();
});
