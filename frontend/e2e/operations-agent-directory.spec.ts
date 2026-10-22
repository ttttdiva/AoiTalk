import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace } from "./support/persona-workspace-fixture";

const agents = Array.from({ length: 60 }, (_, index) => ({
  id: `directory-${index}`, slug: `research-${index}`, display_name: `調査担当 ${index}`, state: "active", thumbnail_artifact_id: null as string | null,
}));

for (const { width, height } of [{ width: 1600, height: 900 }, { width: 390, height: 900 }, { width: 844, height: 390 }]) {
  test(`fills the Agent gallery and restores browsing after detail at ${width}x${height}`, async ({ page }) => {
    agents.forEach(agent => { agent.thumbnail_artifact_id = null; });
    await page.setViewportSize({ width, height });
    await addAuthCookie(page); await mockAuthenticatedApis(page); await mockPersonaWorkspace(page);
    const writes: string[] = [];
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      if (request.url().includes("/agents") && !["GET", "HEAD", "OPTIONS"].includes(request.method())) writes.push(request.url());
    });
    await page.route("**/api/python-proxy/agents**", route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/agents")) return route.fulfill({ json: { agents } });
      if (path.endsWith("/catalog")) return route.fulfill({ json: { can_manage: true, teams: [], capabilities: [] } });
      if (path.endsWith("/revisions")) return route.fulfill({ json: { revisions: [] } });
      if (path.endsWith("/effective-authority")) return route.fulfill({ json: { authority: { allowed: false, reason_code: "revision_missing" } } });
      if (path.endsWith("/organization-profile")) return route.fulfill({ json: { organization_profile: {} } });
      if (path.endsWith("/thumbnail")) {
        const agent = agents.find(row => path.includes(`/${row.id}/`))!;
        agent.thumbnail_artifact_id = route.request().postDataJSON().artifact_id;
        return route.fulfill({ json: { agent_id: agent.id, artifact_id: agent.thumbnail_artifact_id } });
      }
      const agent = agents.find(row => path.endsWith(`/${row.id}`));
      return agent ? route.fulfill({ json: { agent } }) : route.fallback();
    });
    await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: {
      summary: { working: 0, awaiting_approval: 0, uncertain: 0 },
      agents: agents.map(agent => ({ ...agent, job_title: `資料整理 ${agent.id}` })), current_work: [],
    } }));
    await page.route("**/api/python-proxy/operations/artifacts**", route => {
      if (route.request().method() === "POST") return route.fulfill({ json: { id: "thumbnail-upload" } });
      return route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64") });
    });
    await page.goto("/operations?tab=agents");
    const directory = page.getByRole("region", { name: "Agent一覧", exact: true });
    const results = directory.locator('[role="region"][aria-label="Agentの検索結果"]');
    const search = directory.getByRole("searchbox", { name: "Agentを検索" });
    const cards = results.locator('button[aria-pressed]');
    await expect(cards).toHaveCount(60);
    const bounds = await results.evaluate(element => ({ height: element.clientHeight, scroll: element.scrollHeight }));
    expect(bounds.height).toBeLessThan(height);
    expect(bounds.height).toBeGreaterThanOrEqual(120);
    expect(bounds.scroll).toBeGreaterThan(bounds.height);
    const tiles = await cards.evaluateAll(elements => elements.slice(0, 2).map(element => ({ left: element.getBoundingClientRect().left, top: element.getBoundingClientRect().top })));
    expect(tiles[1].left).toBeGreaterThan(tiles[0].left);
    expect(tiles[1].top).toBe(tiles[0].top);
    expect(await directory.evaluate(element => Math.abs(element.getBoundingClientRect().right - element.parentElement!.getBoundingClientRect().right))).toBeLessThan(2);

    await results.evaluate(element => { element.scrollTop = element.scrollHeight; });
    const last = cards.filter({ hasText: "調査担当 59" });
    await last.scrollIntoViewIfNeeded();
    const position = await results.evaluate(element => element.scrollTop);
    await last.click();
    await expect(page).toHaveURL(/agent=directory-59/);
    await expect(page.getByRole("region", { name: "Agent詳細", exact: true })).toBeVisible();
    await expect(directory).toBeHidden();
    await expect(page.getByText(/実効権限:|agent_inactive|Agent ID:/)).toHaveCount(0);
    await page.getByRole("button", { name: "職務・モデル", exact: true }).click();
    await expect(page).toHaveURL(/panel=role/);
    const dialog = page.getByRole("dialog", { name: "Agent詳細", exact: true });
    await expect(page.getByLabel("ミッション", { exact: true })).toBeVisible();
    expect(await dialog.evaluate(element => {
      const bounds = element.getBoundingClientRect();
      return [...element.querySelectorAll("input,textarea")].every(input => {
        const rect = input.getBoundingClientRect();
        return !rect.width || (rect.left >= bounds.left && rect.right <= bounds.right);
      });
    })).toBe(true);
    await page.getByRole("button", { name: "一覧に戻る", exact: true }).click();
    await expect(directory).toBeVisible();
    await expect(last).toHaveAttribute("aria-pressed", "true");
    await expect(last).toBeInViewport();
    expect(await results.evaluate(element => element.scrollTop)).toBe(position);

    await search.fill("ＲＥＳＥＡＲＣＨ－５９");
    await expect(cards).toHaveCount(1);
    await expect(last).toHaveAttribute("aria-pressed", "true");
    await search.fill("存在しないAgent");
    await expect(results.getByText("一致するAgentはありません。")).toBeVisible();
    await expect(page).toHaveURL(/agent=directory-59/);
    await directory.getByRole("button", { name: "検索をクリア" }).click();
    await expect(cards).toHaveCount(60);
    await expect(last).toHaveAttribute("aria-pressed", "true");
    expect(await results.evaluate(element => element.scrollTop)).toBe(position);

    await directory.getByRole("button", { name: "選択中の詳細へ", exact: true }).click();
    await expect(page).toHaveURL(/panel=role/);
    await page.getByRole("button", { name: "一覧に戻る", exact: true }).click();
    await page.getByRole("button", { name: "一覧を更新", exact: true }).click();
    await expect(last).toHaveAttribute("aria-pressed", "true");
    expect(await results.evaluate(element => element.scrollTop)).toBe(position);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(writes).toEqual([]);
    const file = { name: "thumbnail.png", mimeType: "image/png", buffer: Buffer.from("png") };
    await results.getByLabel("調査担当 59の画像ファイル", { exact: true }).setInputFiles(file);
    await expect(last.getByRole("img")).toHaveAttribute("src", /thumbnail-upload\/content/);
    await page.reload();
    await page.getByRole("button", { name: "一覧に戻る", exact: true }).click();
    await expect(last.getByRole("img")).toHaveAttribute("src", /thumbnail-upload\/content/);
    expect(writes).toHaveLength(1);
    expect(errors).toEqual([]);
  });
}
