import { expect, test, type WebSocketRoute } from "@playwright/test";
import { SESSION_COOKIE_NAME } from "../src/lib/session-cookie";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

for (const reducedMotion of ["no-preference", "reduce"] as const) {
  test.describe(`記憶候補の更新待ち ${reducedMotion}`, () => {
    test.use({ viewport: { width: 1440, height: 995 }, contextOptions: { reducedMotion } });

    test("完了通知が候補GETに先行しても未確定の候補を除外扱いにしない", async ({ page }, testInfo) => {
      // A read-only browser harness can supply its normal authenticated session.
      // The canonical isolated E2E suite otherwise supplies its fixture account.
      if (!(await page.context().cookies()).some(cookie => cookie.name === SESSION_COOKIE_NAME)) {
        await addAuthCookie(page);
      }
      await mockAuthenticatedApis(page);
      const sessionId = "session-e2e", runId = "projection-refresh-run";
      let clientId = "", sequence = 0, heldReads = 0, holdFinalReads = false;
      const sockets: WebSocketRoute[] = [];
      await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
        sockets.push(socket);
        socket.onMessage(message => {
          const packet = JSON.parse(message.toString());
          if (packet.type === "user_message") clientId = packet.data.client_message_id;
        });
      });
      await page.route(`**/api/python-proxy/conversations/${sessionId}/dispatch`, route => {
        clientId = route.request().postDataJSON().client_message_id;
        return route.fulfill({ json: { success: true, queued: false, session_id: sessionId, agent_run_id: runId } });
      });
      await page.route(`**/api/python-proxy/conversations/${sessionId}/generation/status`, route => route.fulfill({ json: {
        session_id: sessionId, running: Boolean(clientId), status: clientId ? "generating" : "idle", agent_run_id: clientId ? runId : null,
      } }));
      await page.route("**/api/python-proxy/llm/session-settings*", route => route.fulfill({ json: {
        settings: { main_route: { provider: "mock", model: "mock-model" }, special_routing: {} },
        effective_main: { provider: "mock", model: "mock-model" },
      } }));
      const common = { schema_version: 1, pipeline: "automatic_context_memory", engine: "host" };
      const scope = { ...common, stage: "scope_selection", status: "completed", candidate_count: 3 };
      const relevancePending = { ...common, engine: "jev", stage: "relevance_decision", status: "running" };
      const relevanceComplete = { ...common, engine: "jev", stage: "relevance_decision", status: "completed" };
      const contextComplete = { ...common, stage: "context_budget", status: "completed", candidate_count: 3, selected_count: 2, excluded_count: 1 };
      const partialItems = ["安全条件", "担当者", "無関係な候補"].map((title, index) => ({
        memory_id: `projection-original-${index}`, title, excerpt: `合成原本 ${title}`, version: 1,
        scope: "user", candidate_rank: index + 1, selected: false, selection_reason: "semantic_match",
        weighted_score: null, jev_relevance: null, updated_at: null, source: "scoped_memory",
      }));
      const decidedItems = partialItems.map((item, index) => ({
        ...item, selected: index < 2, selection_reason: index < 2 ? "semantic_match" : "jev_low_relevance",
        jev_relevance: [.94, .87, .05][index], weighted_score: [.593, .6047, .5393][index],
      }));
      let release!: () => void;
      const refreshGate = new Promise<void>(resolve => { release = resolve; });
      await page.route(`**/api/python-proxy/conversations/${sessionId}/memory-selection*`, async route => {
        const finalRead = holdFinalReads;
        if (finalRead) { heldReads++; await refreshGate; }
        const stages = finalRead
          ? { scope_selection: scope, relevance_decision: relevanceComplete, context_budget: contextComplete }
          : { scope_selection: scope, relevance_decision: relevancePending };
        await route.fulfill({ json: { status: "available", binding: {
          session_id: sessionId, agent_run_id: runId, active_branch: true,
        }, observation: { ...common, stages, history: Object.values(stages), items: finalRead ? decidedItems : partialItems }, tool_recall: null } });
      });
      await page.goto(`/chat?s=${sessionId}&view=memory`);
      await expect.poll(() => sockets.length).toBeGreaterThan(0);
      await page.getByRole("textbox", { name: "メッセージ入力", exact: true }).fill("合成fixtureの採否を確認");
      await page.getByRole("button", { name: "送信", exact: true }).click();
      await expect.poll(() => clientId).not.toBe("");
      const send = (type: string, extra: object = {}) => sockets.at(-1)!.send(JSON.stringify({
        type, session_id: sessionId, agent_run_id: runId, client_message_id: clientId, event_sequence: ++sequence,
        event_id: `projection-event-${sequence}`, ...extra,
      }));
      send("stream_start");
      send("memory_selection_progress", scope);
      send("memory_selection_progress", relevancePending);
      const cards = page.locator(".thought-card");
      await expect(cards).toHaveCount(3);
      await expect(cards.locator(".thought-title b")).toHaveText(["参照候補", "参照候補", "参照候補"]);
      await expect(page.locator(".brain-run-status")).toContainText("2.");
      await expect(page.locator(".brain-run-status")).toContainText("処理中");

      holdFinalReads = true;
      send("memory_selection_progress", relevanceComplete);
      send("memory_selection_progress", contextComplete);
      await expect.poll(() => heldReads).toBeGreaterThan(0);
      await expect(page.locator(".brain-run-status")).toContainText("3.");
      await expect(page.locator(".brain-run-status")).toContainText("完了");
      await page.getByRole("button", { name: "関係図", exact: true }).click();
      await expect(page.locator(".brain-node.action text")).toHaveText(["判断を確認中", "判断を確認中", "判断を確認中"]);
      await expect(cards.locator(".thought-title b")).toHaveText(["参照候補", "参照候補", "参照候補"]);
      await page.getByRole("button", { name: "採用のようす", exact: true }).click();
      await expect(page.locator(".rank-row")).toHaveCount(3);
      await expect(page.locator(".rank-row.muted")).toHaveCount(0);
      await expect(page.locator(".memory-reference").getByText("採用せず", { exact: true })).toHaveCount(0);
      await expect(page.locator(".rank-row small")).toHaveText(["", "", ""]);
      await page.screenshot({ path: testInfo.outputPath("decision-refresh-pending.png") });

      release();
      await expect(cards.locator(".thought-title b")).toHaveText(["文脈に採用", "文脈に採用", "採用せず"]);
      await expect(page.locator(".rank-row.muted")).toHaveCount(1);
      await expect(page.locator(".rank-row small")).toHaveText([
        "選択重み 0.593 · Jev 関連度 0.94 · 文脈に採用",
        "選択重み 0.605 · Jev 関連度 0.87 · 文脈に採用",
        "選択重み 0.539 · Jev 関連度 0.05 · 採用せず",
      ]);
      await page.getByRole("button", { name: "関係図", exact: true }).click();
      await expect(page.locator(".brain-node.action text")).toHaveText(["文脈に採用", "文脈に採用", "採用せず"]);
      if (reducedMotion === "reduce") {
        await expect(page.locator(".memory-reference")).toHaveClass(/reduce-memory-motion/);
        await expect(page.locator(".is-drawing,.memory-flight-overlay")).toHaveCount(0);
      }
      await page.screenshot({ path: testInfo.outputPath("decision-refresh-settled.png") });
      send("stream_cancelled", { status: "cancelled" });
      await expect(page.locator(".memory-reference")).toHaveAttribute("data-run-phase", "idle");
      await expect(page.locator(".is-drawing,.memory-flight-overlay")).toHaveCount(0);
    });
  });
}
