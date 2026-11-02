import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

const SESSION_A = "long-history-session-a";
const SESSION_B = "long-history-session-b";
const BASE_COUNT = 320;
const PAGE_SIZE = 10;
const START_TIME = Date.parse("2026-01-01T00:00:00.000Z");
const DELTA_MESSAGE_ID = `long-${BASE_COUNT}`;

type FixtureMessage = {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  branch_index: number;
  branch_count?: number;
  is_active_branch: boolean;
};

type MessageRequest = {
  query: string;
  status: number;
};

type LongHistoryFixture = {
  requests: MessageRequest[];
  sockets: WebSocketRoute[];
  enableDelta: () => void;
  staleNextCursor: () => void;
  pauseImageLoad: () => void;
  waitForImageRequest: () => Promise<void>;
  releaseImageLoad: () => void;
};

function makeMessage(index: number, branch: 0 | 1 = 0): FixtureMessage {
  const createdAt = new Date(START_TIME + index * 60_000).toISOString();
  const assistant = index % 2 === 1;
  const longBody = index % 11 === 0;
  const content = index === 311
    ? "fixture image-height anchor\n\n![Delayed fixture image](/long-history.svg)"
    : longBody
      ? `fixture message ${index}: ${"長文データを含む仮想化確認用テキスト。".repeat(18)}`
      : `fixture message ${index}`;
  return {
    id: `${branch === 0 ? "long" : "alt"}-${String(index).padStart(3, "0")}`,
    session_id: SESSION_A,
    role: assistant ? "assistant" : "user",
    content,
    metadata: {
      ...(index % 37 === 0
        ? { attachments: [{ name: `fixture-${index}.pdf`, kind: "attachment", size: 2048 }] }
        : {}),
    },
    created_at: createdAt,
    updated_at: createdAt,
    branch_index: branch,
    ...(assistant ? { branch_count: 2 } : {}),
    is_active_branch: true,
  };
}

function cursorFor(message: FixtureMessage) {
  return Buffer.from(
    JSON.stringify({ v: 1, s: SESSION_A, t: message.created_at, i: message.id }),
    "utf8",
  ).toString("base64url");
}

