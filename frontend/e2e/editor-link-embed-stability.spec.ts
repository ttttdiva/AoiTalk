import { expect, test, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const nearX = "https://x.com/aoitalk-e2e/status/700000000000000101";
const nearNormal = "https://near.example.test/ogp-card";
const farX = "https://x.com/aoitalk-e2e/status/700000000000000102";
const farNormal = "https://far.example.test/ogp-card";

const description = [
  nearNormal,
  nearX,
  ...Array.from({ length: 4 }, (_, index) => `near filler line ${index + 1}`),
  "ANCHOR-MARKER",
  ...Array.from({ length: 180 }, (_, index) => `far filler line ${index + 1}`),
  farX,
  farNormal,
].join("\n");

const project = {
  id: "project-link-embed",
  name: "Link Embed QA Project",
  slug: "link-embed-qa",
  space_id: "space-link-embed",
  is_completed: false,
  source: "manual",
  can_write: true,
};

const task = {
  id: "task-link-embed-stability",
  project_id: project.id,
  project_name: project.name,
  title: "リンク埋め込みレイアウト安定性",
  description,
  status: "open",
  priority: "medium",
  start_at: null,
  end_at: null,
  all_day: false,
  reminder_offsets: [],
  notifications_enabled: true,
  source: "manual",
  created_by: "user-1",
  created_at: "2026-08-20T00:00:00.000Z",
  updated_at: "2026-08-20T00:00:00.000Z",
  metadata: {},
  assignees: [],
  tags: [],
  active_time_entry: null,
  estimated_hours: null,
  parent_task_id: null,
  subtasks: [],
  comments: [],
  activities: [],
  has_recurrence: false,
  total_time_seconds: 0,
  sort_order: 0,
};

type OgsRequestState = {
  requests: string[];
};

async function installTaskRoutes(
  page: Page,
  taskDescription = description,
): Promise<OgsRequestState> {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  const ogpState: OgsRequestState = { requests: [] };
  const taskResponse = { ...task, description: taskDescription };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/spaces" && method === "GET") {
      await route.fulfill({
        json: {
          spaces: [
            {
              id: "space-link-embed",
              name: "Link Embed QA Space",
              slug: "link-embed-qa",
              description: null,
              color: "#64748b",
            },
          ],
          total: 1,
        },
      });
      return;
    }
    if (url.pathname === "/api/projects" && method === "GET") {
      await route.fulfill({ json: { projects: [project], total: 1 } });
      return;
    }
    if (url.pathname === "/api/tasks" && method === "GET") {
      await route.fulfill({ json: [taskResponse] });
      return;
    }
    if (url.pathname === `/api/tasks/${task.id}` && method === "GET") {
      await route.fulfill({ json: taskResponse });
      return;
    }
    if (url.pathname === `/api/tasks/${task.id}` && method === "PATCH") {
      Object.assign(taskResponse, route.request().postDataJSON());
      await route.fulfill({ json: taskResponse });
      return;
    }
    if (
      url.pathname === `/api/tasks/${task.id}/attachments` &&
      method === "GET"
    ) {
      await route.fulfill({ json: [] });
      return;
    }
    if (
      url.pathname === `/api/tasks/${task.id}/references` &&
      method === "GET"
    ) {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === `/api/tasks/${task.id}/recurrence`) {
      await route.fulfill({ json: null });
      return;
    }
    if (url.pathname === `/api/projects/${project.id}/tags`) {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === `/api/projects/${project.id}/assignee-candidates`) {
      await route.fulfill({ json: { members: [], total: 0 } });
      return;
    }
    if (url.pathname === `/api/python-proxy/tasks/${task.id}/apps`) {
      await route.fulfill({ json: { task_id: task.id, apps: [] } });
      return;
    }
    if (url.pathname === "/api/python-proxy/ogp" && method === "GET") {
      const requestedUrl = url.searchParams.get("url");
      if (!requestedUrl) {
        await route.fulfill({ status: 400, json: { success: false } });
        return;
      }
      ogpState.requests.push(requestedUrl);
      if (requestedUrl === nearX || requestedUrl === farX) {
        await route.fulfill({
          json: {
            success: true,
            url: requestedUrl,
            embed_type: "x-post",
            embed_html:
              '<blockquote class="twitter-tweet" data-test-height="760">fake tweet</blockquote>',
          },
        });
      } else {
        await route.fulfill({
          json: {
            success: true,
            url: requestedUrl,
            title: `OGP ${requestedUrl}`,
            description: "deterministic metadata",
          },
        });
      }
      return;
    }

    await route.fallback();
  });

  return ogpState;
}

