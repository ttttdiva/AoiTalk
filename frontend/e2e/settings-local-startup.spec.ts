import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const MODEL = "fixture-llama-cpp";
const API_MODEL = "fixture-api";

async function fixtureApis(page: Page) {
  let current = { provider: "openai", model: API_MODEL };
  let serverStatus = "waiting";
  const writes: string[] = [];
  let statusReads = 0;
  await page.route("**/api/python-proxy/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace("/api/python-proxy", "");
    if (path === "/llm/engine" && req.method() === "POST") {
      const body = req.postDataJSON() as { provider: string; model: string };
      current = { provider: body.provider, model: body.model };
      writes.push(current.provider);
      if (current.provider === "openai_compatible_local") serverStatus = "loading";
      await route.fulfill({ json: { success: true, ...current } });
    } else if (path === "/llm/models") {
      await route.fulfill({ json: { current, providers: [
        { id: "openai", label: "Fixture API", configured_model: API_MODEL, source: "static",
          models: [{ id: API_MODEL, label: "Fixture API model" }] },
        { id: "openai_compatible_local", label: "Fixture Local", configured_model: MODEL,
          source: "static", supports_custom_model: true,
          settings: { llama_cpp: { auto_start: true } },
          models: [{ id: MODEL, label: "Fixture Local model", runtime: "llama_cpp" }] },
      ] } });
    } else if (path === "/llm/local-runtime/status") {
      statusReads += 1;
      const failed = serverStatus === "failed";
      await route.fulfill({ json: {
        model: MODEL, runtime: "llama_cpp", task_id: null, phase: "runtime",
        status: failed ? "failed" : "ready", done: true, prepared: true,
        completed: 2, total: 2, percent: 100, error: failed ? "fixture loader failed" : null,
        reason: failed ? "fixture loader failed" : null, prepare_supported: true,
        runtime_installed: true, model_installed: true,
        runtime_install: { runtime: "llama_cpp", installed: true, status: "installed", installer_required: false },
        model_artifact: { installed: true, status: "installed", managed: true },
        server: { status: serverStatus, ready: serverStatus === "ready", startup_progress: null },
        actions: { prepare: false, poll: false, retry: false, installer_required: false },
      } });
    } else if (path === "/llm/llama-cpp/model-root") {
      await route.fulfill({ json: { model_root_override: null } });
    } else if (req.method() === "GET") {
      await route.fulfill({ json: {} });
    } else {
      await route.fulfill({ status: 409, json: { detail: "Unexpected fixture mutation" } });
    }
  });
  return { writes, reads: () => statusReads, finish: (state: string) => { serverStatus = state; } };
}

test.describe("ローカルモデルの起動状態", () => {
  for (const terminal of ["ready", "failed"] as const) {
    test(`長時間loadingを再保存せず追跡し${terminal}でpollを終了する`, async ({ page }) => {
      await addAuthCookie(page);
      await mockAuthenticatedApis(page);
      const fixture = await fixtureApis(page);
      await page.clock.install();
      await page.goto("/settings");
      const section = page.locator('[data-settings-surface="language-models"]');
      await section.getByRole("button", { name: /言語モデル/ }).click();
      await section.getByRole("combobox", { name: "LLMプロバイダー" }).click();
      await page.getByRole("option", { name: /Fixture Local/ }).click();
      await expect.poll(() => fixture.writes).toEqual(["openai_compatible_local"]);
      await expect(section.getByText("保存中...", { exact: true })).toHaveCount(0);
      // Cross the observed five-minute HTTP deadline without another POST.
      await page.clock.fastForward(310000);
      await expect(section.getByText("loading", { exact: true })).toBeVisible();
      expect(fixture.writes).toEqual(["openai_compatible_local"]);
      fixture.finish(terminal);
      await page.clock.fastForward(5000);
      if (terminal === "ready") await expect(section.getByText("ready", { exact: true })).toBeVisible();
      else await expect(section.getByText("fixture loader failed", { exact: true })).toBeVisible();
      const reads = fixture.reads();
      await page.clock.fastForward(20000);
      expect(fixture.reads()).toBe(reads);
      expect(fixture.writes).toEqual(["openai_compatible_local"]);
      await section.getByRole("combobox", { name: "LLMプロバイダー" }).click();
      await page.getByRole("option", { name: /Fixture API/ }).click();
      await expect.poll(() => fixture.writes).toEqual(["openai_compatible_local", "openai"]);
      await expect(section.getByRole("combobox", { name: "LLMプロバイダー" })).toContainText("Fixture API");
    });
  }
});
