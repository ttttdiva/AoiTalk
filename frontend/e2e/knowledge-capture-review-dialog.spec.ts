import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const projectId = "11111111-1111-4111-8111-111111111111";
const candidateId = "22222222-2222-4222-8222-222222222222";
const questionId = "33333333-3333-4333-8333-333333333333";
const notificationId = "77777777-7777-4777-8777-777777777777";

const notification = {
  id: notificationId,
  type: "knowledge_capture_question",
  title: "解決内容を確認してください",
  message: "最終手順を確認してください",
  project_id: projectId,
  is_read: false,
  created_at: "2026-09-09T00:00:00Z",
  payload: {
    kind: "knowledge_capture",
    project_id: projectId,
    candidate_id: candidateId,
    candidate_version: 7,
    question_id: questionId,
    action_url: "https://attacker.invalid/should-not-be-used",
  },
};

const candidateDetail = {
  can_write: true,
  can_publish: true,
  id: candidateId,
  project_id: projectId,
  status: "needs_user",
  version: 7,
  publication_guard_adoption_required: false,
  questions: [
    {
      id: questionId,
      status: "pending",
      candidate_version: 7,
      title: "確認",
      message: "どの手順で解決しましたか？",
      options: [{ id: "runbook", label: "手順書を実行" }],
    },
  ],
  seed_task: {
    id: "44444444-4444-4444-8444-444444444444",
    title: "元Task",
    status: "completed",
    completed_at: "2026-09-09T00:00:00Z",
  },
  review_context: {
    review_reason: "最終手順だけ確認が必要です。",
    source_task: {
      id: "44444444-4444-4444-8444-444444444444",
      title: "元Task",
      status: "completed",
      completed_at: "2026-09-09T00:00:00Z",
    },
    chat_sessions: [
      {
        id: "55555555-5555-4555-8555-555555555555",
        title: "直接参照チャット",
        relation: "direct_reference",
      },
    ],
    publication_target: {
      action: "update",
      node_id: "66666666-6666-4666-8666-666666666666",
      title: "運用手順",
    },
  },
};

async function installKnowledgeCaptureRoutes(
  page: Page,
  options: { detailStatus?: number; draft?: boolean; readOnly?: boolean; canPublish?: boolean } = {},
) {
  const activeNotification = options.draft ? { ...notification, type: "knowledge_capture_draft", payload: { ...notification.payload, question_id: null } } : notification;
  const detail = options.draft ? {
    ...candidateDetail, status: "draft_ready", questions: [],
    draft: { title: "同期の整合性", problem: "関連付けのみの変更が反映されない", resolution: "flush後に関連付けを再取得する", verification: [{ text: "差分同期を確認した" }], evidence_summary: "検証記録 2件" },
    review_context: { ...candidateDetail.review_context, review_reason: null },
  } : candidateDetail;
  detail.can_write = options.readOnly !== true;
  detail.can_publish = options.canPublish !== false && options.readOnly !== true;
  let notificationActive = true;
  const requestLog: string[] = [];
  let answerRequests = 0;
  let reviewRequests = 0;
  let dismissRequests = 0;
  let publishRequests = 0;

  await page.route(`**/api/projects/${projectId}/knowledge-capture/settings`, async (route) => {
    await route.fulfill({ json: { project_id: projectId, mode: "suggest" } });
  });
  await page.route("**/api/notifications", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ json: notificationActive ? [activeNotification] : [] });
  });
  await page.route(`**/api/notifications/${notificationId}`, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    requestLog.push("notification");
    await route.fulfill({ json: activeNotification });
  });
  await page.route(
    `**/api/projects/${projectId}/knowledge-capture/candidates/${candidateId}`,
    async (route) => {
      requestLog.push("detail");
      if (options.detailStatus) {
        await route.fulfill({
          status: options.detailStatus,
          json: { detail: "candidate missing" },
        });
        return;
      }
      await route.fulfill({ json: detail });
    },
  );
  await page.route(`**/api/notifications/${notificationId}/read`, async (route) => {
    requestLog.push("read");
    await route.fulfill({ json: {} });
  });
  await page.route(
    options.draft
      ? `**/api/projects/${projectId}/knowledge-capture/candidates/${candidateId}/review`
      : `**/api/projects/${projectId}/knowledge-capture/candidates/${candidateId}/questions/${questionId}/review`,
    async (route) => {
      reviewRequests += 1;
      await route.fulfill({
        json: {
          candidate_version: 7,
          action: "discard_candidate",
          reply: "保存しない選択もできます。",
          rephrased_question: null,
        },
      });
    },
  );
  await page.route(
    `**/api/projects/${projectId}/knowledge-capture/candidates/${candidateId}/questions/${questionId}/answer`,
    async (route) => {
      answerRequests += 1;
      await route.fulfill({ status: 500, json: { detail: "unexpected answer" } });
    },
  );
  await page.route(
    `**/api/projects/${projectId}/knowledge-capture/candidates/${candidateId}/dismiss`,
    async (route) => {
      dismissRequests += 1;
      notificationActive = false;
      await route.fulfill({
        json: { ...candidateDetail, status: "dismissed", version: 8, questions: [] },
      });
    },
  );

  await page.route(`**/api/projects/${projectId}/knowledge-capture/candidates/${candidateId}/publish`, async (route) => {
    publishRequests += 1;
    expect(route.request().postDataJSON()).toEqual({ expected_candidate_version: 7 });
    notificationActive = false;
    await route.fulfill({ json: { ...detail, status: "published", version: 8, questions: [] } });
  });

  return {
    requestLog,
    get publishRequests() { return publishRequests; },
    get answerRequests() {
      return answerRequests;
    },
    get reviewRequests() {
      return reviewRequests;
    },
    get dismissRequests() {
      return dismissRequests;
    },
  };
}

