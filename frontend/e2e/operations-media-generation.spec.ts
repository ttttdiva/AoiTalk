import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("MediaOps Generation Studio", () => {
  test("loads a managed PNG through the canonical preview proxy and keeps it after reload", async ({ page }) => {
    const bytes = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN5kAAAAASUVORK5CYII=",
      "base64",
    );
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const outputUuid = "123e4567-e89b-12d3-a456-426614174000";
    const proxyPreview = `/api/python-proxy/operations/media/assets/${outputUuid}/preview?expected_sha256=${sha256}`;
    const owner = "fixture-owner";
    const output = {
      id: "output-row",
      external_asset_id: "ast_fixture1234",
      external_output_version: "outv_fixture1234",
      sha256,
      mime_type: "image/png",
      generation_kind: "image",
      width: 1,
      height: 1,
      preview_url: `/api/operations/media/assets/${outputUuid}/preview?expected_sha256=${sha256}`,
      deep_link: "https://provider.example.invalid/signed/output.png?token=secret",
    };
    const plan = {
      id: "plan-row",
      persona_revision_id: "persona-revision-row",
      creative_recipe_revision_id: "recipe-revision-row",
      workspace_id: "workspace-row",
      requested_outputs: 1,
      request_spec: {
        prompt: "fixture prompt",
        negative_prompt: "",
        seed: null,
        image_model_selection_id: "ims_fixture1234",
        generation_settings: {},
        size_preset_id: "normal_square",
        width: null,
        height: null,
        accept_metered_generation: false,
        aspect_ratio: null,
      },
      status: "succeeded",
    };
    const run = {
      id: "run-row",
      plan_id: plan.id,
      external_run_id: "run_fixture1234",
      external_workspace_id: "wsp_fixture1234",
      status: "succeeded",
      outputs: [output],
    };
    const previewRequests: string[] = [];

    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/operations/media/generation-workspaces*", route => route.fulfill({
      status: 200,
      json: [{
        id: "workspace-row",
        owner_user_id: owner,
        project_id: null,
        provider: "comfyui_workbench",
        external_workspace_id: "wsp_fixture1234",
        external_project_id: "prj_fixture1234",
        base_url: "https://studio.example.invalid",
        status: "configured",
      }],
    }));
    await page.route("**/api/python-proxy/operations/media/creative-recipes*", route => route.fulfill({
      status: 200,
      json: [{
        id: "recipe-row",
        owner_user_id: owner,
        project_id: null,
        persona_id: "persona-row",
        name: "Image recipe",
        current_revision: {
          id: "recipe-revision-row",
          creative_recipe_id: "recipe-row",
          persona_id: "persona-row",
          persona_revision_id: "persona-revision-row",
          version: 1,
          recipe_type: "image",
          image_model_selection_id: "ims_fixture1234",
          prompt_template: "{{prompt}}",
          reference_asset_ids: [],
          candidate_count: 1,
          content_hash: "a".repeat(64),
        },
      }],
    }));
    await page.route("**/api/python-proxy/operations/media/content-items*", route => route.fulfill({ status: 200, json: [] }));
    await page.route("**/api/python-proxy/operations/media/generation-plans*", route => route.fulfill({ status: 200, json: [plan] }));
    await page.route("**/api/python-proxy/operations/media/generation-runs*", route => route.fulfill({ status: 200, json: [run] }));
    await page.route("**/api/python-proxy/operations/connections*", route => route.fulfill({ status: 200, json: [] }));
    await page.route("**/api/python-proxy/operations/media/assets/*/preview?*", async route => {
      const url = new URL(route.request().url());
      previewRequests.push(`${url.pathname}${url.search}`);
      expect(url.pathname).toBe(`/api/python-proxy/operations/media/assets/${outputUuid}/preview`);
      expect(Array.from(url.searchParams.keys())).toEqual(["expected_sha256"]);
      expect(url.searchParams.get("expected_sha256")).toBe(sha256);
      await route.fulfill({ status: 200, contentType: "image/png", body: bytes });
    });

    await page.goto("/operations?tab=generation");
    const image = page.getByRole("img", { name: "Generated image preview" });
    await expect(image).toBeVisible();
    await expect(image).toHaveAttribute("src", proxyPreview);
    await expect.poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBe(1);
    await expect.poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalHeight)).toBe(1);
    expect(previewRequests.length).toBeGreaterThan(0);
    expect(previewRequests.every(request => request === proxyPreview)).toBe(true);

    await page.reload();
    const reloadedImage = page.getByRole("img", { name: "Generated image preview" });
    await expect(reloadedImage).toBeVisible();
    await expect(reloadedImage).toHaveAttribute("src", proxyPreview);
    await expect.poll(() => reloadedImage.evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBe(1);
    await expect.poll(() => reloadedImage.evaluate((element) => (element as HTMLImageElement).naturalHeight)).toBe(1);
    expect(previewRequests.every(request => request === proxyPreview)).toBe(true);
  });

  test("pins a selected ContentItem without sending research context and keeps saved context after reload", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    const hash = "a".repeat(64);
    const owner = "fixture-owner";
    const brief = "A01-T1: original fictional data; rights unknown; acceptance not_run";
    const context = { context_hash: hash, brief: { requirements: brief }, verification: { rights: "unknown", acceptance: "not_run", revenue: "unverified" } };
    const item = { id: "content-row", owner_user_id: owner, project_id: null, persona_revision_id: "persona-revision-row", title: "A01-T1", brief, version: 1, status: "draft" };
    let savedPlan: Record<string, unknown> | null = null;
    const submissions: Record<string, unknown>[] = [];
    await page.route("**/api/python-proxy/operations/media/generation-workspaces*", route => route.fulfill({ status: 200, json: [{
      id: "workspace-row", owner_user_id: owner, project_id: null, provider: "comfyui_workbench",
      external_workspace_id: "wsp_demo", external_project_id: "prj_demo", base_url: "https://studio.example.invalid", status: "configured",
    }] }));
    await page.route("**/api/python-proxy/operations/media/creative-recipes*", route => route.fulfill({ status: 200, json: [{
      id: "recipe-row", owner_user_id: owner, project_id: null, persona_id: "persona-row", name: "Image recipe",
      current_revision: { id: "recipe-revision-row", creative_recipe_id: "recipe-row", persona_id: "persona-row",
        persona_revision_id: "persona-revision-row", version: 1, recipe_type: "image", model_selection_id: "ims_fixture1234",
        prompt_template: "{{prompt}}", reference_asset_ids: [], candidate_count: 1, content_hash: hash },
    }] }));
    await page.route("**/api/python-proxy/operations/media/content-items*", route => route.fulfill({ status: 200, json: [item] }));
    await page.route("**/api/python-proxy/operations/media/generation-plans*", async route => {
      if (route.request().method() === "POST") {
        const submitted = route.request().postDataJSON() as Record<string, unknown>;
        submissions.push(submitted);
        savedPlan = { ...submitted, id: "plan-row", status: "draft", generation_kind: "image", plan_hash: hash,
          request_spec: { ...(submitted.request_spec as Record<string, unknown>), research_context: context } };
        await route.fulfill({ status: 200, json: savedPlan });
      } else {
        await route.fulfill({ status: 200, json: savedPlan ? [savedPlan] : [] });
      }
    });
    await page.route("**/api/python-proxy/operations/media/generation-runs*", route => route.fulfill({ status: 200, json: [] }));
    await page.route("**/api/python-proxy/operations/connections*", route => route.fulfill({ status: 200, json: [] }));
    await page.goto("/operations?tab=generation");
    await page.getByRole("combobox", { name: "Generation ContentItem", exact: true }).click();
    await page.getByRole("option", { name: "A01-T1 · v1", exact: true }).click();
    await page.getByLabel("GenerationPlan prompt", { exact: true }).fill("Create an original exercise diagram");
    await page.getByRole("button", { name: "GenerationPlanを作成", exact: true }).click();
    await expect(page.getByTestId("generation-research-context")).toContainText(hash);
    expect(submissions).toHaveLength(1);
    expect(submissions[0]).toMatchObject({ content_item_id: item.id });
    expect(submissions[0].request_spec).not.toHaveProperty("research_context");
    await page.getByTestId("generation-research-context").getByText("固定したbriefを確認", { exact: true }).click();
    await expect(page.getByTestId("generation-research-context")).toContainText(brief);
    await page.reload();
    await expect(page.getByTestId("generation-research-context")).toContainText(hash);
    await expect(page.getByTestId("generation-research-context")).toContainText(item.id);
  });

  test("pages past 100 incompatible ContentItems to select the matching item", async ({ page }, testInfo) => {
    const pageErrors: string[] = [];
    const consoleErrors: string[] = [];
    const failedResponses: { status: number; path: string }[] = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
    page.on("response", response => {
      if (response.status() >= 400) failedResponses.push({ status: response.status(), path: new URL(response.url()).pathname });
    });
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    const hash = "a".repeat(64);
    const owner = "fixture-owner";
    const brief = "A01-T1: original fictional data; rights unknown; acceptance not_run";
    const context = { context_hash: hash, brief: { requirements: brief }, verification: { rights: "unknown", acceptance: "not_run", revenue: "unverified" } };
    const item = { id: "content-row", owner_user_id: owner, project_id: null, persona_revision_id: "persona-revision-row", title: "A01-T1", brief, version: 1, status: "draft" };
    let savedPlan: Record<string, unknown> | null = null;
    const submissions: Record<string, unknown>[] = [];
    await page.route("**/api/python-proxy/operations/media/generation-workspaces*", route => route.fulfill({ status: 200, json: [{
      id: "workspace-row", owner_user_id: owner, project_id: null, provider: "comfyui_workbench",
      external_workspace_id: "wsp_demo", external_project_id: "prj_demo", base_url: "https://studio.example.invalid", status: "configured",
    }] }));
    await page.route("**/api/python-proxy/operations/media/creative-recipes*", route => route.fulfill({ status: 200, json: [{
      id: "recipe-row", owner_user_id: owner, project_id: null, persona_id: "persona-row", name: "Image recipe",
      current_revision: { id: "recipe-revision-row", creative_recipe_id: "recipe-row", persona_id: "persona-row",
        persona_revision_id: "persona-revision-row", version: 1, recipe_type: "image", model_selection_id: "ims_fixture1234",
        prompt_template: "{{prompt}}", reference_asset_ids: [], candidate_count: 1, content_hash: hash },
    }] }));
    const offsets: number[] = [];
    await page.route("**/api/python-proxy/operations/media/content-items*", route => {
      const query = new URL(route.request().url()).searchParams;
      expect(query.get("limit")).toBe("100");
      const offset = Number(query.get("offset") || 0);
      offsets.push(offset);
      return route.fulfill({ status: 200, json: offset === 0
        ? Array.from({ length: 100 }, (_, index) => ({ ...item, id: `foreign-${index}`, persona_revision_id: "another-revision", title: `Other scope ${index}` }))
        : offset === 100 ? [item,
          { ...item, id: "wrong-owner", owner_user_id: "another-owner", title: "Wrong owner" },
          { ...item, id: "wrong-project", project_id: "another-project", title: "Wrong project" },
          { ...item, id: "wrong-revision", persona_revision_id: "another-revision", title: "Wrong revision" },
        ] : [] });
    });
    await page.route("**/api/python-proxy/operations/media/generation-plans*", async route => {
      if (route.request().method() === "POST") {
        const submitted = route.request().postDataJSON() as Record<string, unknown>;
        submissions.push(submitted);
        savedPlan = { ...submitted, id: "plan-row", status: "draft", generation_kind: "image", plan_hash: hash,
          request_spec: { ...(submitted.request_spec as Record<string, unknown>), research_context: context } };
        await route.fulfill({ status: 200, json: savedPlan });
      } else {
        await route.fulfill({ status: 200, json: savedPlan ? [savedPlan] : [] });
      }
    });
    await page.route("**/api/python-proxy/operations/media/generation-runs*", route => route.fulfill({ status: 200, json: [] }));
    await page.route("**/api/python-proxy/operations/connections*", route => route.fulfill({ status: 200, json: [] }));
    await page.goto("/operations?tab=generation");
    await page.getByRole("combobox", { name: "Generation ContentItem", exact: true }).click();
    await expect(page.getByRole("option", { name: "A01-T1 · v1", exact: true })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "次のContentItem", exact: true }).click();
    await expect(page.getByText("ContentItem 2ページ目", { exact: true })).toBeVisible();
    await page.getByRole("combobox", { name: "Generation ContentItem", exact: true }).click();
    await expect(page.getByRole("option", { name: /^Wrong (owner|project|revision)/u })).toHaveCount(0);
    await page.getByRole("option", { name: "A01-T1 · v1", exact: true }).click();
    await page.getByLabel("GenerationPlan prompt", { exact: true }).fill("Create an original exercise diagram");
    await page.getByRole("button", { name: "GenerationPlanを作成", exact: true }).click();
    await expect(page.getByTestId("generation-research-context")).toContainText(hash);
    expect(submissions).toHaveLength(1);
    expect(submissions[0]).toMatchObject({ content_item_id: item.id });
    expect(submissions[0].request_spec).not.toHaveProperty("research_context");
    await page.getByTestId("generation-research-context").getByText("固定したbriefを確認", { exact: true }).click();
    await expect(page.getByTestId("generation-research-context")).toContainText(brief);
    expect(offsets).toContain(100);
    await page.getByRole("button", { name: "前のContentItem", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Generation ContentItem", exact: true })).toContainText("研究briefを指定しない");
    await page.reload();
    await expect(page.getByTestId("generation-research-context")).toContainText(hash);
    await expect(page.getByTestId("generation-research-context")).toContainText(item.id);
    expect(pageErrors).toEqual([]);
    expect(failedResponses.filter(response => response.path.includes("/operations/media/"))).toEqual([]);
    await testInfo.attach("pagination-browser-observations", { body: JSON.stringify({
      source: "private auth-only PG and mocked MediaOps HTTP responses",
      offsets, pageErrors, consoleErrors, failedResponses,
      ownerProjectRevisionFilterVerified: true, selectionClearedOnPrevious: true,
      contentItemSubmitted: item.id, contextInjection: false, contextHashReload: hash,
    }), contentType: "application/json" });
  });

  test("keeps Overview operations route and exposes safe Creative/Generation tab", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/operations/media/content-items*", route => route.fulfill({ status: 200, json: [] }));

    await page.route("**/api/python-proxy/operations/media/generation-workspaces*", async (route) => {
      await route.fulfill({
        status: 200,
        json: [{
          id: "workspace-row",
          provider: "comfyui_workbench",
          external_workspace_id: "wsp_fixture1234",
          external_project_id: "prj_fixture1234",
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
    await expect(page.getByText("wsp_fixture1234", { exact: true })).toBeVisible();

    const navigation = page.getByRole("navigation", { name: "Operationsワークスペース" });
    await expect(navigation.getByRole("link", { name: /^Generation /u })).toHaveAttribute("href", "/operations?tab=generation");
    await expect(navigation.getByRole("link", { name: /Personas/u })).toHaveAttribute("href", "/operations?tab=personas");

    await page.goto("/operations?tab=overview");
    await expect(page.getByTestId("operations-command-center")).toBeVisible();
    await expect(page.getByTestId("operations-media-generation-panel")).toHaveCount(0);
  });
});
