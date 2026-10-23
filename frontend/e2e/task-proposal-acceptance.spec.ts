import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

/**
 * 承認/拒否/確認の受け入れ試験。
 *
 * ブラウザから出る API はすべて専用 fixture backend（このファイル内の
 * route handler）で応答し、本番 API・本番 DB・実エージェントには届かない。
 * 「実行」は fixture の executionCount で数え、承認が1回だけ実行を開始し、
 * 拒否と確認が一度も実行しないことを検証する。
 *
 * APPROVAL_EVIDENCE_DIR を指定した場合だけ、スクリーンショットと操作ログを
 * そのディレクトリへ保存する（通常の CI 実行では何も書かない）。
 */

const BASE_PATH = (
  process.env.PLAYWRIGHT_BASE_PATH?.trim() ||
  process.env.NEXT_PUBLIC_BASE_PATH?.trim() ||
  ""
).replace(/^\/+|\/+$/g, "");
const EVIDENCE_DIR = process.env.APPROVAL_EVIDENCE_DIR?.trim() || "";

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  narrow: { width: 390, height: 844 },
} as const;

const project = {
  id: "acceptance-project",
  name: "Acceptance project",
  slug: "acceptance-project",
  space_id: "acceptance-space",
  is_member: true,
  is_participating: true,
  can_write: true,
  can_manage_settings: true,
  is_completed: false,
};

type ProposalStatus = "pending" | "approved" | "rejected" | "cancelled" | "completed";

type FixtureTask = ReturnType<typeof makeTask>;
type FixtureProposal = ReturnType<typeof makeProposal>;

function makeTask(
  id: string,
  title: string,
  overrides: Record<string, unknown> = {},
) {
  const now = Date.now();
  return {
    id,
    project_id: project.id,
    project_name: project.name,
    title,
    description: "",
    status: "open",
    priority: "medium",
    start_at: new Date(now - 5 * 60 * 1000).toISOString(),
    end_at: new Date(now + 60 * 60 * 1000).toISOString(),
    all_day: false,
    reminder_offsets: [],
    notifications_enabled: true,
    source: "manual",
    created_by: "e2e-fixture",
    completed_at: null,
    created_at: "2026-10-21T00:00:00.000Z",
    updated_at: "2026-10-21T00:00:00.000Z",
    metadata: {},
    assignees: [],
    tags: [],
    active_time_entry: null as Record<string, unknown> | null,
    estimated_hours: null,
    sort_order: 0,
    total_time_seconds: 0,
    parent_task_id: null,
    subtasks: [],
    comments: [],
    activities: [],
    has_recurrence: false,
    ...overrides,
  };
}

function makeProposal(task: FixtureTask, suffix: string, title: string) {
  return {
    id: `acceptance-proposal-${suffix}`,
    task_id: task.id,
    session_id: `acceptance-session-${suffix}`,
    agent_run_id: `acceptance-run-${suffix}`,
    revision: 3,
    plan_id: `acceptance-plan-${suffix}`,
    plan_digest: `plan-digest-${suffix}`,
    action_digest: `action-digest-${suffix}`,
    material_action_preview_digest: `preview-digest-${suffix}`,
    title,
    summary: "対象タスクのリンク済み会話を確認してから整理します。",
    plan_text: "objective: " + title + "\napproach: 会話を確認して整理する",
    status: "pending" as const,
  };
}

function liveTimer(task: FixtureTask) {
  return {
    id: `timer-${task.id}`,
    task_id: task.id,
    user_id: "e2e-fixture",
    started_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
    ended_at: null,
    deleted_at: null,
    source: "manual",
  };
}

function apiPath(url: URL): string {
  const prefix = BASE_PATH ? `/${BASE_PATH}` : "";
  return prefix && url.pathname.startsWith(`${prefix}/`)
    ? url.pathname.slice(prefix.length)
    : url.pathname;
}

type Fixture = {
  records: Map<string, { proposal: FixtureProposal; status: ProposalStatus }>;
  executionCount: number;
  decisionRequests: Array<{ proposalId: string; action: unknown; outcome: string }>;
  dispatchRequests: string[];
  dropNextDecisionResponse: boolean;
  failNextDecisionBeforeApply: boolean;
  failProposalList: boolean;
  log: (event: string, detail?: Record<string, unknown>) => void;
};

