import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const BASE_PATH = (
  process.env.PLAYWRIGHT_BASE_PATH?.trim() ||
  process.env.NEXT_PUBLIC_BASE_PATH?.trim() ||
  ""
).replace(/^\/+|\/+$/g, "");
const tasksPath = `${BASE_PATH ? `/${BASE_PATH}` : ""}/tasks`;

const project = {
  id: "cold-detail-project",
  name: "Cold detail project",
  slug: "cold-detail-project",
  space_id: "cold-detail-space",
  is_member: true,
  can_write: true,
  is_completed: false,
};

function makeTask(id: string) {
  return {
    id,
    project_id: project.id,
    project_name: project.name,
    title: "Cold detail regression task",
    description: "Controlled Playwright task fixture",
    status: "open",
    priority: "medium",
    start_at: null,
    end_at: null,
    all_day: false,
    reminder_offsets: [],
    notifications_enabled: true,
    source: "manual",
    created_by: "e2e-fixture",
    completed_at: null,
    created_at: "2026-10-03T00:00:00.000Z",
    updated_at: "2026-10-03T00:00:00.000Z",
    metadata: {},
    assignees: [],
    tags: [],
    active_time_entry: null,
    estimated_hours: null,
    sort_order: 0,
    total_time_seconds: 0,
    parent_task_id: null,
    subtasks: [],
    comments: [],
    activities: [],
    has_recurrence: false,
  };
}

async function routeTaskFixture(
  page: Page,
  task: ReturnType<typeof makeTask>,
  options: { readOnly?: () => boolean } = {},
) {
  await mockAuthenticatedApis(page);
  let detailReads = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const apiPrefix = BASE_PATH ? `/${BASE_PATH}` : "";
    const pathname = apiPrefix && url.pathname.startsWith(`${apiPrefix}/`)
      ? url.pathname.slice(apiPrefix.length)
      : url.pathname;

    if (pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: "e2e-fixture", username: "__playwright_e2e__", role: "admin" },
        },
      });
      return;
    }
    if (pathname === "/api/spaces") {
      await route.fulfill({
        json: {
          spaces: [
            {
              id: project.space_id,
              name: "Cold detail space",
              slug: "cold-detail-space",
              is_member: true,
              can_write: true,
            },
          ],
          total: 1,
        },
      });
      return;
    }
    if (pathname === "/api/projects") {
      await route.fulfill({
        json: {
          projects: [{ ...project, can_write: !options.readOnly?.() }],
          total: 1,
        },
      });
      return;
    }
    if (pathname === "/api/tasks" && method === "GET") {
      await route.fulfill({ json: [task] });
      return;
    }
    if (pathname === `/api/tasks/${task.id}` && method === "GET") {
      detailReads += 1;
      await route.fulfill({ status: 200, json: task });
      return;
    }
    if (
      pathname === `/api/tasks/${task.id}/attachments` &&
      method === "GET"
    ) {
      await route.fulfill({ json: [] });
      return;
    }
    if (
      pathname === `/api/tasks/${task.id}/references` &&
      method === "GET"
    ) {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/tasks/${task.id}/recurrence`) {
      await route.fulfill({ json: null });
      return;
    }
    if (pathname === `/api/projects/${project.id}/tags`) {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/projects/${project.id}/assignee-candidates`) {
      await route.fulfill({ json: { members: [], total: 0 } });
      return;
    }

    // Keep the shell requests fixture-local when the app is mounted under a
    // configured base path.  The shared helper is retained for legacy
    // unprefixed runners, but this spec owns the prefixed responses it needs.
    if (pathname === "/api/users/list") {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === "/api/notifications") {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === "/api/time-entries/active") {
      await route.fulfill({ json: null });
      return;
    }
    if (pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }
    if (pathname === "/api/users/me/settings") {
      await route.fulfill({ json: { settings: {} } });
      return;
    }
    if (pathname === "/api/python-proxy/health") {
      await route.fulfill({ status: 503, json: { ok: false } });
      return;
    }
    if (pathname === "/api/python-proxy/characters") {
      await route.fulfill({ json: { characters: [], current: "" } });
      return;
    }
    if (pathname === "/api/python-proxy/runtime/features") {
      await route.fulfill({ json: { features: {} } });
      return;
    }
    if (pathname === "/api/python-proxy/llm/engine") {
      await route.fulfill({ json: { available: [], provider: "", model: "" } });
      return;
    }
    if (pathname === "/api/python-proxy/llm/models") {
      await route.fulfill({
        json: {
          current: { provider: "mock", model: "mock-model" },
          providers: [{ id: "mock", label: "Mock", models: [] }],
        },
      });
      return;
    }
    if (pathname === "/api/pets") {
      await route.fulfill({ json: [] });
      return;
    }

    await route.fulfill({ json: {} });
  });
  return { getDetailReads: () => detailReads };
}

async function expectTaskDetail(page: Page, title: string) {
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(title, { exact: true })).toBeVisible();
  return dialog;
}

const responsiveTitles = [
  {
    kind: "identifier",
    title: "QA_TASK_DETAIL_LAYOUT_" + "LONG_UNBROKEN_TITLE_".repeat(8),
  },
  {
    kind: "japanese",
    title: "合成回帰：長い日本語のタスク名も省略せず最後まで読む確認".repeat(5),
  },
  {
    kind: "url",
    title: "https://example.invalid/tasks/" + "long-unbroken-path".repeat(12),
  },
];

