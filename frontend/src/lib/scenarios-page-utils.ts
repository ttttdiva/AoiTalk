// scenarios ページから抽出した型・定数・API ヘルパー・純粋ユーティリティ

// ─── Types ───

type Scenario = {
  id: string;
  title: string;
  scenario_kind?: "writing" | "trpg";
  ruleset?: string;
  description: string;
  genre: string;
  perspective: "first_person" | "third_person";
  setting: string;
  opening_text: string;
  tags: string[];
  difficulty: string;
  created_at: string;
  updated_at: string;
};

function scenarioDefaultImage(scenario: Pick<Scenario, "scenario_kind">) {
  return scenario.scenario_kind === "trpg"
    ? "/images/ui/scenario-trpg.png"
    : "/images/ui/scenario-writing.png";
}

type ScenarioCharacter = {
  id: string;
  scenario_id: string;
  character_id?: string | null;
  name: string;
  role: "protagonist" | "antagonist" | "npc" | "companion";
  description: string;
  importance?: number;
  speech_pattern?: string;
  psychology?: string;
  backstory?: string;
  relationships?: string;
  arc?: string;
  dialogue_samples?: string;
  trpg_ruleset?: string;
  trpg_pc_state?: CocPcState;
};

type CocPcState = {
  sheet_format?: string;
  ruleset?: string;
  name?: string;
  player_name?: string;
  occupation?: string;
  age?: string;
  sex?: string;
  hp?: number;
  max_hp?: number;
  mp?: number;
  max_mp?: number;
  sanity?: number;
  max_sanity?: number;
  luck?: number;
  idea?: number;
  knowledge?: number;
  characteristics?: Record<string, number>;
  stats?: Record<string, number>;
  skills?: Record<string, number>;
  weapons?: Array<Record<string, unknown>>;
  armor?: string;
  conditions?: string[];
  items?: string[];
  notes?: string;
  personal?: Record<string, string>;
};

type ScenarioScene = {
  id: string;
  scenario_id: string;
  title: string;
  description: string;
  scene_type:
    | "intro"
    | "exploration"
    | "combat"
    | "dialogue"
    | "choice"
    | "ending";
  gm_instructions: string;
  image_prompt: string;
  order_index: number;
  episode_id?: string | null;
  status?: "draft" | "in_progress" | "completed";
  body?: string;
};

type ScenarioEpisode = {
  id: string;
  scenario_id: string;
  title: string;
  one_line_summary: string;
  paragraph_summary: string;
  full_summary: string;
  status: "draft" | "in_progress" | "completed";
  beat_sheet: string;
  sort_order: number;
};

type CanonEntry = {
  id: string;
  scenario_id: string;
  category: string;
  fact: string;
};

type LoreBookEntry = {
  id: string;
  name: string;
  keywords: string[];
  content: string;
  priority: number;
  is_enabled: boolean;
  case_sensitive: boolean;
  constant: boolean;
};

type LoreBook = {
  id: string;
  scenario_id?: string | null;
  name: string;
  description: string;
  is_enabled: boolean;
  entries?: LoreBookEntry[];
};

