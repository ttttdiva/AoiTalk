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

async function installTaskRoutes(page: Page): Promise<OgsRequestState> {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  await page.addInitScript(() => {
    const state = window as typeof window & {
      __aoitalkFakeTwitterLoads?: number;
      __aoitalkFakeTwitterRelease?: boolean;
      __aoitalkFakeTwitterHeight?: number;
    };
    state.__aoitalkFakeTwitterLoads = 0;
    state.__aoitalkFakeTwitterRelease = false;
    state.__aoitalkFakeTwitterHeight = 760;
    state.twttr = {
      widgets: {
        load(container?: HTMLElement) {
          state.__aoitalkFakeTwitterLoads =
            (state.__aoitalkFakeTwitterLoads ?? 0) + 1;
          if (!container) return;
          const renderFakeTweet = () => {
            const tweet = container.querySelector("blockquote.twitter-tweet");
            if (!tweet) return;
            const iframe = document.createElement("iframe");
            const height = state.__aoitalkFakeTwitterHeight ?? 760;
            iframe.setAttribute("data-fake-twitter", "true");
            iframe.style.display = "block";
            iframe.style.width = "100%";
            iframe.style.height = `${height}px`;
            container.replaceChildren(iframe);
          };
          const waitForRelease = () => {
            if (state.__aoitalkFakeTwitterRelease) {
              renderFakeTweet();
              return;
            }
            window.setTimeout(waitForRelease, 25);
          };
          waitForRelease();
        },
      },
    };
  });

  const ogpState: OgsRequestState = { requests: [] };
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
      await route.fulfill({ json: [task] });
      return;
    }
    if (url.pathname === `/api/tasks/${task.id}` && method === "GET") {
      await route.fulfill({ json: task });
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

  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as typeof window & {
            __aoitalkFakeTwitterLoads?: number;
          }).__aoitalkFakeTwitterLoads ?? 0,
      ),
    )
    .toBeGreaterThan(0);

  const marker = editor.locator(".cm-line", { hasText: "ANCHOR-MARKER" });
  await expect(marker).toBeVisible();
  await marker.evaluate((element) =>
    element.scrollIntoView({ block: "start", inline: "nearest" }),
  );
  const beforeTop = await marker.evaluate(
    (element) => element.getBoundingClientRect().top,
  );

  await page.evaluate(() => {
    (window as typeof window & { __aoitalkFakeTwitterRelease?: boolean }).__aoitalkFakeTwitterRelease = true;
  });
  await page.waitForTimeout(300);
  const afterTop = await marker.evaluate(
    (element) => element.getBoundingClientRect().top,
  );
  expect(Math.abs(afterTop - beforeTop)).toBeLessThanOrEqual(4);
  await expect
    .poll(() =>
      page.evaluate((url) => {
        const raw = window.sessionStorage.getItem(
          "aoitalk.linkEmbed.xHeights.v1",
        );
        if (!raw) return null;
        const values = JSON.parse(raw) as Record<string, number>;
        return values[url] ?? null;
      }, nearX),
    )
    .toBeGreaterThanOrEqual(760);

  await page.reload();
  const reopenedDialog = page.locator('[data-slot="dialog-content"]');
  await expect(reopenedDialog).toBeVisible();
  const reopenedX = reopenedDialog.locator(".cm-link-embed-x-post").first();
  await expect(reopenedX).toBeVisible();
  await expect
    .poll(() => reopenedX.evaluate((element) => parseInt(getComputedStyle(element).minHeight, 10)))
    .toBeGreaterThanOrEqual(760);

  // Reopen with a smaller actual iframe to verify the cached reservation can
  // shrink instead of pinning the previous 760px height forever.
  await page.addInitScript(() => {
    (window as typeof window & { __aoitalkFakeTwitterHeight?: number }).__aoitalkFakeTwitterHeight = 300;
  });
  await page.reload();
  const shrinkDialog = page.locator('[data-slot="dialog-content"]');
  await expect(shrinkDialog).toBeVisible();
  const shrinkX = shrinkDialog.locator(".cm-link-embed-x-post").first();
  await expect(shrinkX).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as typeof window & { __aoitalkFakeTwitterLoads?: number })
            .__aoitalkFakeTwitterLoads ?? 0,
      ),
    )
    .toBeGreaterThan(0);
  await page.evaluate(() => {
    (window as typeof window & { __aoitalkFakeTwitterRelease?: boolean }).__aoitalkFakeTwitterRelease = true;
  });
  await expect
    .poll(() => shrinkX.evaluate((element) => parseInt(getComputedStyle(element).minHeight, 10)))
    .toBeLessThanOrEqual(304);

  await page.evaluate(() => {
    (window as typeof window & { __aoitalkFakeTwitterRelease?: boolean }).__aoitalkFakeTwitterRelease = true;
  });

  const farScroller = shrinkDialog.locator(".cm-scroller");
  await farScroller.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await shrinkDialog.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect.poll(() => ogpState.requests).toContain(farX);
  // Rendering the far X post reserves its measured height; scroll to the new
  // bottom once more so the adjacent normal URL enters the near-viewport band.
  await farScroller.evaluate((element) => {
    element.scrollTop = element.scrollHeight - element.clientHeight;
  });
  await reopenedDialog.evaluate((element) => {
    element.scrollTop = element.scrollHeight - element.clientHeight;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  await page.waitForTimeout(100);
  await expect.poll(() => ogpState.requests).toContain(farNormal);
});
