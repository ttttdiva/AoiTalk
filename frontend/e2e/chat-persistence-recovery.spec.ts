import { expect, test, type Page, type Response, type WebSocketRoute } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

const SESSION_A = "portal-fetch-session-a";
const SESSION_B = "portal-fetch-session-b";
const SESSION_CACHE = "portal-cache-poison-session";
const A_TITLE = "会話A 初期タイトル";
const A_UPDATED_TITLE = "会話A WS更新タイトル";
const B_TITLE = "会話B 隣接タイトル";
const A_BODY = "会話Aの保存済み本文";
const B_BODY = "会話Bの初期本文";
const B_RECOVERED_BODY = "会話Bの再接続後に復旧した本文";
const CACHE_BODY = "再訪時に誤って残るキャッシュ本文";

const QA_DIR = resolve(process.cwd(), "../.local/portal-qa");
const QA_HARNESS_CONSOLE_ERRORS = new Set([
  "A bad HTTP response code (404) was received when fetching the script.",
]);

function unexpectedConsoleErrors(
  messages: Array<{ type: string; text: string }>,
  expectedExact: Set<string> = new Set(),
  expectedPrefixes: string[] = [],
): Array<{ type: string; text: string }> {
  return messages.filter(
    (item) =>
      item.type === "error" &&
      !QA_HARNESS_CONSOLE_ERRORS.has(item.text) &&
      !expectedExact.has(item.text) &&
      !expectedPrefixes.some((prefix) => item.text.startsWith(prefix)),
  );
}

type FixtureSession = {
  id: string;
  user_id: string;
  title: string;
  character_name: string;
  message_count: number;
  is_active: boolean;
  is_group_chat: boolean;
  group_character_names: string[];
};

type FixtureMessage = {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  metadata: Record<string, unknown>;
  branch_index: number;
  is_active_branch: boolean;
};

type NetworkEvent = {
  kind: "request" | "response";
  method: string;
  path: string;
  status?: number;
};

type Fixture = {
  sockets: WebSocketRoute[];
  socketUrls: string[];
  events: NetworkEvent[];
  consoleMessages: Array<{ type: string; text: string }>;
  pageErrors: string[];
  failedResponses: Array<{ path: string; status: number }>;
  httpFailures: Array<{ method: string; path: string; status: number }>;
  counts: {
    resume: Record<string, number>;
    messages: Record<string, number>;
    generation: Record<string, number>;
  };
  armReconnectRecovery: () => void;
};

function session(id: string, title: string, messageCount: number): FixtureSession {
  return {
    id,
    user_id: E2E_USER_ID,
    title,
    character_name: "aoi",
    message_count: messageCount,
    is_active: true,
    is_group_chat: false,
    group_character_names: [],
  };
}

function message(
  id: string,
  sessionId: string,
  role: FixtureMessage["role"],
  content: string,
): FixtureMessage {
  return {
    id,
    session_id: sessionId,
    role,
    content,
    metadata: {},
    branch_index: 0,
    is_active_branch: true,
  };
}

function pathOf(url: string): string {
  return new URL(url).pathname;
}

function conversationId(path: string): string {
  return path.match(/\/api\/conversations\/([^/]+)/)?.[1] ?? "";
}

function proxyConversationId(path: string): string {
  return path.match(/\/api\/python-proxy\/conversations\/([^/]+)/)?.[1] ?? "";
}

const QA_CACHE_DB_NAME = `aoitalk-cache::${encodeURIComponent(E2E_USER_ID)}`;
const QA_CACHE_STORE = "kv";

type CacheSnapshot = {
  messages: FixtureMessage[];
  serverTime: string | null;
};

async function seedChatSnapshot(
  page: Page,
  sessionId: string,
  snapshot: CacheSnapshot,
): Promise<void> {
  await page.goto("/login");
  await page.evaluate(
    ({ dbName, storeName, key, value }) =>
      new Promise<void>((resolve, reject) => {
        const request = indexedDB.open(dbName, 1);
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(storeName)) {
            db.createObjectStore(storeName);
          }
        };
        request.onerror = () => reject(request.error ?? new Error("IDB open failed"));
        request.onsuccess = () => {
          const db = request.result;
          const transaction = db.transaction(storeName, "readwrite");
          transaction.objectStore(storeName).put(value, key);
          transaction.oncomplete = () => {
            db.close();
            resolve();
          };
          transaction.onerror = () => reject(transaction.error ?? new Error("IDB seed failed"));
        };
      }),
    {
      dbName: QA_CACHE_DB_NAME,
      storeName: QA_CACHE_STORE,
      key: `chat/messages/${sessionId}`,
      value: snapshot,
    },
  );
}

