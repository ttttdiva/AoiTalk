import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie } from "./support/auth";

type MockNode = {
  id: string;
  workspace_id: string;
  parent_id: string | null;
  root_page_id: string | null;
  project_id: string | null;
  title: string;
  body_json: Record<string, unknown>;
  body_text: string;
  node_type: "page" | "block" | "object";
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
};

type MockState = {
  nodes: MockNode[];
  supertags: Array<{
    id: string;
    workspace_id: string;
    parent_supertag_id: string | null;
    name: string;
    base_type: string;
    description: string | null;
    color: string | null;
    icon: string | null;
    template_json: Record<string, unknown>;
    pinned_field_ids: string[];
    title_template: string | null;
    ai_instructions: string | null;
  }>;
  node_supertags: Array<{ node_id: string; supertag_id: string }>;
  fields: Array<{
    id: string;
    workspace_id: string;
    supertag_id: string;
    name: string;
    field_type: string;
    required: boolean;
    options_json: Record<string, unknown>;
    default_value_json: unknown;
    sort_order: number;
  }>;
  field_values: Array<{
    node_id: string;
    field_id: string;
    value_json: unknown;
    value_text: string | null;
    value_number: number | null;
    value_datetime: string | null;
    target_node_id: string | null;
  }>;
  views: Array<{
    id: string;
    workspace_id: string;
    supertag_id: string | null;
    name: string;
    layout: string;
    config_json: Record<string, unknown>;
    sort_order: number;
    created_at: string | null;
    updated_at: string | null;
  }>;
  projects: Array<{ id: string; name: string; space_id: string | null; color: string | null }>;
};

const blockJson = (block_type = "paragraph", checked = false) => ({
  format: "doc_block",
  block_type,
  checked,
});

function createState(): MockState {
  return {
    nodes: [
      {
        id: "node-a",
        workspace_id: "workspace-1",
        parent_id: null,
        root_page_id: "node-a",
        project_id: "project-1",
        title: "SLCO中部納整センター",
        body_json: blockJson(),
        body_text: "",
        node_type: "page",
        sort_order: 1,
        created_at: "2026-07-04T00:00:00",
        updated_at: "2026-07-04T00:00:00",
        archived_at: null,
      },
      {
        id: "block-title",
        workspace_id: "workspace-1",
        parent_id: "node-a",
        root_page_id: "node-a",
        project_id: "project-1",
        title: "概要",
        body_json: blockJson("heading_1"),
        body_text: "概要",
        node_type: "block",
        sort_order: 1,
        created_at: "2026-07-04T00:00:00",
        updated_at: "2026-07-04T00:00:00",
        archived_at: null,
      },
      {
        id: "block-task",
        workspace_id: "workspace-1",
        parent_id: "node-a",
        root_page_id: "node-a",
        project_id: "project-1",
        title: "初回打合せ日程を確定",
        body_json: blockJson("checkbox", false),
        body_text: "初回打合せ日程を確定",
        node_type: "block",
        sort_order: 2,
        created_at: "2026-07-04T00:00:00",
        updated_at: "2026-07-04T00:00:00",
        archived_at: null,
      },
      {
        id: "block-link",
        workspace_id: "workspace-1",
        parent_id: "node-a",
        root_page_id: "node-a",
        project_id: "project-1",
        title: "関連ページ",
        body_json: blockJson("paragraph"),
        body_text: "関連は [[関連Meeting]] を参照",
        node_type: "block",
        sort_order: 3,
        created_at: "2026-07-04T00:00:00",
        updated_at: "2026-07-04T00:00:00",
        archived_at: null,
      },
      {
        id: "node-meeting",
        workspace_id: "workspace-1",
        parent_id: null,
        root_page_id: "node-meeting",
        project_id: "project-1",
        title: "関連Meeting",
        body_json: blockJson(),
        body_text: "",
        node_type: "page",
        sort_order: 2,
        created_at: "2026-07-04T00:00:00",
        updated_at: "2026-07-04T00:00:00",
        archived_at: null,
      },
    ],
    supertags: [
      {
        id: "tag-task",
        workspace_id: "workspace-1",
        parent_supertag_id: null,
        name: "Task",
        base_type: "task",
        description: "",
        color: "#22c55e",
        icon: "check-square",
        template_json: {},
        pinned_field_ids: ["field-status", "field-due"],
        title_template: null,
        ai_instructions: "状態と期日を更新する",
      },
    ],
    node_supertags: [{ node_id: "block-task", supertag_id: "tag-task" }],
    fields: [
      {
        id: "field-status",
        workspace_id: "workspace-1",
        supertag_id: "tag-task",
        name: "状態",
        field_type: "select",
        required: false,
        options_json: { values: ["todo", "doing", "done"] },
        default_value_json: "todo",
        sort_order: 1,
      },
      {
        id: "field-due",
        workspace_id: "workspace-1",
        supertag_id: "tag-task",
        name: "期日",
        field_type: "date",
        required: false,
        options_json: {},
        default_value_json: null,
        sort_order: 2,
      },
    ],
    field_values: [
      {
        node_id: "block-task",
        field_id: "field-status",
        value_json: "todo",
        value_text: "todo",
        value_number: null,
        value_datetime: null,
        target_node_id: null,
      },
    ],
    views: [
      {
        id: "view-task",
        workspace_id: "workspace-1",
        supertag_id: "tag-task",
        name: "SLCO 未完了Task board",
        layout: "board",
        config_json: { filters: { supertag: "Task" } },
        sort_order: 1,
        created_at: "2026-07-04T00:00:00",
        updated_at: "2026-07-04T00:00:00",
      },
    ],
    projects: [{ id: "project-1", name: "SLCO中部納整センター", space_id: null, color: null }],
  };
}

