import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

// These endpoints return fixtures only; no official login or model is invoked.
async function fixture(page: Page) {
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  const state = { owner: E2E_USER_ID, connected: true, status503: false,
    hold: null as Promise<void> | null, profileReads: 0, posts: [] as Record<string, unknown>[], cancelled: false };
  const task = { task_id: "a".repeat(32), status: "running", is_terminal: false, goal: "Saved fixture task",
    workspace: "/fixture", mode: "read_only", summary_text: "Fixture report", counters: { llm_calls: 0, tool_calls: 0 }, changed_files: [] };
  await page.route("**/api/auth/status", route => route.fulfill({ json: { authenticated: true, user: { id: state.owner, role: "admin" } } }));
  await page.route("**/api/python-proxy/api/local-agent/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/codex-app-profile/status")) {
      state.profileReads++; const held = state.hold; if (held) await held;
      return route.fulfill(state.status503 ? { status: 503, json: { detail: "profile status503" } } : { json: {
        enabled: true, connector_available: true, connected: state.connected, state: state.connected ? "connected" : "disconnected",
        model_ready: false, generation: 1, profile_id: "fixture", login_started: false,
      } });
    }
    if (path.endsWith("/codex-app-profile/disconnect")) {
      state.connected = false;
      return route.fulfill({ json: { enabled: true, connector_available: true, connected: false, state: "disconnected", model_ready: false, generation: 2, profile_id: "fixture", login_started: false } });
    }
    if (path.endsWith("/status")) return route.fulfill({ json: { enabled: true, running: true, model: "gpt-5.6-sol", allowed_roots: ["/fixture"], max_concurrency: 1, queued: 0, limits: {},
      supports_task_routes: true, supported_providers: ["codex-cli"], default_route: { provider: "codex-cli", model: "gpt-5.6-sol", effort: "high" },
      codex_app_profile_connection_enabled: true, codex_app_profile_connector_available: true } });
    if (path.endsWith("/tasks")) {
      if (route.request().method() === "POST") {
        state.posts.push(route.request().postDataJSON() as Record<string, unknown>);
        return route.fulfill({ json: { task: { ...task, status: "completed", is_terminal: true }, created: true } });
      }
      return route.fulfill({ json: { tasks: [task] } });
    }
    if (path.endsWith("/cancel")) { state.cancelled = true; return route.fulfill({ json: { task: { ...task, status: "cancelled", is_terminal: true } } }); }
    if (path.endsWith("/tail")) return route.fulfill({ json: { events: [], truncated: false } });
    if (path.endsWith(`/tasks/${task.task_id}`)) return route.fulfill({ json: { task: state.cancelled ? { ...task, status: "cancelled", is_terminal: true } : task } });
    return route.fulfill({ status: 404, json: { detail: "fixture only" } });
  });
  await page.goto("/operations?tab=work");
  const panel = page.getByTestId("operations-local-tasks");
  await panel.getByLabel("作業フォルダー").fill("/fixture"); await panel.getByLabel("目的", { exact: true }).fill("Read fixture");
  return { state, panel };
}

test("profile choice survives review, unavailable status, cancellation, expiry and reload; host requires selection", async ({ page }) => {
  const { state, panel } = await fixture(page);
  const connection = panel.getByLabel("Codex接続先"), submit = panel.getByRole("button", { name: "タスクを保存して実行" });
  await panel.getByRole("button", { name: "接続条件を確認" }).click();
  await connection.click(); await page.getByRole("option", { name: "本人が接続した専用profile" }).click();
  let release!: () => void; state.hold = new Promise<void>(resolve => { release = resolve; });
  const reads = state.profileReads;
  await panel.getByRole("button", { name: "接続条件を確認" }).click(); await expect.poll(() => state.profileReads).toBe(reads + 1);
  await expect(connection).toContainText("本人が接続した専用profile"); await expect(submit).toBeDisabled();
  await panel.getByRole("form").evaluate(form => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(state.posts).toHaveLength(0); state.hold = null; release(); await expect(submit).toBeEnabled();

  state.status503 = true; await panel.getByRole("button", { name: "接続条件を確認" }).click();
  await expect(panel.getByRole("alert")).toContainText("profile status503");
  await expect(connection).toContainText("本人が接続した専用profile"); await expect(submit).toBeDisabled();
  state.status503 = false; await panel.getByRole("button", { name: "接続条件を確認" }).click(); await expect(submit).toBeEnabled();

  await panel.getByRole("button", { name: /Saved fixture task/u }).click(); await panel.getByRole("button", { name: "このタスクを中止" }).click();
  await expect(submit).toBeDisabled(); await expect(connection).toContainText("本人が接続した専用profile");
  await panel.getByRole("button", { name: "接続条件を確認" }).click(); await expect(submit).toBeEnabled();
  state.connected = false; await submit.click();
  await expect(panel.getByRole("alert")).toContainText("専用Codex接続を確認してから実行してください");
  await expect(submit).toBeDisabled(); expect(state.posts).toHaveLength(0);

  await panel.getByRole("button", { name: "切断・承認待ちを破棄" }).click();
  await expect(connection).toContainText("本人が接続した専用profile"); await expect(submit).toBeDisabled();
  const beforeReload = state.profileReads; await page.reload();
  await expect(connection).toContainText("本人が接続した専用profile"); await expect(submit).toBeDisabled(); expect(state.profileReads).toBe(beforeReload);
  await panel.getByLabel("作業フォルダー").fill("/fixture"); await panel.getByLabel("目的", { exact: true }).fill("Read fixture");
  await connection.click(); await page.getByRole("option", { name: "既存のホスト接続" }).click(); await submit.click();
  await expect.poll(() => state.posts.length).toBe(1); expect(state.posts[0]).not.toHaveProperty("codex_connection");
});

test("owner switch discards a delayed confirmation without adopting the previous owner route", async ({ page }) => {
  const { state, panel } = await fixture(page);
  await panel.getByRole("button", { name: "接続条件を確認" }).click();
  await panel.getByLabel("Codex接続先").click(); await page.getByRole("option", { name: "本人が接続した専用profile" }).click();
  let release!: () => void; state.hold = new Promise<void>(resolve => { release = resolve; });
  const reads = state.profileReads; await panel.getByRole("button", { name: "接続条件を確認" }).click();
  await expect.poll(() => state.profileReads).toBe(reads + 1);
  state.owner = "22222222-2222-4222-8222-222222222222"; state.hold = null; release();
  await expect(panel.getByRole("alert")).toContainText("ログイン中の所有者を確認できません");
  await expect(panel.getByRole("form")).toHaveCount(0); expect(state.posts).toHaveLength(0);
  await page.reload(); await expect(panel.getByRole("form")).toBeVisible();
  await expect(panel.getByLabel("Codex接続先")).toHaveCount(0);
  const preferences = await page.evaluate(([oldOwner, newOwner]) => [localStorage.getItem(`aoitalk:local-task-codex-route:${oldOwner}`), localStorage.getItem(`aoitalk:local-task-codex-route:${newOwner}`)], [E2E_USER_ID, state.owner]);
  expect(preferences).toEqual(["app_profile", null]); expect(state.posts).toHaveLength(0);
});
