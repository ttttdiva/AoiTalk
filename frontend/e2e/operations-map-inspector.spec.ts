import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace } from "./support/persona-workspace-fixture";

const snapshot = {
  agents: [
    { id: "manager", name: "監督AI", state: "active", manager_relation: "human" },
    { id: "worker", name: "担当AI", state: "active", manager_relation: "agent", manager_agent_id: "manager" },
  ],
  current_work: [
    { id: "running", task_title: "担当の仕事", state: "running", assigned_agent_id: "worker" },
    { id: "done", task_title: "完了した仕事", state: "succeeded", assigned_agent_id: "worker" },
  ],
  attention: [],
};

async function mockOverview(page: Page) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: {
    features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true },
  } }));
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: snapshot }));
  await page.route("**/api/python-proxy/operations/work/running/execution*", route => route.fulfill({ json: {
    work: { id: "running", state: "running", execution_summary: { attention_reasons: [], observation_status: "available" } },
    runs: [], events: [], tool_calls: [], runs_page: { has_more: false }, events_page: { has_more: false }, tools_page: { has_more: false },
  } }));
}

const graphNode = (page: Page, id: string) => page.locator(`.react-flow__node[data-id="${id}"]`);
const graphMain = (page: Page) => page.getByTestId("operations-map-main");

for (const selection of [{ id: "agent:manager", label: "監督AI" }, { id: "work:running", label: "担当の仕事" }]) {
  test(`mobile ${selection.id} inspector isolates the covered graph during keyboard navigation`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockOverview(page);
    await page.goto("/operations?tab=overview&work_view=all");
    const original = graphNode(page, selection.id);
    await expect(original).toBeVisible();
    await original.focus();
    await page.keyboard.press("Enter");
    const inspector = page.getByRole("dialog", { name: selection.label, exact: true });
    const dismiss = inspector.getByRole("button", { name: "選択を解除" });
    await expect(inspector).toHaveAttribute("aria-modal", "false");
    await expect(dismiss).toBeFocused();
    await expect(graphMain(page)).toHaveAttribute("inert", "");
    const transform = await page.locator(".react-flow__viewport").getAttribute("style");
    // Both directions must avoid all graph nodes and controls beneath the panel.
    for (const key of ["Tab", "Shift+Tab"]) {
      let reachedNavigation = false;
      await dismiss.focus();
      for (let step = 0; step < 35; step++) {
        await page.keyboard.press(key);
        const focus = await page.evaluate(() => ({
          concealed: Boolean(document.activeElement?.closest('[data-testid="operations-map-main"]')),
          navigation: Boolean(document.activeElement?.closest('nav[aria-label="Operationsワークスペース"]')) || document.activeElement?.getAttribute('aria-label') === 'Workspace Navigationを切り替え',
        }));
        expect(focus.concealed).toBe(false);
        reachedNavigation ||= focus.navigation;
        if (reachedNavigation) break;
      }
      expect(reachedNavigation).toBe(true);
      await expect(inspector).toBeVisible();
    }
    await dismiss.focus();
    for (const handling of ["preventDefault", "stopPropagation"] as const) {
      await dismiss.evaluate((element, method) => element.addEventListener("keydown", event => {
        if ((event as KeyboardEvent).key === "Escape") event[method]();
      }, { once: true }), handling);
      await page.keyboard.press("Escape");
      await expect(inspector).toBeVisible();
      await expect(graphMain(page)).toHaveAttribute("inert", "");
    }
    await page.keyboard.press("Escape");
    await expect(inspector).toHaveCount(0);
    await expect(original).toBeFocused();
    await expect(graphMain(page)).not.toHaveAttribute("inert");
    await expect(page).not.toHaveURL(/graph_node=/);
    expect(await page.locator(".react-flow__viewport").getAttribute("style")).toBe(transform);
    await page.goBack();
    await expect(inspector).toBeVisible();
    await expect(dismiss).toBeFocused();
    await page.goForward();
    await expect(inspector).toHaveCount(0);
    await expect(original).toBeFocused();
  });
}

