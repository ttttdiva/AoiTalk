import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { and, eq, inArray, isNull, sql } from "drizzle-orm";

const WORKSPACE_ID = "3e73f8c6-5870-4902-821d-241d3653c4ea";
const RECURRING_COST_SYSTEM_KEY = "recurring_cost";

const COLLECTIONS = [
  {
    id: "77cd2257-29b0-4ce1-9a2e-3cfd66f87b30",
    title: "固定費",
    tagSystemKey: RECURRING_COST_SYSTEM_KEY,
  },
  {
    id: "1b0e0553-c2cb-4f53-a6b3-d909ede9d6ee",
    title: "趣味の定期支出",
    tagSystemKey: RECURRING_COST_SYSTEM_KEY,
  },
  {
    id: "f8c780f5-2c58-497a-8a43-77b8622d673a",
    title: "食費・光熱費",
    tagSystemKey: RECURRING_COST_SYSTEM_KEY,
  },
  {
    id: "0b2d6038-3ff1-4e67-b326-26cc2207a84a",
    title: "税金・年金・健康保険",
    tagSystemKey: RECURRING_COST_SYSTEM_KEY,
  },
  {
    id: "0561245c-cbd9-4a60-ada4-8a69f8950a6a",
    title: "全体合計",
    tagSystemKey: RECURRING_COST_SYSTEM_KEY,
  },
  {
    id: "62b1c819-8017-4d66-8ff9-2af54fdc6450",
    title: "家電購入記録",
    tagName: "家電購入",
  },
  {
    id: "4aa49b07-d5a5-4e7a-a878-4d368426e7ec",
    title: "現在のPC構成",
    tagName: "家電購入",
  },
  {
    id: "39d02800-f053-4335-9d3e-95ee9bda824c",
    title: "周辺機器",
    tagName: "家電購入",
  },
  {
    id: "7fa5bd9e-d096-445c-9574-f072185c9625",
    title: "旧PC構成",
    tagName: "家電購入",
  },
  {
    id: "2b302ee8-d0f4-4f74-aa2c-1c517b5936f8",
    title: "売却済み",
    tagName: "記録",
  },
  {
    id: "f83bcc2c-7392-4833-af4d-4a67ef130461",
    title: "未売却",
    tagName: "家電購入",
  },
  {
    id: "ff43781e-3581-48ad-a85e-9d89afc18e04",
    title: "購入候補",
    tagName: "製品候補",
  },
] as const;

type RecurringCostRow = {
  id: string;
  title: string;
  category:
    | "固定費"
    | "趣味"
    | "食費・光熱費"
    | "税・社会保険"
    | "全体合計"
    | "合計";
  provider?: string;
  monthly: number;
  annualOrContract?: string;
  condition?: string;
};