test("task link embeds stay lazy and preserve the reading anchor", async ({
  page,
}) => {
  const ogpState = await installTaskRoutes(page);
  await page.goto(`/tasks/${task.id}`);

  const dialog = page.locator('[data-slot="dialog-content"]');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(task.title, { exact: true })).toBeVisible();

  const editor = dialog.locator(".cm-editor");
  await expect(editor).toBeVisible();
  await expect.poll(() => ogpState.requests).toContain(nearX);
  await expect.poll(() => ogpState.requests).toContain(nearNormal);
  expect(ogpState.requests).not.toContain(farX);
  expect(ogpState.requests).not.toContain(farNormal);

  const card = editor.locator(".cm-link-embed-x-post").first();
  await expect(card).toHaveCSS("height", "220px");
  await expect(card).toContainText("fake tweet");
  await expect(editor.locator("iframe")).toHaveCount(0);
  const requestCount = ogpState.requests.filter(url => url === nearX).length;
  await page.reload();
  const reopened = page.locator('[data-slot="dialog-content"]');
  await expect(reopened).toBeVisible();
  const reopenedCard = reopened.locator(".cm-link-embed-x-post").first();
  await expect(reopenedCard).toContainText("fake tweet");
  await expect(reopenedCard).toHaveCSS("height", "220px");
  expect(ogpState.requests.filter(url => url === nearX)).toHaveLength(requestCount);
  await expect(reopened.locator("iframe")).toHaveCount(0);
  const farScroller = reopened.locator(".cm-scroller");
  await farScroller.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await reopened.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect.poll(() => ogpState.requests).toContain(farX);
  await farScroller.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await reopened.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect.poll(() => ogpState.requests).toContain(farNormal);
});

test("unavailable task previews stay compact after reload and mode changes", async ({ page }) => {
  const privateUrl = "https://studio.tripo3d.ai/workspace/generate/4259900b-bdd8-41d7-85ea-a59ba0786dfb";
  await installTaskRoutes(page, privateUrl);
  let requests = 0;
  await page.route("**/api/python-proxy/ogp?*", async (route) => {
    requests += 1;
    await route.fulfill({ json: { success: false, url: privateUrl } });
  });
  // The private destination must never be contacted by this regression test.
  await page.route("https://studio.tripo3d.ai/**", (route) => route.abort());
  await page.goto(`/tasks/${task.id}`);
  const editor = page.locator('[data-slot="dialog-content"] .cm-editor');
  const card = editor.locator(".cm-link-embed-fallback");
  await expect(card.getByRole("link", { name: privateUrl, exact: true })).toBeVisible();
  await expect(card).not.toContainText("リンク情報を取得中...");
  await expect.poll(async () => (await card.boundingBox())?.height ?? 0).toBeLessThan(80);
  await expect.poll(() => requests).toBe(1);

  await page.reload();
  await expect(card.getByRole("link", { name: privateUrl, exact: true })).toBeVisible();
  await expect.poll(async () => (await card.boundingBox())?.height ?? 0).toBeLessThan(80);
  expect(requests).toBe(1);

  await card.hover();
  await card.getByTitle("リンクに戻す", { exact: true }).click();
  const plain = editor.locator(".cm-link-inline");
  await expect(plain.getByRole("link", { name: privateUrl })).toBeVisible();
  await plain.hover();
  await plain.getByTitle("埋め込みで表示", { exact: true }).click();
  await expect(card).toBeVisible();
  await expect.poll(async () => (await card.boundingBox())?.height ?? 0).toBeLessThan(80);
  expect(requests).toBe(1);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card.getByRole("link", { name: privateUrl })).toBeVisible();
  await expect.poll(async () => (await card.boundingBox())?.height ?? 0).toBeLessThan(80);
  expect(await card.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
});
