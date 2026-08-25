import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie } from "./support/auth";

const savedNode = {
  id: "saved-node",
  workspace_id: "workspace-1",
  parent_id: null,
  root_page_id: "saved-node",
  project_id: null,
  title: "保存されたノード",
  description: "",
  body_json: {},
  body_text: "",
  node_type: "page",
  sort_order: 1,
  created_at: "2026-07-23T00:00:00",
  updated_at: "2026-07-23T00:00:00",
  archived_at: null,
};

const homeNode = {
  ...savedNode,
  id: "home-node",
  root_page_id: "home-node",
  title: "取り込み前に開いているノード",
  sort_order: 0,
};

const docsState = {
  workspace: {
    id: "workspace-1",
    name: "Personal Docs",
    description: "",
    owner_user_id: "user-1",
    settings: {},
    created_at: "2026-07-23T00:00:00",
    updated_at: "2026-07-23T00:00:00",
  },
  nodes: [homeNode, savedNode],
  supertags: [],
  node_supertags: [],
  fields: [],
  field_values: [],
  views: [],
  ai_suggestions: [],
  import_jobs: [],
  import_items: [],
  attachments: [],
  edges: [],
  projects: [],
  has_children_ids: [],
  loaded_children_parent_ids: ["saved-node"],
  details_loaded_ids: ["saved-node"],
  has_details_ids: [],
  children_next_cursor_by_parent: {},
};

type MockServerStatus = "queued" | "running" | "success" | "failed";

type MockJob = {
  id: string;
  source: string;
  status: MockServerStatus;
  created_at: string;
  retry_of_job_id: string | null;
  result: Record<string, unknown> | null;
  error: Record<string, unknown>;
  retryable: boolean;
};

type MockIngestOptions = {
  initialStatus?: MockServerStatus;
  retryStatus?: MockServerStatus;
};

type MockIngestController = {
  jobs: Map<string, MockJob>;
  createRequests: Array<{ id: string; body: Record<string, unknown> }>;
  createStatuses: number[];
  retryRequests: Array<{ sourceId: string; id: string }>;
  detailRequests: Map<string, number>;
  setStatus: (id: string, status: MockServerStatus) => void;
  failNextPoll: (id: string) => void;
  status: (id: string) => MockServerStatus | undefined;
};

function resultForSource(source: string): Record<string, unknown> {
  return {
    target_id: "inbox",
    target_label: "Inbox",
    action: "create",
    changed_node_id: "changed-node-that-is-not-opened",
    changed_node_title: "変更された別ノード",
    open_node_id: savedNode.id,
    open_node_title: savedNode.title,
    direct_urls: source.includes("example.com") ? ["https://example.com/article"] : [],
    supplemental_urls: [],
    failed_urls: [],
    used_urls: source.includes("example.com") ? ["https://example.com/article"] : [],
    unconfirmed: [],
  };
}

function wireJob(job: MockJob): Record<string, unknown> {
  return {
    job_id: job.id,
    id: job.id,
    status: job.status,
    created_at: job.created_at,
    source: job.source,
    target_node_id: null,
    upload_ids: [],
    skip_image_recognition: false,
    enable_external_research: true,
    retry_of_job_id: job.retry_of_job_id,
    retryable: job.retryable,
    result: job.result ?? {},
    error: job.error,
  };
}

