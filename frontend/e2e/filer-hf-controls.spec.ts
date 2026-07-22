import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("HFファイラー操作", () => {
  let uploadRequests = 0;

  test.beforeEach(async ({ page }) => {
    uploadRequests = 0;
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
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
        await route.fulfill({
          json: {
            repoId: url.searchParams.get("repoId"),
            repoType: url.searchParams.get("repoType"),
            path: "",
            entries: [],
          },
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
});
