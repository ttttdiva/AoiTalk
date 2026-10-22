import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const longTitle = "顧客向け提出資料を整理し、内容を確認して保存する長い仕事の名前・確認担当への引継ぎと最終記録を含む作業";
async function fixture(page: Page) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: {
    features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true },
  } }));
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: {
    spaces: [{ id: "recorded-space", name: "記録されたSpace" }],
    projects: [{ id: "recorded-project", name: "記録されたProject", space_id: "recorded-space" }],
    agents: [
      { id: "worker", name: "記録された担当AI", state: "active", manager_relation: "unrecorded", project_grants: [{ project_id: "recorded-project" }] },
      { id: "unrelated", name: "無関係のAI", state: "paused", manager_relation: "unrecorded" },
    ],
    current_work: Array.from({ length: 12 }, (_, i) => ({
      id: `recorded-work-${i}`, task_title: i ? `記録された仕事 ${i}` : longTitle,
      assigned_agent_id: "worker", project_id: "recorded-project", space_id: "recorded-space", state: "running",
      parent_work_item_id: i === 1 ? "recorded-work-0" : null,
    })),
  } }));
}
async function selectRecord(page: Page, id: string) {
  const browser = page.locator("details.operations-map-browser");
  if (await browser.getAttribute("open") === null) await browser.locator(":scope > summary").click();
  await browser.getByRole("searchbox", { name: "取得済みの記録を検索" }).fill(id.split(":").slice(1).join(":"));
  await browser.locator(`[data-map-search-id="${id}"]`).click();
}

for (const width of [1440, 390]) {
  test(`recorded drilldown preserves camera and detail scroll at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await fixture(page);
    await page.goto("/operations?tab=overview&overview_view=map");
    await expect(page.locator(".react-flow__edge")).toHaveCount(27);
    await expect(page.locator(".operations-map-node.is-dimmed")).toHaveCount(0);
    if (width === 1440) {
      const canvas = await page.getByTestId("operations-map-flow").boundingBox();
      const detail = await page.getByTestId("operations-map-overview").boundingBox();
      expect(canvas!.x + canvas!.width).toBeLessThanOrEqual(detail!.x);
    } else await expect(page.getByTestId("operations-map-overview")).toHaveCount(0);
    await selectRecord(page, "agent:worker");
    const panel = page.getByTestId("operations-map-inspector");
    await expect(panel).toContainText("記録された担当AI");
    await expect(page.locator('.react-flow__node[data-id="agent:unrelated"] .operations-map-node')).toHaveClass(/is-dimmed/);
    const before = await page.locator(".react-flow__viewport").getAttribute("style");
    await panel.evaluate(element => { element.scrollTop = 80; });
    const savedScroll = await panel.evaluate(element => element.scrollTop);
    expect(savedScroll).toBeGreaterThan(0);
    await panel.getByRole("button", { name: "選択を解除" }).click();
    await expect(panel).toHaveCount(0);
    await selectRecord(page, "agent:worker");
    await expect.poll(() => panel.evaluate(element => element.scrollTop)).toBe(savedScroll);
    await expect(page.locator(".react-flow__viewport")).toHaveAttribute("style", before!);
    await panel.getByRole("button", { name: /関連する内訳を開く/ }).click();
    const breadcrumb = page.getByRole("navigation", { name: "関係図の階層" });
    await expect(breadcrumb).toContainText("記録された担当AI");
    await expect(page.locator('.react-flow__node[data-id="agent:unrelated"]')).toHaveCount(0);
    await breadcrumb.getByRole("button", { name: "関係図の前の階層へ戻る" }).click();
    await expect(panel).toContainText("記録された担当AI");
    await expect(page.locator(".react-flow__viewport")).toHaveAttribute("style", before!);
    await panel.getByRole("button", { name: "選択を解除" }).click();
    await selectRecord(page, "work:recorded-work-0");
    await expect(panel.getByRole("heading", { name: longTitle })).toBeVisible();
    const title = page.locator('.react-flow__node[data-id="work:recorded-work-0"] .operations-map-node-name');
    expect(await title.evaluate(element => getComputedStyle(element).whiteSpace)).toBe("normal");
    await expect(page.locator('.react-flow__node[data-id="work:recorded-work-0"] code')).toHaveAttribute("title", "recorded-work-0");
  });
}