async function installDocsClipIngestMock(
  page: Page,
  options: MockIngestOptions = {},
): Promise<MockIngestController> {
  const jobs = new Map<string, MockJob>();
  const createRequests: Array<{ id: string; body: Record<string, unknown> }> = [];
  const createStatuses: number[] = [];
  const retryRequests: Array<{ sourceId: string; id: string }> = [];
  const detailRequests = new Map<string, number>();
  const failedPollIds = new Set<string>();
  let sequence = 0;
  const initialStatus = options.initialStatus ?? "success";
  const retryStatus = options.retryStatus ?? "success";

  const nextJobId = () => {
    sequence += 1;
    return `00000000-0000-4000-8000-${sequence.toString().padStart(12, "0")}`;
  };

  const makeJob = (
    source: string,
    status: MockServerStatus,
    retryOfJobId: string | null = null,
  ): MockJob => {
    const id = nextJobId();
    const job: MockJob = {
      id,
      source,
      status,
      created_at: `2026-07-23T00:00:${sequence.toString().padStart(2, "0")}Z`,
      retry_of_job_id: retryOfJobId,
      result: status === "success" ? resultForSource(source) : null,
      error: status === "failed"
        ? {
            message: "テスト用の取り込み失敗",
            code: "TEST_FAILURE",
            retryable: true,
          }
        : {},
      retryable: true,
    };
    jobs.set(id, job);
    return job;
  };

  const controller: MockIngestController = {
    jobs,
    createRequests,
    createStatuses,
    retryRequests,
    detailRequests,
    setStatus: (id, status) => {
      const job = jobs.get(id);
      if (!job) return;
      job.status = status;
      job.result = status === "success" ? resultForSource(job.source) : null;
      job.error = status === "failed"
        ? {
            message: "テスト用の取り込み失敗",
            code: "TEST_FAILURE",
            retryable: true,
          }
        : {};
    },
    failNextPoll: (id) => {
      failedPollIds.add(id);
    },
    status: (id) => jobs.get(id)?.status,
  };

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: "user-1", username: "tester", role: "admin" },
        },
      });
      return;
    }
    if (url.pathname === "/api/docs/ingest/jobs") {
      if (method === "GET") {
        const currentJobs = Array.from(jobs.values()).reverse().map(wireJob);
        await route.fulfill({ json: { jobs: currentJobs, items: currentJobs } });
        return;
      }
      if (method === "POST") {
        let body: Record<string, unknown> = {};
        try {
          const parsed = route.request().postDataJSON();
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            body = parsed as Record<string, unknown>;
          }
        } catch {
          // Keep the deterministic mock tolerant of an empty malformed body.
        }
        const source = typeof body.source === "string" ? body.source : "";
        const job = makeJob(source, initialStatus);
        createRequests.push({ id: job.id, body });
        createStatuses.push(202);
        await route.fulfill({ status: 202, json: wireJob(job) });
        return;
      }
    }

    const jobMatch = url.pathname.match(/^\/api\/docs\/ingest\/jobs\/([^/]+)(?:\/(retry))?$/);
    if (jobMatch) {
      const [, jobId, suffix] = jobMatch;
      if (method === "GET" && !suffix) {
        if (failedPollIds.delete(jobId)) {
          await route.abort("failed");
          return;
        }
        detailRequests.set(jobId, (detailRequests.get(jobId) ?? 0) + 1);
        const job = jobs.get(jobId);
        if (!job) {
          await route.fulfill({ status: 404, json: { detail: "job not found" } });
          return;
        }
        await route.fulfill({ json: wireJob(job) });
        return;
      }
      if (method === "POST" && suffix === "retry") {
        const sourceJob = jobs.get(jobId);
        if (!sourceJob || sourceJob.status !== "failed") {
          await route.fulfill({ status: 409, json: { detail: "job is not retryable" } });
          return;
        }
        const retried = makeJob(sourceJob.source, retryStatus, sourceJob.id);
        retryRequests.push({ sourceId: sourceJob.id, id: retried.id });
        await route.fulfill({ status: 202, json: wireJob(retried) });
        return;
      }
    }
    if (url.pathname === "/api/docs/bootstrap") {
      await route.fulfill({ json: docsState });
      return;
    }
    if (
      url.pathname === "/api/docs/nodes/saved-node/tree"
      || url.pathname === "/api/docs/nodes/saved-node/children"
      || url.pathname === "/api/docs/nodes/saved-node/details"
    ) {
      await route.fulfill({ json: { ...docsState, focus_node_id: savedNode.id } });
      return;
    }
    if (url.pathname === "/api/projects") {
      await route.fulfill({ json: { projects: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/users/me/settings") {
      await route.fulfill({ json: { settings: {} } });
      return;
    }
    if (
      url.pathname === "/api/spaces"
      || url.pathname === "/api/notifications"
      || url.pathname === "/api/tasks"
      || url.pathname === "/api/users/list"
    ) {
      await route.fulfill({ json: url.pathname === "/api/spaces" ? { spaces: [], total: 0 } : [] });
      return;
    }
    if (url.pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/time-entries/active") {
      await route.fulfill({ json: null });
      return;
    }
    await route.fulfill({ json: {} });
  });

  return controller;
}

