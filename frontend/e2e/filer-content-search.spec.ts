import { expect, test, type Page, type Request } from "@playwright/test";

import { addAuthCookie } from "./support/auth";

// Files 検索パネルの「名前・本文の文字列」/「内容（意味）」モード、索引状況、
// 0件判定からの文字列検索への切替、結果からの「Filesで場所を開く」を検証する。

const project = {
  id: "project-1",
  name: "Filer Project",
  description: null,
  slug: "filer-project",
  color: "#2563eb",
  space_id: "space-1",
  is_completed: false,
};

const space = {
  id: "space-1",
  name: "Filer Space",
  slug: "filer-space",
  description: null,
  color: "#2563eb",
};

const rootPath = `_projects/project_${project.id}`;
const paths = {
  folderA: `${rootPath}/folder-a`,
  fileC: `${rootPath}/file-c.txt`,
  nestedA: `${rootPath}/folder-a/alpha.txt`,
  nestedPdf: `${rootPath}/folder-a/estimate.pdf`,
};

type SearchScenario = "results" | "empty-indexing";

const indexStatus = {
  enabled: true,
  semantic_available: true,
  queue_waiting: 3,
  scanner_running: false,
  scanner_error: null,
  last_scan_at: "2026-10-12T01:00:00Z",
  last_indexed_at: "2026-10-12T01:05:00Z",
  excluded_reasons: { "pattern:*.log": 2 },
  roots: [],
  counts: {
    total: 12,
    indexable: 10,
    ready: 6,
    pending: 3,
    running: 0,
    failed: 1,
    pending_hash: 0,
    excluded: 2,
    unsupported: 0,
  },
};

function semanticHit() {
  return {
    target_kind: "files",
    title: "estimate.pdf",
    text: "見積もり条件は前回と同じとする",
    score: 0.82,
    similarity: 0.82,
    modality: "text",
    locator: { kind: "workspace_file", id: "entry-1" },
    source_kind: "workspace_file",
    source_id: "entry-1",
    unit_id: "11111111-2222-4333-8444-555555555555",
    page_number: 4,
    files_location: {
      scope: "project",
      project_id: project.id,
      relative_path: "folder-a/estimate.pdf",
      path: paths.nestedPdf,
    },
    other_locations: [
      { target_kind: "media", source_kind: "chat_attachment", title: "estimate.pdf", locator: {} },
    ],
    citation: {},
    matched_query_sources: [],
  };
}

function listing(path: string) {
  if (path === paths.folderA) {
    return {
      success: true,
      current_path: paths.folderA,
      parent_path: rootPath,
      can_go_up: true,
      directories: [],
      files: [
        { name: "alpha.txt", path: paths.nestedA, type: "text/plain", size: 30, extension: ".txt" },
        { name: "estimate.pdf", path: paths.nestedPdf, type: "application/pdf", size: 4000, extension: ".pdf" },
      ],
      total_items: 2,
    };
  }
  return {
    success: true,
    current_path: rootPath,
    parent_path: null,
    can_go_up: false,
    directories: [{ name: "folder-a", path: paths.folderA, item_count: 2 }],
    files: [{ name: "file-c.txt", path: paths.fileC, type: "text/plain", size: 10, extension: ".txt" }],
    total_items: 2,
  };
}

