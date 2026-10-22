import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const dashboard = {
  current_work: [
    { id: "running", source_type: "task", task_id: "task-a", task_title: "進行中のAPI改善", state: "running", agent: "Aoi", project: "AoiTalk", updated_at: "2026-10-03T01:00:00Z" },
    { id: "blocked", source_type: "task", task_title: "結果を確認する仕事", state: "uncertain" },
    { id: "done", source_type: "task", task_title: "完了した調査", state: "succeeded" },
  ],
  attention: [{ id: "approval-a", kind: "approval", status: "proposed" }, { id: "approved-b", kind: "approval", status: "approved" }],
  spaces: [{ id: "space-a", name: "Inbox" }, { id: "space-b", name: "Inbox" }],
  projects: [{ id: "project-a", name: "Inbox", space_id: "space-a" }, { id: "project-b", name: "Inbox", space_id: "space-b" }],
};

const features = { features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true } };
const graphSnapshot = {
  ...dashboard,
  agents: [
    { id: "supervisor", name: "監督AI", manager_relation: "human", state: "active" },
    { id: "worker", name: "担当AI", manager_relation: "agent", manager_agent_id: "supervisor", state: "active" },
  ],
  current_work: dashboard.current_work.map(work => ({ ...work, assigned_agent_id: "worker" })),
};
const node = (page: import("@playwright/test").Page, id: string) => page.locator(`[data-entity-id="${id}"]`);

