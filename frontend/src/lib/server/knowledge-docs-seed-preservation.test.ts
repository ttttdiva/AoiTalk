import { beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_DOCS_SUPERTAGS } from "@/lib/docs-model";

type Row = Record<string, unknown>;

const store = vi.hoisted(() => ({
  library: { id: "lib-1", ownerUserId: "user-1", libraryType: "personal", name: "Personal Docs", description: "d", settingsJson: { canonical_store: "postgresql", derived_index: "qdrant" } } as Row,
  tags: [] as Row[],
  fields: [] as Row[],
  links: [] as Row[],
  seq: 0,
}));

vi.mock("@/db", async () => {
  const schema = await import("@/db/schema");
  const { PgDialect: Dialect } = await import("drizzle-orm/pg-core");
  const dialect = new Dialect();
  const paramsOf = (cond: unknown) => (cond ? dialect.sqlToQuery(cond as never).params : []);
  const nextId = () => `id-${++store.seq}`;

  function rowsFor(table: unknown, params: unknown[]) {
    if (table === schema.knowledgeSupertags) return store.tags;
    if (table === schema.knowledgeFields) return store.fields.filter((f) => f.supertagId === params[0]);
    if (table === schema.docsLibraries) return [store.library];
    return [];
  }

  function selectBuilder() {
    let table: unknown;
    let params: unknown[] = [];
    const chain: Row = {
      from: (t: unknown) => ((table = t), chain),
      where: (cond: unknown) => ((params = paramsOf(cond)), chain),
      limit: () => chain,
      orderBy: () => chain,
      innerJoin: () => chain,
      then: (resolve: (v: unknown) => unknown, reject?: (e: unknown) => unknown) =>
        Promise.resolve(rowsFor(table, params).map((r) => ({ ...r }))).then(resolve, reject),
    };
    return chain;
  }

  function insertBuilder(table: unknown) {
    const inserted: Row[] = [];
    const chain: Row = {
      values: (v: Row | Row[]) => {
        for (const raw of Array.isArray(v) ? v : [v]) {
          if (table === schema.knowledgeSupertags) {
            if (store.tags.some((t) => t.name === raw.name)) throw new Error("uq_knowledge_supertag_name");
            if (raw.systemKey && store.tags.some((t) => t.systemKey === raw.systemKey)) throw new Error("uq system_key");
            const row = { id: nextId(), parentSupertagId: null, titleTemplate: null, ...raw };
            store.tags.push(row);
            inserted.push(row);
          } else if (table === schema.knowledgeFields) {
            if (store.fields.some((f) => f.supertagId === raw.supertagId && f.name === raw.name)) throw new Error("uq_knowledge_field_name");
            const row = { id: nextId(), defaultValueJson: null, ...raw };
            store.fields.push(row);
            inserted.push(row);
          } else if (table === schema.knowledgeSupertagFields) {
            if (!store.links.some((l) => l.supertagId === raw.supertagId && l.fieldId === raw.fieldId)) {
              store.links.push({ ...raw });
            }
          } else {
            inserted.push({ id: nextId(), ...raw });
          }
        }
        return chain;
      },
      onConflictDoNothing: () => chain,
      onConflictDoUpdate: () => chain,
      returning: async () => inserted,
      then: (resolve: (v: unknown) => unknown, reject?: (e: unknown) => unknown) => Promise.resolve(undefined).then(resolve, reject),
    };
    return chain;
  }

  function updateBuilder(table: unknown) {
    let values: Row = {};
    const chain: Row = {
      set: (v: Row) => ((values = v), chain),
      where: (cond: unknown) => {
        const [id] = paramsOf(cond);
        const target =
          table === schema.knowledgeSupertags ? store.tags : table === schema.knowledgeFields ? store.fields : [];
        for (const row of target) if (row.id === id) Object.assign(row, values);
        return chain;
      },
      returning: async () => [],
      then: (resolve: (v: unknown) => unknown, reject?: (e: unknown) => unknown) => Promise.resolve(undefined).then(resolve, reject),
    };
    return chain;
  }

  const fake: Row = {
    select: selectBuilder,
    insert: insertBuilder,
    update: updateBuilder,
    execute: async () => [],
    transaction: async (cb: (tx: unknown) => unknown) => cb(fake),
  };
  return { db: fake };
});

