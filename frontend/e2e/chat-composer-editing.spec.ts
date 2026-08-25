import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const E2E_SNIPPET = {
  prefix: "E2E_SNIPPET",
  body: "const expandedSnippet = true;",
  description: "composer E2E snippet",
};

const BASE_FENCED_MESSAGE = [
  "前置テキスト",
  "```typescript",
  "const result = /slash @mention # heading;",
  "```",
  "後置テキスト",
].join("\n");

type BrowserEvidence = {
  consoleErrors: string[];
  expectedConsoleErrors: string[];
  pageErrors: string[];
  requestFailures: string[];
  expectedRequestFailures: string[];
  httpErrors: string[];
};

const evidenceByPage = new WeakMap<Page, BrowserEvidence>();

function collectBrowserEvidence(page: Page) {
  const evidence: BrowserEvidence = {
    consoleErrors: [],
    expectedConsoleErrors: [],
    pageErrors: [],
    requestFailures: [],
    expectedRequestFailures: [],
    httpErrors: [],
  };
  evidenceByPage.set(page, evidence);

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    // The shared fixture intentionally reports Python health as unavailable;
    // retain that console observation separately from unexpected UI errors.
    if (message.text().includes("status of 503 (Service Unavailable)")) {
      evidence.expectedConsoleErrors.push(message.text());
    } else {
      evidence.consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    evidence.pageErrors.push(error.stack || error.message);
  });
  page.on("requestfailed", (request) => {
    const url = request.url();
    // The Python/WebSocket runtime is intentionally unavailable in this
    // fixture. Keep those expected connection failures as evidence, but do
    // not treat them as composer regressions.
    if (url.startsWith("ws:") || url.startsWith("wss:")) return;
    // Next App Router cancels speculative RSC prefetches during the sidebar
    // navigation. They are expected browser events, not failed API calls.
    if (request.failure()?.errorText === "net::ERR_ABORTED" && !url.includes("/api/")) {
      evidence.expectedRequestFailures.push(`${request.method()} ${url}: net::ERR_ABORTED`);
      return;
    }
    evidence.requestFailures.push(`${request.method()} ${url}: ${request.failure()?.errorText ?? "unknown"}`);
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() >= 500 && !url.includes("/api/python-proxy/health")) {
      evidence.httpErrors.push(`${response.status()} ${url}`);
    }
  });
}

async function installComposerFixtures(page: Page) {
  // `mockAuthenticatedApis` provides the common authenticated shell routes;
  // this route only adds a deterministic snippet so code-block suppression is
  // tested even when the real settings store is empty.
  await page.route("**/api/users/me/settings", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        json: {
          settings: {
            snippets: [E2E_SNIPPET],
          },
        },
      });
      return;
    }
    await route.fulfill({ json: { settings: { snippets: [E2E_SNIPPET] } } });
  });

  // The sidebar's regular new-chat path is fail-closed unless the current
  // character and an authoritative Provider/Model route are available.
  await page.route("**/api/python-proxy/characters", async (route) => {
    await route.fulfill({ json: { characters: ["aoi"], current: "aoi" } });
  });
  await page.route("**/api/python-proxy/llm/new-chat-defaults", async (route) => {
    await route.fulfill({
      json: {
        last_used_main: { provider: "mock", model: "mock-model" },
        effective_main: { provider: "mock", model: "mock-model" },
      },
    });
  });
  await page.route("**/api/python-proxy/llm/session-settings", async (route) => {
    await route.fulfill({
      json: {
        settings: {
          agent_team_selection: {
            mode: "auto",
            team_id: "",
            loaded_team_ids: [],
          },
          main_route: { provider: "mock", model: "mock-model" },
          special_routing: {},
          execution_profile_id: "",
        },
        effective_main: { provider: "mock", model: "mock-model" },
      },
    });
  });
  await page.route(
    "**/api/python-proxy/conversations/session-e2e/generation/status",
    async (route) => {
      await route.fulfill({
        json: {
          session_id: "session-e2e",
          running: false,
          status: "idle",
          updated_at: "2026-08-23T00:00:00.000Z",
        },
      });
    },
  );
}

async function openNewComposer(page: Page) {
  collectBrowserEvidence(page);
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await installComposerFixtures(page);

  const settingsResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/api/users/me/settings") &&
      response.request().method() === "GET",
  );
  await page.goto("/chat");
  await settingsResponse;

  const resumeResponse = page.waitForResponse(
    (response) => response.url().includes("/api/conversations/session-e2e/resume"),
  );
  await page.getByRole("button", { name: "新規会話", exact: true }).click();
  await expect(page).toHaveURL(/\/chat\?s=session-e2e$/);
  await resumeResponse;

  const input = page.getByRole("textbox", { name: "メッセージ入力" });
  await expect(input).toBeVisible();
  await expect(page.locator('[data-chat-composer-editor="true"] textarea')).toHaveCount(1);
  await input.evaluate((element) => {
    element.setAttribute("data-e2e-input-identity", "stable");
  });
  return input;
}

async function putCaretAt(input: ReturnType<Page["getByRole"]>, position: number) {
  await input.evaluate((element, nextPosition) => {
    const textarea = element as HTMLTextAreaElement;
    textarea.focus();
    textarea.setSelectionRange(nextPosition, nextPosition);
    textarea.dispatchEvent(new Event("select", { bubbles: true }));
  }, position);
}

async function expectStableComposerInput(
  input: ReturnType<Page["getByRole"]>,
) {
  await expect(input).toHaveAttribute("data-e2e-input-identity", "stable");
  await expect(input).toBeFocused();
}