function evidenceLog(testName: string) {
  return (event: string, detail: Record<string, unknown> = {}) => {
    if (!EVIDENCE_DIR) return;
    mkdirSync(EVIDENCE_DIR, { recursive: true });
    appendFileSync(
      join(EVIDENCE_DIR, "op-log.jsonl"),
      JSON.stringify({ at: new Date().toISOString(), test: testName, event, ...detail }) + "\n",
      "utf8",
    );
  };
}

async function shot(page: Page, name: string, fixture: Fixture) {
  if (!EVIDENCE_DIR) return;
  mkdirSync(EVIDENCE_DIR, { recursive: true });
  const path = join(EVIDENCE_DIR, `${name}.png`);
  await page.screenshot({ path, fullPage: false });
  fixture.log("screenshot", { path });
}

async function installFixtureBackend(
  page: Page,
  tasks: FixtureTask[],
  proposals: Array<{ proposal: FixtureProposal; status: ProposalStatus }>,
  testName: string,
): Promise<Fixture> {
  const fixture: Fixture = {
    records: new Map(proposals.map((item) => [item.proposal.id, { ...item }])),
    executionCount: 0,
    decisionRequests: [],
    dispatchRequests: [],
    dropNextDecisionResponse: false,
    failNextDecisionBeforeApply: false,
    failProposalList: false,
    log: evidenceLog(testName),
  };
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  const session = (id: string) => ({
    id,
    user_id: "e2e-fixture",
    title: "提案の会話",
    character_name: "aoi",
    message_count: 0,
    is_active: true,
    is_group_chat: false,
    group_character_names: [],
  });

  await mockAuthenticatedApis(page);
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = apiPath(url);
    const method = request.method();

    if (pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: "e2e-fixture", username: "__playwright_e2e__", role: "admin", user_settings: {} },
        },
      });
      return;
    }
    if (pathname === "/api/spaces") {
      await route.fulfill({
        json: {
          spaces: [{ id: project.space_id, name: "Acceptance space", slug: "acceptance-space", is_member: true, can_write: true }],
          total: 1,
        },
      });
      return;
    }
    if (pathname === "/api/projects") {
      await route.fulfill({ json: { projects: [project], total: 1 } });
      return;
    }
    if (pathname === "/api/tasks" && method === "GET") {
      const todaySummary = url.searchParams.get("today_summary") === "true";
      // Mirrors todayTaskListCondition: closed tasks never enter Today.
      const visible = todaySummary
        ? tasks.filter((task) => !["done", "closed"].includes(task.status))
        : tasks;
      await route.fulfill({ json: visible });
      return;
    }
    const taskMatch = pathname.match(/^\/api\/tasks\/([^/]+)$/);
    if (taskMatch && method === "GET" && taskById.has(taskMatch[1])) {
      await route.fulfill({ json: taskById.get(taskMatch[1]) });
      return;
    }
    if (/^\/api\/tasks\/[^/]+\/(attachments|references)$/.test(pathname) && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (/^\/api\/tasks\/[^/]+\/recurrence$/.test(pathname) && method === "GET") {
      await route.fulfill({ json: null });
      return;
    }
    if (pathname === "/api/task-occurrences" && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/projects/${project.id}/tags` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/projects/${project.id}/assignee-candidates` && method === "GET") {
      await route.fulfill({ json: { members: [], total: 0 } });
      return;
    }
    if (pathname === "/api/users/me/settings" && method === "GET") {
      await route.fulfill({ json: { settings: {} } });
      return;
    }
    if (pathname === "/api/python-proxy/task-proposals" && method === "GET") {
      if (fixture.failProposalList) {
        fixture.log("proposal-list", { outcome: "500" });
        await route.fulfill({ status: 500, json: { detail: "fixture failure" } });
        return;
      }
      const ids = new Set((url.searchParams.get("task_ids") ?? "").split(",").filter(Boolean));
      // Mirrors the server: only live pending proposals are listed.
      const pending = [...fixture.records.values()]
        .filter((item) => item.status === "pending" && ids.has(item.proposal.task_id))
        .map((item) => item.proposal);
      fixture.log("proposal-list", { ids: [...ids], pending: pending.map((item) => item.id) });
      await route.fulfill({ json: { success: true, proposals: pending } });
      return;
    }
    const decisionMatch = pathname.match(
      /^\/api\/python-proxy\/tasks\/([^/]+)\/agent-proposals\/([^/]+)\/decision$/,
    );
    if (decisionMatch && method === "POST") {
      const body = (request.postDataJSON() ?? {}) as Record<string, unknown>;
      if (fixture.failNextDecisionBeforeApply) {
        // The request never reached the server: nothing was applied.
        fixture.failNextDecisionBeforeApply = false;
        fixture.decisionRequests.push({ proposalId: decisionMatch[2], action: body.action, outcome: "network-before-apply" });
        fixture.log("decision", { proposal: decisionMatch[2], action: body.action, outcome: "network failure before apply" });
        await route.abort("failed");
        return;
      }
      const record = fixture.records.get(decodeURIComponent(decisionMatch[2]));
      if (!record || record.status !== "pending" || record.proposal.task_id !== decodeURIComponent(decisionMatch[1])) {
        fixture.decisionRequests.push({ proposalId: decisionMatch[2], action: body.action, outcome: "409" });
        fixture.log("decision", { proposal: decisionMatch[2], action: body.action, outcome: "409 proposal_stale" });
        await route.fulfill({
          status: 409,
          json: { detail: { code: "proposal_stale", message: "The live proposal has changed or was already consumed." } },
        });
        return;
      }
      // Server-owned single winner: the state transition and the execution
      // start happen exactly once, before any response is written.
      if (body.action === "approve") {
        record.status = "approved";
        fixture.executionCount += 1;
      } else {
        record.status = "rejected";
      }
      fixture.decisionRequests.push({ proposalId: record.proposal.id, action: body.action, outcome: "200" });
      fixture.log("decision", {
        proposal: record.proposal.id,
        action: body.action,
        outcome: fixture.dropNextDecisionResponse ? "applied, response dropped" : "200",
        executionCount: fixture.executionCount,
      });
      if (fixture.dropNextDecisionResponse) {
        fixture.dropNextDecisionResponse = false;
        await route.abort("failed");
        return;
      }
      await route.fulfill({
        json: {
          success: true,
          status: "resolved",
          proposal_id: record.proposal.id,
          action: body.action,
          resolution_action: body.action === "approve" ? "approve" : "cancel",
        },
      });
      return;
    }
    const conversationMatch = pathname.match(/^\/api\/conversations\/([^/]+)\/(messages|read|resume)$/);
    if (conversationMatch) {
      const id = decodeURIComponent(conversationMatch[1]);
      if (conversationMatch[2] === "messages" && method === "GET") {
        await route.fulfill({ json: { messages: [] } });
        return;
      }
      if (conversationMatch[2] === "read") {
        await route.fulfill({ json: { success: true, session_id: id, last_read_at: null } });
        return;
      }
      if (conversationMatch[2] === "resume") {
        await route.fulfill({ json: { success: true, session: session(id), messages: [] } });
        return;
      }
    }
    if (pathname === "/api/conversations" && method === "GET") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }
    if (/\/generation\/status$/.test(pathname) && method === "GET") {
      await route.fulfill({ json: { running: false, status: "idle", message: null } });
      return;
    }
    if (pathname.endsWith("/dispatch") && method === "POST") {
      fixture.dispatchRequests.push(pathname);
      fixture.log("dispatch", { path: pathname });
      await route.fulfill({ json: { success: true, queued: false } });
      return;
    }
    await route.fallback();
  });
  return fixture;
}