async function readChatSnapshot(
  page: Page,
  sessionId: string,
): Promise<CacheSnapshot | null> {
  return page.evaluate(
    ({ dbName, storeName, key }) =>
      new Promise<CacheSnapshot | null>((resolve) => {
        const request = indexedDB.open(dbName, 1);
        request.onerror = () => resolve(null);
        request.onsuccess = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(storeName)) {
            db.close();
            resolve(null);
            return;
          }
          const transaction = db.transaction(storeName, "readonly");
          const getRequest = transaction.objectStore(storeName).get(key);
          getRequest.onsuccess = () => {
            db.close();
            resolve((getRequest.result as CacheSnapshot | undefined) ?? null);
          };
          getRequest.onerror = () => {
            db.close();
            resolve(null);
          };
        };
      }),
    {
      dbName: QA_CACHE_DB_NAME,
      storeName: QA_CACHE_STORE,
      key: `chat/messages/${sessionId}`,
    },
  );
}

function sessionItem(page: Page, id: string) {
  return page.locator(`[data-chat-session-id="${id}"]`);
}

async function writeEvidence(name: string, data: unknown): Promise<void> {
  const evidenceDir = resolve(QA_DIR, "evidence");
  await mkdir(evidenceDir, { recursive: true });
  await writeFile(
    resolve(evidenceDir, name),
    `${JSON.stringify(data, null, 2)}\n`,
    "utf8",
  );
}

