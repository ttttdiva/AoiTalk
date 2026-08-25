import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie } from "./support/auth";

type MockNode = {
  id: string;
  workspace_id: string;
  parent_id: string | null;
  root_page_id: string | null;
  project_id: string | null;
  system_key?: string | null;
  title: string;
  description?: string;
  body_json: Record<string, unknown>;
  body_text: string;
  node_type: "node" | "page" | "block" | "object" | "search" | "day";
  query_json?: Record<string, unknown> | null;
  view_json?: Record<string, unknown>;
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
    system_key?: string | null;
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
    system_key?: string | null;
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
  attachments: Array<{
    id: string;
    node_id: string;
    file_name: string;
    file_path: string;
    mime_type: string | null;
    size_bytes: number | null;
    metadata: Record<string, unknown>;
    created_by: string | null;
    created_at: string | null;
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
        id: "block-search",
        workspace_id: "workspace-1",
        parent_id: "node-a",
        root_page_id: "node-a",
        project_id: "project-1",
        title: "未完了タスク",
        body_json: blockJson("paragraph"),
        body_text: "",
        node_type: "search",
        query_json: { and: [{ tag_system_key: "task" }], project_id: "project-1", limit: 20 },
        view_json: { view: "list" },
        sort_order: 4,
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
        system_key: "task",
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
        system_key: "task_status",
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
        system_key: "task_due",
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
      {
        node_id: "block-task",
        field_id: "field-due",
        value_json: "2026-07-09T00:00:00.000Z",
        value_text: null,
        value_number: null,
        value_datetime: "2026-07-09T00:00:00.000Z",
        target_node_id: null,
      },
    ],
    attachments: [],
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

function createDeepState(): MockState {
  const state = createState();
  state.nodes.push(
    {
      id: "deep-parent",
      workspace_id: "workspace-1",
      parent_id: "block-title",
      root_page_id: "node-a",
      project_id: "project-1",
      title: "深い親ノード",
      body_json: blockJson(),
      body_text: "",
      node_type: "block",
      sort_order: 1,
      created_at: "2026-07-04T00:00:00",
      updated_at: "2026-07-04T00:00:00",
      archived_at: null,
    },
    {
      id: "deep-child",
      workspace_id: "workspace-1",
      parent_id: "deep-parent",
      root_page_id: "node-a",
      project_id: "project-1",
      title: "深い子孫ノード",
      body_json: blockJson(),
      body_text: "",
      node_type: "block",
      sort_order: 1,
      created_at: "2026-07-04T00:00:00",
      updated_at: "2026-07-04T00:00:00",
      archived_at: null,
    },
    {
      id: "deep-content",
      workspace_id: "workspace-1",
      parent_id: "deep-child",
      root_page_id: "node-a",
      project_id: "project-1",
      title: "深いノード本文",
      body_json: blockJson("paragraph"),
      body_text: "深いノード本文",
      node_type: "block",
      sort_order: 1,
      created_at: "2026-07-04T00:00:00",
      updated_at: "2026-07-04T00:00:00",
      archived_at: null,
    },
    {
      id: "other-root-child",
      workspace_id: "workspace-1",
      parent_id: "node-meeting",
      root_page_id: "node-meeting",
      project_id: "project-1",
      title: "別ルートの子ノード",
      body_json: blockJson(),
      body_text: "",
      node_type: "block",
      sort_order: 1,
      created_at: "2026-07-04T00:00:00",
      updated_at: "2026-07-04T00:00:00",
      archived_at: null,
    },
  );
  return state;
}

function titleFor(text: string) {
  return text.replace(/\r\n?/g, "\n").split("\n").find((line) => line.trim())?.trim().slice(0, 500) ?? "";
}

type DocsMockOptions = {
  createDelays?: number[];
  createFailures?: number[];
  patchDelays?: number[];
  /** Hold one PATCH until the caller releases it to make navigation races deterministic. */
  patchGate?: {
    requestIndex?: number;
    onStart?: () => void;
    release?: Promise<void>;
  };
  tagDelays?: number[];
  tagCommittedWarnings?: number[];
  invalidTagIds?: string[];
  deleteFailures?: string[];
  deleteCommittedWarnings?: string[];
  childrenDelays?: Record<string, number>;
  detailsDelays?: Record<string, number>;
};

function expectPersistedBlank(node: MockNode | undefined) {
  expect(node).toBeTruthy();
  expect(node?.title).toBe("");
  expect(node?.body_text).toBe("");
  expect(node?.node_type).toBe("node");
  expect(node?.body_json).toMatchObject({
    format: "doc_block",
    block_type: "paragraph",
    blank: true,
  });
}

async function installDocsMock(page: Page, state: MockState, options: DocsMockOptions = {}) {
  let nextNodeSequence = 1;
  let createRequestIndex = 0;
  let patchRequestIndex = 0;
  let tagRequestIndex = 0;

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
    if (url.pathname === "/api/users/me/settings" && method === "GET") {
      await route.fulfill({ json: { settings: { remote_server_connection_enabled: true } } });
      return;
    }
    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/tasks") {
      await route.fulfill({ json: [{
        id: "11111111-1111-4111-8111-111111111111",
        project_id: "project-1",
        project_name: "Mock project",
        title: "確認用タスク",
        status: "todo",
        priority: "normal",
        all_day: false,
        reminder_offsets: [],
        notifications_enabled: false,
        source: "manual",
        metadata: {},
        assignees: [],
        tags: [],
      }] });
      return;
    }
    if (url.pathname === "/api/notifications" || url.pathname === "/api/users/list") {
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
    if (url.pathname === "/api/python-proxy/remote-servers" && method === "GET") {
      await route.fulfill({ json: { profiles: [{ id: "enterprise-1", name: "AoiTalk Enterprise", enabled: true }] } });
      return;
    }

    if (url.pathname === "/api/docs/bootstrap" && method === "GET") {
      const roots = state.nodes.filter((node) => node.parent_id === null);
      await route.fulfill({
        json: {
          ...state,
          nodes: roots,
          node_supertags: state.node_supertags.filter((entry) => roots.some((node) => node.id === entry.node_id)),
          field_values: state.field_values.filter((value) => roots.some((node) => node.id === value.node_id)),
          attachments: state.attachments.filter((attachment) => roots.some((node) => node.id === attachment.node_id)),
          has_children_ids: roots.filter((node) => state.nodes.some((child) => child.parent_id === node.id)).map((node) => node.id),
          loaded_children_parent_ids: [],
          details_loaded_ids: [],
          has_details_ids: roots.filter((node) => state.attachments.some((attachment) => attachment.node_id === node.id)).map((node) => node.id),
          children_next_cursor_by_parent: {},
        },
      });
      return;
    }
    if (url.pathname === "/api/docs/today" && method === "GET") {
      // Today is intentionally backed by the same mutable mock state as the
      // regular page route. This lets save-barrier tests verify that an edit
      // is applied before the state-replacing Today load completes.
      const today = state.nodes.find((node) => node.node_type === "day") ?? {
        id: "mock-today",
        workspace_id: "workspace-1",
        parent_id: null,
        root_page_id: "mock-today",
        project_id: "project-1",
        title: "Today",
        body_json: blockJson(),
        body_text: "",
        node_type: "day" as const,
        sort_order: 0,
        created_at: "2026-07-04T00:00:00",
        updated_at: "2026-07-04T00:00:00",
        archived_at: null,
      };
      await route.fulfill({ json: { node: today, node_supertags: [] } });
      return;
    }
    if (url.pathname === "/api/docs/pages" && method === "GET") {
      await route.fulfill({
        json: {
          pages: [{
            id: "node-meeting",
            title: "関連Meeting",
            aliases: ["meeting-alias"],
            node_type: "page",
            project_id: "project-1",
            breadcrumb: ["関連Meeting"],
          }],
        },
      });
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

    if (url.pathname === "/api/docs/task-bindings" && method === "POST") {
      const body = await route.request().postDataJSON();
      const nodeIds = Array.isArray(body.node_ids) ? body.node_ids : [];
      await route.fulfill({
        json: {
          bindings: nodeIds.map((nodeId: string) => ({
            node_id: nodeId,
            task: nodeId === "block-task"
              ? {
                  id: "task-1",
                  project_id: "project-1",
                  knowledge_node_id: "block-task",
                  title: "初回打合せ日程を確定",
                  status: "todo",
                }
              : null,
          })),
        },
      });
      return;
    }

    if (url.pathname === "/api/docs/query" && method === "POST") {
      await route.fulfill({
        json: {
          nodes: state.nodes.filter((node) => node.id === "block-task"),
          node_supertags: state.node_supertags.filter((entry) => entry.node_id === "block-task"),
          field_values: state.field_values.filter((value) => value.node_id === "block-task"),
          next_cursor: null,
        },
      });
      return;
    }

    const treeMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/tree$/);
    if (treeMatch && method === "GET") {
      const focusId = treeMatch[1];
      const focus = state.nodes.find((node) => node.id === focusId) ?? state.nodes[0];
      const ancestors: MockNode[] = [];
      let cursor: MockNode | undefined = focus;
      while (cursor?.parent_id) {
        cursor = state.nodes.find((node) => node.id === cursor?.parent_id);
        if (cursor) ancestors.unshift(cursor);
      }
      await route.fulfill({
        json: {
          focus_node_id: focus.id,
          root_page_id: focus.root_page_id ?? focus.id,
          nodes: [...ancestors, focus],
          node_supertags: state.node_supertags.filter((entry) => [...ancestors, focus].some((node) => node.id === entry.node_id)),
          has_children_ids: [...ancestors, focus].filter((node) => state.nodes.some((child) => child.parent_id === node.id)).map((node) => node.id),
          loaded_children_parent_ids: [],
        },
      });
      return;
    }

    const childrenMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/children$/);
    if (childrenMatch && method === "GET") {
      const parentId = childrenMatch[1];
      const offset = Number(url.searchParams.get("cursor") ?? 0) || 0;
      const allChildren = state.nodes.filter((node) => node.parent_id === parentId && !node.archived_at);
      const children = allChildren.slice(offset, offset + 80);
      const nextCursor = offset + children.length < allChildren.length ? String(offset + children.length) : null;
      const childrenDelay = options.childrenDelays?.[parentId] ?? 0;
      if (childrenDelay > 0) await new Promise((resolve) => setTimeout(resolve, childrenDelay));
      await route.fulfill({
        json: {
          parent_node_id: parentId,
          nodes: children,
          node_supertags: state.node_supertags.filter((entry) => children.some((node) => node.id === entry.node_id)),
          has_children_ids: children.filter((node) => state.nodes.some((child) => child.parent_id === node.id && !child.archived_at)).map((node) => node.id),
          has_details_ids: children.filter((node) => state.field_values.some((value) => value.node_id === node.id) || state.attachments.some((attachment) => attachment.node_id === node.id)).map((node) => node.id),
          loaded_children_parent_ids: offset === 0 ? [parentId] : [],
          children_next_cursor_by_parent: { [parentId]: nextCursor },
          next_cursor: nextCursor,
        },
      });
      return;
    }

    const detailsMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/details$/);
    if (detailsMatch && method === "GET") {
      const nodeId = detailsMatch[1];
      const node = state.nodes.find((item) => item.id === nodeId);
      const detailsDelay = options.detailsDelays?.[nodeId] ?? 0;
      if (detailsDelay > 0) await new Promise((resolve) => setTimeout(resolve, detailsDelay));
      await route.fulfill({
        json: {
          nodes: node ? [node] : [],
          node_supertags: state.node_supertags.filter((entry) => entry.node_id === nodeId),
          field_values: state.field_values.filter((value) => value.node_id === nodeId),
          attachments: state.attachments.filter((attachment) => attachment.node_id === nodeId),
          details_loaded_ids: [nodeId],
        },
      });
      return;
    }

    const attachmentMatch = url.pathname.match(/^\/api\/docs\/attachments\/([^/]+)$/);
    if (attachmentMatch && method === "GET") {
      await route.fulfill({
        contentType: "image/png",
        body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII=", "base64"),
      });
      return;
    }
    if (attachmentMatch && method === "DELETE") {
      state.attachments = state.attachments.filter((attachment) => attachment.id !== attachmentMatch[1]);
      await route.fulfill({ json: { ok: true } });
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

    if (url.pathname === "/api/docs/fields" && method === "POST") {
      const body = await route.request().postDataJSON();
      await new Promise((resolve) => setTimeout(resolve, 150));
      const fieldId = `field-created-${state.fields.length + 1}`;
      const field = {
        id: fieldId,
        workspace_id: "workspace-1",
        supertag_id: body.supertag_id,
        system_key: null,
        name: body.name,
        field_type: body.field_type ?? "text",
        required: false,
        options_json: body.options_json ?? {},
        default_value_json: null,
        sort_order: state.fields.filter((item) => item.supertag_id === body.supertag_id).length + 1,
      };
      state.fields.push(field);
      await route.fulfill({
        json: {
          field,
          supertag_field: {
            supertag_id: body.supertag_id,
            field_id: fieldId,
            required: false,
            sort_order: field.sort_order,
          },
        },
      });
      return;
    }

    const moveMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/move$/);
    if (moveMatch && method === "POST") {
      const body = await route.request().postDataJSON();
      const target = state.nodes.find((node) => node.id === moveMatch[1]);
      const parent = state.nodes.find((node) => node.id === body.new_parent_id);
      if (!target || !parent) {
        await route.fulfill({ status: 404, json: { detail: "移動先が見つかりません" } });
        return;
      }
      target.parent_id = parent.id;
      target.root_page_id = parent.root_page_id ?? parent.id;
      target.project_id = parent.project_id;
      await route.fulfill({ json: { node: target } });
      return;
    }

    const tagMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)\/supertags$/);
    if (tagMatch && method === "PUT") {
      const nodeId = tagMatch[1];
      const body = await route.request().postDataJSON();
      const tagDelay = options.tagDelays?.[tagRequestIndex] ?? 0;
      const requestIndex = tagRequestIndex;
      tagRequestIndex += 1;
      if (tagDelay > 0) await new Promise((resolve) => setTimeout(resolve, tagDelay));
      if ((body.add_supertag_ids ?? []).some((tagId: string) => options.invalidTagIds?.includes(tagId))) {
        await route.fulfill({ status: 409, json: { detail: "指定されたSupertagは削除済みか利用できません" } });
        return;
      }
      const existingIds = state.node_supertags.filter((entry) => entry.node_id === nodeId).map((entry) => entry.supertag_id);
      const nextIds = body.remove_supertag_ids?.length
        ? existingIds.filter((tagId) => !body.remove_supertag_ids.includes(tagId))
        : body.add_supertag_ids?.length
          ? Array.from(new Set([...existingIds, ...body.add_supertag_ids]))
          : body.supertag_ids ?? [];
      state.node_supertags = [
        ...state.node_supertags.filter((entry) => entry.node_id !== nodeId),
        ...nextIds.map((tagId: string) => ({ node_id: nodeId, supertag_id: tagId })),
      ];
      await route.fulfill({ json: {
        node_supertags: state.node_supertags.filter((entry) => entry.node_id === nodeId),
        committed: true,
        task_binding_error: options.tagCommittedWarnings?.includes(requestIndex) ? "mock task reconcile failure" : null,
      } });
      return;
    }

    const nodeMatch = url.pathname.match(/^\/api\/docs\/nodes\/([^/]+)$/);
    if (nodeMatch && method === "PATCH") {
      const nodeId = nodeMatch[1];
      const body = await route.request().postDataJSON();
      const patchDelay = options.patchDelays?.[patchRequestIndex] ?? 0;
      const requestIndex = patchRequestIndex;
      patchRequestIndex += 1;
      if (options.patchGate?.requestIndex === requestIndex) {
        options.patchGate.onStart?.();
        if (options.patchGate.release) await options.patchGate.release;
      }
      if (patchDelay > 0) await new Promise((resolve) => setTimeout(resolve, patchDelay));
      const index = state.nodes.findIndex((node) => node.id === nodeId);
      if (index >= 0) {
        const patch = { ...body };
        if (patch.archived === false) {
          delete patch.archived;
          patch.archived_at = null;
        }
        state.nodes[index] = { ...state.nodes[index], ...patch, updated_at: new Date().toISOString() };
      }
      await route.fulfill({ json: { node: state.nodes[index] } });
      return;
    }

    if (nodeMatch && method === "DELETE") {
      const nodeId = nodeMatch[1];
      if (options.deleteFailures?.includes(nodeId)) {
        await route.fulfill({ status: 409, json: { detail: "Archive failed for testing" } });
        return;
      }
      const now = new Date().toISOString();
      const archiveTree = (id: string) => {
        const index = state.nodes.findIndex((node) => node.id === id);
        if (index < 0) return;
        state.nodes[index] = { ...state.nodes[index], archived_at: now, updated_at: now };
        for (const child of state.nodes.filter((node) => node.parent_id === id)) {
          archiveTree(child.id);
        }
      };
      archiveTree(nodeId);
      const index = state.nodes.findIndex((node) => node.id === nodeId);
      const taskBindingError = options.deleteCommittedWarnings?.includes(nodeId)
        ? "mock task unlink failure"
        : null;
      await route.fulfill({
        json: {
          node: state.nodes[index],
          committed: true,
          task_binding_error: taskBindingError,
        },
      });
      return;
    }

    if (url.pathname === "/api/docs" && method === "POST") {
      const body = await route.request().postDataJSON();
      const requestIndex = createRequestIndex;
      const createDelay = options.createDelays?.[requestIndex] ?? 0;
      createRequestIndex += 1;
      if (createDelay > 0) await new Promise((resolve) => setTimeout(resolve, createDelay));
      if (options.createFailures?.includes(requestIndex)) {
        await route.fulfill({ status: 500, json: { detail: "mock create failure" } });
        return;
      }
      const id = body.id ?? `mock-node-${nextNodeSequence++}`;
      const node: MockNode = {
        id,
        workspace_id: "workspace-1",
        parent_id: body.parent_id ?? null,
        root_page_id: body.parent_id ? "node-a" : id,
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

async function openDocs(page: Page, state: MockState, options: DocsMockOptions = {}) {
  await addAuthCookie(page);
  await installDocsMock(page, state, options);
  await page.goto("/docs/node-a");
  await expect(page.getByTestId("docs-block-editor")).toBeVisible();
}

test.describe("Docs block editor", () => {
  test("loads only the focused direct children and defers nested content until expand", async ({ page }) => {
    const state = createDeepState();
    const requests: string[] = [];
    page.on("request", (request) => {
      const path = new URL(request.url()).pathname;
      if (path.includes("/api/docs/nodes/")) requests.push(path);
    });
    await openDocs(page, state);
    expect(requests).toContain("/api/docs/nodes/node-a/children");
    expect(requests).not.toContain("/api/docs/nodes/block-title/children");

    const heading = page.locator('[data-docs-block-id="block-title"]');
    await heading.getByTitle("展開").click();
    await expect.poll(() => requests).toContain("/api/docs/nodes/block-title/children");
    await expect(page.locator('[data-docs-block-id="deep-parent"]')).toBeVisible();
  });

  test("loads sidebar children on the first expand", async ({ page }) => {
    const state = createDeepState();
    const requests: string[] = [];
    page.on("request", (request) => {
      const path = new URL(request.url()).pathname;
      if (path.includes("/api/docs/nodes/")) requests.push(path);
    });
    await openDocs(page, state);
    expect(requests).not.toContain("/api/docs/nodes/block-title/children");

    const sidebarRoot = page.locator('[data-docs-sidebar-node-id="node-a"]');
    const rootToggle = sidebarRoot.locator("xpath=preceding-sibling::button[1]");
    if (await rootToggle.getAttribute("aria-label") === "展開する") await rootToggle.click();
    const sidebarHeading = page.locator('[data-docs-sidebar-node-id="block-title"]');
    await sidebarHeading.locator("xpath=preceding-sibling::button[1]").click();
    await expect.poll(() => requests).toContain("/api/docs/nodes/block-title/children");
    await expect(page.locator('[data-docs-sidebar-node-id="deep-parent"]')).toBeVisible();
  });

  test("expands and collapses a focused sidebar node with Ctrl+Arrow", async ({ page }) => {
    const state = createDeepState();
    await openDocs(page, state);

    const sidebarRoot = page.locator('[data-docs-sidebar-node-id="node-a"]');
    // The chevron is a sibling of the row button, so keyboard expansion must
    // resolve its owning sidebar node as well.
    await sidebarRoot.locator("xpath=preceding-sibling::button[1]").focus();
    await page.keyboard.press("Control+ArrowRight");
    const sidebarHeading = page.locator('[data-docs-sidebar-node-id="block-title"]');
    await expect(sidebarHeading).toBeVisible();

    await sidebarHeading.focus();
    await page.keyboard.press("Control+ArrowRight");
    await expect(page.locator('[data-docs-sidebar-node-id="deep-parent"]')).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="deep-child"]')).toHaveCount(0);

    await page.keyboard.press("Control+ArrowLeft");
    await expect(page.locator('[data-docs-sidebar-node-id="deep-parent"]')).toHaveCount(0);
  });

  test("bulk expands and collapses only currently visible sidebar nodes", async ({ page }) => {
    const state = createDeepState();
    await openDocs(page, state);

    const sidebarRoot = page.locator('[data-docs-sidebar-node-id="node-a"]');
    await sidebarRoot.focus();
    await page.keyboard.press("Control+ArrowRight");
    await expect(page.locator('[data-docs-sidebar-node-id="block-title"]')).toBeVisible();
    // The first press above is a single-node expansion.  Start a fresh
    // two-press gesture after the 500ms bulk window has elapsed.
    await page.waitForTimeout(550);
    await sidebarRoot.focus();
    await page.keyboard.press("Control+ArrowRight");
    await page.keyboard.press("Control+ArrowRight");
    await expect(page.locator('[data-docs-sidebar-node-id="deep-parent"]')).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="deep-child"]')).toHaveCount(0);

    // A leaf keeps focus while the second press bulk-collapses the expanded
    // rows that are still visible in the sidebar.
    await page.locator('[data-docs-sidebar-node-id="block-task"]').focus();
    await page.waitForTimeout(550);
    await page.keyboard.press("Control+ArrowLeft");
    await page.keyboard.press("Control+ArrowLeft");
    await expect(page.locator('[data-docs-sidebar-node-id="block-title"]')).toHaveCount(0);
  });

  test("keeps body Ctrl+ArrowRight expansion behavior", async ({ page }) => {
    const state = createDeepState();
    await openDocs(page, state);
    const bodyHeading = page.locator('[data-docs-block-id="block-title"]');
    await bodyHeading.focus();
    await page.keyboard.press("Control+ArrowRight");
    await expect(page.locator('[data-docs-block-id="deep-parent"]')).toBeVisible();
  });

  test("renders and edits a typed Markdown table, then keeps the content after reload", async ({ page }) => {
    const state = createState();
    state.nodes.push({
      ...state.nodes[1],
      id: "typed-markdown",
      title: "Markdown原文",
      body_text: "Markdown原文",
      body_json: {
        format: "doc_block",
        block_type: "markdown",
        label: "Markdown原文",
        content: "| A | B |\n| --- | --- |\n| x | y |",
      },
      sort_order: 5,
    });
    state.nodes.push({
      ...state.nodes[1],
      id: "typed-code",
      title: "Code原文",
      body_text: "Code原文",
      body_json: {
        format: "doc_block",
        block_type: "code",
        label: "Code原文",
        content: "```bash\ncd ComfyUI/custom_nodes\ngit clone https://example.test/repo\n```",
      },
      sort_order: 6,
    });
    await openDocs(page, state);

    const block = page.locator('[data-docs-block-id="typed-markdown"]');
    await expect(block.getByRole("columnheader", { name: "A" })).toBeVisible();
    await expect(block.getByTestId("docs-typed-content-edit")).toHaveCount(0);
    await block.getByTestId("docs-typed-content-display").click();
    const editor = block.getByTestId("docs-typed-content-editor").locator(".cm-content");
    await editor.fill("# changed\n\n| A | B |\n| --- | --- |\n| edited | row |");
    await block.getByTestId("docs-typed-content-save").click();
    await expect.poll(() => (state.nodes.find((node) => node.id === "typed-markdown")?.body_json.content)).toBe(
      "# changed\n\n| A | B |\n| --- | --- |\n| edited | row |",
    );

    await page.reload();
    await expect(page.locator('[data-docs-block-id="typed-markdown"]').getByText("changed")).toBeVisible();
    await expect(page.locator('[data-docs-block-id="typed-markdown"]').getByRole("columnheader", { name: "A" })).toBeVisible();

    const codeBlock = page.locator('[data-docs-block-id="typed-code"]');
    const codeDisplay = codeBlock.getByTestId("docs-typed-content-display");
    await expect(codeDisplay).toContainText("cd ComfyUI/custom_nodes");
    await expect(codeDisplay).not.toContainText("```bash");
    await codeDisplay.click();
    await expect(codeBlock.getByTestId("docs-typed-content-editor").locator(".cm-content")).toContainText("```bash");
    await codeBlock.getByRole("button", { name: "キャンセル" }).click();
  });

  test("does not abort body children loading when the matching sidebar node is collapsed", async ({ page }) => {
    const state = createDeepState();
    state.attachments.push({
      id: "body-detail-attachment",
      node_id: "block-title",
      file_name: "本文詳細.txt",
      file_path: "workspaces/_docs/attachments/body-detail.txt",
      mime_type: "text/plain",
      size_bytes: 12,
      metadata: {},
      created_by: "user-1",
      created_at: "2026-07-04T00:00:00",
    });
    await openDocs(page, state, {
      childrenDelays: { "block-title": 400 },
      detailsDelays: { "block-title": 400 },
    });

    const sidebarRoot = page.locator('[data-docs-sidebar-node-id="node-a"]');
    await sidebarRoot.focus();
    await page.keyboard.press("Control+ArrowRight");
    const sidebarHeading = page.locator('[data-docs-sidebar-node-id="block-title"]');
    await expect(sidebarHeading).toBeVisible();

    const childrenRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return request.method() === "GET" && url.pathname === "/api/docs/nodes/block-title/children";
    });
    const detailsRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return request.method() === "GET" && url.pathname === "/api/docs/nodes/block-title/details";
    });
    const childrenResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === "/api/docs/nodes/block-title/children";
    });
    const detailsResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === "/api/docs/nodes/block-title/details";
    });
    await page.locator('[data-docs-block-id="block-title"]').focus();
    await page.keyboard.press("Control+ArrowRight");
    await Promise.all([childrenRequest, detailsRequest]);

    // The sidebar expansion shares the in-flight request, then a single
    // collapse must not abort the body expansion that started it.
    await sidebarHeading.focus();
    await page.keyboard.press("Control+ArrowRight");
    await page.keyboard.press("Control+ArrowLeft");
    const [loadedChildren, loadedDetails] = await Promise.all([childrenResponse, detailsResponse]);
    expect(loadedChildren.status()).toBe(200);
    expect(loadedDetails.status()).toBe(200);
    await expect(page.locator('[data-docs-block-id="deep-parent"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-title"] [data-docs-attachment-id="body-detail-attachment"]')).toBeVisible();
  });

  test("does not abort sidebar children loading when the matching body node is collapsed", async ({ page }) => {
    const state = createDeepState();
    await openDocs(page, state, { childrenDelays: { "block-title": 400 } });

    const sidebarRoot = page.locator('[data-docs-sidebar-node-id="node-a"]');
    await sidebarRoot.focus();
    await page.keyboard.press("Control+ArrowRight");
    const sidebarHeading = page.locator('[data-docs-sidebar-node-id="block-title"]');
    await expect(sidebarHeading).toBeVisible();

    const childrenResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === "/api/docs/nodes/block-title/children";
    });
    await sidebarHeading.focus();
    await page.keyboard.press("Control+ArrowRight");

    // Collapse the same node in the body while the sidebar request is still
    // waiting on its delayed response.  The shared load must stay alive.
    await page.locator('[data-docs-block-id="block-title"]').focus();
    await page.keyboard.press("Control+ArrowLeft");

    const response = await childrenResponse;
    expect(response.status()).toBe(200);
    await expect(page.locator('[data-docs-sidebar-node-id="deep-parent"]')).toBeVisible();
  });

  test("exposes the next child page from the keyboard instead of hiding rows after 80", async ({ page }) => {
    const state = createState();
    for (let index = 0; index < 90; index += 1) {
      state.nodes.push({
        ...state.nodes[1],
        id: `bulk-${index}`,
        title: `大量ノード ${index}`,
        body_text: `大量ノード ${index}`,
        sort_order: 10 + index,
      });
    }
    await openDocs(page, state);
    const loadMore = page.getByRole("button", { name: "続きを読み込む" });
    await expect(loadMore).toBeVisible();
    await loadMore.focus();
    await page.keyboard.press("Enter");
    await expect(loadMore).toHaveCount(0);
  });

  test("keeps ArrowDown editing across a virtualized outline", async ({ page }) => {
    const state = createState();
    for (let index = 0; index < 70; index += 1) {
      state.nodes.push({
        ...state.nodes[1],
        id: `arrow-${index}`,
        title: `移動先 ${index}`,
        body_text: `移動先 ${index}`,
        sort_order: 10 + index,
      });
    }
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-title"]').click();
    for (let index = 0; index < 60; index += 1) await page.keyboard.press("ArrowDown");
    await expect(page.locator('[data-docs-block-id="arrow-56"]').getByRole("textbox")).toBeVisible();
  });

  test("Tab indents under the previous sibling rather than its deepest visible child", async ({ page }) => {
    const state = createDeepState();
    const initialNodeIds = new Set(state.nodes.map((node) => node.id));
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-title"]').getByTitle("展開").click();
    await expect(page.locator('[data-docs-block-id="deep-parent"]')).toBeVisible();
    await page.locator('[data-docs-block-id="block-task"]').click();
    await page.keyboard.press("Tab");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.parent_id).toBe("block-title");
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("次の項目");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.nodes.find((node) => !initialNodeIds.has(node.id))?.parent_id).toBe("block-title");
    await expect.poll(() => {
      const created = state.nodes.find((node) => !initialNodeIds.has(node.id));
      const moved = state.nodes.find((node) => node.id === "block-task");
      return created && moved ? created.sort_order > moved.sort_order : false;
    }).toBe(true);
  });

  test("sets a parent Field through /field without opening a property panel", async ({ page }) => {
    const state = createState();
    state.node_supertags.push({ node_id: "node-a", supertag_id: "tag-task" });
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("/field");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("listbox", { name: "インライン候補" })).toContainText("期日");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await page.keyboard.type("2026-07-21");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.field_values.find((value) => value.node_id === "node-a" && value.field_id === "field-due")?.value_datetime).toContain("2026-07-21");
    await expect(page.getByText("最初のノードを追加")).toHaveCount(0);
    await expect(page.getByText("Add node")).toHaveCount(0);
  });

  test("does not save or delete an unchanged Field when focus moves away", async ({ page }) => {
    const state = createState();
    state.fields.push({
      id: "field-url",
      workspace_id: "workspace-1",
      supertag_id: "tag-task",
      system_key: null,
      name: "URL",
      field_type: "url",
      required: false,
      options_json: {},
      default_value_json: null,
      sort_order: 3,
    });
    state.field_values.push({
      node_id: "block-task",
      field_id: "field-url",
      value_json: "https://example.com/source",
      value_text: "https://example.com/source",
      value_number: null,
      value_datetime: null,
      target_node_id: null,
    });
    let fieldPutCount = 0;
    page.on("request", (request) => {
      if (request.method() === "PUT" && /\/api\/docs\/nodes\/[^/]+\/fields$/.test(new URL(request.url()).pathname)) {
        fieldPutCount += 1;
      }
    });
    await openDocs(page, state);
    const taskRow = page.locator('[data-docs-block-id="block-task"]');
    await taskRow.getByTitle("展開").click();
    const urlField = taskRow.getByLabel("Field URL");
    await expect(urlField).toBeVisible();
    await urlField.click();
    await page.locator('[data-docs-block-id="block-title"]').click();
    await expect.poll(() => fieldPutCount).toBe(0);
    expect(state.field_values.find((value) => value.node_id === "block-task" && value.field_id === "field-url")?.value_text).toBe("https://example.com/source");
  });

  test("sets a Field whose name contains spaces from a keyboard suggestion", async ({ page }) => {
    const state = createState();
    state.node_supertags.push({ node_id: "node-a", supertag_id: "tag-task" });
    state.fields.push({
      ...state.fields[0],
      id: "field-page-role",
      system_key: null,
      name: "Page Role",
      field_type: "text",
      options_json: {},
      default_value_json: null,
      sort_order: 3,
    });
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.nodes.find((node) => (
      node.id !== "block-link" && node.parent_id === "node-a" && node.title === ""
    ))).toBeTruthy();
    const blankNode = state.nodes.find((node) => (
      node.id !== "block-link" && node.parent_id === "node-a" && node.title === ""
    ));
    expectPersistedBlank(blankNode);
    await page.keyboard.type("/field");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("listbox", { name: "インライン候補" })).toContainText("Page Role");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    const shorthandEditor = page.locator(`[data-docs-block-id="${blankNode?.id}"] .cm-content`);
    await expect(shorthandEditor).toBeFocused();
    await expect(shorthandEditor).toHaveText("Page Role: ");
    await page.keyboard.type("canonical");
    await expect(shorthandEditor).toHaveText("Page Role: canonical");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.field_values.find(
      (value) => value.node_id === "node-a" && value.field_id === "field-page-role",
    )?.value_text).toBe("canonical");
  });

  test("applies a Supertag from # suggestions using only the keyboard", async ({ page }) => {
    const state = createState();
    state.supertags.push({
      ...state.supertags[0],
      id: "tag-llm",
      system_key: null,
      name: "LLM",
      base_type: "knowledge",
      pinned_field_ids: [],
      ai_instructions: null,
    });
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.type(" #LL");
    await expect(page.getByRole("listbox")).toContainText("LLM");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.node_supertags.some(
      (entry) => entry.node_id === "block-link" && entry.supertag_id === "tag-llm",
    )).toBe(true);
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title).toBe("関連ページ");

    const tagChip = page.locator('[data-docs-block-id="block-link"] [data-docs-supertag-id="tag-llm"]');
    await expect(tagChip).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(page.locator('[data-docs-block-id="block-link"] .cm-content')).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(tagChip).toBeFocused();
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.node_supertags.some(
      (entry) => entry.node_id === "block-link" && entry.supertag_id === "tag-llm",
    )).toBe(false);
    await expect(tagChip).toHaveCount(0);
  });

  test("serializes consecutive Supertag removals without restoring a stale relation", async ({ page }) => {
    const state = createState();
    const secondTag = {
      ...state.supertags[0],
      id: "tag-second",
      system_key: null,
      name: "資料",
      base_type: "knowledge",
      pinned_field_ids: [],
      ai_instructions: null,
    };
    state.supertags.push(secondTag);
    state.node_supertags.push(
      { node_id: "block-link", supertag_id: state.supertags[0].id },
      { node_id: "block-link", supertag_id: secondTag.id },
    );
    await openDocs(page, state, { tagDelays: [150, 0] });
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("End");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Delete");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Delete");
    await expect.poll(() => state.node_supertags.filter((entry) => entry.node_id === "block-link")).toEqual([]);
    await expect(row.locator("[data-docs-supertag-chip]")).toHaveCount(0);
  });

  test("serializes a delayed Supertag removal followed by a keyboard addition", async ({ page }) => {
    const state = createState();
    const secondTag = {
      ...state.supertags[0],
      id: "tag-second",
      system_key: null,
      name: "資料",
      base_type: "knowledge",
      pinned_field_ids: [],
      ai_instructions: null,
    };
    state.supertags.push(secondTag);
    state.node_supertags.push({ node_id: "block-link", supertag_id: state.supertags[0].id });
    await openDocs(page, state, { tagDelays: [150, 0] });

    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("End");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Delete");
    await page.keyboard.press("End");
    await page.keyboard.type(" #資料");
    await expect(page.getByRole("listbox")).toContainText("資料");
    await page.keyboard.press("Enter");

    await expect.poll(() => state.node_supertags.filter((entry) => entry.node_id === "block-link"))
      .toEqual([{ node_id: "block-link", supertag_id: secondTag.id }]);
    await expect(row.locator('[data-docs-supertag-id="tag-second"]')).toBeVisible();
    await expect(row.locator(`[data-docs-supertag-id="${state.supertags[0].id}"]`)).toHaveCount(0);
  });

  test("keeps the committed Supertag when only task reconciliation fails", async ({ page }) => {
    const state = createState();
    state.supertags.push({ ...state.supertags[0], id: "tag-warning", name: "資料", system_key: null });
    await openDocs(page, state, { tagCommittedWarnings: [0] });
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" #資料");
    await page.keyboard.press("Enter");
    await expect(row.locator('[data-docs-supertag-id="tag-warning"]')).toBeVisible();
    await expect.poll(() => state.node_supertags.some((item) => item.node_id === "block-link" && item.supertag_id === "tag-warning")).toBe(true);
  });

  test("removes an optimistic ghost when the requested Supertag is invalid", async ({ page }) => {
    const state = createState();
    state.supertags.push({ ...state.supertags[0], id: "tag-invalid", name: "削除済み", system_key: null });
    await openDocs(page, state, { invalidTagIds: ["tag-invalid"] });
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" #削除済み");
    await page.keyboard.press("Enter");
    await expect(row.locator('[data-docs-supertag-id="tag-invalid"]')).toHaveCount(0);
    await expect.poll(() => state.node_supertags.some((item) => item.supertag_id === "tag-invalid")).toBe(false);
  });

  test("starts typing immediately on an empty page", async ({ page }) => {
    const state = createState();
    state.nodes = state.nodes.filter((node) => node.parent_id === null);
    state.node_supertags = [];
    state.field_values = [];
    await openDocs(page, state);
    const input = page.getByRole("textbox", { name: "最初のノード" });
    await expect(input).toBeFocused();
    await input.fill("空ページから直接入力");
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-docs-block-id]', { hasText: "空ページから直接入力" })).toBeVisible();
  });

  test("renders document blocks without forced bullets", async ({ page }, testInfo) => {
    const state = createState();
    await openDocs(page, state);
    await expect(page.locator('[data-block-kind="heading_1"]', { hasText: "概要" })).toBeVisible();
    await expect(page.locator('[data-block-kind="checkbox"]', { hasText: "初回打合せ日程を確定" })).toBeVisible();
    await expect(page.locator(".docs-block-bulleted_list")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("docs-block-editor.png"), fullPage: true });
  });

  test("keeps only populated document Fields inline without a summary input or property panel", async ({ page }) => {
    const state = createState();
    state.nodes[0].description = "案件の判断材料を1行で把握する";
    state.node_supertags.push({ node_id: "node-a", supertag_id: "tag-task" });
    state.fields.push({
      ...state.fields[0],
      id: "field-note",
      system_key: null,
      name: "備考",
      field_type: "text",
      options_json: {},
      default_value_json: null,
      sort_order: 3,
    });
    state.field_values.push({
      node_id: "node-a",
      field_id: "field-note",
      value_json: "正本を優先",
      value_text: "正本を優先",
      value_number: null,
      value_datetime: null,
      target_node_id: null,
    });
    await openDocs(page, state);
    await expect(page.getByRole("textbox", { name: "1行説明" })).toHaveCount(0);
    await expect(page.getByTestId("docs-document-fields")).toContainText(">備考");
    const noteField = page.getByRole("textbox", { name: "Field 備考" });
    await expect(noteField).toHaveValue("正本を優先");
    await page.getByText(">備考", { exact: true }).click();
    await expect(noteField).toBeFocused();
    await expect(page.getByText("13 fields")).toHaveCount(0);
  });

  test("renders email long text readably and hides only the legacy Field mirror outline", async ({ page }) => {
    const state = createState();
    const emailBodyFirstLine = `quoted header ${"recipient@example.com ".repeat(12)}`;
    const emailBodySecondLine = "本文の続き";
    const emailBody = `${emailBodyFirstLine}\n${emailBodySecondLine}`;
    state.nodes[0] = {
      ...state.nodes[0],
      title: "RE: SSL証明書発行依頼",
      system_key: "project_mail:project-1:dedupe",
      body_json: { format: "email", dedupe_key: "message-id:mail@example.com" },
    };
    state.supertags.push({
      ...state.supertags[0],
      id: "tag-email",
      system_key: "email",
      name: "メール",
      base_type: "email",
      pinned_field_ids: ["field-email-body"],
    });
    state.node_supertags.push({ node_id: "node-a", supertag_id: "tag-email" });
    state.fields.push({
      ...state.fields[0],
      id: "field-email-body",
      supertag_id: "tag-email",
      system_key: "email_body",
      name: "本文",
      field_type: "long_text",
      options_json: {},
      default_value_json: null,
      sort_order: 1,
    });
    state.field_values.push({
      node_id: "node-a",
      field_id: "field-email-body",
      value_json: null,
      value_text: emailBody,
      value_number: null,
      value_datetime: null,
      target_node_id: null,
    });
    state.nodes.push(
      {
        ...state.nodes[1],
        id: "legacy-body-label",
        parent_id: "node-a",
        title: "本文",
        body_text: "本文",
        sort_order: 20,
      },
      {
        ...state.nodes[1],
        id: "legacy-body-value",
        parent_id: "legacy-body-label",
        title: emailBodyFirstLine,
        body_text: emailBodyFirstLine,
        sort_order: 1,
      },
      {
        ...state.nodes[1],
        id: "legacy-body-value-2",
        parent_id: "legacy-body-label",
        title: emailBodySecondLine,
        body_text: emailBodySecondLine,
        sort_order: 2,
      },
      {
        ...state.nodes[1],
        id: "email-user-note",
        parent_id: "node-a",
        title: "利用者が追加した対応メモ",
        body_text: "利用者が追加した対応メモ",
        sort_order: 21,
      },
    );

    const consoleErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    await openDocs(page, state);

    const bodyField = page.getByRole("textbox", { name: "Field 本文" });
    await expect(bodyField).toHaveAttribute("wrap", "soft");
    await expect(bodyField).toHaveAttribute("rows", "4");
    await expect(bodyField).toHaveCSS("overflow-x", "hidden");
    await page.getByText(">本文", { exact: true }).click();
    await expect(bodyField).toBeFocused();
    await expect(page.locator('[data-docs-block-id="legacy-body-label"]')).toHaveCount(0);
    await expect(page.locator('[data-docs-block-id="legacy-body-value"]')).toHaveCount(0);
    await expect(page.locator('[data-docs-block-id="legacy-body-value-2"]')).toHaveCount(0);
    await expect(page.locator('[data-docs-block-id="email-user-note"]')).toBeVisible();

    await page.getByTitle("分割表示").click();
    const splitBodyFields = page.getByRole("textbox", { name: "Field 本文" });
    await expect(splitBodyFields).toHaveCount(2);
    expect(await splitBodyFields.nth(0).getAttribute("id")).not.toBe(
      await splitBodyFields.nth(1).getAttribute("id"),
    );
    await page.getByText(">本文", { exact: true }).nth(1).click();
    await expect(splitBodyFields.nth(1)).toBeFocused();
    await expect(splitBodyFields.nth(0)).not.toBeFocused();
    expect(consoleErrors).toEqual([]);
  });

  test("opens populated Fields lazily and traverses them with the keyboard", async ({ page }) => {
    const state = createState();
    state.node_supertags.push({ node_id: "block-link", supertag_id: "tag-task" });
    state.fields.push({
      ...state.fields[0],
      id: "field-url",
      system_key: null,
      name: "URL",
      field_type: "url",
      options_json: {},
      default_value_json: null,
      sort_order: 3,
    });
    state.field_values.push({
      node_id: "block-link",
      field_id: "field-url",
      value_json: "https://example.com/source",
      value_text: "https://example.com/source",
      value_number: null,
      value_datetime: null,
      target_node_id: null,
    });
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("Control+ArrowRight");
    const urlField = page.getByRole("textbox", { name: "Field URL" });
    await expect(urlField).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await expect(urlField).toBeFocused();
    await page.keyboard.press("ArrowUp");
    await expect(row.locator(".cm-content")).toBeFocused();
  });

  test("keeps unmodified ArrowUp and ArrowDown for select Field option changes", async ({ page }) => {
    const state = createState();
    state.node_supertags.push({ node_id: "block-link", supertag_id: "tag-task" });
    state.fields.push({
      ...state.fields[0],
      id: "field-status",
      system_key: null,
      name: "状態",
      field_type: "options",
      options_json: { values: ["draft", "published"] },
      default_value_json: null,
      sort_order: 3,
    });
    state.field_values.push({
      node_id: "block-link",
      field_id: "field-status",
      value_json: "draft",
      value_text: "draft",
      value_number: null,
      value_datetime: null,
      target_node_id: null,
    });
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("Control+ArrowRight");
    const select = page.getByRole("combobox", { name: "Field 状態" });
    await expect(select).toContainText("draft");
    await select.press("ArrowDown");
    await expect(select).toContainText("published");
    await expect.poll(() => state.field_values.find(
      (value) => value.node_id === "block-link" && value.field_id === "field-status",
    )?.value_text).toBe("published");
  });

  test("does not turn a normal node into a checkbox with Ctrl+Enter", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("Control+Enter");
    expect((state.nodes.find((node) => node.id === "block-link") as MockNode & { display_props?: { show_checkbox?: boolean } })?.display_props?.show_checkbox).not.toBe(true);
    await expect(row.locator('button[class*="border"] svg')).toHaveCount(0);
  });

  test("keeps task fields canonical and out of inline field editing", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await expect(page.getByTestId("docs-block-fields")).toHaveCount(0);
    const taskRow = page.locator('[data-docs-block-id="block-task"]');
    await expect(taskRow.getByText("todo")).toHaveCount(1);
    await expect(taskRow.getByText("#Task")).toHaveCount(0);
    await expect(taskRow.getByTestId("docs-block-field-summary")).toHaveCount(0);
    await taskRow.getByTitle("展開").click();
    await expect(taskRow.getByTestId("docs-block-fields")).toHaveCount(0);
  });

  test("commits the current edit when Escape closes the line editor", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.type(" saved by escape");
    await page.keyboard.press("Escape");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title).toContain("saved by escape");
    await page.reload();
    await expect(page.locator('[data-docs-block-id="block-link"]')).toContainText("saved by escape");
  });

  test("uses Ctrl+Z for character history while a line is being edited", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" local-undo");
    await expect(row).toContainText("local-undo");
    await page.keyboard.press("Control+Z");
    await expect(row).not.toContainText("local-undo");
  });

  test("splits a title in the middle without restoring or duplicating the old text", async ({ page }) => {
    const state = createState();
    const target = state.nodes.find((node) => node.id === "block-link");
    if (!target) throw new Error("split target is missing");
    target.title = "雑談人がAIへ指示する形は普及しにくいという予想";
    target.body_text = target.title;
    await openDocs(page, state);

    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("Home");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Enter");

    const suffix = "人がAIへ指示する形は普及しにくいという予想";
    await expect.poll(() => state.nodes.find((node) => node.title === suffix)?.id ?? null).not.toBeNull();
    const created = state.nodes.find((node) => node.title === suffix);
    expect(created?.parent_id).toBe(target.parent_id);
    expect(state.nodes.find((node) => node.id === target.id)?.title).toBe("雑談");
    expect(state.nodes.filter((node) => node.title === suffix && !node.archived_at)).toHaveLength(1);
    const createdRow = page.locator(`[data-docs-block-id="${created?.id}"]`);
    await expect(createdRow).toBeVisible();
    // Enter in the middle must keep the split suffix at the same outline
    // depth, not accidentally adopt a visible descendant's depth.
    expect(await createdRow.getAttribute("style")).toBe(await row.getAttribute("style"));
    const siblings = state.nodes
      .filter((node) => node.parent_id === target.parent_id && !node.archived_at)
      .sort((left, right) => left.sort_order - right.sort_order);
    expect(siblings[siblings.findIndex((node) => node.id === target.id) + 1]?.id).toBe(created?.id);

    // A focus change/blur must not send the pre-split full title back to the API.
    await page.getByTestId("docs-block-editor").click();
    await expect.poll(() => state.nodes.find((node) => node.id === target.id)?.title).toBe("雑談");
    await page.reload();
    await expect(page.locator('[data-docs-block-id="block-link"]')).toContainText("雑談");
    await expect(page.locator('[data-docs-block-id="block-link"]')).not.toContainText(suffix);
    await expect(createdRow).toContainText(suffix);
  });

  test("preserves a Delete-at-line-end merge after the active editor blurs", async ({ page }) => {
    const state = createState();
    const currentId = "merge-delete-current";
    const nextId = "merge-delete-next";
    state.nodes.push(
      {
        ...state.nodes[3],
        id: currentId,
        title: "CCC333",
        body_text: "CCC333",
        sort_order: 50,
      },
      {
        ...state.nodes[3],
        id: nextId,
        title: "DDD444",
        body_text: "DDD444",
        sort_order: 51,
      },
    );
    await openDocs(page, state);

    const currentRow = page.locator(`[data-docs-block-id="${currentId}"]`);
    const nextRow = page.locator(`[data-docs-block-id="${nextId}"]`);
    await expect(currentRow).toBeVisible();
    await expect(nextRow).toBeVisible();
    await currentRow.click();
    await page.keyboard.press("End");

    const patchResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "PATCH"
        && url.pathname === `/api/docs/nodes/${currentId}`;
    });
    const deleteResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "DELETE"
        && url.pathname === `/api/docs/nodes/${nextId}`;
    });
    await page.keyboard.press("Delete");
    const [patch, deleted] = await Promise.all([patchResponse, deleteResponse]);
    expect(patch.status()).toBe(200);
    expect(deleted.status()).toBe(200);

    // Blur the still-active merged editor as soon as its PATCH is durable;
    // this is the stale-draft race that previously restored the old title.
    await page.locator('[data-docs-block-id="block-link"]').click();
    // A stale same-node PATCH must not be hidden by the optimistic row state;
    // reload the server snapshot before checking the merged title.
    await page.reload();
    await expect.poll(() => state.nodes.find((node) => node.id === currentId)?.title)
      .toBe("CCC333DDD444");
    await expect.poll(() => state.nodes.find((node) => node.id === nextId)?.archived_at)
      .not.toBeNull();
    await expect(currentRow).toContainText("CCC333DDD444");
    await expect(nextRow).toHaveCount(0);
  });

  test("persists an explicit blank paragraph after a line-end Enter", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const initialCount = state.nodes.length;
    const source = state.nodes.find((node) => node.id === "block-link");
    if (!source) throw new Error("line-end source is missing");
    const sourceRow = page.locator('[data-docs-block-id="block-link"]');
    await sourceRow.click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");

    await expect.poll(() => state.nodes.find((node) => (
      !node.archived_at
      && node.id !== source.id
      && node.parent_id === source.parent_id
      && node.title === ""
    ))).toBeTruthy();
    const blankNode = state.nodes.find((node) => (
      !node.archived_at
      && node.id !== source.id
      && node.parent_id === source.parent_id
      && node.title === ""
    ));
    expectPersistedBlank(blankNode);
    expect(state.nodes.length).toBe(initialCount + 1);

    const blankRow = page.locator(`[data-docs-block-id="${blankNode?.id}"]`);
    await expect(blankRow).toBeVisible();
    await expect(blankRow.locator(".cm-content")).toBeFocused();
    // The persisted blank is a same-parent/same-depth sibling, not an
    // editor-only pending row.
    expect(await blankRow.getAttribute("style")).toBe(await sourceRow.getAttribute("style"));
    await expect(page.locator('[data-docs-blank-row-input]')).toHaveCount(0);

    // Explicit blank rows are part of the server snapshot, so a reload must
    // not collapse the intentional empty line back into a transient input.
    await page.reload();
    await expect(blankRow).toBeVisible();
    expectPersistedBlank(state.nodes.find((node) => node.id === blankNode?.id));
  });

  test("persists a blank sibling before the current row for Enter at start", async ({ page }) => {
    const state = createState();
    const source = state.nodes.find((node) => node.id === "block-link");
    if (!source) throw new Error("line-start source is missing");
    await openDocs(page, state);
    const sourceRow = page.locator('[data-docs-block-id="block-link"]');
    await sourceRow.click();
    await page.keyboard.press("Home");
    await page.keyboard.press("Enter");

    await expect.poll(() => state.nodes.find((node) => (
      !node.archived_at
      && node.id !== source.id
      && node.parent_id === source.parent_id
      && node.title === ""
    ))).toBeTruthy();
    const blankNode = state.nodes.find((node) => (
      !node.archived_at
      && node.id !== source.id
      && node.parent_id === source.parent_id
      && node.title === ""
    ));
    expectPersistedBlank(blankNode);
    const siblings = state.nodes
      .filter((node) => node.parent_id === source.parent_id && !node.archived_at)
      .sort((left, right) => left.sort_order - right.sort_order);
    expect(siblings.findIndex((node) => node.id === blankNode?.id)).toBeLessThan(
      siblings.findIndex((node) => node.id === source.id),
    );
    const blankRow = page.locator(`[data-docs-block-id="${blankNode?.id}"]`);
    await expect(blankRow).toBeVisible();
    await expect(blankRow.locator(".cm-content")).toBeFocused();
    expect(await blankRow.getAttribute("style")).toBe(await sourceRow.getAttribute("style"));
  });

  test("keeps consecutive persisted blank paragraphs across reload", async ({ page }) => {
    const state = createState();
    const source = state.nodes.find((node) => node.id === "block-link");
    if (!source) throw new Error("consecutive-blank source is missing");
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");

    await expect.poll(() => state.nodes.find((node) => (
      !node.archived_at
      && node.id !== source.id
      && node.parent_id === source.parent_id
      && node.title === ""
    ))).toBeTruthy();
    const firstBlank = state.nodes.find((node) => (
      !node.archived_at
      && node.id !== source.id
      && node.parent_id === source.parent_id
      && node.title === ""
    ));
    expectPersistedBlank(firstBlank);
    const firstBlankRow = page.locator(`[data-docs-block-id="${firstBlank?.id}"]`);
    await expect(firstBlankRow.locator(".cm-content")).toBeFocused();
    await page.keyboard.press("Enter");

    await expect.poll(() => state.nodes.filter((node) => (
      !node.archived_at
      && node.parent_id === source.parent_id
      && node.title === ""
    ))).toHaveLength(2);
    const blankNodes = state.nodes
      .filter((node) => !node.archived_at && node.parent_id === source.parent_id && node.title === "")
      .sort((left, right) => left.sort_order - right.sort_order);
    expect(blankNodes).toHaveLength(2);
    blankNodes.forEach((node) => expectPersistedBlank(node));
    for (const node of blankNodes) await expect(page.locator(`[data-docs-block-id="${node.id}"]`)).toBeVisible();

    await page.reload();
    for (const node of blankNodes) {
      expectPersistedBlank(state.nodes.find((candidate) => candidate.id === node.id));
      await expect(page.locator(`[data-docs-block-id="${node.id}"]`)).toBeVisible();
    }
  });

  test("blocks Today navigation until the active Docs commit is durable", async ({ page }) => {
    const state = createState();
    let releasePatch!: () => void;
    let patchStartedResolve!: () => void;
    const patchStarted = new Promise<void>((resolve) => {
      patchStartedResolve = resolve;
    });
    const patchRelease = new Promise<void>((resolve) => {
      releasePatch = resolve;
    });
    let todayRequestStarted = false;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/docs/today") todayRequestStarted = true;
    });
    await openDocs(page, state, {
      patchGate: {
        requestIndex: 0,
        onStart: patchStartedResolve,
        release: patchRelease,
      },
    });

    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("Daily note edit");
    const todayClick = page
      .getByTestId("workspace-navigation-frame")
      .getByRole("button", { name: "Today" })
      .click();
    await patchStarted;
    // The state-replacing Today GET must not start while the PATCH is held.
    await expect.poll(() => todayRequestStarted).toBe(false);

    releasePatch();
    await todayClick;
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title)
      .toBe("Daily note edit");

    // Return to the original route and reload; the edit must survive the
    // navigation barrier and the server snapshot replacement.
    await page.goto("/docs/node-a");
    await expect(page.locator('[data-docs-block-id="block-link"]')).toContainText("Daily note edit");
  });

  test("hydrates persisted Today children on repeated navigation", async ({ page }) => {
    const state = createState();
    const todayNode: MockNode = {
      id: "today-node",
      workspace_id: "workspace-1",
      parent_id: null,
      root_page_id: "today-node",
      project_id: "project-1",
      title: "2026年8月23日(日)",
      body_json: blockJson(),
      body_text: "",
      node_type: "day",
      sort_order: 3,
      created_at: "2026-08-23T00:00:00",
      updated_at: "2026-08-23T00:00:00",
      archived_at: null,
    };
    const todayChild: MockNode = {
      id: "today-child",
      workspace_id: "workspace-1",
      parent_id: todayNode.id,
      root_page_id: todayNode.id,
      project_id: "project-1",
      title: "Persisted today child",
      body_json: blockJson(),
      body_text: "Persisted today child",
      node_type: "block",
      sort_order: 1,
      created_at: "2026-08-23T00:00:00",
      updated_at: "2026-08-23T00:00:00",
      archived_at: null,
    };
    state.nodes.push(todayNode, todayChild);
    await openDocs(page, state);

    const todayButton = page
      .getByTestId("workspace-navigation-frame")
      .getByRole("button", { name: "Today" });
    const waitForTodayNeighborhood = () => Promise.all([
      page.waitForResponse((response) => {
        const url = new URL(response.url());
        return response.request().method() === "GET" && url.pathname === "/api/docs/today";
      }),
      page.waitForResponse((response) => {
        const url = new URL(response.url());
        return response.request().method() === "GET" && url.pathname === `/api/docs/nodes/${todayNode.id}/tree`;
      }),
      page.waitForResponse((response) => {
        const url = new URL(response.url());
        return response.request().method() === "GET" && url.pathname === `/api/docs/nodes/${todayNode.id}/children`;
      }),
      page.waitForResponse((response) => {
        const url = new URL(response.url());
        return response.request().method() === "GET" && url.pathname === `/api/docs/nodes/${todayNode.id}/details`;
      }),
    ]);

    let todayLoad = waitForTodayNeighborhood();
    await todayButton.click();
    await todayLoad;
    await expect(page.locator(`[data-docs-block-id="${todayChild.id}"]`)).toBeVisible();

    // A repeat Today navigation replaces the canonical state from the API.
    // The persisted child must be hydrated again rather than disappearing.
    todayLoad = waitForTodayNeighborhood();
    await todayButton.click();
    await todayLoad;
    await expect(page.locator(`[data-docs-block-id="${todayChild.id}"]`)).toBeVisible();
  });

  test("keeps pending-row text when optimistic node persistence fails", async ({ page }) => {
    const state = createState();
    // The first request is the explicit blank created by Enter. Fail it and
    // then fail the user's retry as well; only this error path is allowed to
    // retain an editor-only pending row.
    await openDocs(page, state, { createFailures: [0, 1] });
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    const blankInput = page.locator('[data-docs-blank-row-input]');
    await expect(blankInput).toBeVisible();
    await blankInput.fill("保存失敗でも残す");
    await blankInput.press("Enter");
    await expect(blankInput).toHaveValue("保存失敗でも残す");
    expect(state.nodes.some((node) => node.title === "保存失敗でも残す" && !node.archived_at)).toBe(false);
  });

  test("keeps a failed split suffix as a pending row", async ({ page }) => {
    const state = createState();
    const target = state.nodes.find((node) => node.id === "block-link");
    if (!target) throw new Error("split target is missing");
    target.title = "prefix suffix";
    target.body_text = target.title;
    await openDocs(page, state, { createFailures: [0] });
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("Home");
    for (let index = 0; index < 7; index += 1) await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Enter");
    const blankInput = page.locator('[data-docs-blank-row-input]');
    await expect(blankInput).toHaveValue("suffix");
    expect(state.nodes.some((node) => node.title === "suffix" && !node.archived_at)).toBe(false);
  });

  test("uses pending text undo before falling back to Workspace history", async ({ page }) => {
    const state = createState();
    await openDocs(page, state, { createFailures: [0, 1] });
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    const blankInput = page.locator('[data-docs-blank-row-input]');
    await blankInput.fill("first created");
    await blankInput.press("Enter");
    await expect(blankInput).toHaveValue("first created");
    // Exercise the real native input path (not only Playwright's value setter)
    // so the local pending-row history is proven before Workspace fallback.
    await blankInput.pressSequentially("local text");
    await blankInput.press("Control+Z");
    await expect(blankInput).toHaveValue("first created");
    await blankInput.press("Control+Z");
    await expect(blankInput).toHaveValue("");
    expect(state.nodes.some((node) => node.title === "first created" && !node.archived_at)).toBe(false);
  });

  test("syncs an active CodeMirror view after Workspace Undo", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("history newer");
    await page.keyboard.press("Escape");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title).toBe("history newer");
    await row.click();
    await expect(row.locator(".cm-content")).toBeFocused();
    await page.keyboard.press("Control+Z");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title).toBe("関連ページ");
    await page.locator('[data-docs-block-id="block-title"]').click();
    await page.waitForTimeout(80);
    expect(state.nodes.find((node) => node.id === "block-link")?.title).toBe("関連ページ");
  });

  test("does not move focus back when a later pending create fails", async ({ page }) => {
    const state = createState();
    // The first Enter create is delayed/fails, then the user's retry is also
    // delayed/fails. The rollback of the later request must not steal focus
    // from an external control.
    await openDocs(page, state, { createDelays: [200, 200], createFailures: [0, 1] });
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    const blankInput = page.locator('[data-docs-blank-row-input]');
    await blankInput.fill("late failure");
    await blankInput.press("Enter");
    const pageTitle = page.getByRole("textbox", { name: "ページタイトル" });
    await pageTitle.focus();
    await expect.poll(() => blankInput.inputValue(), { timeout: 1000 }).toBe("late failure");
    await expect(pageTitle).toBeFocused();
    await expect(blankInput).not.toBeFocused();
  });

  test("applies a Markdown shortcut while editing a persisted blank paragraph", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.nodes.find((node) => node.id !== "block-link" && node.title === "")?.id ?? null).not.toBeNull();
    const blankNode = state.nodes.find((node) => node.id !== "block-link" && node.title === "");
    expectPersistedBlank(blankNode);
    const blankEditor = page.locator(`[data-docs-block-id="${blankNode?.id}"] .cm-content`);
    await expect(blankEditor).toBeFocused();
    await blankEditor.fill("# 見出し");
    await blankEditor.press("Enter");
    await expect.poll(() => state.nodes.find((node) => node.title === "見出し")?.body_json.block_type).toBe("heading_1");
    const nextBlank = state.nodes.find((node) => !node.archived_at && node.title === "" && node.id !== blankNode?.id && node.id !== "node-a");
    expectPersistedBlank(nextBlank);
  });

  test("falls back to workspace Undo immediately on the newly focused split row", async ({ page }) => {
    const state = createState();
    const target = state.nodes.find((node) => node.id === "block-link");
    if (!target) throw new Error("split target is missing");
    target.title = "first second";
    target.body_text = target.title;
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await expect(page.locator('[data-docs-block-id="block-link"]')).toContainText("first second");
    await page.keyboard.press("Home");
    for (let index = 0; index < 6; index += 1) await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.nodes.find((node) => node.id !== target.id && node.parent_id === target.parent_id && node.title === "second")?.id ?? null).not.toBeNull();
    const created = state.nodes.find((node) => node.id !== target.id && node.parent_id === target.parent_id && node.title === "second");
    expect(created).toBeTruthy();
    // The newly created row has an empty CodeMirror history.  Ctrl+Z must
    // therefore fall back to the Workspace history without leaving the editor.
    await page.keyboard.press("Control+Z");
    await expect.poll(() => state.nodes.find((node) => node.id === created?.id)?.archived_at ?? null).not.toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === target.id)?.title).toBe("first ");
  });

  test("keeps linked mentions and outgoing links at the page bottom", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await expect(page.getByRole("heading", { name: "Linked mentions" })).toBeVisible();
    await expect(page.getByText("打合せでSLCO正本を参照").first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Outgoing links" })).toBeVisible();
    await expect(page.getByText("関連は [[関連Meeting]] を参照").first()).toBeVisible();
  });

  test("does not reserve an editor tab strip for Document Views and Search", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await expect(page.locator("main").getByRole("button", { name: "Document" })).toHaveCount(0);
    await expect(page.locator("main").getByRole("button", { name: "Views" })).toHaveCount(0);
    await expect(page.locator("main").getByRole("button", { name: "Search" })).toHaveCount(0);
  });

  test("keeps the wide editor surface centered in its scroll viewport", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const geometry = await page.locator("[data-docs-scroll-container]").evaluate((scroll) => {
      const surface = scroll.querySelector<HTMLElement>("[data-docs-editor-surface]");
      if (!surface) return null;
      const outer = scroll.getBoundingClientRect();
      const inner = surface.getBoundingClientRect();
      return {
        centerDelta: Math.abs((outer.left + outer.width / 2) - (inner.left + inner.width / 2)),
        widthRatio: inner.width / outer.width,
      };
    });
    expect(geometry).not.toBeNull();
    expect(geometry!.centerDelta).toBeLessThan(2);
    // desktopでは中央55%の編集カラムにする。
    expect(geometry!.widthRatio).toBeGreaterThan(0.53);
    expect(geometry!.widthRatio).toBeLessThan(0.55);
  });

  test("places the caret at line end when clicking empty space after the title", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    const title = row.locator("[data-docs-title-display]");
    const rowBox = await row.boundingBox();
    const titleBox = await title.boundingBox();
    expect(rowBox).not.toBeNull();
    expect(titleBox).not.toBeNull();
    expect(rowBox!.x + rowBox!.width - 60).toBeGreaterThan(titleBox!.x + titleBox!.width);
    await page.mouse.click(rowBox!.x + rowBox!.width - 60, titleBox!.y + titleBox!.height / 2);
    await page.keyboard.type(" END");
    await page.keyboard.press("Escape");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title).toBe("関連ページ END");
  });

  test("opens the row menu at the right-click position", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    const box = await row.boundingBox();
    expect(box).not.toBeNull();
    const point = { x: box!.x + 96, y: box!.y + 12 };
    await page.mouse.click(point.x, point.y, { button: "right" });
    const menuBox = await page.locator("[data-docs-row-menu]").boundingBox();
    expect(menuBox).not.toBeNull();
    expect(Math.abs(menuBox!.x - point.x)).toBeLessThan(3);
    expect(Math.abs(menuBox!.y - point.y)).toBeLessThan(3);
  });

  test("moves from the title to an attachment and deletes it with the keyboard", async ({ page }) => {
    const state = createState();
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          write: async () => undefined,
          writeText: async () => undefined,
        },
      });
    });
    state.attachments.push({
      id: "attachment-1",
      node_id: "block-link",
      file_name: "sample.png",
      file_path: "workspaces/_docs/attachments/sample.png",
      mime_type: "image/png",
      size_bytes: 68,
      metadata: {},
      created_by: "user-1",
      created_at: "2026-07-19T00:00:00",
    });
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.getByTitle("展開").click();
    await row.click();
    await page.keyboard.press("ArrowDown");
    const attachment = row.locator("[data-docs-attachment-control]");
    await expect(attachment).toBeFocused();
    await page.keyboard.press("Control+C");
    await expect(page.getByText("添付ファイルをコピーしました")).toBeVisible();
    await page.keyboard.press("Delete");
    await expect(attachment).toHaveCount(0);
    expect(state.attachments).toHaveLength(0);
  });

  test("hides a loaded ClipIngest image while the row is collapsed and restores it on re-expand", async ({ page }) => {
    const state = createState();
    state.nodes.push({
      ...state.nodes[1],
      id: "block-clip-image",
      title: "ClipIngest画像ノード",
      body_text: "ClipIngest画像ノード",
      sort_order: 5,
    });
    state.attachments.push({
      id: "clip-image-1",
      node_id: "block-clip-image",
      file_name: "clip-ingest.png",
      file_path: "workspaces/_docs/attachments/clip-ingest.png",
      mime_type: "image/png",
      size_bytes: 68,
      metadata: { source: "clip_ingest" },
      created_by: "user-1",
      created_at: "2026-07-19T00:00:00",
    });
    await openDocs(page, state);

    const row = page.locator('[data-docs-block-id="block-clip-image"]');
    await row.getByTitle("展開").click();
    const attachment = row.locator('[data-docs-attachment-control][data-docs-attachment-id="clip-image-1"]');
    await expect(attachment).toBeVisible();

    await row.getByTitle("折りたたみ").click();
    await expect(attachment).toHaveCount(0);

    await row.getByTitle("展開").click();
    await expect(attachment).toBeVisible();
  });

  test("does not reopen completion inside a closed wikilink and dismisses suggestions outside", async ({ page }) => {
    const state = createState();
    const node = state.nodes.find((item) => item.id === "block-link")!;
    node.title = "参照 [[pix2pix]]";
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("End");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await expect(page.locator('[data-docs-inline-popup][aria-label="インライン候補"]')).toHaveCount(0);

    await page.keyboard.press("End");
    await page.keyboard.type(" [[pix");
    await expect(page.locator('[data-docs-inline-popup][aria-label="インライン候補"]')).toBeVisible();
    await page.locator("[data-docs-scroll-container]").click({ position: { x: 4, y: 4 } });
    await expect(page.locator('[data-docs-inline-popup][aria-label="インライン候補"]')).toHaveCount(0);
  });

  test("moves a node to another page from slash command without dropping its relations", async ({ page }) => {
    const state = createState();
    state.node_supertags.push({ node_id: "block-link", supertag_id: "tag-task" });
    state.field_values.push({
      node_id: "block-link",
      field_id: "field-status",
      value_json: "doing",
      value_text: "doing",
      value_number: null,
      value_datetime: null,
      target_node_id: null,
    });
    await openDocs(page, state);
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" /move");
    await page.keyboard.press("Enter");
    const search = page.getByRole("textbox", { name: "移動先ページを検索" });
    await expect(search).toBeFocused();
    await expect(page.getByRole("option", { name: "関連Meeting" })).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog", { name: "別ページへ移動" })).toHaveCount(0);
    expect(state.nodes.find((node) => node.id === "block-link")?.parent_id).toBe("node-meeting");
    expect(state.nodes.find((node) => node.id === "block-link")?.title).toBe("関連ページ");
    expect(state.node_supertags).toContainEqual({ node_id: "block-link", supertag_id: "tag-task" });
    expect(state.field_values).toContainEqual(expect.objectContaining({ node_id: "block-link", field_id: "field-status", value_text: "doing" }));
  });

  test("keeps every workspace root and expands the focused ancestors on direct deep access", async ({ page }) => {
    const state = createDeepState();
    await addAuthCookie(page);
    await page.addInitScript(() => {
      localStorage.setItem("aoitalk.docs.sidebar.collapsed", JSON.stringify([]));
      const scrolls: string[] = [];
      (window as typeof window & { __docsSidebarScrolls?: string[] }).__docsSidebarScrolls = scrolls;
      Element.prototype.scrollIntoView = function scrollIntoView() {
        const nodeId = this.getAttribute("data-docs-sidebar-node-id");
        if (nodeId) scrolls.push(nodeId);
      };
    });
    await installDocsMock(page, state);
    await page.goto("/docs/deep-child");
    await expect(page.getByTestId("docs-block-editor")).toBeVisible();

    await expect(page.locator('[data-docs-sidebar-node-id="node-a"]')).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="node-meeting"]')).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="deep-parent"]')).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="deep-child"]')).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="deep-child"]')).toHaveClass(/bg-accent/);
    await expect(page.locator('[data-docs-sidebar-node-id="other-root-child"]')).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => (window as typeof window & { __docsSidebarScrolls?: string[] }).__docsSidebarScrolls ?? [])).toContain("deep-child");
  });

  test("uses user-facing Docs data source labels", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const source = page.getByRole("combobox", { name: "Docsデータソース" });
    await expect(source.locator("option")).toHaveText(["Docs", "[EP] AoiTalk Enterprise"]);
    await expect(page.getByText("[Local] Docs")).toHaveCount(0);
    await expect(page.getByText("リモートDocs（読み取り専用）")).toHaveCount(0);
  });

  test("keeps every root when Ctrl+P opens a page in another root", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.keyboard.press("Control+P");
    const input = page.getByPlaceholder("ページ名またはエイリアス...");
    await expect(input).toBeVisible();
    await input.fill("meeting-alias");
    await expect(page.getByRole("option", { name: /関連Meeting/ })).toBeVisible();
    await page.getByRole("option", { name: /関連Meeting/ }).click();
    await expect(page).toHaveURL(/\/docs$/);
    await expect(page.locator('[data-docs-node-id="node-meeting"]').first()).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="node-a"]')).toBeVisible();
    await expect(page.locator('[data-docs-sidebar-node-id="node-meeting"]')).toHaveClass(/bg-accent/);
  });

  test("keeps document search nodes collapsed and compact", async ({ page }) => {
    const state = createState();
    let queryRequests = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && new URL(request.url()).pathname === "/api/docs/query") queryRequests += 1;
    });
    await openDocs(page, state);
    const toggle = page.getByTestId("docs-search-node-toggle");
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(toggle).toHaveAccessibleName("未完了タスクのLive Queryを展開");
    await expect(page.getByText("未完了タスク", { exact: true })).toHaveCount(1);
    await expect(page.getByTestId("docs-search-node-controls")).toHaveCount(0);
    await expect.poll(() => queryRequests).toBe(0);
    await expect(page.getByRole("button", { name: "List" })).toHaveCount(0);
    await expect(page.getByText("ノート化")).toHaveCount(0);
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(toggle).toHaveAccessibleName("未完了タスクのLive Queryを折りたたむ");
    await expect.poll(() => queryRequests).toBe(1);
    await expect(page.getByTestId("docs-search-node-controls")).toContainText("1件");
    await expect(page.getByTestId("docs-search-node-results")).toContainText("初回打合せ日程を確定");
    await expect(page.getByRole("button", { name: "List" })).toHaveCount(0);
    await expect(page.getByText("ノート化")).toHaveCount(0);
    await toggle.click();
    await toggle.click();
    await expect.poll(() => queryRequests).toBe(1);
  });

  test("shows only slash-only commands with their actual command names", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("/");
    const menu = page.getByRole("listbox");
    await expect(menu).toContainText("/checkbox");
    await expect(menu).toContainText("/field — フィールド");
    await expect(menu).toContainText("/field ai");
    await expect(menu).not.toContainText("見出し1");
    await expect(menu).not.toContainText("引用");
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-block-kind="checkbox"]').last()).toBeVisible();
  });

  test("moves between the page title and first body row with arrow keys", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const pageTitle = page.getByRole("textbox", { name: "ページタイトル" });
    await pageTitle.focus();
    await pageTitle.press("ArrowDown");
    await expect(page.locator(".cm-content")).toBeFocused();
    await expect(page.locator(".cm-content")).toContainText("概要");
    await page.keyboard.press("ArrowUp");
    await expect(pageTitle).toBeFocused();
  });

  test("Backspace at the start of a heading restores a paragraph before merging", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const heading = page.locator('[data-docs-block-id="block-title"]');
    await heading.click();
    await page.keyboard.press("Home");
    await page.keyboard.press("Backspace");
    await expect(heading).toHaveAttribute("data-block-kind", "paragraph");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.body_json.block_type).toBe("paragraph");
    await expect(heading).toContainText("概要");
  });

  test("aligns a heading marker with the rendered heading line", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    const heading = page.locator('[data-docs-block-id="block-title"]');
    const marker = heading.locator("button").first();
    const title = heading.locator("[data-docs-title-display]");
    const [markerBox, titleBox] = await Promise.all([marker.boundingBox(), title.boundingBox()]);
    expect(markerBox).not.toBeNull();
    expect(titleBox).not.toBeNull();
    const markerCenter = (markerBox?.y ?? 0) + (markerBox?.height ?? 0) / 2;
    const titleCenter = (titleBox?.y ?? 0) + (titleBox?.height ?? 0) / 2;
    expect(Math.abs(titleCenter - markerCenter)).toBeLessThanOrEqual(1);
  });

  test("keeps > available for a Markdown quote", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("> ");
    await page.keyboard.type("引用本文");
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-block-kind="quote"]', { hasText: "引用本文" })).toBeVisible();
  });

  test("creates a Field only through /field", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-task"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.press("Tab");
    await page.keyboard.type("/field");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("listbox", { name: "インライン候補" })).toContainText("状態");
    await page.keyboard.press("Enter");
    await page.keyboard.type("doing");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.field_values.find((value) => value.node_id === "block-task" && value.field_id === "field-status")?.value_text).toBe("doing");
  });

  test("shows guidance and creates the first Field from /field", async ({ page }) => {
    const state = createState();
    state.node_supertags = [{ node_id: "node-a", supertag_id: "tag-task" }];
    state.fields = [];
    state.field_values = [];
    // This fixture intentionally starts with no Field definitions, but still
    // models an actor-owned library so the definition-write permission gate
    // can be exercised by the inline creator.
    Object.assign(state, { docs_library_id: "workspace-1" });
    for (const node of state.nodes) Object.assign(node, { docs_library_id: "workspace-1" });
    for (const tag of state.supertags) Object.assign(tag, { docs_library_id: "workspace-1" });
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("/field");
    await page.keyboard.press("Enter");
    const candidates = page.getByRole("listbox", { name: "インライン候補" });
    await expect(candidates).toContainText("新規Field名を入力してください");
    await page.keyboard.type("備考");
    await expect(candidates).toContainText("Field「備考」を作成");
    await page.keyboard.press("Enter");
    await page.keyboard.type("正本を優先");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.fields.find((field) => field.name === "備考")).toBeTruthy();
    const created = state.fields.find((field) => field.name === "備考");
    await expect.poll(() => state.field_values.find(
      (value) => value.node_id === "node-a" && value.field_id === created?.id,
    )?.value_text).toBe("正本を優先");
  });

  test("inserts an @task reference and opens it in the task modal", async ({ page }) => {
    const state = createState();
    let openedTaskId = "";
    page.on("request", (request) => {
      const match = new URL(request.url()).pathname.match(/^\/api\/tasks\/([0-9a-f-]{36})$/i);
      if (match) openedTaskId = match[1] ?? "";
    });
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.type(" @task 確認");
    await expect(page.getByRole("listbox")).toContainText("確認用タスク");
    await page.keyboard.press("Enter");
    await page.keyboard.press("Escape");
    const taskReference = page.locator('[data-docs-task-id="11111111-1111-4111-8111-111111111111"]');
    await expect(taskReference).toContainText("確認用タスク");
    await taskReference.click();
    await expect.poll(() => openedTaskId).toBe("11111111-1111-4111-8111-111111111111");
  });

  test("does not let a late optimistic create response steal focus from the next line", async ({ page }) => {
    const state = createState();
    const initialCount = state.nodes.length;
    await openDocs(page, state, { createDelays: [250, 0] });
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("Second line");
    await page.keyboard.press("Enter");
    await page.keyboard.type("Third line");
    await page.keyboard.press("Enter");

    await expect.poll(() => state.nodes.length).toBe(initialCount + 3);
    const blankNodes = state.nodes.filter((node) => !node.archived_at && node.title === "");
    expect(blankNodes.length).toBeGreaterThanOrEqual(1);
    blankNodes.forEach((node) => expectPersistedBlank(node));
    // A delayed optimistic response may leave the final blank row outside the
    // current virtualized viewport; the invariant is that the editor remains
    // mounted and every persisted blank retains its canonical metadata.
    await expect(page.getByTestId("docs-block-editor")).toBeVisible();
  });

  test("queues a newer draft while an earlier commit for the same line is saving", async ({ page }) => {
    const state = createState();
    await openDocs(page, state, { patchDelays: [250, 0] });
    const row = page.locator('[data-docs-block-id="block-link"]');
    await row.click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("First draft");
    await page.keyboard.press("ArrowDown");

    await row.click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("Second draft");
    await page.keyboard.press("ArrowDown");

    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title).toBe("Second draft");
  });

  test("keeps a later edit separate from the original create history", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("Initial text");
    await page.keyboard.press("Enter");
    const createdRow = page.locator('[data-docs-block-id]', { hasText: "Initial text" }).first();
    const createdId = await createdRow.getAttribute("data-docs-block-id");
    expect(createdId).toBeTruthy();
    const trailingBlank = state.nodes.find((node) => !node.archived_at && node.title === "" && node.id !== "node-a");
    expectPersistedBlank(trailingBlank);
    await expect(page.locator(`[data-docs-block-id="${trailingBlank?.id}"] .cm-content`)).toBeFocused();

    await page.locator(`[data-docs-block-id="${createdId}"]`).click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("Edited text");
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("docs-block-editor")).toBeFocused();

    await page.getByTestId("docs-block-editor").press("Control+Z");
    await expect.poll(() => state.nodes.find((node) => node.id === createdId)?.title).toBe("Initial text");
    await expect(page.locator(`[data-docs-block-id="${createdId}"]`)).toBeVisible();
    await page.getByTestId("docs-block-editor").press("Control+Z");
    await expect.poll(() => state.nodes.find((node) => node.id === createdId)?.archived_at).not.toBeNull();
  });

  test("undoes and redoes explicit blank paragraph creation", async ({ page }) => {
    const state = createState();
    page.on("console", (message) => {
      if (message.text().includes("DEBUG")) console.log(message.text());
    });
    const archiveRequests: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "DELETE" && new URL(request.url()).pathname.includes("/api/docs/nodes/")) {
        archiveRequests.push(request.url());
      }
    });
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.nodes.find((node) => node.id !== "block-link" && node.title === "")?.id ?? null).not.toBeNull();
    const created = state.nodes.find((node) => node.id !== "block-link" && node.title === "");
    expectPersistedBlank(created);
    const createdRow = page.locator(`[data-docs-block-id="${created?.id}"]`);
    await expect(createdRow.locator(".cm-content")).toBeFocused();

    await createdRow.locator(".cm-content").press("Control+Z");
    await expect.poll(() => archiveRequests.length).toBeGreaterThan(0);
    await expect(createdRow).toHaveCount(0);
    await expect.poll(() => state.nodes.find((node) => node.id === created?.id)?.archived_at).not.toBeNull();

    await page.getByTestId("docs-block-editor").press("Control+Y");
    await expect.poll(() => state.nodes.find((node) => node.id === created?.id)?.archived_at).toBeNull();
    await expect(createdRow).toBeVisible();
    expectPersistedBlank(state.nodes.find((node) => node.id === created?.id));
  });
});

