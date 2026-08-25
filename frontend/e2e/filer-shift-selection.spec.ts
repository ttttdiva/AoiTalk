import { expect, test } from "@playwright/test";

import { addAuthCookie } from "./support/auth";

const project = {
  id: "project-1",
  name: "Filer Project",
  description: null,
  slug: "filer-project",
  color: "#2563eb",
  // ProjectContext keeps local Files roots selectable only within a Space.
  space_id: "space-1",
  is_completed: false,
};

const rootPath = `_projects/project_${project.id}`;
const paths = {
  folderA: `${rootPath}/folder-a`,
  folderB: `${rootPath}/folder-b`,
  fileC: `${rootPath}/file-c.txt`,
  fileD: `${rootPath}/file-d.txt`,
  fileE: `${rootPath}/file-e.txt`,
  executable: `${rootPath}/AoiTalk.exe`,
  archive: `${rootPath}/bundle.tar.gz`,
  externalRoot: "D:/Outside",
  externalFile: "D:/Outside/outside.txt",
};

type ExplorerOperation = {
  method: string;
  pathname: string;
  payload: unknown;
  phase?: "preflight" | "native";
  contentType?: string;
  preflightHeader?: string;
};

function parseExplorerOperationPayload(
  request: import("@playwright/test").Request,
  url: URL,
): unknown {
  if (request.method() === "GET") {
    return { path: url.searchParams.get("path") };
  }

  const postData = request.postData() || "";
  const contentType = request.headers()["content-type"] || "";
  if (
    contentType.toLowerCase().includes("application/json") ||
    postData.trimStart().startsWith("{")
  ) {
    try {
      return JSON.parse(postData);
    } catch {
      return postData;
    }
  }

  // The native multi-download fallback submits a hidden form. Its
  // application/x-www-form-urlencoded body carries the same JSON paths
  // value as the fetch preflight, so record the decoded payload uniformly.
  const form = new URLSearchParams(postData);
  const encodedPaths = form.get("paths");
  if (encodedPaths !== null) {
    try {
      return { paths: JSON.parse(encodedPaths) };
    } catch {
      return { paths: encodedPaths };
    }
  }
  return Object.fromEntries(form.entries());
}

