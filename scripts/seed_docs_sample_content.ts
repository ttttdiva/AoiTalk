/**
 * Docs お手本コンテンツ seed スクリプト（D12）
 *
 * 対象: ユーザー本番ワークスペース（Home配下ノード最多・案件ページ約7件のもの）。
 * 目的: 「GPUサーバ調達検討」ページと本日のDayノードを実データとして構築し、
 *       主要Supertagに付与ノードが存在する状態にする。
 *
 * 冪等性: 各ルートページはタイトルで存在チェックし、既にあればスキップ。
 *
 * ノード作成は必ず docs-node-writer の insertDocsNode を通す（body暗号化を保証）。
 * タグ付与・フィールド値は today/route.ts / seed と同じく既存サーバーユーティリティ経由。
 * ただし drizzle クエリビルダは scripts/ から解決できないため、直接の read / タグ付与 /
 * フィールド値 insert / saved view 削除は frontend 同梱 postgres の生SQLクライアントで行う
 * （フィールド値の列は normalizeFieldValueInput が算出した値をそのまま入れる）。
 *
 * さらに dead な saved view「全案件 Risk table」行を対象ワークスペースから削除する。
 *
 * 実行:
 *   frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/seed_docs_sample_content.ts
 *
 * 接続情報はリポジトリ直下 .env の POSTGRES_*（または DATABASE_URL）から取得する。
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import postgres from "../frontend/node_modules/postgres/src/index.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "..");

const TARGET_OWNER_ID = "118eb287-43cf-43aa-bb10-c8cabaf21b0f";
const TARGET_WORKSPACE_ID = "3e73f8c6-5870-4902-821d-241d3653c4ea";
const TODAY_ISO = "2026-07-11";

function loadDotEnv(path: string): void {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return;
  }
  for (const line of raw.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq === -1) continue;
    const key = t.slice(0, eq).trim();
    let value = t.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  }
}

function connString(): string {
  if (process.env.DATABASE_URL) return process.env.DATABASE_URL;
  const user = process.env.POSTGRES_USER || "aoitalk";
  const password = process.env.POSTGRES_PASSWORD || "";
  const host = process.env.POSTGRES_HOST || "127.0.0.1";
  const port = process.env.POSTGRES_PORT || "5432";
  const dbName = process.env.POSTGRES_DB || "aoitalk_memory";
  return `postgres://${user}:${encodeURIComponent(password)}@${host}:${port}/${dbName}`;
}

async function main() {
  loadDotEnv(resolve(REPO_ROOT, ".env"));

  // env 読み込み後に app モジュールを動的 import（@/db は import 時に接続文字列を確定するため）。
  const { db } = await import("@/db");
  const { insertDocsNode } = await import("@/lib/server/docs-node-writer");
  const utils = await import("@/lib/server/knowledge-docs-utils");

  const userId = TARGET_OWNER_ID;
  const pg = postgres(connString(), { max: 1 });

  try {
    // 1) 対象ワークスペースへ seed 実行（Candidate 等の新タグ定義・フィールドを DB に用意）。
    const workspace = await utils.ensureDocsWorkspace({ id: userId });
    if (workspace.id !== TARGET_WORKSPACE_ID) {
      throw new Error(`想定外: ensureDocsWorkspace が別WSを返した (${workspace.id})。中止。`);
    }
    const wsId = workspace.id;
    console.log(`[seed] 対象ワークスペース: ${wsId} (owner ${userId})`);

    // 2) タグ・フィールド解決 -------------------------------------------------
    const tagRows = await pg`
      select id, name, system_key from knowledge_supertags where workspace_id = ${wsId}
    ` as Array<{ id: string; name: string; system_key: string | null }>;
    const tagBySys = new Map(tagRows.filter((t) => t.system_key).map((t) => [t.system_key as string, t]));
    const tagByName = new Map(tagRows.map((t) => [t.name, t]));
    const resolveTag = (opts: { systemKey?: string; name?: string }) => {
      const t = (opts.systemKey ? tagBySys.get(opts.systemKey) : undefined) ?? (opts.name ? tagByName.get(opts.name) : undefined);
      if (!t) throw new Error(`タグ未解決: ${JSON.stringify(opts)}`);
      return t;
    };

    const tagCandidate = resolveTag({ systemKey: "candidate", name: "Candidate" });
    const tagEvidence = resolveTag({ name: "Evidence", systemKey: "evidence" });
    const tagMeeting = resolveTag({ systemKey: "meeting" });
    const tagTask = resolveTag({ systemKey: "task" });
    const tagPerson = resolveTag({ systemKey: "person" });
    const tagDevice = resolveTag({ name: "Device", systemKey: "device" });
    const tagDay = resolveTag({ systemKey: "day", name: "Day" });

    // タグごとのフィールドを name -> row で引く。normalizeFieldValueInput が要求する形に整える。
    type FieldRow = {
      id: string;
      supertagId: string | null;
      workspaceId: string;
      systemKey: string | null;
      name: string;
      fieldType: string;
      required: boolean;
      optionsJson: unknown;
      defaultValueJson: unknown;
      sortOrder: number | null;
    };
    const fieldsByTag = new Map<string, Map<string, FieldRow>>();
    for (const tag of tagRows) {
      const rows = await pg`
        select id, supertag_id, workspace_id, system_key, name, field_type, required, options_json, default_value_json, sort_order
        from knowledge_fields where supertag_id = ${tag.id}
      ` as Array<Record<string, unknown>>;
      const map = new Map<string, FieldRow>();
      for (const r of rows) {
        map.set(r.name as string, {
          id: r.id as string,
          supertagId: r.supertag_id as string,
          workspaceId: r.workspace_id as string,
          systemKey: (r.system_key as string) ?? null,
          name: r.name as string,
          fieldType: r.field_type as string,
          required: !!r.required,
          optionsJson: r.options_json,
          defaultValueJson: r.default_value_json,
          sortOrder: (r.sort_order as number) ?? 0,
        });
      }
      fieldsByTag.set(tag.id, map);
    }
    const field = (tag: { id: string }, name: string): FieldRow => {
      const f = fieldsByTag.get(tag.id)?.get(name);
      if (!f) throw new Error(`フィールド未解決: tag=${tag.id} name=${name}`);
      return f;
    };

    // 生SQL ヘルパ ----------------------------------------------------------
    const tagNode = async (nodeId: string, supertagId: string) => {
      await pg`
        insert into knowledge_node_supertags (node_id, supertag_id, created_by)
        values (${nodeId}, ${supertagId}, ${userId})
        on conflict (node_id, supertag_id) do nothing
      `;
    };
    const setField = async (nodeId: string, f: FieldRow, value: unknown) => {
      // normalizeFieldValueInput は knowledgeFields 相当のオブジェクトを期待するので
      // camelCase 化した FieldRow を渡す（fieldType のみ参照される）。
      const input = utils.normalizeFieldValueInput(f as never, value);
      const vj = input.valueJson;
      await pg`
        insert into knowledge_field_values
          (node_id, field_id, value_json, value_text, value_number, value_datetime, target_node_id, updated_by)
        values (
          ${nodeId}, ${f.id},
          ${vj === null || vj === undefined ? null : pg.json(vj as never)},
          ${input.valueText ?? null},
          ${input.valueNumber ?? null},
          ${input.valueDatetime ?? null},
          ${input.targetNodeId ?? null},
          ${userId}
        )
        on conflict (node_id, field_id) do update set
          value_json = excluded.value_json,
          value_text = excluded.value_text,
          value_number = excluded.value_number,
          value_datetime = excluded.value_datetime,
          target_node_id = excluded.target_node_id,
          updated_by = excluded.updated_by
      `;
    };
    const findRootPage = async (title: string): Promise<string | null> => {
      const rows = await pg`
        select id from knowledge_nodes
        where workspace_id = ${wsId} and title = ${title} and parent_id is null and archived_at is null
        limit 1
      ` as Array<{ id: string }>;
      return rows[0]?.id ?? null;
    };

    type NodeRow = Awaited<ReturnType<typeof insertDocsNode>>;
    const createNode = async (input: {
      title: string;
      parentId: string | null;
      rootPageId: string | null;
      blockType?: string;
      nodeType?: "node" | "search" | "day" | "system";
      queryJson?: Record<string, unknown> | null;
      sortOrder: number;
      revisionSummary: string;
    }): Promise<NodeRow> => {
      const id = crypto.randomUUID();
      const node = await insertDocsNode(db, {
        id,
        workspaceId: wsId,
        parentId: input.parentId,
        rootPageId: input.rootPageId ?? id,
        projectId: null,
        systemKey: null,
        title: input.title,
        nodeType: input.nodeType ?? "node",
        bodyJson: { format: "doc_block", block_type: input.blockType ?? "paragraph" },
        displayProps: {},
        queryJson: input.queryJson ?? null,
        viewJson: {},
        dayDate: null,
        sortOrder: input.sortOrder,
        createdBy: userId,
        updatedBy: userId,
      });
      await utils.upsertKnowledgeSearchIndex(db, node, node.title);
      await utils.appendKnowledgeRevision(db, node, userId, input.revisionSummary);
      return node;
    };

    const wikilink = (nodeId: string, label: string) => `[[node:${nodeId}|${label}]]`;
    const created: string[] = [];

    // 参照先ノード Device / Person（ルート、冪等）-----------------------------
    const ensureRootTagged = async (
      title: string,
      tag: { id: string },
      fields: Array<{ name: string; value: unknown }>,
      label: string,
    ): Promise<string> => {
      const existingId = await findRootPage(title);
      if (existingId) {
        await tagNode(existingId, tag.id);
        for (const f of fields) await setField(existingId, field(tag, f.name), f.value);
        return existingId;
      }
      const node = await createNode({
        title,
        parentId: null,
        rootPageId: null,
        sortOrder: 0,
        revisionSummary: "お手本コンテンツ(参照先ノード)を作成",
      });
      await tagNode(node.id, tag.id);
      for (const f of fields) await setField(node.id, field(tag, f.name), f.value);
      created.push(`${title} #${label}`);
      return node.id;
    };

    const deviceNodeId = await ensureRootTagged(
      "HP ZGX Nano G1n AI Station",
      tagDevice,
      [
        { name: "型番", value: "ZGX Nano G1n" },
        { name: "用途", value: "LLM推論用GPUサーバ" },
      ],
      "Device",
    );
    const personNodeId = await ensureRootTagged("自分", tagPerson, [{ name: "役割", value: "調達検討担当" }], "Person");

    // ===== 内容1: GPUサーバ調達検討 =====
    let gpuSummary: string;
    if (await findRootPage("GPUサーバ調達検討")) {
      gpuSummary = "[content1] GPUサーバ調達検討 は既存のためスキップ";
      console.log(gpuSummary);
    } else {
      const gpu = await createNode({ title: "GPUサーバ調達検討", parentId: null, rootPageId: null, sortOrder: 0, revisionSummary: "お手本: GPUサーバ調達検討 ページ" });
      created.push("GPUサーバ調達検討 (root)");
      const rootId = gpu.id;
      const heading = (title: string, order: number) =>
        createNode({ title, parentId: rootId, rootPageId: rootId, blockType: "heading_2", sortOrder: order, revisionSummary: "お手本: 見出し" });

      const hGaiyo = await heading("概要", 0);
      await createNode({ title: "LLM推論用GPUサーバの調達検討。", parentId: hGaiyo.id, rootPageId: rootId, sortOrder: 0, revisionSummary: "お手本: 概要本文" });

      const hKouho = await heading("候補", 1);
      const cand8 = await createNode({ title: "HP ZGX Nano G1n ×8台", parentId: hKouho.id, rootPageId: rootId, sortOrder: 0, revisionSummary: "お手本: 候補(8台)" });
      await tagNode(cand8.id, tagCandidate.id);
      await setField(cand8.id, field(tagCandidate, "製品"), deviceNodeId);
      await setField(cand8.id, field(tagCandidate, "台数"), 8);
      await setField(cand8.id, field(tagCandidate, "本体価格"), 8003600);
      await setField(cand8.id, field(tagCandidate, "ケーブル価格"), 88000);
      await setField(cand8.id, field(tagCandidate, "状態"), "第一候補");
      await createNode({ title: "不成立時は4台構成×2へ切り替える", parentId: cand8.id, rootPageId: rootId, sortOrder: 0, revisionSummary: "お手本: 候補(8台)補足" });

      const cand4 = await createNode({ title: "HP ZGX Nano G1n ×4台 ×2セット", parentId: hKouho.id, rootPageId: rootId, sortOrder: 1, revisionSummary: "お手本: 候補(4台×2)" });
      await tagNode(cand4.id, tagCandidate.id);
      await setField(cand4.id, field(tagCandidate, "製品"), deviceNodeId);
      await setField(cand4.id, field(tagCandidate, "状態"), "代替案");

      const hKonkyo = await heading("根拠", 2);
      const evi = await createNode({ title: "Reddit性能報告", parentId: hKonkyo.id, rootPageId: rootId, sortOrder: 0, revisionSummary: "お手本: 根拠(Evidence)" });
      await tagNode(evi.id, tagEvidence.id);
      await setField(evi.id, field(tagEvidence, "対象候補"), cand8.id);
      await setField(evi.id, field(tagEvidence, "出典"), "Reddit");
      await setField(evi.id, field(tagEvidence, "信頼度"), "低");
      await createNode({ title: "推定速度は約20 tok/s との報告。", parentId: evi.id, rootPageId: rootId, sortOrder: 0, revisionSummary: "お手本: 根拠補足" });

      const hKaigi = await heading("会議", 3);
      const meeting = await createNode({ title: "定例 2026-07-08", parentId: hKaigi.id, rootPageId: rootId, sortOrder: 0, revisionSummary: "お手本: 会議(Meeting)" });
      await tagNode(meeting.id, tagMeeting.id);
      await setField(meeting.id, field(tagMeeting, "日時"), "2026-07-08");
      const meetingChild = await createNode({
        title: `${wikilink(personNodeId, "自分")}出席。ZGX Nano 8台構成で進める方向で合意。${wikilink(rootId, "GPUサーバ調達検討")}`,
        parentId: meeting.id,
        rootPageId: rootId,
        sortOrder: 0,
        revisionSummary: "お手本: 会議メモ(参照付き)",
      });
      await utils.syncKnowledgeNodeReferenceEdges(db, meetingChild, userId);

      const hTask = await heading("関連タスク", 4);
      await createNode({
        title: "この案件の未完了タスク",
        parentId: hTask.id,
        rootPageId: rootId,
        nodeType: "search",
        queryJson: {
          and: [
            { tag_system_key: "task" },
            { field: "task_status", op: "!=", value: "done" },
          ],
          include_virtual_tasks: true,
          limit: 100,
          sort: "updated_desc",
        },
        sortOrder: 0,
        revisionSummary: "お手本: Live query(未完了タスク)",
      });
      const task1 = await createNode({ title: "販社へ見積依頼", parentId: hTask.id, rootPageId: rootId, sortOrder: 1, revisionSummary: "お手本: タスク1" });
      await tagNode(task1.id, tagTask.id);
      await setField(task1.id, field(tagTask, "状態"), "todo");
      await setField(task1.id, field(tagTask, "期日"), "2026-07-15");
      const task2 = await createNode({ title: "200GbEスイッチ選定", parentId: hTask.id, rootPageId: rootId, sortOrder: 2, revisionSummary: "お手本: タスク2" });
      await tagNode(task2.id, tagTask.id);
      await setField(task2.id, field(tagTask, "状態"), "todo");

      gpuSummary = `[content1] GPUサーバ調達検討 作成完了 root=${rootId}`;
      console.log(gpuSummary);
    }

    // ===== 内容2: 本日(2026-07-11)の Day ノード =====
    let daySummary: string;
    const existingDay = await pg`
      select id from knowledge_nodes where workspace_id = ${wsId} and day_date = ${TODAY_ISO} and archived_at is null limit 1
    ` as Array<{ id: string }>;
    const gpuRootId = await findRootPage("GPUサーバ調達検討");

    if (existingDay[0]) {
      await tagNode(existingDay[0].id, tagDay.id);
      daySummary = `[content2] Day(${TODAY_ISO}) は既存 (${existingDay[0].id})。タグ付与を担保`;
      console.log(daySummary);
    } else {
      const ensureSystemNode = async (title: string, parentId: string | null, sortOrder: number): Promise<string> => {
        const ex = await pg`
          select id from knowledge_nodes
          where workspace_id = ${wsId} and title = ${title} and archived_at is null
            and ${parentId === null ? pg`parent_id is null` : pg`parent_id = ${parentId}`}
          order by created_at asc limit 1
        ` as Array<{ id: string }>;
        if (ex[0]) return ex[0].id;
        const node = await insertDocsNode(db, {
          workspaceId: wsId,
          parentId,
          rootPageId: parentId,
          title,
          description: "",
          bodyJson: { inline: [{ type: "text", text: title }] },
          nodeType: "system",
          displayProps: {},
          viewJson: {},
          sortOrder,
          createdBy: userId,
          updatedBy: userId,
        });
        return node.id;
      };
      const isoWeekNumber = (date: Date) => {
        const utc = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
        const day = utc.getUTCDay() || 7;
        utc.setUTCDate(utc.getUTCDate() + 4 - day);
        const yearStart = new Date(Date.UTC(utc.getUTCFullYear(), 0, 1));
        return Math.ceil(((utc.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
      };
      const targetDate = new Date(`${TODAY_ISO}T00:00:00+09:00`);
      const dayTitle = new Intl.DateTimeFormat("ja-JP", {
        timeZone: "Asia/Tokyo",
        year: "numeric",
        month: "short",
        day: "numeric",
        weekday: "short",
      }).format(targetDate);
      const dailyRootId = await ensureSystemNode("Daily notes", null, 10);
      const year = TODAY_ISO.slice(0, 4);
      const yearRootId = await ensureSystemNode(year, dailyRootId, Number(year));
      const weekRootId = await ensureSystemNode(`Week ${String(isoWeekNumber(targetDate)).padStart(2, "0")}`, yearRootId, isoWeekNumber(targetDate));

      const dayNode = await insertDocsNode(db, {
        workspaceId: wsId,
        parentId: weekRootId,
        rootPageId: dailyRootId,
        title: dayTitle,
        description: "",
        bodyJson: { inline: [{ type: "text", text: dayTitle }] },
        nodeType: "day",
        displayProps: {},
        viewJson: { view: "outline" },
        dayDate: TODAY_ISO,
        sortOrder: 1,
        createdBy: userId,
        updatedBy: userId,
      });
      await tagNode(dayNode.id, tagDay.id);
      await utils.upsertKnowledgeSearchIndex(db, dayNode, dayTitle);
      await utils.appendKnowledgeRevision(db, dayNode, userId, "お手本: 本日のDayノードを作成");
      created.push(`Day ${TODAY_ISO}`);

      const dayChildTitle = gpuRootId
        ? `${wikilink(gpuRootId, "GPUサーバ調達検討")}の候補整理を実施。`
        : "[[GPUサーバ調達検討]]の候補整理を実施。";
      const dayChild = await createNode({ title: dayChildTitle, parentId: dayNode.id, rootPageId: dailyRootId, sortOrder: 0, revisionSummary: "お手本: Day子(GPU参照)" });
      if (gpuRootId) await utils.syncKnowledgeNodeReferenceEdges(db, dayChild, userId);

      daySummary = `[content2] Day(${TODAY_ISO}) 作成完了 (${dayNode.id})`;
      console.log(daySummary);
    }

    // ===== dead saved view「全案件 Risk table」除去 =====
    const delViews = await pg`
      delete from knowledge_saved_views
      where workspace_id = ${wsId} and name = ${"全案件 Risk table"}
      returning id
    ` as Array<{ id: string }>;
    console.log(`[seed] 「全案件 Risk table」ビュー削除: ${delViews.length}件`);

    console.log("\n[seed] 新規作成ノード:");
    for (const c of created) console.log(`  + ${c}`);
    console.log("[seed] 完了。");
  } finally {
    await pg.end({ timeout: 5 });
  }
}

main()
  .then(() => process.exit(0))
  .catch((err) => {
    console.error("[seed] エラー:", err);
    process.exit(1);
  });