test("mobile Work execution returns to its inspector before selection returns to the graph", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockOverview(page);
  await page.goto("/operations?tab=overview&work_view=all");
  const original = graphNode(page, "work:running");
  await expect(original).toBeVisible();
  const canvas = await page.getByTestId("operations-map-flow").elementHandle();
  await original.focus();
  await page.keyboard.press("Enter");
  const inspector = page.getByRole("dialog", { name: "担当の仕事", exact: true });
  const dismiss = page.getByTestId("operations-map-inspector-dismiss");
  const execution = page.getByRole("dialog", { name: "選択した仕事の実行詳細: 担当の仕事" });
  await expect(dismiss).toBeFocused();
  const transform = await page.locator(".react-flow__viewport").getAttribute("style");
  const openExecution = async () => {
    await inspector.getByRole("button", { name: "実行の内側を見る" }).focus();
    await page.keyboard.press("Enter");
    await expect(execution.getByRole("button", { name: "実行詳細を閉じる" })).toBeFocused();
    await expect(inspector).toHaveCount(0);
    await expect(page.getByTestId("operations-overview-background")).toHaveAttribute("inert", "");
  };
  const expectInspectorRestored = async () => {
    await expect(execution).toHaveCount(0);
    await expect(inspector).toBeVisible();
    await expect(dismiss).toBeFocused();
    await expect(graphMain(page)).toHaveAttribute("inert", "");
    await expect(page).toHaveURL(/graph_node=work%3Arunning/);
    expect(await canvas!.evaluate(element => element.isConnected)).toBe(true);
    expect(await page.locator(".react-flow__viewport").getAttribute("style")).toBe(transform);
  };
  await openExecution();
  await page.keyboard.press("Escape");
  await expectInspectorRestored();
  await openExecution();
  await page.goBack();
  await expectInspectorRestored();
  await page.goForward();
  await expect(execution.getByRole("button", { name: "実行詳細を閉じる" })).toBeFocused();
  await page.reload();
  await expect(execution.getByRole("button", { name: "実行詳細を閉じる" })).toBeFocused();
  await execution.getByRole("button", { name: "実行詳細を閉じる" }).click();
  await expect(inspector).toBeVisible();
  await expect(dismiss).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(inspector).toHaveCount(0);
  await expect(original).toBeFocused();
});

test("desktop selection retains the complementary inspector and graph keyboard access", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await mockOverview(page);
  await page.goto("/operations?tab=overview&overview_view=map");
  const original = graphNode(page, "agent:manager");
  await expect(original).toBeVisible();
  await original.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", { name: "関係図の詳細" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(graphMain(page)).not.toHaveAttribute("inert");
  await expect(original).toBeFocused();
  await graphNode(page, "agent:worker").focus();
  await expect(graphNode(page, "agent:worker")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("operations-map-inspector")).toHaveCount(0);
});

test("Overview routing survives filters and final legacy selection keys while explicit Studio navigation remains available", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await mockOverview(page);
  await page.goto("/operations?tab=overview&overview_view=map");
  const filters = page.getByTestId("operations-command-center-filters");
  await filters.locator("summary").click();
  await filters.getByRole("combobox", { name: "State", exact: true }).click();
  await page.getByRole("option", { name: "Working", exact: true }).click();
  await expect(page).toHaveURL(/tab=overview/);
  await expect(page).toHaveURL(/state=running/);
  await expect(page.getByTestId("operations-map-flow")).toBeVisible();
  await filters.getByRole("combobox", { name: "Scope", exact: true }).click();
  await page.getByRole("option", { name: "Agent", exact: true }).click();
  await filters.getByRole("textbox", { name: "Agent ID" }).fill("worker");
  await expect(page).toHaveURL(/scope=agent/);
  await expect(page).toHaveURL(/agent_id=worker/);
  await expect(page).toHaveURL(/tab=overview/);
  await expect(page.getByTestId("operations-map-flow")).toBeVisible();
  for (const action of ["dismiss", "pane", "Escape"]) {
    await page.goto("/operations?graph_node=agent%3Amanager");
    await expect(page.getByTestId("operations-map-inspector")).toBeVisible();
    if (action === "dismiss") await page.getByTestId("operations-map-inspector").getByRole("button", { name: "選択を解除" }).click();
    else if (action === "pane") await page.getByTestId("operations-map-flow").click({ position: { x: 10, y: 10 } });
    else await page.keyboard.press("Escape");
    await expect(page).toHaveURL(/tab=overview/);
    await expect(page.getByTestId("operations-map-inspector")).toHaveCount(0);
    await expect(page.getByTestId("operations-map-flow")).toBeVisible();
  }
  await page.goto("/operations?graph_work=running");
  const execution = page.getByRole("dialog", { name: "選択した仕事の実行詳細: 担当の仕事" });
  await expect(execution.getByRole("button", { name: "実行詳細を閉じる" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(/tab=overview/);
  await expect(execution).toHaveCount(0);
  await expect(page.getByTestId("operations-map-flow")).toBeVisible();
  await page.goto("/operations?work_view=open");
  await expect(page.getByTestId("operations-map-flow")).toBeVisible();
  await page.getByRole("button", { name: "すべて", exact: true }).click();
  await expect(page).toHaveURL(/tab=overview/);
  await expect(page.getByTestId("operations-map-flow")).toBeVisible();
  await mockPersonaWorkspace(page, { empty: true });
  const navigation = page.getByRole("navigation", { name: "Operationsワークスペース" });
  const studio = navigation.getByRole("link", { name: /^人格workspace/ });
  await expect(studio).toHaveAttribute("href", new URL(page.url()).pathname);
  await studio.click();
  await expect(page).toHaveURL(/tab=studio/);
  await expect(page.getByTestId("operations-map-canvas")).toHaveCount(0);
  await expect(studio).toHaveAttribute("aria-current", "page");
});