async function installFixture(page: Page): Promise<Fixture> {
  const sessions = {
    [SESSION_A]: session(SESSION_A, A_TITLE, 2),
    [SESSION_B]: session(SESSION_B, B_TITLE, 1),
    [SESSION_CACHE]: session(SESSION_CACHE, "キャッシュ権限確認", 1),
  } as Record<string, FixtureSession>;
  const messages = {
    [SESSION_A]: [
      message("a-user", SESSION_A, "user", "会話Aの質問"),
      message("a-assistant", SESSION_A, "assistant", A_BODY),
    ],
    [SESSION_B]: [message("b-initial", SESSION_B, "assistant", B_BODY)],
    [SESSION_CACHE]: [message("cache-cached", SESSION_CACHE, "assistant", CACHE_BODY)],
  } as Record<string, FixtureMessage[]>;
  const sockets: WebSocketRoute[] = [];
  const socketUrls: string[] = [];
  const events: NetworkEvent[] = [];
  const consoleMessages: Array<{ type: string; text: string }> = [];
  const pageErrors: string[] = [];
  const failedResponses: Array<{ path: string; status: number }> = [];
  const httpFailures: Array<{ method: string; path: string; status: number }> = [];
  const counts: Fixture["counts"] = {
    resume: { [SESSION_A]: 0, [SESSION_B]: 0 },
    messages: { [SESSION_A]: 0, [SESSION_B]: 0, [SESSION_CACHE]: 0 },
    generation: { [SESSION_A]: 0, [SESSION_B]: 0, [SESSION_CACHE]: 0 },
  };
  let reconnectRecoveryArmed = false;
  let failNextBMessages = false;

  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  // Keep the browser shell green while all backend data remains fixture-only.
  // The shared fixture intentionally returns 503 for health checks in most
  // tests; this flow needs a connected runtime shell to exercise reconnect.
  await page.route("**/api/python-proxy/health", (route) =>
    route.fulfill({ json: { status: "ok" } }),
  );
  await page.route("**/notifications-sw.js", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: "",
    }),
  );
  await page.route("**/login", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<!doctype html><html><body>QA seed page</body></html>",
    }),
  );

  page.on("request", (request) => {
    const path = pathOf(request.url());
    const method = request.method();
    if (
      method === "GET" &&
      (path.includes("/api/conversations/") ||
        path === "/api/conversations")
    ) {
      events.push({ kind: "request", method, path });
    }
  });
  page.on("response", (response: Response) => {
    const path = pathOf(response.url());
    const method = response.request().method();
    if (response.status() >= 400) {
      httpFailures.push({ method, path, status: response.status() });
    }
    if (path.includes("/api/conversations/") || path === "/api/conversations") {
      events.push({ kind: "response", method, path, status: response.status() });
      if (response.status() >= 400) {
        failedResponses.push({ path, status: response.status() });
      }
    }
  });
  page.on("console", (message) => {
    consoleMessages.push({ type: message.type(), text: message.text() });
  });
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
  });

  await page.route("**/api/python-proxy/characters", (route) =>
    route.fulfill({ json: { characters: ["aoi"], current: "aoi" } }),
  );
  await page.route("**/api/python-proxy/agent-team/config", (route) =>
    route.fulfill({ json: { teams: {} } }),
  );
  await page.route("**/api/python-proxy/llm/execution-profiles", (route) =>
    route.fulfill({
      json: {
        active_profile_id: "manual",
        profiles: [],
        effective_main: { provider: "mock", model: "mock-model" },
      },
    }),
  );
  await page.route("**/api/python-proxy/llm/session-settings*", (route) =>
    route.fulfill({
      json: {
        settings: {
          main_route: { provider: "mock", model: "mock-model" },
          special_routing: {},
        },
        effective_main: { provider: "mock", model: "mock-model" },
      },
    }),
  );

  await page.route("**/api/conversations", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        json: { conversations: [sessions[SESSION_A], sessions[SESSION_B]], total: 2 },
      });
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/conversations/*/resume**", async (route) => {
    const id = conversationId(pathOf(route.request().url()));
    counts.resume[id] = (counts.resume[id] ?? 0) + 1;
    const target = sessions[id];
    if (!target) {
      await route.fulfill({ status: 404, json: { detail: "unknown fixture session" } });
      return;
    }
    await route.fulfill({ json: { session: target, messages: [] } });
  });
  await page.route("**/api/conversations/*/messages**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    const id = conversationId(pathOf(route.request().url()));
    counts.messages[id] = (counts.messages[id] ?? 0) + 1;
    // The cache-poisoning case exhausts the initial 503 retry budget, then
    // returns a permanent 403 from the background recovery read. This is the
    // path that must clear both the rendered cache and its IndexedDB key.
    if (id === SESSION_CACHE) {
      if (counts.messages[id] <= 3) {
        await route.fulfill({
          status: 503,
          json: { detail: "fixture initial history temporarily unavailable" },
        });
        return;
      }
      await route.fulfill({
        status: 403,
        json: { detail: "fixture session access revoked" },
      });
      return;
    }
    if (id === SESSION_B && failNextBMessages) {
      failNextBMessages = false;
      await route.fulfill({ status: 503, json: { detail: "fixture terminal body temporarily unavailable" } });
      return;
    }
    if (id === SESSION_B && reconnectRecoveryArmed) {
      await route.fulfill({
        json: {
          messages: [message("b-recovered", SESSION_B, "assistant", B_RECOVERED_BODY)],
          server_time: "2026-10-18T00:00:02.000Z",
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        messages: messages[id] ?? [],
        server_time: "2026-10-18T00:00:01.000Z",
      },
    });
  });
  await page.route(
    "**/api/python-proxy/conversations/*/generation/status**",
    async (route) => {
      const id = proxyConversationId(pathOf(route.request().url()));
      counts.generation[id] = (counts.generation[id] ?? 0) + 1;
      const terminal = id === SESSION_B && reconnectRecoveryArmed;
      await route.fulfill({
        json: {
          session_id: id,
          running: false,
          status: terminal ? "completed" : "idle",
          updated_at: terminal ? "2026-10-18T00:00:02.000Z" : "2026-10-18T00:00:01.000Z",
        },
      });
    },
  );
  await page.route(
    "**/api/python-proxy/conversations/*/generate-title",
    (route) => route.fulfill({ json: { title: A_UPDATED_TITLE } }),
  );
  await page.route("**/api/conversations/*/related-tasks", (route) =>
    route.fulfill({ json: { tasks: [] } }),
  );

  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
    socketUrls.push(socket.url());
    sockets.push(socket);
  });

  return {
    sockets,
    socketUrls,
    events,
    consoleMessages,
    pageErrors,
    failedResponses,
    httpFailures,
    counts,
    armReconnectRecovery: () => {
      reconnectRecoveryArmed = true;
      failNextBMessages = true;
    },
  };
}

test.describe("Portal会話取得の実ブラウザQA", () => {
  test.use({ viewport: { width: 1440, height: 995 } });

  test("初回messages/generation各1回のままsession title更新を反映する", async ({ page }) => {
    const fixture = await installFixture(page);

    await page.goto(`/chat?s=${SESSION_A}`);
    await expect(page.getByText(A_BODY, { exact: true })).toBeVisible();
    await expect.poll(() => fixture.sockets.length).toBeGreaterThan(0);

    const socket = fixture.sockets.at(-1);
    if (!socket) throw new Error("Chat WebSocket did not connect");
    socket.send(
      JSON.stringify({
        type: "conversation_title_updated",
        session_id: SESSION_A,
        title: A_UPDATED_TITLE,
        event_id: "portal-title-update-1",
      }),
    );
    await expect(sessionItem(page, SESSION_A)).toContainText(A_UPDATED_TITLE, {
      timeout: 10_000,
    });

    // The title event changes the current session object. Wait for the
    // dependent effects to settle before asserting that it did not cause a
    // second history/status read.
    await page.waitForTimeout(700);
    const evidence = {
      url: page.url(),
      counts: fixture.counts,
      socketUrls: fixture.socketUrls,
      events: fixture.events,
      failedResponses: fixture.failedResponses,
      httpFailures: fixture.httpFailures,
      consoleMessages: fixture.consoleMessages,
      pageErrors: fixture.pageErrors,
      bodyVisible: await page.getByText(A_BODY, { exact: true }).isVisible(),
      titleVisible: await sessionItem(page, SESSION_A).getByText(A_UPDATED_TITLE, { exact: true }).isVisible(),
    };
    await writeEvidence("initial-title-update.json", evidence);

    expect(fixture.counts.messages[SESSION_A]).toBe(1);
    expect(fixture.counts.generation[SESSION_A]).toBe(1);
    expect(evidence.bodyVisible).toBe(true);
    expect(evidence.titleVisible).toBe(true);
    expect(fixture.pageErrors).toEqual([]);
    expect(
      unexpectedConsoleErrors(fixture.consoleMessages),
    ).toEqual([]);
  });

  test("隣接会話へ切替後、WS再接続とterminal本文503から本文表示を復旧する", async ({ page }) => {
    const fixture = await installFixture(page);

    await page.goto(`/chat?s=${SESSION_A}`);
    await expect(page.getByText(A_BODY, { exact: true })).toBeVisible();
    await expect.poll(() => fixture.sockets.length).toBeGreaterThan(0);

    const adjacent = sessionItem(page, SESSION_B).locator(
      'button[data-sidebar="menu-button"]',
    );
    await expect(adjacent).toBeVisible();
    await adjacent.click();
    await expect(page).toHaveURL(new RegExp(`[?&]s=${SESSION_B}(?:&|$)`));
    await expect(page.getByText(B_BODY, { exact: true })).toBeVisible();
    await expect.poll(() => fixture.sockets.length).toBeGreaterThan(1);

    fixture.armReconnectRecovery();
    const activeSocket = fixture.sockets.at(-1);
    if (!activeSocket) throw new Error("Adjacent chat WebSocket did not connect");
    activeSocket.close();

    await expect.poll(() => fixture.sockets.length, { timeout: 10_000 }).toBeGreaterThan(2);
    await expect(page.getByText(B_RECOVERED_BODY, { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(A_BODY, { exact: true })).toHaveCount(0);

    const countsAfterRecovery = {
      messages: fixture.counts.messages[SESSION_B],
      generation: fixture.counts.generation[SESSION_B],
    };
    // A successful terminal body read ends recovery polling. Wait longer than
    // the two-second recovery cadence and verify that no follow-up GETs land.
    await page.waitForTimeout(2_500);
    const countsAfterStabilityWindow = {
      messages: fixture.counts.messages[SESSION_B],
      generation: fixture.counts.generation[SESSION_B],
    };

    const evidence = {
      url: page.url(),
      counts: fixture.counts,
      socketUrls: fixture.socketUrls,
      events: fixture.events,
      failedResponses: fixture.failedResponses,
      httpFailures: fixture.httpFailures,
      consoleMessages: fixture.consoleMessages,
      pageErrors: fixture.pageErrors,
      bodyVisible: await page.getByText(B_RECOVERED_BODY, { exact: true }).isVisible(),
      staleBodyCount: await page.getByText(A_BODY, { exact: true }).count(),
      socketCount: fixture.sockets.length,
      countsAfterRecovery,
      countsAfterStabilityWindow,
    };
    await writeEvidence("adjacent-reconnect-recovery.json", evidence);

    expect(fixture.failedResponses).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          path: `/api/conversations/${SESSION_B}/messages`,
          status: 503,
        }),
      ]),
    );
    expect(fixture.counts.messages[SESSION_B]).toBeGreaterThanOrEqual(3);
    expect(fixture.counts.generation[SESSION_B]).toBeGreaterThanOrEqual(2);
    expect(evidence.bodyVisible).toBe(true);
    expect(evidence.staleBodyCount).toBe(0);
    expect(evidence.countsAfterStabilityWindow).toEqual(evidence.countsAfterRecovery);
    expect(fixture.pageErrors).toEqual([]);
    expect(
      unexpectedConsoleErrors(
        fixture.consoleMessages,
        new Set([
          "Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
        ]),
      ),
    ).toEqual([]);
  });

  test("初期503後の背景403で本文とIndexedDB cacheを消去し、再訪で再hydrateしない", async ({ page }) => {
    const fixture = await installFixture(page);
    const cachedMessage = message("cache-cached", SESSION_CACHE, "assistant", CACHE_BODY);

    await seedChatSnapshot(page, SESSION_CACHE, {
      messages: [cachedMessage],
      serverTime: "2026-10-18T00:00:01.000Z",
    });
    const seededSnapshot = await readChatSnapshot(page, SESSION_CACHE);
    expect(seededSnapshot?.messages[0]?.id).toBe(cachedMessage.id);

    await page.goto(`/chat?s=${SESSION_CACHE}`);
    await expect(page.getByText(CACHE_BODY, { exact: true })).toBeVisible({
      timeout: 10_000,
    });
    await expect(
      page.getByText("この会話を表示する権限がありません。", { exact: true }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(CACHE_BODY, { exact: true })).toHaveCount(0);

    const snapshotAfterForbidden = await readChatSnapshot(page, SESSION_CACHE);
    await page.reload();
    await expect(
      page.getByText("この会話を表示する権限がありません。", { exact: true }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(CACHE_BODY, { exact: true })).toHaveCount(0);
    const snapshotAfterReload = await readChatSnapshot(page, SESSION_CACHE);

    const evidence = {
      url: page.url(),
      counts: fixture.counts,
      events: fixture.events,
      failedResponses: fixture.failedResponses,
      httpFailures: fixture.httpFailures,
      consoleMessages: fixture.consoleMessages,
      pageErrors: fixture.pageErrors,
      seededSnapshot,
      snapshotAfterForbidden,
      snapshotAfterReload,
      bodyVisibleAfterForbidden: await page.getByText(CACHE_BODY, { exact: true }).count(),
      permissionErrorVisible: await page
        .getByText("この会話を表示する権限がありません。", { exact: true })
        .isVisible(),
    };
    await writeEvidence("cache-forbidden-eviction.json", evidence);

    expect(fixture.failedResponses).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          path: `/api/conversations/${SESSION_CACHE}/messages`,
          status: 503,
        }),
        expect.objectContaining({
          path: `/api/conversations/${SESSION_CACHE}/messages`,
          status: 403,
        }),
      ]),
    );
    expect(fixture.counts.messages[SESSION_CACHE]).toBeGreaterThanOrEqual(5);
    expect(snapshotAfterForbidden).toBeNull();
    expect(snapshotAfterReload).toBeNull();
    expect(evidence.bodyVisibleAfterForbidden).toBe(0);
    expect(evidence.permissionErrorVisible).toBe(true);
    expect(fixture.pageErrors).toEqual([]);
    expect(
      unexpectedConsoleErrors(
        fixture.consoleMessages,
        new Set([
          "Failed to load resource: the server responded with a status of 404 (Not Found)",
          "Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
          "Failed to load resource: the server responded with a status of 403 (Forbidden)",
        ]),
        ["セッション再開失敗: ChatApiError: fixture session access revoked"],
      ),
    ).toEqual([]);
  });
});