function makeSession(id: string, title: string, messageCount: number) {
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

async function installLongHistoryFixture(page: Page): Promise<LongHistoryFixture> {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  const requests: MessageRequest[] = [];
  const sockets: WebSocketRoute[] = [];
  const primary = Array.from({ length: BASE_COUNT }, (_, index) => makeMessage(index));
  const alternate = Array.from({ length: BASE_COUNT + 1 }, (_, index) => makeMessage(index, 1));
  const newMessage: FixtureMessage = {
    ...makeMessage(BASE_COUNT),
    id: DELTA_MESSAGE_ID,
    content: "new message from since delta",
  };
  const sessions = [
    makeSession(SESSION_A, "Long history A", BASE_COUNT + 1),
    makeSession(SESSION_B, "Short history B", 2),
  ];
  let activeBranch: 0 | 1 = 0;
  let deltaEnabled = false;
  let staleCursor = false;
  let imageLoadPaused = false;
  let notifyImageRequest!: () => void;
  let releaseImage!: () => void;
  const imageRequested = new Promise<void>((resolve) => { notifyImageRequest = resolve; });
  const imageGate = new Promise<void>((resolve) => { releaseImage = resolve; });

  await page.route("**/api/python-proxy/health", (route) =>
    route.fulfill({ json: { status: "ok" } }),
  );
  await page.route("**/long-history.svg", async (route) => {
    if (imageLoadPaused) {
      notifyImageRequest();
      await imageGate;
    }
    return route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="640"><rect width="300" height="640" fill="#dbeafe"/></svg>',
    });
  });
  await page.route("**/api/python-proxy/characters", (route) =>
    route.fulfill({ json: { characters: ["aoi"], current: "aoi" } }),
  );
  await page.route("**/api/python-proxy/agent-team/config", (route) =>
    route.fulfill({ json: { teams: {} } }),
  );
  await page.route("**/api/python-proxy/llm/execution-profiles", (route) =>
    route.fulfill({ json: { active_profile_id: "manual", profiles: [], effective_main: { provider: "mock", model: "mock" } } }),
  );
  await page.route("**/api/python-proxy/llm/session-settings*", (route) =>
    route.fulfill({ json: { settings: { main_route: { provider: "mock", model: "mock" }, special_routing: {} }, effective_main: { provider: "mock", model: "mock" } } }),
  );
  await page.route("**/api/python-proxy/conversations/*/generation/status**", (route) => {
    const sessionId = new URL(route.request().url()).pathname.split("/").at(-3);
    return route.fulfill({
      json: {
        session_id: sessionId,
        running: false,
        status: "idle",
        updated_at: "2026-10-27T00:00:00.000Z",
      },
    });
  });
  await page.route("**/api/conversations", (route) =>
    route.fulfill({ json: { conversations: sessions, total: sessions.length } }),
  );
  await page.route("**/api/conversations/*/related-tasks", (route) =>
    route.fulfill({ json: { tasks: [] } }),
  );
  await page.route("**/api/conversations/*/resume**", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-2) ?? "";
    const session = sessions.find((candidate) => candidate.id === id);
    if (!session) return route.fulfill({ status: 404, json: { detail: "unknown fixture session" } });
    const messages = id === SESSION_B
      ? [
          { id: "short-user", session_id: SESSION_B, role: "user", content: "B user", metadata: {}, branch_index: 0, is_active_branch: true },
          { id: "short-assistant", session_id: SESSION_B, role: "assistant", content: "B assistant", metadata: {}, branch_index: 0, is_active_branch: true },
        ]
      : [];
    return route.fulfill({ json: { session, messages } });
  });
  await page.route("**/api/conversations/*/messages**", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const url = new URL(route.request().url());
    const sessionId = url.pathname.split("/").at(-2) ?? "";
    const query = url.search.slice(1);
    const before = url.searchParams.get("before");
    const since = url.searchParams.get("since");
    if (sessionId !== SESSION_A) {
      requests.push({ query, status: 200 });
      return route.fulfill({ json: { messages: [], server_time: null, has_more: false, next_cursor: null } });
    }
    const current = activeBranch === 0 ? primary : alternate;
    if (since) {
      requests.push({ query, status: 200 });
      return route.fulfill({
        json: {
          messages: deltaEnabled ? [newMessage] : [],
          server_time: deltaEnabled ? newMessage.updated_at : since,
          has_more: false,
          next_cursor: null,
        },
      });
    }
    if (before) {
      if (staleCursor) {
        staleCursor = false;
        requests.push({ query, status: 409 });
        return route.fulfill({
          status: 409,
          json: { error: { code: "history_cursor_stale", message: "fixture branch changed" } },
        });
      }
      const decoded = JSON.parse(Buffer.from(before, "base64url").toString("utf8")) as { i: string };
      const cursorIndex = current.findIndex((message) => message.id === decoded.i);
      if (cursorIndex < 0) {
        requests.push({ query, status: 409 });
        return route.fulfill({ status: 409, json: { error: { code: "history_cursor_stale" } } });
      }
      const start = Math.max(0, cursorIndex - PAGE_SIZE);
      const messages = current.slice(start, cursorIndex);
      const hasMore = start > 0;
      requests.push({ query, status: 200 });
      return route.fulfill({
        json: {
          messages,
          server_time: current.at(-1)?.updated_at ?? null,
          has_more: hasMore,
          next_cursor: hasMore && messages[0] ? cursorFor(messages[0]) : null,
        },
      });
    }
    const messages = current.slice(-PAGE_SIZE);
    requests.push({ query, status: 200 });
    return route.fulfill({
      json: {
        messages,
        server_time: current.at(-1)?.updated_at ?? null,
        has_more: current.length > PAGE_SIZE,
        next_cursor: current.length > PAGE_SIZE ? cursorFor(messages[0]) : null,
        ...(url.searchParams.get("history_index") === "1"
          ? {
              history_index: current.map((message, index) => ({
                id: message.id,
                role: message.role,
                position: index + 1,
                preview: message.role === "user" ? `メッセージ ${index + 1}` : "",
                created_at: message.created_at,
              })),
            }
          : {}),
      },
    });
  });
  await page.route("**/api/python-proxy/conversations/*/messages/*/switch-branch", async (route) => {
    activeBranch = 1;
    staleCursor = true;
    await route.fulfill({ json: { success: true } });
  });
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => sockets.push(socket));

  return {
    requests,
    sockets,
    enableDelta: () => { deltaEnabled = true; },
    staleNextCursor: () => { staleCursor = true; },
    pauseImageLoad: () => { imageLoadPaused = true; },
    waitForImageRequest: () => imageRequested,
    releaseImageLoad: () => releaseImage(),
  };
}

