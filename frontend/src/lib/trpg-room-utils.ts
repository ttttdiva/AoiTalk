// TRPG ルームページから抽出した型・定数・API ヘルパー・純粋ユーティリティ

import type { TRPGUIModule } from "@/components/trpg/ui-module-panel";

// ─── Types ───

type Participant = {
  id: string;
  play_session_id: string;
  user_id: string | null;
  character_id: string | null;
  display_name: string;
  role: "player" | "gm" | "npc" | "observer";
  participant_kind: "human" | "ai_character" | "system";
  avatar_url: string;
  color: string;
  seat_index: number;
  pc_state: Record<string, unknown>;
  is_active_participant: boolean;
  is_connected: boolean;
};

type ScenarioCharacter = {
  id: string;
  character_id: string | null;
  name: string;
  role: string;
  description: string;
  relationships_data?: Array<Record<string, unknown>>;
  trpg_ruleset?: string;
  trpg_pc_state?: Record<string, unknown>;
  sheet_metadata?: Record<string, unknown>;
};

type PlayLog = {
  id: string;
  play_session_id: string;
  participant_id: string | null;
  log_type:
    | "narration"
    | "speech"
    | "action"
    | "dice"
    | "scene_change"
    | "system"
    | "image"
    | "bgm"
    | "state_change"
    | "ooc";
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

type Disclosure = {
  id: string;
  play_session_id: string;
  creator_participant_id: string | null;
  disclosure_type: "handout" | "item" | "clue" | "image" | "note";
  visibility: "public" | "private" | "gm";
  target_participant_ids: string[];
  title: string;
  content: string;
  image_url: string;
  image_path: string;
  tags: string[];
  metadata: Record<string, unknown>;
  is_pinned: boolean;
  created_at: string;
  updated_at: string | null;
};

type PrivateMessage = {
  id: string;
  play_session_id: string;
  sender_participant_id: string | null;
  sender_label: string;
  target_participant_ids: string[];
  message_type: "private" | "gm" | "mention";
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

type QuickNPCSuggestion = {
  name: string;
  source: "ai" | "fallback" | string;
  profile?: Record<string, unknown>;
  pc_state?: Record<string, unknown>;
};

type Scenario = {
  id: string;
  title: string;
  description: string;
  opening_text: string;
  scenario_kind?: "writing" | "trpg";
  ruleset?: string;
  genre?: string;
  tags?: string[];
  cover_image_path?: string;
  characters?: ScenarioCharacter[];
};

type Scene = {
  id: string;
  title: string;
  description: string;
  image_prompt: string;
  state_snapshot?: Record<string, unknown>;
};

type Room = {
  id: string;
  room_code: string;
  room_title: string;
  status: string;
  host_user_id?: string | null;
  max_players: number;
  gm_mode: string;
  is_multiplayer: boolean;
  is_public: boolean;
  turn_order: string[];
  current_turn_participant_id: string | null;
  shared_state: Record<string, unknown>;
  scenario?: Scenario;
  current_scene?: Scene;
  participants: Participant[];
  logs: PlayLog[];
};

type NpcStrategyState = {
  id?: string;
  status?: "scheduled" | "processing" | "processed" | "error" | string;
  phase?: string;
  focus?: string;
  delay_seconds?: number;
  due_at?: string;
  processed_at?: string | null;
  error?: string;
};

type BgmState = {
  track?: string;
  volume?: number;
  at?: string;
};

type CocActionResult = {
  participant?: Participant;
  defender?: Participant | null;
  log?: PlayLog;
  result?: unknown;
};

type CocPostSessionResult = {
  room?: Room;
  participants?: Participant[];
  logs?: PlayLog[];
  results?: Array<Record<string, unknown>>;
};

const COC_CHARACTERISTICS = ["STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU"] as const;
const COC_DEFAULT_CHARACTERISTICS: Record<string, number> = {
  STR: 10,
  CON: 11,
  POW: 12,
  DEX: 12,
  APP: 10,
  SIZ: 11,
  INT: 14,
  EDU: 14,
};
const COC_KEY_SKILLS = [
  "目星",
  "聞き耳",
  "図書館",
  "心理学",
  "説得",
  "応急手当",
  "回避",
  "こぶし（パンチ）",
] as const;
const COC_DEFAULT_SKILLS: Record<string, number> = {
  目星: 55,
  聞き耳: 50,
  図書館: 50,
  心理学: 40,
  説得: 45,
  応急手当: 30,
  回避: 24,
  "こぶし（パンチ）": 50,
};
const COC_CORE_WEAPONS = [
  "こぶし",
  "キック",
  "頭突き",
  "組み付き",
  "ナイフ",
  "小型棍棒",
  "大型棍棒",
  "拳銃",
  "ライフル",
  "ショットガン",
  "投擲",
] as const;

type CocPersonal = {
  name: string;
  occupation: string;
  age: string;
  sex: string;
};

type GenericSkillDraft = {
  name: string;
  value: string;
};

type GenericPcDraft = {
  description: string;
  hp: string;
  mp: string;
  skills: GenericSkillDraft[];
  items: string;
  notes: string;
};

const DEFAULT_GENERIC_PC_DRAFT: GenericPcDraft = {
  description: "",
  hp: "10",
  mp: "",
  skills: [
    { name: "知覚", value: "" },
    { name: "交渉", value: "" },
    { name: "運動", value: "" },
    { name: "", value: "" },
  ],
  items: "",
  notes: "",
};

function intValue(value: unknown, fallback = 0): number {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function isCocScenario(room: Room): boolean {
  if (room.scenario?.scenario_kind === "trpg") {
    return ["coc", "coc6", "coc7"].includes((room.scenario.ruleset || "").toLowerCase());
  }
  const genre = (room.scenario?.genre || "").toLowerCase();
  const tags = room.scenario?.tags?.map((tag) => tag.toLowerCase()) || [];
  return ["coc", "coc6", "coc7", "call_of_cthulhu"].includes(genre)
    || tags.some((tag) => ["coc", "coc6", "coc7", "cthulhu"].includes(tag));
}

function defaultDiceExpression(room: Room): string {
  const ruleset = (room.scenario?.ruleset || room.scenario?.genre || "").toLowerCase();
  if (["coc", "coc6", "coc7"].includes(ruleset)) return "1d100";
  if (ruleset.includes("shinobigami")) return "2d6";
  return "2d6";
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

function buildCocPcState(
  personal: CocPersonal,
  characteristics: Record<string, number>,
  skills: Record<string, number>,
) {
  const derived = deriveCoc6(characteristics);
  const mergedSkills = {
    ...skills,
    回避: skills["回避"] ?? derived.dodge,
    母国語: derived.knowledge,
  };
  const stats = Object.fromEntries(
    COC_CHARACTERISTICS.map((key) => [key, clampPercent(characteristics[key] * 5)]),
  );
  return {
    sheet_format: "coc_investigator_v1",
    ruleset: "coc6",
    name: personal.name,
    occupation: personal.occupation,
    age: personal.age,
    sex: personal.sex,
    hp: derived.hp,
    max_hp: derived.hp,
    mp: derived.mp,
    max_mp: derived.mp,
    sanity: derived.sanity,
    max_sanity: 99,
    luck: derived.luck,
    idea: derived.idea,
    knowledge: derived.knowledge,
    characteristics,
    stats: {
      ...stats,
      アイデア: derived.idea,
      幸運: derived.luck,
      知識: derived.knowledge,
    },
    skills: mergedSkills,
    conditions: [],
    items: ["スマートフォン", "財布", "筆記具"],
    notes: "",
  };
}

function buildGenericPcState(
  name: string,
  draft: GenericPcDraft,
  ruleset?: string,
) {
  const skills = Object.fromEntries(
    draft.skills
      .map((skill) => ({
        name: skill.name.trim(),
        value: skill.value.trim(),
      }))
      .filter((skill) => skill.name)
      .map((skill) => [
        skill.name,
        skill.value ? intValue(skill.value, 0) : 0,
      ]),
  );
  const hp = intValue(draft.hp, 0);
  const mp = intValue(draft.mp, 0);
  return {
    sheet_format: "generic_pc_v1",
    ruleset: ruleset || "generic",
    name,
    description: draft.description.trim(),
    hp: hp > 0 ? hp : undefined,
    max_hp: hp > 0 ? hp : undefined,
    mp: mp > 0 ? mp : undefined,
    max_mp: mp > 0 ? mp : undefined,
    skills,
    items: draft.items
      .split(/[\n,、]/)
      .map((item) => item.trim())
      .filter(Boolean),
    notes: draft.notes.trim(),
  };
}

function applyCocPcState(
  state: Record<string, unknown> | undefined,
  fallbackName: string,
) {
  const characteristics = { ...COC_DEFAULT_CHARACTERISTICS };
  const rawCharacteristics =
    state?.characteristics && typeof state.characteristics === "object"
      ? (state.characteristics as Record<string, unknown>)
      : {};
  for (const key of COC_CHARACTERISTICS) {
    if (key in rawCharacteristics) {
      characteristics[key] = intValue(rawCharacteristics[key], characteristics[key]);
    }
  }
  const skills = { ...COC_DEFAULT_SKILLS };
  const rawSkills =
    state?.skills && typeof state.skills === "object"
      ? (state.skills as Record<string, unknown>)
      : {};
  for (const skill of COC_KEY_SKILLS) {
    if (skill in rawSkills) {
      skills[skill] = clampPercent(intValue(rawSkills[skill], skills[skill]));
    }
  }
  return {
    personal: {
      name: String(state?.name || fallbackName || ""),
      occupation: String(state?.occupation || ""),
      age: String(state?.age || ""),
      sex: String(state?.sex || ""),
    },
    characteristics,
    skills,
  };
}

function applyGenericPcState(
  state: Record<string, unknown> | undefined,
): GenericPcDraft {
  const skills = state?.skills && typeof state.skills === "object"
    ? Object.entries(state.skills as Record<string, unknown>)
        .slice(0, 4)
        .map(([name, value]) => ({ name, value: String(value ?? "") }))
    : [];
  while (skills.length < 4) {
    skills.push({ name: "", value: "" });
  }
  return {
    description: String(state?.description || ""),
    hp: String(state?.hp || state?.max_hp || "10"),
    mp: String(state?.mp || state?.max_mp || ""),
    skills,
    items: Array.isArray(state?.items)
      ? state.items.map((item) => String(item)).join("\n")
      : "",
    notes: String(state?.notes || ""),
  };
}

function parseCocPaste(text: string) {
  const normalized = text.normalize("NFKC").replaceAll("(", "（").replaceAll(")", "）");
  const characteristics: Record<string, number> = {};
  for (const key of COC_CHARACTERISTICS) {
    const match = normalized.match(new RegExp(`\\b${key}\\b\\s*[:：]?\\s*(\\d{1,3})`, "i"));
    if (match) characteristics[key] = intValue(match[1]);
  }
  const personal: Partial<CocPersonal> = {};
  for (const [label, key] of [
    ["キャラクター名", "name"],
    ["職業", "occupation"],
    ["年齢", "age"],
    ["性別", "sex"],
  ] as const) {
    const match = normalized.match(new RegExp(`${label}\\s*[:：]?\\s*([^\\n\\r\\t|/]+)`));
    if (match) personal[key] = match[1].trim();
  }
  const skills: Record<string, number> = {};
  for (const skill of COC_KEY_SKILLS) {
    const match = normalized.match(new RegExp(`${skill.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*[:：]?\\s*(?:[^\\d\\n\\r%]{0,20})?(\\d{1,3})\\s*%?`));
    if (match) skills[skill] = clampPercent(intValue(match[1]));
  }
  return { personal, characteristics, skills };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function checkedCocSkillNames(state: Record<string, unknown> | null): string[] {
  if (!state || !isRecord(state.skills) || !isRecord(state.skill_checks)) {
    return [];
  }
  return Object.entries(state.skill_checks)
    .filter(([skill, checked]) =>
      Boolean(checked)
      && skill !== "クトゥルフ神話"
      && Object.prototype.hasOwnProperty.call(state.skills, skill),
    )
    .map(([skill]) => skill)
    .sort();
}

function getBgmAutoEnabled(sharedState: Record<string, unknown>): boolean {
  const value = sharedState.bgm_auto_enabled;
  return value === undefined ? true : Boolean(value);
}

function getBgmState(sharedState: Record<string, unknown>): BgmState | null {
  const bgm = sharedState.bgm;
  if (!isRecord(bgm)) return null;
  return {
    track: typeof bgm.track === "string" ? bgm.track : undefined,
    volume: typeof bgm.volume === "number" ? bgm.volume : undefined,
    at: typeof bgm.at === "string" ? bgm.at : undefined,
  };
}

function getNpcStrategyState(sharedState: Record<string, unknown>): NpcStrategyState | null {
  const strategy = sharedState.ai_npc_strategy;
  if (!isRecord(strategy)) return null;
  return {
    id: typeof strategy.id === "string" ? strategy.id : undefined,
    status: typeof strategy.status === "string" ? strategy.status : undefined,
    phase: typeof strategy.phase === "string" ? strategy.phase : undefined,
    focus: typeof strategy.focus === "string" ? strategy.focus : undefined,
    delay_seconds:
      typeof strategy.delay_seconds === "number" ? strategy.delay_seconds : undefined,
    due_at: typeof strategy.due_at === "string" ? strategy.due_at : undefined,
    processed_at:
      typeof strategy.processed_at === "string" ? strategy.processed_at : null,
    error: typeof strategy.error === "string" ? strategy.error : undefined,
  };
}

function npcStrategyStatusLabel(status?: string): string {
  if (status === "scheduled") return "予約中";
  if (status === "processing") return "思考中";
  if (status === "processed") return "完了";
  if (status === "error") return "エラー";
  return "未予約";
}

function uiModuleList(value: unknown): TRPGUIModule[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is TRPGUIModule =>
      isRecord(item) && typeof item.id === "string" && item.id.trim().length > 0,
  );
}

function collectUiModules(room: Room | null): TRPGUIModule[] {
  if (!room) return [];
  const sharedModules = uiModuleList(room.shared_state?.ui_modules);
  const sceneModules = uiModuleList(room.current_scene?.state_snapshot?.ui_modules);
  const merged = new Map<string, TRPGUIModule>();
  for (const uiModule of [...sharedModules, ...sceneModules]) {
    merged.set(uiModule.id, uiModule);
  }
  return Array.from(merged.values());
}

function getUiModuleState(sharedState: Record<string, unknown>): Record<string, Record<string, unknown>> {
  return isRecord(sharedState.ui_module_state)
    ? (sharedState.ui_module_state as Record<string, Record<string, unknown>>)
    : {};
}

function isVideoAudioUrl(value: string): boolean {
  return /(?:youtube\.com|youtu\.be|nicovideo\.jp|nico\.ms)/i.test(value);
}

function isDirectAudioUrl(value: string): boolean {
  return /^https?:\/\/.+\.(?:mp3|wav|ogg|m4a|flac)(?:[?#].*)?$/i.test(value);
}

async function py<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    throw new Error(`API Error: ${res.status}`);
  }
  return res.json();
}

function generatedImageSrc(imagePath: string): string {
  if (!imagePath) return "";
  if (/^https?:\/\//i.test(imagePath) || imagePath.startsWith("/api/")) {
    return imagePath;
  }
  return `/api/python-proxy/filer/image-thumbnail?path=${encodeURIComponent(imagePath)}&size=1024`;
}

function scenarioImageSrc(path?: string, size = 640): string {
  if (!path) return "";
  if (/^https?:\/\//i.test(path) || path.startsWith("/api/")) {
    return path;
  }
  return `/api/python-proxy/filer/image-thumbnail?path=${encodeURIComponent(path)}&size=${size}`;
}

function participantInitials(name?: string): string {
  const trimmed = (name || "").trim();
  if (!trimmed) return "?";
  const words = trimmed.split(/\s+/).filter(Boolean);
  if (words.length >= 2) {
    return `${words[0][0] || ""}${words[1][0] || ""}`.toUpperCase();
  }
  return trimmed.slice(0, 2).toUpperCase();
}

function targetLabel(targetId: string, participants: Participant[]): string {
  if (targetId === "gm") return "AI GM";
  return participants.find((participant) => participant.id === targetId)?.display_name || "不明";
}

function extractMentionTargets(
  text: string,
  participants: Participant[],
): string[] {
  const targets = new Set<string>();
  for (const match of text.matchAll(/@([^\s@]+)/g)) {
    const token = match[1].replace(/[、,。.!?！？]$/, "").toLowerCase();
    if (["gm", "aigm", "ai", "ゲームマスター"].includes(token)) {
      targets.add("gm");
      continue;
    }
    const participant = participants.find((item) => {
      const name = item.display_name.toLowerCase();
      return name === token || name.startsWith(token);
    });
    if (participant) targets.add(participant.id);
  }
  return Array.from(targets);
}

export type {
  Participant,
  ScenarioCharacter,
  PlayLog,
  Disclosure,
  PrivateMessage,
  QuickNPCSuggestion,
  Scenario,
  Scene,
  Room,
  NpcStrategyState,
  BgmState,
  CocActionResult,
  CocPostSessionResult,
  CocPersonal,
  GenericSkillDraft,
  GenericPcDraft,
};
export {
  COC_CHARACTERISTICS,
  COC_DEFAULT_CHARACTERISTICS,
  COC_KEY_SKILLS,
  COC_DEFAULT_SKILLS,
  COC_CORE_WEAPONS,
  DEFAULT_GENERIC_PC_DRAFT,
  intValue,
  clampPercent,
  isCocScenario,
  defaultDiceExpression,
  deriveCoc6,
  buildCocPcState,
  buildGenericPcState,
  applyCocPcState,
  applyGenericPcState,
  parseCocPaste,
  isRecord,
  checkedCocSkillNames,
  getBgmAutoEnabled,
  getBgmState,
  getNpcStrategyState,
  npcStrategyStatusLabel,
  uiModuleList,
  collectUiModules,
  getUiModuleState,
  isVideoAudioUrl,
  isDirectAudioUrl,
  py,
  generatedImageSrc,
  scenarioImageSrc,
  participantInitials,
  targetLabel,
  extractMentionTargets,
};