async function mockFilerApis(
  page: import("@playwright/test").Page,
  operations: ExplorerOperation[] = [],
  options: {
    failDownloads?: boolean;
    listDelayAfterFirstMs?: number;
    externalBookmark?: boolean;
  } = {},
) {
  let listRequestCount = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: "user-1", username: "tester", role: "admin" },
        },
      });
      return;
    }

    if (url.pathname === "/api/projects") {
      await route.fulfill({ json: { projects: [project], total: 1 } });
      return;
    }

    if (url.pathname === "/api/spaces") {
      await route.fulfill({
        json: {
          spaces: [
            {
              id: "space-1",
              name: "Filer Space",
              slug: "filer-space",
              description: null,
              color: "#2563eb",
              owner_id: "user-1",
            },
          ],
          total: 1,
        },
      });
      return;
    }

    if (url.pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/huggingface/accounts") {
      await route.fulfill({
        json: {
          accounts: [],
          references: [],
        },
      });
      return;
    }

    if (url.pathname === `/api/projects/${project.id}/records`) {
      await route.fulfill({ json: { tables: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/explorer/list") {
      listRequestCount += 1;
      if (listRequestCount > 1 && options.listDelayAfterFirstMs) {
        await new Promise((resolve) =>
          setTimeout(resolve, options.listDelayAfterFirstMs),
        );
      }
      const requestedPath = url.searchParams.get("path") || rootPath;
      const isExternal = requestedPath === paths.externalRoot;
      await route.fulfill({
        json: {
          success: true,
          current_path: requestedPath,
          parent_path: isExternal
            ? "D:/"
            : requestedPath === rootPath
              ? null
              : rootPath,
          can_go_up: requestedPath !== rootPath,
          directories: isExternal
            ? []
            : [
                { name: "folder-a", path: paths.folderA, item_count: 0 },
                { name: "folder-b", path: paths.folderB, item_count: 0 },
              ],
          files: isExternal
            ? [
                {
                  name: "outside.txt",
                  path: paths.externalFile,
                  type: "text/plain",
                  size: 50,
                  extension: ".txt",
                },
              ]
            : [
                {
                  name: "file-c.txt",
                  path: paths.fileC,
                  type: "text/plain",
                  size: 10,
                  extension: ".txt",
                },
                {
                  name: "file-d.txt",
                  path: paths.fileD,
                  type: "text/plain",
                  size: 20,
                  extension: ".txt",
                },
                {
                  name: "file-e.txt",
                  path: paths.fileE,
                  type: "text/plain",
                  size: 30,
                  extension: ".txt",
                },
                {
                  name: "AoiTalk.exe",
                  path: paths.executable,
                  type: "application/x-msdownload",
                  size: 35,
                  extension: ".exe",
                },
                {
                  name: "bundle.tar.gz",
                  path: paths.archive,
                  type: "application/gzip",
                  size: 40,
                  extension: ".gz",
                },
              ],
          total_items: isExternal ? 1 : 7,
          is_admin_mode: isExternal,
        },
      });
      return;
    }

    if (url.pathname.endsWith("/explorer/bookmarks")) {
      await route.fulfill({
        json: {
          success: true,
          bookmarks: options.externalBookmark
            ? [{ id: "outside", name: "外部", path: paths.externalRoot }]
            : [],
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/storage/contexts") {
      await route.fulfill({
        json: {
          success: true,
          contexts: [],
          current_context: { type: "personal", id: "user-1" },
          is_admin: true,
        },
      });
      return;
    }

    if (url.pathname.endsWith("/explorer/launchers")) {
      await route.fulfill({
        json: {
          success: true,
          launchers: [],
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ json: { ok: true } });
      return;
    }

    if (
      url.pathname === "/api/python-proxy/explorer/download" ||
      url.pathname === "/api/python-proxy/explorer/archive" ||
      url.pathname === "/api/python-proxy/explorer/extract"
    ) {
      const request = route.request();
      const isDownload = url.pathname.endsWith("/download");
      const preflightHeader = request.headers()[
        "x-aoitalk-download-preflight"
      ];
      const payload = parseExplorerOperationPayload(request, url);
      operations.push({
        method: request.method(),
        pathname: url.pathname,
        payload,
        ...(isDownload
          ? {
              phase: preflightHeader === "1" ? "preflight" : "native",
              contentType: request.headers()["content-type"],
              preflightHeader,
            }
          : {}),
      });
      if (url.pathname.endsWith("/download")) {
        if (options.failDownloads) {
          await route.fulfill({
            status: 500,
            json: { detail: "テスト用ダウンロードエラー" },
          });
          return;
        }
        const payloadObject =
          typeof payload === "object" && payload !== null
            ? (payload as { paths?: string[]; path?: string })
            : {};
        const requestedPaths = Array.isArray(payloadObject.paths)
          ? payloadObject.paths
          : payloadObject.path
            ? [payloadObject.path]
            : [];
        const filename =
          requestedPaths.length > 1
            ? "archive.zip"
            : requestedPaths[0] === paths.folderA
              ? "folder-a.zip"
              : requestedPaths[0] === paths.executable
                ? "AoiTalk.exe"
                : "file-c.txt";
        const body = Buffer.from(
          filename === "AoiTalk.exe" ? "MZ-test" : "mock archive",
        );
        await route.fulfill({
          body,
          contentType:
            filename === "AoiTalk.exe"
              ? "application/x-msdownload"
              : "application/zip",
          headers: {
            "Content-Disposition": `attachment; filename="${filename}"`,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
          },
        });
      } else if (url.pathname.endsWith("/archive")) {
        await route.fulfill({
          json: { success: true, archive_name: "archive.zip" },
        });
      } else {
        await route.fulfill({
          json: {
            success: true,
            extracted: [{ archive_name: "bundle.tar.gz" }],
          },
        });
      }
      return;
    }

    await route.fulfill({ json: {} });
  });
}

function item(page: import("@playwright/test").Page, path: string) {
  return page.locator(`[data-explorer-item-path="${path}"]`);
}

test.describe("ファイラーの選択操作", () => {
  let runtimeErrors: string[];

  test.beforeEach(async ({ page }) => {
    runtimeErrors = [];
    page.on("pageerror", (error) => runtimeErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") runtimeErrors.push(message.text());
    });
    page.on("response", (response) => {
      if (response.status() >= 500) {
        runtimeErrors.push(`${response.status()} ${response.url()}`);
      }
    });
    await addAuthCookie(page);
    await page.addInitScript((projectId) => {
      localStorage.clear();
      localStorage.setItem("selectedProjectId", projectId);
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "grid");
    }, project.id);
    await mockFilerApis(page);
  });

  test("グリッド表示でアンカーからShiftクリック対象まで選択する", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(
      item(page, paths.fileD),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.folderB).click();
    await item(page, paths.fileD).click({ modifiers: ["Shift"] });

    await expect(item(page, paths.folderA)).not.toHaveClass(/bg-accent/);
    await expect(item(page, paths.folderB)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileC)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileD)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileE)).not.toHaveClass(/bg-accent/);
  });

  test("Ctrl+Hでプロジェクトルートへ戻る", async ({ page }) => {
    await page.goto("/filer");
    await expect(
      item(page, paths.folderA),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.folderA).click();
    const folderRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/python-proxy/explorer/list" &&
        url.searchParams.get("path") === paths.folderA
      );
    });
    await page.keyboard.press("Enter");
    await folderRequest;
    await expect(
      page.getByRole("button", { name: "上のフォルダへ" }),
    ).toBeVisible();

    const homeRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/python-proxy/explorer/list" &&
        url.searchParams.get("path") === rootPath
      );
    });
    await page.keyboard.press("Control+h");
    await homeRequest;

    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "上のフォルダへ" }),
    ).not.toBeVisible();
  });

  test("Ctrl+ArrowLeft/RightでFilesタブを循環切り替えする", async ({
    page,
  }) => {
    await page.goto("/filer");

    const projectTab = page.getByRole("button", {
      name: "Project Files",
      exact: true,
    });
    const userTab = page.getByRole("button", {
      name: "User Files",
      exact: true,
    });
    const hfTab = page.getByRole("button", {
      name: "HF",
      exact: true,
    });
    const hydrusTab = page.getByRole("button", {
      name: "Hydrus",
      exact: true,
    });

    await expect(
      item(page, paths.folderA),
      runtimeErrors.join("\n"),
    ).toBeVisible();
    await expect(projectTab).toHaveClass(/border-primary/);

    await page.keyboard.press("Control+ArrowRight");
    await expect(userTab).toHaveClass(/border-primary/);
    await expect(
      item(page, paths.folderA),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await page.keyboard.press("Control+ArrowRight");
    await expect(hfTab).toHaveClass(/border-primary/);
    await expect(page.getByText("HFリポジトリは空です。")).toBeVisible();

    await page.keyboard.press("Control+ArrowRight");
    await expect(hydrusTab).toHaveClass(/border-primary/);

    // right edge wraps Hydrus -> Project Files
    await page.keyboard.press("Control+ArrowRight");
    await expect(projectTab).toHaveClass(/border-primary/);
    await expect(
      item(page, paths.folderA),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    // left edge wraps Project Files -> Hydrus
    await page.keyboard.press("Control+ArrowLeft");
    await expect(hydrusTab).toHaveClass(/border-primary/);

    await page.keyboard.press("Control+ArrowLeft");
    await expect(hfTab).toHaveClass(/border-primary/);
    await expect(page.getByText("HFリポジトリは空です。")).toBeVisible();

    await page.keyboard.press("Control+ArrowLeft");
    await expect(userTab).toHaveClass(/border-primary/);
    await expect(
      item(page, paths.folderA),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await page.keyboard.press("Control+ArrowLeft");
    await expect(projectTab).toHaveClass(/border-primary/);

    expect(runtimeErrors).toEqual([]);
  });

  test("絶対パス閲覧中もCtrl+Hとホームボタンでプロジェクトルートへ戻る", async ({
    page,
  }) => {
    await page.unroute("**/api/**");
    await mockFilerApis(page, [], { externalBookmark: true });
    await page.goto("/filer");
    await expect(item(page, paths.fileC)).toBeVisible();

    await page.getByRole("button", { name: "外部", exact: true }).click();
    await expect(item(page, paths.externalFile)).toBeVisible();

    let homeRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/python-proxy/explorer/list" &&
        url.searchParams.get("path") === rootPath
      );
    });
    await page.keyboard.press("Control+h");
    await homeRequest;
    await expect(item(page, paths.fileC)).toBeVisible();

    await page.getByRole("button", { name: "外部", exact: true }).click();
    await expect(item(page, paths.externalFile)).toBeVisible();
    homeRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/python-proxy/explorer/list" &&
        url.searchParams.get("path") === rootPath
      );
    });
    await page.getByTitle("ホーム").click();
    await homeRequest;
    await expect(item(page, paths.fileC)).toBeVisible();
  });

  test("リスト表示ではソート後の表示順でShift範囲選択する", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(
      item(page, paths.fileD),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await page.getByTitle("リスト表示").click();
    await expect(page.getByTitle("グリッド表示")).toBeVisible();

    await item(page, paths.folderB).click();
    await item(page, paths.fileD).click({ modifiers: ["Shift"] });

    await expect(item(page, paths.folderA)).not.toHaveClass(/bg-accent/);
    await expect(item(page, paths.folderB)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileC)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileD)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileE)).not.toHaveClass(/bg-accent/);
  });

  test("Ctrl+Xした項目をグリッドとリストで薄く表示する", async ({ page }) => {
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.fileC).click();
    await page.keyboard.press("Control+x");

    await expect(item(page, paths.fileC)).toHaveCSS("opacity", "0.5");
    await expect(item(page, paths.fileD)).toHaveCSS("opacity", "1");

    await page.getByTitle("リスト表示").click();
    await expect(item(page, paths.fileC)).toHaveCSS("opacity", "0.5");

    await page.keyboard.press("Control+c");
    await expect(item(page, paths.fileC)).toHaveCSS("opacity", "1");
  });

  test("Ctrl+Shift+Lで単一ファイル・単一フォルダ・複数選択をダウンロードする", async ({
    page,
  }) => {
    const operations: ExplorerOperation[] = [];
    const downloadOperations = () =>
      operations.filter((operation) =>
        operation.pathname.endsWith("/download"),
      );
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.fileC).click();
    let downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+Shift+l");
    let download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("file-c.txt");
    await expect.poll(() => downloadOperations().length).toBe(2);
    expect(downloadOperations()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          method: "GET",
          payload: { path: paths.fileC },
          phase: "preflight",
          preflightHeader: "1",
        }),
        expect.objectContaining({
          method: "GET",
          payload: { path: paths.fileC },
          phase: "native",
        }),
      ]),
    );

    await item(page, paths.folderA).click();
    downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+Shift+l");
    download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("folder-a.zip");
    await expect.poll(() => downloadOperations().length).toBe(4);

    await item(page, paths.fileC).click({ modifiers: ["Control"] });
    downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+Shift+l");
    download = await downloadPromise;

    expect(download.suggestedFilename()).toBe("archive.zip");
    await expect.poll(() => downloadOperations().length).toBe(6);
    expect(downloadOperations()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          method: "GET",
          payload: { path: paths.folderA },
          phase: "preflight",
          preflightHeader: "1",
        }),
        expect.objectContaining({
          method: "GET",
          payload: { path: paths.folderA },
          phase: "native",
        }),
        expect.objectContaining({
          method: "POST",
          payload: { paths: [paths.folderA, paths.fileC] },
          phase: "preflight",
          preflightHeader: "1",
          contentType: expect.stringContaining("application/json"),
        }),
        expect.objectContaining({
          method: "POST",
          payload: { paths: [paths.folderA, paths.fileC] },
          phase: "native",
          contentType: expect.stringContaining(
            "application/x-www-form-urlencoded",
          ),
        }),
      ]),
    );
  });

  test("右クリックのダウンロードも同じGET canonical経路を使う", async ({
    page,
  }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(item(page, paths.fileC)).toBeVisible();

    await item(page, paths.fileC).click({ button: "right" });
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("menuitem", { name: /^ダウンロード/ }).click();
    const download = await downloadPromise;

    expect(download.suggestedFilename()).toBe("file-c.txt");
    expect(operations.filter((operation) => operation.pathname.endsWith("/download"))).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          method: "GET",
          payload: { path: paths.fileC },
          phase: "preflight",
          preflightHeader: "1",
        }),
        expect.objectContaining({
          method: "GET",
          payload: { path: paths.fileC },
          phase: "native",
        }),
      ]),
    );
  });

  test(".exe単体はuser gestureを保つ直接ダウンロードを開始する", async ({ page }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(item(page, paths.executable)).toBeVisible();

    await item(page, paths.executable).click();
    const downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+Shift+l");
    const download = await downloadPromise;

    expect(download.suggestedFilename()).toBe("AoiTalk.exe");
    await expect
      .poll(
        () =>
          operations.filter((operation) =>
            operation.pathname.endsWith("/download"),
          ).length,
      )
      .toBe(2);
  });

  test("絶対パスでも圧縮と作成UIを利用できる", async ({ page }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations, { externalBookmark: true });
    await page.goto("/filer");
    await page.getByRole("button", { name: "外部", exact: true }).click();
    await expect(item(page, paths.externalFile)).toBeVisible();

    await expect(page.getByTitle("新規フォルダ")).toBeVisible();
    await expect(page.getByTitle("アップロード")).toBeVisible();

    await item(page, paths.externalFile).click();
    await page.keyboard.press("Control+i");
    await expect.poll(() => operations.length).toBe(1);
    expect(operations[0]).toEqual({
      method: "POST",
      pathname: "/api/python-proxy/explorer/archive",
      payload: {
        paths: [paths.externalFile],
        dest: paths.externalRoot,
      },
    });

    await expect(item(page, paths.externalFile)).toBeVisible();
    await item(page, paths.externalFile).click({ button: "right" });
    await expect(
      page.getByRole("menuitem", { name: /^リネーム/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("menuitem", { name: /^コピー/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("menuitem", { name: /^切り取り/ }),
    ).toBeVisible();
    await page.getByRole("menuitem", { name: /^削除/ }).click();
    await expect(page.getByText("ファイルを完全に削除")).toBeVisible();
    await expect(page.getByText(/元に戻せません/)).toBeVisible();
    await page.getByRole("button", { name: "キャンセル" }).click();
  });

  test("Ctrl+IとCtrl+Uで選択対象を圧縮・展開APIへ渡す", async ({ page }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.folderA).click();
    await item(page, paths.fileC).click({ modifiers: ["Control"] });
    await page.keyboard.press("Control+i");
    await expect.poll(() => operations.length).toBe(1);
    expect(operations[0]).toEqual({
      method: "POST",
      pathname: "/api/python-proxy/explorer/archive",
      payload: { paths: [paths.folderA, paths.fileC], dest: rootPath },
    });

    await item(page, paths.archive).click();
    await page.keyboard.press("Control+u");
    await expect.poll(() => operations.length).toBe(2);
    expect(operations[1]).toEqual({
      method: "POST",
      pathname: "/api/python-proxy/explorer/extract",
      payload: { paths: [paths.archive], dest: rootPath },
    });
  });

  test("入力欄ではファイラーのCtrlショートカットを処理しない", async ({
    page,
  }) => {
    const operations: ExplorerOperation[] = [];
    let listRequestCount = 0;
    page.on("request", (request) => {
      if (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/list"
      ) {
        listRequestCount += 1;
      }
    });
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.fileC).click();
    await page.keyboard.press("Control+s");
    const searchInput = page.getByPlaceholder("ファイル名で絞り込み...");
    await expect(searchInput).toBeVisible();
    await searchInput.focus();
    const projectTab = page.getByRole("button", {
      name: "Project Files",
      exact: true,
    });
    await expect(projectTab).toHaveClass(/border-primary/);
    await page.keyboard.press("Control+ArrowRight");
    await expect(projectTab).toHaveClass(/border-primary/);
    await page.keyboard.press("Control+Shift+l");
    const listRequestCountBeforeHome = listRequestCount;
    await page.keyboard.press("Control+h");
    await page.waitForTimeout(100);
    expect(operations).toEqual([]);
    expect(listRequestCount).toBe(listRequestCountBeforeHome);
    await expect(searchInput).toBeFocused();
  });

  test("Ctrl+Shift+Lのダウンロード失敗を日本語で通知する", async ({ page }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations, { failDownloads: true });
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.fileC).click();
    await page.keyboard.press("Control+Shift+l");
    await expect(page.getByText(/ダウンロードに失敗しました/)).toBeVisible();
    expect(operations).toContainEqual(
      expect.objectContaining({
        method: "GET",
        pathname: "/api/python-proxy/explorer/download",
        payload: { path: paths.fileC },
        phase: "preflight",
        preflightHeader: "1",
      }),
    );
  });

  test("フォルダ読み込み中は旧選択へのファイル操作を発火しない", async ({
    page,
  }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations, { listDelayAfterFirstMs: 500 });
    await page.goto("/filer");
    await expect(
      item(page, paths.folderA),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.folderA).click();
    const navigationRequest = page.waitForRequest((request) =>
      request.url().includes("/api/python-proxy/explorer/list"),
    );
    await page.keyboard.press("Enter");
    await navigationRequest;
    await page.keyboard.press("Control+Shift+l");
    await page.keyboard.press("Control+i");
    await page.keyboard.press("Control+u");
    await page.waitForTimeout(100);
    expect(operations).toEqual([]);
  });

  test("ショートカットヘルプに圧縮・展開・ダウンロードを表示する", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await page.getByTitle("ショートカット一覧 (?)").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("選択中の項目をZIP圧縮");
    await expect(dialog).toContainText("選択中の圧縮ファイルを展開");
    await expect(dialog).toContainText("選択中の項目をダウンロード");
    await expect(dialog).toContainText("Ctrl+Shift+L");
    await expect(dialog).toContainText("現在のタブのホームへ移動");
    await expect(dialog).toContainText("Ctrl+H");
    await expect(dialog).toContainText(
      "Filesタブ（Project Files / User Files / HF / Hydrus）を切り替え",
    );
    await expect(dialog).toContainText("Ctrl+←/→");
  });
});
