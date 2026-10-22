import { expect, test, type WebSocketRoute } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const SESSION = "session-e2e";
const digest = (value: string) => `sha256:${value.repeat(64)}`;
const binding = { plan_id: "synthetic-routine", revision: 1, plan_digest: digest("a"),
  action_digest: digest("b"), context_selection_hash: digest("c"),
  evidence_hash_set_digest: digest("d"), material_action_preview_digest: digest("e") };
const approval = () => ({
  session_id: SESSION, request_id: "synthetic-approval", revision: 1, plan_revision: 1,
  summary: "この手順を保存します: 架空サロン / 2030-01-02 10:00 / 4000円",
  plan_text: "架空サロン / 2030-01-02 10:00 / 4000円\n" + "手順を確認してください。\n".repeat(20),
  plan_binding: binding, plan_digest: binding.plan_digest, action_digest: binding.action_digest,
  material_action_preview_digest: binding.material_action_preview_digest,
  material_action_preview: { version: 1, action_count: 1, actions: [{ index: 0, tool: "manage_skill",
    arguments: { operation: "create", prompt_template: "架空サロンの手順。".repeat(100),
      browser_routine: { confirmation: { target: "架空サロン", datetime: "2030-01-02 10:00", price: "4000円" },
        completion_text: "架空予約が完了しました" } }, dynamic_fields: [] }] },
});

for (const viewport of [{ width: 1366, height: 768 }, { width: 390, height: 844 }]) {
  test.describe(`Skill承認 ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("長い保存条件とdigestが画面内に収まり、同じbindingで承認する", async ({ page }) => {
      await addAuthCookie(page);
      await mockAuthenticatedApis(page);
      await page.route("**/api/python-proxy/characters", route => route.fulfill({ json: { characters: ["aoi"], current: "aoi" } }));
      await page.route("**/api/python-proxy/llm/session-settings*", route => route.fulfill({ json: {
        settings: { main_route: { provider: "mock", model: "mock" }, special_routing: {} },
        effective_main: { provider: "mock", model: "mock" },
      } }));
      await page.route(`**/api/python-proxy/conversations/${SESSION}/generation/status`, route => route.fulfill({
        json: { session_id: SESSION, running: false, status: "idle" },
      }));
      const sockets: WebSocketRoute[] = [];
      const decisions: Array<{ type: string; data: Record<string, unknown> }> = [];
      await page.routeWebSocket(/\/ws(?:\?|$)/, socket => {
        sockets.push(socket);
        socket.onMessage(message => {
          const packet = JSON.parse(message.toString());
          if (packet.type === "plan_approval_response") decisions.push(packet);
        });
      });
      await page.goto(`/chat?s=${SESSION}`);
      await expect.poll(() => sockets.length).toBeGreaterThan(0);
      sockets.at(-1)!.send(JSON.stringify({ type: "plan_approval_request", data: approval() }));
      const dialog = page.getByRole("dialog", { name: "計画の承認" });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByTestId("plan-approval-actions")).toContainText("2030-01-02 10:00");
      const bounds = await dialog.boundingBox();
      expect(bounds).not.toBeNull();
      expect(bounds!.x).toBeGreaterThanOrEqual(0);
      expect(bounds!.y).toBeGreaterThanOrEqual(0);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
      expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      const approve = dialog.getByRole("button", { name: "承認して実行" });
      await approve.scrollIntoViewIfNeeded();
      await expect(approve).toBeInViewport({ ratio: 1 });
      await approve.click();
      await expect.poll(() => decisions.length).toBe(1);
      expect(decisions[0].data.plan_binding).toEqual(binding);
      expect(decisions[0].data.action).toBe("approve");

      // Neighboring malformed-request regression: never approve mismatched evidence.
      sockets.at(-1)!.send(JSON.stringify({ type: "plan_approval_request",
        data: { ...approval(), request_id: "changed", material_action_preview_digest: digest("f") } }));
      await expect(dialog).toBeVisible();
      await expect(approve).toBeDisabled();
      await dialog.getByRole("button", { name: "キャンセル", exact: true }).click();
      await expect.poll(() => decisions.length).toBe(2);
      expect(decisions[1].data.action).toBe("cancel");
    });
  });
}