for (const viewport of [{ width: 1440, height: 1000 }, { width: 1366, height: 768 }, { width: 390, height: 844 }]) {
  test(`relationship overview fills the available viewport and persists filters at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: features }));
    await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: graphSnapshot }));
    const executionRequests: string[] = [];
    page.on("request", request => { if (request.url().includes("/execution")) executionRequests.push(request.url()); });
    await page.goto("/operations?tab=overview&overview_view=map");
    await expect(page.getByRole("heading", { name: "Operations Map" })).toBeVisible();
    await expect(page.getByTestId("operations-map-node")).toHaveCount(7);
    await expect(page.getByTestId("operations-map-inspector")).toHaveCount(0);
    await expect(page).not.toHaveURL(/graph_work=/);
    expect(executionRequests).toEqual([]);
    if (viewport.width < 700) {
      await page.getByRole("button", { name: "Workspace Navigationを切り替え", exact: true }).click();
      await expect(page.getByRole("navigation", { name: "Operationsワークスペース" })).toHaveCount(1);
      await page.keyboard.press("Escape");
    } else await expect(page.getByRole("navigation", { name: "Operationsワークスペース" })).toHaveCount(1);
    await expect(page.getByRole("tablist", { name: "Operations sections" })).toHaveCount(0);
    await expect(page.getByTestId("operations-unfinished-count")).toHaveText("未完了 2件");
    await expect(page.getByTestId("operations-command-center-filters")).not.toHaveAttribute("open");
    const dimensions = await page.getByTestId("operations-map-flow").evaluate(element => {
      const rect = element.getBoundingClientRect();
      const shell = document.querySelector('[data-shell-region="main-canvas"]')!.getBoundingClientRect();
      return { width: rect.width, height: rect.height, shellWidth: shell.width, shellHeight: shell.height, bottom: rect.bottom };
    });
    expect(dimensions.width).toBeGreaterThan(dimensions.shellWidth - 2);
    // Mobile also keeps the owner/production summary and filters reachable.
    // The map retains a readable card viewport and fills the remaining space.
    expect(dimensions.height).toBeGreaterThan(dimensions.shellHeight * (viewport.width < 700 ? 0.38 : 0.48));
    expect(dimensions.height).toBeGreaterThan(viewport.width < 700 ? 300 : 320);
    expect(dimensions.bottom).toBeLessThanOrEqual(viewport.height);
    await expect.poll(async () => {
      const supervisor = await node(page, "agent:supervisor").boundingBox();
      const flow = await page.getByTestId("operations-map-flow").boundingBox();
      return Boolean(supervisor && flow && supervisor.y >= flow.y && supervisor.y + supervisor.height <= flow.y + flow.height);
    }).toBe(true);
    // Rank positions follow supervision and every assigned Work, including completed records.
    const y = async (id: string) => node(page, id).evaluate(element => Number((element.closest('.react-flow__node') as HTMLElement).style.transform.match(/translate\([^,]+,\s*([\d.-]+)px\)/)?.[1]));
    expect(await y("agent:supervisor")).toBeLessThan(await y("agent:worker"));
    expect(await y("agent:worker")).toBeLessThan(await y("work:done"));
    await page.getByRole("button", { name: "完了履歴", exact: true }).click();
    await expect(page).toHaveURL(/work_view=completed/);
    await expect(node(page, "work:done")).toHaveCount(1);
    await expect(node(page, "work:running")).toHaveCount(0);
    await page.reload();
    await expect(node(page, "work:done")).toHaveCount(1);
    await page.getByRole("button", { name: "未完了", exact: true }).click();
    await expect(page).toHaveURL(/work_view=open/);
    await expect(node(page, "work:blocked")).toHaveCount(1);
    await expect(node(page, "work:done")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}

test("selection and viewport survive reordered refresh, and explicit Work drilldown preserves provenance", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  let refreshed = false;
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: refreshed ? { ...graphSnapshot, agents: [...graphSnapshot.agents].reverse(), current_work: [...graphSnapshot.current_work].reverse() } : graphSnapshot }));
  await page.route("**/api/python-proxy/operations/work/running/execution*", route => route.fulfill({ json: {
    work: { id: "running", state: "running", execution_summary: { attention_reasons: [], observation_status: "available" } },
    runs: [], events: [], tool_calls: [], runs_page: { has_more: false }, events_page: { has_more: false }, tools_page: { has_more: false },
  } }));
  await page.goto("/operations?tab=overview&overview_view=map");
  await expect(node(page, "work:running")).toHaveCount(1);
  await page.getByRole("button", { name: "関係図全体を表示", exact: true }).first().click();
  await node(page, "work:running").click();
  await expect(page).toHaveURL(/graph_node=work%3Arunning/);
  const inspector = page.getByTestId("operations-map-inspector");
  await expect(inspector).toContainText("進行中のAPI改善");
  await page.getByRole("button", { name: "拡大", exact: true }).click();
  const viewport = page.locator(".react-flow__viewport").first();
  await page.waitForTimeout(350);
  const transform = await viewport.getAttribute("style");
  refreshed = true;
  await page.getByRole("button", { name: "更新", exact: true }).click();
  await expect(page.getByRole("button", { name: "更新", exact: true })).toBeEnabled();
  await expect(page).toHaveURL(/graph_node=work%3Arunning/);
  expect(await viewport.getAttribute("style")).toBe(transform);
  await page.keyboard.press("Escape");
  await expect(inspector).toHaveCount(0);
  await node(page, "work:running").click();
  await expect(inspector).toBeVisible();
  await inspector.getByRole("button", { name: "実行の内側を見る" }).click();
  await expect(page).toHaveURL(/graph_work=running/);
  await expect(page.getByText(/取得済みのrun記録がありません/)).toBeVisible();
  await expect(page.getByTestId("operations-execution-graph")).toHaveCount(0);
  await page.getByRole("button", { name: "実行詳細を閉じる" }).click();
  await expect(page).not.toHaveURL(/graph_work=/);
  await expect(inspector).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(inspector).toHaveCount(0);
});

for (const width of [1366, 390]) {
  test(`execution panel isolates covered controls and restores keyboard focus at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 768 });
    await addAuthCookie(page); await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: graphSnapshot }));
    await page.route("**/api/python-proxy/operations/work/running/execution*", route => route.fulfill({ json: {
      work: { id: "running", state: "running", execution_summary: { attention_reasons: [], observation_status: "available" } },
      runs: [], events: [], tool_calls: [], runs_page: { has_more: false }, events_page: { has_more: false }, tools_page: { has_more: false },
    } }));
    await page.goto("/operations?tab=overview&work_view=all");
    const originalNode = page.locator('.react-flow__node[data-id="work:running"]');
    await expect(originalNode).toBeVisible();
    await originalNode.focus();
    await page.keyboard.press("Enter");
    const inspector = page.getByTestId("operations-map-inspector");
    await expect(inspector).toBeVisible();
    const transform = await page.locator(".react-flow__viewport").first().getAttribute("style");
    await inspector.getByRole("button", { name: "実行の内側を見る" }).focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "選択した仕事の実行詳細: 進行中のAPI改善" });
    const close = dialog.getByRole("button", { name: "実行詳細を閉じる" });
    await expect(dialog).toHaveAttribute("aria-modal", "false");
    await expect(close).toBeFocused();
    await expect(page.getByTestId("operations-overview-background")).toHaveAttribute("inert", "");
    // Real Tab navigation must leave the nonmodal panel for the shell, while
    // never visiting the covered graph, inspector, attention or metrics.
    let reachedNavigation = false;
    for (let step = 0; step < 28; step++) {
      await page.keyboard.press("Tab");
      const focus = await page.evaluate(() => ({
        covered: Boolean(document.activeElement?.closest('[data-testid="operations-overview-background"]')),
        navigation: Boolean(document.activeElement?.closest('nav[aria-label="Operationsワークスペース"]')) || document.activeElement?.getAttribute('aria-label') === 'Workspace Navigationを切り替え',
      }));
      expect(focus.covered).toBe(false);
      reachedNavigation ||= focus.navigation;
      if (reachedNavigation) break;
    }
    expect(reachedNavigation).toBe(true);
    await expect(dialog).toBeVisible();
    await close.focus();
    await page.keyboard.press("Shift+Tab");
    expect(await page.evaluate(() => Boolean(document.activeElement?.closest('[data-testid="operations-overview-background"]')))).toBe(false);
    await close.focus();
    // Child controls may consume Escape before the execution panel sees it.
    for (const handling of ["preventDefault", "stopPropagation"] as const) {
      await close.evaluate((element, method) => {
        element.addEventListener("keydown", event => {
          if ((event as KeyboardEvent).key === "Escape") event[method]();
        }, { once: true });
      }, handling);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeVisible();
      await expect(page).toHaveURL(/graph_work=running/);
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(width === 390 ? page.getByTestId("operations-map-inspector-dismiss") : originalNode).toBeFocused();
    await expect(page).toHaveURL(/graph_node=work%3Arunning/);
    await expect(page).not.toHaveURL(/graph_work=/);
    await expect(page.getByTestId("operations-overview-background")).not.toHaveAttribute("inert");
    expect(await page.locator(".react-flow__viewport").first().getAttribute("style")).toBe(transform);
    // History and reload use the same focus lifecycle as the close button.
    await inspector.getByRole("button", { name: "実行の内側を見る" }).focus();
    await page.keyboard.press("Enter");
    await expect(close).toBeFocused();
    await page.reload();
    await expect(close).toBeFocused();
    await page.goBack();
    await expect(dialog).toHaveCount(0);
    await expect(width === 390 ? page.getByTestId("operations-map-inspector-dismiss") : originalNode).toBeFocused();
    await expect(page).toHaveURL(/graph_node=work%3Arunning/);
    await expect(page).toHaveURL(/work_view=all/);
    await page.goto("/operations?tab=overview&work_view=all&graph_work=running");
    await expect(close).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(originalNode).toBeFocused();
    await expect(page).toHaveURL(/work_view=all/);
  });
}