const RECURRING_COST_ROWS: RecurringCostRow[] = [
  {
    id: "601fa2ca-d64e-4e7d-a11a-b995f08ed565",
    title: "インターネット：So-net光 v6プラス 4,480円",
    category: "固定費",
    provider: "So-net光 v6プラス",
    monthly: 4480,
  },
  {
    id: "1ef468da-9b77-46cc-a217-04a135bf4571",
    title: "家賃：ハウスメイト 86,000円",
    category: "固定費",
    provider: "ハウスメイト",
    monthly: 86000,
  },
  {
    id: "279baa47-a434-4669-ac4c-267952857510",
    title: "携帯回線：LINEモバイル 0円（500MB以上使用時は1,225円）",
    category: "固定費",
    provider: "LINEモバイル",
    monthly: 0,
    condition: "500MB以上使用時は1,225円",
  },
  {
    id: "24cad883-93af-4564-a264-d516812e8887",
    title: "携帯回線：楽天モバイル 980円（3GB以上使用時は1,980円）",
    category: "固定費",
    provider: "楽天モバイル",
    monthly: 980,
    condition: "3GB以上使用時は1,980円",
  },
  {
    id: "9af5c390-451a-4378-a2b6-7f9002a413df",
    title: "総合保険：e-net 月換算750円（2年18,000円）",
    category: "固定費",
    provider: "e-net",
    monthly: 750,
    annualOrContract: "2年18,000円",
  },
  {
    id: "c4df326d-5491-4495-a7b8-0b5b45155623",
    title: "保証代行：レジデンシャルサービス 月換算1,802円（2年43,270円）",
    category: "固定費",
    provider: "レジデンシャルサービス",
    monthly: 1802,
    annualOrContract: "2年43,270円",
  },
  {
    id: "ddd21fc6-e688-4ed2-9aa3-fe744ef1a8ad",
    title: "合計：94,012円",
    category: "合計",
    monthly: 94012,
  },
  {
    id: "0c292672-8bce-4ce7-bd7a-1385428b1cd4",
    title: "Adobe CC Complete：0円（条件変更時は年39,336円）",
    category: "趣味",
    provider: "Adobe CC Complete",
    monthly: 0,
    annualOrContract: "年39,336円",
    condition: "条件変更時",
  },
  {
    id: "9f836e27-97f1-40a8-a4b1-644203b0db9d",
    title: "Amazon Prime：月換算491.6円（年5,900円）",
    category: "趣味",
    provider: "Amazon Prime",
    monthly: 491.6,
    annualOrContract: "年5,900円",
  },
  {
    id: "c81585bd-c7f3-4e1b-bdff-63d7ed6cb26c",
    title: "ChatGPT：2,987.2円（20ドル）",
    category: "趣味",
    provider: "ChatGPT",
    monthly: 2987.2,
    annualOrContract: "20ドル",
  },
  {
    id: "08d56aa8-7477-416c-a8cc-174f6f37009b",
    title: "FANBOX えなみ教授：300円",
    category: "趣味",
    provider: "FANBOX えなみ教授",
    monthly: 300,
  },
  {
    id: "a8971fbe-bb7b-47a9-bef3-22836db131ca",
    title: "FANBOX こうば：120円",
    category: "趣味",
    provider: "FANBOX こうば",
    monthly: 120,
  },
  {
    id: "8d5d240e-8df5-41d3-825c-52204b2a7eb5",
    title: "FANBOX 芦山：500円",
    category: "趣味",
    provider: "FANBOX 芦山",
    monthly: 500,
  },
  {
    id: "616cd5d6-76a7-42a9-a39e-c0fab3503b3d",
    title: "FANBOX 根岸野蓮：540円",
    category: "趣味",
    provider: "FANBOX 根岸野蓮",
    monthly: 540,
  },
  {
    id: "e3d49ef4-05c5-4886-bbb6-7623c84c2598",
    title: "FANBOX 山桃：300円",
    category: "趣味",
    provider: "FANBOX 山桃",
    monthly: 300,
  },
  {
    id: "922a8659-58d9-40ec-8507-fde02c0001c8",
    title: "FANBOX 模造クリスタル：300円",
    category: "趣味",
    provider: "FANBOX 模造クリスタル",
    monthly: 300,
  },
  {
    id: "dd74b685-bec0-468b-b465-5405122a8957",
    title: "Perplexity：2,987.2円（20ドル）",
    category: "趣味",
    provider: "Perplexity",
    monthly: 2987.2,
    annualOrContract: "20ドル",
  },
  {
    id: "4aa4f1e7-91b2-4168-9454-e04b2f0d9450",
    title: "ニコニコ動画：0円（VISA復活後は月790円）",
    category: "趣味",
    provider: "ニコニコ動画",
    monthly: 0,
    condition: "VISA復活後は月790円",
  },
  {
    id: "da1ea5e1-050c-4047-be65-3a7fcbeed538",
    title: "合計：8,526円",
    category: "合計",
    monthly: 8526,
  },
  {
    id: "7d3a5a25-5470-40bf-9b37-3e750950c409",
    title: "食費：推定40,000円",
    category: "食費・光熱費",
    provider: "食費",
    monthly: 40000,
    condition: "推定値",
  },
  {
    id: "89006808-2f97-4d40-bd0b-7e5a7fc81800",
    title: "水道：東京都水道局 月換算1,903円（2か月3,806円）",
    category: "食費・光熱費",
    provider: "東京都水道局",
    monthly: 1903,
    annualOrContract: "2か月3,806円",
  },
  {
    id: "e3ed90e2-1f2f-4662-ad09-c40d2c39a906",
    title: "電気：東京電力 月換算11,379.5円（2022年76,048円）",
    category: "食費・光熱費",
    provider: "東京電力",
    monthly: 11379.5,
    annualOrContract: "2022年76,048円",
  },
  {
    id: "d308b513-b5e5-4824-b7f0-ef2cf8fa4960",
    title: "合計：53,282.5円",
    category: "合計",
    monthly: 53282.5,
  },
  {
    id: "d413556e-0226-4e89-8c3a-ae5f90e8f6a8",
    title: "健康保険料：16,966円",
    category: "税・社会保険",
    provider: "健康保険料",
    monthly: 16966,
  },
  {
    id: "ed0a22ce-347e-4f9c-b4e2-34da5e578a73",
    title: "雇用保険：1,915円",
    category: "税・社会保険",
    provider: "雇用保険",
    monthly: 1915,
  },
  {
    id: "342b43ed-7b36-4e44-bca7-2346ffba5c2e",
    title: "厚生年金：31,110円",
    category: "税・社会保険",
    provider: "厚生年金",
    monthly: 31110,
  },
  {
    id: "4917d6b6-19d4-45ab-81ee-749a357262d0",
    title:
      "住民税：13,400円（ふるさと納税を限度額まで利用できていない可能性あり）",
    category: "税・社会保険",
    provider: "住民税",
    monthly: 13400,
    condition: "ふるさと納税を限度額まで利用できていない可能性あり",
  },
  {
    id: "4ddd21b4-9775-4695-8f85-fe2fa04c1a96",
    title: "所得税：0円（定額減税 -7,280円）",
    category: "税・社会保険",
    provider: "所得税",
    monthly: 0,
    condition: "定額減税 -7,280円",
  },
  {
    id: "f8627d92-0627-4046-b88a-b1834f630179",
    title: "合計：63,391円",
    category: "合計",
    monthly: 63391,
  },
  {
    id: "fa669be0-af05-44ab-9a4d-13866f0bdcde",
    title: "税金を含む合計：217,410円",
    category: "全体合計",
    provider: "税金を含む合計",
    monthly: 217410,
  },
  {
    id: "1462df21-b0fa-4c09-8368-3fd70dc2ff85",
    title: "税金を除く合計：154,019円",
    category: "全体合計",
    provider: "税金を除く合計",
    monthly: 154019,
  },
];

