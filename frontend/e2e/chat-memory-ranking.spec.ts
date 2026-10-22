import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const sessionId = "session-e2e";
const runId = "memory-ranking-run";
const messageId = "memory-ranking-answer";
const items = Array.from({ length: 8 }, (_, index) => ({
  memory_id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
  title: `順位確認用の長い記憶タイトル ${index + 1}`,
  excerpt: `確認用の原本 ${index + 1}`,
  version: 1, scope: "user", candidate_rank: index + 1, selected: true,
  selection_reason: "pinned", weighted_score: .8 - index * .02,
  jev_relevance: null, updated_at: null, source: "scoped_memory",
}));

async function mockMemoryApis(page: Page) {
      await addAuthCookie(page);
      await mockAuthenticatedApis(page);
      await page.routeWebSocket(/\/ws(?:\?|$)/, () => {});
      const messages = [{
        id: messageId, session_id: sessionId, role: "assistant", content: "確認用の回答",
        metadata: { agent_run_id: runId }, parent_message_id: null,
        branch_index: 0, is_active_branch: true,
      }];
      await page.route(`**/api/conversations/${sessionId}/resume`, route => route.fulfill({ json: {
        session: { id: sessionId, user_id: "user-1", title: "記憶表示の確認", character_name: "aoi", is_active: true }, messages,
      } }));
      await page.route(`**/api/conversations/${sessionId}/messages`, route => route.fulfill({ json: { messages } }));
      await page.route(`**/api/python-proxy/conversations/${sessionId}/generation/status`, route => route.fulfill({ json: { session_id: sessionId, running: false, status: "idle" } }));
      const common = { schema_version: 1, pipeline: "automatic_context_memory", engine: "host" };
      await page.route(`**/api/python-proxy/conversations/${sessionId}/memory-selection*`, route => route.fulfill({ json: {
        status: "available", binding: { session_id: sessionId, message_id: messageId, agent_run_id: runId, active_branch: true },
        observation: { ...common, history: [], items, stages: {
          scope_selection: { ...common, stage: "scope_selection", status: "completed", candidate_count: 8 },
          relevance_decision: { ...common, stage: "relevance_decision", status: "failed" },
          context_budget: { ...common, stage: "context_budget", status: "completed", candidate_count: 8, selected_count: 8, excluded_count: 0 },
        } }, tool_recall: null,
      } }));
      const reads: string[] = [];
      await page.route("**/api/python-proxy/api/memories/*", route => {
        const id = new URL(route.request().url()).pathname.split("/").at(-1)!;
        reads.push(id);
        const item = items.find(item => item.memory_id === id)!;
        return route.fulfill({ json: { success: true, memory: { ...item, scope_type: item.scope, status: "active", content: item.excerpt, expires_at: null } } });
      });
      return reads;
}

for (const reducedMotion of ["no-preference", "reduce"] as const) {
  test.describe(`記憶の採用表示 ${reducedMotion}`, () => {
    test.use({ viewport: { width: 1188, height: 995 }, contextOptions: { reducedMotion } });
    test("8件が重ならず、各行の原本と失敗状態を確認できる", async ({ page }) => {
      const reads = await mockMemoryApis(page);
      await page.goto(`/chat?s=${sessionId}&view=memory&memory_view=process`);
      const rows = page.locator(".rank-row");
      await expect(rows).toHaveCount(8);
      await expect(page.getByRole("tab", { name: "2. Jev の関連判断 失敗" }).getByLabel("失敗", { exact: true })).toBeVisible();
      const bounds = await rows.evaluateAll(elements => elements.map(element => {
        const row = element.getBoundingClientRect();
        const title = element.querySelector("b")!.getBoundingClientRect();
        return { top: row.top, bottom: row.bottom, width: row.width, titleWidth: title.width };
      }));
      bounds.forEach((row, index) => {
        if (index) expect(row.top).toBeGreaterThanOrEqual(bounds[index - 1].bottom - .5);
        expect(row.titleWidth).toBeGreaterThan(row.width * .85);
      });
      for (let index = 0; index < items.length; index++) {
        await rows.nth(index).click();
        const dialog = page.getByRole("dialog", { name: items[index].title, exact: true });
        await expect(dialog).toBeVisible();
        await expect(dialog.getByText(items[index].excerpt, { exact: true })).toBeVisible();
        expect(reads.at(-1)).toBe(items[index].memory_id);
        await dialog.getByRole("button", { name: "とじる", exact: true }).click();
      }
    });
  });
}