function titleFor(text: string) {
  return text.replace(/\r\n?/g, "\n").split("\n").find((line) => line.trim())?.trim().slice(0, 500) ?? "";
}

async function installDocsMock(page: Page, state: MockState) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({ json: { authenticated: true, user: { id: "user-1", username: "tester", role: "admin" } } });
      return;
    }
    if (url.pathname === "/api/projects") {
      await route.fulfill({ json: { projects: state.projects, total: state.projects.length } });
      return;
    }
    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/notifications" || url.pathname === "/api/tasks" || url.pathname === "/api/users/list") {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/time-entries/active") {
      await route.fulfill({ json: null });
      return;
    }

    if (url.pathname === "/api/docs/search" && method === "GET") {
      const query = url.searchParams.get("q")?.toLowerCase() ?? "";
      const nodeType = url.searchParams.get("node_type");
      const nodes = state.nodes
        .filter((node) => !nodeType || node.node_type === nodeType)
        .filter((node) => !query || node.title.toLowerCase().includes(query) || node.body_text.toLowerCase().includes(query));
      await route.fulfill({ json: { nodes } });
      return;
    }

    const treeMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/tree$/);
    if (treeMatch && method === "GET") {
      const focusId = treeMatch[1];
      const focus = state.nodes.find((node) => node.id === focusId) ?? state.nodes[0];
      const rootId = focus.root_page_id ?? focus.id;
      await route.fulfill({
        json: {
          focus_node_id: focus.id,
          root_page_id: rootId,
          nodes: state.nodes.filter((node) => node.id === rootId || node.root_page_id === rootId),
          supertags: state.supertags,
          fields: state.fields,
          node_supertags: state.node_supertags,
          field_values: state.field_values,
          views: state.views,
          projects: state.projects,
        },
      });
      return;
    }

    const referencesMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/references$/);
    if (referencesMatch && method === "GET") {
      const target = state.nodes.find((node) => node.id === referencesMatch[1]) ?? state.nodes[0];
      await route.fulfill({
        json: {
          backlinks: target.id === "node-a"
            ? [{ node: state.nodes.find((node) => node.id === "node-meeting"), kind: "wikilink", snippet: "打合せでSLCO正本を参照" }]
            : [],
          outgoing: target.id === "node-a"
            ? [{ node: state.nodes.find((node) => node.id === "node-meeting"), kind: "wikilink", snippet: "関連は [[関連Meeting]] を参照" }]
            : [],
        },
      });
      return;
    }

    const fieldsMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/fields$/);
    if (fieldsMatch && method === "PUT") {
      const nodeId = fieldsMatch[1];
      const body = await route.request().postDataJSON();
      for (const item of body.field_values ?? []) {
        const field = state.fields.find((candidate) => candidate.id === item.field_id);
        if (!field) continue;
        state.field_values = state.field_values.filter((value) => !(value.node_id === nodeId && value.field_id === field.id));
        state.field_values.push({
          node_id: nodeId,
          field_id: field.id,
          value_json: item.value,
          value_text: item.value == null ? null : String(item.value),
          value_number: field.field_type === "number" ? Number(item.value) : null,
          value_datetime: field.field_type === "date" ? String(item.value) : null,
          target_node_id: null,
        });
      }
      await route.fulfill({ json: { field_values: state.field_values.filter((value) => value.node_id === nodeId) } });
      return;
    }

    const tagMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/supertags$/);
    if (tagMatch && method === "PUT") {
      const nodeId = tagMatch[1];
      const body = await route.request().postDataJSON();
      state.node_supertags = [
        ...state.node_supertags.filter((entry) => entry.node_id !== nodeId),
        ...(body.supertag_ids ?? []).map((tagId: string) => ({ node_id: nodeId, supertag_id: tagId })),
      ];
      await route.fulfill({ json: { node_supertags: state.node_supertags.filter((entry) => entry.node_id === nodeId) } });
      return;
    }

    const nodeMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)$/);
    if (nodeMatch && method === "PATCH") {
      const nodeId = nodeMatch[1];
      const body = await route.request().postDataJSON();
      const index = state.nodes.findIndex((node) => node.id === nodeId);
      if (index >= 0) {
        state.nodes[index] = { ...state.nodes[index], ...body, updated_at: new Date().toISOString() };
      }
      await route.fulfill({ json: { node: state.nodes[index] } });
      return;
    }

    if (url.pathname === "/api/docs" && method === "POST") {
      const body = await route.request().postDataJSON();
      const node: MockNode = {
        id: body.id,
        workspace_id: "workspace-1",
        parent_id: body.parent_id ?? null,
        root_page_id: body.parent_id ? "node-a" : body.id,
        project_id: body.project_id ?? null,
        title: body.title ?? titleFor(body.body_text ?? ""),
        body_json: body.body_json ?? blockJson(),
        body_text: body.body_text ?? "",
        node_type: body.node_type ?? "block",
        sort_order: body.sort_order ?? state.nodes.length + 1,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        archived_at: null,
      };
      state.nodes.push(node);
      await route.fulfill({ json: { node }, status: 201 });
      return;
    }

    await route.fulfill({ json: {} });
  });
}