async function mockApis(page: Page, scenario: { search: SearchScenario }) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({
        json: { authenticated: true, user: { id: "user-1", username: "tester", role: "user" } },
      });
      return;
    }
    if (url.pathname === "/api/projects") {
      await route.fulfill({ json: { projects: [project], total: 1 } });
      return;
    }
    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [space], total: 1 } });
      return;
    }
    if (url.pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/notifications" || url.pathname === "/api/tasks") {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === "/api/time-entries/active") {
      await route.fulfill({ json: null });
      return;
    }
    if (url.pathname === `/api/projects/${project.id}/records`) {
      await route.fulfill({ json: { tables: [] } });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/list") {
      await route.fulfill({ json: listing(url.searchParams.get("path") ?? rootPath) });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/search") {
      const content = url.searchParams.get("mode") === "content";
      await route.fulfill({
        json: {
          success: true,
          mode: content ? "content" : undefined,
          results: [
            {
              name: "alpha.txt",
              path: paths.nestedA,
              kind: "file",
              type: "text/plain",
              size_bytes: 30,
              extension: ".txt",
              ...(content
                ? { match_count: 2, matches: [{ line: 12, text: "見積もり条件は前回と同じ" }] }
                : {}),
            },
          ],
          total: 1,
          total_returned: 1,
          root_path: rootPath,
          truncated: false,
          limit_reason: null,
          skipped_large_files: 0,
          query: url.searchParams.get("q") ?? "",
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/search/files/status") {
      await route.fulfill({ json: indexStatus });
      return;
    }
    if (url.pathname === "/api/python-proxy/search/files/entries") {
      await route.fulfill({
        json: {
          total: 1,
          offset: 0,
          limit: 50,
          entries: [
            {
              id: "entry-failed",
              scope: "project",
              project_id: project.id,
              rel_path: "folder-a/broken.docx",
              name: "broken.docx",
              size_bytes: 100,
              status: "indexable",
              index_state: "failed",
              excluded_reason: null,
              error: "変換に失敗しました",
              last_scanned_at: null,
              files_location: {
                scope: "project",
                project_id: project.id,
                relative_path: "folder-a/broken.docx",
                path: `${paths.folderA}/broken.docx`,
              },
            },
          ],
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/search/files/settings") {
      await route.fulfill({
        json: {
          global_settings: {
            enabled: true,
            scopes: { user: true, project: true },
            include_roots: [],
            exclude_patterns: ["node_modules/", "*.log"],
            include_patterns: [],
            scope_exclude_patterns: { user: [], project: [] },
            max_file_bytes: 1,
            max_text_file_bytes: 1,
            max_concurrency: 1,
            scan_interval_seconds: 60,
            max_failed_attempts: 3,
            heavy_media_policy: { defer_bytes: 1, max_media_bytes: 1, max_duration_seconds: 1 },
          },
          user_override: {},
          project_id: project.id,
          project_override: {},
          can_edit_global: false,
          can_edit_project: false,
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/search/files/rescan" && method === "POST") {
      await route.fulfill({ json: { roots: 1, retried: 1, scanner_running: true } });
      return;
    }
    if (url.pathname === "/api/python-proxy/search/media" && method === "POST") {
      await route.fulfill({
        json: {
          version: 1,
          complete: true,
          warnings: [],
          weak_match: false,
          query_sources: [],
          lane_status: [{ target_kind: "files", status: "complete", partitions: [], warnings: [] }],
          results: scenario.search === "results" ? [semanticHit()] : [],
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/bookmarks") {
      await route.fulfill({ json: { success: true, bookmarks: [] } });
      return;
    }
    if (url.pathname === "/api/python-proxy/storage/contexts") {
      await route.fulfill({
        json: { success: true, contexts: [], current_context: { type: "personal", id: "user-1" }, is_admin: false },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ status: 503, json: { ok: false } });
      return;
    }
    await route.fulfill({ json: {} });
  });
}

function item(page: Page, path: string) {
  return page.locator(`[data-explorer-item-path="${path}"]`);
}

function isSemanticSearch(request: Request) {
  return new URL(request.url()).pathname === "/api/python-proxy/search/media" && request.method() === "POST";
}

async function openSearchPanel(page: Page) {
  await page.goto("/filer");
  await expect(item(page, paths.folderA)).toBeVisible();
  await page.locator('[data-shell-workspace="files"]').focus();
  await page.keyboard.press("Control+f");
  const panel = page.getByTestId("filer-search-panel");
  await expect(panel).toBeVisible();
  return panel;
}

test.describe("Files の内容検索と本文の文字列検索", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript((projectId) => {
      localStorage.clear();
      localStorage.setItem("selectedProjectId", projectId);
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "list");
    }, project.id);
  });

  test("本文の文字列検索は mode=content で検索し、一致行を表示する", async ({ page }) => {
    await mockApis(page, { search: "results" });
    const panel = await openSearchPanel(page);

    await panel.getByRole("radio", { name: "本文" }).click();
    const input = page.getByRole("textbox", { name: "本文の文字列検索" });
    await expect(input).toBeFocused();
    // 本文検索では名前の一括置換は出さない。
    await expect(page.getByRole("textbox", { name: "置換後の文字列" })).toHaveCount(0);

    const request = page.waitForRequest((req) => {
      const url = new URL(req.url());
      return (
        url.pathname === "/api/python-proxy/explorer/search" &&
        url.searchParams.get("mode") === "content" &&
        url.searchParams.get("q") === "見積もり" &&
        url.searchParams.get("root") === rootPath
      );
    });
    await input.fill("見積もり");
    await input.press("Enter");
    await request;

    await expect(item(page, paths.nestedA)).toBeVisible();
    const matches = page.getByTestId("filer-content-search-matches");
    await expect(matches).toContainText("12: 見積もり条件は前回と同じ");
    await expect(matches).toContainText("2箇所");
  });

  test("内容（意味）検索で索引状況と結果を表示し、Filesで場所を開くと該当ファイルを選択する", async ({ page }) => {
    await mockApis(page, { search: "results" });
    const panel = await openSearchPanel(page);

    await panel.getByRole("radio", { name: "内容（意味）" }).click();
    const input = page.getByRole("textbox", { name: "内容（意味）で検索" });
    await expect(input).toBeFocused();
    const status = page.getByTestId("files-index-status");
    await expect(status).toContainText("索引済み 60% (6/10件)");
    await expect(status).toContainText("失敗 1");
    await expect(status).toContainText("除外 2");

    const request = page.waitForRequest(isSemanticSearch);
    await input.fill("見積もりの条件");
    await input.press("Enter");
    const body = (await request).postDataJSON();
    expect(body.plan.targets).toEqual(["files"]);
    expect(body.plan.scope).toBe("project");
    expect(body.project_id).toBe(project.id);

    const results = page.getByTestId("files-semantic-results");
    await expect(results).toContainText("estimate.pdf");
    await expect(results).toContainText("/folder-a");
    await expect(results).toContainText("4ページ");
    await expect(results).toContainText("0.82");
    await expect(results).toContainText("同じ内容: チャット添付: estimate.pdf");
    // 索引中なので結果が不完全である旨を添える。
    await expect(results).toContainText("未索引のファイルは結果に含まれません");
    await expect(results.getByRole("link", { name: /原本を開く/ })).toHaveAttribute(
      "href",
      `/api/python-proxy/search/media/11111111-2222-4333-8444-555555555555/content?project_id=${project.id}#page=4`,
    );

    const navigation = page.waitForRequest((req) => {
      const url = new URL(req.url());
      return url.pathname === "/api/python-proxy/explorer/list" && url.searchParams.get("path") === paths.folderA;
    });
    await results.getByRole("button", { name: "Filesで場所を開く" }).click();
    await navigation;
    await expect(item(page, paths.nestedPdf)).toHaveClass(/border-primary|bg-primary/);
    // 結果は移動後も残り、次の候補を続けて開ける。
    await expect(page.getByTestId("files-semantic-results")).toContainText("estimate.pdf");
  });

  test("0件のときは索引中であることを示し、本文の文字列検索へ切り替えられる", async ({ page }) => {
    await mockApis(page, { search: "empty-indexing" });
    const panel = await openSearchPanel(page);
    await panel.getByRole("radio", { name: "内容（意味）" }).click();
    const input = page.getByRole("textbox", { name: "内容（意味）で検索" });
    await input.fill("議事録の結論");
    const request = page.waitForRequest(isSemanticSearch);
    await input.press("Enter");
    await request;

    const outcome = page.getByTestId("files-semantic-outcome");
    await expect(outcome).toHaveAttribute("data-outcome", "indexing");
    await expect(outcome).toContainText("索引済みの範囲では見つかりませんでした");
    await expect(outcome).toContainText("索引完了 60%");

    const fallback = page.waitForRequest((req) => {
      const url = new URL(req.url());
      return (
        url.pathname === "/api/python-proxy/explorer/search" &&
        url.searchParams.get("mode") === "content" &&
        url.searchParams.get("q") === "議事録の結論"
      );
    });
    await outcome.getByRole("button", { name: "名前・本文の文字列検索に切り替える" }).click();
    await fallback;
    await expect(panel.getByRole("radio", { name: "名前・本文の文字列" })).toHaveAttribute("aria-checked", "true");
    await expect(panel.getByRole("radio", { name: "本文" })).toHaveAttribute("aria-checked", "true");
    await expect(item(page, paths.nestedA)).toBeVisible();
  });

  test("索引の詳細で失敗一覧・再試行・権限のない設定を確認できる", async ({ page }) => {
    await mockApis(page, { search: "results" });
    const panel = await openSearchPanel(page);
    await panel.getByRole("radio", { name: "内容（意味）" }).click();
    await panel.getByRole("button", { name: /詳細・設定/ }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("内容検索の索引 — Project Files");
    await expect(page.getByTestId("files-index-details")).toContainText("除外パターン *.log: 2");

    const rescan = page.waitForRequest((req) => {
      return new URL(req.url()).pathname === "/api/python-proxy/search/files/rescan" && req.method() === "POST";
    });
    await dialog.getByRole("button", { name: "失敗を再試行（1件）" }).click();
    expect((await rescan).postDataJSON()).toEqual({ scope: "project", project_id: project.id, retry_failed: true });

    await dialog.getByRole("tab", { name: "失敗・除外" }).click();
    const entries = page.getByTestId("files-index-entries");
    await expect(entries).toContainText("broken.docx");
    await expect(entries).toContainText("変換に失敗しました");

    await dialog.getByRole("tab", { name: "設定" }).click();
    const settings = page.getByTestId("files-index-settings");
    await expect(settings).toContainText("編集権限がありません");
    await expect(settings).toContainText("管理者のみ編集できます");
    await expect(settings).toContainText("node_modules/、*.log");
    await expect(settings.getByRole("button", { name: "保存" })).toHaveCount(0);

    // Escape はダイアログだけを閉じ、検索パネルは残す。
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(panel).toBeVisible();
  });
});