vi.mock("@/lib/server/docs-node-writer", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/server/docs-node-writer")>();
  return {
    ...actual,
    insertDocsNode: async (_tx: unknown, body: Row) => ({ id: `node-${++store.seq}`, ...body }),
    updateDocsNode: async () => null,
  };
});

import { ensureDocsWorkspace } from "@/lib/server/knowledge-docs-utils";

const USER = { id: "user-1" };

function tagByKey(key: string) {
  const tag = store.tags.find((t) => t.systemKey === key);
  if (!tag) throw new Error(`missing tag ${key}`);
  return tag;
}

function fieldsOf(tag: Row) {
  return store.fields.filter((f) => f.supertagId === tag.id);
}

const snapshot = () => structuredClone({ tags: store.tags, fields: store.fields, links: store.links });

beforeEach(() => {
  store.tags.length = 0;
  store.fields.length = 0;
  store.links.length = 0;
  store.seq = 0;
});

function customizeTask(overrides: Row = {}) {
  const task = tagByKey("task");
  const custom = {
    id: "custom-field",
    docsLibraryId: "lib-1",
    supertagId: task.id,
    systemKey: null,
    name: "独自項目",
    fieldType: "options",
    required: true,
    optionsJson: { values: ["a", "b"] },
    defaultValueJson: "a",
    sortOrder: 99,
  };
  store.fields.push(custom);
  store.links.push({ supertagId: task.id, fieldId: custom.id, sortOrder: 99, required: true, showInTemplate: false, optional: true });
  const status = fieldsOf(task).find((f) => f.systemKey === "task_status")!;
  status.optionsJson = { values: ["open", "closed"] };
  status.defaultValueJson = "open";
  Object.assign(task, {
    name: "私のタスク",
    baseType: "note",
    description: "利用者の説明",
    icon: "star",
    color: "#000000",
    templateJson: { format: "mine" },
    aiInstructions: "独自指示",
    configJson: { tools: ["only_this"], extra: { k: 1 } },
    pinnedFieldIds: [custom.id],
    ...overrides,
  });
  return { task, custom };
}