async function visibleAnchor(page: Page) {
  return page.getByTestId("chat-message-list").evaluate((feed) => {
    const feedRect = feed.getBoundingClientRect();
    const row = [...feed.querySelectorAll<HTMLElement>("[data-chat-message-id]")]
      .find((element) => {
        const rect = element.getBoundingClientRect();
        return rect.bottom > feedRect.top && rect.top < feedRect.bottom;
      });
    if (!row) return null;
    return {
      id: row.dataset.chatMessageId ?? "",
      offset: row.getBoundingClientRect().top - feedRect.top,
    };
  });
}

test.describe("長い会話のページングと仮想化", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("10件から複数ページを追加し、差分・409リセット・A→B→Aを維持する", async ({ page }) => {
    const fixture = await installLongHistoryFixture(page);
    fixture.pauseImageLoad();
    await page.goto(`/chat?s=${SESSION_A}`);

    const feed = page.getByTestId("chat-message-list");
    await expect(page.getByText("fixture message 319", { exact: true })).toBeVisible();
    expect(fixture.requests.filter((request) => !new URLSearchParams(request.query).has("before") && !new URLSearchParams(request.query).has("since"))).toHaveLength(1);
    expect(new URLSearchParams(fixture.requests[0]?.query ?? "").get("limit")).toBe("10");
    await expect(feed).toHaveAttribute("role", "feed");
    await expect(feed.locator("[role=article]")).toHaveCount(10);

    await fixture.waitForImageRequest();
    await feed.evaluate((element) => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event("scroll")); });
    const imageRow = page.locator('[data-chat-message-id="long-311"]');
    await expect.poll(() => imageRow.evaluate((row) => {
      const feedRect = row.closest("[data-testid=chat-message-list]")!.getBoundingClientRect();
      return row.getBoundingClientRect().bottom < feedRect.top;
    })).toBe(true);
    const anchorBeforeImage = await visibleAnchor(page);
    fixture.releaseImageLoad();
    await expect(page.locator('img[alt="Delayed fixture image"]')).toHaveJSProperty("naturalHeight", 640);
    const anchorAfterImage = await visibleAnchor(page);
    expect(anchorAfterImage?.id).toBe(anchorBeforeImage?.id);
    expect(Math.abs((anchorAfterImage?.offset ?? 0) - (anchorBeforeImage?.offset ?? 0))).toBeLessThanOrEqual(2);

    // Measure a real, visible row before a keyed prepend. The value comes from
    // browser geometry; assertions allow no more than two CSS pixels of drift.
    await feed.evaluate((element) => { element.scrollTop = 240; });
    await page.waitForTimeout(100);
    const anchorBefore = await visibleAnchor(page);
    expect(anchorBefore).not.toBeNull();
    const loadButton = page.getByTestId("chat-load-older");
    await loadButton.evaluate((button) => (button as HTMLButtonElement).click());
    await expect.poll(() => fixture.requests.filter((request) => new URLSearchParams(request.query).has("before")).length).toBe(1);
    await page.waitForTimeout(150);
    const anchorAfter = await page.locator(`[data-chat-message-id="${anchorBefore?.id}"]`).evaluate((row) => {
      const feedRect = row.closest("[data-testid=chat-message-list]")!.getBoundingClientRect();
      return row.getBoundingClientRect().top - feedRect.top;
    });
    expect(Math.abs(anchorAfter - (anchorBefore?.offset ?? 0))).toBeLessThanOrEqual(2);

    for (let pageNumber = 2; pageNumber <= 5; pageNumber += 1) {
      const previousPages = fixture.requests.filter((request) => new URLSearchParams(request.query).has("before")).length;
      await loadButton.evaluate((button) => (button as HTMLButtonElement).click());
      await expect.poll(() => fixture.requests.filter((request) => new URLSearchParams(request.query).has("before")).length).toBe(previousPages + 1);
      await page.waitForTimeout(60);
    }
    const domMetrics = await page.evaluate(() => ({
      rows: document.querySelectorAll("[data-chat-message-id]").length,
      articles: [...document.querySelectorAll<HTMLElement>("[role=article]")].map((row) => row.getAttribute("aria-posinset")),
      heapBytes: (performance as Performance & { memory?: { usedJSHeapSize?: number } }).memory?.usedJSHeapSize ?? null,
    }));
    console.log("chat-long-history virtualized metrics", JSON.stringify(domMetrics));
    expect(domMetrics.rows).toBeLessThanOrEqual(40);
    expect(new Set(domMetrics.articles).size).toBe(domMetrics.articles.length);
    await expect(page.getByText("fixture message 270", { exact: true })).toBeVisible();

    // The persisted event requests only a since delta while the reader is away
    // from the bottom; the scroll anchor must stay put until the reader returns.
    await feed.evaluate((element) => { element.scrollTop = 500; element.dispatchEvent(new Event("scroll")); });
    const scrollBeforeDelta = await feed.evaluate((element) => element.scrollTop);
    fixture.enableDelta();
    await expect.poll(() => fixture.sockets.length).toBeGreaterThan(0);
    fixture.sockets.at(-1)?.send(JSON.stringify({
      type: "conversation_persisted",
      session_id: SESSION_A,
      role: "assistant",
      message_id: DELTA_MESSAGE_ID,
      event_id: "long-history-delta-1",
    }));
    await expect.poll(() => fixture.requests.some((request) => new URLSearchParams(request.query).has("since"))).toBe(true);
    await page.waitForTimeout(100);
    expect(await feed.evaluate((element) => element.scrollTop)).toBe(scrollBeforeDelta);

    await feed.evaluate((element) => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event("scroll")); });
    await expect(page.getByText("new message from since delta", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: "次の分岐" }).first().click();
    await expect(page.getByText("fixture message 320", { exact: true })).toBeVisible();
    await loadButton.evaluate((button) => (button as HTMLButtonElement).click());
    await expect.poll(() => fixture.requests.some((request) => request.status === 409)).toBe(true);
    await expect(page.getByText("fixture message 320", { exact: true })).toBeVisible();

    const idsAfterReset = await feed.locator("[data-chat-message-id]").evaluateAll((rows) => rows.map((row) => (row as HTMLElement).dataset.chatMessageId));
    expect(new Set(idsAfterReset).size).toBe(idsAfterReset.length);
    await page.locator(`[data-chat-session-id="${SESSION_B}"]`).click();
    await expect(page.getByText("B assistant", { exact: true })).toBeVisible();
    await page.locator(`[data-chat-session-id="${SESSION_A}"]`).click();
    await expect(page.getByText("fixture message 320", { exact: true })).toBeVisible();
    const idsAfterReturn = await feed.locator("[data-chat-message-id]").evaluateAll((rows) => rows.map((row) => (row as HTMLElement).dataset.chatMessageId));
    expect(new Set(idsAfterReturn).size).toBe(idsAfterReturn.length);
    expect(fixture.requests.filter((request) => new URLSearchParams(request.query).get("limit") === "10").length).toBeGreaterThanOrEqual(3);
  });

  test("?message= の古い対象までcursorをたどって中央へ移動する", async ({ page }) => {
    test.setTimeout(60_000);
    const fixture = await installLongHistoryFixture(page);
    await page.goto(`/chat?s=${SESSION_A}&message=long-050`);

    const target = page.locator('[data-chat-message-id="long-050"]');
    await expect(target).toBeInViewport({ timeout: 45_000 });
    await expect.poll(() => fixture.requests.filter((request) => new URLSearchParams(request.query).has("before")).length, {
      timeout: 45_000,
    }).toBeGreaterThanOrEqual(26);
    const centered = await target.evaluate((row) => {
      const feed = row.closest("[data-testid=chat-message-list]")!;
      const rowRect = row.getBoundingClientRect();
      const feedRect = feed.getBoundingClientRect();
      return Math.abs((rowRect.top + rowRect.height / 2) - (feedRect.top + feedRect.height / 2));
    });
    expect(centered).toBeLessThan(140);

    // The rail index contains IDs outside the loaded window, and selections
    // use the same paged navigation path as ?message=.
    const railTarget = page.locator('[data-testid="chat-message-history-item"][data-message-id="long-040"]');
    await expect(railTarget).toBeVisible();
    await railTarget.click();
    await expect(page.locator('[data-chat-message-id="long-040"]')).toBeInViewport({ timeout: 10_000 });
    await expect.poll(() => fixture.requests.filter((request) => new URLSearchParams(request.query).has("before")).length).toBeGreaterThanOrEqual(27);
  });
});