test.describe("Knowledge Capture review dialog without product DB", () => {
  test("readonly notification shows the full draft and disables mutation controls", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    const routes = await installKnowledgeCaptureRoutes(page, { draft: true, readOnly: true });
    await page.goto("/tasks");
    await page.getByTestId("notification-trigger").click();
    await page.getByRole("button", { name: notification.title }).click();
    await expect(page.getByTestId("knowledge-capture-draft-preview")).toContainText("flush後に関連付けを再取得する");
    await expect(page.getByText(/閲覧のみの権限です/)).toBeVisible();
    await expect(page.getByTestId("knowledge-capture-review-composer")).toBeDisabled();
    for (const action of ["publish", "ask-ai", "dismiss"]) {
      await expect(page.getByTestId(`knowledge-capture-${action}-button`)).toBeDisabled();
    }
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.getByTestId("knowledge-capture-review-dialog")).toBeHidden();
    expect(routes.reviewRequests).toBe(0);
    expect(routes.publishRequests).toBe(0);
    expect(routes.dismissRequests).toBe(0);
  });

  test("a writer without management permission can ask but cannot adopt", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    const routes = await installKnowledgeCaptureRoutes(page, { draft: true, canPublish: false });
    await page.goto("/tasks");
    await page.getByTestId("notification-trigger").click();
    await page.getByRole("button", { name: notification.title }).click();
    await expect(page.getByText(/Docsへの採用・保存にはプロジェクトの管理権限が必要です/)).toBeVisible();
    await expect(page.getByTestId("knowledge-capture-publish-button")).toBeDisabled();
    await page.getByTestId("knowledge-capture-review-composer").fill("なぜ保存候補ですか？");
    await page.getByTestId("knowledge-capture-ask-ai-button").click();
    await expect(page.getByTestId("knowledge-capture-review-response")).toContainText("保存しない選択もできます。");
    expect(routes.reviewRequests).toBe(1);
    expect(routes.publishRequests).toBe(0);
    await expect(page.getByTestId("knowledge-capture-dismiss-button")).toBeEnabled();
  });

  test("opens in place, keeps Ask-AI non-authoritative, and explicitly dismisses", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    const routes = await installKnowledgeCaptureRoutes(page);
    await page.goto("/tasks");
    await expect(page.getByTestId("task-list-toolbar")).toBeVisible();

    const urlBefore = page.url();
    await page.getByRole("button", { name: /通知を開く/ }).click();
    await expect(page.getByTestId("notification-popover")).toBeVisible();
    await page.getByRole("button", { name: notification.title }).click();
    await expect(page.getByTestId("knowledge-capture-review-dialog")).toBeVisible();
    expect(page.url()).toBe(urlBefore);
    await expect(page.getByText("最終手順だけ確認が必要です。")).toBeVisible();
    await expect(page.getByText("元Task")).toBeVisible();
    await expect(page.getByText("直接参照チャット")).toBeVisible();
    await expect(page.getByText("運用手順")).toBeVisible();
    await expect(page.getByText("どの手順で解決しましたか？")).toBeVisible();
    await expect(page.getByRole("link", { name: "元Task" })).toHaveAttribute(
      "href",
      "/tasks/44444444-4444-4444-8444-444444444444",
    );
    await expect(page.getByRole("link", { name: "直接参照チャット" })).toHaveAttribute(
      "href",
      "/chat?s=55555555-5555-4555-8555-555555555555",
    );
    await expect(page.getByRole("link", { name: "運用手順" })).toHaveAttribute(
      "href",
      "/docs/66666666-6666-4666-8666-666666666666",
    );

    await expect.poll(() => routes.requestLog).toEqual(["notification", "detail", "read"]);
    await page.getByTestId("knowledge-capture-review-composer").fill("これいらなくね？");
    await page.getByRole("button", { name: "AIに聞き返す" }).click();
    await expect(page.getByTestId("knowledge-capture-review-response")).toContainText(
      "保存しない選択もできます。",
    );
    await expect(page.getByText("どの手順で解決しましたか？")).toBeVisible();
    expect(routes.reviewRequests).toBe(1);
    expect(routes.answerRequests).toBe(0);
    expect(routes.dismissRequests).toBe(0);
    await expect(page.getByText(/AIは今回は保存しない判断も可能/)).toBeVisible();

    await page.getByTestId("knowledge-capture-dismiss-button").click();
    await expect(page.getByTestId("knowledge-capture-review-dialog")).toBeHidden();
    await expect.poll(() => routes.dismissRequests).toBe(1);
    expect(routes.answerRequests).toBe(0);
    expect(page.url()).toBe(urlBefore);
  });

  test("keeps the notification unread and the URL unchanged on a 404", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    const routes = await installKnowledgeCaptureRoutes(page, { detailStatus: 404 });
    await page.goto("/tasks");

    const urlBefore = page.url();
    await page.getByRole("button", { name: /通知を開く/ }).click();
    await page.getByRole("button", { name: notification.title }).click();
    await expect(page.getByRole("alert").filter({ hasText: "未読のまま" })).toBeVisible();
    expect(page.url()).toBe(urlBefore);
    expect(routes.requestLog).toEqual(["notification", "detail"]);
    expect(routes.answerRequests).toBe(0);
    expect(routes.reviewRequests).toBe(0);
    expect(routes.dismissRequests).toBe(0);
  });
});