for (const width of [1188, 1440, 390]) {
  test.describe(`記憶チャットの表示幅 ${width}`, () => {
    test.use({ viewport: { width, height: 995 } });
    test("通常表示の設定とcomposerを保持し、記憶表示の幅と原本sidebarを確保する", async ({ page }, testInfo) => {
      await mockMemoryApis(page);
      await page.addInitScript(() => localStorage.setItem("aoitalk:context-rail-width", "420"));
      await page.goto(`/chat?s=${sessionId}&view=normal`);
      const composer = page.locator("[data-chat-composer] textarea").first();
      await composer.fill("MEMUI-layout-draft");
      const originalComposer = await composer.elementHandle();
      if (!originalComposer) throw new Error("Composer was not mounted");
      const navigation = page.getByTestId("workspace-navigation-frame");
      const normalNavState = width >= 768 ? await navigation.getAttribute("data-sidebar-state") : null;
      const cookieBefore = await page.context().cookies();
      const sidebarCookie = cookieBefore.find(cookie => cookie.name === "sidebar_state")?.value ?? null;
      const main = page.locator('[data-shell-region="main-canvas"]');
      const normalWidth = (await main.boundingBox())!.width;
      const rail = page.locator('[data-shell-region="context-rail"]');
      const originalRail = width >= 768 ? await rail.elementHandle() : null;
      await page.getByRole("button", { name: "記憶可視化", exact: true }).click();
      await expect(page.getByRole("region", { name: "記憶可視化チャット" })).toBeVisible();
      await expect(composer).toHaveValue("MEMUI-layout-draft");
      expect(await originalComposer.evaluate(element => element.isConnected)).toBe(true);
      await expect(page.locator("[data-chat-composer]")).toHaveCount(1);
      const thoughts = page.locator(".brain-memories");
      await expect(thoughts).toBeVisible();
      const thoughtsBounds = (await thoughts.boundingBox())!;
      expect(thoughtsBounds.x).toBeGreaterThanOrEqual(0);
      expect(thoughtsBounds.x + thoughtsBounds.width).toBeLessThanOrEqual(width + 1);
      await page.locator(".memory-reference").evaluate(async element => {
        const animations = element.getAnimations({ subtree: true }).filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity);
        await Promise.all(animations.map(animation => animation.finished.catch(() => {})));
      });
      await page.screenshot({ path: testInfo.outputPath("memory-layout.png") });
      if (width >= 768) {
        await expect(rail).toBeHidden();
        await expect(rail).toHaveCount(1);
        expect((await main.boundingBox())!.width).toBeGreaterThan(normalWidth + 200);
        expect((await page.locator(".brain-main").boundingBox())!.width).toBeGreaterThan(500);
        expect(await originalRail!.evaluate(element => element.isConnected)).toBe(true);
        await page.getByRole("button", { name: "詳細パネルを開く", exact: true }).click();
        await expect(rail).toBeVisible();
        await page.getByRole("separator", { name: "Context Railの幅" }).press("Shift+ArrowLeft");
        expect(await page.evaluate(() => localStorage.getItem("aoitalk:context-rail-width"))).toBe("420");
        await page.keyboard.press("Escape");
        await expect(rail).toBeHidden();
        await expect(navigation).toHaveAttribute("data-sidebar-state", normalNavState!);
      } else {
        const graphBounds = (await page.locator(".brain-main").boundingBox())!;
        expect(thoughtsBounds.y).toBeGreaterThanOrEqual(graphBounds.y + graphBounds.height - 1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
        await composer.scrollIntoViewIfNeeded();
        await expect(composer).toBeInViewport({ ratio: 1 });
        await expect(composer).toBeEditable();
      }
      await page.getByRole("button", { name: "通常", exact: true }).click();
      await expect(page.getByRole("region", { name: "記憶可視化チャット" })).toHaveCount(0);
      await expect(composer).toHaveValue("MEMUI-layout-draft");
      expect(await originalComposer.evaluate(element => element.isConnected)).toBe(true);
      expect(await page.evaluate(() => localStorage.getItem("aoitalk:context-rail-width"))).toBe("420");
      expect((await page.context().cookies()).find(cookie => cookie.name === "sidebar_state")?.value ?? null).toBe(sidebarCookie);
      if (width >= 768) {
        await expect(rail).toBeVisible();
        expect(await originalRail!.evaluate(element => element.isConnected)).toBe(true);
        expect((await main.boundingBox())!.width).toBeCloseTo(normalWidth, 0);
      }
    });
  });
}