for (const width of [390, 1440]) {
  for (const { kind, title } of responsiveTitles) {
    test(`${width}px task detail wraps ${kind} and keeps controls reachable`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      const task = {
        ...makeTask(randomUUID()),
        title,
        description: Array.from(
          { length: 24 },
          (_, index) => `合成の説明${index + 1}行目：縦スクロールで全文を確認します。`,
        ).join("\n"),
      };
      let readOnly = false;
      await addAuthCookie(page);
      await routeTaskFixture(page, task, { readOnly: () => readOnly });
      await page.goto(`${tasksPath}?detail=${encodeURIComponent(task.id)}`);
      const dialog = await expectTaskDetail(page, title);
      const heading = dialog.getByRole("heading", { level: 1 });
      await expect(heading).toHaveText(title);
      const close = dialog.getByRole("button", { name: "Close", exact: true });
      const chat = dialog.getByRole("button", { name: "Open in Chat", exact: true });
      await expect(close).toBeVisible();
      await expect(chat).toBeVisible();

      const geometry = await dialog.evaluate((popup) => {
        const bounds = popup.getBoundingClientRect();
        const heading = popup.querySelector("h1")!;
        const range = document.createRange();
        range.selectNodeContents(heading);
        return {
          pageWidth: document.documentElement.clientWidth,
          pageScrollWidth: document.documentElement.scrollWidth,
          popupWidth: popup.clientWidth,
          popupScrollWidth: popup.scrollWidth,
          titleWidth: heading.clientWidth,
          titleScrollWidth: heading.scrollWidth,
          bounds: { left: bounds.left, right: bounds.right },
          lines: Array.from(range.getClientRects(), (rect) => ({
            left: rect.left,
            right: rect.right,
            bottom: rect.bottom,
          })),
          buttons: Array.from(popup.querySelectorAll("button"))
            .filter((button) => button.getClientRects().length > 0)
            .map((button) => {
              const rect = button.getBoundingClientRect();
              return { left: rect.left, right: rect.right };
            }),
        };
      });
      expect(geometry.pageScrollWidth).toBeLessThanOrEqual(geometry.pageWidth + 1);
      expect(geometry.popupScrollWidth).toBeLessThanOrEqual(geometry.popupWidth + 1);
      expect(geometry.titleScrollWidth).toBeLessThanOrEqual(geometry.titleWidth + 1);
      expect(geometry.lines.length).toBeGreaterThan(1);
      for (const rect of [...geometry.lines, ...geometry.buttons]) {
        expect(rect.left).toBeGreaterThanOrEqual(geometry.bounds.left);
        expect(rect.right).toBeLessThanOrEqual(geometry.bounds.right);
      }
      await testInfo.attach(`task-detail-${width}-${kind}`, {
        body: await page.screenshot(),
        contentType: "image/png",
      });

      await close.focus();
      await page.keyboard.press("Shift+Tab");
      await expect.poll(() => dialog.evaluate((popup) => popup.contains(document.activeElement))).toBe(true);
      await page.keyboard.press("Tab");
      await expect(close).toBeFocused();

      // Keyboard scrolling targets the existing modal body, while Close
      // remains outside that scroll area and inside the viewport.
      await chat.focus();
      await page.keyboard.press("PageDown");
      await expect.poll(() => dialog.evaluate((popup) => Array.from(popup.querySelectorAll("div"))
        .some((element) => element.scrollTop > 0))).toBe(true);
      const closeBounds = await close.boundingBox();
      expect(closeBounds).not.toBeNull();
      expect(closeBounds!.y).toBeGreaterThanOrEqual(0);
      expect(closeBounds!.y + closeBounds!.height).toBeLessThanOrEqual(900);
      await close.click();
      await expect(dialog).toBeHidden();

      // A readable project without write permission opens the same component
      // in read-only mode. The browse link alone does not remove permission.
      readOnly = true;
      await page.goto(`${tasksPath}?detail=${encodeURIComponent(task.id)}&browse_project_id=${project.id}`);
      const readOnlyDialog = await expectTaskDetail(page, title);
      await expect(readOnlyDialog.getByRole("heading", { level: 1 })).toHaveText(title);
      await expect(readOnlyDialog.getByRole("button", { name: "Open in Chat" })).toHaveCount(0);
      await expect(readOnlyDialog.getByRole("button", { name: "Close", exact: true })).toBeVisible();
      expect(await readOnlyDialog.evaluate((popup) => popup.scrollWidth <= popup.clientWidth + 1)).toBe(true);
      await page.keyboard.press("Escape");
      await expect(readOnlyDialog).toBeHidden();
    });
  }
}

test("cold detail URL opens after API 200 and survives reload/back workflow", async ({
  browser,
}) => {
  const task = makeTask(randomUUID());
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await addAuthCookie(page);
    const fixture = await routeTaskFixture(page, task);

    // Seed browser history in this newly opened tab, then open the same
    // readable UUID as a cold deep link.
    await page.goto(tasksPath);
    await expect(page.getByTestId(`task-row-${task.id}`)).toBeVisible();
    await page.goto(`${tasksPath}?detail=${encodeURIComponent(task.id)}`);

    await expectTaskDetail(page, task.title);
    await expect.poll(fixture.getDetailReads).toBeGreaterThan(0);
    await expect
      .poll(() => new URL(page.url()).search)
      .toBe("");

    // Reload after the detail intent has been consumed returns to the list;
    // it must not leave a half-open modal or a stale selected task behind.
    await page.reload();
    await expect(page.getByTestId(`task-row-${task.id}`)).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // Re-enter through the deep link, close the modal, and use browser Back.
    // The previous list selection remains available and the modal closes.
    await page.goto(`${tasksPath}?detail=${encodeURIComponent(task.id)}`);
    const dialog = await expectTaskDetail(page, task.title);
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).toBeHidden();
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`${tasksPath.replace("/", "\\/")}$`));
    await expect(page.getByTestId(`task-row-${task.id}`)).toBeVisible();
  } finally {
    await context.close();
  }
});
