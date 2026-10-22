import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace } from "./support/persona-workspace-fixture";

async function registrationFixture(page: Page, loseFirstResponse = false) {
  const writes: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() !== "GET" &&
      new URL(request.url()).pathname.startsWith(
        "/api/python-proxy/operations/media/",
      )
    )
      writes.push(new URL(request.url()).pathname);
  });
  const requests: Array<{ body: Record<string, unknown>; key: string }> = [];
  let record: Record<string, unknown> | null = null;
  let inserts = 0;
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.route("**/api/auth/status", (route) =>
    route.fulfill({
      json: { authenticated: true, user: { id: "fixture-owner" } },
    }),
  );
  await mockPersonaWorkspace(page, {
    override: async (route, url) => {
      const path = url.pathname.replace(
        "/api/python-proxy/operations/media",
        "",
      );
      if (!path.startsWith("/generation-workspaces")) return false;
      if (route.request().method() === "POST") {
        const request = {
          body: route.request().postDataJSON() as Record<string, unknown>,
          key: route.request().headers()["idempotency-key"],
        };
        requests.push(request);
        expect(request.body).toMatchObject({
          project_id: "project-3",
          provider: "comfyui_workbench",
          external_workspace_id: "wsp_fixture01",
          external_project_id: null,
          base_url: "https://studio.example.invalid",
        });
        expect(request.body).not.toHaveProperty("owner_user_id");
        expect(request.key).toMatch(/^media-registration:/);
        if (!record) {
          inserts++;
          record = {
            ...request.body,
            id: "workspace-3",
            owner_user_id: "fixture-owner",
            config_hash: "a".repeat(64),
            status: "configured",
          };
        }
        if (loseFirstResponse && requests.length === 1)
          await route.fulfill({
            status: 503,
            json: { detail: "fixture response lost after persistence" },
          });
        else await route.fulfill({ json: record });
        return true;
      }
      await route.fulfill({
        json:
          path === "/generation-workspaces" ? (record ? [record] : []) : record,
      });
      return true;
    },
  });
  return { requests, writes, inserts: () => inserts };
}

async function workspacePreview(page: Page) {
  await page.goto("/operations?tab=studio&persona=persona-3&stage=generation");
  const panel = page.getByRole("region", { name: "Generation設定を登録" });
  await panel
    .getByRole("button", { name: "Workspaceを登録", exact: true })
    .click();
  await expect(panel.getByLabel("Generation Studioの接続先origin")).toHaveValue(
    "",
  );
  await panel
    .getByLabel("Generation Studioの接続先origin")
    .fill("https://studio.example.invalid");
  await panel
    .getByLabel("73 Workspace ID", { exact: true })
    .fill("wsp_fixture01");
  await panel
    .getByRole("button", { name: "登録内容を確認", exact: true })
    .click();
  await expect(panel.getByLabel("Generation登録内容の確認")).toContainText(
    "persona-revision-3",
  );
  return panel;
}

test("registration reload keeps the exact request/key and requires a fresh explicit retry", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const fixture = await registrationFixture(page, true);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const panel = await workspacePreview(page);
  const approval = panel.getByRole("checkbox", {
    name: "この所属・人格版と登録内容を確認しました",
  });
  const save = panel.getByRole("button", {
    name: "確認したWorkspaceを保存",
    exact: true,
  });
  await expect(save).toBeDisabled();
  expect(fixture.requests).toHaveLength(0);
  await approval.check();
  await save.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect(panel.getByRole("alert")).toContainText("fixture response lost");
  expect(fixture.requests).toHaveLength(1);
  await page.reload();
  const retry = panel.getByRole("button", {
    name: "同じ内容で登録を再試行",
    exact: true,
  });
  await expect(retry).toBeDisabled();
  expect(fixture.requests).toHaveLength(1);
  await expect(
    panel.getByLabel("73 Workspace ID", { exact: true }),
  ).toHaveCount(0);
  await approval.check();
  await retry.click();
  await expect(panel.getByRole("status")).toContainText(
    "登録と読み戻しを確認しました",
  );
  expect(fixture.requests).toHaveLength(2);
  expect(fixture.requests[1]).toEqual(fixture.requests[0]);
  expect(fixture.inserts()).toBe(1);
  expect(fixture.writes).toEqual(
    Array(2).fill("/api/python-proxy/operations/media/generation-workspaces"),
  );
  await expect(
    page.getByLabel("Generation Workspace", { exact: true }),
  ).toContainText("Workspaceを選択");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

test("changing an unsubmitted registration kind discards its preview and cancel creates nothing", async ({
  page,
}) => {
  const fixture = await registrationFixture(page);
  const panel = await workspacePreview(page);
  await panel
    .getByRole("button", { name: "Recipeを登録", exact: true })
    .click();
  await expect(panel.getByLabel("Generation登録内容の確認")).toHaveCount(0);
  await expect(panel.getByLabel("Recipe名")).toHaveValue("");
  await panel.getByLabel("Recipe名").fill("Original fixture recipe");
  await panel
    .getByLabel("Recipeのprompt template")
    .fill("Original illustration");
  await panel
    .getByRole("button", { name: "登録内容を確認", exact: true })
    .click();
  await expect(panel.getByLabel("Generation登録内容の確認")).toContainText(
    "Original fixture recipe",
  );
  await expect(
    panel.getByRole("button", { name: "確認したRecipeを保存", exact: true }),
  ).toBeDisabled();
  await panel
    .getByRole("button", { name: "登録を取り消す", exact: true })
    .click();
  await expect(panel.getByLabel("Generation登録内容の確認")).toHaveCount(0);
  expect(fixture.requests).toHaveLength(0);
  expect(fixture.inserts()).toBe(0);
  expect(fixture.writes).toEqual([]);
});
