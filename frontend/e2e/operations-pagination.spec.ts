import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

async function visibleAnchor(page: Page, selectedId?: string) {
  return page.getByTestId("operations-map-flow").evaluate((flow, selected) => {
    const bounds = flow.getBoundingClientRect();
    const records = Array.from(flow.querySelectorAll<HTMLElement>(".react-flow__node")).map(element => {
      const rect = element.getBoundingClientRect();
      return { id: element.dataset.id!, x: rect.x - bounds.x, y: rect.y - bounds.y, width: rect.width, height: rect.height };
    }).filter(rect => rect.x < bounds.width && rect.y < bounds.height && rect.x + rect.width > 0 && rect.y + rect.height > 0);
    const distance = (record: typeof records[number]) => (record.x + record.width / 2 - bounds.width / 2) ** 2 + (record.y + record.height / 2 - bounds.height / 2) ** 2;
    const anchor = records.find(record => record.id === selected) || records.sort((left, right) => distance(left) - distance(right) || left.id.localeCompare(right.id))[0];
    if (!anchor) throw new Error("Expected a visible existing record before continuation");
    const viewport = flow.querySelector(".react-flow__viewport")!;
    return { ...anchor, zoom: new DOMMatrixReadOnly(getComputedStyle(viewport).transform).a };
  }, selectedId);
}

async function expectAnchorPreserved(page: Page, anchor: Awaited<ReturnType<typeof visibleAnchor>>) {
  await expect.poll(() => page.locator(`.react-flow__node[data-id="${anchor.id}"]`).evaluate((element, previous) => {
    const rect = element.getBoundingClientRect();
    const flow = element.closest('[data-testid="operations-map-flow"]')!;
    const bounds = flow.getBoundingClientRect();
    const zoom = new DOMMatrixReadOnly(getComputedStyle(flow.querySelector(".react-flow__viewport")!).transform).a;
    return Math.abs(rect.x - bounds.x - previous.x) < 1 && Math.abs(rect.y - bounds.y - previous.y) < 1 && Math.abs(zoom - previous.zoom) < 0.000001;
  }, anchor)).toBe(true);
}