function card(scope: Page | Locator, proposalId: string): Locator {
  return scope.locator(`[data-testid="task-proposal-card-${proposalId}"]:visible`);
}

async function expectSameProposal(scope: Locator, proposal: FixtureProposal) {
  const target = card(scope, proposal.id);
  await expect(target).toBeVisible();
  await expect(target).toHaveAttribute("data-proposal-revision", String(proposal.revision));
  await expect(target.getByRole("heading", { name: proposal.title, exact: true })).toBeVisible();
  await expect(target.getByRole("button")).toHaveText(["承認", "拒否", "確認"]);
}

async function openToday(page: Page): Promise<Locator> {
  const trigger = page.getByRole("button", { name: "Todayを開く", exact: true }).first();
  if (await trigger.isVisible().catch(() => false)) {
    await trigger.click();
  } else {
    const userMenu = page.getByRole("button", {
      name: "ユーザーメニューを開く",
      exact: true,
    });
    if (await userMenu.isVisible().catch(() => false)) {
      await userMenu.click();
      await page.getByRole("menuitem", { name: "Todayを開く", exact: true }).click();
    } else {
      // Narrow layouts without a visible Today button use the app shortcut.
      await page.keyboard.press("Control+Shift+H");
    }
  }
  const dialog = page.locator('[role="dialog"]:visible').filter({ hasText: "Today" }).first();
  await expect(dialog).toBeVisible();
  return dialog;
}

