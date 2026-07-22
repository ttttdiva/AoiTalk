/**
 * 「イラスト修正テク」だけを、原文準拠のTana風アウトラインへ直すpilot。
 * 外部APIやLLMは使用しない。公開Docsへ出典パス・行番号・hashは書き込まない。
 *
 * frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_illustration_pilot.ts preview
 * frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_illustration_pilot.ts backup
 * frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_illustration_pilot.ts apply --backup <path>
 * frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_illustration_pilot.ts verify
 * frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_illustration_pilot.ts seal --backup <path>
 * frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_illustration_pilot.ts rollback --backup <path> [--receipt <path>]
 */

import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { and, eq, inArray, or, sql } from "../frontend/node_modules/drizzle-orm/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..");
const ARTIFACT_ROOT = resolve(REPO_ROOT, "artifacts/foam_curation/illustration_repair_pilot_v1");
const DEFAULT_RECEIPT_PATH = resolve(ARTIFACT_ROOT, "applied-state.json");
const SOURCE_FILE = "D:/Dev/00_foam/Permanent_Notes/イラスト修正テク.md";
const OTHER_SOURCE_FILE = "D:/Dev/00_foam/Permanent_Notes/その他倉庫.md";
const SOURCE_SHA256 = "b306fffd052cc25cc6eff10f116c060d317d5c66e1e2efcfd2c2c2a929c34570";
const OTHER_SOURCE_SHA256 = "23eb861b9ce65d98e5bd07465546c3934dd85e34035469b72d978c880cdc07e1";
const WORKSPACE_ID = "3e73f8c6-5870-4902-821d-241d3653c4ea";
const OWNER_ID = "118eb287-43cf-43aa-bb10-c8cabaf21b0f";
const ROOT_ID = "c86afe66-8741-495e-b7f0-2edc673a1eda";
const OTHER_DUPLICATE_ID = "82c60972-f2bd-434f-be2f-88e4164425d4";
const OTHER_WAREHOUSE_ID = "5ccb8877-954e-4ce5-a053-ca24c912daf7";
const MIGRATION_KEY = "docs_illustration_repair_pilot_v1";
const REVISION_SUMMARY = "イラスト修正テク pilot v1";
const DOCS_ATTACHMENT_ROOT = resolve(REPO_ROOT, "workspaces/_docs/attachments");
const OTHER_DUPLICATE_URL = "https://kurokumasoft.com/2023/03/10/colorize-drawings-with-controlnet/";
const OTHER_DUPLICATE_BODY = `${OTHER_DUPLICATE_URL}\nhttps://minorgame.syowp.com/archives/lineart-controlnet.html`;

type TagName = "手法" | "ツール" | "資料";
type Technique = {
  key: string;
  sourceTitle?: string;
  title: string;
  tag: TagName;
  url?: string;
  settings?: string;
  images?: string;
  details?: string[];
};

const TECHNIQUES: Technique[] = [
  {
    key: "content-aware-move",
    sourceTitle: "コンテンツに応じた移動ツール（なんちゃって魔法）",
    title: "コンテンツに応じた移動ツール - フォトショで加筆せず指や髪の毛を修正",
    tag: "手法",
    url: "https://sp8999.com/drawing-retouch/2023/04/06/1142/",
  },
  {
    key: "controlnet-tile",
    sourceTitle: "自力で修正してCN Tile",
    title: "CN Tile - 自力修正後の画像をTileモデルのi2iで馴染ませる",
    tag: "手法",
    images: [
      "![](../attachments/2023-05-22-01-37-20.jpeg)",
      "![](../attachments/2023-05-22-01-37-24.jpeg)",
      "![](../attachments/2023-05-22-01-37-27.jpeg)",
      "![](../attachments/2023-05-22-01-37-29.jpeg)",
      "![](../attachments/2023-05-22-01-37-33.jpeg)",
    ].join("\n"),
    details: [
      "粗い手修正でも、Tileモデルのi2iなら自然に馴染ませやすく操作もしやすい。",
    ],
  },
  {
    key: "i2i-hand-lottery",
    sourceTitle: "i2iガチャで手だけの当たりを出してコラi2i",
    title: "i2iガチャ - DN0.4〜0.5で手の当たりを出し、切り取ってベース画像へ合成",
    tag: "手法",
    settings: "DN0.4〜0.5",
    details: [
      "効率的に直すなら、フォトショなどの画像加工ソフトが必要ですね。",
      "破綻が大きい場合は、そのままベース画像をDN0.4〜0.5くらいで何度かi2iして、当たりを引いたら手の部分を切り取ってベースに合成するのが早いです。",
      "欲しい指の形が決まっている場合は、加筆して誘導する必要があります。",
      "ペインターで出来る範囲でひとまずやってみるのが良いかもしれません。",
    ],
  },
  {
    key: "negative-ti-inpaint",
    sourceTitle: "Negative TI + inpaint",
    title: "Negative TI + inpaint - badhandsv4と指用ネガティブ指定後、加筆してi2iで馴染ませる",
    tag: "手法",
    details: [
      "①badhandsv4突っ込んで祈る。",
      "②missing finger, extra finger fusion finger,を倍率1.4でネガティブプロンプトに突っ込む",
      "③比較的低解像度で出力した画像に直接加筆修正して、i2iで加筆の違和感を消す",
    ],
  },
  {
    key: "hakuimg",
    sourceTitle: "HakuImg（Extension）を使った効率的なi2i",
    title: "HakuImg - Extensionを使って効率的にi2iする",
    tag: "ツール",
    url: "https://sp8999.com/stable-diffusion/2023/04/02/1065/",
  },
  {
    key: "controlnet-pix2pix",
    sourceTitle: "CN pix2pix",
    title: "CN pix2pix - pix2pixを参照",
    tag: "手法",
    details: ["[[pix2pix]]"],
  },
  {
    key: "controlnet-inpaint",
    sourceTitle: "CN+Inpaint",
    title: "CN+Inpaint - Depth Libraryと組み合わせ、手だけを修正",
    tag: "手法",
    details: ["Depth Libraryと組み合わせて手だけ綺麗にする試み", "Strength上げればまぁまぁ行ける"],
  },
  {
    key: "openpose-hed",
    sourceTitle: "openpose+hed",
    title: "openpose+hed - ポーズを指定しながら詳細な背景と正しい手を描画",
    tag: "手法",
    url: "https://twitter.com/Zuntan03/status/1629739504801320960",
  },
  {
    key: "expression-inpaint",
    sourceTitle: "表情Inpaint",
    title: "表情Inpaint - 表情CNまたはCN Inpaintモデルを使う",
    tag: "手法",
    details: ["表情CNはかなり難易度高そう"],
  },
  {
    key: "sam-inpaint",
    sourceTitle: "SAM＋Inpaint",
    title: "SAM＋Inpaint - SAMのSegへ点や線でマスクを指定",
    tag: "手法",
    details: ["SAMを使ったSegに点や線を書いて使うマスクを指定するナウい方法", "[[20230522011725|inpaint-anything]]"],
  },
  {
    key: "adetailer",
    sourceTitle: "After Detailer(ADtailer)",
    title: "ADetailer - 顔や指など指定部位を検出し、その部分だけ詳細化",
    tag: "ツール",
    url: "https://github.com/Bing-su/adetailer",
    details: ["かなり精度良さそう"],
  },
  {
    key: "lineart-coloring",
    sourceTitle: "自動着色",
    title: "自動着色 - 黒地に白線を読み込み、lineartモデルで主線を合わせる",
    tag: "手法",
    url: "https://twitter.com/abubu_newnanka/status/1661293991269376005",
    settings: "Preprocessor : none、Model：任意のlineartモデル",
    details: [
      "普通にlineart使用のことだった",
      "はい、すみません。AI着色の使い方間違ってました。自作線画に着色させたい場合は、「黒地に白線」の線画をcontrolnetに読ませて、「Preprocessor : none、Model：任意のlineartモデル」で生成を行います。これで生成物の主線と読ませた線画は９９％一致します。",
    ],
  },
  {
    key: "lama-cleaner-inpaint",
    sourceTitle: "オーソドックスな手法",
    title: "Lama-Cleaner＋Inpaint - 余計な指を消し、クリスタで微調整後に馴染ませる",
    tag: "手法",
    url: "https://twitter.com/rootport/status/1669156392677298176",
  },
  {
    key: "texture-guide",
    sourceTitle: "まとめガイド",
    title: "テクスチャ法・カムカム法 - ノイズやテクスチャで描き込み量を増やす資料",
    tag: "資料",
    url: "https://note.com/mitsukinozomi/n/n1c5913239ed8",
  },
  {
    key: "nijijourney-guide",
    sourceTitle: "Nijijourney 利用指南",
    title: "Nijijourney利用指南 - niji.academyのガイド",
    tag: "資料",
    url: "https://www.niji.academy/",
  },
  {
    key: "sdxl-sd1-inpaint",
    sourceTitle: "SDXL＋SD1.x系 Inpaint",
    title: "SDXL＋SD1.x Inpaint - SDXLの構図・背景とSD1.xの人物描写を組み合わせる",
    tag: "手法",
    details: [
      "SDXL系で生成した後、人物を自動でマスクしてSD1.x系で描き直すワークフローを置いときます",
      "SDXL系の構図力&背景力とSD1.x系の人物描写力のいいとこどりになる…という狙い",
    ],
  },
  {
    key: "manual-light-shadow",
    sourceTitle: "手動で陰影追加",
    title: "手動で陰影追加 - 元絵をぼかして加算し、不要な光を消す",
    tag: "手法",
    images: ["![](../attachments/2023-10-07-16-30-14.jpeg)", "![](../attachments/2023-10-07-16-30-21.jpeg)"].join("\n"),
    details: [
      "1.元絵を複製してボケボケにした画像を加算で重ねます。",
      "2.光源を考えて、暗くなりそうな所、光ってなくて良い所を消しゴムで消します(加算素材の方)",
    ],
  },
  {
    key: "grisaille",
    sourceTitle: "グリザイユ色塗り",
    title: "グリザイユ色塗り - Xの参照ポスト",
    tag: "資料",
    url: "https://x.com/Lhata4564/status/1719393869987762259?s=20",
  },
  {
    key: "wanke-course",
    sourceTitle: "WANKE AIイラスト講座",
    title: "WANKE AIイラスト講座 - fastcampus.co.krの講座",
    tag: "資料",
    url: "https://fastcampus.co.kr/dgn_online_wankeai",
  },
  {
    key: "composition-video",
    sourceTitle: "画面構成 解説",
    title: "画面構成 - 多摩美プロダクトデザイン入試色彩1位の解説",
    tag: "資料",
    url: "https://www.youtube.com/watch?v=su8fBFRqKHw&list=PLM47kap_rYWGbjG75ONxHIPsS7iozC9kz",
  },
  {
    key: "copainter-base-color",
    sourceTitle: "Copainter 下塗り",
    title: "Copainter下塗り - Xの参照ポスト",
    tag: "資料",
    url: "https://x.com/reiwagonen/status/1918333953662226919",
  },
  {
    key: "controlnet-color-article",
    title: "ControlNetで線画へ着色 - 参照記事",
    tag: "資料",
    url: "https://kurokumasoft.com/2023/03/10/colorize-drawings-with-controlnet/",
  },
  {
    key: "lineart-controlnet-article",
    title: "lineart ControlNet - 参照記事",
    tag: "資料",
    url: "https://minorgame.syowp.com/archives/lineart-controlnet.html",
  },
];