test("a failed refresh is visibly unavailable and retry restores fetched counts", async ({ page }) => {
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  let unavailable = true;
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill(unavailable ? { status: 503, json: { detail: "unavailable" } } : { json: graphSnapshot }));
  await page.goto("/operations?tab=overview&overview_view=map");
  await expect(page.getByTestId("operations-command-center").getByRole("alert")).toContainText("読み込めませんでした");
  await expect(page.getByTestId("operations-command-center-overview")).toHaveCount(0);
  unavailable = false;
  await page.getByRole("button", { name: "更新", exact: true }).click();
  await expect(page.getByTestId("operations-unfinished-count")).toHaveText("未完了 2件");
  await expect(page.getByTestId("operations-command-center").getByRole("alert")).toHaveCount(0);
});

test("execution hierarchy restores URL focus and keeps phase/tool evidence separate", async ({ page }) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  const paging = { limit: 50, has_more: false, next_cursor: null, truncated: false };
  const execution = { schema_version: "operations-work-execution-v1", observed_at: "2026-10-03T01:00:00Z",
    work: { id: "running", state: "running", execution_summary: { attempt_count: 1, max_attempts: 3, attention_reasons: [], observation_status: "partial" } },
    runs: [{ id: "main", work_item_id: "running", acting_subagent_id: "Aoi", status: "running" }, { id: "child", work_item_id: "child-work", parent_run_id: "main", acting_subagent_id: "Reviewer", status: "awaiting_approval" }],
    events: [{ id: "phase-a", run_id: "main", sequence: 1, phase: "planning", event_type: "stream.status_update" }],
    tool_calls: [{ id: "tool-a", run_id: "main", tool_name: "search", status: "running" }], runs_page: paging, events_page: paging, tools_page: paging };
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: dashboard }));
  await page.route("**/api/python-proxy/operations/work/running/execution*", route => route.fulfill({ json: execution }));
  await page.goto("/operations?tab=overview&graph_work=running&execution_focus=run%3Amain&execution_node=run%3Achild&work_view=all");
  const panel = page.getByRole("complementary", { name: "選択した実行の詳細" });
  await expect(panel).toContainText("Reviewer");
  await expect(panel).toContainText("child-work");
  await expect(panel).toContainText("正本の画面で承認内容を確認");
  await expect(page.getByText("正本の実行記録は一部だけ確認できています")).toBeVisible();
  const list = page.getByRole("list", { name: "実行階層の選択" });
  await expect(list.getByRole("button", { name: /工程 planning/ })).toBeVisible();
  await expect(list.getByRole("button", { name: /Tool search/ })).toBeVisible();
  await list.getByRole("button", { name: /Reviewer/ }).focus();
  await page.keyboard.press("Enter");
  await expect(panel).toContainText("Reviewer");
  await page.reload();
  await expect(panel).toContainText("Reviewer");
  await page.getByRole("button", { name: "上の階層へ戻る" }).click();
  await expect(page).toHaveURL(/execution_focus=context%3Arunning/);
  await expect(page).toHaveURL(/work_view=all/);
  await page.goBack();
  await expect(page).toHaveURL(/execution_focus=run%3Amain/);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("operations-execution-graph")).toBeHidden();
  await expect(list).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});


