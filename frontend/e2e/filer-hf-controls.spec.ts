import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("HFファイラー操作", () => {
  let uploadRequests = 0;
  let localSearchRequests = 0;
  let hfSearchRequests = 0;
  let lastHfSearchPath: string | null = null;

  const imageBytes = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    "base64",
  );
  const hfRootEntries = [
    {
      path: "nested",
      name: "nested",
      type: "directory" as const,
    },
    {
      path: "root.png",
      name: "root.png",
      type: "file" as const,
      size: 128,
      lastModified: "2026-07-19T00:00:00Z",
    },
  ];
  const hfNestedEntries = [
    {
      path: "nested/first.png",
      name: "first.png",
      type: "file" as const,
      size: 128,
      lastModified: "2026-07-19T00:00:00Z",
    },
    {
      path: "nested/second.png",
      name: "second.png",
      type: "file" as const,
      size: 256,
      lastModified: "2026-07-19T00:00:00Z",
    },
  ];

  test.beforeEach(async ({ page }) => {
    uploadRequests = 0;
    localSearchRequests = 0;
    hfSearchRequests = 0;
    lastHfSearchPath = null;
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/explorer/search**", async (route) => {
      localSearchRequests += 1;
      await route.fulfill({
        json: {
          success: true,
          results: [],
          total: 0,
          query: new URL(route.request().url()).searchParams.get("q") ?? "",
        },
      });
    });
    await page.route("**/api/huggingface/**", async (route) => {
      const url = new URL(route.request().url());

      if (url.pathname === "/api/huggingface/accounts") {
        await route.fulfill({
          json: {
            accounts: [
              { id: "writer", username: "writer", label: "Writer", source: "HF_TOKEN_WRITER" },
            ],
            references: [
              { repoId: "public/demo", repoType: "dataset" },
            ],
          },
        });
        return;
      }

      if (url.pathname === "/api/huggingface/repos") {
        await route.fulfill({
          json: {
            accountId: "writer",
            username: "writer",
            repos: [
              {
                id: "writer/photos",
                name: "photos",
                owner: "writer",
                private: true,
                lastModified: "2026-07-19T00:00:00Z",
                type: "dataset",
              },
            ],
          },
        });
        return;
      }

      if (url.pathname === "/api/huggingface/tree") {
        const path = url.searchParams.get("path") ?? "";
        await route.fulfill({
          json: {
            repoId: url.searchParams.get("repoId"),
            repoType: url.searchParams.get("repoType"),
            path,
            entries: path === "nested" ? hfNestedEntries : hfRootEntries,
          },
        });
        return;
      }

      if (url.pathname === "/api/huggingface/search") {
        hfSearchRequests += 1;
        lastHfSearchPath = url.searchParams.get("path");
        const repoId = url.searchParams.get("repoId") ?? "writer/photos";
        const repoType = url.searchParams.get("repoType") ?? "dataset";
        const accountId = url.searchParams.get("accountId") ?? "writer";
        const prefix = `HF|${accountId}|${repoType}|${repoId}|`;
        await route.fulfill({
          json: {
            success: true,
            results: [
              {
                name: "nested",
                path: `${prefix}nested`,
                kind: "directory",
              },
              {
                name: "first.png",
                path: `${prefix}nested/first.png`,
                kind: "file",
                type: "image/png",
                extension: ".png",
                size_bytes: 128,
              },
              {
                name: "second.png",
                path: `${prefix}nested/second.png`,
                kind: "file",
                type: "image/png",
                extension: ".png",
                size_bytes: 256,
              },
            ],
            total: 3,
            total_returned: 3,
            root_path: `${prefix.slice(0, -1)}`,
            truncated: false,
            query: url.searchParams.get("q") ?? "",
          },
        });
        return;
      }

      if (url.pathname === "/api/huggingface/file") {
        await route.fulfill({
          status: 200,
          contentType: "image/png",
          body: imageBytes,
        });
        return;
      }

      if (url.pathname === "/api/huggingface/references") {
        await route.fulfill({
          json: {
            kind: "repository",
            repositories: [
              {
                repoId: "new/public",
                repoType: "dataset",
                path: "HF|~|dataset|new/public|",
              },
            ],
          },
        });
        return;
      }

      if (url.pathname === "/api/huggingface/upload") {
        uploadRequests += 1;
        await route.fulfill({
          json: { success: true, successCount: 1, failureCount: 0, failures: [] },
        });
        return;
      }

      await route.fallback();
    });
    await page.addInitScript(() => localStorage.setItem("filer-tab", "hf"));
  });

  test("ホームでHFルートへ戻り、参照追加は単一入力で行える", async ({ page }) => {
    await page.goto("/filer");

    const writableRepo = page.getByText("writer/photos (dataset)", { exact: true });
    await expect(writableRepo).toBeVisible();
    await writableRepo.dblclick();
    const uploadButton = page.getByTitle("現在のHFディレクトリへアップロード");
    await expect(uploadButton).toBeEnabled();
    const fileChooserPromise = page.waitForEvent("filechooser");
    await uploadButton.click();
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({
      name: "runtime-check.png",
      mimeType: "image/png",
      buffer: Buffer.from("mock-image"),
    });
    await expect.poll(() => uploadRequests).toBe(1);

    await page.getByTitle("ホーム").click();
    await expect(page.getByText("writer/photos (dataset)", { exact: true })).toBeVisible();
    await expect(page.getByTitle("HF参照を追加")).toBeVisible();

    await page.getByTitle("HF参照を追加").click();
    await expect(page.getByRole("dialog")).toContainText(
      "HFトークン、owner/repository、またはHugging Face URL",
    );
    await expect(page.getByRole("radio")).toHaveCount(0);

    const input = page.getByPlaceholder("hf_... または owner/repository");
    await expect(input).toHaveCount(1);
    await input.fill("new/public");
    await page.getByRole("button", { name: "追加" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test("nested HF検索結果からviewerを開き、trusted keyboardで移動とFullscreenへ到達する", async ({
    page,
  }) => {
    await page.goto("/filer");

    const writableRepo = page.getByText("writer/photos (dataset)", { exact: true });
    await expect(writableRepo).toBeVisible();

    // HF root search is bounded to the repositories already displayed; it
    // must not recurse through every repository or hit the local explorer API.
    await page.keyboard.press("Control+f");
    const rootSearch = page.getByRole("textbox", {
      name: "ファイル名・フォルダ名の検索",
    });
    await expect(rootSearch).toBeFocused();
    await rootSearch.fill("photos");
    await rootSearch.press("Enter");
    await expect(page.getByText("writer/photos (dataset)", { exact: true })).toBeVisible();
    await expect(page.getByText("public/demo (dataset)", { exact: true })).toHaveCount(0);
    await expect(page.getByText("1件", { exact: true })).toBeVisible();
    expect(hfSearchRequests).toBe(0);
    expect(localSearchRequests).toBe(0);
    await rootSearch.press("Escape");
    await expect(page.getByText("public/demo (dataset)", { exact: true })).toBeVisible();

    await writableRepo.dblclick();
    await expect(page.getByText("nested", { exact: true })).toBeVisible();
    await page.getByText("nested", { exact: true }).dblclick();
    await expect(page.getByText("first.png", { exact: true })).toBeVisible();

    await page.keyboard.press("Control+f");
    const searchInput = page.getByRole("textbox", {
      name: "ファイル名・フォルダ名の検索",
    });
    await expect(searchInput).toBeVisible();
    await searchInput.fill("png");
    await searchInput.press("Enter");
    await expect.poll(() => hfSearchRequests).toBe(1);
    expect(lastHfSearchPath).toBe("nested");
    await expect.poll(() => localSearchRequests).toBe(0);

    const firstResult = page.locator(
      '[data-explorer-item-path="HF|writer|dataset|writer/photos|nested/first.png"]',
    );
    const secondResult = page.locator(
      '[data-explorer-item-path="HF|writer|dataset|writer/photos|nested/second.png"]',
    );
    await expect(firstResult).toBeVisible();
    await expect(secondResult).toBeVisible();
    await firstResult.dblclick();

    await expect(page.getByRole("dialog", { name: "first.png" })).toBeVisible();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("dialog", { name: "second.png" })).toBeVisible();
    await page.keyboard.press("ArrowLeft");
    await expect(page.getByRole("dialog", { name: "first.png" })).toBeVisible();

    await page.evaluate("window.__filerFullscreenCalls = []; Object.defineProperty(HTMLElement.prototype, 'requestFullscreen', { configurable: true, value: function() { window.__filerFullscreenCalls.push(this.dataset.filesViewerMediaSurface === 'true' ? 'surface' : 'other'); return Promise.resolve(); } });");
    await page.keyboard.press("F");
    await expect.poll(() => page.evaluate("window.__filerFullscreenCalls || []")).toEqual(["surface"]);
  });
});