for (const width of [1366, 390]) {
  test(`authorized continuation reaches late running Work and retains execution focus at ${width}px`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width, height: width === 390 ? 844 : 768 });
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: {
      features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true },
    } }));
    const agents = Array.from({ length: 125 }, (_, index) => ({
      id: `agent-${index}`, name: `担当AI ${index}`, state: "active", manager_relation: index === 0 ? "agent" : "unrecorded",
      manager_agent_id: index === 0 ? "agent-124" : null,
    }));
    const work = Array.from({ length: 603 }, (_, index) => ({
      id: `work-${index}`, task_title: `${index < 147 ? "案件付き" : "案件なし"}の仕事 ${index}`, project_id: index < 147 ? `project-${index}` : null, assigned_agent_id: index < 599 ? "agent-0" : null,
      state: index < 346 ? "blocked" : index < 519 ? "dead_letter" : index < 523 ? "awaiting_approval" : "running",
    }));
    const requests: { work: string | null; agents: string | null }[] = [];
    const metadata = (available: number, fetched: number, next: string | null) => ({
      available, fetched, limit: 100, has_more: Boolean(next), truncated: Boolean(next), next_cursor: next,
    });
    await page.route("**/api/python-proxy/operations/command-center*", route => {
      const query = new URL(route.request().url()).searchParams;
      requests.push({ work: query.get("work_cursor"), agents: query.get("agents_cursor") });
      const workPage = Number(query.get("work_cursor")?.split("-").at(-1) || 0);
      const agentPage = Number(query.get("agents_cursor")?.split("-").at(-1) || 0);
      const pageWork = work.slice(workPage * 100, (workPage + 1) * 100);
      const pageAgents = agents.slice(agentPage * 100, (agentPage + 1) * 100);
      return route.fulfill({ json: {
        agents: pageAgents, current_work: pageWork, projects: Array.from({ length: 147 }, (_, index) => ({ id: `project-${index}`, name: `Project ${index}` })),
        work_page: metadata(603, pageWork.length, workPage < 6 ? `work-${workPage + 1}` : null),
        agents_page: metadata(125, pageAgents.length, agentPage === 0 ? "agents-1" : null),
      } });
    });
    await page.route("**/api/python-proxy/operations/work/work-598/execution*", route => route.fulfill({ json: {
      work: { id: "work-598", project_id: null, state: "running", execution_summary: { attention_reasons: [], observation_status: "available" } },
      runs: [], events: [], tool_calls: [], runs_page: { has_more: false }, events_page: { has_more: false }, tools_page: { has_more: false },
    } }));
    await page.goto("/operations?tab=overview");
    const footer = page.getByTestId("operations-record-pagination");
    await expect(footer).toContainText("仕事: 取得済み 100 / 取得可能 603件");
    const viewport = page.locator(".react-flow__viewport").first();
    const canvas = await page.getByTestId("operations-map-flow").elementHandle();
    const browser = page.locator('details.operations-map-browser');
    await browser.locator(':scope > summary').click();
    await browser.getByRole("searchbox", { name: "取得済みの記録を検索" }).fill("agent-0");
    await browser.locator('[data-map-search-id="agent:agent-0"]').click();
    await expect(page).toHaveURL(/graph_node=agent%3Aagent-0/);
    // Added Projects, a later manager and hundreds of Work move world
    // coordinates. Preserve the visible record, zoom and URL selection.
    for (let index = 1; index <= 6; index++) {
      const anchor = await visibleAnchor(page);
      await page.getByRole("button", { name: "続きを取得", exact: true }).click();
      await expect(footer).toContainText(`仕事: 取得済み ${Math.min((index + 1) * 100, 603)} / 取得可能 603件`);
      await expectAnchorPreserved(page, anchor);
      await expect(page).toHaveURL(/graph_node=agent%3Aagent-0/);
      expect(await canvas!.evaluate(element => element.isConnected)).toBe(true);
    }
    await expect(footer).toContainText("Agent: 取得済み 125 / 取得可能 125件");
    await expect(page.getByRole("button", { name: "続きを取得" })).toHaveCount(0);
    expect(requests.slice(2).every(request => request.agents === null)).toBe(true);
    // All fetched records remain available, while the canvas initially shows
    // twelve assigned Work, four unassigned Work and every fetched Agent.
    await expect(page.getByTestId("operations-map-node")).toHaveCount(141);
    const agentGroup = page.getByTestId("operations-map-inspector").getByTestId("operations-map-work-group");
    await expect(agentGroup).toContainText("表示12件");
    await expect(agentGroup).toContainText("取得済み599件");
    await expect(agentGroup).toContainText("対象総数599件");
    await agentGroup.getByRole("button", { name: "さらに24件表示", exact: true }).click();
    await expect(agentGroup).toContainText("表示36件");
    await expect(page.getByTestId("operations-map-node")).toHaveCount(165);
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("operations-map-inspector")).toHaveCount(0);
    // Search is limited to fetched records. Select the acquired late assigned
    // Work using the ordinary search result; it is pinned into the canvas.
    await browser.locator(':scope > summary').click();
    await browser.getByRole("searchbox", { name: "取得済みの記録を検索" }).fill("work-598");
    await browser.locator('[data-map-search-id="work:work-598"]').click();
    const originalNode = page.locator('.react-flow__node[data-id="work:work-598"]');
    await expect(originalNode).toBeVisible();
    const inspector = page.getByTestId("operations-map-inspector");
    await expect(inspector).toContainText("案件なしの仕事 598");
    const selectedTransform = await viewport.getAttribute("style");
    await inspector.getByRole("button", { name: "実行の内側を見る" }).click();
    const execution = page.getByRole("dialog", { name: "選択した仕事の実行詳細: 案件なしの仕事 598" });
    await expect(execution).toBeVisible();
    await expect(execution.getByRole("button", { name: "実行詳細を閉じる" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(execution).toHaveCount(0);
    await expect(inspector).toBeVisible();
    const restored = width === 390 ? page.getByTestId("operations-map-inspector-dismiss") : originalNode;
    await expect(restored).toBeFocused();
    expect(await viewport.getAttribute("style")).toBe(selectedTransform);
    await page.goBack();
    await expect(execution).toBeVisible();
    await page.goBack();
    await expect(execution).toHaveCount(0);
    await expect(inspector).toBeVisible();
    await expect(restored).toBeFocused();
    await expect(footer).toContainText("仕事: 取得済み 603 / 取得可能 603件");
    expect(await viewport.getAttribute("style")).toBe(selectedTransform);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    // ReactFlow handles Escape internally too. Dismissing the inspector must
    // keep the acquired original record as the keyboard continuation point.
    await page.keyboard.press("Escape");
    await expect(inspector).toHaveCount(0);
    await expect(originalNode).toBeFocused();
    expect(await viewport.getAttribute("style")).toBe(selectedTransform);
  });
}