async function openDocs(page: Page, state: MockState) {
  await addAuthCookie(page);
  await installDocsMock(page, state);
  await page.goto("/docs/node-a");
  await expect(page.getByTestId("docs-block-editor")).toBeVisible();
}

test.describe("Docs block editor", () => {
  test("renders document blocks without forced bullets", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await expect(page.locator('[data-block-kind="heading_1"]', { hasText: "概要" })).toBeVisible();
    await expect(page.locator('[data-block-kind="checkbox"]', { hasText: "初回打合せ日程を確定" })).toBeVisible();
    await expect(page.locator(".docs-block-bulleted_list")).toHaveCount(0);
  });

  test("shows pinned fields for typed blocks and saves inline edits", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await expect(page.getByRole("button", { name: "#Task" }).first()).toBeVisible();
    const blockFields = page.getByTestId("docs-block-fields");
    await blockFields.getByLabel("Field 状態").selectOption("doing");
    await blockFields.getByLabel("Field 状態").blur();
    await expect.poll(() => state.field_values.find((value) => value.field_id === "field-status")?.value_text).toBe("doing");
  });

  test("keeps linked mentions and outgoing links at the page bottom", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await expect(page.getByRole("heading", { name: "Linked mentions" })).toBeVisible();
    await expect(page.getByText("打合せでSLCO正本を参照").first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Outgoing links" })).toBeVisible();
    await expect(page.getByText("関連は [[関連Meeting]] を参照").first()).toBeVisible();
  });

  test("opens saved views and quick search", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator("main").getByRole("button", { name: "Views" }).click();
    await expect(page.getByRole("button", { name: "SLCO 未完了Task board" })).toBeVisible();
    await expect(page.getByRole("button", { name: "#Task" }).first()).toBeVisible();
    await page.locator("main").getByRole("button", { name: "Document" }).click();
    await page.locator("main").getByRole("button", { name: "Search" }).last().click();
    await expect(page.getByTestId("docs-quick-open-input")).toBeVisible();
  });

  test("opens the slash menu and converts the current block type", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("/quote");
    await expect(page.getByRole("listbox")).toContainText("Quote");
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-block-kind="quote"]').last()).toBeVisible();
  });
});
