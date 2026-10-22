import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace, personaFixtureBriefs, personaFixtureRows, PERSONA_FIXTURE_NAMES } from "./support/persona-workspace-fixture";

test("an initial portfolio outage recovers on a read without duplicating setup", async ({ page }) => {
  let unavailable = true;
  const writes: string[] = [];
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  await mockPersonaWorkspace(page, { override: async (route, url) => {
    if (!url.pathname.endsWith("/brand-portfolio") || !unavailable) return false;
    await route.fulfill({ status: 503, json: { detail: "temporary outage" } }); return true;
  } });
  page.on("request", request => { if (request.method() !== "GET" && request.url().includes("/operations/media/")) writes.push(request.url()); });
  await page.goto("/operations?tab=studio");
  await expect(page.getByText("SNS v6の構成を取得できませんでした。構成を再取得してください。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "この8人格の初期構成を用意" })).toBeDisabled();
  unavailable = false;
  await page.getByRole("button", { name: "構成を再取得", exact: true }).click();
  const selection = page.getByRole("complementary", { name: "公開人格の選択" });
  await expect(selection.getByRole("link", { name: /UN\/\/REAL/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "ログイン所有者の制作状況" })).toContainText("8人格");
  expect(writes).toEqual([]);
});

test("a saved SNS portfolio under another owner is explained and cannot be duplicated", async ({ page }) => {
  const source = { sha256: "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd", filename: "SNS_v6_fixture.md" };
  const brands = PERSONA_FIXTURE_NAMES.map((display_name, index) => ({ brand_key: `brand-${index}`, display_name, channels: [{ platform: "x" }, { platform: "youtube" }] }));
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  await mockPersonaWorkspace(page, { override: async (route, url) => {
    const path = url.pathname.replace("/api/python-proxy/operations/media", "");
    if (path === "/brand-portfolio") {
      await route.fulfill({ json: { source, status: "not_configured", owner_user_id: "qa-owner", brands: brands.map(definition => ({ definition, project_id: null, persona: null, channels: [] })) } });
      return true;
    }
    if (path === "/personas") {
      await route.fulfill({ json: personaFixtureRows.map((persona, index) => ({ ...persona, parent_brand_ref: `sns-v6:brand-${index}`,
        current_revision: { ...persona.current_revision, research_policy: { source_sha256: source.sha256 } } })) });
      return true;
    }
    return false;
  } });
  const unexpected: string[] = [];
  page.on("request", request => {
    if (request.url().includes("/api/python-proxy/operations/media/") &&
      (request.method() !== "GET" || /\/personas\/|\/platform-accounts/.test(new URL(request.url()).pathname))) unexpected.push(request.url());
  });
  await page.goto("/operations?tab=studio&persona=persona-0");
  await expect(page.getByText("保存済みのSNS v6人格が別の所有者に8件あります。", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "この8人格の初期構成を用意" })).toBeDisabled();
  await expect(page.getByRole("heading", { name: PERSONA_FIXTURE_NAMES[0], exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("button", { name: "この8人格の初期構成を用意" })).toBeDisabled();
  expect(unexpected).toEqual([]);
});

for (const width of [1440, 390]) {
  test(`persona-first workspace keeps selected scope and saved preview at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 }); await addAuthCookie(page); await mockAuthenticatedApis(page); await mockPersonaWorkspace(page);
    const writes: string[] = []; page.on("request", request => { if (request.url().includes("/api/python-proxy/operations/") && request.method() !== "GET") writes.push(request.url()); });
    await page.goto("/operations");
    await expect(page.getByRole("heading", { name: "人格から制作する" })).toBeVisible();
    const selection = page.getByRole("complementary", { name: "公開人格の選択" });
    for (const name of PERSONA_FIXTURE_NAMES) await expect(selection.getByRole("link", { name: new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) })).toBeVisible();
    await selection.getByRole("link", { name: /刺されても死なない大きなクチバシ/ }).focus(); await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "刺されても死なない大きなクチバシ", exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "担当Agentと仕事" })).toContainText("刺されても死なない大きなクチバシの制作社員");
    await expect(page.getByRole("link", { name: "この人格の制作を進める" })).toHaveAttribute("href", /work_id=work-3/);
    await page.getByRole("button", { name: /3\. QA・媒体別内容/ }).click();
    const preview = page.getByRole("region", { name: "保存済みコンテンツのプレビュー" });
    await expect(preview).toContainText("刺されても死なない大きなクチバシの保存済みfixture本文");
    await page.getByRole("button", { name: /YouTube/ }).click(); await expect(preview).toContainText("公開設定: private");
    await selection.getByRole("link", { name: /UN\/\/REAL/ }).click();
    await expect(page.getByRole("heading", { name: "UN//REAL", exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "担当Agentと仕事" })).toContainText("UN//REALの制作社員");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(writes).toEqual([]);
  });
}

test("a persona list failure has one error and never requests a fallback account", async ({ page }) => {
  await addAuthCookie(page); await mockAuthenticatedApis(page); await mockPersonaWorkspace(page, { override: async (route, url) => {
    if (!url.pathname.endsWith("/personas")) return false;
    await route.fulfill({ status: 503, json: { detail: "unavailable fixture" } }); return true;
  } });
  const accounts: string[] = []; page.on("request", request => { if (request.url().includes("platform-accounts")) accounts.push(request.url()); });
  await page.goto("/operations?tab=studio&persona=persona-3");
  const workspace = page.getByTestId("persona-workspace");
  await expect(workspace.getByRole("alert")).toHaveCount(1); await expect(workspace.getByRole("alert")).toContainText("公開人格を取得できませんでした");
  expect(accounts).toEqual([]);
});

for (const width of [1440, 390]) {
  test(`an original user brief survives save and reload and pins a draft Plan at ${width}px`, async ({ page }) => {
    const rawBrief = "  架空の製品AとBでオリジナル画像を作る。\n\n確認済み調査とは扱わない。  \n";
    let saved: Record<string, unknown> | undefined; let plan: Record<string, unknown> | undefined;
    let briefWrites = 0; let planWrites = 0; const otherWrites: string[] = [];
    const workspace = { id: "workspace-3", owner_user_id: "fixture-owner", project_id: "project-3", provider: "comfyui_workbench", external_workspace_id: "wsp_fixture1234", status: "configured" };
    const recipe = { id: "recipe-3", owner_user_id: "fixture-owner", project_id: "project-3", persona_id: "persona-3", name: "fixture画像の制作方法", current_revision: { id: "recipe-revision-3", version: 1, persona_revision_id: "persona-revision-3", recipe_type: "image", image_model_selection_id: "ims_fixture1234", candidate_count: 1 } };
    await page.setViewportSize({ width, height: 900 }); await addAuthCookie(page); await mockAuthenticatedApis(page);
    await mockPersonaWorkspace(page, { override: async (route, url) => {
      const path = url.pathname.replace("/api/python-proxy/operations/media", ""); const method = route.request().method();
      if (path === "/content-items" && method === "POST") {
        const body = route.request().postDataJSON(); briefWrites++;
        expect(body).toMatchObject({ editorial_program_id: "program-3", persona_revision_id: "persona-revision-3", input_kind: "user_authored", brief: rawBrief });
        expect(body).not.toHaveProperty("finding_ids"); expect(body).not.toHaveProperty("candidate_ids"); expect(route.request().headers()["idempotency-key"]).toBeTruthy();
        saved = { ...personaFixtureBriefs[3], ...body, id: "user-brief-3", owner_user_id: "fixture-owner", project_id: "project-3", created_by: "fixture-owner", content_hash: "f".repeat(64), source_finding_ids: [], source_candidate_ids: [] };
        await route.fulfill({ json: saved }); return true;
      }
      if (path === "/content-items" && method === "GET") { await route.fulfill({ json: [...personaFixtureBriefs.filter(item => item.project_id === "project-3"), ...(saved ? [saved] : [])] }); return true; }
      if (path === "/generation-workspaces") { await route.fulfill({ json: [workspace] }); return true; }
      if (path === "/creative-recipes") { await route.fulfill({ json: [recipe] }); return true; }
      if (path === "/generation-plans" && method === "POST") {
        const body = route.request().postDataJSON(); planWrites++;
        expect(body).toMatchObject({ content_item_id: "user-brief-3", persona_revision_id: "persona-revision-3", creative_recipe_revision_id: "recipe-revision-3", workspace_id: "workspace-3" });
        expect(body.request_spec).not.toHaveProperty("research_context");
        plan = { ...body, id: "user-plan-3", owner_user_id: "fixture-owner", project_id: "project-3", status: "draft", plan_hash: "9".repeat(64), request_spec: { ...body.request_spec, research_context: { context_hash: "7".repeat(64), content_item_id: "user-brief-3", content_item_hash: saved?.content_hash, content_item_version: 1, owner_user_id: "fixture-owner", project_id: "project-3", persona_id: "persona-3", brief: { requirements: rawBrief, input_kind: "user_authored", created_by: "fixture-owner", persona_revision_id: "persona-revision-3" }, research: { findings: [], candidates: [] } } } };
        await route.fulfill({ json: plan }); return true;
      }
      if (path === "/generation-plans") { await route.fulfill({ json: plan ? [plan] : [] }); return true; }
      if (path === "/generation-plans/user-plan-3/runs") { await route.fulfill({ json: [] }); return true; }
      return false;
    } });
    page.on("request", request => { if (request.method() !== "GET" && request.url().includes("/api/python-proxy/") && !/\/(content-items|generation-plans)$/.test(new URL(request.url()).pathname)) otherWrites.push(request.url()); });
    await page.goto("/operations?tab=studio&persona=persona-3");
    await page.getByLabel("制作計画", { exact: true }).click(); await page.getByRole("option", { name: "fixture制作計画" }).click();
    await page.getByLabel("企画名").fill("本人のオリジナル制作"); await page.getByRole("textbox", { name: "制作brief" }).fill(rawBrief);
    await page.getByRole("button", { name: "制作briefを保存" }).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
    const savedItem = page.getByRole("button", { name: /本人のオリジナル制作/ }); await expect(savedItem).toContainText("本人入力");
    await expect(savedItem).toHaveAttribute("aria-pressed", "true"); await expect(page.getByRole("button", { name: /刺されても死なない大きなクチバシの制作brief/ })).toContainText("研究から採用");
    expect(briefWrites).toBe(1); await page.reload(); await expect(savedItem).toBeVisible(); expect(briefWrites).toBe(1);
    await page.getByRole("button", { name: "このbriefで計画・生成へ" }).click(); await expect(page).toHaveURL(/stage=generation.*content=user-brief-3/);
    await page.getByLabel("Generation Workspace", { exact: true }).click(); await page.getByRole("option", { name: /wsp_fixture1234/ }).click();
    await page.getByLabel("Creative Recipe", { exact: true }).click(); await page.getByRole("option", { name: "image · v1" }).click();
    await expect(page.getByLabel("Generation ContentItem")).toContainText("本人のオリジナル制作");
    await page.getByLabel("GenerationPlan prompt", { exact: true }).fill("Original illustration"); await page.getByRole("button", { name: "GenerationPlanを作成", exact: true }).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
    const context = page.getByTestId("generation-research-context"); await expect(context).toContainText("本人入力の創作briefを固定");
    await context.getByText("固定したbriefを確認").click(); expect(await context.locator("details p").textContent()).toBe(rawBrief);
    await page.reload(); await page.getByLabel("Generation Workspace", { exact: true }).click(); await page.getByRole("option", { name: /wsp_fixture1234/ }).click();
    await page.getByRole("button", { name: /user-plan-3/ }).click(); await expect(context).toContainText("本人入力の創作briefを固定");
    expect(briefWrites).toBe(1); expect(planWrites).toBe(1); expect(otherWrites).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}


test("authenticated binary image registration pins bytes and hands its saved output to QA", async ({ page }) => {
  const bytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN5kAAAAASUVORK5CYII=", "base64");
  const sha = createHash("sha256").update(bytes).digest("hex");
  const receipt = { asset_revision_id: "asset-revision", generation_output_id: "registered-output", persona_id: "persona-3", content_item_id: "brief-3", owner_user_id: "fixture-owner", project_id: "project-3", source_kind: "upload", source_id: null, sha256: sha, mime_type: "image/png", size_bytes: bytes.length, revision_hash: "e".repeat(64), preview_url: "https://untrusted.test/unused", created_at: "2026-10-07T05:00:00Z" };
  let writes = 0; let registered = false;
  await page.setViewportSize({ width: 390, height: 844 }); await addAuthCookie(page); await mockAuthenticatedApis(page);
  await mockPersonaWorkspace(page, { override: async (route, url) => {
    const path = url.pathname.replace("/api/python-proxy/operations/media", "");
    if (path === "/content-items/brief-3") { await route.fulfill({ json: { id: "brief-3", title: "刺されても死なない大きなクチバシの制作brief", owner_user_id: "fixture-owner", project_id: "project-3", persona_revision_id: "persona-revision-3", content_hash: "a".repeat(64) } }); return true; }
    if (path === "/assets/capabilities") { await route.fulfill({ json: { image_max_bytes: 25 * 1024 * 1024, video_max_bytes: 512 * 1024 * 1024, supported_mime_types: ["image/png", "video/webm"], upload_transport: "binary_stream" } }); return true; }
    if (path === "/asset-sources") { await route.fulfill({ json: [] }); return true; }
    if (path === "/assets") { await route.fulfill({ json: registered ? [receipt] : [] }); return true; }
    if (path === "/assets/upload") {
      writes++; expect(route.request().postDataBuffer()).toEqual(bytes); expect(route.request().headers()["x-asset-sha256"]).toBe(sha);
      expect(url.searchParams.get("persona_id")).toBe("persona-3"); expect(url.searchParams.get("expected_content_item_hash")).toBe("a".repeat(64));
      registered = true; await route.fulfill({ json: receipt }); return true;
    }
    if (path === "/assets/registered-output") { await route.fulfill({ json: receipt }); return true; }
    if (path === "/assets/registered-output/preview") { expect(url.searchParams.get("expected_sha256")).toBe(sha); await route.fulfill({ contentType: "image/png", body: bytes }); return true; }
    return false;
  } });
  await page.goto("/operations?tab=studio&persona=persona-3&stage=generation&creation=existing&content=brief-3");
  await page.getByLabel("登録する画像・動画").setInputFiles({ name: "fixture.png", mimeType: "image/png", buffer: bytes });
  await page.getByRole("button", { name: "このファイルを登録", exact: true }).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  const image = page.getByRole("img", { name: "登録した制作画像" }); await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  expect(writes).toBe(1); await page.reload(); await expect(page.getByRole("heading", { name: "既存の画像・動画を使う" })).toBeVisible(); expect(writes).toBe(1);
  await page.getByRole("button", { name: /image\/png.*registered-output/ }).click();
  await page.getByRole("button", { name: "この制作物でQA・媒体別内容へ" }).click();
  await expect(page).toHaveURL(/stage=qa.*output=registered-output/);
  await expect(page.getByLabel("添付Asset ID（1行1件）")).toHaveValue("registered-output");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true); expect(writes).toBe(1);
});


test("offline asset registration shows safe storage guidance and keeps permission denial", async ({ page }) => {
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  let status = 503; let writes = 0;
  await mockPersonaWorkspace(page, { override: async (route, url) => {
    const path = url.pathname.replace("/api/python-proxy/operations/media", "");
    if (path === "/content-items/brief-3") { await route.fulfill({ json: personaFixtureBriefs[3] }); return true; }
    if (path === "/assets/capabilities") { await route.fulfill({ json: { image_max_bytes: 25 * 1024 * 1024, video_max_bytes: 512 * 1024 * 1024, supported_mime_types: ["image/png"], upload_transport: "binary_stream" } }); return true; }
    if (path === "/assets") { await route.fulfill({ json: [] }); return true; }
    if (path === "/asset-sources") { await route.fulfill({ json: [{ source_kind: "library_file", source_id: "offline-file", mime_type: "image/png", size_bytes: 100, sha256: null }] }); return true; }
    if (path === "/assets/register") {
      writes++;
      await route.fulfill({ status, json: { detail: { category: "storage_unavailable", code: "storage_unavailable", message: "endpoint=https://private.test/token-secret path=/private/mount" } } });
      return true;
    }
    return false;
  } });
  await page.goto("/operations?tab=studio&persona=persona-3&stage=generation&creation=existing&content=brief-3");
  const assets = page.getByRole("region", { name: "既存制作物の登録" });
  await page.getByRole("combobox", { name: "登録する既存制作物" }).click();
  await page.getByRole("option", { name: /保存済みファイル.*offline-file/ }).click();
  await page.getByRole("button", { name: /選択した制作物を登録|同じ制作物の登録を明示再開/ }).click();
  await expect(assets.getByRole("alert")).toContainText("素材の保存先を利用できません。接続状態と登録記録を確認してください。");
  await expect(assets.getByRole("alert")).not.toContainText("token-secret");
  await expect(assets.getByRole("alert")).not.toContainText("/private/mount");
  expect(writes).toBe(1);
  status = 403;
  await page.reload();
  await page.getByRole("combobox", { name: "登録する既存制作物" }).click();
  await page.getByRole("option", { name: /保存済みファイル.*offline-file/ }).click();
  await page.getByRole("button", { name: /選択した制作物を登録|同じ制作物の登録を明示再開/ }).click();
  await expect(assets.getByRole("alert")).toContainText("管理者権限、所属範囲または実行環境の許可がありません。");
  await expect(assets.getByRole("alert")).not.toContainText("token-secret");
  expect(writes).toBe(2);
});