type TRPGScenarioDocument = {
  id: string;
  scenario_id: string;
  ruleset: string;
  source_label: string;
  source_text: string;
  structure: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

type TRPGStructureNode = {
  id: string;
  type: string;
  title: string;
  summary?: string;
  body?: string;
  tags?: string[];
  metadata?: Record<string, unknown>;
};

type TRPGStructureLink = {
  from: string;
  to: string;
  relation: string;
  condition?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

type TRPGStructure = Record<string, unknown> & {
  version: number;
  nodes: TRPGStructureNode[];
  links: TRPGStructureLink[];
  metadata: Record<string, unknown>;
};

type ScenarioDetail = Scenario & {
  characters: ScenarioCharacter[];
  scenes: ScenarioScene[];
  episodes?: ScenarioEpisode[];
  trpg_documents?: TRPGScenarioDocument[];
};

type ScenarioPayload<T extends Scenario> = T | { success?: boolean; scenario?: T };

// ─── API Helper ───

async function pyFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

function unwrapScenario<T extends Scenario>(data: ScenarioPayload<T>): T {
  if (
    data &&
    typeof data === "object" &&
    "scenario" in data &&
    data.scenario
  ) {
    return data.scenario;
  }
  return data as T;
}

const TRPG_STRUCTURE_NODE_TYPES = [
  { value: "location", label: "場所" },
  { value: "scene", label: "場面" },
  { value: "npc", label: "NPC" },
  { value: "item", label: "アイテム" },
  { value: "clue", label: "手掛かり" },
  { value: "hazard", label: "脅威" },
  { value: "ending", label: "結末" },
  { value: "rule", label: "ルール" },
  { value: "handout", label: "ハンドアウト" },
  { value: "custom", label: "その他" },
];

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function normalizeStructureForEditor(value: unknown): TRPGStructure {
  const source = asRecord(value);
  const nodes = Array.isArray(source.nodes)
    ? source.nodes.flatMap((node, index) => {
        if (!node || typeof node !== "object" || Array.isArray(node)) return [];
        const raw = asRecord(node);
        const title = String(
          raw.title ?? raw.name ?? raw.id ?? `Node ${index + 1}`,
        ).trim();
        const id = String(
          raw.id ?? raw.key ?? (title || `node-${index + 1}`),
        ).trim();
        const tags = Array.isArray(raw.tags)
          ? raw.tags.map((tag) => String(tag).trim()).filter(Boolean)
          : String(raw.tags ?? "")
              .split(",")
              .map((tag) => tag.trim())
              .filter(Boolean);
        return [
          {
            id,
            type: String(raw.type ?? "custom").trim().toLowerCase() || "custom",
            title,
            summary: String(raw.summary ?? "").trim(),
            body: String(raw.body ?? ""),
            tags,
            metadata: asRecord(raw.metadata),
          },
        ];
      })
    : [];
  const links = Array.isArray(source.links)
    ? source.links.flatMap((link) => {
        const raw = asRecord(link);
        const from = String(raw.from ?? raw.source ?? "").trim();
        const to = String(raw.to ?? raw.target ?? "").trim();
        if (!from || !to) return [];
        return [
          {
            from,
            to,
            relation: String(raw.relation ?? "related").trim().toLowerCase() || "related",
            condition: asRecord(raw.condition),
            metadata: asRecord(raw.metadata),
          },
        ];
      })
    : [];
  const version = Number(source.version ?? 1);
  return {
    ...source,
    version: Number.isFinite(version) ? version : 1,
    nodes,
    links,
    metadata: asRecord(source.metadata),
  };
}

function parseStructureText(text: string): TRPGStructure {
  if (!text.trim()) return normalizeStructureForEditor({});
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("structure must be an object");
  }
  return normalizeStructureForEditor(parsed);
}

function makeStructureNodeId(type: string, title: string, nodes: TRPGStructureNode[]) {
  const slug = title
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9_-]/g, "");
  const base = slug || `${type || "node"}-${nodes.length + 1}`;
  const used = new Set(nodes.map((node) => node.id));
  if (!used.has(base)) return base;
  let index = 2;
  while (used.has(`${base}-${index}`)) index += 1;
  return `${base}-${index}`;
}

// ─── Select class ───

const selectClassName =
  "h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30";

// ─── Genre labels ───

const GENRES = [
  "fantasy",
  "sci-fi",
  "horror",
  "mystery",
  "romance",
  "adventure",
  "comedy",
  "drama",
  "historical",
  "other",
];

