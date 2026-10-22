import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

async function recordsBrowser(page: Page) {
  const browser = page.locator("details.operations-map-browser");
  if ((await browser.getAttribute("open")) === null) await browser.locator(":scope > summary").click();
  return browser;
}
async function selectFetched(page: Page, id: string) {
  const browser = await recordsBrowser(page);
  await browser.getByRole("searchbox", { name: "取得済みの記録を検索" }).fill(id.split(":").slice(1).join(":"));
  await browser.locator(`[data-map-search-id="${id}"]`).click();
}
async function installShell(page: Page) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: {
    features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true },
  } }));
}
for (const width of [1366, 390]) {
  test(`staged Work expansion follows display filters while explicit targets stay available at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 768 });
    await installShell(page);
    const works = Array.from({ length: 80 }, (_, index) => ({
      id: `staged-work-${String(index + 1).padStart(3, "0")}`,
      task_title: `段階表示の仕事 ${index + 1}`, assigned_agent_id: "staged-agent",
      state: index < 40 ? "blocked" : "succeeded", project_id: null,
    }));
    await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: {
      agents: [{ id: "staged-agent", name: "段階表示の担当AI", state: "active", manager_relation: "unrecorded" }],
      current_work: works,
      work_page: { limit: 100, fetched: 80, available: 80, has_more: false, truncated: false, next_cursor: null },
      agents_page: { limit: 100, fetched: 1, available: 1, has_more: false, truncated: false, next_cursor: null },
    } }));
    await page.goto("/operations?tab=overview&overview_view=map");
    await expect(page.getByTestId("operations-map-node")).toHaveCount(13);
    // The measured mobile layout must own initial fitting before any resize
    // anchor correction. Its root Agent should start fully inside the map.
    await expect.poll(() => page.locator('.react-flow__node[data-id="agent:staged-agent"]').evaluate(element => {
      const card = element.getBoundingClientRect();
      const map = element.closest('[data-testid="operations-map-flow"]')!.getBoundingClientRect();
      return card.left >= map.left && card.right <= map.right && card.top >= map.top && card.bottom <= map.bottom;
    })).toBe(true);
    await selectFetched(page, "agent:staged-agent");
    const group = page.getByTestId("operations-map-inspector").getByTestId("operations-map-work-group");
    await expect(group).toContainText("表示12件");
    await expect(group).toContainText("取得済み80件");
    await expect(group).toContainText("対象総数80件");
    await group.getByRole("button", { name: "さらに24件表示", exact: true }).click();
    await expect(group).toContainText("表示36件");
    await expect(page.getByTestId("operations-map-node")).toHaveCount(37);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "完了履歴", exact: true }).click();
    await expect(page.locator('.react-flow__node[data-id="work:staged-work-001"]')).toHaveCount(0);
    await expect(page.locator('.react-flow__node[data-id="work:staged-work-036"]')).toHaveCount(0);
    await selectFetched(page, "work:staged-work-001");
    await expect(page.locator('.react-flow__node[data-id="work:staged-work-001"]')).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(page.locator('.react-flow__node[data-id="work:staged-work-001"]')).toHaveCount(1);
    await expect(page.getByTestId("operations-record-pagination")).toContainText("仕事: 取得済み 80 / 取得可能 80件");
    expect(new URL(page.url()).searchParams.get("tab")).toBe("overview");
  });
}

test("a deferred Work deep link reads authorized continuation and an absent ID creates no record", async ({ page }) => {
  await installShell(page);
  const works = Array.from({ length: 123 }, (_, index) => ({
    id: `deferred-work-${String(index + 1).padStart(3, "0")}`, task_title: `後続の仕事 ${index + 1}`,
    assigned_agent_id: "deferred-agent", state: "running", project_id: null,
  }));
  const requests: string[] = [];
  let executionRequests = 0;
  await page.route("**/api/python-proxy/operations/command-center*", route => {
    const cursor = new URL(route.request().url()).searchParams.get("work_cursor");
    requests.push(cursor || "initial");
    const offset = cursor ? 100 : 0;
    return route.fulfill({ json: {
      agents: [{ id: "deferred-agent", name: "後続ページの担当AI", manager_relation: "unrecorded", state: "active" }],
      current_work: works.slice(offset, offset + 100),
      work_page: { limit: 100, fetched: Math.min(100, 123 - offset), available: 123, has_more: !cursor, truncated: !cursor, next_cursor: cursor ? null : "authorized-next" },
      agents_page: { limit: 100, fetched: 1, available: 1, has_more: false, truncated: false, next_cursor: null },
    } });
  });
  await page.route("**/api/python-proxy/operations/work/*/execution*", route => {
    executionRequests += 1;
    return route.fulfill({ json: { work: works[122], runs: [], events: [], tool_calls: [] } });
  });
  await page.goto("/operations?tab=overview&overview_view=map&graph_node=work%3Adeferred-work-123");
  await expect(page.getByTestId("operations-map-inspector")).toContainText("後続の仕事 123");
  await expect(page.locator('.react-flow__node[data-id="work:deferred-work-123"]')).toHaveCount(1);
  expect(requests).toEqual(["initial", "authorized-next"]);
  expect(executionRequests).toBe(0);
  await page.goto("/operations?tab=overview&overview_view=map&graph_node=work%3Amissing-deferred-record");
  await expect(page.getByTestId("operations-record-pagination")).toContainText("仕事: 取得済み 123 / 取得可能 123件");
  await expect(page.locator('.react-flow__node[data-id="work:missing-deferred-record"]')).toHaveCount(0);
  await expect(page.getByTestId("operations-map-inspector")).toHaveCount(0);
  expect(executionRequests).toBe(0);
});