function todayProposalSection(dialog: Locator): Locator {
  return dialog
    .locator("section")
    .filter({ has: dialog.page().getByRole("heading", { name: "承認待ちの提案" }) })
    .first();
}

async function openDetail(page: Page, task: FixtureTask): Promise<Locator> {
  await page.goto(`/tasks?detail=${encodeURIComponent(task.id)}`);
  const dialog = page.locator('[role="dialog"]:visible').filter({ hasText: task.title }).first();
  await expect(dialog).toBeVisible();
  return dialog;
}

async function visibleTaskListItem(page: Page, task: FixtureTask): Promise<Locator> {
  const isNarrow = (page.viewportSize()?.width ?? 1280) < 768;
  const item = page.getByTestId(
    `${isNarrow ? "mobile-task-card" : "task-row"}-${task.id}`,
  );
  await expect(item).toBeVisible();
  return item;
}

async function expectTaskListApproval(page: Page, task: FixtureTask) {
  const item = await visibleTaskListItem(page, task);
  await expect(item.getByText(/承認待ち/).first()).toBeVisible();
  await expect(item.getByTestId(/^task-proposal-card-/)).toHaveCount(0);
  await expect(page.getByTestId("task-proposal-list")).toHaveCount(0);
  const isNarrow = (page.viewportSize()?.width ?? 1280) < 768;
  if (isNarrow) {
    await expect(item).toHaveClass(/border-amber-400\/70/);
  } else {
    await expect(item.locator(":scope > td").first()).toHaveClass(/border-l-amber-500/);
  }
}

async function expectTaskListApprovalCleared(page: Page, task: FixtureTask) {
  const item = await visibleTaskListItem(page, task);
  await expect(item.getByText(/承認待ち/)).toHaveCount(0);
}

async function openTaskFromList(page: Page, task: FixtureTask): Promise<Locator> {
  const item = await visibleTaskListItem(page, task);
  await item.getByText(task.title, { exact: true }).first().click();
  const dialog = page.locator('[role="dialog"]:visible').filter({ hasText: task.title }).first();
  await expect(dialog).toBeVisible();
  return dialog;
}

async function boot(page: Page, viewport: keyof typeof VIEWPORTS = "desktop") {
  await page.setViewportSize(VIEWPORTS[viewport]);
  await page.addInitScript(() => localStorage.clear());
  await addAuthCookie(page);
}

for (const viewport of ["desktop", "narrow"] as const) {
  test(`[${viewport}] Today・タスク一覧・詳細に同じ提案と状態を表示し、実行中0件を計測中と偽らない`, async ({ page }) => {
    const proposalTask = makeTask("acc-task-proposal", "週次レポートを整理する");
    // Canonical status says in_progress but no live timer exists (stale status).
    const staleStatusTask = makeTask("acc-task-stale", "状態だけ進行中のタスク", { status: "in_progress" });
    const doneTask = makeTask("acc-task-done", "完了済みタスク", { status: "done" });
    const proposal = makeProposal(proposalTask, "same", "リンク済み会話を要約してタスクに添付する");
    const doneProposal = makeProposal(doneTask, "done", "完了済みタスクの古い提案");
    const fixture = await installFixtureBackend(
      page,
      [proposalTask, staleStatusTask, doneTask],
      [
        { proposal, status: "pending" },
        { proposal: doneProposal, status: "completed" },
      ],
      `same-proposal-${viewport}`,
    );
    await boot(page, viewport);

    await page.goto("/tasks");
    const today = await openToday(page);
    const section = todayProposalSection(today);
    await expectSameProposal(section, proposal);
    await expect(section.getByText("1件", { exact: true })).toBeVisible();
    // The summary metric shows the fetched count immediately (no 0 flash).
    expect(
      await today
        .locator('[data-today-metric="承認待ち"] span[aria-label] > span[aria-hidden="true"]')
        .textContent(),
    ).toBe("1");
    await expect(today.getByText(doneProposal.title)).toHaveCount(0);
    await expect(today.getByText(/タイマー計測中/)).toHaveCount(0);
    await expect(today.getByText(/件が進行中/)).toHaveCount(0);
    await shot(page, `today-${viewport}`, fixture);
    await page.keyboard.press("Escape");
    await expect(today).toBeHidden();

    await page.goto("/tasks");
    await expectTaskListApproval(page, proposalTask);
    await shot(page, `task-list-${viewport}`, fixture);

    const detail = await openTaskFromList(page, proposalTask);
    await expectSameProposal(detail, proposal);
    const details = detail.getByTestId(`task-proposal-details-${proposal.id}`);
    await details.locator("summary").click();
    await expect(details).toContainText("approach: 会話を確認して整理する");
    await shot(page, `detail-modal-${viewport}`, fixture);

    expect(fixture.decisionRequests).toHaveLength(0);
    expect(fixture.executionCount).toBe(0);
    expect(fixture.dispatchRequests).toHaveLength(0);
  });
}

