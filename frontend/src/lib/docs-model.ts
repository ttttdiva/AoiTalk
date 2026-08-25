export const DOCS_NAV_LABEL = "Docs";
export const DOCS_ROUTE = "/docs";

export const DOCS_NODE_TYPES = ["node", "search", "day", "system"] as const;
export type DocsNodeType = (typeof DOCS_NODE_TYPES)[number];

export const DOCS_BASE_TYPES = [
  "note",
  "task",
  "decision",
  "risk",
  "question",
  "meeting",
  "person",
  "vendor",
  "device",
  "spec",
  "estimate",
  "evidence",
  "email",
  "url",
  "project",
  "project_information",
  "day",
  "record",
] as const;

export type DocsBaseType = (typeof DOCS_BASE_TYPES)[number];

export const DOCS_FIELD_TYPES = [
  "text",
  "long_text",
  "options",
  "options_from_supertag",
  "number",
  "date",
  "checkbox",
  "url",
  "email",
  "user",
  "reference",
] as const;
export type DocsFieldType = (typeof DOCS_FIELD_TYPES)[number];

export type DefaultDocsField = {
  name: string;
  systemKey?: string;
  fieldType: DocsFieldType;
  required?: boolean;
  options?: Record<string, unknown>;
  defaultValue?: unknown;
};

export type DefaultDocsSupertag = {
  name: string;
  systemKey?: string;
  baseType: DocsBaseType;
  parentName?: string;
  description: string;
  icon: string;
  color: string;
  fields: DefaultDocsField[];
  pinnedFieldKeys: string[];
  templateJson: Record<string, unknown>;
  aiInstructions: string;
  configJson?: Record<string, unknown>;
};

function scaffold(blocks: Array<{ type: string; text: string }>) {
  return { format: "doc_block_template", blocks };
}

