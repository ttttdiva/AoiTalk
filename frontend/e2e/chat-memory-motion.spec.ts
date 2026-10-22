import { expect, test, type WebSocketRoute } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

for (const reducedMotion of ["no-preference", "reduce"] as const) {
  test.describe(`原本到着からの記憶描画 ${reducedMotion}`, () => {
    test.use({ viewport: { width: 1440, height: 720 }, contextOptions: { reducedMotion } });
    test("遅延GETの後に描画し、可視原本だけへ飛び、停止で消える", async ({ page }, testInfo) => {
      await addAuthCookie(page);
      await mockAuthenticatedApis(page);
      const sessionId = "session-e2e", runId = "motion-run";
      let clientId = "", sequence = 0, reads = 0;
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
      let release!: () => void;
      const originalsGate = new Promise<void>(resolve => { release = resolve; });
      const common = { schema_version: 1, pipeline: "automatic_context_memory", engine: "host" };
      const stages = {
        scope_selection: { ...common, stage: "scope_selection", status: "completed", candidate_count: 8 },
        relevance_decision: { ...common, stage: "relevance_decision", status: "failed" },
        context_budget: { ...common, stage: "context_budget", status: "completed", candidate_count: 8, selected_count: 8, excluded_count: 0 },
      };
      await page.route(`**/api/python-proxy/conversations/${sessionId}/memory-selection*`, async route => {
        reads++;
        await originalsGate;
        await route.fulfill({ json: { status: "available", binding: {
          session_id: sessionId, agent_run_id: runId, active_branch: true,
        }, observation: { ...common, stages, history: Object.values(stages), items: Array.from({ length: 8 }, (_, index) => ({
          memory_id: `motion-original-${index}`, title: `実際の候補 ${index}`, excerpt: `原本 ${index}`, version: 1,
          scope: "user", candidate_rank: index + 1, selected: true, selection_reason: "pinned", weighted_score: .8,
          jev_relevance: null, updated_at: null, source: "scoped_memory",
        })) }, tool_recall: null } });
      });
      await page.goto(`/chat?s=${sessionId}&view=memory`);
      await expect.poll(() => sockets.length).toBeGreaterThan(0);
      await page.getByRole("textbox", { name: "メッセージ入力", exact: true }).fill("描画確認");
      await page.getByRole("button", { name: "送信", exact: true }).click();
      await expect.poll(() => clientId).not.toBe("");
      const send = (type: string, extra: object = {}) => sockets.at(-1)!.send(JSON.stringify({
        type, session_id: sessionId, agent_run_id: runId, client_message_id: clientId, event_sequence: ++sequence,
        event_id: `motion-event-${sequence}`, ...extra,
      }));
      send("stream_start");
      await expect(page.getByRole("button", { name: "応答生成を停止", exact: true })).toBeVisible();
      send("stream_token", { content: "原本を待っていても回答は届きます" });
      await expect(page.locator(".brain-latest p")).toContainText("回答は届きます");
      for (const observation of Object.values(stages)) {
        send("memory_selection_progress", observation);
        await expect(page.locator(".brain-run-status")).toContainText(observation.stage === "scope_selection" ? "1." : observation.stage === "relevance_decision" ? "2." : "3.");
      }
      await expect.poll(() => reads).toBeGreaterThan(0);
      // Longer than the former empty connecting clock, reproducing the missed draw.
      await page.waitForTimeout(2200);
      await expect(page.locator(".memory-reference")).toHaveAttribute("data-run-phase", "idle");
      await expect(page.locator(".brain-graph.is-drawing")).toHaveCount(0);
      release();
      await expect(page.locator(".thought-card")).toHaveCount(8);
      if (reducedMotion === "reduce") {
        await expect(page.locator(".brain-relation .line")).toHaveCount(24);
        await expect(page.locator(".is-drawing,.memory-flight-overlay")).toHaveCount(0);
      } else {
        await expect(page.locator(".brain-graph.is-drawing")).toBeVisible();
        expect(await page.locator(".brain-relation .line").first().evaluate(element => getComputedStyle(element).animationName)).toBe("memory-draw");
        await page.screenshot({ path: testInfo.outputPath("original-ready-drawing.png") });
        await expect(page.locator(".memory-reference")).toHaveAttribute("data-run-phase", "collecting");
        await expect.poll(() => page.locator("[data-flight-id]").count()).toBeGreaterThan(0);
        const geometry = await page.locator(".memory-reference").evaluate(root => {
          const graph = root.querySelector(".brain-graph-scroll")!.getBoundingClientRect();
          const sidebar = root.querySelector(".brain-memories")!.getBoundingClientRect();
          const clip = root.querySelector(".memory-flight-overlay clipPath rect")!;
          const composer = document.querySelector(".memory-host-composer")!.getBoundingClientRect();
          const flights = Array.from(root.querySelectorAll("[data-flight-id]")).map(flight => {
            const id = flight.getAttribute("data-flight-id");
            const a = Array.from(root.querySelectorAll("[data-flight-anchor]")).find(node => node.getAttribute("data-flight-anchor") === id)!.getBoundingClientRect();
            const b = Array.from(root.querySelectorAll("[data-thought-id]")).find(node => node.getAttribute("data-thought-id") === id)!.getBoundingClientRect();
            return { sourceY: a.y + a.height / 2, targetY: b.y + b.height / 2 };
          });
          return { graphTop: graph.top, graphBottom: graph.bottom, sidebarTop: sidebar.top, sidebarBottom: Math.min(sidebar.bottom, innerHeight),
            clipBottom: Number(clip.getAttribute("y")) + Number(clip.getAttribute("height")), composerTop: composer.top, flights };
        });
        geometry.flights.forEach(flight => {
          expect(flight.sourceY).toBeGreaterThanOrEqual(geometry.graphTop);
          expect(flight.sourceY).toBeLessThanOrEqual(geometry.graphBottom);
          expect(flight.targetY).toBeGreaterThanOrEqual(geometry.sidebarTop);
          expect(flight.targetY).toBeLessThanOrEqual(geometry.sidebarBottom);
        });
        expect(geometry.flights.length).toBeLessThan(8);
        expect(geometry.clipBottom).toBeLessThanOrEqual(geometry.composerTop + 1);
        await page.screenshot({ path: testInfo.outputPath("visible-original-flights.png") });
      }
      send("stream_cancelled", { status: "cancelled" });
      await expect(page.locator(".memory-reference")).toHaveAttribute("data-run-phase", "idle");
      await expect(page.locator(".is-drawing,.memory-flight-overlay")).toHaveCount(0);
    });
  });
}