test("external actions stay separate from Work and retain origin-actor evidence", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: {
    ...graphSnapshot,
    attention: Array.from({ length: 8 }, (_, i) => ({ id: `approval-${i}`, kind: "approval", status: "proposed", approval_summary: { operation_label: "応募を送信", target_name: `案件${i}`, actor_name: "監督AI", actor: "supervisor", work_item_id: "running", current_assignee: "worker", current_assignee_name: "担当AI" } })),
  } }));
  await page.goto("/operations?tab=overview&graph_node=action%3Aapproval-0");
  await expect(page.locator('[data-entity-id^="action:"]')).toHaveCount(8);
  const inspector = page.getByTestId("operations-map-inspector");
  await expect(inspector).toContainText("応募を送信");
  await expect(inspector.getByRole("link", { name: "正本で確認" })).toHaveAttribute("href", new URL(page.url()).pathname + "?tab=actions&action=approval-0");
  await inspector.locator("summary").click();
  await expect(inspector).toContainText("提案者Agent ID");
  await expect(inspector).toContainText("supervisor");
  await expect(inspector).toContainText("現在の担当Agent ID");
  await expect(inspector).toContainText("worker");
  const bands = page.locator(".operations-map-band--actions");
  await expect(bands).toHaveText("外部操作 · 8件");
  expect(await bands.evaluate(element => Number((element as HTMLElement).style.top.replace("px", "")))).toBeGreaterThan(await node(page, "work:running").evaluate(element => Number((element.closest('.react-flow__node') as HTMLElement).style.transform.match(/translate\([^,]+,\s*([\d.-]+)px\)/)?.[1]) + 100));
});