const FIELD_SPECS = [
  {
    systemKey: "recurring_cost_category",
    name: "区分",
    fieldType: "options",
    optionsJson: {
      values: [
        "固定費",
        "趣味",
        "食費・光熱費",
        "税・社会保険",
        "全体合計",
        "合計",
      ],
    },
  },
  {
    systemKey: "recurring_cost_provider",
    name: "支払先・サービス",
    fieldType: "text",
    optionsJson: {},
  },
  {
    systemKey: "recurring_cost_monthly",
    name: "月額",
    fieldType: "number",
    optionsJson: {},
  },
  {
    systemKey: "recurring_cost_contract",
    name: "年額・契約額",
    fieldType: "text",
    optionsJson: {},
  },
  {
    systemKey: "recurring_cost_condition",
    name: "条件",
    fieldType: "text",
    optionsJson: {},
  },
] as const;

function loadEnv() {
  const root = resolve(process.cwd(), "..");
  for (const envPath of [
    resolve(root, ".env"),
    resolve(process.cwd(), ".env"),
  ]) {
    if (!existsSync(envPath)) continue;
    for (const line of readFileSync(envPath, "utf8").split(/\r?\n/)) {
      const match = line.match(/^\s*([^#=]+)=(.*)$/);
      if (!match) continue;
      const key = match[1].trim();
      if (!(key in process.env))
        process.env[key] = match[2].trim().replace(/^['"]|['"]$/g, "");
    }
  }
  if (process.env.DATABASE_URL?.startsWith("postgresql+asyncpg://")) {
    process.env.DATABASE_URL = process.env.DATABASE_URL.replace(
      "postgresql+asyncpg://",
      "postgresql://",
    );
  }
}

function sameFieldValue(
  left: {
    valueJson: unknown;
    valueText: string | null;
    valueNumber: number | null;
    valueDatetime: Date | null;
    targetNodeId: string | null;
  },
  right: {
    valueJson?: unknown;
    valueText?: string | null;
    valueNumber?: number | null;
    valueDatetime?: Date | null;
    targetNodeId?: string | null;
  },
) {
  return (
    JSON.stringify(left.valueJson ?? null) ===
      JSON.stringify(right.valueJson ?? null) &&
    left.valueText === (right.valueText ?? null) &&
    left.valueNumber === (right.valueNumber ?? null) &&
    left.valueDatetime?.toISOString() === right.valueDatetime?.toISOString() &&
    left.targetNodeId === (right.targetNodeId ?? null)
  );
}

async function main() {
  loadEnv();
  const apply = process.argv.includes("--apply");
  const [{ db }, schema, writer, utils] = await Promise.all([
    import("../src/db/index"),
    import("../src/db/schema"),
    import("../src/lib/server/docs-node-writer"),
    import("../src/lib/server/knowledge-docs-utils"),
  ]);

  const collectionIds = COLLECTIONS.map((collection) => collection.id);
  const recurringRowIds = RECURRING_COST_ROWS.map((row) => row.id);
  const [collections, recurringRows, workspaceRows, tags] = await Promise.all([
    db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          eq(schema.knowledgeNodes.docsLibraryId, WORKSPACE_ID),
          inArray(schema.knowledgeNodes.id, collectionIds),
          isNull(schema.knowledgeNodes.archivedAt),
        ),
      ),
    db
      .select()
      .from(schema.knowledgeNodes)
      .where(
        and(
          eq(schema.knowledgeNodes.docsLibraryId, WORKSPACE_ID),
          inArray(schema.knowledgeNodes.id, recurringRowIds),
          isNull(schema.knowledgeNodes.archivedAt),
        ),
      ),
    db
      .select()
      .from(schema.knowledgeWorkspaces)
      .where(eq(schema.knowledgeWorkspaces.id, WORKSPACE_ID)),
    db
      .select()
      .from(schema.knowledgeSupertags)
      .where(eq(schema.knowledgeSupertags.docsLibraryId, WORKSPACE_ID)),
  ]);
  const collectionById = new Map(collections.map((row) => [row.id, row]));
  const recurringById = new Map(recurringRows.map((row) => [row.id, row]));
  const missingCollections = COLLECTIONS.filter((expected) => {
    const actual = collectionById.get(expected.id);
    return !actual || actual.title !== expected.title;
  });
  const missingRows = RECURRING_COST_ROWS.filter((expected) => {
    const actual = recurringById.get(expected.id);
    return !actual || actual.title !== expected.title;
  });
  if (
    workspaceRows.length !== 1 ||
    missingCollections.length > 0 ||
    missingRows.length > 0
  ) {
    throw new Error(
      JSON.stringify(
        {
          detail: "対象Docsデータが監査済み状態と一致しません",
          workspaceCount: workspaceRows.length,
          missingCollections: missingCollections.map((row) => row.title),
          missingRows: missingRows.map((row) => row.title),
        },
        null,
        2,
      ),
    );
  }

  const ownerUserId = workspaceRows[0]?.ownerUserId;
  if (!ownerUserId) throw new Error("Docs workspace ownerが見つかりません");
  const tagByName = new Map(tags.map((tag) => [tag.name, tag]));
  const tagBySystemKey = new Map(
    tags
      .filter((tag) => tag.systemKey)
      .map((tag) => [tag.systemKey as string, tag]),
  );
  const tagWithSystemKey = tagBySystemKey.get(RECURRING_COST_SYSTEM_KEY);
  const tagWithName = tagByName.get("定期支出");
  if (
    tagWithSystemKey &&
    tagWithName &&
    tagWithSystemKey.id !== tagWithName.id
  ) {
    throw new Error(
      "定期支出Supertagのnameとsystem_keyが別の定義に割り当てられています",
    );
  }

  console.log(
    JSON.stringify(
      {
        mode: apply ? "apply" : "dry-run",
        collections: COLLECTIONS.map((item) => item.title),
        recurringCostRows: RECURRING_COST_ROWS.length,
        existingRecurringCostTag: tagBySystemKey.has(RECURRING_COST_SYSTEM_KEY),
      },
      null,
      2,
    ),
  );
  if (!apply) return;

  const verification = await db.transaction(async (tx) => {
    await tx.execute(
      sql`select pg_advisory_xact_lock(hashtext('docs-table-collections'), hashtext(${WORKSPACE_ID}))`,
    );
    const [lockedCollections, lockedRecurringRows, lockedTags] =
      await Promise.all([
        tx
          .select()
          .from(schema.knowledgeNodes)
          .where(
            and(
              eq(schema.knowledgeNodes.docsLibraryId, WORKSPACE_ID),
              inArray(schema.knowledgeNodes.id, collectionIds),
              isNull(schema.knowledgeNodes.archivedAt),
            ),
          )
          .for("update"),
        tx
          .select()
          .from(schema.knowledgeNodes)
          .where(
            and(
              eq(schema.knowledgeNodes.docsLibraryId, WORKSPACE_ID),
              inArray(schema.knowledgeNodes.id, recurringRowIds),
              isNull(schema.knowledgeNodes.archivedAt),
            ),
          )
          .for("update"),
        tx
          .select()
          .from(schema.knowledgeSupertags)
          .where(eq(schema.knowledgeSupertags.docsLibraryId, WORKSPACE_ID))
          .for("update"),
      ]);
    const lockedCollectionById = new Map(
      lockedCollections.map((row) => [row.id, row]),
    );
    const lockedRecurringById = new Map(
      lockedRecurringRows.map((row) => [row.id, row]),
    );
    const lockedMissingCollections = COLLECTIONS.filter((expected) => {
      const actual = lockedCollectionById.get(expected.id);
      return !actual || actual.title !== expected.title;
    });
    const lockedMissingRows = RECURRING_COST_ROWS.filter((expected) => {
      const actual = lockedRecurringById.get(expected.id);
      return !actual || actual.title !== expected.title;
    });
    if (lockedMissingCollections.length > 0 || lockedMissingRows.length > 0) {
      throw new Error(
        JSON.stringify(
          {
            detail: "lock取得後に対象Docsデータの変更を検出しました",
            missingCollections: lockedMissingCollections.map(
              (row) => row.title,
            ),
            missingRows: lockedMissingRows.map((row) => row.title),
          },
          null,
          2,
        ),
      );
    }
    const lockedTagByName = new Map(lockedTags.map((tag) => [tag.name, tag]));
    const lockedTagBySystemKey = new Map(
      lockedTags
        .filter((tag) => tag.systemKey)
        .map((tag) => [tag.systemKey as string, tag]),
    );
    const lockedTagWithSystemKey = lockedTagBySystemKey.get(
      RECURRING_COST_SYSTEM_KEY,
    );
    const lockedTagWithName = lockedTagByName.get("定期支出");
    if (
      lockedTagWithSystemKey &&
      lockedTagWithName &&
      lockedTagWithSystemKey.id !== lockedTagWithName.id
    ) {
      throw new Error(
        "定期支出Supertagのnameとsystem_keyが別の定義に割り当てられています",
      );
    }
    let recurringTag =
      lockedTagBySystemKey.get(RECURRING_COST_SYSTEM_KEY) ??
      lockedTagByName.get("定期支出");
    if (recurringTag && recurringTag.name !== "定期支出") {
      throw new Error(
        `system_key ${RECURRING_COST_SYSTEM_KEY} のSupertag名が異なります: ${recurringTag.name}`,
      );
    }
    if (!recurringTag) {
      [recurringTag] = await tx
        .insert(schema.knowledgeSupertags)
        .values({
          docsLibraryId: WORKSPACE_ID,
          systemKey: RECURRING_COST_SYSTEM_KEY,
          name: "定期支出",
          baseType: "record",
          description: "毎月または定期的に発生する支出",
          icon: "table-2",
          color: "#0ea5e9",
          configJson: { default_layout: "table" },
          aiInstructions:
            "1支出=1nodeとして、区分・支払先・月額・年額や契約額・条件をfieldへ分けて更新する。",
        })
        .returning();
    } else {
      [recurringTag] = await tx
        .update(schema.knowledgeSupertags)
        .set({
          systemKey: RECURRING_COST_SYSTEM_KEY,
          baseType: "record",
          configJson: {
            ...(recurringTag.configJson as Record<string, unknown>),
            default_layout: "table",
          },
          updatedAt: new Date(),
        })
        .where(eq(schema.knowledgeSupertags.id, recurringTag.id))
        .returning();
    }

    const existingFields = await tx
      .select()
      .from(schema.knowledgeFields)
      .where(eq(schema.knowledgeFields.supertagId, recurringTag.id));
    const fieldBySystemKey = new Map(
      existingFields
        .filter((field) => field.systemKey)
        .map((field) => [field.systemKey as string, field]),
    );
    for (const [sortOrder, spec] of FIELD_SPECS.entries()) {
      let field = fieldBySystemKey.get(spec.systemKey);
      if (
        field &&
        (field.name !== spec.name ||
          field.fieldType !== spec.fieldType ||
          JSON.stringify(field.optionsJson ?? {}) !==
            JSON.stringify(spec.optionsJson))
      ) {
        throw new Error(
          `既存Field定義が移行仕様と異なります: ${spec.systemKey}`,
        );
      }
      if (!field) {
        [field] = await tx
          .insert(schema.knowledgeFields)
          .values({
            docsLibraryId: WORKSPACE_ID,
            supertagId: recurringTag.id,
            systemKey: spec.systemKey,
            name: spec.name,
            fieldType: spec.fieldType,
            optionsJson: spec.optionsJson,
            sortOrder,
          })
          .returning();
      }
      await tx
        .insert(schema.knowledgeSupertagFields)
        .values({
          supertagId: recurringTag.id,
          fieldId: field.id,
          sortOrder,
          showInTemplate: true,
        })
        .onConflictDoNothing();
      fieldBySystemKey.set(spec.systemKey, field);
    }

    await tx
      .insert(schema.knowledgeNodeSupertags)
      .values(
        RECURRING_COST_ROWS.map((row) => ({
          nodeId: row.id,
          supertagId: recurringTag.id,
          createdBy: ownerUserId,
        })),
      )
      .onConflictDoNothing();

    const values = RECURRING_COST_ROWS.flatMap((row) => {
      const rawValues: Array<[string, unknown]> = [
        ["recurring_cost_category", row.category],
        ["recurring_cost_provider", row.provider],
        ["recurring_cost_monthly", row.monthly],
        ["recurring_cost_contract", row.annualOrContract],
        ["recurring_cost_condition", row.condition],
      ];
      return rawValues.flatMap(([systemKey, value]) => {
        if (value === undefined || value === "") return [];
        const field = fieldBySystemKey.get(systemKey);
        if (!field) throw new Error(`fieldが見つかりません: ${systemKey}`);
        return [
          {
            ...utils.normalizeFieldValueInput(field, value),
            nodeId: row.id,
            updatedBy: ownerUserId,
            updatedAt: new Date(),
          },
        ];
      });
    });
    const recurringFieldIds = [...fieldBySystemKey.values()].map(
      (field) => field.id,
    );
    const existingValues = await tx
      .select()
      .from(schema.knowledgeFieldValues)
      .where(
        and(
          inArray(schema.knowledgeFieldValues.nodeId, recurringRowIds),
          inArray(schema.knowledgeFieldValues.fieldId, recurringFieldIds),
        ),
      );
    const existingByKey = new Map(
      existingValues.map((value) => [
        `${value.nodeId}:${value.fieldId}`,
        value,
      ]),
    );
    const missingValues = values.filter((value) => {
      const existing = existingByKey.get(`${value.nodeId}:${value.fieldId}`);
      if (!existing) return true;
      if (!sameFieldValue(existing, value)) {
        throw new Error(
          `既存Field値と移行値が異なります: ${value.nodeId}:${value.fieldId}`,
        );
      }
      return false;
    });
    if (missingValues.length > 0) {
      await tx.insert(schema.knowledgeFieldValues).values(missingValues);
    }

    const refreshedTags = await tx
      .select()
      .from(schema.knowledgeSupertags)
      .where(eq(schema.knowledgeSupertags.docsLibraryId, WORKSPACE_ID));
    const refreshedByName = new Map(
      refreshedTags.map((tag) => [tag.name, tag]),
    );
    const refreshedBySystemKey = new Map(
      refreshedTags
        .filter((tag) => tag.systemKey)
        .map((tag) => [tag.systemKey as string, tag]),
    );
    for (const collection of COLLECTIONS) {
      const node = lockedCollectionById.get(collection.id);
      if (!node)
        throw new Error(`collectionが見つかりません: ${collection.title}`);
      const tag =
        "tagSystemKey" in collection
          ? refreshedBySystemKey.get(collection.tagSystemKey)
          : refreshedByName.get(collection.tagName);
      if (!tag)
        throw new Error(
          `collection用Supertagが見つかりません: ${collection.title}`,
        );
      await writer.updateDocsNode(tx, node.id, {
        displayProps: {
          ...((node.displayProps as Record<string, unknown>) ?? {}),
          children_layout: "table",
          table_supertag_id: tag.id,
        },
        updatedBy: ownerUserId,
        updatedAt: new Date(),
      });
    }

    const [
      verifiedFields,
      verifiedRelations,
      verifiedValues,
      verifiedCollections,
    ] = await Promise.all([
      tx
        .select()
        .from(schema.knowledgeFields)
        .where(
          and(
            eq(schema.knowledgeFields.supertagId, recurringTag.id),
            inArray(
              schema.knowledgeFields.systemKey,
              FIELD_SPECS.map((spec) => spec.systemKey),
            ),
          ),
        ),
      tx
        .select()
        .from(schema.knowledgeNodeSupertags)
        .where(
          and(
            eq(schema.knowledgeNodeSupertags.supertagId, recurringTag.id),
            inArray(schema.knowledgeNodeSupertags.nodeId, recurringRowIds),
          ),
        ),
      tx
        .select()
        .from(schema.knowledgeFieldValues)
        .innerJoin(
          schema.knowledgeFields,
          eq(schema.knowledgeFieldValues.fieldId, schema.knowledgeFields.id),
        )
        .where(
          and(
            eq(schema.knowledgeFields.supertagId, recurringTag.id),
            inArray(
              schema.knowledgeFields.systemKey,
              FIELD_SPECS.map((spec) => spec.systemKey),
            ),
            inArray(schema.knowledgeFieldValues.nodeId, recurringRowIds),
          ),
        ),
      tx
        .select()
        .from(schema.knowledgeNodes)
        .where(
          and(
            eq(schema.knowledgeNodes.docsLibraryId, WORKSPACE_ID),
            inArray(schema.knowledgeNodes.id, collectionIds),
            isNull(schema.knowledgeNodes.archivedAt),
          ),
        ),
    ]);
    const expectedValueCount = RECURRING_COST_ROWS.reduce(
      (count, row) =>
        count +
        2 +
        Number(Boolean(row.provider)) +
        Number(Boolean(row.annualOrContract)) +
        Number(Boolean(row.condition)),
      0,
    );
    const invalidLayouts = verifiedCollections.filter((node) => {
      const collection = COLLECTIONS.find((item) => item.id === node.id);
      const tag =
        collection &&
        ("tagSystemKey" in collection
          ? refreshedBySystemKey.get(collection.tagSystemKey)
          : refreshedByName.get(collection.tagName));
      const props = (node.displayProps as Record<string, unknown>) ?? {};
      return (
        props.children_layout !== "table" || props.table_supertag_id !== tag?.id
      );
    });
    if (
      verifiedFields.length !== FIELD_SPECS.length ||
      verifiedRelations.length !== RECURRING_COST_ROWS.length ||
      verifiedValues.length !== expectedValueCount ||
      verifiedCollections.length !== COLLECTIONS.length ||
      invalidLayouts.length > 0
    ) {
      throw new Error(
        JSON.stringify(
          {
            detail: "適用後検証に失敗しました",
            fields: verifiedFields.length,
            relations: verifiedRelations.length,
            values: verifiedValues.length,
            expectedValueCount,
            collections: verifiedCollections.length,
            invalidLayouts: invalidLayouts.map((node) => node.title),
          },
          null,
          2,
        ),
      );
    }
    return {
      fields: verifiedFields.length,
      values: verifiedValues.length,
    };
  });

  console.log(
    JSON.stringify(
      {
        applied: true,
        collectionsUpdated: COLLECTIONS.length,
        recurringCostRowsUpdated: RECURRING_COST_ROWS.length,
        fieldsVerified: verification.fields,
        fieldValuesVerified: verification.values,
      },
      null,
      2,
    ),
  );
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error instanceof Error ? error.stack : String(error));
    if (error && typeof error === "object" && "cause" in error) {
      console.error("cause:", (error as { cause?: unknown }).cause);
    }
    process.exit(1);
  });