const DUPLICATE_SECTION_TITLE = "Psコンテンツに応じた移動ツール（なんちゃって魔法）";

function loadDotEnv(path: string) {
  if (!existsSync(path)) return;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const match = line.match(/^\s*([^#=]+)=(.*)$/);
    if (!match) continue;
    const key = match[1].trim();
    let value = match[2].trim();
    if ((value.startsWith("\"") && value.endsWith("\"")) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    if (!(key in process.env)) process.env[key] = value;
  }
}

function sha256(value: string | Buffer) {
  return createHash("sha256").update(value).digest("hex");
}

function stableUuid(value: string) {
  const chars = sha256(value).slice(0, 32).split("");
  chars[12] = "4";
  chars[16] = ((parseInt(chars[16], 16) & 3) | 8).toString(16);
  const hex = chars.join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function atomicWrite(path: string, value: string) {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.tmp-${process.pid}`;
  writeFileSync(temporary, value, "utf8");
  renameSync(temporary, path);
}

function stableValue(value: any): any {
  if (value instanceof Date) return value.toISOString();
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]));
  }
  return value;
}

function protectedFingerprint(nodes: any[]) {
  return sha256(JSON.stringify(stableValue([...nodes].sort((left, right) => String(left.id).localeCompare(String(right.id))))));
}

function backupNode(node: any, writer: any) {
  const { bodyJson: _bodyJson, bodyText: _bodyText, ...rest } = node;
  return {
    ...rest,
    bodyJsonDecrypted: writer.decryptDocsNodeBodyJson(node.bodyJson),
  };
}

function readVerifiedBackup(path: string) {
  if (!existsSync(path)) throw new Error(`backup not found: ${path}`);
  const backupText = readFileSync(path, "utf8");
  const checksumPath = `${path}.sha256`;
  if (!existsSync(checksumPath)) throw new Error(`backup checksum not found: ${checksumPath}`);
  const expected = readFileSync(checksumPath, "utf8").trim().split(/\s+/)[0];
  const actual = sha256(backupText);
  if (expected !== actual) throw new Error(`backup checksum mismatch: expected=${expected} actual=${actual}`);
  const backup = JSON.parse(backupText);
  if (backup.schema_version !== "docs-illustration-pilot-backup/v1") throw new Error("invalid backup schema");
  return { backup, backupText, checksum: actual };
}

function readVerifiedReceipt(path: string) {
  if (!existsSync(path)) throw new Error(`applied-state receipt not found: ${path}`);
  const text = readFileSync(path, "utf8");
  const checksumPath = `${path}.sha256`;
  if (!existsSync(checksumPath)) throw new Error(`applied-state checksum not found: ${checksumPath}`);
  const expected = readFileSync(checksumPath, "utf8").trim().split(/\s+/)[0];
  const actual = sha256(text);
  if (expected !== actual) throw new Error(`applied-state checksum mismatch: expected=${expected} actual=${actual}`);
  const receipt = JSON.parse(text);
  if (receipt.schema_version !== "docs-illustration-pilot-applied-state/v1") throw new Error("invalid applied-state schema");
  return receipt;
}

function arg(name: string) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] ?? null : null;
}

function assertSources() {
  const sourceBuffer = readFileSync(SOURCE_FILE);
  const otherBuffer = readFileSync(OTHER_SOURCE_FILE);
  const sourceHash = sha256(sourceBuffer);
  const otherHash = sha256(otherBuffer);
  if (sourceHash !== SOURCE_SHA256) throw new Error(`イラスト修正テク.md changed: ${sourceHash}`);
  if (otherHash !== OTHER_SOURCE_SHA256) throw new Error(`その他倉庫.md changed: ${otherHash}`);
  const rawSources = `${sourceBuffer.toString("utf8")}\n${otherBuffer.toString("utf8")}`;
  const verbatimValues = [
    ...TECHNIQUES.flatMap((technique) => technique.url ? [technique.url] : []),
    ...TECHNIQUES.flatMap((technique) => technique.settings ? [technique.settings] : []),
    ...TECHNIQUES.flatMap((technique) => technique.images ? technique.images.split("\n") : []),
    "②missing finger, extra finger fusion finger,を倍率1.4でネガティブプロンプトに突っ込む",
  ];
  const missingVerbatim = verbatimValues.filter((value) => !rawSources.includes(value));
  if (missingVerbatim.length > 0) throw new Error(`verbatim values changed: ${missingVerbatim.join(" | ")}`);
  return { sourceHash, otherHash };
}

async function context() {
  loadDotEnv(resolve(REPO_ROOT, ".env"));
  if (process.env.DATABASE_URL?.startsWith("postgresql+asyncpg://")) {
    process.env.DATABASE_URL = process.env.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://");
  }
  const [{ db }, schema, writer, docsUtils] = await Promise.all([
    import("@/db"),
    import("@/db/schema"),
    import("@/lib/server/docs-node-writer"),
    import("@/lib/server/knowledge-docs-utils"),
  ]);
  return { db, schema, writer, docsUtils };
}

async function relevantNodes(db: any, schema: any) {
  const descendants = await db.execute(sql`
    with recursive target_tree as (
      select id from knowledge_nodes where id=${ROOT_ID} and workspace_id=${WORKSPACE_ID}
      union all
      select child.id
      from knowledge_nodes child
      join target_tree parent on child.parent_id=parent.id
      where child.workspace_id=${WORKSPACE_ID}
    )
    select id::text from target_tree
  `);
  const ids = [...new Set([...descendants.map((row: any) => String(row.id)), OTHER_DUPLICATE_ID])];
  return db.select().from(schema.knowledgeNodes).where(inArray(schema.knowledgeNodes.id, ids));
}

async function audit(db: any, schema: any) {
  const nodes = await relevantNodes(db, schema);
  const root = nodes.find((node: any) => node.id === ROOT_ID);
  const direct = nodes.filter((node: any) => node.parentId === ROOT_ID && !node.archivedAt).sort((a: any, b: any) => (a.sortOrder ?? 0) - (b.sortOrder ?? 0));
  const generated = nodes.filter((node: any) => String(node.systemKey ?? "").startsWith(`${MIGRATION_KEY}:`));
  const duplicate = nodes.find((node: any) => node.id === OTHER_DUPLICATE_ID);
  const [tagLinks, fieldValues, placements] = await Promise.all([
    db.select().from(schema.knowledgeNodeSupertags).where(inArray(schema.knowledgeNodeSupertags.nodeId, nodes.map((node: any) => node.id))),
    db.select().from(schema.knowledgeFieldValues).where(inArray(schema.knowledgeFieldValues.nodeId, nodes.map((node: any) => node.id))),
    db.select().from(schema.knowledgeNodePlacements).where(or(
      inArray(schema.knowledgeNodePlacements.nodeId, nodes.map((node: any) => node.id)),
      inArray(schema.knowledgeNodePlacements.parentNodeId, nodes.map((node: any) => node.id)),
    )),
  ]);
  return {
    root,
    nodes,
    direct,
    generated,
    duplicate,
    tagLinks,
    fieldValues,
    placements,
    summary: {
      root_title: root?.title ?? null,
      active_direct_count: direct.length,
      active_direct_titles: direct.map((node: any) => node.title),
      generated_count: generated.length,
      tag_link_count: tagLinks.length,
      field_value_count: fieldValues.length,
      other_duplicate_archived: Boolean(duplicate?.archivedAt),
    },
  };
}

function assertOtherDuplicateIdentity(node: any, writer: any) {
  if (!node) throw new Error("その他倉庫 duplicate not found");
  const decrypted = writer.decryptDocsNodeBodyJson(node.bodyJson);
  const checks = {
    id: node.id === OTHER_DUPLICATE_ID,
    workspace: node.workspaceId === WORKSPACE_ID,
    parent: node.parentId === OTHER_WAREHOUSE_ID,
    title: node.title === "イラスト修正テク",
    description: node.description === OTHER_DUPLICATE_URL,
    body: decrypted?.verbatim_content === OTHER_DUPLICATE_BODY,
  };
  if (Object.values(checks).some((matches) => !matches)) {
    throw new Error(`その他倉庫 duplicate identity/content mismatch: ${JSON.stringify(checks)}`);
  }
}

function sortedRows(rows: any[]) {
  return [...rows].sort((left, right) => JSON.stringify(stableValue(left)).localeCompare(JSON.stringify(stableValue(right))));
}

async function appliedStateFingerprint(db: any, schema: any, writer: any) {
  const state = await audit(db, schema);
  const ids = state.nodes.map((node: any) => node.id);
  const [searchRows, revisions, tags, fields] = await Promise.all([
    ids.length ? db.select().from(schema.knowledgeSearchIndex).where(inArray(schema.knowledgeSearchIndex.nodeId, ids)) : [],
    ids.length ? db.select().from(schema.knowledgeRevisions).where(and(
      inArray(schema.knowledgeRevisions.nodeId, ids),
      eq(schema.knowledgeRevisions.changeSummary, REVISION_SUMMARY),
    )) : [],
    db.select().from(schema.knowledgeSupertags).where(and(
      eq(schema.knowledgeSupertags.workspaceId, WORKSPACE_ID),
      inArray(schema.knowledgeSupertags.name, ["手法", "ツール", "資料"]),
    )),
    db.select().from(schema.knowledgeFields).where(and(
      eq(schema.knowledgeFields.workspaceId, WORKSPACE_ID),
      inArray(schema.knowledgeFields.name, ["URL", "設定", "画像"]),
    )),
  ]);
  const tagIds = tags.map((tag: any) => tag.id);
  const fieldIds = fields.map((field: any) => field.id);
  const relations = tagIds.length && fieldIds.length
    ? await db.select().from(schema.knowledgeSupertagFields).where(and(
        inArray(schema.knowledgeSupertagFields.supertagId, tagIds),
        inArray(schema.knowledgeSupertagFields.fieldId, fieldIds),
      ))
    : [];
  return sha256(JSON.stringify(stableValue({
    nodes: sortedRows(state.nodes.map((node: any) => backupNode(node, writer))),
    node_supertags: sortedRows(state.tagLinks),
    field_values: sortedRows(state.fieldValues),
    placements: sortedRows(state.placements),
    search_rows: sortedRows(searchRows),
    revisions: sortedRows(revisions),
    supertags: sortedRows(tags),
    fields: sortedRows(fields),
    supertag_fields: sortedRows(relations),
  })));
}

function prestateFingerprint(snapshot: any) {
  return sha256(JSON.stringify(stableValue({
    nodes: sortedRows(snapshot.nodes ?? []),
    node_supertags: sortedRows(snapshot.node_supertags ?? []),
    field_values: sortedRows(snapshot.field_values ?? []),
    placements: sortedRows(snapshot.placements ?? []),
    search_rows: sortedRows(snapshot.search_rows ?? []),
    supertag_fields: sortedRows(snapshot.supertag_fields ?? []),
    supertags: sortedRows(snapshot.supertags ?? []),
    fields: sortedRows(snapshot.fields ?? []),
  })));
}

async function capturePrestate(db: any, schema: any, writer: any, auditState?: Awaited<ReturnType<typeof audit>>) {
  const state = auditState ?? await audit(db, schema);
  const ids = state.nodes.map((node: any) => node.id);
  const [searchRows, nodeTags, fieldValues, placements, supertags, fields] = await Promise.all([
    db.select().from(schema.knowledgeSearchIndex).where(inArray(schema.knowledgeSearchIndex.nodeId, ids)),
    db.select().from(schema.knowledgeNodeSupertags).where(inArray(schema.knowledgeNodeSupertags.nodeId, ids)),
    db.select().from(schema.knowledgeFieldValues).where(inArray(schema.knowledgeFieldValues.nodeId, ids)),
    db.select().from(schema.knowledgeNodePlacements).where(or(
      inArray(schema.knowledgeNodePlacements.nodeId, ids),
      inArray(schema.knowledgeNodePlacements.parentNodeId, ids),
    )),
    db.select().from(schema.knowledgeSupertags).where(and(
      eq(schema.knowledgeSupertags.workspaceId, WORKSPACE_ID),
      inArray(schema.knowledgeSupertags.name, ["手法", "ツール", "資料"]),
    )),
    db.select().from(schema.knowledgeFields).where(and(
      eq(schema.knowledgeFields.workspaceId, WORKSPACE_ID),
      inArray(schema.knowledgeFields.name, ["URL", "設定", "画像"]),
    )),
  ]);
  const tagIds = supertags.map((tag: any) => tag.id);
  const fieldIds = fields.map((field: any) => field.id);
  const supertagFields = tagIds.length && fieldIds.length
    ? await db.select().from(schema.knowledgeSupertagFields).where(and(
        inArray(schema.knowledgeSupertagFields.supertagId, tagIds),
        inArray(schema.knowledgeSupertagFields.fieldId, fieldIds),
      ))
    : [];
  return {
    nodes: state.nodes.map((node: any) => backupNode(node, writer)),
    node_supertags: nodeTags,
    field_values: fieldValues,
    placements,
    search_rows: searchRows,
    supertag_fields: supertagFields,
    supertags,
    fields,
  };
}

async function sealAppliedState(backupPath: string, receiptPath = DEFAULT_RECEIPT_PATH) {
  const hashes = assertSources();
  const { backup, checksum: backupChecksum } = readVerifiedBackup(backupPath);
  if (backup.hashes?.sourceHash !== hashes.sourceHash || backup.hashes?.otherHash !== hashes.otherHash) {
    throw new Error("backup source hash mismatch");
  }
  const { db, schema, writer } = await context();
  const fingerprint = await db.transaction(async (tx: any) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${WORKSPACE_ID}:${MIGRATION_KEY}`}))`);
    await verify(tx);
    const state = await audit(tx, schema);
    assertOtherDuplicateIdentity(state.duplicate, writer);
    return appliedStateFingerprint(tx, schema, writer);
  });
  const payload = {
    schema_version: "docs-illustration-pilot-applied-state/v1",
    created_at: new Date().toISOString(),
    hashes,
    backup_sha256: backupChecksum,
    applied_fingerprint: fingerprint,
  };
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  atomicWrite(receiptPath, text);
  atomicWrite(`${receiptPath}.sha256`, `${sha256(text)}  ${receiptPath.split(/[\\/]/).at(-1)}\n`);
  return { receipt: receiptPath, applied_fingerprint: fingerprint };
}

function assertLegacyShape(auditResult: Awaited<ReturnType<typeof audit>>) {
  if (!auditResult.root || auditResult.root.title !== "イラスト修正テク") throw new Error("canonical イラスト修正テク root not found");
  if (auditResult.generated.length > 0) return;
  const expected = new Set([...TECHNIQUES.flatMap((item) => item.sourceTitle ? [item.sourceTitle] : []), DUPLICATE_SECTION_TITLE]);
  const actual = new Set(auditResult.direct.map((node: any) => node.title));
  const missing = [...expected].filter((title) => !actual.has(title));
  if (missing.length > 0) throw new Error(`legacy sections missing: ${missing.join(", ")}`);
  if (auditResult.direct.length !== 22) throw new Error(`unexpected legacy direct count: ${auditResult.direct.length}`);
}

async function createBackup() {
  const hashes = assertSources();
  const { db, schema, writer } = await context();
  const state = await audit(db, schema);
  assertLegacyShape(state);
  assertOtherDuplicateIdentity(state.duplicate, writer);
  const prestate = await capturePrestate(db, schema, writer, state);
  const createdAt = new Date().toISOString();
  const payload = {
    schema_version: "docs-illustration-pilot-backup/v1",
    created_at: createdAt,
    hashes,
    protected_fingerprint: protectedFingerprint(prestate.nodes),
    prestate_fingerprint: prestateFingerprint(prestate),
    ...prestate,
  };
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  const path = resolve(ARTIFACT_ROOT, `backup-${createdAt.replace(/[:.]/g, "-")}.json`);
  atomicWrite(path, text);
  atomicWrite(`${path}.sha256`, `${sha256(text)}  ${path.split(/[\\/]/).at(-1)}\n`);
  return { backup: path, sha256: sha256(text), protected_nodes: state.nodes.length, summary: state.summary };
}

async function ensureTagsAndFields(tx: any, schema: any) {
  const tags = new Map<TagName, any>();
  for (const [index, name] of (["手法", "ツール", "資料"] as TagName[]).entries()) {
    const matches = await tx.select().from(schema.knowledgeSupertags).where(and(
      eq(schema.knowledgeSupertags.workspaceId, WORKSPACE_ID),
      eq(schema.knowledgeSupertags.name, name),
    ));
    if (matches.length > 1) throw new Error(`ambiguous supertag: ${name}`);
    const tag = matches[0] ?? (await tx.insert(schema.knowledgeSupertags).values({
      id: stableUuid(`${MIGRATION_KEY}:tag:${name}`),
      workspaceId: WORKSPACE_ID,
      systemKey: `${MIGRATION_KEY}:tag:${name}`,
      name,
      baseType: "note",
      description: name === "手法" ? "再現可能な手順や組み合わせ" : name === "ツール" ? "使用する拡張機能やソフト" : "後で参照する記事・動画・講座",
      color: ["#2563eb", "#7c3aed", "#0f766e"][index],
      templateJson: {},
      configJson: {},
      pinnedFieldIds: [],
    }).returning())[0];
    tags.set(name, tag);
  }

  const fields = new Map<string, any>();
  const definitions = [
    { name: "URL", type: "url", tags: ["手法", "ツール", "資料"] as TagName[] },
    { name: "設定", type: "long_text", tags: ["手法"] as TagName[] },
    { name: "画像", type: "long_text", tags: ["手法"] as TagName[] },
  ];
  for (const [index, definition] of definitions.entries()) {
    const matches = await tx.select().from(schema.knowledgeFields).where(and(
      eq(schema.knowledgeFields.workspaceId, WORKSPACE_ID),
      eq(schema.knowledgeFields.name, definition.name),
      eq(schema.knowledgeFields.fieldType, definition.type),
    ));
    if (matches.length > 1) throw new Error(`ambiguous field: ${definition.name}`);
    const owner = tags.get(definition.tags[0])!;
    const field = matches[0] ?? (await tx.insert(schema.knowledgeFields).values({
      id: stableUuid(`${MIGRATION_KEY}:field:${definition.name}`),
      workspaceId: WORKSPACE_ID,
      supertagId: owner.id,
      systemKey: `${MIGRATION_KEY}:field:${definition.name}`,
      name: definition.name,
      fieldType: definition.type,
      required: false,
      optionsJson: {},
      sortOrder: index + 1,
    }).returning())[0];
    fields.set(definition.name, field);
    for (const [sortOrder, tagName] of definition.tags.entries()) {
      await tx.insert(schema.knowledgeSupertagFields).values({
        supertagId: tags.get(tagName)!.id,
        fieldId: field.id,
        sortOrder: sortOrder + index + 1,
        required: false,
        showInTemplate: true,
        optional: true,
      }).onConflictDoNothing();
    }
  }
  return { tags, fields };
}

async function saveFieldValue(tx: any, schema: any, nodeId: string, field: any, value: string | undefined) {
  await tx.delete(schema.knowledgeFieldValues).where(and(
    eq(schema.knowledgeFieldValues.nodeId, nodeId),
    eq(schema.knowledgeFieldValues.fieldId, field.id),
  ));
  if (!value) return;
  await tx.insert(schema.knowledgeFieldValues).values({
    nodeId,
    fieldId: field.id,
    valueJson: value,
    valueText: value,
    updatedBy: OWNER_ID,
    updatedAt: new Date(),
  });
}

function imageReferences(value: string | undefined) {
  if (!value) return [];
  return Array.from(value.matchAll(/!\[[^\]]*\]\(([^)]+)\)/g), (match) => match[1]).filter(Boolean) as string[];
}

function imageMimeType(path: string) {
  const extension = extname(path).toLowerCase();
  if (extension === ".jpg" || extension === ".jpeg") return "image/jpeg";
  if (extension === ".png") return "image/png";
  if (extension === ".webp") return "image/webp";
  if (extension === ".gif") return "image/gif";
  return "application/octet-stream";
}

async function syncTechniqueAttachments(tx: any, schema: any, nodeId: string, technique: Technique) {
  const references = imageReferences(technique.images);
  const expectedIds = references.map((reference) => stableUuid(`${MIGRATION_KEY}:attachment:${nodeId}:${reference}`));
  const existing = await tx.select().from(schema.knowledgeAttachments).where(eq(schema.knowledgeAttachments.nodeId, nodeId));
  for (const attachment of existing) {
    if (attachment.attachmentMetadata?.migration_key === MIGRATION_KEY && !expectedIds.includes(attachment.id)) {
      await tx.delete(schema.knowledgeAttachments).where(eq(schema.knowledgeAttachments.id, attachment.id));
    }
  }
  for (const reference of references) {
    const sourcePath = resolve(dirname(SOURCE_FILE), reference);
    if (!existsSync(sourcePath)) throw new Error(`image attachment not found: ${sourcePath}`);
    const destinationDirectory = resolve(DOCS_ATTACHMENT_ROOT, nodeId);
    mkdirSync(destinationDirectory, { recursive: true });
    const fileName = basename(sourcePath);
    const destinationPath = resolve(destinationDirectory, fileName);
    copyFileSync(sourcePath, destinationPath);
    const values = {
      id: stableUuid(`${MIGRATION_KEY}:attachment:${nodeId}:${reference}`),
      nodeId,
      fileName,
      filePath: destinationPath,
      mimeType: imageMimeType(destinationPath),
      sizeBytes: statSync(destinationPath).size,
      attachmentMetadata: { migration_key: MIGRATION_KEY, original_reference: reference },
      createdBy: OWNER_ID,
    };
    await tx.insert(schema.knowledgeAttachments).values(values).onConflictDoUpdate({
      target: schema.knowledgeAttachments.id,
      set: {
        fileName: values.fileName,
        filePath: values.filePath,
        mimeType: values.mimeType,
        sizeBytes: values.sizeBytes,
        attachmentMetadata: values.attachmentMetadata,
      },
    });
  }
}

async function apply(backupPath: string) {
  assertSources();
  const { backup } = readVerifiedBackup(backupPath);
  if (backup.hashes?.sourceHash !== SOURCE_SHA256 || backup.hashes?.otherHash !== OTHER_SOURCE_SHA256) throw new Error("backup source hash mismatch");
  const { db, schema, writer, docsUtils } = await context();
  return db.transaction(async (tx: any) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${WORKSPACE_ID}:${MIGRATION_KEY}`}))`);
    const before = await audit(tx, schema);
    if (before.generated.length > 0) return { status: "already_applied", verification: await verify(tx) };
    assertLegacyShape(before);
    assertOtherDuplicateIdentity(before.duplicate, writer);
    if (!backup.prestate_fingerprint) throw new Error("backup lacks full pre-state fingerprint; create a new backup before apply");
    const currentPrestate = await capturePrestate(tx, schema, writer, before);
    if (prestateFingerprint(currentPrestate) !== backup.prestate_fingerprint) {
      throw new Error("backup is stale for current full DB state");
    }
    const expectedFingerprint = backup.protected_fingerprint ?? protectedFingerprint(backup.nodes);
    const currentFingerprint = protectedFingerprint(before.nodes.map((node: any) => backupNode(node, writer)));
    if (currentFingerprint !== expectedFingerprint) {
      throw new Error(`backup is stale for current DB state: expected=${expectedFingerprint} actual=${currentFingerprint}`);
    }
    const root = before.root!;
    const byTitle = new Map(before.direct.map((node: any) => [node.title, node]));
    const duplicateSection = byTitle.get(DUPLICATE_SECTION_TITLE);
    if (!duplicateSection) throw new Error("duplicate Ps section not found");
    const { tags, fields } = await ensureTagsAndFields(tx, schema);
    const now = new Date();

    for (const [index, technique] of TECHNIQUES.entries()) {
      const existing = technique.sourceTitle ? byTitle.get(technique.sourceTitle) : null;
      let node: any;
      if (existing) {
        node = await writer.updateDocsNode(tx, existing.id, {
          title: technique.title,
          description: "",
          bodyJson: { format: "doc_block", block_type: "paragraph" },
          displayProps: {},
          sortOrder: index + 1,
          archivedAt: null,
          updatedBy: OWNER_ID,
          updatedAt: now,
        });
      } else {
        node = await writer.insertDocsNode(tx, {
          id: stableUuid(`${MIGRATION_KEY}:node:${technique.key}`),
          workspaceId: WORKSPACE_ID,
          parentId: ROOT_ID,
          rootPageId: root.rootPageId,
          projectId: null,
          systemKey: `${MIGRATION_KEY}:node:${technique.key}`,
          title: technique.title,
          aliases: [],
          description: "",
          bodyJson: { format: "doc_block", block_type: "paragraph" },
          nodeType: "node",
          displayProps: {},
          queryJson: null,
          viewJson: {},
          dayDate: null,
          sortOrder: index + 1,
          createdBy: OWNER_ID,
          updatedBy: OWNER_ID,
        });
      }
      await tx.delete(schema.knowledgeNodeSupertags).where(eq(schema.knowledgeNodeSupertags.nodeId, node.id));
      await tx.insert(schema.knowledgeNodeSupertags).values({ nodeId: node.id, supertagId: tags.get(technique.tag)!.id, createdBy: OWNER_ID });
      await saveFieldValue(tx, schema, node.id, fields.get("URL"), technique.url);
      await saveFieldValue(tx, schema, node.id, fields.get("設定"), technique.settings);
      await saveFieldValue(tx, schema, node.id, fields.get("画像"), technique.images);
      await syncTechniqueAttachments(tx, schema, node.id, technique);
      await docsUtils.upsertKnowledgeSearchIndex(tx, node, technique.title);
      await docsUtils.appendKnowledgeRevision(tx, node, OWNER_ID, REVISION_SUMMARY, []);

      for (const [detailIndex, detail] of (technique.details ?? []).entries()) {
        const detailNode = await writer.insertDocsNode(tx, {
          id: stableUuid(`${MIGRATION_KEY}:node:${technique.key}:detail:${detailIndex + 1}`),
          workspaceId: WORKSPACE_ID,
          parentId: node.id,
          rootPageId: root.rootPageId,
          projectId: null,
          systemKey: `${MIGRATION_KEY}:node:${technique.key}:detail:${detailIndex + 1}`,
          title: detail,
          aliases: [],
          description: "",
          bodyJson: { format: "doc_block", block_type: "paragraph" },
          nodeType: "node",
          displayProps: {},
          queryJson: null,
          viewJson: {},
          dayDate: null,
          sortOrder: detailIndex + 1,
          createdBy: OWNER_ID,
          updatedBy: OWNER_ID,
        });
        await docsUtils.upsertKnowledgeSearchIndex(tx, detailNode, detail);
        await docsUtils.appendKnowledgeRevision(tx, detailNode, OWNER_ID, REVISION_SUMMARY, []);
      }
    }

    await writer.updateDocsNode(tx, duplicateSection.id, { archivedAt: now, updatedAt: now, updatedBy: OWNER_ID });
    const otherDuplicate = before.duplicate;
    if (!otherDuplicate) throw new Error("その他倉庫 duplicate not found");
    await writer.updateDocsNode(tx, otherDuplicate.id, { archivedAt: now, updatedAt: now, updatedBy: OWNER_ID });
    await tx.insert(schema.knowledgeNodePlacements).values({
      id: stableUuid(`${MIGRATION_KEY}:placement:other-warehouse`),
      nodeId: ROOT_ID,
      parentNodeId: OTHER_WAREHOUSE_ID,
      sortOrder: otherDuplicate.sortOrder,
      collapsed: true,
      createdBy: OWNER_ID,
    }).onConflictDoNothing();
    return { status: "applied", verification: await verify(tx) };
  });
}

async function verify(client?: any) {
  assertSources();
  const ctx = await context();
  const db = client ?? ctx.db;
  const { schema } = ctx;
  const state = await audit(db, schema);
  const expectedTitles = TECHNIQUES.map((item) => item.title);
  const actualTitles = state.direct.map((node: any) => node.title);
  const missingTitles = expectedTitles.filter((title) => !actualTitles.includes(title));
  const activeIds = state.direct.map((node: any) => node.id);
  const descriptions = state.direct.filter((node: any) => String(node.description ?? "").trim());
  const generatedDetails = state.generated.filter((node: any) => node.parentId !== ROOT_ID && !node.archivedAt);
  const expectedDetails = TECHNIQUES.reduce((count, item) => count + (item.details?.length ?? 0), 0);
  const [tagLinks, fieldValues, tags, fields, revisions] = await Promise.all([
    activeIds.length ? db.select().from(schema.knowledgeNodeSupertags).where(inArray(schema.knowledgeNodeSupertags.nodeId, activeIds)) : [],
    activeIds.length ? db.select().from(schema.knowledgeFieldValues).where(inArray(schema.knowledgeFieldValues.nodeId, activeIds)) : [],
    db.select().from(schema.knowledgeSupertags).where(eq(schema.knowledgeSupertags.workspaceId, WORKSPACE_ID)),
    db.select().from(schema.knowledgeFields).where(eq(schema.knowledgeFields.workspaceId, WORKSPACE_ID)),
    state.nodes.some((node: any) => !node.archivedAt) ? db.select().from(schema.knowledgeRevisions).where(and(
      inArray(schema.knowledgeRevisions.nodeId, state.nodes.filter((node: any) => !node.archivedAt).map((node: any) => node.id)),
      eq(schema.knowledgeRevisions.changeSummary, REVISION_SUMMARY),
    )) : [],
  ]);
  const blankValues = fieldValues.filter((value: any) => !String(value.valueText ?? "").trim());
  const expectedUrls = TECHNIQUES.flatMap((item) => item.url ? [item.url] : []);
  const actualValues = fieldValues.map((value: any) => String(value.valueText ?? ""));
  const missingUrls = expectedUrls.filter((url) => !actualValues.includes(url));
  const nodeByTitle = new Map(state.direct.map((node: any) => [node.title, node]));
  const tagNameById = new Map(tags.map((tag: any) => [tag.id, tag.name]));
  const fieldNameById = new Map(fields.map((field: any) => [field.id, field.name]));
  const tagMismatches: Array<{ title: string; expected: string; actual: string[] }> = [];
  const fieldMismatches: Array<{ title: string; expected: Record<string, string>; actual: Record<string, string> }> = [];
  const detailMismatches: Array<{ title: string; expected: string[]; actual: string[] }> = [];
  for (const technique of TECHNIQUES) {
    const node = nodeByTitle.get(technique.title);
    if (!node) continue;
    const actualTags = tagLinks
      .filter((link: any) => link.nodeId === node.id)
      .map((link: any) => String(tagNameById.get(link.supertagId) ?? link.supertagId))
      .sort();
    if (actualTags.length !== 1 || actualTags[0] !== technique.tag) {
      tagMismatches.push({ title: technique.title, expected: technique.tag, actual: actualTags });
    }
    const expectedFields = Object.fromEntries([
      ["URL", technique.url],
      ["設定", technique.settings],
      ["画像", technique.images],
    ].filter((entry): entry is [string, string] => Boolean(entry[1])));
    const actualFields = Object.fromEntries(fieldValues
      .filter((value: any) => value.nodeId === node.id)
      .map((value: any) => [String(fieldNameById.get(value.fieldId) ?? value.fieldId), String(value.valueText ?? "")]));
    if (JSON.stringify(actualFields) !== JSON.stringify(expectedFields)) {
      fieldMismatches.push({ title: technique.title, expected: expectedFields, actual: actualFields });
    }
    const expectedChildren = technique.details ?? [];
    const actualChildren = state.nodes
      .filter((child: any) => child.parentId === node.id && !child.archivedAt)
      .sort((a: any, b: any) => (a.sortOrder ?? 0) - (b.sortOrder ?? 0))
      .map((child: any) => child.title);
    if (JSON.stringify(actualChildren) !== JSON.stringify(expectedChildren)) {
      detailMismatches.push({ title: technique.title, expected: expectedChildren, actual: actualChildren });
    }
  }
  const orderMatches = JSON.stringify(actualTitles) === JSON.stringify(expectedTitles);
  const revisionsWithProvenance = revisions.filter((revision: any) => Array.isArray(revision.sourceRefsJson) && revision.sourceRefsJson.length > 0);
  const promptNode = state.nodes.find((node: any) => node.title === "②missing finger, extra finger fusion finger,を倍率1.4でネガティブプロンプトに突っ込む");
  const placement = state.placements.find((item: any) => item.nodeId === ROOT_ID && item.parentNodeId === OTHER_WAREHOUSE_ID);
  const report = {
    active_direct_count: state.direct.length,
    expected_direct_count: TECHNIQUES.length,
    generated_detail_count: generatedDetails.length,
    expected_detail_count: expectedDetails,
    tag_link_count: tagLinks.length,
    field_value_count: fieldValues.length,
    missing_titles: missingTitles,
    descriptions_present: descriptions.map((node: any) => node.id),
    blank_field_values: blankValues.map((value: any) => `${value.nodeId}:${value.fieldId}`),
    missing_urls: missingUrls,
    exact_order_preserved: orderMatches,
    tag_mismatches: tagMismatches,
    field_mismatches: fieldMismatches,
    detail_mismatches: detailMismatches,
    revision_count: revisions.length,
    expected_revision_count: TECHNIQUES.length + expectedDetails,
    revisions_with_provenance: revisionsWithProvenance.map((revision: any) => revision.id),
    exact_negative_prompt_preserved: Boolean(promptNode),
    duplicate_section_active: state.direct.some((node: any) => node.title === DUPLICATE_SECTION_TITLE),
    other_duplicate_archived: Boolean(state.duplicate?.archivedAt),
    other_warehouse_reference: Boolean(placement),
  };
  if (
    report.active_direct_count !== report.expected_direct_count ||
    report.generated_detail_count !== report.expected_detail_count ||
    report.tag_link_count !== TECHNIQUES.length ||
    report.missing_titles.length ||
    report.descriptions_present.length ||
    report.blank_field_values.length ||
    report.missing_urls.length ||
    !report.exact_order_preserved ||
    report.tag_mismatches.length ||
    report.field_mismatches.length ||
    report.detail_mismatches.length ||
    report.revision_count !== report.expected_revision_count ||
    report.revisions_with_provenance.length ||
    !report.exact_negative_prompt_preserved ||
    report.duplicate_section_active ||
    !report.other_duplicate_archived ||
    !report.other_warehouse_reference
  ) throw new Error(`pilot verification failed: ${JSON.stringify(report)}`);
  return report;
}

async function reconcile() {
  assertSources();
  const { db, schema, writer, docsUtils } = await context();
  return db.transaction(async (tx: any) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${WORKSPACE_ID}:${MIGRATION_KEY}`}))`);
    const state = await audit(tx, schema);
    if (state.generated.length === 0) throw new Error("pilot is not applied");
    const fieldRows = await tx.select().from(schema.knowledgeFields).where(and(
      eq(schema.knowledgeFields.workspaceId, WORKSPACE_ID),
      inArray(schema.knowledgeFields.name, ["URL", "設定", "画像"]),
    ));
    const fields = new Map<string, any>();
    for (const name of ["URL", "設定", "画像"]) {
      const matches = fieldRows.filter((field: any) => field.name === name);
      if (matches.length !== 1) throw new Error(`expected exactly one pilot field: ${name}`);
      fields.set(name, matches[0]);
    }
    const nodeByTitle = new Map(state.direct.map((node: any) => [node.title, node]));
    for (const technique of TECHNIQUES) {
      const node = nodeByTitle.get(technique.title);
      if (!node) throw new Error(`pilot node missing: ${technique.title}`);
      await saveFieldValue(tx, schema, node.id, fields.get("URL"), technique.url);
      await saveFieldValue(tx, schema, node.id, fields.get("設定"), technique.settings);
      await saveFieldValue(tx, schema, node.id, fields.get("画像"), technique.images);
      await syncTechniqueAttachments(tx, schema, node.id, technique);

      const desiredDetailIds = new Set<string>();
      for (const [detailIndex, detail] of (technique.details ?? []).entries()) {
        const detailId = stableUuid(`${MIGRATION_KEY}:node:${technique.key}:detail:${detailIndex + 1}`);
        desiredDetailIds.add(detailId);
        const existing = state.nodes.find((item: any) => item.id === detailId);
        if (!existing) throw new Error(`pilot detail missing: ${technique.key}:${detailIndex + 1}`);
        const updated = await writer.updateDocsNode(tx, detailId, {
          title: detail,
          sortOrder: detailIndex + 1,
          archivedAt: null,
          updatedAt: new Date(),
          updatedBy: OWNER_ID,
        });
        await docsUtils.upsertKnowledgeSearchIndex(tx, updated, detail);
      }
      for (const existing of state.nodes.filter((item: any) =>
        item.parentId === node.id
        && String(item.systemKey ?? "").startsWith(`${MIGRATION_KEY}:node:${technique.key}:detail:`)
        && !desiredDetailIds.has(item.id)
        && !item.archivedAt
      )) {
        await writer.updateDocsNode(tx, existing.id, {
          archivedAt: new Date(),
          updatedAt: new Date(),
          updatedBy: OWNER_ID,
        });
      }
    }
    return verify(tx);
  });
}

async function rollback(backupPath: string) {
  const hashes = assertSources();
  const { backup, checksum: backupChecksum } = readVerifiedBackup(backupPath);
  if (backup.hashes?.sourceHash !== hashes.sourceHash || backup.hashes?.otherHash !== hashes.otherHash) {
    throw new Error("backup source hash mismatch");
  }
  const receiptPath = resolve(arg("--receipt") ?? DEFAULT_RECEIPT_PATH);
  const receipt = readVerifiedReceipt(receiptPath);
  if (
    receipt.hashes?.sourceHash !== hashes.sourceHash
    || receipt.hashes?.otherHash !== hashes.otherHash
    || receipt.backup_sha256 !== backupChecksum
  ) throw new Error("applied-state receipt does not match sources/backup");
  const { db, schema, writer } = await context();
  return db.transaction(async (tx: any) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${WORKSPACE_ID}:${MIGRATION_KEY}`}))`);
    await verify(tx);
    const currentState = await audit(tx, schema);
    assertOtherDuplicateIdentity(currentState.duplicate, writer);
    const currentAppliedFingerprint = await appliedStateFingerprint(tx, schema, writer);
    if (currentAppliedFingerprint !== receipt.applied_fingerprint) {
      throw new Error("pilot DB changed after apply; refusing destructive rollback");
    }
    const backupIds = backup.nodes.map((node: any) => node.id);
    const [relevantTags, relevantFields] = await Promise.all([
      tx.select().from(schema.knowledgeSupertags).where(and(
        eq(schema.knowledgeSupertags.workspaceId, WORKSPACE_ID),
        inArray(schema.knowledgeSupertags.name, ["手法", "ツール", "資料"]),
      )),
      tx.select().from(schema.knowledgeFields).where(and(
        eq(schema.knowledgeFields.workspaceId, WORKSPACE_ID),
        inArray(schema.knowledgeFields.name, ["URL", "設定", "画像"]),
      )),
    ]);
    const relevantTagIds = relevantTags.map((tag: any) => tag.id);
    const relevantFieldIds = relevantFields.map((field: any) => field.id);
    const currentRelations = relevantTagIds.length && relevantFieldIds.length
      ? await tx.select().from(schema.knowledgeSupertagFields).where(and(
          inArray(schema.knowledgeSupertagFields.supertagId, relevantTagIds),
          inArray(schema.knowledgeSupertagFields.fieldId, relevantFieldIds),
        ))
      : [];
    if (!Array.isArray(backup.supertag_fields) && currentRelations.some((relation: any) => {
      const tag = relevantTags.find((item: any) => item.id === relation.supertagId);
      const field = relevantFields.find((item: any) => item.id === relation.fieldId);
      return !String(tag?.systemKey ?? "").startsWith(`${MIGRATION_KEY}:`)
        || !String(field?.systemKey ?? "").startsWith(`${MIGRATION_KEY}:`);
    })) {
      throw new Error("legacy backup cannot safely restore reused Supertag/Field relations");
    }
    for (const relation of currentRelations) {
      await tx.delete(schema.knowledgeSupertagFields).where(and(
        eq(schema.knowledgeSupertagFields.supertagId, relation.supertagId),
        eq(schema.knowledgeSupertagFields.fieldId, relation.fieldId),
      ));
    }
    if ((backup.supertag_fields ?? []).length) {
      await tx.insert(schema.knowledgeSupertagFields).values(backup.supertag_fields).onConflictDoNothing();
    }
    if (backupIds.length) {
      await tx.delete(schema.knowledgeRevisions).where(and(
        inArray(schema.knowledgeRevisions.nodeId, backupIds),
        eq(schema.knowledgeRevisions.changeSummary, REVISION_SUMMARY),
      ));
    }
    const generatedNodes = await tx.select({ id: schema.knowledgeNodes.id }).from(schema.knowledgeNodes).where(and(
      eq(schema.knowledgeNodes.workspaceId, WORKSPACE_ID),
      sql`${schema.knowledgeNodes.systemKey} like ${`${MIGRATION_KEY}:%`}`,
    ));
    if (generatedNodes.length) await tx.delete(schema.knowledgeNodes).where(inArray(schema.knowledgeNodes.id, generatedNodes.map((node: any) => node.id)));
    await tx.delete(schema.knowledgeNodePlacements).where(eq(schema.knowledgeNodePlacements.id, stableUuid(`${MIGRATION_KEY}:placement:other-warehouse`)));
    if (backupIds.length) {
      await tx.delete(schema.knowledgeNodePlacements).where(or(
        inArray(schema.knowledgeNodePlacements.nodeId, backupIds),
        inArray(schema.knowledgeNodePlacements.parentNodeId, backupIds),
      ));
    }
    if (backupIds.length) {
      await tx.delete(schema.knowledgeNodeSupertags).where(inArray(schema.knowledgeNodeSupertags.nodeId, backupIds));
      await tx.delete(schema.knowledgeFieldValues).where(inArray(schema.knowledgeFieldValues.nodeId, backupIds));
    }
    for (const node of backup.nodes) {
      await writer.updateDocsNode(tx, node.id, {
        parentId: node.parentId,
        rootPageId: node.rootPageId,
        projectId: node.projectId,
        systemKey: node.systemKey,
        title: node.title,
        aliases: node.aliases,
        description: node.description,
        bodyJson: node.bodyJsonDecrypted,
        nodeType: node.nodeType,
        displayProps: node.displayProps,
        queryJson: node.queryJson,
        viewJson: node.viewJson,
        dayDate: node.dayDate,
        sortOrder: node.sortOrder,
        archivedAt: node.archivedAt ? new Date(node.archivedAt) : null,
        updatedBy: OWNER_ID,
        updatedAt: node.updatedAt ? new Date(node.updatedAt) : new Date(),
      });
    }
    if (backupIds.length) await tx.delete(schema.knowledgeSearchIndex).where(inArray(schema.knowledgeSearchIndex.nodeId, backupIds));
    if (backup.search_rows.length) await tx.insert(schema.knowledgeSearchIndex).values(backup.search_rows).onConflictDoNothing();
    if (backup.node_supertags.length) await tx.insert(schema.knowledgeNodeSupertags).values(backup.node_supertags).onConflictDoNothing();
    if (backup.field_values.length) await tx.insert(schema.knowledgeFieldValues).values(backup.field_values).onConflictDoNothing();
    if (backup.placements.length) await tx.insert(schema.knowledgeNodePlacements).values(backup.placements).onConflictDoNothing();
    await tx.delete(schema.knowledgeFields).where(and(eq(schema.knowledgeFields.workspaceId, WORKSPACE_ID), sql`${schema.knowledgeFields.systemKey} like ${`${MIGRATION_KEY}:%`}`));
    await tx.delete(schema.knowledgeSupertags).where(and(eq(schema.knowledgeSupertags.workspaceId, WORKSPACE_ID), sql`${schema.knowledgeSupertags.systemKey} like ${`${MIGRATION_KEY}:%`}`));
    const restoredState = await audit(tx, schema);
    const expectedFingerprint = backup.protected_fingerprint ?? protectedFingerprint(backup.nodes);
    const restoredFingerprint = protectedFingerprint(restoredState.nodes.map((node: any) => backupNode(node, writer)));
    if (restoredFingerprint !== expectedFingerprint) throw new Error("rollback node fingerprint mismatch");
    const restoredPrestate = await capturePrestate(tx, schema, writer, restoredState);
    const expectedPrestateFingerprint = backup.prestate_fingerprint ?? prestateFingerprint(backup);
    const restoredPrestateFingerprint = prestateFingerprint(restoredPrestate);
    if (restoredPrestateFingerprint !== expectedPrestateFingerprint) throw new Error("rollback full pre-state fingerprint mismatch");
    return { status: "rolled_back", restored_nodes: backup.nodes.length, protected_fingerprint: restoredFingerprint, prestate_fingerprint: restoredPrestateFingerprint };
  });
}

async function main() {
  const command = process.argv[2] ?? "preview";
  if (command === "preview") {
    const hashes = assertSources();
    const { db, schema } = await context();
    const state = await audit(db, schema);
    assertLegacyShape(state);
    console.log(JSON.stringify({ hashes, plan: { techniques: TECHNIQUES.length, exact_detail_nodes: TECHNIQUES.reduce((n, item) => n + (item.details?.length ?? 0), 0), source_sections_reused: TECHNIQUES.filter((item) => item.sourceTitle).length, inserted_direct_nodes: TECHNIQUES.filter((item) => !item.sourceTitle).length, duplicate_sections_archived: 2, other_warehouse_reference: true }, current: state.summary }, null, 2));
    return;
  }
  if (command === "backup") {
    console.log(JSON.stringify(await createBackup(), null, 2));
    return;
  }
  const backupPath = arg("--backup");
  if (command === "apply") {
    if (!backupPath) throw new Error("--backup is required");
    console.log(JSON.stringify(await apply(resolve(backupPath)), null, 2));
    return;
  }
  if (command === "verify") {
    console.log(JSON.stringify(await verify(), null, 2));
    return;
  }
  if (command === "seal") {
    if (!backupPath) throw new Error("--backup is required");
    console.log(JSON.stringify(await sealAppliedState(resolve(backupPath)), null, 2));
    return;
  }
  if (command === "reconcile") {
    console.log(JSON.stringify(await reconcile(), null, 2));
    return;
  }
  if (command === "rollback") {
    if (!backupPath) throw new Error("--backup is required");
    console.log(JSON.stringify(await rollback(resolve(backupPath)), null, 2));
    return;
  }
  throw new Error(`unknown command: ${command}`);
}

main().then(
  () => process.exit(0),
  (error) => {
    console.error(error instanceof Error ? error.stack ?? error.message : error);
    process.exit(1);
  },
);