test("sparse mobile records keep external actions within the initial viewport without fake links", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: {
    agents: Array.from({ length: 4 }, (_, i) => ({ id: `agent-${i}`, name: `Agent ${i}`, state: "active", manager_relation: "unrecorded" })),
    current_work: [],
    attention: Array.from({ length: 8 }, (_, i) => ({ id: `action-${i}`, kind: "approval", status: "proposed", approval_summary: { operation_label: "外部操作", target_name: `案件 ${i}` } })),
  } }));
  await page.goto("/operations?tab=overview&overview_view=map");
  await expect(page.getByTestId("operations-map-node")).toHaveCount(12);
  await expect(page.locator(".react-flow__edge")).toHaveCount(0);
  await expect(node(page, "action:action-0")).toBeInViewport();
  await expect(node(page, "action:action-1")).toBeInViewport();
  await expect(page.locator('[data-entity-id^="work:"]')).toHaveCount(0);
  await expect(page.getByTestId("operations-map-inspector")).toHaveCount(0);
});

test("Actions-only overview retains its canvas, selection and zoom throughout a pending refresh", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  const attention = Array.from({ length: 8 }, (_, i) => ({ id: `action-${i}`, kind: "approval", status: "proposed", approval_summary: { operation_label: "外部操作", target_name: `案件 ${i}` } }));
  let holdRefresh = false;
  let releaseRefresh!: () => void;
  const pendingRefresh = new Promise<void>(resolve => { releaseRefresh = resolve; });
  await page.route("**/api/python-proxy/operations/command-center*", async route => {
    if (holdRefresh) await pendingRefresh;
    await route.fulfill({ json: { attention: holdRefresh ? [...attention].reverse() : attention } });
  });
  await page.goto("/operations?tab=overview&graph_node=action%3Aaction-0");
  await expect(page.getByTestId("operations-map-node")).toHaveCount(8);
  await expect(page.getByTestId("operations-map-inspector")).toBeVisible();
  await page.getByRole("button", { name: "拡大", exact: true }).click();
  await page.waitForTimeout(350);
  const canvas = await page.getByTestId("operations-map-flow").elementHandle();
  const viewport = page.locator(".react-flow__viewport").first();
  const transform = await viewport.getAttribute("style");
  holdRefresh = true;
  await page.getByRole("button", { name: "更新", exact: true }).click();
  try {
    await expect(page.getByRole("button", { name: "更新", exact: true })).toBeDisabled();
    expect(await canvas!.evaluate(element => element.isConnected)).toBe(true);
    await expect(page.getByTestId("operations-map-node")).toHaveCount(8);
    await expect(page.getByTestId("operations-map-inspector")).toBeVisible();
    expect(await viewport.getAttribute("style")).toBe(transform);
  } finally {
    releaseRefresh();
  }
  await expect(page.getByRole("button", { name: "更新", exact: true })).toBeEnabled();
  expect(await canvas!.evaluate(element => element.isConnected)).toBe(true);
  expect(await viewport.getAttribute("style")).toBe(transform);
  await expect(page).toHaveURL(/graph_node=action%3Aaction-0/);
});
