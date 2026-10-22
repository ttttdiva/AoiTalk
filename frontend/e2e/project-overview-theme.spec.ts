import { expect, test, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const project = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
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
    await route.fulfill({
      json: {
        status_counts: [],
        priority_counts: [],
        tag_stats: [],
        recent_completed: [],
        active_timer_count: 0,
        total_time_seconds: 0,
        effort_tracking: {
          project_estimated_hours: null,
          task_estimated_hours_total: 0,
          task_estimated_count: 0,
          actual_hours: 0,
          member_stats: [],
        },
      },
    });
  });
  await page.route(`**/api/projects/${project.id}/overview`, async (route) => {
    await route.fulfill({ json: overview });
  });
}

async function readGraphStyles(page: Page) {
  return page.evaluate(() => {
    const flow = document.querySelector<HTMLElement>(
      '[data-testid="project-overview-graph"] .react-flow',
    );
    const node = flow?.querySelector<HTMLElement>(
      '.react-flow__node[data-id="system-node"]',
    );
    const edge = flow?.querySelector<SVGPathElement>(".react-flow__edge-path");
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
    await expect(
      graph.locator('.react-flow__node[data-id="system-node"]'),
    ).toContainText("Core System");
    await expect(graph.locator(".react-flow__edge-text")).toContainText("owns");
    await expect(
      graph.locator(".react-flow__controls-button").first(),
    ).toBeVisible();

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

test.describe("Project dashboard の情報密度", () => {
  test("長文・同文・異なる記録件数を保持し、詳細と現在タスクを分ける", async ({
    page,
  }) => {
    await addAuthCookie(page);
    await mockProjectOverviewApis(page);
    const otherMemoryId = "22222222-2222-4222-8222-222222222222";
    const same =
      "提示された記録の8件は完了。現在のタスク状態とは別の記録です。";
    const longText = `対象の確認は元資料で行います。 ${"long-hostname-without-spaces-".repeat(60)}`;
    await page.route(
      `**/api/projects/${project.id}/overview`,
      async (route) => {
        await route.fulfill({
          json: {
            ...overview,
            layout: {
              ...overview.layout,
              sections: [
                {
                  kind: "highlight",
                  title: "確認手順の記録",
                  emphasis: "primary",
                  columns: 1,
                  density: "compact",
                  memory_ids: [memoryId],
                },
                {
                  kind: "bullets",
                  title: "提供記録の集計",
                  emphasis: "normal",
                  columns: 1,
                  density: "compact",
                  memory_ids: [otherMemoryId],
                },
              ],
            },
            memory_refs: {
              [memoryId]: {
                ...overview.memory_refs[memoryId],
                title: "長文の確認手順",
                content: longText,
              },
              [otherMemoryId]: {
                ...overview.memory_refs[memoryId],
                id: otherMemoryId,
                title: same,
                content: same,
                memory_type: "status",
              },
            },
          },
        });
      },
    );
    await page.route(
      `**/api/projects/${project.id}/dashboard`,
      async (route) => {
        await route.fulfill({
          json: {
            status_counts: [
              { status: "todo", count: 2 },
              { status: "in_progress", count: 1 },
              { status: "closed", count: 3 },
            ],
            priority_counts: [],
            tag_stats: [],
            recent_completed: [],
            active_timer_count: 0,
            total_time_seconds: 0,
            effort_tracking: {
              project_estimated_hours: null,
              task_estimated_hours_total: 0,
              task_estimated_count: 0,
              actual_hours: 0,
              member_stats: [],
            },
          },
        });
      },
    );
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/projects?project_id=${project.id}`);
    const panel = page.getByTestId("project-overview-panel");
    await expect(panel.getByText(same, { exact: true })).toHaveCount(1);
    const currentTasks = panel.getByRole("region", { name: "現在のタスク" });
    await expect(currentTasks).toContainText("総タスク 6件");
    await expect(
      currentTasks.getByRole("button", { name: "未完了 3", exact: true }),
    ).toBeVisible();
    await expect(currentTasks).not.toContainText("8件");
    const metadata = panel
      .locator("details")
      .filter({ has: page.locator("summary", { hasText: "生成情報" }) });
    await expect(metadata).not.toHaveAttribute("open", "");
    await metadata.locator("summary").click();
    await expect(metadata).toHaveAttribute("open", "");
    await expect(metadata).toContainText("theme-regression");
    await metadata.locator("summary").click();
    const record = panel
      .locator("details")
      .filter({ has: page.locator("summary", { hasText: "長文の確認手順" }) });
    await record.locator("summary").click();
    await expect(record).toHaveAttribute("open", "");
    await expect(record.locator("summary + div")).toContainText(longText);
    for (const width of [1440, 768, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await expect
        .poll(() =>
          panel.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
        )
        .toBe(true);
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth + 1,
          ),
        )
        .toBe(true);
    }
    await record.locator("summary").click();
    await expect(record).not.toHaveAttribute("open", "");
    await panel
      .getByRole("navigation", { name: "参照セクション" })
      .getByRole("button", { name: "提供記録の集計" })
      .click();
    await expect(
      panel
        .getByRole("heading", { name: "提供記録の集計" })
        .locator("..")
        .locator(".."),
    ).toBeFocused();
    expect(errors).toEqual([]);
  });
});

test("対応項目から現在版のナレッジ候補へ到達し、実行理由を混同しない", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockProjectOverviewApis(page);
  const candidateId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const candidate = {
    id: candidateId,
    project_id: project.id,
    status: "draft_ready",
    version: 2,
    questions: [],
    draft: {
      title: "対象手順の採用候補",
      sections: [
        { title: "確認事項", body: "保存前に元の資料を確認してください" },
      ],
    },
    sources: [],
  };
  await page.route("**/api/python-proxy/runtime/features", (route) =>
    route.fulfill({
      json: {
        application_features: {
          virtual_company: true,
          autonomous_agent_runtime: true,
        },
      },
    }),
  );
  await page.route(
    "**/api/python-proxy/operations/command-center?**",
    (route) =>
      route.fulfill({
        json: {
          schema_version: "operations-command-center-v1",
          filters: { project_id: project.id },
          attention_page: { limit: 100, has_more: false },
          current_work: [
            {
              id: "work-1",
              project_id: project.id,
              task_id: "task-1",
              task_title: "検証の実行",
              agent: "確認担当Agent",
              state: "failed",
              execution_summary: {
                safe_error_code: "verification_failed",
                phase: "verification",
              },
            },
          ],
          attention: [
            {
              kind: "work",
              id: "work-1",
              state: "failed",
              execution_summary: { safe_error_code: "verification_failed" },
            },
          ],
        },
      }),
  );
  await page.route("**/api/tasks?**", (route) =>
    route.fulfill({
      json: [
        {
          id: "task-1",
          project_id: project.id,
          title: "対象の確認作業",
          status: "review",
          priority: "medium",
          metadata: {},
          parent_task_id: null,
          assignees: [],
          tags: [],
        },
      ],
    }),
  );
  await page.route(
    `**/api/projects/${project.id}/knowledge-capture/candidates?**`,
    (route) =>
      route.fulfill({
        json: {
          items:
            new URL(route.request().url()).searchParams.get("status") ===
            "draft_ready"
              ? [candidate]
              : [],
          total: 1,
        },
      }),
  );
  await page.route(
    `**/api/projects/${project.id}/knowledge-capture/candidates/${candidateId}`,
    (route) => route.fulfill({ json: candidate }),
  );
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`/projects?project_id=${project.id}`);
  const attention = page.getByRole("region", { name: "あなたの対応が必要" });
  await expect(attention).toContainText("対象手順の採用候補");
  await expect(attention).toContainText("verification_failed");
  await expect(
    attention.getByRole("link", { name: "実行の詳細を見る" }),
  ).toHaveAttribute("href", /work_id=work-1/);
  const work = page.getByRole("region", { name: "作業の状況" });
  await expect(work).toContainText("レビュー中");
  await expect(work).toContainText("実行失敗");
  await attention.getByRole("button", { name: "候補を確認" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("対象手順の採用候補");
  await expect(dialog).toContainText("保存前に元の資料を確認してください");
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect
      .poll(() =>
        page
          .getByTestId("project-situation-dashboard")
          .evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
      )
      .toBe(true);
  }
  expect(errors).toEqual([]);
});