for (const viewport of ["desktop", "narrow"] as const) {
  test(`[${viewport}] closed子タスクの承認待ちは親から選択して詳細を開ける`, async ({ page }) => {
    const parent = makeTask(`acc-parent-${viewport}`, `親タスク ${viewport}`);
    const child = makeTask(`acc-child-${viewport}`, `子タスク ${viewport}`, {
      parent_task_id: parent.id,
      status: "closed",
    });
    const proposal = makeProposal(child, `child-${viewport}`, "子タスクの提案内容を確認する");
    const fixture = await installFixtureBackend(
      page,
      [parent, child],
      [{ proposal, status: "pending" }],
      `child-proposal-${viewport}`,
    );
    await boot(page, viewport);
    await page.goto("/tasks");
    const taskWriteRequests: string[] = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (
        ["POST", "PATCH", "PUT", "DELETE"].includes(request.method()) &&
        pathname.startsWith("/api/tasks/")
      ) {
        taskWriteRequests.push(`${request.method()} ${pathname}`);
      }
    });

    if (viewport === "narrow") {
      const parentCard = page.getByTestId(`mobile-task-card-${parent.id}`);
      await expect(parentCard.getByText("子タスク承認待ち")).toBeVisible();
      const trigger = parentCard.getByRole("button", {
        name: "承認待ちの子タスクを開く 1件",
      });
      await trigger.click();
    } else {
      const parentRow = page.getByTestId(`task-row-${parent.id}`);
      await expect(parentRow.getByText("子タスク承認待ち")).toBeVisible();
      await expect(page.getByTestId(`task-row-${child.id}`)).toHaveCount(0);
      const trigger = parentRow.getByRole("button", {
        name: "承認待ちの子タスクを開く 1件",
      });
      await trigger.focus();
      await page.keyboard.press("Enter");
    }

    await expect(page.getByRole("menu")).toBeVisible();
    await expect(page.locator('[role="dialog"]:visible')).toHaveCount(0);
    await shot(page, `closed-child-menu-${viewport}`, fixture);
    const childMenuItem = page.getByRole("menuitem", {
      name: child.title,
      exact: true,
    });
    await expect(childMenuItem).toBeVisible();
    if (viewport === "desktop") {
      await expect(childMenuItem).toBeFocused();
      await page.keyboard.press("Enter");
    } else {
      await childMenuItem.click();
    }

    const detail = page.locator('[role="dialog"]:visible').filter({ hasText: child.title }).first();
    await expect(detail).toBeVisible();
    await expect(detail.getByRole("heading", { name: child.title, exact: true })).toBeVisible();
    await expectSameProposal(detail, proposal);
    await shot(page, `closed-child-detail-${viewport}`, fixture);
    expect(taskWriteRequests).toHaveLength(0);
    expect(fixture.decisionRequests).toHaveLength(0);
    expect(fixture.dispatchRequests).toHaveLength(0);
    expect(child.status).toBe("closed");
  });
}