export const DEFAULT_DOCS_SUPERTAGS: DefaultDocsSupertag[] = [
  {
    name: "案件情報",
    systemKey: "project_info",
    baseType: "project_information",
    description: "案件概要、体制、決定事項、課題、タスク、参照をまとめる正本ページ",
    icon: "book-open",
    color: "#2563eb",
    pinnedFieldKeys: ["Project", "Page Role", "Progress Digest"],
    fields: [
      { name: "Project", fieldType: "reference", required: true, options: { default: "ancestor_project" } },
      { name: "Page Role", fieldType: "options", options: { values: ["canonical", "child", "archive"] }, defaultValue: "canonical" },
      { name: "Agent Update Policy", fieldType: "long_text", defaultValue: { require_source_refs: true, preserve_headings: true } },
      { name: "Q&A Enabled", fieldType: "checkbox", defaultValue: true },
      { name: "Source Scope", fieldType: "long_text", defaultValue: { conversations: "project", docs: "project", records: "project" } },
      { name: "Progress Digest", fieldType: "text" },
    ],
    templateJson: scaffold([
      { type: "heading_1", text: "概要" },
      { type: "heading_2", text: "体制" },
      { type: "heading_2", text: "決定事項" },
      { type: "heading_2", text: "課題" },
      { type: "heading_2", text: "タスク" },
      { type: "heading_2", text: "参照" },
    ]),
    aiInstructions: "このページを案件情報の正本として扱う。本文更新では既存見出しを尊重し、根拠とrevisionを必ず残す。",
  },
  {
    name: "Task",
    systemKey: "task",
    baseType: "task",
    description: "次アクションや作業項目",
    icon: "check-square",
    color: "#22c55e",
    pinnedFieldKeys: ["状態", "期日"],
    fields: [
      { name: "状態", systemKey: "task_status", fieldType: "options", options: { values: ["todo", "doing", "done"] }, defaultValue: "todo" },
      { name: "開始", systemKey: "task_start", fieldType: "date" },
      { name: "期日", systemKey: "task_due", fieldType: "date", options: { default: "today" } },
      { name: "優先度", systemKey: "task_priority", fieldType: "options", options: { values: ["low", "normal", "high", "urgent"] }, defaultValue: "normal" },
      { name: "担当", fieldType: "text" },
      { name: "案件", systemKey: "task_project", fieldType: "reference", options: { default: "ancestor_project" } },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "完了条件: " }]),
    aiInstructions: "作業項目として、状態・期日・担当・案件を明示して更新する。完了時は根拠になる会話やDocs参照を残す。",
  },
  {
    name: "メール",
    systemKey: "email",
    baseType: "email",
    description: "プロジェクトへ取り込んだメールの恒久記録",
    icon: "mail",
    color: "#0284c7",
    pinnedFieldKeys: ["メール日時", "From", "Message-ID"],
    fields: [
      { name: "件名", systemKey: "email_subject", fieldType: "text" },
      { name: "メール日時", systemKey: "email_date", fieldType: "text" },
      { name: "From", systemKey: "email_from", fieldType: "text" },
      { name: "To", systemKey: "email_to", fieldType: "long_text" },
      { name: "CC", systemKey: "email_cc", fieldType: "long_text" },
      { name: "BCC", systemKey: "email_bcc", fieldType: "long_text" },
      { name: "Message-ID", systemKey: "email_message_id", fieldType: "text" },
      { name: "In-Reply-To", systemKey: "email_in_reply_to", fieldType: "text" },
      { name: "References", systemKey: "email_references", fieldType: "long_text" },
      { name: "本文", systemKey: "email_body", fieldType: "long_text" },
      { name: "元ファイル名", systemKey: "email_source_filename", fieldType: "text" },
      { name: "元ファイルのプロジェクト内パス", systemKey: "email_source_path", fieldType: "text" },
      { name: "重複判定キー", systemKey: "email_dedupe_key", fieldType: "text" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "本文" }]),
    aiInstructions: "1メール=1ノード。同じプロジェクトの過去メールに関する質問では、ヘッダーと本文を根拠として使う。",
  },
  {
    name: "メールメッセージ",
    systemKey: "email_message",
    baseType: "email",
    description: "メール原本の引用チェーンから復元した、個別に参照できる1通",
    icon: "mail-open",
    color: "#0ea5e9",
    pinnedFieldKeys: ["送信日時", "From"],
    fields: [
      { name: "件名", systemKey: "email_message_subject", fieldType: "text" },
      { name: "送信日時", systemKey: "email_message_date", fieldType: "text" },
      { name: "From", systemKey: "email_message_from", fieldType: "text" },
      { name: "To", systemKey: "email_message_to", fieldType: "long_text" },
      { name: "CC", systemKey: "email_message_cc", fieldType: "long_text" },
      { name: "本文", systemKey: "email_message_body", fieldType: "long_text" },
      { name: "原本", systemKey: "email_message_source", fieldType: "reference" },
      { name: "復元キー", systemKey: "email_message_source_key", fieldType: "text" },
    ],
    templateJson: scaffold([]),
    aiInstructions: "メールの経緯を裏付ける個別メッセージ。要約の事実は該当メッセージへ直接リンクする。",
  },
  {
    name: "Inbox項目",
    systemKey: "work_intake",
    baseType: "record",
    description: "/inboxで受け付けた問い合わせ・依頼・情報共有の管理単位",
    icon: "inbox",
    color: "#6366f1",
    pinnedFieldKeys: ["Inbox ID", "対応状態", "受付日時"],
    fields: [
      { name: "Inbox ID", systemKey: "inbox_item_id", fieldType: "text" },
      { name: "分類", systemKey: "inbox_classification", fieldType: "options", options: { values: ["質問", "依頼", "情報共有"] } },
      { name: "対応状態", systemKey: "inbox_status", fieldType: "options", options: { values: ["受付", "対応中", "確認待ち", "レビュー待ち", "完了", "保存のみ"] }, defaultValue: "受付" },
      { name: "受付元", systemKey: "inbox_source_type", fieldType: "options", options: { values: ["チャット", "メール", "複合"] } },
      { name: "受付日時", systemKey: "inbox_received_at", fieldType: "date" },
      { name: "最終更新", systemKey: "inbox_last_updated_at", fieldType: "date" },
      { name: "受付内容", systemKey: "inbox_instruction", fieldType: "long_text" },
      { name: "取りまとめ", systemKey: "inbox_summary", fieldType: "long_text" },
    ],
    templateJson: scaffold([]),
    aiInstructions: "1回の/inbox受付を1つのInbox項目として扱う。概要を最優先し、内容に必要な章だけを作る。複数回の応酬は経緯を意味的に要約し、各事実の直下へ根拠をリンクする。確認事項・次の対応・参考資料・更新履歴を固定で作らない。追加情報は同じUUIDの文書全体へ統合し、新しい項目を推測で作らない。",
  },
  {
    name: "Day",
    systemKey: "day",
    baseType: "day",
    description: "日付単位のキャプチャと日次メモ",
    icon: "calendar",
    color: "#0ea5e9",
    pinnedFieldKeys: ["日付"],
    fields: [
      { name: "日付", fieldType: "date", options: { default: "today" } },
    ],
    templateJson: scaffold([{ type: "heading_2", text: "Capture" }, { type: "heading_2", text: "Notes" }]),
    aiInstructions: "日次ページは一時キャプチャの入口として扱い、案件に属する情報は適切な案件ページへ移す。",
  },
  {
    name: "Meeting",
    systemKey: "meeting",
    baseType: "meeting",
    description: "会議メモと議事",
    icon: "calendar-days",
    color: "#a855f7",
    pinnedFieldKeys: ["日時", "出席者"],
    fields: [
      { name: "日時", systemKey: "meeting_date", fieldType: "date", options: { default: "today" } },
      { name: "出席者", fieldType: "text" },
      { name: "案件", fieldType: "reference", options: { default: "ancestor_project" } },
    ],
    templateJson: scaffold([{ type: "heading_2", text: "議題" }, { type: "heading_2", text: "決定事項" }, { type: "heading_2", text: "宿題" }]),
    aiInstructions: "会議メモは議題、決定、宿題、未確認事項を分ける。後続のDecisionやTaskへつながる根拠として扱う。",
  },
  {
    name: "Person",
    systemKey: "person",
    baseType: "person",
    description: "関係者",
    icon: "user",
    color: "#ec4899",
    pinnedFieldKeys: ["所属"],
    fields: [
      { name: "所属", fieldType: "text" },
      { name: "連絡先", fieldType: "text" },
      { name: "役割", fieldType: "text" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "担当領域: " }]),
    aiInstructions: "人物情報は所属・連絡先・役割を区別して扱う。推測の個人情報は書かない。",
  },
  {
    name: "Device",
    baseType: "device",
    description: "機材、製品、構成要素",
    icon: "cpu",
    color: "#06b6d4",
    pinnedFieldKeys: ["型番"],
    fields: [
      { name: "型番", fieldType: "text" },
      { name: "数量", fieldType: "number" },
      { name: "用途", fieldType: "text" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "構成上の役割: " }]),
    aiInstructions: "機器は型番、数量、用途、関連仕様を分けて記録する。曖昧な数量は要確認へ回す。",
  },
  {
    name: "Evidence",
    baseType: "evidence",
    description: "候補や判断の根拠となる証跡（対象候補・出典・信頼度で管理）",
    icon: "file-check",
    color: "#14b8a6",
    pinnedFieldKeys: ["対象候補", "信頼度"],
    fields: [
      { name: "対象候補", fieldType: "reference" },
      { name: "出典", fieldType: "text" },
      { name: "信頼度", fieldType: "options", options: { values: ["低", "中", "高"] }, defaultValue: "中" },
      { name: "案件", fieldType: "reference", options: { default: "ancestor_project" } },
    ],
    templateJson: scaffold([{ type: "quote", text: "根拠: " }]),
    aiInstructions: "Evidenceは候補や判断の根拠として使う。対象候補・出典・信頼度を分けて記録し、本文には出典と要約を残す。",
  },
  {
    name: "Candidate",
    systemKey: "candidate",
    baseType: "note",
    description: "製品候補。製品・台数・価格・状態を保持し、Evidenceで根拠を裏付ける",
    icon: "package",
    color: "#f97316",
    pinnedFieldKeys: ["状態", "本体価格"],
    fields: [
      { name: "製品", fieldType: "reference" },
      { name: "台数", fieldType: "number" },
      { name: "本体価格", fieldType: "number" },
      { name: "ケーブル価格", fieldType: "number" },
      { name: "状態", fieldType: "options", options: { values: ["第一候補", "代替案", "不成立", "採用"] }, defaultValue: "第一候補" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "選定理由: " }]),
    aiInstructions: "製品候補として、製品・台数・本体価格・ケーブル価格・状態を分けて記録する。根拠はEvidenceで裏付け、状態は採用/不成立まで更新する。",
  },
];

export function normalizeDocsNodeType(value: unknown): DocsNodeType {
  if (value === "page" || value === "block" || value === "object") return "node";
  return DOCS_NODE_TYPES.includes(value as DocsNodeType)
    ? (value as DocsNodeType)
    : "node";
}

export function normalizeDocsBaseType(value: unknown): DocsBaseType {
  return DOCS_BASE_TYPES.includes(value as (typeof DOCS_BASE_TYPES)[number])
    ? (value as DocsBaseType)
    : "note";
}

export function normalizeDocsFieldType(value: unknown): DocsFieldType {
  if (value === "select" || value === "multi_select") return "options";
  if (value === "node_ref" || value === "multi_node_ref" || value === "project_ref" || value === "task_ref") {
    return "reference";
  }
  if (value === "datetime") return "date";
  if (value === "json") return "long_text";
  return DOCS_FIELD_TYPES.includes(value as DocsFieldType)
    ? (value as DocsFieldType)
    : "text";
}