async function submitClip(page: Page, source: string) {
  await page.keyboard.press("Control+Alt+I");
  const dialog = page.getByRole("dialog", {
    name: "クリップ取り込み",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("取り込むURLまたは文章").fill(source);
  await dialog.getByRole("button", { name: "取り込む" }).click();
  await expect(dialog).toHaveCount(0);
}

async function waitForCreated(
  controller: MockIngestController,
  count = 1,
): Promise<string> {
  await expect.poll(() => controller.createRequests.length).toBe(count);
  return controller.createRequests[count - 1].id;
}

test("Docsのショートカットからクリップを取り込み、保存ノードを開ける", async ({ page }) => {
  await addAuthCookie(page);
  await installDocsClipIngestMock(page);
  await page.goto("/docs");

  await expect(page.getByRole("button", { name: "クリップ取り込み" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "ページタイトル" })).toHaveValue(homeNode.title);
  await page.keyboard.press("Control+I");
  await expect(
    page.getByRole("dialog", { name: "クリップ取り込み", exact: true }),
  ).toHaveCount(0);

  await page.keyboard.press("Control+Alt+I");
  const dialog = page.getByRole("dialog", {
    name: "クリップ取り込み",
    exact: true,
  });
  await expect(dialog).toBeVisible();

  await dialog.getByLabel("取り込むURLまたは文章").fill(
    "https://example.com/article\n補足文章",
  );
  await dialog.getByRole("button", { name: "取り込む" }).click();
  await expect(dialog).toHaveCount(0);

  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history).toBeVisible();
  await expect(history.getByText("完了")).toBeVisible();
  await history.locator("button").filter({ hasText: "完了" }).click();

  const resultDialog = page.getByRole("dialog", { name: "取り込みが完了しました" });
  await expect(resultDialog).toBeVisible();
  await expect(resultDialog.getByText("Inbox")).toBeVisible();
  await expect(resultDialog.getByText("新規作成")).toBeVisible();

  await resultDialog.getByRole("button", { name: "Docsで開く" }).click();
  await expect(resultDialog).toHaveCount(0);
  // Docs 内では canonical route に遷移せず、現在の Workspace を in-place で切り替える。
  await expect(page).toHaveURL(/\/docs\/?$/);
  await expect(history).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "ページタイトル" })).toHaveValue(savedNode.title);
});

test("Docs以外のタブでもショートカットは遷移せずモーダルだけ開く", async ({ page }) => {
  await addAuthCookie(page);
  await installDocsClipIngestMock(page);
  await page.goto("/chat");
  const currentUrl = page.url();

  await page.keyboard.press("Control+Alt+I");

  await expect(
    page.getByRole("dialog", { name: "クリップ取り込み", exact: true }),
  ).toBeVisible();
  expect(page.url()).toBe(currentUrl);
});

test("Docs外から取り込み、Docsで開くと対象ノードまで入る", async ({ page }) => {
  await addAuthCookie(page);
  await installDocsClipIngestMock(page);
  await page.goto("/chat");

  await page.keyboard.press("Control+Alt+I");
  const dialog = page.getByRole("dialog", {
    name: "クリップ取り込み",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("取り込むURLまたは文章").fill("https://example.com/article");
  await dialog.getByRole("button", { name: "取り込む" }).click();

  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history).toBeVisible();
  await expect(history.getByText("完了")).toBeVisible();
  await history.locator("button").filter({ hasText: "完了" }).click();

  const resultDialog = page.getByRole("dialog", { name: "取り込みが完了しました" });
  await expect(resultDialog).toBeVisible();
  await resultDialog.getByRole("button", { name: "Docsで開く" }).click();

  await expect(page).toHaveURL(/\/docs\/saved-node\/?$/);
  await expect(page.getByRole("textbox", { name: "ページタイトル" })).toHaveValue(savedNode.title);
});

test("durableな取り込みジョブはクライアント遷移後も同じIDと状態を保つ", async ({ page }) => {
  await addAuthCookie(page);
  const controller = await installDocsClipIngestMock(page, { initialStatus: "running" });
  await page.goto("/docs");

  await submitClip(page, "client navigation job");
  const jobId = await waitForCreated(controller);
  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history.getByText("取り込み中")).toBeVisible();

  // The open Sheet owns a modal overlay. Close it explicitly before using the
  // global-rail links; closing history must not cancel the server job.
  await history.getByRole("button", { name: "取り込み履歴を閉じる" }).click();
  await expect(history).toHaveCount(0);
  await page.locator('a[aria-label="チャット"]').click();
  await expect(page).toHaveURL(/\/chat\/?$/);
  await page.locator('a[aria-label="Docs"]').click();
  await expect(page).toHaveURL(/\/docs\/?$/);

  await page.getByRole("button", { name: "取り込み履歴" }).click();
  const reopenedHistory = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(reopenedHistory.getByText("取り込み中")).toBeVisible();
  expect(controller.createRequests.map((request) => request.id)).toEqual([jobId]);
  expect(controller.status(jobId)).toBe("running");
});