const SCENARIO_KINDS = [
  { value: "writing", label: "通常シナリオ" },
  { value: "trpg", label: "TRPGシナリオ" },
] as const;
const TRPG_RULESETS = [
  { value: "generic", label: "汎用TRPG" },
  { value: "coc6", label: "クトゥルフ神話TRPG 6版" },
  { value: "coc7", label: "クトゥルフ神話TRPG 7版" },
  { value: "shinobigami", label: "シノビガミ" },
  { value: "swordworld2_5", label: "ソード・ワールド2.5" },
] as const;
const COC_RULESETS = new Set(["coc6", "coc7"]);
const ROLES = ["protagonist", "antagonist", "npc", "companion"] as const;
const SCENE_TYPES = [
  "intro",
  "exploration",
  "combat",
  "dialogue",
  "choice",
  "ending",
] as const;
const DIFFICULTIES = ["easy", "normal", "hard", "very_hard"] as const;
const IMPORTANCES = [
  { value: 0, label: "主要" },
  { value: 1, label: "サブ" },
  { value: 2, label: "脇役" },
] as const;
const STATUSES = ["draft", "in_progress", "completed"] as const;
const STATUS_LABELS: Record<string, string> = {
  draft: "下書き",
  in_progress: "執筆中",
  completed: "完了",
};
const STATUS_COLORS: Record<string, string> = {
  draft: "secondary",
  in_progress: "default",
  completed: "outline",
};
const CANON_CATEGORIES = [
  "geography",
  "timeline",
  "magic",
  "character_facts",
  "political",
  "cultural",
  "established",
] as const;
const CANON_CATEGORY_LABELS: Record<string, string> = {
  geography: "地理",
  timeline: "時系列",
  magic: "魔法",
  character_facts: "キャラ情報",
  political: "政治",
  cultural: "文化",
  established: "確定事項",
};

const COC_CHARACTERISTICS = ["STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU"] as const;
const COC6_DEFAULT_CHARACTERISTICS: Record<string, number> = {
  STR: 10,
  CON: 11,
  POW: 12,
  DEX: 12,
  APP: 10,
  SIZ: 11,
  INT: 14,
  EDU: 14,
};
const COC_SKILL_CATEGORIES: Record<string, string[]> = {
  戦闘技能: [
    "回避",
    "キック",
    "組み付き",
    "こぶし（パンチ）",
    "頭突き",
    "投擲",
    "マーシャルアーツ",
    "拳銃",
    "サブマシンガン",
    "ショットガン",
    "マシンガン",
    "ライフル",
  ],
  探索技能: [
    "応急手当",
    "鍵開け",
    "隠す",
    "隠れる",
    "聞き耳",
    "忍び歩き",
    "写真術",
    "精神分析",
    "追跡",
    "登攀",
    "図書館",
    "目星",
  ],
  行動技能: [
    "運転（自動車）",
    "機械修理",
    "重機械操作",
    "乗馬",
    "水泳",
    "製作",
    "操縦",
    "跳躍",
    "電気修理",
    "ナビゲート",
    "変装",
  ],
  交渉技能: ["言いくるめ", "信用", "説得", "値切り", "母国語"],
  知識技能: [
    "医学",
    "オカルト",
    "化学",
    "クトゥルフ神話",
    "芸術",
    "経理",
    "考古学",
    "コンピューター",
    "心理学",
    "人類学",
    "生物学",
    "地質学",
    "電子工学",
    "天文学",
    "博物学",
    "物理学",
    "法律",
    "薬学",
    "歴史",
  ],
};
const COC6_SKILL_BASES: Record<string, number> = Object.fromEntries(
  Object.values(COC_SKILL_CATEGORIES).flat().map((name) => [name, 1]),
);
Object.assign(COC6_SKILL_BASES, {
  回避: 24,
  キック: 25,
  組み付き: 25,
  "こぶし（パンチ）": 50,
  頭突き: 10,
  投擲: 25,
  拳銃: 20,
  サブマシンガン: 15,
  ショットガン: 30,
  マシンガン: 15,
  ライフル: 25,
  応急手当: 30,
  隠す: 15,
  隠れる: 10,
  聞き耳: 25,
  忍び歩き: 10,
  写真術: 10,
  追跡: 10,
  登攀: 40,
  図書館: 25,
  目星: 25,
  "運転（自動車）": 20,
  機械修理: 20,
  乗馬: 5,
  水泳: 25,
  製作: 5,
  跳躍: 25,
  電気修理: 10,
  ナビゲート: 10,
  変装: 1,
  言いくるめ: 5,
  信用: 15,
  説得: 15,
  値切り: 5,
  母国語: 70,
  医学: 5,
  オカルト: 5,
  クトゥルフ神話: 0,
  経理: 10,
  博物学: 10,
  歴史: 20,
});

