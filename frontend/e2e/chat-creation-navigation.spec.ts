import { expect, test } from "@playwright/test";
import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

// All mutable endpoints are intercepted. These races never create a real
// conversation, save real model settings, or start an actual generation.
for (const phase of ["create", "handoff"] as const) {
  test(`late new-chat ${phase} preserves the user's newer conversation selection`, async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => socket.close());
    const mainRoute = { provider: "mock", model: "mock-model" };
    const settings = {
      agent_team_selection: { mode: "auto", team_id: "", loaded_team_ids: [] },
      main_route: mainRoute, special_routing: {}, execution_profile_id: "",
    };
    const existing = {
      id: "existing-selection", user_id: E2E_USER_ID, character_name: "aoi",
      title: "Keep this conversation", message_count: 1, is_active: true,
      last_activity: "2026-09-08T00:00:00.000Z",
    };
    const created = { ...existing, id: "late-created", title: "Background conversation", message_count: 0 };
    const messages = [{
      id: "existing-message", session_id: existing.id, role: "assistant",
      content: "The selected conversation stays visible", metadata: {},
      branch_index: 0, is_active_branch: true,
    }];
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let waiting = false;
    const dispatched: string[] = [];
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      if (url.pathname === "/api/conversations" && method === "POST") {
        if (phase === "create") { waiting = true; await gate; }
        await route.fulfill({ json: { session: created } });
        return;
      }
      if (url.pathname === "/api/conversations") {
        await route.fulfill({ json: { conversations: [existing], total: 1 } });
        return;
      }
      if (url.pathname === `/api/conversations/${existing.id}/resume`) {
        await route.fulfill({ json: { session: existing, messages } });
        return;
      }
      if (url.pathname === `/api/conversations/${existing.id}/messages`) {
        await route.fulfill({ json: { messages } });
        return;
      }
      if (url.pathname.endsWith("/related-tasks")) {
        await route.fulfill({ json: { tasks: [] } });
        return;
      }
      if (url.pathname.endsWith("/generation/status")) {
        await route.fulfill({ json: { session_id: existing.id, running: false, status: "idle" } });
        return;
      }
      if (url.pathname === `/api/python-proxy/conversations/${created.id}/dispatch`) {
        dispatched.push(created.id);
        await route.fulfill({ json: { success: true, queued: true, session_id: created.id, agent_run_id: "fixture-run" } });
        return;
      }
      if (url.pathname === "/api/python-proxy/characters") {
        await route.fulfill({ json: { current: "aoi", characters: ["aoi"] } });
        return;
      }
      if (url.pathname === "/api/python-proxy/llm/new-chat-defaults") {
        await route.fulfill({ json: { effective_main: mainRoute, last_used_main: mainRoute } });
        return;
      }
      if (url.pathname === "/api/python-proxy/llm/session-settings") {
        const body = method === "PUT" ? route.request().postDataJSON() : null;
        if (phase === "handoff" && body?.session_id === created.id) { waiting = true; await gate; }
        await route.fulfill({ json: { settings: body?.settings ?? settings, effective_main: mainRoute } });
        return;
      }
      await route.fallback();
    });

    await page.goto("/chat");
    await expect(page.getByTestId("chat-model-control")).toContainText("mock-model");
    await page.getByRole("textbox", { name: "メッセージ入力", exact: true }).fill("A message submitted before navigation");
    await page.getByRole("button", { name: "送信", exact: true }).click();
    await expect.poll(() => waiting).toBe(true);
    await page.getByText(existing.title, { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/chat\\?s=${existing.id}$`));
    await expect(page.getByText(messages[0].content, { exact: true })).toBeVisible();
    release();
    // Dispatch is past the activation branch, so this cannot pass merely
    // because the deferred create/handoff response has not been handled yet.
    await expect.poll(() => dispatched).toEqual([created.id]);
    await expect(page).toHaveURL(new RegExp(`/chat\\?s=${existing.id}$`));
    await expect(page.getByText(messages[0].content, { exact: true })).toBeVisible();
    await expect(page.getByText(created.title, { exact: true })).toBeVisible();
  });
}