test("タスク一覧の提案取得エラーは実エラーと更新導線を保つ", async ({ page }) => {
  const task = makeTask("acc-task-fetch-error", "提案取得エラーを確認するタスク");
  const proposal = makeProposal(task, "fetch-error", "取得復旧後に一覧で強調する");
  const fixture = await installFixtureBackend(page, [task], [{ proposal, status: "pending" }], "proposal-fetch-error");
  fixture.failProposalList = true;
  await boot(page);
  await page.goto("/tasks");

  await expect(page.getByRole("alert").filter({ hasText: "fixture failure" })).toBeVisible();
  fixture.failProposalList = false;
  await page.getByRole("button", { name: "更新", exact: true }).click();
  await expectTaskListApproval(page, task);
  expect(fixture.decisionRequests).toHaveLength(0);
  expect(fixture.dispatchRequests).toHaveLength(0);
});

test("実タイマー1件だけをTodayで計測中として数える", async ({ page }) => {
  const running = makeTask("acc-task-running", "タイマー計測中のタスク", { status: "in_progress" });
  running.active_time_entry = liveTimer(running);
  const stale = makeTask("acc-task-stale-2", "状態だけ進行中", { status: "in_progress" });
  const fixture = await installFixtureBackend(page, [running, stale], [], "one-running");
  await boot(page);
  await page.goto("/tasks");
  const today = await openToday(page);
  await expect(today.getByText("1件をタイマー計測中です。")).toBeVisible();
  await expect(todayProposalSection(today).getByText("承認待ちの提案はありません。")).toBeVisible();
  await shot(page, "today-one-running-desktop", fixture);
  expect(fixture.executionCount).toBe(0);
});

test("Todayの承認はダブルクリックでも1回だけ実行し、全画面と再読込で再出現しない", async ({ page }) => {
  const task = makeTask("acc-task-approve", "承認対象タスク");
  const proposal = makeProposal(task, "approve", "会議メモから次のアクションを作成する");
  const fixture = await installFixtureBackend(page, [task], [{ proposal, status: "pending" }], "approve-double-click");
  await boot(page);
  await page.goto("/tasks");
  const today = await openToday(page);
  const section = todayProposalSection(today);
  await expectSameProposal(section, proposal);
  await card(section, proposal.id).getByRole("button", { name: "承認", exact: true }).dblclick();
  await expect(card(page, proposal.id)).toHaveCount(0);
  await expect(page.getByText("承認を受け付けました。実行を開始します。")).toBeVisible();
  await shot(page, "today-after-approve-desktop", fixture);
  expect(fixture.decisionRequests.map((item) => item.outcome)).toEqual(["200"]);
  expect(fixture.executionCount).toBe(1);

  await page.keyboard.press("Escape");
  await expectTaskListApprovalCleared(page, task);
  await page.reload();
  await expect(page.getByTestId(`task-row-${task.id}`)).toBeVisible();
  await expect(card(page, proposal.id)).toHaveCount(0);
  const detail = await openDetail(page, task);
  await expect(card(detail, proposal.id)).toHaveCount(0);
  expect(fixture.executionCount).toBe(1);
  expect(fixture.decisionRequests).toHaveLength(1);
});

test("タスク詳細の拒否は実行せず、再読込後に一覧の強調も解除する", async ({ page }) => {
  const task = makeTask("acc-task-reject", "拒否対象タスク");
  const proposal = makeProposal(task, "reject", "古い下書きをすべて削除する");
  const fixture = await installFixtureBackend(page, [task], [{ proposal, status: "pending" }], "reject");
  await boot(page);
  await page.goto("/tasks");
  await expectTaskListApproval(page, task);
  const detail = await openTaskFromList(page, task);
  await card(detail, proposal.id).getByRole("button", { name: "拒否", exact: true }).click();
  await expect(card(page, proposal.id)).toHaveCount(0);
  await expect(page.getByText("拒否を受け付けました。")).toBeVisible();
  expect(fixture.records.get(proposal.id)?.status).toBe("rejected");
  await page.keyboard.press("Escape");
  await expect(detail).toBeHidden();
  await expectTaskListApprovalCleared(page, task);
  await page.reload();
  await expect(page.getByTestId(`task-row-${task.id}`)).toBeVisible();
  await expectTaskListApprovalCleared(page, task);
  await expect(card(page, proposal.id)).toHaveCount(0);
  const today = await openToday(page);
  await expect(card(today, proposal.id)).toHaveCount(0);
  expect(fixture.executionCount).toBe(0);
  expect(fixture.decisionRequests).toHaveLength(1);
});

