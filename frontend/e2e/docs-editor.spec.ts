import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie } from "./support/auth";

type MockNode = {
  id: string;
  workspace_id: string;
  parent_id: string | null;
  root_page_id: string | null;
  project_id: string | null;
  title: string;
  description?: string;
  body_json: Record<string, unknown>;
  body_text: string;
  node_type: "page" | "block" | "object" | "search";
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

async function installDocsMock(page: Page, state: MockState, options: { createDelays?: number[]; patchDelays?: number[]; tagDelays?: number[]; tagCommittedWarnings?: number[]; invalidTagIds?: string[] } = {}) {
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
      patchRequestIndex += 1;
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
      const index = state.nodes.findIndex((node) => node.id === nodeId);
      if (index >= 0) {
        state.nodes[index] = { ...state.nodes[index], archived_at: new Date().toISOString(), updated_at: new Date().toISOString() };
      }
      await route.fulfill({ json: { node: state.nodes[index] } });
      return;
    }

    if (url.pathname === "/api/docs" && method === "POST") {
      const body = await route.request().postDataJSON();
      const createDelay = options.createDelays?.[createRequestIndex] ?? 0;
      createRequestIndex += 1;
      if (createDelay > 0) await new Promise((resolve) => setTimeout(resolve, createDelay));
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

async function openDocs(page: Page, state: MockState, options: { createDelays?: number[]; patchDelays?: number[]; tagDelays?: number[]; tagCommittedWarnings?: number[]; invalidTagIds?: string[] } = {}) {
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
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-title"]').getByTitle("展開").click();
    await expect(page.locator('[data-docs-block-id="deep-parent"]')).toBeVisible();
    await page.locator('[data-docs-block-id="block-task"]').click();
    await page.keyboard.press("Tab");
    await expect.poll(() => state.nodes.find((node) => node.id === "block-task")?.parent_id).toBe("block-title");
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
    await page.keyboard.type("/field");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("listbox", { name: "インライン候補" })).toContainText("Page Role");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    const shorthandEditor = page.locator('.cm-content[contenteditable="true"]');
    await expect(shorthandEditor).toBeFocused();
    await expect(shorthandEditor).toHaveText("Page Role:");
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
    await expect(page.getByRole("textbox", { name: "Field 備考" })).toHaveValue("正本を優先");
    await expect(page.getByText("13 fields")).toHaveCount(0);
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
    await expect(select).toHaveValue("draft");
    await select.press("ArrowDown");
    await expect(select).toHaveValue("published");
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
    await expect(page).toHaveURL(/\/docs\/node-meeting$/);
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
    const summary = page.getByTestId("docs-search-node-summary");
    await expect(summary).toBeVisible();
    await expect(summary).toContainText("未完了タスク");
    await expect(summary).toContainText("0件");
    await expect.poll(() => queryRequests).toBe(0);
    await expect(page.getByRole("button", { name: "List" })).toHaveCount(0);
    await expect(page.getByText("ノート化")).toHaveCount(0);
    await summary.click();
    await expect.poll(() => queryRequests).toBe(1);
    await expect(page.locator('[data-testid="docs-search-node-summary"] + div')).toContainText("初回打合せ日程を確定");
    await expect(page.getByRole("button", { name: "List" })).toHaveCount(0);
    await expect(page.getByText("ノート化")).toHaveCount(0);
    await summary.click();
    await summary.click();
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
    await page.keyboard.press("Escape");
    await expect(page.locator('[data-block-kind="quote"]', { hasText: "引用本文" })).toBeVisible();
  });

  test("creates a Field only through /field", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-task"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.press("Tab");
    const fieldRowId = state.nodes.at(-1)?.id;
    await expect.poll(() => state.nodes.find((node) => node.id === fieldRowId)?.parent_id).toBe("block-task");
    const taskRow = page.locator('[data-docs-block-id="block-task"]');
    const expand = taskRow.getByTitle("展開");
    if (await expand.count()) await expand.click();
    await page.locator(`[data-docs-block-id="${fieldRowId}"]`).click();
    await page.keyboard.type("/field");
    await expect(page.getByRole("listbox")).toContainText("/field — フィールド");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("listbox")).toContainText("状態");
    await page.keyboard.press("Enter");
    await page.keyboard.type("doing");
    await page.keyboard.press("Enter");
    await expect.poll(() => state.field_values.find((value) => value.node_id === "block-task" && value.field_id === "field-status")?.value_text).toBe("doing");
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

    await expect.poll(() => state.nodes.length).toBe(initialCount + 2);
    await expect(page.locator(".cm-content")).toBeFocused();
    await expect(page.locator(".cm-content")).toContainText("Third line");
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
    const createdRow = page.locator('[data-docs-block-id]', { hasText: "Initial text" }).first();
    const createdId = await createdRow.getAttribute("data-docs-block-id");
    expect(createdId).toBeTruthy();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("docs-block-editor")).toBeFocused();

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

  test("undoes and redoes block split operations", async ({ page }) => {
    const state = createState();
    await openDocs(page, state);
    await page.locator('[data-docs-block-id="block-link"]').click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("Undo split block");
    const createdRow = page.locator('[data-docs-block-id]', { hasText: "Undo split block" }).first();
    await expect(createdRow).toBeVisible();
    const createdId = await createdRow.getAttribute("data-docs-block-id");
    expect(createdId).toBeTruthy();
    await expect(page.locator(`[data-docs-block-id="${createdId}"]`)).toContainText("Undo split block");

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("docs-block-editor")).toBeFocused();
    await page.getByTestId("docs-block-editor").press("Control+Z");
    await expect(page.locator(`[data-docs-block-id="${createdId}"]`)).toHaveCount(0);
    await expect.poll(() => state.nodes.find((node) => node.id === createdId)?.archived_at).not.toBeNull();

    await page.getByTestId("docs-block-editor").press("Control+Y");
    await expect.poll(() => state.nodes.find((node) => node.id === createdId)?.archived_at).toBeNull();
    await expect(page.locator(`[data-docs-block-id="${createdId}"]`)).toBeVisible();
    await expect(page.locator(`[data-docs-block-id="${createdId}"]`)).toContainText("Undo split block");
    await expect.poll(() => state.nodes.find((node) => node.id === createdId)?.archived_at).toBeNull();
  });
});
