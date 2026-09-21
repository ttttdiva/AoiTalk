import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("MediaOps Generation Studio", () => {
  test("keeps Overview operations route and exposes safe Creative/Generation tab", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);

    await page.route("**/api/python-proxy/operations/media/generation-workspaces*", async (route) => {
      await route.fulfill({
        status: 200,
        json: [{
          id: "workspace-row",
          provider: "comfyui_workbench",
          external_workspace_id: "wsp_demo",
          external_project_id: "prj_demo",
          base_url: "https://studio.example.invalid",
          status: "configured",
        }],
      });
    });
    await page.route("**/api/python-proxy/operations/media/creative-recipes*", async (route) => {
      await route.fulfill({
        status: 200,
        json: [{
          id: "recipe-row",
          persona_id: "persona-row",
          name: "Image recipe",
          current_revision: {
            id: "recipe-revision-row",
            creative_recipe_id: "recipe-row",
            persona_id: "persona-row",
            persona_revision_id: "persona-revision-row",
            version: 1,
            recipe_type: "image",
            prompt_template: "{{prompt}}",
            reference_asset_ids: [],
            candidate_count: 1,
            content_hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          },
        }],
      });
    });
    await page.route("**/api/python-proxy/operations/media/generation-plans*", async (route) => {
      await route.fulfill({ status: 200, json: [] });
    });
    await page.route("**/api/python-proxy/operations/media/generation-runs*", async (route) => {
      await route.fulfill({ status: 200, json: [] });
    });
    await page.route("**/api/python-proxy/operations/connections*", async (route) => {
      await route.fulfill({ status: 200, json: [] });
    });

    await page.goto("/operations?tab=generation");
    await expect(page.getByTestId("operations-media-generation-panel")).toBeVisible();
    await expect(page.getByText("wsp_demo")).toBeVisible();

    const navigation = page.getByRole("navigation", { name: "Operationsワークスペース" });
    await expect(navigation.getByRole("link", { name: /Creative \/ Generation/u })).toHaveAttribute("href", "/operations?tab=generation");
    await expect(navigation.getByRole("link", { name: /Personas/u })).toHaveAttribute("href", "/operations?tab=personas");

    await page.goto("/operations");
    await expect(page.getByTestId("operations-command-center")).toBeVisible();
    await expect(page.getByTestId("operations-media-generation-panel")).toHaveCount(0);
  });
});