for (const origin of ["today", "list", "detail"] as const) {
  test(`[${origin}] 確認は提案を識別できる既存チャットへ遷移し、決定も実行もしない`, async ({ page }) => {
    const task = makeTask(`acc-task-confirm-${origin}`, `確認対象タスク ${origin}`);
    const proposal = makeProposal(task, `confirm-${origin}`, "顧客への返信案を作成する");
    const fixture = await installFixtureBackend(page, [task], [{ proposal, status: "pending" }], `confirm-${origin}`);
    await boot(page);
    let scope: Locator;
    if (origin === "today") {
      await page.goto("/tasks");
      scope = await openToday(page);
    } else if (origin === "list") {
      await page.goto("/tasks");
      await expectTaskListApproval(page, task);
      scope = await openTaskFromList(page, task);
    } else {
      scope = await openDetail(page, task);
    }
    await Promise.all([
      page.waitForURL(
        (url) =>
          url.pathname.endsWith("/chat") &&
          url.searchParams.get("s") === proposal.session_id &&
          url.searchParams.get("proposal") === proposal.id,
      ),
      card(scope, proposal.id).getByRole("button", { name: "確認", exact: true }).click(),
    ]);
    fixture.log("navigated", { url: page.url() });
    await expect(page.locator('[data-chat-composer-editor="true"] textarea').first()).toBeVisible();
    if (origin === "list") await shot(page, "chat-after-confirm-desktop", fixture);
    expect(fixture.decisionRequests).toHaveLength(0);
    expect(fixture.dispatchRequests).toHaveLength(0);
    expect(fixture.executionCount).toBe(0);
    expect(fixture.records.get(proposal.id)?.status).toBe("pending");
  });
}

test("詳細での承認が通信失敗しても再送せず、更新後はサーバーの確定状態を表示する", async ({ page }) => {
  const task = makeTask("acc-task-network", "通信失敗タスク");
  const proposal = makeProposal(task, "network", "請求書PDFを経理フォルダへ保存する");
  const fixture = await installFixtureBackend(page, [task], [{ proposal, status: "pending" }], "network-failure");
  await boot(page);
  const detail = await openDetail(page, task);
  const target = card(detail, proposal.id);
  await expectSameProposal(detail, proposal);
  fixture.dropNextDecisionResponse = true;
  // Fail the follow-up refresh too, so the unconfirmed outcome stays observable.
  fixture.failProposalList = true;
  await target.getByRole("button", { name: "承認", exact: true }).click();
  // The response was lost and the follow-up GET failed: the UI must neither
  // claim success nor offer the same decision again from this render.
  const list = detail.getByTestId("task-proposal-list");
  await expect(list.getByRole("alert").first()).toContainText("結果を確認できませんでした");
  await expect(list.getByRole("alert").last()).toContainText("fixture failure");
  await expect(list.getByText("承認待ちの提案はありません。")).toHaveCount(0);
  await expect(card(detail, proposal.id)).toHaveCount(0);
  await expect(page.getByText("承認を受け付けました。実行を開始します。")).toHaveCount(0);
  await shot(page, "detail-network-failure-desktop", fixture);
  expect(fixture.decisionRequests).toHaveLength(1);
  expect(fixture.executionCount).toBe(1);

  fixture.failProposalList = false;
  const decisionsBeforeRefresh = fixture.decisionRequests.length;
  await list.getByRole("button", { name: "更新", exact: true }).click();
  await expect(list.getByText("承認待ちの提案はありません。")).toBeVisible();
  expect(fixture.decisionRequests).toHaveLength(decisionsBeforeRefresh);
  const reloaded = await openDetail(page, task);
  await expect(reloaded.getByText("承認待ちの提案はありません。")).toBeVisible();
  await shot(page, "detail-after-network-recovery-desktop", fixture);
  expect(fixture.decisionRequests).toHaveLength(1);
  expect(fixture.executionCount).toBe(1);
});

