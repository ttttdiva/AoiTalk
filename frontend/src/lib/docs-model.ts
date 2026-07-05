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
};

function scaffold(blocks: Array<{ type: string; text: string }>) {
  return { format: "doc_block_template", blocks };
}

export const DEFAULT_DOCS_SUPERTAGS: DefaultDocsSupertag[] = [
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
    name: "Decision",
    baseType: "decision",
    description: "判断と根拠の記録",
    icon: "gavel",
    color: "#f59e0b",
    pinnedFieldKeys: ["決定日"],
    fields: [
      { name: "決定日", fieldType: "date", options: { default: "today" } },
      { name: "根拠", fieldType: "text" },
      { name: "案件", fieldType: "reference", options: { default: "ancestor_project" } },
    ],
    templateJson: scaffold([{ type: "callout", text: "決定: " }]),
    aiInstructions: "決定事項として、決定文・決定日・根拠を必ず残す。未確定のものはDecisionにせずQuestionまたはRiskへ回す。",
  },
  {
    name: "Risk",
    baseType: "risk",
    description: "懸念、制約、互換性リスク",
    icon: "alert-triangle",
    color: "#ef4444",
    pinnedFieldKeys: ["深刻度", "状態"],
    fields: [
      { name: "深刻度", fieldType: "options", options: { values: ["low", "mid", "high"] }, defaultValue: "mid" },
      { name: "状態", fieldType: "options", options: { values: ["open", "watch", "closed"] }, defaultValue: "open" },
      { name: "対策", fieldType: "text" },
      { name: "案件", fieldType: "reference", options: { default: "ancestor_project" } },
    ],
    templateJson: scaffold([{ type: "callout", text: "リスク: " }, { type: "paragraph", text: "対策: " }]),
    aiInstructions: "リスクは影響・状態・対策を分けて記録する。根拠が弱いものは状態をwatchにし、断定しない。",
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
    name: "Vendor",
    baseType: "vendor",
    description: "取引先や問い合わせ先",
    icon: "building-2",
    color: "#64748b",
    pinnedFieldKeys: ["窓口"],
    fields: [
      { name: "窓口", fieldType: "text" },
      { name: "連絡先", fieldType: "text" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "取引先メモ: " }]),
    aiInstructions: "ベンダー情報は窓口、連絡先、関係する案件を明確にする。実在確認がない固有名詞は作らない。",
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
    name: "Question",
    baseType: "question",
    description: "未回答の確認事項",
    icon: "circle-help",
    color: "#8b5cf6",
    pinnedFieldKeys: ["状態"],
    fields: [
      { name: "状態", fieldType: "options", options: { values: ["open", "answered"] }, defaultValue: "open" },
      { name: "回答", fieldType: "text" },
      { name: "案件", fieldType: "reference", options: { default: "ancestor_project" } },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "確認したいこと: " }]),
    aiInstructions: "未確定事項はQuestionとして残し、回答が得られたら回答fieldと根拠を更新する。",
  },
  {
    name: "案件情報",
    systemKey: "project_info",
    baseType: "project_information",
    description: "案件概要、進捗、課題管理、決定事項、参照、Q&Aをまとめる正本ページ",
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
      { type: "heading_2", text: "進捗" },
      { type: "heading_2", text: "課題管理" },
      { type: "heading_2", text: "決定事項" },
      { type: "heading_2", text: "タスク" },
      { type: "heading_2", text: "Q&A" },
    ]),
    aiInstructions: "このページを案件情報の正本として扱う。本文更新では既存見出しを尊重し、根拠とrevisionを必ず残す。",
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
    name: "Note",
    baseType: "note",
    description: "自由記述メモ",
    icon: "notebook-text",
    color: "#78716c",
    pinnedFieldKeys: ["案件"],
    fields: [{ name: "案件", fieldType: "reference", options: { default: "ancestor_project" } }],
    templateJson: scaffold([{ type: "paragraph", text: "" }]),
    aiInstructions: "自由メモとして扱い、型が明確になったらより具体的なタグへ移す。",
  },
  {
    name: "Spec",
    baseType: "spec",
    description: "仕様、要件、制約条件",
    icon: "file-text",
    color: "#2563eb",
    pinnedFieldKeys: ["版"],
    fields: [
      { name: "版", fieldType: "text" },
      { name: "参照元", fieldType: "url" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "仕様: " }]),
    aiInstructions: "仕様は版、参照元、適用範囲を区別する。変更時は影響するDecisionやTaskを確認する。",
  },
  {
    name: "Estimate",
    baseType: "estimate",
    description: "見積と費用情報",
    icon: "receipt",
    color: "#16a34a",
    pinnedFieldKeys: ["金額"],
    fields: [
      { name: "金額", fieldType: "number" },
      { name: "取引先", fieldType: "reference" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "見積条件: " }]),
    aiInstructions: "見積は金額、前提条件、取引先、根拠資料を分けて扱う。",
  },
  {
    name: "Evidence",
    baseType: "evidence",
    description: "メール、仕様、見積、URLなどの証跡",
    icon: "file-check",
    color: "#14b8a6",
    pinnedFieldKeys: ["出典"],
    fields: [
      { name: "出典", fieldType: "url" },
      { name: "案件", fieldType: "reference", options: { default: "ancestor_project" } },
    ],
    templateJson: scaffold([{ type: "quote", text: "根拠: " }]),
    aiInstructions: "Evidenceは他の判断の根拠として使う。本文には出典と要約を分けて残す。",
  },
  {
    name: "Email",
    baseType: "email",
    description: "メール内容や回答",
    icon: "mail",
    color: "#475569",
    pinnedFieldKeys: ["差出人"],
    fields: [
      { name: "差出人", fieldType: "email" },
      { name: "受信日時", fieldType: "date" },
    ],
    templateJson: scaffold([{ type: "quote", text: "要旨: " }]),
    aiInstructions: "メールは差出人、日時、要旨、関連するDecisionやTaskを分けて扱う。",
  },
  {
    name: "URL",
    baseType: "url",
    description: "参照URL",
    icon: "link",
    color: "#0284c7",
    pinnedFieldKeys: ["URL"],
    fields: [
      { name: "URL", fieldType: "url", required: true },
      { name: "要約", fieldType: "text" },
    ],
    templateJson: scaffold([{ type: "paragraph", text: "参照メモ: " }]),
    aiInstructions: "URLはリンク先の要約と利用目的を短く残す。リンク切れに備えて重要点を本文にも残す。",
  },
  {
    name: "Project",
    baseType: "project",
    description: "案件や作業単位の正本ページ",
    icon: "folder-kanban",
    color: "#0ea5e9",
    pinnedFieldKeys: ["状態"],
    fields: [
      { name: "状態", fieldType: "options", options: { values: ["open", "active", "done"] }, defaultValue: "active" },
      { name: "責任者", fieldType: "user" },
    ],
    templateJson: scaffold([{ type: "heading_1", text: "案件概要" }]),
    aiInstructions: "Projectは案件全体の入口として扱う。詳細な正本は案件情報ページへ集約する。",
  },
];

export function normalizeDocsNodeType(value: unknown): DocsNodeType {
  if (value === "page" || value === "block" || value === "object") return "node";
  return DOCS_NODE_TYPES.includes(value as DocsNodeType)
    ? (value as DocsNodeType)
    : "node";
}

export function normalizeDocsBaseType(value: unknown): DocsBaseType {
  return DOCS_BASE_TYPES.includes(value as DocsBaseType)
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
