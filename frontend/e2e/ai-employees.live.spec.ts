import { expect, test, type Page } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// Invoked only by the isolated AI employee QA harness. Credentials originate
// in repo .env.qa-login and are injected in memory; no trace/video captures.
const baseURL = process.env.AI_EMPLOYEE_QA_BASE_URL;
const backendURL = process.env.AI_EMPLOYEE_QA_BACKEND_URL;
const username = process.env.AI_EMPLOYEE_QA_EMAIL;
const password = process.env.AI_EMPLOYEE_QA_PASSWORD;
const projectId = process.env.AI_EMPLOYEE_QA_PROJECT_ID;
const spaceId = process.env.AI_EMPLOYEE_QA_SPACE_ID;
const conversationId = process.env.AI_EMPLOYEE_QA_CONVERSATION_ID;
const expectedStatus = process.env.AI_EMPLOYEE_QA_EXPECTED_ACTION_STATUS ?? "succeeded";
test.use({ baseURL, trace: "off", screenshot: "off", video: "off" });

async function choose(page: Page, label: string, option: string | RegExp) {
  await test.step(`選択: ${label}`, async () => {
    const control = page.getByRole("combobox", { name: label, exact: true });
    await expect(control).toBeEnabled();
    await control.click();
    await page.getByRole("listbox").getByRole("option", { name: option, exact: typeof option === "string" }).click();
    await expect(control).toContainText(option);
  });
}
async function capture(page: Page, name: string) {
  const directory = process.env.AI_EMPLOYEE_QA_SCREENSHOT_DIR;
  if (directory) await page.screenshot({ path: join(directory, `${name}.png`), fullPage: true });
}
test("AI社員: authenticated draft, role, scopes, bounded fixture policy, rule, revision and deep links", async ({ page }) => {
  test.skip(!baseURL || !backendURL || !username || !password || !projectId || !spaceId, "Requires isolated AI employee QA harness");
  test.setTimeout(240_000);
  page.setDefaultTimeout(15_000);
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const serverErrors: { status: number; path: string }[] = [];
  const offScopeRequests: { kind: string; origin: string; path: string }[] = [];
  const allowedOrigins = new Set([new URL(baseURL!).origin, new URL(backendURL!).origin]);
  await page.context().route(/^https?:\/\//u, async route => {
    const url = new URL(route.request().url());
    if (allowedOrigins.has(url.origin)) await route.continue();
    else { offScopeRequests.push({ kind: "http", origin: url.origin, path: url.pathname }); await route.abort("blockedbyclient"); }
  });
  await page.routeWebSocket("**/*", socket => {
    const url = new URL(socket.url()); url.protocol = url.protocol === "wss:" ? "https:" : "http:";
    if (allowedOrigins.has(url.origin)) socket.connectToServer();
    else { offScopeRequests.push({ kind: "websocket", origin: url.origin, path: url.pathname }); socket.close({ code: 1008, reason: "QA scope boundary" }); }
  });
  page.on("pageerror", error => pageErrors.push(error.name));
  page.on("console", message => {
    if (message.type() !== "error") return;
    const text = message.text();
    const code = text.includes("Failed to load resource") ? "resource_error"
      : text.includes("same key") ? "duplicate_key"
      : /hydrat/i.test(text) ? "hydration_error"
      : text.includes("uncontrolled") ? "uncontrolled_input"
      : text.includes("cannot be a descendant") ? "invalid_html"
      : text.includes("RSC payload") ? "rsc_error" : "console.error";
    let path = "";
    try { path = new URL(message.location().url).pathname; } catch { /* no raw console text */ }
    consoleErrors.push(`${code}:${path}`);
  });
  page.on("response", response => { if (response.status() >= 500) serverErrors.push({ status: response.status(), path: new URL(response.url()).pathname }); });
  let journey: Record<string, unknown> = {};
  try {
  const name = `QA AI社員 ${Date.now()}`;
  await page.goto("/login");
  await page.getByLabel("ユーザー名", { exact: true }).fill(username!);
  await page.getByLabel("パスワード", { exact: true }).fill(password!);
  await page.getByRole("button", { name: "ログイン", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
  await page.goto("/operations?tab=agents");
  await expect(page.getByRole("button", { name: "＋ AI社員を追加" })).toBeVisible();
  await capture(page, "01-employee-list");
  await page.getByRole("button", { name: "＋ AI社員を追加" }).click();
  await page.getByLabel("社員名", { exact: true }).fill(name);
  await page.getByLabel("役職", { exact: true }).fill("備品・ウォーターサーバー担当");
  await page.getByLabel("責務の概要", { exact: true }).fill("交換用水の在庫を確認して補充する");
  await page.getByRole("button", { name: "下書きを保存して職務設定へ" }).click();
  await expect(page).toHaveURL(/agent=.+panel=role/, { timeout: 15_000 });
  const agentId = new URL(page.url()).searchParams.get("agent")!;
  const projectOptionsResponse = await page.request.get("/api/projects"); expect(projectOptionsResponse.ok()).toBeTruthy();
  const projectOptions = await projectOptionsResponse.json() as { projects: { id: string; name: string }[] };
  const qaProject = projectOptions.projects.find(p => p.id === projectId); expect(qaProject, "QA Project must be visible to the authenticated user").toBeTruthy();
  const catalogResponse = await page.request.get("/api/python-proxy/agents/catalog");
  expect(catalogResponse.ok()).toBeTruthy();
  const catalog = await catalogResponse.json() as { teams: { team_id: string; name: string; execution_profiles: { profile_id: string; name: string }[] }[] };
  const team = catalog.teams.find(t => t.team_id === "employee"); expect(team).toBeTruthy();
  await choose(page, "Agent Team", team!.name);
  const manualProfile = team!.execution_profiles.find(p => p.profile_id === "manual"); expect(manualProfile).toBeTruthy();
  await choose(page, "Execution Profile", manualProfile!.name);
  const operator = page.getByRole("group", { name: "許可する Subagents" }).getByRole("checkbox").first();
  await operator.check();
  await page.getByRole("checkbox", { name: /external_action_propose/ }).check();
  await page.getByLabel("ミッション", { exact: true }).fill("社員の水切れ報告に対応する");
  await page.getByLabel("業務指示", { exact: true }).fill("固定の商品と配送先を使い、要確認の注文を再実行しない。");
  await page.getByRole("checkbox", { name: "変更内容を確認しました", exact: true }).check();
  await page.getByRole("button", { name: "新しい職務 revision v1 を保存" }).click();
  await expect(page.getByRole("heading", { name: "職務・モデル — 現在 v1" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "職務・モデル — 現在 v1" })).toBeVisible();
  await page.getByRole("button", { name: "組織・所属", exact: true }).click();
  await choose(page, "主所属 Space", "AI Employee QA Space (test)");
  await choose(page, "自律レベル", "範囲内自律");
  await page.getByRole("button", { name: "組織プロフィールを保存" }).click();
  await choose(page, "割り当て先", "AI Employee QA Space (test)");
  await page.getByRole("button", { name: "所属・権限を追加" }).click();
  await expect(page.getByRole("button", { name: "解除", exact: true })).toBeVisible();
  await choose(page, "所属・権限の種類", "Project 権限");
  await choose(page, "割り当て先", qaProject!.name); await choose(page, "担当ロール", "メンバー");
  await page.getByRole("checkbox", { name: "書込を許可", exact: true }).check();
  await page.getByRole("button", { name: "所属・権限を追加" }).click();
  await expect(page.getByRole("button", { name: "解除", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "自動化", exact: true }).click();
  await page.getByRole("button", { name: "ポリシーを追加" }).click();
  await page.getByLabel("ポリシー名", { exact: true }).fill("QA 交換水・範囲内自動");
  await choose(page, "登録済みアクション", /deterministic TEST fixture/);
  await choose(page, "登録済み接続先", "Deterministic procurement TEST fixture — no real orders");
  await choose(page, "承認方式", "範囲内自動実行");
  await page.getByLabel("固定商品参照キー", { exact: true }).fill("water-ref");
  await page.getByLabel("固定配送先参照キー", { exact: true }).fill("tokyo-office");
  await page.getByLabel("注文総額上限（最小通貨単位・JPYは円）", { exact: true }).fill("10000");
  await page.getByRole("checkbox", { name: "上記の許可範囲を確認しました", exact: true }).check();
  await capture(page, "02-structured-policy");
  await page.getByRole("button", { name: "ポリシー revision を保存" }).click();
  await expect(page.getByRole("button", { name: "ポリシーを有効化", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "ポリシーを有効化", exact: true }).click();
  await expect(page.getByRole("button", { name: "ポリシーを有効化", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "自動化ルールを追加" }).click();
  await page.getByLabel("ルール名", { exact: true }).fill("QA 水切れの意味判定");
  await choose(page, "対象のチャット範囲", qaProject!.name);
  await choose(page, "反応する条件", "意味で判定");
  await page.getByLabel("どんな報告に反応するか", { exact: true }).fill("社員がウォーターサーバー用の交換水がない、空になった、在庫切れと報告したとき");
  await choose(page, "追加する操作ポリシー", /QA 交換水・範囲内自動/);
  await page.getByRole("button", { name: "操作を紐付ける" }).click();
  await capture(page, "03-structured-rule");
  await page.getByRole("button", { name: "ルール revision を保存" }).click();
  await expect(page.getByLabel("テストする報告", { exact: true })).toBeVisible();
  await page.getByLabel("テストする報告", { exact: true }).fill("ウォーターサーバーの水が空です");
  await page.getByRole("button", { name: "条件をテスト", exact: true }).click();
  await expect(page.getByText(/条件に一致しました/)).toBeVisible();
  await page.getByRole("button", { name: "概要・有効化", exact: true }).click();
  await page.getByRole("button", { name: "社員を有効化", exact: true }).click();
  await expect(page.getByRole("button", { name: "社員を一時停止", exact: true })).toBeEnabled();
  await capture(page, "04-employee-overview");
  await page.getByRole("button", { name: "自動化", exact: true }).click();
  await page.getByRole("button", { name: "ルールを有効化", exact: true }).click();
  await expect(page.getByRole("button", { name: "ルールを有効化", exact: true })).toBeDisabled();
  // Post through the same authenticated canonical message persistence route
  // used by chat; this triggers durable automation without invoking chat LLMs.
  expect(conversationId, "Harness must seed an ACL-scoped QA conversation").toBeTruthy();
  const rulesResponse = await page.request.get(`/api/python-proxy/agent-automation/rules?agent_id=${agentId}`);
  expect(rulesResponse.ok()).toBeTruthy();
  const rules = await rulesResponse.json() as { rules: { id: string; current_revision: { id: string } }[] };
  const firstMessageResponse = await page.request.post(`/api/python-proxy/conversations/${conversationId}/messages`, { data: { role: "user", content: "ウォーターサーバーの水が空です", client_message_id: crypto.randomUUID() } });
  expect(firstMessageResponse.ok(), "Canonical human message persistence").toBeTruthy();
  const firstMessage = await firstMessageResponse.json() as { message: { id: string } };
  type Action = { id: string; status: string; origin_agent_id?: string; attempts?: { id: string; status: string }[]; receipt?: { id: string; attempt_id?: string } | null };
  async function actionList(): Promise<Action[]> {
    const result = await page.request.get(`/api/python-proxy/operations/actions?project_id=${projectId}`);
    expect(result.ok()).toBeTruthy();
    const body = await result.json() as Action[];
    return body.filter(a => a.origin_agent_id === agentId);
  }
  let action: Action | undefined;
  await expect.poll(async () => { action = (await actionList()).find(a => a.status === expectedStatus); return action?.status; }, { timeout: 90_000, intervals: [1000, 2000] }).toBe(expectedStatus);
  const actionResponse = await page.request.get(`/api/python-proxy/operations/actions/${action!.id}`);
  expect(actionResponse.ok()).toBeTruthy();
  const actionPayload = await actionResponse.json() as Action | { action: Action };
  const detail = "action" in actionPayload ? actionPayload.action : actionPayload;
  if (expectedStatus === "succeeded") expect(detail.receipt?.id, "Strong provider Receipt must exist").toBeTruthy();
  else { expect(expectedStatus).toBe("uncertain"); expect(detail.receipt, "Uncertain submission must not fabricate a Receipt").toBeNull(); }
  expect(detail.attempts?.some(a => a.status === expectedStatus), "Trusted execution Attempt must retain the provider outcome").toBeTruthy();
  await page.getByRole("button", { name: "活動・要確認", exact: true }).click();
  await page.getByRole("button", { name: "活動状態を更新", exact: true }).click();
  if (expectedStatus === "succeeded") await expect(page.getByText("action_receipt", { exact: true }).first()).toBeVisible();
  else await expect(page.getByText("uncertain", { exact: true }).first()).toBeVisible();
  const secondMessageResponse = await page.request.post(`/api/python-proxy/conversations/${conversationId}/messages`, { data: { role: "user", content: "水切れです", client_message_id: crypto.randomUUID() } });
  expect(secondMessageResponse.ok()).toBeTruthy();
  const secondMessage = await secondMessageResponse.json() as { message: { id: string } };
  const snapshotPath = `/api/python-proxy/operations/command-center?agent_id=${agentId}&scope=agent`;
  await expect.poll(async () => {
    const snapshotResponse = await page.request.get(snapshotPath); expect(snapshotResponse.ok()).toBeTruthy();
    const snapshot = await snapshotResponse.json() as { agents: { id: string; employee_observability?: { last_automation_result?: { reason_code?: string; suppressed?: boolean } } }[] };
    const result = snapshot.agents.find(a => a.id === agentId)?.employee_observability?.last_automation_result;
    return result?.reason_code === "action_dedupe_suppressed" || result?.suppressed === true;
  }, { timeout: 90_000, intervals: [1000, 2000] }).toBe(true);
  expect(await actionList()).toHaveLength(1);
  await page.getByRole("button", { name: "活動状態を更新", exact: true }).click();
  await capture(page, "05-activity-receipt-suppression");
  let providerSubmissionCount: number | null = null;
  if (process.env.AI_EMPLOYEE_QA_PROVIDER_EVIDENCE_PATH) {
    const providerEvidence = JSON.parse(readFileSync(process.env.AI_EMPLOYEE_QA_PROVIDER_EVIDENCE_PATH, "utf8")) as { submission_count: number };
    providerSubmissionCount = providerEvidence.submission_count; expect(providerSubmissionCount).toBe(1);
  }
  journey = { agent_id: agentId, rule_id: rules.rules[0].id, rule_revision_id: rules.rules[0].current_revision.id, action_id: detail.id, action_status: detail.status, receipt_id: detail.receipt?.id ?? null, attempt_ids: detail.attempts?.map(a => a.id), session_id: conversationId, message_ids: [firstMessage.message.id, secondMessage.message.id], provider_submission_count: providerSubmissionCount, duplicate_suppressed: true };
  if (process.env.AI_EMPLOYEE_QA_RESULT_PATH) writeFileSync(process.env.AI_EMPLOYEE_QA_RESULT_PATH, JSON.stringify(journey, null, 2));
  await page.getByRole("button", { name: "職務・モデル", exact: true }).click();
  await page.getByLabel("ミッション", { exact: true }).fill("交換水の在庫管理と重複防止を徹底する");
  await page.getByRole("checkbox", { name: "変更内容を確認しました", exact: true }).check();
  await page.getByRole("button", { name: "新しい職務 revision v2 を保存" }).click();
  await expect(page.getByRole("heading", { name: "職務・モデル — 現在 v2" })).toBeVisible();
  await page.getByRole("button", { name: "活動・要確認", exact: true }).click();
  await expect(page.getByText(/要確認の操作は結果を照合するまで再実行できません/)).toBeVisible();
  await page.reload(); await expect(page).toHaveURL(new RegExp(`agent=${agentId}&panel=activity`));
  // A URL can be restored before App Router hydrates its history listener.
  // Wait for the client-only panel's initial data load, not merely the URL.
  await expect(page.getByRole("button", { name: "活動状態を更新", exact: true })).toBeEnabled();
  await page.goBack(); await expect(page).toHaveURL(/panel=role/);
  await expect(page.getByRole("heading", { name: "職務・モデル — 現在 v2" })).toBeVisible();
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(serverErrors).toEqual([]);
  expect(offScopeRequests).toEqual([]);
  const revisionsResponse = await page.request.get(`/api/python-proxy/agents/${agentId}/revisions`);
  expect(revisionsResponse.ok()).toBeTruthy();
  const revisions = await revisionsResponse.json() as { revisions: { version: number; mission: string }[] };
  expect(revisions.revisions.find(r => r.version === 1)?.mission).toBe("社員の水切れ報告に対応する");
  expect(revisions.revisions.find(r => r.version === 2)?.mission).toBe("交換水の在庫管理と重複防止を徹底する");
  // Results contain only QA-created IDs and counts; the independent harness
  // additionally verifies the durable rows and provider submission counter.
  } finally {
    if (process.env.AI_EMPLOYEE_QA_RESULT_PATH) writeFileSync(process.env.AI_EMPLOYEE_QA_RESULT_PATH, JSON.stringify({ ...journey, page_errors: pageErrors, console_errors: consoleErrors, server_errors: serverErrors, off_scope_requests: offScopeRequests }, null, 2));
  }
});