test("別画面で承認済みの提案を拒否するとサーバーの確定状態に合わせて消え、実行は増えない", async ({ page }) => {
  const task = makeTask("acc-task-race", "競合タスク");
  const proposal = makeProposal(task, "race", "週報を共有チャンネルへ投稿する");
  const fixture = await installFixtureBackend(page, [task], [{ proposal, status: "pending" }], "approve-reject-race");
  await boot(page);
  await page.goto("/tasks");
  await expectTaskListApproval(page, task);
  const detail = await openTaskFromList(page, task);
  await expectSameProposal(detail, proposal);
  // Another tab approves first; this page still shows its earlier GET.
  fixture.records.get(proposal.id)!.status = "approved";
  fixture.executionCount += 1;
  fixture.log("other-client-approved", { executionCount: fixture.executionCount });
  await card(detail, proposal.id).getByRole("button", { name: "拒否", exact: true }).click();
  await expect(page.getByText("提案は最新状態ではありません。更新して確認してください。").first()).toBeVisible();
  await expect(card(page, proposal.id)).toHaveCount(0);
  await shot(page, "task-detail-race-desktop", fixture);
  expect(fixture.records.get(proposal.id)?.status).toBe("approved");
  expect(fixture.executionCount).toBe(1);
  expect(fixture.decisionRequests.map((item) => item.outcome)).toEqual(["409"]);
});

test("完了・拒否・取消済みの提案は承認待ちに出さず、未取得は0と区別する", async ({ page }) => {
  const task = makeTask("acc-task-settled", "決着済み提案のタスク");
  const fixture = await installFixtureBackend(
    page,
    [task],
    [
      { proposal: makeProposal(task, "completed", "完了済みの提案"), status: "completed" },
      { proposal: makeProposal(task, "rejected", "拒否済みの提案"), status: "rejected" },
      { proposal: makeProposal(task, "cancelled", "取消済みの提案"), status: "cancelled" },
    ],
    "settled-and-unfetched",
  );
  await boot(page);
  await page.goto("/tasks");
  let today = await openToday(page);
  let section = todayProposalSection(today);
  await expect(section.getByText("承認待ちの提案はありません。")).toBeVisible();
  await expect(section.getByText("0件", { exact: true })).toBeVisible();
  for (const title of ["完了済みの提案", "拒否済みの提案", "取消済みの提案"]) {
    await expect(page.getByText(title)).toHaveCount(0);
  }
  await shot(page, "today-settled-zero-desktop", fixture);
  await page.keyboard.press("Escape");
  await expect(today).toBeHidden();

  fixture.failProposalList = true;
  today = await openToday(page);
  section = todayProposalSection(today);
  await expect(section.getByRole("alert")).toContainText("fixture failure");
  await expect(section.getByText("承認待ちの提案はありません。")).toHaveCount(0);
  await expect(section.getByText("—", { exact: true })).toBeVisible();
  await expect(section.getByText("0件", { exact: true })).toHaveCount(0);
  await shot(page, "today-unfetched-desktop", fixture);
  expect(fixture.executionCount).toBe(0);
});

test("送信前に通信が切れた承認は自動再送せず、再読込後の明示操作で1回だけ実行する", async ({ page }) => {
  const task = makeTask("acc-task-resend", "再送確認タスク");
  const proposal = makeProposal(task, "resend", "見積書の下書きを作成する");
  const fixture = await installFixtureBackend(page, [task], [{ proposal, status: "pending" }], "resend-after-network-failure");
  await boot(page);
  await page.goto("/tasks");
  await expectTaskListApproval(page, task);
  const detail = await openTaskFromList(page, task);
  fixture.failNextDecisionBeforeApply = true;
  await card(detail, proposal.id).getByRole("button", { name: "承認", exact: true }).click();
  const locked = card(detail, proposal.id);
  // The follow-up GET still lists it as pending, but this render stays locked.
  await expect(locked.getByRole("status")).toContainText("確認できませんでした");
  await expect(locked.getByRole("button", { name: "承認", exact: true })).toBeDisabled();
  await expect(locked.getByRole("button", { name: "拒否", exact: true })).toBeDisabled();
  await expect(locked.getByRole("button", { name: "確認", exact: true })).toBeEnabled();
  await shot(page, "task-detail-network-before-apply-desktop", fixture);
  expect(fixture.executionCount).toBe(0);
  expect(fixture.decisionRequests).toHaveLength(1);

  await page.reload();
  const reloadedDetail = await openTaskFromList(page, task);
  await card(reloadedDetail, proposal.id).getByRole("button", { name: "承認", exact: true }).click();
  await expect(card(reloadedDetail, proposal.id)).toHaveCount(0);
  expect(fixture.executionCount).toBe(1);
  expect(fixture.decisionRequests.map((item) => item.outcome)).toEqual(["network-before-apply", "200"]);
});
