import { expect, test, type Page } from "@playwright/test";

import {
  addAuthCookie,
  mockAuthenticatedApis,
} from "./support/auth";

const project = {
  id: "project-overview-theme",
  name: "Project Overview Theme",
  description: null,
  slug: "project-overview-theme",
  color: "#0f9fa8",
  space_id: null,
  is_completed: false,
  can_manage_settings: true,
  can_write: true,
};

const memoryId = "11111111-1111-4111-8111-111111111111";

const overview = {
  project_id: project.id,
  status: "fresh",
  layout: {
    schema_version: 1,
    sections: [],
    graph: {
      title: "Project relationships",
      nodes: [
        {
          id: "lead-node",
          kind: "lead",
          label: "Project Lead",
          subtitle: "Owner",
          memory_ids: [memoryId],
        },
        {
          id: "system-node",
          kind: "system",
          label: "Core System",
          subtitle: "",
          memory_ids: [memoryId],
        },
      ],
      edges: [
        {
          id: "edge-1",
          source: "lead-node",
          target: "system-node",
          label: "owns",
          memory_ids: [memoryId],
        },
      ],
    },
  },
  source_digest: "theme-regression",
  generated_at: "2026-09-05T00:00:00.000Z",
  generation_version: 1,
  error_message: null,
  memory_refs: {
    [memoryId]: {
      id: memoryId,
      title: "Theme regression memory",
      memory_type: "fact",
      content: "The graph remains readable in both themes.",
      importance: 9,
      confidence: 1,
      is_pinned: true,
      updated_at: "2026-09-05T00:00:00.000Z",
    },
  },
};

async function mockProjectOverviewApis(page: Page) {
  await mockAuthenticatedApis(page);

  await page.route("**/api/projects", async (route) => {
    await route.fulfill({ json: { projects: [project], total: 1 } });
  });
  await page.route(`**/api/projects/${project.id}/members`, async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route(`**/api/projects/${project.id}/dashboard`, async (route) => {
    await route.fulfill({ json: {
      status_counts: [], priority_counts: [], tag_stats: [], recent_completed: [],
      active_timer_count: 0, total_time_seconds: 0,
      effort_tracking: { project_estimated_hours: null, task_estimated_hours_total: 0,
        task_estimated_count: 0, actual_hours: 0, member_stats: [] },
    } });
  });
  await page.route(
    `**/api/projects/${project.id}/overview`,
    async (route) => {
      await route.fulfill({ json: overview });
    },
  );
}

async function readGraphStyles(page: Page) {
  return page.evaluate(() => {
    const flow = document.querySelector<HTMLElement>(
      '[data-testid="project-overview-graph"] .react-flow',
    );
    const node = flow?.querySelector<HTMLElement>(
      '.react-flow__node[data-id="system-node"]',
    );
    const edge = flow?.querySelector<SVGPathElement>(
      ".react-flow__edge-path",
    );
    const edgeLabel = flow?.querySelector<SVGTextElement>(
      ".react-flow__edge-text",
    );
    const edgeLabelBackground = flow?.querySelector<SVGRectElement>(
      ".react-flow__edge-textbg",
    );
    const controlsButton = flow?.querySelector<HTMLButtonElement>(
      ".react-flow__controls-button",
    );
    const background = flow?.querySelector<SVGElement>(
      ".react-flow__background",
    );

    if (
      !flow ||
      !node ||
      !edge ||
      !edgeLabel ||
      !edgeLabelBackground ||
      !controlsButton ||
      !background
    ) {
      throw new Error("Project Overview React Flow elements are incomplete");
    }

    return {
      flowClass: flow.className,
      nodeBackground: getComputedStyle(node).backgroundColor,
      nodeColor: getComputedStyle(node).color,
      nodeBorder: getComputedStyle(node).borderTopColor,
      edgeStroke: getComputedStyle(edge).stroke,
      edgeLabelColor: getComputedStyle(edgeLabel).fill,
      edgeLabelBackground: getComputedStyle(edgeLabelBackground).fill,
      controlsBackground: getComputedStyle(controlsButton).backgroundColor,
      controlsColor: getComputedStyle(controlsButton).color,
      backgroundColor: getComputedStyle(background).backgroundColor,
    };
  });
}

test.describe("Project Overview graph theme", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      localStorage.clear();
      localStorage.setItem("aoitalk-theme", "light");
    });
    await mockProjectOverviewApis(page);
  });

  test("keeps graph surfaces and labels readable through light/dark toggles", async ({
    page,
  }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));

    await page.goto(`/projects?project_id=${project.id}`);

    const graph = page.getByTestId("project-overview-graph");
    await expect(graph).toBeVisible();
    await expect(graph.locator('.react-flow__node[data-id="system-node"]')).toContainText(
      "Core System",
    );
    await expect(graph.locator(".react-flow__edge-text")).toContainText("owns");
    await expect(graph.locator(".react-flow__controls-button").first()).toBeVisible();

    const light = await readGraphStyles(page);
    expect(light.flowClass.split(/\s+/)).toContain("light");

    await graph.locator('.react-flow__node[data-id="system-node"]').click();
    await expect(
      page.getByRole("region", { name: "選択中のProject Memory" }),
    ).toBeVisible();

    const themeToggle = page.getByRole("button", {
      name: /テーマを(?:ダーク|ライト)に切り替え/,
    });
    await expect(themeToggle).toBeVisible();
    await themeToggle.click();
    await expect(themeToggle).toHaveAccessibleName("テーマをライトに切り替え");
    await expect
      .poll(async () => (await readGraphStyles(page)).flowClass.split(/\s+/))
      .toContain("dark");

    const dark = await readGraphStyles(page);
    expect(dark.nodeBackground).not.toBe(light.nodeBackground);
    expect(dark.nodeColor).not.toBe(light.nodeColor);
    expect(dark.nodeBorder).not.toBe(light.nodeBorder);
    expect(dark.edgeStroke).not.toBe(light.edgeStroke);
    expect(dark.edgeLabelColor).not.toBe(light.edgeLabelColor);
    expect(dark.edgeLabelBackground).not.toBe(light.edgeLabelBackground);
    expect(dark.controlsBackground).not.toBe(light.controlsBackground);
    expect(dark.controlsColor).not.toBe(light.controlsColor);
    expect(dark.backgroundColor).not.toBe(light.backgroundColor);
    expect(dark.nodeBackground).not.toBe("rgb(255, 255, 255)");

    await themeToggle.click();
    await expect(themeToggle).toHaveAccessibleName("テーマをダークに切り替え");
    await expect
      .poll(async () => (await readGraphStyles(page)).flowClass.split(/\s+/))
      .toContain("light");
    await expect.poll(async () => readGraphStyles(page)).toEqual(light);

    expect(pageErrors).toEqual([]);
  });
});