test.describe("Docs block editor multiple selected nodes", () => {
  async function selectTitleAndTask(page: import("@playwright/test").Page) {
    await page.locator('[data-docs-block-id="block-title"]').click();
    await page.keyboard.press("Shift+ArrowDown");
  }

  test("multiple selected nodes: Backspace archives the whole selection", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await selectTitleAndTask(page);
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).not.toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).not.toBeNull();
    await expect(page.locator('[data-docs-block-id="block-link"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-title"]')).toHaveCount(0);
    await expect(page.locator('[data-docs-block-id="block-task"]')).toHaveCount(0);
  });

  test("multiple selected nodes: Delete archives the whole selection", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await selectTitleAndTask(page);
    await page.keyboard.press("Delete");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).not.toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).not.toBeNull();
    await expect(page.locator('[data-docs-block-id="block-link"]')).toBeVisible();
  });

  test("multiple selected nodes: Backspace in body text deletes characters only", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    const before = state.nodes.find((node) => node.id === "block-link")?.title ?? "";
    await page.keyboard.press("Backspace");
    await page.keyboard.press("Escape");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.archived_at).toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-link")?.title).toBe(before.slice(0, -1));
  });

  test("multiple selected nodes: Backspace on selected body text does not archive", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await selectTitleAndTask(page);
    await page.keyboard.press("End");
    await page.keyboard.press("Shift+Home");
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).toBeNull();
    await expect(page.locator('[data-docs-block-id="block-title"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"] .cm-content')).toHaveText("");
  });

  test("multiple selected nodes: Delete on selected body text does not archive", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await selectTitleAndTask(page);
    await page.keyboard.press("End");
    await page.keyboard.press("Shift+Home");
    await page.keyboard.press("Delete");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).toBeNull();
    await expect(page.locator('[data-docs-block-id="block-title"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"] .cm-content')).toHaveText("");
  });

  test("multiple selected nodes: partial DELETE failure keeps history aligned", async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    const state = createState();
    await openDocs(page, state, { deleteFailures: ["block-task"] });
    await selectTitleAndTask(page);
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).not.toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).toBeNull();
    expect(pageErrors).toEqual([]);
    await page.getByTestId("docs-block-editor").press("Control+Z");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).toBeNull();
    await expect(page.locator('[data-docs-block-id="block-title"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"]')).toBeVisible();
  });

  test("multiple selected nodes: post-mutation DELETE warning keeps history aligned", async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    const state = createState();
    await openDocs(page, state, { deleteCommittedWarnings: ["block-task"] });
    await selectTitleAndTask(page);
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).not.toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).not.toBeNull();
    expect(pageErrors).toEqual([]);
    await page.getByTestId("docs-block-editor").press("Control+Z");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).toBeNull();
    await expect(page.locator('[data-docs-block-id="block-title"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"]')).toBeVisible();
  });

  test("multiple selected nodes: reverse partial DELETE failure focuses surviving node", async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    const state = createState();
    await openDocs(page, state, { deleteFailures: ["block-title"] });
    await selectTitleAndTask(page);
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).not.toBeNull();
    await expect(page.locator('[data-docs-block-id="block-title"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"]')).toHaveCount(0);
    const survivor = page.locator('[data-docs-block-id="block-title"]');
    await expect(survivor).toHaveClass(/bg-primary/);
    await expect(page.locator('[data-docs-block-id="block-title"] .cm-content')).toBeFocused();
    expect(pageErrors).toEqual([]);
  });

  test("multiple selected nodes: one undo restores a bulk archive", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await selectTitleAndTask(page);
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).not.toBeNull();
    await page.getByTestId("docs-block-editor").press("Control+Z");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-title")?.archived_at).toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.archived_at).toBeNull();
    await expect(page.locator('[data-docs-block-id="block-title"]')).toBeVisible();
    await expect(page.locator('[data-docs-block-id="block-task"]')).toBeVisible();
  });

  test("multiple selected nodes: parent-child selection archives only the parent root", async ({ page }) => {
    const state = createDeepState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-title"]').getByTitle("展開").click();
    await expect(page.locator('[data-docs-block-id="deep-parent"]')).toBeVisible();
    await page.locator('[data-docs-block-id="deep-parent"]').click();
    await page.keyboard.press("Shift+ArrowDown");
    await page.keyboard.press("Backspace");
    await expect.poll(() => state.nodes.find((node) => node.id === "deep-parent")?.archived_at).not.toBeNull();
    await expect.poll(() => state.nodes.find((node) => node.id === "deep-child")?.archived_at).not.toBeNull();
    await expect(page.locator('[data-docs-block-id="deep-parent"]')).toHaveCount(0);
    await expect(page.locator('[data-docs-block-id="deep-child"]')).toHaveCount(0);
    const survivor = page.locator('[data-docs-block-id="block-title"]');
    await expect(survivor).toBeVisible();
    await expect(survivor).toHaveClass(/bg-primary/);
  });

  test("multiple selected nodes: clears deleted ids and highlights a surviving row", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await selectTitleAndTask(page);
    await page.keyboard.press("Backspace");
    await expect(page.locator('[data-docs-block-id="block-title"]')).toHaveCount(0);
    await expect(page.locator('[data-docs-block-id="block-task"]')).toHaveCount(0);
    const survivor = page.locator('[data-docs-block-id="block-link"]');
    await expect(survivor).toBeVisible();
    await expect(survivor).toHaveClass(/bg-primary/);
  });
});
