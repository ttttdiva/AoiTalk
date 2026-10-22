import type { Page, Route } from "@playwright/test";

/** Isolated API data only; no SNS account, credential, post or user Task is created. */
export const PERSONA_FIXTURE_NAMES = ["よぱにしゃっど！", "元気が2番！", "鬱戦", "刺されても死なない大きなクチバシ", "OUTDATED™", "どうせ嘘だろうが", "OFFBY1", "UN//REAL"];
export const personaFixtureRows = PERSONA_FIXTURE_NAMES.map((name, index) => ({
  id: `persona-${index}`, owner_user_id: "fixture-owner", project_id: `project-${index}`, state: "active",
  current_revision: { id: `persona-revision-${index}`, persona_id: `persona-${index}`, owner_user_id: "fixture-owner", project_id: `project-${index}`,
    display_name: name, summary: `${name}のfixture戦略`, audience: "この人格の読者", platforms: ["x", "youtube"], kpi_objectives: ["保存率"], content_hash: "b".repeat(64), version: 1 },
}));
export const personaFixtureAccounts = personaFixtureRows.flatMap((p, index) => ["x", "youtube"].map(platform => ({
  id: `account-${platform}-${index}`, persona_id: p.id, owner_user_id: p.owner_user_id, project_id: p.project_id, platform,
  account_ref: `fixture-${platform}-${index}`, connection_id: `fixture-connection-${platform}-${index}`, status: "active",
  current_revision: { id: `account-revision-${platform}-${index}`, platform_account_id: `account-${platform}-${index}`, owner_user_id: p.owner_user_id,
    project_id: p.project_id, display_name: `fixture ${platform}`, content_hash: "d".repeat(64), credential_status: "configured", publish_capability: "available", version: 1 },
})));
export const personaFixtureBriefs = personaFixtureRows.map((p, index) => ({ id: `brief-${index}`, editorial_program_id: `program-${index}`,
  owner_user_id: p.owner_user_id, project_id: p.project_id, persona_revision_id: p.current_revision.id,
  input_kind: "research", created_by: "fixture-owner",
  title: `${p.current_revision.display_name}の制作brief`, brief: "選択した人格の保存済みfixture企画です。", objective: "保存率を測る", status: "draft", content_type: "x_post", content_hash: "a".repeat(64), version: 1,
}));
export const personaFixtureVariants = personaFixtureAccounts.map((account, index) => {
  const personaIndex = Number(account.persona_id.replace("persona-", ""));
  return { id: `variant-${index}`, owner_user_id: account.owner_user_id, project_id: account.project_id,
    content_item_id: `brief-${personaIndex}`, platform: account.platform, status: "publishable",
    current_revision: { id: `variant-revision-${index}`, owner_user_id: account.owner_user_id, project_id: account.project_id,
      content_variant_id: `variant-${index}`, content_item_id: `brief-${personaIndex}`, content_item_hash: "a".repeat(64),
      persona_revision_id: `persona-revision-${personaIndex}`, persona_revision_hash: "b".repeat(64), platform_account_id: account.id,
      platform_account_revision_id: account.current_revision.id, platform_account_revision_hash: account.current_revision.content_hash,
      platform: account.platform, version: 1, content_hash: "c".repeat(64), generation_output_refs: [], source_evidence: [],
      payload: account.platform === "x" ? { type: "x_post", text: `${PERSONA_FIXTURE_NAMES[personaIndex]}の保存済みfixture本文`, media: [] }
        : { type: "youtube_video", title: `${PERSONA_FIXTURE_NAMES[personaIndex]}のfixture動画`, description: "保存済みの説明", media_asset: ["fixture-asset"], tags: [], visibility: "private", audience: "not_made_for_kids", disclosure: "ai_assisted" },
    },
  };
});

