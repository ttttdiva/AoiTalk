import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const SESSION_ID = "session-agent-chat";
const AGENT_ID = "agent-1";

const session: {
  id: string;
  user_id: string;
  character_name: string;
  title: string;
  message_count: number;
  is_active: boolean;
  is_group_chat: boolean;
  group_character_names: string[];
  agent_id: string | null;
} = {
  id: SESSION_ID,
  user_id: "user-1",
  character_name: "aoi",
  title: "Agent bound chat",
  message_count: 1,
  is_active: true,
  is_group_chat: false,
  group_character_names: [],
  agent_id: AGENT_ID,
};

async function openAgentChat(page: Page, onBind: (body: Record<string, unknown>) => Promise<void>) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => socket.close());
  await page.route("**/api/conversations", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: { conversations: [session], total: 1 } });
      return;
    }
    await route.fallback();
  });
  await page.route(`**/api/conversations/${SESSION_ID}/resume*`, (route) =>
    route.fulfill({ json: { session, messages: [{ id: "message-1", session_id: SESSION_ID, role: "assistant", content: "準備完了", metadata: {}, branch_index: 0, is_active_branch: true }] } }),
  );
  await page.route(`**/api/conversations/${SESSION_ID}/messages*`, (route) =>
    route.fulfill({ json: { messages: [{ id: "message-1", session_id: SESSION_ID, role: "assistant", content: "準備完了", metadata: {}, branch_index: 0, is_active_branch: true }] } }),
  );
  await page.route(`**/api/conversations/${SESSION_ID}/related-tasks*`, (route) =>
    route.fulfill({ json: { tasks: [] } }),
  );
  await page.route(`**/api/python-proxy/conversations/${SESSION_ID}/generation/status*`, (route) =>
    route.fulfill({ json: { session_id: SESSION_ID, running: false, status: "idle" } }),
  );
  await page.route("**/api/python-proxy/characters*", (route) =>
    route.fulfill({ json: { characters: [{ slug: "aoi", name: "Aoi" }], current: "aoi" } }),
  );
  await page.route("**/api/python-proxy/characters/manage*", (route) =>
    route.fulfill({ json: { characters: [{ slug: "aoi", name: "Aoi", character_type: "assistant" }] } }),
  );
  await page.route("**/api/python-proxy/llm/session-settings*", (route) =>
    route.fulfill({ json: { settings: { main_route: { provider: "mock", model: "mock-model" } }, effective_main: { provider: "mock", model: "mock-model" } } }),
  );
  await page.route("**/api/python-proxy/conversation-agents*", (route) =>
    route.fulfill({ json: { success: true, agents: [{ id: AGENT_ID, display_name: "調査Agent", state: "active", latest_revision_id: "revision-1", latest_revision_version: 2 }] } }),
  );
  await page.route(`**/api/conversations/${SESSION_ID}/agent`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    await onBind(body);
    await route.fulfill({ json: { success: true, session, binding: { session_id: SESSION_ID, agent_id: AGENT_ID, agent_revision_id: "revision-1" } } });
  });
  await page.goto(`/chat?s=${SESSION_ID}`);
  await expect(page.getByTestId("chat-agent-selector")).toBeVisible();
}

test("Agent selection is session-scoped, sends CAS, and survives reload", async ({ page }) => {
  const bindBodies: Record<string, unknown>[] = [];
  await openAgentChat(page, async (body) => {
    bindBodies.push(body);
    session.agent_id = typeof body.agent_id === "string" ? body.agent_id : null;
  });

  const selector = page.getByRole("combobox", { name: "このChatのOperations Agent" });
  await expect(selector).toContainText("調査Agent");
  await selector.click();
  await page.getByRole("option", { name: "Character-only（Agentなし）" }).click();
  await expect.poll(() => bindBodies.length).toBe(1);
  expect(bindBodies[0]).toMatchObject({ agent_id: null, expected_agent_id: AGENT_ID });

  await page.reload();
  await expect(page.getByTestId("chat-agent-selector")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "このChatのOperations Agent" })).toContainText("Character-only");
});