test("実行中ジョブをハードリロードしても同じIDを再取得し完了できる", async ({ page }) => {
  await addAuthCookie(page);
  const controller = await installDocsClipIngestMock(page, { initialStatus: "running" });
  await page.goto("/docs");

  await submitClip(page, "hard reload job");
  const jobId = await waitForCreated(controller);
  await expect(page.getByRole("dialog", { name: "クリップ取り込みの状況" }).getByText("取り込み中")).toBeVisible();

  await page.reload();
  await expect(page.getByRole("button", { name: "取り込み履歴" })).toBeVisible();
  await page.getByRole("button", { name: "取り込み履歴" }).click();
  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history.getByText("取り込み中")).toBeVisible();
  expect(controller.createRequests.map((request) => request.id)).toEqual([jobId]);
  expect(controller.status(jobId)).toBe("running");

  controller.setStatus(jobId, "success");
  await expect(history.getByText("完了")).toBeVisible();
  expect(controller.status(jobId)).toBe("success");
  expect(controller.createRequests).toHaveLength(1);
});

test("二つの取り込みを素早く送信しても二つのジョブが作成される", async ({ page }) => {
  await addAuthCookie(page);
  const controller = await installDocsClipIngestMock(page, { initialStatus: "success" });
  await page.goto("/docs");

  await submitClip(page, "quick submission 1");
  await submitClip(page, "quick submission 2");
  await waitForCreated(controller, 2);

  expect(controller.createStatuses).toEqual([202, 202]);
  expect(new Set(controller.createRequests.map((request) => request.id)).size).toBe(2);
  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history.getByText("完了")).toHaveCount(2);
  expect(Array.from(controller.jobs.values()).every((job) => job.status === "success")).toBe(true);
});

test("履歴を閉じて再度開いても実行中ジョブをキャンセルしない", async ({ page }) => {
  await addAuthCookie(page);
  const controller = await installDocsClipIngestMock(page, { initialStatus: "running" });
  await page.goto("/docs");

  await submitClip(page, "close and reopen history");
  const jobId = await waitForCreated(controller);
  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history.getByText("取り込み中")).toBeVisible();

  await history.getByRole("button", { name: "取り込み履歴を閉じる" }).click();
  await expect(history).toHaveCount(0);
  await page.getByRole("button", { name: "取り込み履歴" }).click();
  const reopenedHistory = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(reopenedHistory.getByText("取り込み中")).toBeVisible();
  expect(controller.createRequests).toHaveLength(1);
  expect(controller.status(jobId)).toBe("running");

  controller.setStatus(jobId, "success");
  await expect(reopenedHistory.getByText("完了")).toBeVisible();
});

test("ポーリングのネットワーク失敗から復旧しても同じジョブを完了する", async ({ page }) => {
  await addAuthCookie(page);
  const controller = await installDocsClipIngestMock(page, { initialStatus: "running" });
  await page.goto("/docs");

  await submitClip(page, "poll recovery job");
  const jobId = await waitForCreated(controller);
  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history.getByText("取り込み中")).toBeVisible();
  controller.failNextPoll(jobId);

  await expect(page.getByText("接続できません。状態を再取得します。")).toBeVisible();
  expect(controller.status(jobId)).toBe("running");
  expect(controller.createRequests).toHaveLength(1);

  controller.setStatus(jobId, "success");
  await expect(history.getByText("完了")).toBeVisible();
  await expect(page.getByText("接続できません。状態を再取得します。")).toHaveCount(0);
  expect(controller.status(jobId)).toBe("success");
  expect(controller.createRequests).toHaveLength(1);
});

test("失敗したジョブの再試行は新しいジョブIDを作成する", async ({ page }) => {
  await addAuthCookie(page);
  const controller = await installDocsClipIngestMock(page, {
    initialStatus: "failed",
    retryStatus: "running",
  });
  await page.goto("/docs");

  await submitClip(page, "retryable failure job");
  const failedJobId = await waitForCreated(controller);
  const history = page.getByRole("dialog", { name: "クリップ取り込みの状況" });
  await expect(history.getByText("失敗")).toBeVisible();
  await history.getByRole("button").filter({ hasText: "失敗" }).click();

  const failureDialog = page.getByRole("dialog", { name: "取り込みに失敗しました" });
  await expect(failureDialog).toBeVisible();
  await failureDialog.getByRole("button", { name: "再試行" }).click();
  await expect.poll(() => controller.retryRequests.length).toBe(1);

  const retryRequest = controller.retryRequests[0];
  expect(retryRequest.sourceId).toBe(failedJobId);
  expect(retryRequest.id).not.toBe(failedJobId);
  expect(controller.createRequests).toHaveLength(1);
  expect(controller.status(retryRequest.id)).toBe("running");
  await expect(page.getByText("取り込み中", { exact: true })).toBeVisible();
});
