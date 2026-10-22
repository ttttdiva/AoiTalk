import { expect, test, type Locator, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const BASE_PATH = (
  process.env.PLAYWRIGHT_BASE_PATH?.trim() ||
  process.env.NEXT_PUBLIC_BASE_PATH?.trim() ||
  ""
).replace(/^\/+|\/+$/g, "");

const project = {
  id: "proposal-project",
  name: "Proposal project",
  slug: "proposal-project",
  space_id: "proposal-space",
  is_member: true,
  is_participating: true,
  can_write: true,
  can_manage_settings: true,
  is_completed: false,
};

function makeTask() {
  const now = Date.now();
  return {
    id: "proposal-task",
    project_id: project.id,
    project_name: project.name,
    title: "提案対象タスク",
    description: "承認待ち提案を確認するタスク",
    status: "open",
    priority: "medium",
    // Keep the task in the browser's local Today window even when the suite
    // runs in a timezone different from the server.
    start_at: new Date(now - 5 * 60 * 1000).toISOString(),
    end_at: new Date(now + 60 * 60 * 1000).toISOString(),
    all_day: false,
    reminder_offsets: [],
    notifications_enabled: true,
    source: "manual",
    created_by: "e2e-fixture",
    completed_at: null,
    created_at: "2026-10-20T00:00:00.000Z",
    updated_at: "2026-10-20T00:00:00.000Z",
    metadata: {},
    assignees: [],
    tags: [],
    active_time_entry: null,
    estimated_hours: null,
    sort_order: 0,
    total_time_seconds: 0,
    parent_task_id: null,
    subtasks: [],
    comments: [],
    activities: [],
    has_recurrence: false,
  };
}

function makeProposal(task: ReturnType<typeof makeTask>) {
  return {
    id: "proposal-shared-1",
    task_id: task.id,
    session_id: "proposal-session-1",
    agent_run_id: "proposal-run-1",
    revision: 7,
    plan_id: "proposal-plan-1",
    plan_digest: "proposal-plan-digest",
    action_digest: "proposal-action-digest",
    material_action_preview_digest: "proposal-preview-digest",
    title: "リンク済み会話をアーカイブする",
    summary: "同じ提案を3つのタスク画面で確認します。",
    plan_text: "提案の計画本文",
    status: "pending" as const,
  };
}

function apiPath(url: URL): string {
  const prefix = BASE_PATH ? `/${BASE_PATH}` : "";
  return prefix && url.pathname.startsWith(`${prefix}/`)
    ? url.pathname.slice(prefix.length)
    : url.pathname;
}

type FixtureState = {
  proposalStatus: "pending" | "resolved";
  apiPosts: string[];
  chatLifecyclePosts: string[];
  resetApiPosts: () => void;
  decisionRequests: Array<{
    path: string;
    body: Record<string, unknown>;
  }>;
  dispatchRequests: string[];
};

async function installFixtureRoutes(
  page: Page,
  task: ReturnType<typeof makeTask>,
  proposal: ReturnType<typeof makeProposal>,
): Promise<FixtureState> {
  const state: FixtureState = {
    proposalStatus: "pending",
    apiPosts: [],
    chatLifecyclePosts: [],
    resetApiPosts: () => {
      state.apiPosts.length = 0;
      state.chatLifecyclePosts.length = 0;
    },
    decisionRequests: [],
    dispatchRequests: [],
  };
  const decisionPath = `/api/python-proxy/tasks/${task.id}/agent-proposals/${proposal.id}/decision`;

  await mockAuthenticatedApis(page);
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = apiPath(url);
    const method = request.method();

    if (method === "POST") state.apiPosts.push(pathname);

    if (pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "e2e-fixture",
            username: "__playwright_e2e__",
            role: "admin",
            user_settings: {},
          },
        },
      });
      return;
    }
    if (pathname === "/api/spaces") {
      await route.fulfill({
        json: {
          spaces: [
            {
              id: project.space_id,
              name: "Proposal space",
              slug: "proposal-space",
              is_member: true,
              can_write: true,
            },
          ],
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
      await route.fulfill({ json: [task] });
      return;
    }
    if (pathname === `/api/tasks/${task.id}` && method === "GET") {
      await route.fulfill({ json: task });
      return;
    }
    if (pathname === "/api/task-occurrences" && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/tasks/${task.id}/attachments` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/tasks/${task.id}/references` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (pathname === `/api/tasks/${task.id}/recurrence` && method === "GET") {
      await route.fulfill({ json: null });
      return;
    }
    if (pathname === `/api/projects/${project.id}/tags` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (
      pathname === `/api/projects/${project.id}/assignee-candidates` &&
      method === "GET"
    ) {
      await route.fulfill({ json: { members: [], total: 0 } });
      return;
    }
    if (pathname === "/api/users/me/settings" && method === "GET") {
      await route.fulfill({ json: { settings: {} } });
      return;
    }
    if (
      pathname === "/api/python-proxy/task-proposals" &&
      method === "GET"
    ) {
      await route.fulfill({
        json: {
          success: true,
          proposals: state.proposalStatus === "pending" ? [proposal] : [],
        },
      });
      return;
    }
    if (pathname === decisionPath && method === "POST") {
      const body = (request.postDataJSON() ?? {}) as Record<string, unknown>;
      state.decisionRequests.push({ path: pathname, body });
      state.proposalStatus = "resolved";
      await route.fulfill({
        json: {
          success: true,
          task_id: task.id,
          proposal_id: proposal.id,
          action: body.action,
        },
      });
      return;
    }
    if (
      pathname === "/api/conversations" &&
      method === "GET"
    ) {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }
    if (pathname === "/api/conversations" && method === "POST") {
      await route.fulfill({
        json: {
          session: {
            id: proposal.session_id,
            user_id: "e2e-fixture",
            title: "Proposal session",
            character_name: "aoi",
            message_count: 0,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
        },
      });
      return;
    }
    if (
      pathname === `/api/conversations/${proposal.session_id}/messages` &&
      method === "GET"
    ) {
      await route.fulfill({ json: { messages: [] } });
      return;
    }
    if (
      pathname === `/api/conversations/${proposal.session_id}/read` &&
      method === "POST"
    ) {
      state.chatLifecyclePosts.push(pathname);
      await route.fulfill({
        json: {
          success: true,
          session_id: proposal.session_id,
          last_read_at: null,
        },
      });
      return;
    }
    if (
      pathname === `/api/conversations/${proposal.session_id}/resume` &&
      method === "POST"
    ) {
      state.chatLifecyclePosts.push(pathname);
      await route.fulfill({
        json: {
          success: true,
          session: {
            id: proposal.session_id,
            user_id: "e2e-fixture",
            title: "Proposal session",
            character_name: "aoi",
            message_count: 0,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
          messages: [],
        },
      });
      return;
    }
    if (
      pathname === `/api/conversations/${proposal.session_id}/resume` &&
      method === "GET"
    ) {
      await route.fulfill({
        json: {
          session: {
            id: proposal.session_id,
            user_id: "e2e-fixture",
            title: "Proposal session",
            character_name: "aoi",
            message_count: 0,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
          messages: [],
        },
      });
      return;
    }
    if (
      pathname ===
        `/api/python-proxy/conversations/${proposal.session_id}/generation/status` &&
      method === "GET"
    ) {
      await route.fulfill({
        json: {
          session_id: proposal.session_id,
          running: false,
          status: "idle",
          message: null,
        },
      });
      return;
    }
    if (pathname.endsWith("/dispatch") && method === "POST") {
      state.dispatchRequests.push(pathname);
      await route.fulfill({
        json: {
          success: true,
          queued: false,
          session_id: proposal.session_id,
        },
      });
      return;
    }

    // mockAuthenticatedApis is the final fixture boundary for shell APIs that
    // are unrelated to this scenario; no request reaches the production API.
    await route.fallback();
  });

  return state;
}

function visibleProposalCard(scope: Page, proposalId: string): Locator;
function visibleProposalCard(scope: Locator, proposalId: string): Locator;
function visibleProposalCard(
  scope: Page | Locator,
  proposalId: string,
): Locator {
  return scope.locator(
    `[data-testid="task-proposal-card-${proposalId}"]:visible`,
  );
}

async function assertProposalCard(
  card: Locator,
  proposal: ReturnType<typeof makeProposal>,
) {
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("data-proposal-id", proposal.id);
  await expect(card).toHaveAttribute(
    "data-proposal-revision",
    String(proposal.revision),
  );
  await expect(
    card.getByRole("heading", { name: proposal.title, exact: true }),
  ).toBeVisible();
  await expect(card.getByRole("button")).toHaveCount(3);
  await expect(card.getByRole("button")).toHaveText([
    "承認",
    "拒否",
    "確認",
  ]);
}

test("同じ提案がToday・タスク一覧・タスク詳細に表示され、確認は既存チャットへ遷移する", async ({
  page,
}) => {
  const task = makeTask();
  const proposal = makeProposal(task);
  const fixture = await installFixtureRoutes(page, task, proposal);
  await page.addInitScript(() => localStorage.clear());
  await addAuthCookie(page);

  await page.goto("/tasks");
  await expect(page.getByTestId(`task-row-${task.id}`)).toBeVisible();

  const todayTrigger = page
    .getByRole("button", { name: "Todayを開く", exact: true })
    .first();
  await expect(todayTrigger).toBeVisible();
  await todayTrigger.click();

  const todayDialog = page
    .locator('[role="dialog"]:visible')
    .filter({ hasText: "Today" })
    .first();
  await expect(todayDialog).toBeVisible();
  await assertProposalCard(
    visibleProposalCard(todayDialog, proposal.id),
    proposal,
  );

  await page.keyboard.press("Escape");
  await expect(todayDialog).toBeHidden();
  await page.goto("/tasks");
  await expect(page.getByTestId(`task-row-${task.id}`)).toBeVisible();
  await assertProposalCard(
    visibleProposalCard(page, proposal.id),
    proposal,
  );

  await page
    .getByTestId(`task-row-${task.id}`)
    .getByText(task.title, { exact: true })
    .click();
  const detailDialog = page
    .locator('[role="dialog"]:visible')
    .filter({ hasText: task.title })
    .first();
  await expect(detailDialog).toBeVisible();
  await assertProposalCard(
    visibleProposalCard(detailDialog, proposal.id),
    proposal,
  );

  fixture.resetApiPosts();
  await Promise.all([
    page.waitForURL(
      (url) =>
        url.pathname.endsWith("/chat") &&
        url.searchParams.get("s") === proposal.session_id,
    ),
    visibleProposalCard(detailDialog, proposal.id)
      .getByRole("button", { name: "確認", exact: true })
      .click(),
  ]);

  await expect(
    page.locator('[data-chat-composer-editor="true"] textarea').first(),
  ).toBeVisible();
  const readPath = `/api/conversations/${proposal.session_id}/read`;
  const resumePath = `/api/conversations/${proposal.session_id}/resume`;
  await expect
    .poll(() => fixture.chatLifecyclePosts.includes(readPath))
    .toBe(true);
  await expect
    .poll(() => fixture.chatLifecyclePosts.includes(resumePath))
    .toBe(true);

  expect(fixture.decisionRequests).toHaveLength(0);
  expect(fixture.dispatchRequests).toHaveLength(0);
  const permittedNavigationPosts = new Set([
    readPath,
    resumePath,
    "/api/python-proxy/aoidots/bootstrap",
  ]);
  expect(fixture.apiPosts.every((path) => permittedNavigationPosts.has(path))).toBe(
    true,
  );
  expect(fixture.apiPosts).not.toContain(
    `/api/conversations/${proposal.session_id}/messages`,
  );
});

for (const action of ["approve", "reject"] as const) {
  test(`${action === "approve" ? "承認" : "拒否"}はモック済みの提案決定エンドポイントだけを呼ぶ`, async ({
    page,
  }) => {
    const task = makeTask();
    const proposal = makeProposal(task);
    const fixture = await installFixtureRoutes(page, task, proposal);
    await page.addInitScript(() => localStorage.clear());
    await addAuthCookie(page);

    await page.goto("/tasks");
    const card = visibleProposalCard(page, proposal.id);
    await assertProposalCard(card, proposal);
    fixture.resetApiPosts();
    await card
      .getByRole("button", {
        name: action === "approve" ? "承認" : "拒否",
        exact: true,
      })
      .click();

    const decisionPath = `/api/python-proxy/tasks/${task.id}/agent-proposals/${proposal.id}/decision`;
    await expect
      .poll(() => fixture.decisionRequests.length)
      .toBe(1);
    await expect(
      page.locator(`[data-testid="task-proposal-card-${proposal.id}"]:visible`),
    ).toHaveCount(0);

    expect(fixture.apiPosts).toEqual([decisionPath]);
    expect(fixture.dispatchRequests).toHaveLength(0);
    expect(fixture.decisionRequests[0]).toEqual({
      path: decisionPath,
      body: {
        action,
        revision: proposal.revision,
        plan_id: proposal.plan_id,
        plan_digest: proposal.plan_digest,
        action_digest: proposal.action_digest,
        material_action_preview_digest: proposal.material_action_preview_digest,
      },
    });
  });
}