function composerPopups(page: Page) {
  // Both slash and mention menus are rendered as an absolute bottom-full
  // sibling of the native textarea editor.
  return page
    .locator('[data-chat-composer-editor="true"]')
    .locator("xpath=..")
    .locator("div.absolute.bottom-full");
}

test.afterEach(async ({ page }, testInfo) => {
  const evidence = evidenceByPage.get(page);
  if (!evidence) return;

  const summary = JSON.stringify(evidence, null, 2);
  await testInfo.attach("browser-evidence.json", {
    body: Buffer.from(summary, "utf8"),
    contentType: "application/json",
  });
  // Keep the observed console/network evidence visible in list/CI output.
  console.log(`[chat-composer-editing evidence] ${summary}`);

  expect(evidence.pageErrors, "ブラウザ pageerror").toEqual([]);
  expect(evidence.consoleErrors, "予期しないBrowser Console error").toEqual([]);
  expect(evidence.requestFailures, "失敗したHTTPリクエスト").toEqual([]);
  expect(evidence.httpErrors, "HTTP 5xx（意図したhealth除外）").toEqual([]);
});

test.describe("chat composer fenced editing", () => {
  test("keeps the complete fenced draft in one native textarea and preserves editing focus", async ({ page }) => {
    const input = await openNewComposer(page);
    await input.fill(BASE_FENCED_MESSAGE);
    await expect(input).toHaveValue(BASE_FENCED_MESSAGE);

    await input.press("Control+Home");
    expect(await input.evaluate((element) => [element.selectionStart, element.selectionEnd])).toEqual([0, 0]);
    await input.press("Control+End");
    expect(await input.evaluate((element) => [element.selectionStart, element.selectionEnd])).toEqual([
      BASE_FENCED_MESSAGE.length,
      BASE_FENCED_MESSAGE.length,
    ]);

    const codePosition = BASE_FENCED_MESSAGE.indexOf("const result");
    await putCaretAt(input, codePosition);
    const textareaValueBeforeArrows = await input.inputValue();
    await input.press("ArrowDown");
    await input.press("ArrowUp");
    await expectStableComposerInput(input);
    await expect(input).toHaveValue(textareaValueBeforeArrows);

    await input.press("Control+A");
    expect(await input.evaluate((element) => [element.selectionStart, element.selectionEnd])).toEqual([
      0,
      BASE_FENCED_MESSAGE.length,
    ]);

    await input.press("End");
    await input.pressSequentially("!");
    await expect(input).toHaveValue(`${BASE_FENCED_MESSAGE}!`);
    await input.press("Control+z");
    await expect(input).toHaveValue(BASE_FENCED_MESSAGE);
    await expectStableComposerInput(input);

    await input.press("Control+End");
    await input.press("Shift+Enter");
    await expect(input).toHaveValue(`${BASE_FENCED_MESSAGE}\n`);
    await expectStableComposerInput(input);
  });

  test("does not activate slash, mention, snippet, or heading shortcuts inside code", async ({ page }) => {
    const input = await openNewComposer(page);
    const popups = composerPopups(page);
    await input.fill(BASE_FENCED_MESSAGE);
    const codePosition = BASE_FENCED_MESSAGE.indexOf("const result");

    await putCaretAt(input, codePosition);
    await input.pressSequentially("/");
    await expect(popups).toHaveCount(0);
    await expect(page.getByPlaceholder("コマンド検索..."))
      .toHaveCount(0);

    await input.fill(BASE_FENCED_MESSAGE);
    await putCaretAt(input, codePosition);
    await input.pressSequentially("@");
    await expect(popups).toHaveCount(0);

    await input.fill(BASE_FENCED_MESSAGE);
    await putCaretAt(input, codePosition);
    await input.pressSequentially(E2E_SNIPPET.prefix);
    await expect(page.getByText(E2E_SNIPPET.prefix, { exact: true })).toHaveCount(0);

    await input.fill(BASE_FENCED_MESSAGE);
    await putCaretAt(input, codePosition);
    const valueBeforeHeadingShortcut = await input.inputValue();
    await input.press("Control+Shift+BracketRight");
    await expect(input).toHaveValue(valueBeforeHeadingShortcut);
    await expectStableComposerInput(input);
  });

  test("retains existing outside-code menu, heading shortcut, newline, and Enter send behavior", async ({ page }) => {
    const input = await openNewComposer(page);
    const popups = composerPopups(page);
    let dispatchRequests = 0;
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        request.url().includes("/api/python-proxy/conversations/session-e2e/dispatch")
      ) {
        dispatchRequests += 1;
      }
    });

    await input.fill("/");
    await expect(page.getByPlaceholder("コマンド検索...")).toBeVisible();
    await input.press("Escape");
    await expect(page.getByPlaceholder("コマンド検索...")).toHaveCount(0);
    await expect(popups).toHaveCount(0);

    await input.fill("見出し");
    await input.press("Control+Shift+BracketRight");
    await expect(input).toHaveValue("# 見出し");

    await input.fill("Shift+Enter の本文");
    await input.press("End");
    await input.press("Shift+Enter");
    await expect(input).toHaveValue("Shift+Enter の本文\n");
    expect(dispatchRequests, "Shift+Enter は送信しない").toBe(0);

    const sentContent = "E2E plain Enter送信";
    await input.fill(sentContent);
    await expect(page.getByTitle("送信")).toBeEnabled();
    const dispatchResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/conversations/session-e2e/dispatch") &&
        response.request().method() === "POST",
    );
    await input.press("Enter");
    await dispatchResponse;
    await expect(
      page.getByTestId("chat-message-list").getByText(sentContent, { exact: true }),
    ).toBeVisible();
  });
});