test("draft notification supports questions before an explicit adoption on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  const routes = await installKnowledgeCaptureRoutes(page, { draft: true });
  await page.goto("/tasks");
  const urlBefore = page.url();
  await page.getByRole("button", { name: /通知を開く/ }).click();
  await page.getByRole("button", { name: notification.title }).click();
  await expect(page.getByTestId("knowledge-capture-review-dialog")).toBeVisible();
  await expect(page.getByTestId("knowledge-capture-draft-preview")).toContainText("flush後に関連付けを再取得する");
  await expect(page.getByTestId("knowledge-capture-draft-preview")).toContainText("検証記録 2件");
  await expect(page.getByTestId("knowledge-capture-publish-button")).toBeEnabled();
  expect(routes.publishRequests).toBe(0);
  await page.getByTestId("knowledge-capture-review-composer").fill("この補足を踏まえて採用の理由を教えて");
  await page.getByTestId("knowledge-capture-ask-ai-button").click();
  await expect(page.getByTestId("knowledge-capture-review-response")).toContainText("保存しない選択もできます");
  expect(routes.reviewRequests).toBe(1);
  expect(routes.answerRequests).toBe(0);
  expect(routes.publishRequests).toBe(0);
  expect(routes.dismissRequests).toBe(0);
  await page.getByTestId("knowledge-capture-publish-button").click();
  await expect(page.getByTestId("knowledge-capture-review-dialog")).toBeHidden();
  expect(routes.publishRequests).toBe(1);
  expect(page.url()).toBe(urlBefore);
});
