import { expect, test, type Locator, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

type FitProbe = { observer: MutationObserver; transforms: string[] };

async function fitTransforms(page: Page, button: Locator) {
  await page.evaluate(() => {
    const viewport = document.querySelector<HTMLElement>(".react-flow__viewport")!;
    const transforms: string[] = [];
    const observer = new MutationObserver(() => {
      const transform = viewport.style.transform;
      if (transforms.at(-1) !== transform) transforms.push(transform);
    });
    observer.observe(viewport, { attributes: true, attributeFilter: ["style"] });
    (window as Window & { operationsFitProbe?: FitProbe }).operationsFitProbe = { observer, transforms };
  });
  const viewport = page.locator(".react-flow__viewport").first();
  const before = await viewport.getAttribute("style");
  await button.click();
  await expect(viewport).not.toHaveAttribute("style", before!);
  // Observe beyond the normal 250ms transition to catch any intermediate frames.
  await page.waitForTimeout(350);
  return page.evaluate(() => {
    const state = (window as Window & { operationsFitProbe?: FitProbe }).operationsFitProbe!;
    state.observer.disconnect();
    delete (window as Window & { operationsFitProbe?: FitProbe }).operationsFitProbe;
    return state.transforms;
  });
}

test("both whole-map controls avoid animated viewport steps when reduced motion changes", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: {
    features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true },
  } }));
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: {
    agents: [{ id: "worker", name: "担当AI", state: "active", manager_relation: "unrecorded" }],
    current_work: Array.from({ length: 8 }, (_, index) => ({
      id: `work-${index}`, task_title: `担当の仕事 ${index}`, state: "running", assigned_agent_id: "worker",
    })),
  } }));
  await page.goto("/operations?tab=overview&overview_view=map");
  await expect(page.getByTestId("operations-map-node")).toHaveCount(9);
  const headerFit = page.locator(".operations-map-fit-button");
  await headerFit.click();
  await page.waitForTimeout(350);
  // Change the OS preference after mount: activation must use the latest value.
  await page.emulateMedia({ reducedMotion: "reduce" });
  for (const fit of [headerFit, page.locator(".react-flow__controls-fitview")]) {
    await page.getByRole("button", { name: "拡大", exact: true }).click();
    const transforms = await fitTransforms(page, fit);
    expect(transforms).toHaveLength(1);
  }
});

test("both whole-map controls fit every displayed record on a narrow screen without shrinking the initial view", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.route("**/api/python-proxy/runtime/features", route => route.fulfill({ json: {
    features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true },
  } }));
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: {
    agents: Array.from({ length: 58 }, (_, index) => ({
      id: `fit-agent-${index}`, name: `全体表示の担当 ${index}`, state: "active", manager_relation: "unrecorded",
    })),
    current_work: Array.from({ length: 12 }, (_, index) => ({
      id: `fit-work-${index}`, task_title: `全体表示の仕事 ${index}`, state: "running", assigned_agent_id: "fit-agent-0",
    })),
  } }));
  await page.goto("/operations?tab=overview&overview_view=map");
  await expect(page.getByTestId("operations-map-node")).toHaveCount(70);
  await expect.poll(() => page.locator(".react-flow__viewport").first().evaluate(element =>
    new DOMMatrixReadOnly(getComputedStyle(element).transform).a,
  )).toBe(1);
  for (const fit of [page.locator(".operations-map-fit-button"), page.locator(".react-flow__controls-fitview")]) {
    await page.getByRole("button", { name: "拡大", exact: true }).click();
    await fit.click();
    await expect.poll(() => page.locator(".react-flow__node-operation").evaluateAll(elements => {
      const flow = document.querySelector('[data-testid="operations-map-flow"]')!.getBoundingClientRect();
      return elements.filter(element => {
        const card = element.getBoundingClientRect();
        return card.left >= flow.left - 1 && card.right <= flow.right + 1 && card.top >= flow.top - 1 && card.bottom <= flow.bottom + 1;
      }).length;
    })).toBe(70);
    await expect(page.getByTestId("operations-map-node")).toHaveCount(70);
  }
});