function intValue(value: unknown, fallback = 0): number {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function deriveCoc6(characteristics: Record<string, number>) {
  const hp = Math.ceil((characteristics.CON + characteristics.SIZ) / 2);
  const mp = characteristics.POW;
  return {
    hp,
    mp,
    sanity: clampPercent(characteristics.POW * 5),
    luck: clampPercent(characteristics.POW * 5),
    idea: clampPercent(characteristics.INT * 5),
    knowledge: Math.min(99, characteristics.EDU * 5),
    dodge: clampPercent(characteristics.DEX * 2),
  };
}

function normalizeCocPcState(
  raw: CocPcState | undefined,
  displayName: string,
  ruleset: string,
): CocPcState {
  const characteristics = { ...COC6_DEFAULT_CHARACTERISTICS };
  for (const key of COC_CHARACTERISTICS) {
    characteristics[key] = intValue(raw?.characteristics?.[key], characteristics[key]);
  }
  const derived = deriveCoc6(characteristics);
  const skills = {
    ...COC6_SKILL_BASES,
    ...(raw?.skills ?? {}),
    回避: intValue(raw?.skills?.["回避"], derived.dodge),
    母国語: derived.knowledge,
  };
  const stats = Object.fromEntries(
    COC_CHARACTERISTICS.map((key) => [key, clampPercent(characteristics[key] * 5)]),
  );
  return {
    sheet_format: "coc_investigator_v1",
    ruleset: COC_RULESETS.has(ruleset) ? ruleset : "coc6",
    name: raw?.name || displayName || "探索者",
    player_name: raw?.player_name || "",
    occupation: raw?.occupation || "",
    age: raw?.age || "",
    sex: raw?.sex || "",
    hp: intValue(raw?.hp, derived.hp),
    max_hp: intValue(raw?.max_hp, derived.hp),
    mp: intValue(raw?.mp, derived.mp),
    max_mp: intValue(raw?.max_mp, derived.mp),
    sanity: intValue(raw?.sanity, derived.sanity),
    max_sanity: intValue(raw?.max_sanity, 99),
    luck: intValue(raw?.luck, derived.luck),
    idea: derived.idea,
    knowledge: derived.knowledge,
    characteristics,
    stats: { ...stats, アイデア: derived.idea, 幸運: derived.luck, 知識: derived.knowledge },
    skills,
    weapons: raw?.weapons ?? [],
    armor: raw?.armor || "",
    conditions: raw?.conditions ?? [],
    items: raw?.items ?? ["スマートフォン", "財布", "筆記具"],
    notes: raw?.notes || "",
  };
}

export type {
  Scenario,
  ScenarioCharacter,
  CocPcState,
  ScenarioScene,
  ScenarioEpisode,
  CanonEntry,
  LoreBookEntry,
  LoreBook,
  TRPGScenarioDocument,
  TRPGStructureNode,
  TRPGStructureLink,
  TRPGStructure,
  ScenarioDetail,
  ScenarioPayload,
};
export {
  scenarioDefaultImage,
  pyFetch,
  unwrapScenario,
  TRPG_STRUCTURE_NODE_TYPES,
  asRecord,
  normalizeStructureForEditor,
  parseStructureText,
  makeStructureNodeId,
  selectClassName,
  GENRES,
  SCENARIO_KINDS,
  TRPG_RULESETS,
  COC_RULESETS,
  ROLES,
  SCENE_TYPES,
  DIFFICULTIES,
  IMPORTANCES,
  STATUSES,
  STATUS_LABELS,
  STATUS_COLORS,
  CANON_CATEGORIES,
  CANON_CATEGORY_LABELS,
  COC_CHARACTERISTICS,
  COC6_DEFAULT_CHARACTERISTICS,
  COC_SKILL_CATEGORIES,
  COC6_SKILL_BASES,
  intValue,
  clampPercent,
  deriveCoc6,
  normalizeCocPcState,
};