export async function mockPersonaWorkspace(page: Page, options: {
  empty?: boolean; disconnected?: boolean;
  override?: (route: Route, url: URL) => Promise<boolean>;
} = {}) {
  await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: { features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true } } }));
  await page.route("**/api/python-proxy/agent-organization", route => route.fulfill({ json: { success: true, nodes: [], edges: [] } }));
  await page.route("**/api/python-proxy/agent-delegations*", route => route.fulfill({ json: { success: true, delegations: [] } }));
  await page.route("**/api/python-proxy/operations/command-center*", route => {
    const projectId = new URL(route.request().url()).searchParams.get("project_id");
    const index = Number(projectId?.replace("project-", ""));
    return route.fulfill({ json: { agents: [{ id: `employee-${index}`, display_name: `${PERSONA_FIXTURE_NAMES[index]}の制作社員`, state: "active", job_title: "制作担当", persona_assignments: [{ persona_id: `persona-${index}`, assignment_role: "creator", state: "active" }] }], current_work: [{ id: `work-${index}`, project_id: projectId, task_title: "この人格の制作を進める", state: "running" }] } });
  });
  await page.route("**/api/python-proxy/operations/media/**", async route => {
    const url = new URL(route.request().url());
    url.pathname = url.pathname.slice(url.pathname.indexOf("/api/"));
    if (options.override && await options.override(route, url)) return;
    const path = url.pathname.replace("/api/python-proxy/operations/media", "");
    if (path === "/brand-portfolio/definition" || path === "/brand-portfolio") {
      const source = { sha256: "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd", filename: "SNS_v6_fixture.md" };
      const brands = PERSONA_FIXTURE_NAMES.map((name, index) => ({ brand_key: `brand-${index}`, display_name: name, channels: [{ platform: "x" }, { platform: "youtube" }] }));
      return route.fulfill({ json: path.endsWith("/definition") ? { source, brands } : { source, status: "ready", owner_user_id: "fixture-owner", brands: brands.map((definition, index) => ({ definition, project_id: `project-${index}`, persona: personaFixtureRows[index], channels: personaFixtureAccounts.filter(account => account.persona_id === `persona-${index}`).map(account => ({ platform: account.platform, account })), editorial_program_id: `program-${index}`, content_item_id: `brief-${index}` })) } });
    }
    if (path === "/personas") return route.fulfill({ json: options.empty ? [] : personaFixtureRows });
    if (path.startsWith("/personas/")) return route.fulfill({ json: personaFixtureRows.find(p => p.id === path.split("/")[2]) });
    if (path === "/platform-accounts") return route.fulfill({ json: options.disconnected ? [] : personaFixtureAccounts.filter(a => a.persona_id === url.searchParams.get("persona_id") && a.project_id === url.searchParams.get("project_id")) });
    if (path === "/editorial-programs") return route.fulfill({ json: personaFixtureRows.filter(p => p.project_id === url.searchParams.get("project_id")).map(p => ({ id: p.id.replace("persona-", "program-"), persona_id: p.id, owner_user_id: p.owner_user_id, project_id: p.project_id, state: "draft", current_revision: { name: "fixture制作計画" } })) });
    if (path === "/content-items") return route.fulfill({ json: personaFixtureBriefs.filter(item => item.project_id === url.searchParams.get("project_id")) });
    if (path === "/content-variants") return route.fulfill({ json: personaFixtureVariants.filter(item => item.project_id === url.searchParams.get("project_id")) });
    if (path.startsWith("/content-variants/")) {
      const variant = personaFixtureVariants.find(v => v.id === path.split("/")[2]);
      if (path.endsWith("/readiness")) return route.fulfill({ json: { content_variant_id: variant?.id, revision_id: variant?.current_revision.id, revision_hash: variant?.current_revision.content_hash, ready: true, publication_allowed: true, status: "publishable", blocking_reasons: [] } });
      return route.fulfill({ json: path.endsWith("/revisions") ? variant ? [variant.current_revision] : [] : variant });
    }
    if (["/generation-workspaces", "/creative-recipes", "/generation-plans"].includes(path)) return route.fulfill({ json: [] });
    return route.fulfill({ status: 503, json: { detail: "fixture route unavailable" } });
  });
}