describe("Docs default seed preservation (real ensureDocsWorkspace, fake DB)", () => {
  it("creates defaults, fields, links and pins on first bootstrap", async () => {
    await ensureDocsWorkspace(USER);

    const keyed = DEFAULT_DOCS_SUPERTAGS.filter((s) => s.systemKey);
    expect(store.tags.length).toBe(DEFAULT_DOCS_SUPERTAGS.length);
    for (const spec of DEFAULT_DOCS_SUPERTAGS) {
      const tag = store.tags.find((t) => t.name === spec.name)!;
      expect(tag.systemKey ?? undefined).toBe(spec.systemKey);
      expect(tag.baseType).toBe(spec.baseType);
      expect(tag.description).toBe(spec.description);
      expect(tag.icon).toBe(spec.icon);
      expect(tag.color).toBe(spec.color);
      expect(tag.templateJson).toEqual(spec.templateJson);
      expect(tag.aiInstructions).toBe(spec.aiInstructions);
      const fields = fieldsOf(tag);
      expect(fields.map((f) => f.name)).toEqual(spec.fields.map((f) => f.name));
      for (const f of fields) {
        expect(store.links.some((l) => l.supertagId === tag.id && l.fieldId === f.id)).toBe(true);
      }
      expect(tag.pinnedFieldIds).toEqual(
        spec.pinnedFieldKeys.map((n) => fields.find((f) => f.name === n)!.id),
      );
    }
    expect(keyed.length).toBeGreaterThan(0);
  });

  it("is idempotent on a fresh bootstrap", async () => {
    await ensureDocsWorkspace(USER);
    const first = snapshot();
    await ensureDocsWorkspace(USER);
    await ensureDocsWorkspace(USER);
    expect(snapshot()).toEqual(first);
  });

  it("keeps a customized system tag, custom field, config and pins across repeated ensure", async () => {
    await ensureDocsWorkspace(USER);
    const { task, custom } = customizeTask();
    const before = snapshot();

    await ensureDocsWorkspace(USER);
    await ensureDocsWorkspace(USER);

    expect(snapshot()).toEqual(before);
    expect(task.name).toBe("私のタスク");
    expect(task.configJson).toEqual({ tools: ["only_this"], extra: { k: 1 } });
    expect(task.pinnedFieldIds).toEqual([custom.id]);
  });

  it.each(["", null])("does not refill blank user values (%j)", async (blank) => {
    await ensureDocsWorkspace(USER);
    const { task } = customizeTask({
      description: blank,
      icon: blank,
      color: blank,
      aiInstructions: blank,
      templateJson: {},
      configJson: { tools: [] },
      pinnedFieldIds: [],
    });

    await ensureDocsWorkspace(USER);
    await ensureDocsWorkspace(USER);

    expect(task.description).toBe(blank);
    expect(task.icon).toBe(blank);
    expect(task.color).toBe(blank);
    expect(task.aiInstructions).toBe(blank);
    expect(task.templateJson).toEqual({});
    expect(task.configJson).toEqual({ tools: [] });
    expect(task.pinnedFieldIds).toEqual([]);
    expect(task.baseType).toBe("note");
  });

  it("keeps a renamed genuine system tag without creating a duplicate", async () => {
    await ensureDocsWorkspace(USER);
    const task = tagByKey("task");
    task.name = "やること";
    const ids = store.tags.map((t) => t.id);

    await ensureDocsWorkspace(USER);

    expect(store.tags.map((t) => t.id)).toEqual(ids);
    expect(task.name).toBe("やること");
    expect(store.tags.some((t) => t.name === "Task")).toBe(false);
  });

  it.each([null, "user_defined"])(
    "does not adopt or overwrite a user-created same-name tag (system key %j)",
    async (existingKey) => {
      const mine: Row = {
        id: "mine",
        docsLibraryId: "lib-1",
        parentSupertagId: null,
        systemKey: existingKey,
        name: "Task",
        baseType: "note",
        description: "自作",
        icon: null,
        color: "",
        templateJson: { x: 1 },
        pinnedFieldIds: [],
        configJson: { tools: [] },
        aiInstructions: "",
      };
      store.tags.push(mine);
      const before = structuredClone(mine);

      await ensureDocsWorkspace(USER);
      await ensureDocsWorkspace(USER);

      expect(mine).toEqual(before);
      expect(fieldsOf(mine)).toEqual([]);
      const genuine = tagByKey("task");
      expect(genuine).not.toBe(mine);
      expect(genuine.baseType).toBe("task");
      expect(fieldsOf(genuine).length).toBe(6);
      expect(store.tags.length).toBe(DEFAULT_DOCS_SUPERTAGS.length + 1);
    },
  );

  it("preserves a user-created same-name tag for a default without a system key", async () => {
    const spec = DEFAULT_DOCS_SUPERTAGS.find((s) => !s.systemKey);
    expect(spec).toBeTruthy();
    const mine: Row = {
      id: "mine",
      docsLibraryId: "lib-1",
      parentSupertagId: null,
      systemKey: null,
      name: spec!.name,
      baseType: "note",
      description: null,
      icon: "",
      color: null,
      templateJson: {},
      pinnedFieldIds: [],
      configJson: { tools: ["mine"] },
      aiInstructions: null,
    };
    store.tags.push(mine);

    await ensureDocsWorkspace(USER);
    await ensureDocsWorkspace(USER);

    expect(store.tags.filter((t) => t.name === spec!.name)).toEqual([mine]);
    expect(fieldsOf(mine)).toEqual([]);
    expect(store.links.filter((l) => l.supertagId === mine.id)).toEqual([]);
    expect(mine).toMatchObject({
      systemKey: null,
      baseType: "note",
      description: null,
      icon: "",
      color: null,
      aiInstructions: null,
      configJson: { tools: ["mine"] },
      pinnedFieldIds: [],
    });
  });

  it("restores a missing field link without changing the existing field", async () => {
    await ensureDocsWorkspace(USER);
    const { task } = customizeTask();
    const status = fieldsOf(task).find((f) => f.systemKey === "task_status")!;
    store.links.splice(
      store.links.findIndex((l) => l.supertagId === task.id && l.fieldId === status.id),
      1,
    );

    await ensureDocsWorkspace(USER);

    expect(store.links.some((l) => l.supertagId === task.id && l.fieldId === status.id)).toBe(true);
    expect(status.optionsJson).toEqual({ values: ["open", "closed"] });
    expect(status.defaultValueJson).toBe("open");
  });
});
