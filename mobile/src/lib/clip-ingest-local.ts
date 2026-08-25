/**
 * モバイルLLMだけで完結するクリップ取り込み。
 *
 * AoiTalk サーバーへ到達できないときの2段目のフォールバック。サーバー実装
 * (`src/services/clip_ingest_service.py`) のルーティングプロンプトを、端末で
 * 実行できる範囲へ簡約して同じ判断（登録済み候補から1件へ分類し、要約する）を行う。
 *
 * 保存はローカルファーストの規約どおり `docsRepo.createNode()` へ乗せる。作成した
 * ノードは outbox 経由で次の同期時にサーバーへ送られる。
 */

import { docsRepo } from "../repositories/docs";
import type { ClipIngestDocBlockInput } from "../repositories/docs";
import type { ClipIngestResult } from "./docs-api";
import {
  generateMobileLlmReply,
  getConfiguredClipIngestMobileLlmSettings,
} from "./mobile-llm";
import {
  loadClipIngestTargets,
  type ClipIngestTarget,
} from "./clip-ingest-targets";
import type { DocsNode } from "../types/api";
import {
  canonicalizeIngestUrl,
  redactPromptValue,
  redactSensitiveUrlsForPrompt,
} from "./clip-url";
export { canonicalizeIngestUrl, redactSensitiveUrlsForPrompt } from "./clip-url";

/**
 * ローカル取り込みが実行できなかった理由。呼び出し側の分岐（保留キュー）に使う。
 *
 * この例外は「ノードを1件も作っていない＝副作用が無い」ことを表す契約なので、
 * ノード作成を始めた後の失敗では絶対に投げない（保留へ回すと二重取り込みになる）。
 */
export class LocalClipIngestUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LocalClipIngestUnavailableError";
  }
}

/**
 * 副作用（ノード作成）前の処理を包み、どんな失敗も「実行不可」へ寄せる。
 * LLM呼び出し失敗・JSON解釈失敗・SQLite読み取り失敗などが対象。
 */
async function beforeSideEffects<T>(
  label: string,
  run: () => T | Promise<T>,
): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (error instanceof LocalClipIngestUnavailableError) throw error;
    const detail = error instanceof Error ? error.message : String(error ?? "");
    throw new LocalClipIngestUnavailableError(
      detail ? `${label}: ${detail}` : label,
    );
  }
}

const URL_PATTERN = /https?:\/\/[^\s<>"'`]+/g;
// Keep the local outline width identical to the server contract.  A wider
// semantic item creates a different tree shape and encourages meta prose.
const SUMMARY_LIMIT = 480;
// v4のknowledge_itemsも、旧summary/detailsと同じくアウトラインの1行へ
// 収まる範囲で安全側に制限する。意味単位を増やしすぎると端末側の
// transaction/outboxが入力一件で膨張するため、サーバーと同じ件数上限を使う。
const KNOWLEDGE_ITEM_LIMIT = 8;
const EXCERPT_LIMIT = 4;
const EXCERPT_LINE_LIMIT = 12;
const TITLE_LIMIT = 240;
const CONTENT_MODES = new Set(["summary", "verbatim", "mixed"] as const);
const SHORT_LITERAL_KINDS = new Set(["command", "setting", "url", "short_quote"] as const);
const VERBATIM_KINDS = new Set(["prompt", "code", "script", "quote", "formatted"] as const);
const PROMPT_MARKER_PATTERN = /(?:\b(?:system|user|negative|image|video)\s*prompt\b|\bprompt\b|プロンプト|指示文|生成指示|ネガティブ(?:プロンプト)?|正のプロンプト)/i;
// A marker in the source line itself must be an explicit leading label, as on
// the server.  A free-standing occurrence of "prompt" inside ordinary prose
// is not enough to turn that prose into durable verbatim content.
const PROMPT_LINE_MARKER_PATTERN = /^\s*(?:\[(?:system|user|negative|image|video)?\s*prompt\]|(?:system|user|negative|image|video)\s*prompt|prompt|プロンプト|指示文|生成指示|ネガティブ(?:プロンプト)?|正のプロンプト)\s*[:：\-–]\s*\S/i;
const PROMPT_MARKER_ONLY_PATTERN = /^\s*(?:\[(?:system|user|negative|image|video)?\s*prompt\]|(?:system|user|negative|image|video)\s*prompt|prompt|プロンプト|指示文|生成指示|ネガティブ(?:プロンプト)?|正のプロンプト)\s*$/i;
const META_SUMMARY_PATTERN = /^(?:この(?:記事|投稿|ページ|内容)|この記事|投稿者|著者|本文|内容|ページ).{0,180}(?:紹介している|紹介する|説明している|説明する|解説している|解説する|所感を含む|感想を含む|おすすめしている|おすすめする|まとめている)[。.!！]?$|^(?:所感|感想|紹介|説明|おすすめ)(?:を含む|している|する|です)?[。.!！]?$/;
// The server also treats attribution-only forms as metadata.  Keep these
// narrowly scoped so a source-grounded concrete sentence (for example a JPG
// procedure) still survives the residual/grounding checks below.
const META_SUMMARY_CONTEXT_PATTERN = /^(?:.+?についての(?:所感|感想)(?:も)?含む|投稿者(?:が|は).*(?:挙げる|述べる))[。.!！]?$/;
const SOURCE_OBSERVER_PREFIX_PATTERN = /^(?:投稿(?:例|文)では[、,]?|投稿者(?:は|が)|この(?:記事|投稿|ページ|内容)では[、,]?|(?:上記)?記事では[、,]?|本文では[、,]?|添付(?:画像)?では[、,]?|画像では[、,]?)/;
const OBSERVER_ONLY_ATTRIBUTION_PATTERN = /^(?:投稿者(?:は|が)|この(?:記事|投稿|ページ|内容)|投稿(?:例|文))[^。!?！？]*(?:挙げる|述べる|説明する|紹介する|触れる|言及する)[。.!！]?$/;
const OBSERVER_SUFFIX_PATTERN = "(?:を|について)?(?:説明している|説明する|紹介している|紹介する|解説している|解説する|触れている|触れる|言及している|言及する|挙げる|挙げている|述べる|述べている)[。.!！]?$";
const OBSERVER_SUFFIX_PATTERN_GLOBAL = new RegExp(OBSERVER_SUFFIX_PATTERN);
const OBSERVER_WRAPPED_CLAIM_PATTERN = new RegExp(
  "^(?:投稿者(?:は|が)|この(?:記事|投稿|ページ|内容)(?:は|では)?[、,]?|投稿(?:例|文)(?:では)?[、,]?|(?:上記)?記事では[、,]?|本文では[、,]?|添付(?:画像)?では[、,]?|画像では[、,]?)(.+?)"
    + OBSERVER_SUFFIX_PATTERN,
);
const PERSONAL_MEASUREMENT_SOURCE_PATTERN = /(?:個人実測|実測した|実測では|運ゲー|投稿者(?:の|は|が))/;
const PERFORMANCE_MEASUREMENT_PATTERN = /\d+\s*(?:秒|fps|フレーム|ms|枚|回)/i;
const ENVIRONMENT_SCOPE_PATTERN = /(?:RTX|GPU|環境|ComfyUI|v\d)/i;
const FABRICATED_CAPABILITY_PATTERN = /(?:OCR|自動認識|自動検出|搭載|対応している|機能がある|機能を持つ|可能である)/i;
const UNSUPPORTED_CLAIM_MATERIAL_PATTERN = /(?:OCR|自動認識|自動検出|有料|無料|品質|対応OS|搭載|機能がある|機能を持つ|ベンチマーク|最速|最高|おすすめ|評価が高い|サービスで)/gi;
const CONCRETE_KNOWLEDGE_TOKEN_PATTERN = /(?:手順|方法|使い方|設定|条件|数値|手続|比較|結果|必要|対応|整え|番号|白塗り|文字|生成|\d|[A-Za-z]{3,})/;
const META_SUMMARY_SCAFFOLD_PATTERN = /(?:この(?:記事|投稿|ページ|内容)|この記事|投稿者|著者|本文|内容|ページ|紹介している|紹介する|説明している|説明する|解説している|解説する|所感を含む|感想を含む|所感(?:も)?含む|感想(?:も)?含む|おすすめしている|おすすめする|まとめている|挙げる|述べる|についての)/g;
// A heading by itself is not reusable knowledge.  Keep concrete phrases such
// as "RTX5090での手順" intact; only exact generic labels are discarded.
const GENERIC_KNOWLEDGE_ITEM_PATTERN = /^(?:概要|要約|詳細|補足|説明|内容|特徴|ポイント|メリット|理由|おすすめ理由|手順|方法|原文|出典|参考|メモ|情報)$/i;

// Topic selection is intentionally stricter than knowledge-item grounding.
// These lines identify provenance/settings rather than the reusable subject of
// a clip.  Keep the classifier narrow so a normal sentence such as
// "投稿者はJPGを圧縮する" remains usable knowledge.
const TOPIC_METADATA_PREFIX_PATTERN = /^\s*(?:model(?:_name)?|使用モデル|利用モデル|checkpoint|provider|author|source|environment|env|実行環境|使用環境|環境|利用環境|作者|著者|出典|提供元)\s*[:：=]/i;
const TOPIC_AUTHOR_ATTRIBUTION_PATTERN = /^\s*(?:[^:：\n]{1,100}\s*)?\(?@[A-Za-z0-9_][A-Za-z0-9_.-]*\)?\s*$/;
const TOPIC_GENERIC_HEADING_PATTERN = /^(?:プロンプト知見|知見|こうかな(?:[?？])?|タイトル|概要|要約|詳細|補足|説明|内容|特徴|ポイント|メリット|理由|メモ|情報|参考|出典|原文|手順|方法|prompt\s*knowledge|prompt\s*tips?|knowledge|note|tips?)$/i;
const TOPIC_CENTER_MARKER_PATTERN = /(?:全体として|構図|設計|ワークフロー|手法|方法|手順|設定|使い方|視点|姿勢|条件|工夫|結果|比較|考え方|流れ)/i;
const TITLE_PREDICATE_GROUPS = [
  ["見せる", "見える", "見せ方"],
  ["使う", "使用", "利用"],
  ["整える", "整えて", "整え"],
  ["設定", "調整", "配置"],
  ["設計", "構図"],
];
const TITLE_UNSUPPORTED_MATERIAL_PATTERN = /(?:万能|高性能|最速|最高|自動認識|自動検出|対応している|対応する|機能がある|機能を持つ|有料|無料|おすすめ|能力|性能)/i;

/** ルーティング中に実際の説明を確認する候補数の上限。 */
export const MAX_ROUTING_INSPECTIONS = 3;
/** accept/最終比較で要求する安全側の確信度。 */
export const MIN_ROUTING_CONFIDENCE = 0.72;

type SourceRange = {
  sourceId: string;
  startLine: number;
  endLine: number;
};

type ShortLiteral = SourceRange & {
  kind: "command" | "setting" | "url" | "short_quote";
  label: string;
};

type VerbatimRange = SourceRange & {
  kind: "prompt" | "code" | "script" | "quote" | "formatted";
  label: string;
};

export interface LocalVerbatimBlock {
  kind: VerbatimRange["kind"];
  label: string;
  content: string;
  sha256: string;
  char_count: number;
  line_count: number;
  blank_line_count: number;
  source_id: string;
  source_type: "input" | "direct" | "supplemental" | "attachment";
  source_url: string;
  start_line: number;
  end_line: number;
}

interface LlmPlan {
  targetId: string;
  matched: boolean;
  topic: string;
  subject: string;
  titleDetail: string;
  titleEvidence: SourceRange[];
  contentMode: "summary" | "verbatim" | "mixed";
  /** v4 canonical wire field. Each item is a directly reusable claim. */
  knowledgeItems: string[];
  /** v2/v3/legacy compatibility aliases. Keep these internal only. */
  summary: string;
  details: string[];
  unconfirmed: string[];
  shortLiterals: ShortLiteral[];
  verbatimRanges: VerbatimRange[];
  excerpts: Array<{ label: string; lines: string[] }>;
  legacySchema: boolean;
}

interface UrlFetchResult {
  url: string;
  success: boolean;
  body: string;
  error?: string;
}

export function extractUrls(source: string): string[] {
  const matches = String(source || "").match(URL_PATTERN) ?? [];
  const urls: string[] = [];
  for (const raw of matches) {
    // 文末の句読点・閉じ括弧はURLに含めない。
    const url = canonicalizeIngestUrl(raw.replace(/[),.。、」』】\]]+$/, ""));
    if (url && !urls.includes(url)) urls.push(url);
  }
  return urls.slice(0, 20);
}

/**
 * Server `canonicalize_ingest_url` と同じ意味の、入力URLの比較用正規化。
 * 取得は端末で行わないが、重複判定・failed_urls・outbox provenance の
 * すべてで同じ値を使い、tracking query や末尾slashで差が出ないようにする。
 */
/** HTML から本文らしいテキストだけを粗く抽出する（端末での判定材料用）。 */
export function htmlToText(html: string): string {
  return String(html || "")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<noscript[\s\S]*?<\/noscript>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|li|h[1-6]|tr)>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export interface LocalRoutingChoice {
  targetId: string;
  confidence: number;
}

export type LocalRoutingActionName = "accept" | "inspect" | "fallback";

export interface LocalRoutingAction {
  action: LocalRoutingActionName;
  currentTargetId: string;
  nextTargetId: string;
  confidence: number;
  reason: string;
  ambiguous: boolean;
}

export interface LocalRoutingHistoryEntry {
  targetId: string;
  title: string;
  breadcrumb: string[];
  routingHint: string;
  confidence: number;
  action: LocalRoutingActionName | "selected";
  reason: string;
}

interface LocalRoutingResult {
  target: ClipIngestTarget;
  /** 自動分類が確定したか。fallback経路では false を維持する。 */
  matched: boolean;
}

function stableTargetId(target: Partial<ClipIngestTarget>): string {
  const nodeId = String(target.nodeId ?? "").trim().slice(0, 100);
  if (nodeId) return nodeId;
  const nodeSystemKey = String(target.nodeSystemKey ?? "").trim().slice(0, 500);
  return nodeSystemKey ? `key:${nodeSystemKey}` : "";
}

function isFallbackTarget(target: Partial<ClipIngestTarget>): boolean {
  return target.fallback === true || target.fallbackConfigured === true;
}

function uniqueNormalTargets(targets: ClipIngestTarget[]): ClipIngestTarget[] {
  const seen = new Set<string>();
  const result: ClipIngestTarget[] = [];
  for (const target of targets) {
    const id = stableTargetId(target);
    if (!id || isFallbackTarget(target) || seen.has(id)) continue;
    seen.add(id);
    result.push(target);
  }
  return result;
}

function safeFallbackTarget(targets: ClipIngestTarget[]): ClipIngestTarget | null {
  const seen = new Set<string>();
  for (const target of targets) {
    const id = stableTargetId(target);
    if (!id || !isFallbackTarget(target) || seen.has(id)) continue;
    seen.add(id);
    // parseClipIngestTargets は最初の fallback だけ `fallback=true` にする。
    // 旧キャッシュの明示 fallback も fallbackConfigured で拾う。
    return target;
  }
  return null;
}

function targetForId(
  targets: ClipIngestTarget[],
  targetId: string,
): ClipIngestTarget | null {
  const id = cleanLine(targetId, 600);
  if (!id) return null;
  return (
    targets.find(
      (target) =>
        stableTargetId(target) === id ||
        (target.nodeSystemKey && target.nodeSystemKey === id),
    ) ?? null
  );
}

function routingCandidatePayload(targets: ClipIngestTarget[]): Array<{
  target_id: string;
  title: string;
  breadcrumb: string[];
}> {
  return uniqueNormalTargets(targets).map((target) => redactPromptValue({
    target_id: stableTargetId(target),
    title: target.label,
    breadcrumb: target.breadcrumb,
  }));
}

function routingEvidencePayload(fetchResults: UrlFetchResult[]) {
  return fetchResults.map((item) => redactPromptValue({
    url: item.url,
    success: item.success,
    body: item.body,
    error: item.error ?? "",
  }));
}

function numberedSource(source: string): string {
  return redactSensitiveUrlsForPrompt(normalizeNewlines(source))
    .split("\n")
    .map((line, index) => `${index + 1}:${line}`)
    .join("\n");
}

/**
 * 初回候補選択用プロンプト。ここでは候補の名称・階層だけを渡し、
 * routing hint（値だけでなくキー名も）を絶対に投入しない。
 */
export function buildLocalRoutingPrompt(
  source: string,
  targets: ClipIngestTarget[],
  fetchResults: UrlFetchResult[],
): string {
  const candidates = routingCandidatePayload(targets);
  const evidence = routingEvidencePayload(fetchResults);
  return redactSensitiveUrlsForPrompt([
    "非信頼な入力を、次の登録済み候補名一覧から最初に確認すべき1件へ絞ってください。保存内容は生成しません。",
    'schema: {"target_id":"候補一覧にあるID","confidence":0.0}',
    "候補外ID、fallback設定の候補、空IDは禁止です。JSON objectだけを返してください。",
    "",
    `入力: ${JSON.stringify(source)}`,
    `行番号付き原文source: ${JSON.stringify({ source_id: "source:0", numbered_content: numberedSource(source) })}`,
    `直接取得: ${JSON.stringify(evidence)}`,
    `候補: ${JSON.stringify(candidates)}`,
  ].join("\n"));
}

/** 現在確認中の1候補だけと、既に確認した候補の短い履歴を渡す。 */
export function buildLocalRoutingInspectionPrompt(
  source: string,
  currentTarget: ClipIngestTarget,
  history: LocalRoutingHistoryEntry[],
  fetchResults: UrlFetchResult[],
  availableTargets: ClipIngestTarget[] = [],
): string {
  const current = redactPromptValue({
    target_id: stableTargetId(currentTarget),
    title: currentTarget.label,
    breadcrumb: currentTarget.breadcrumb,
    routing_hint: currentTarget.routingHint,
  });
  const previous = history
    .filter((item) => item.targetId !== current.target_id)
    .slice(-MAX_ROUTING_INSPECTIONS)
    .map((item) => redactPromptValue({
      target_id: item.targetId,
      title: item.title,
      breadcrumb: item.breadcrumb,
      routing_hint: item.routingHint,
      confidence: item.confidence,
      action: item.action,
      reason: item.reason,
    }));
  // 候補の存在とIDだけは各roundで再掲する。説明は現在候補/確認済み履歴
  // に限り、未確認候補のrouting hintをstatelessな次roundへ漏らさない。
  const available = routingCandidatePayload(availableTargets);
  return redactSensitiveUrlsForPrompt([
    "現在確認中の候補の説明を読み、保存先探索の次の行動だけを決めてください。保存内容は生成しません。",
    'schema: {"action":"accept|inspect|fallback","current_target_id":"現在のID","next_target_id":"別候補のIDまたは空",' +
      '"confidence":0.0,"reason":"短い理由"}',
    "acceptは現在候補で確定、inspectは候補一覧にある未確認候補の説明を1件だけ確認、fallbackは適合なしです。",
    "候補外ID・fallback設定候補・確認済み候補の再inspectは禁止です。JSON objectだけを返してください。",
    "",
    `入力: ${JSON.stringify(source)}`,
    `行番号付き原文source: ${JSON.stringify({ source_id: "source:0", numbered_content: numberedSource(source) })}`,
    `直接取得: ${JSON.stringify(routingEvidencePayload(fetchResults))}`,
    `現在の候補: ${JSON.stringify(current)}`,
    `候補ID一覧（説明未確認）: ${JSON.stringify(available)}`,
    `確認済み履歴: ${JSON.stringify(previous)}`,
  ].join("\n"));
}

/** 最大数に到達した際、確認済み候補だけを比較するプロンプト。 */
export function buildLocalRoutingComparePrompt(
  source: string,
  history: LocalRoutingHistoryEntry[],
  fetchResults: UrlFetchResult[],
): string {
  const candidates = history.slice(-MAX_ROUTING_INSPECTIONS).map((item) => redactPromptValue({
    target_id: item.targetId,
    title: item.title,
    breadcrumb: item.breadcrumb,
    routing_hint: item.routingHint,
    confidence: item.confidence,
    action: item.action,
    reason: item.reason,
  }));
  return redactSensitiveUrlsForPrompt([
    "確認済み候補の説明だけを比較し、最も適切な保存先を最終決定してください。未確認候補は推測しないでください。",
    'schema: {"target_id":"確認済み候補のID","matched":true,"ambiguous":false,"confidence":0.0}',
    "confidenceが0.72未満、matched=false、またはambiguous=trueならfallbackとしてください。JSON objectだけを返してください。",
    `入力: ${JSON.stringify(source)}`,
    `直接取得: ${JSON.stringify(routingEvidencePayload(fetchResults))}`,
    `確認済み候補: ${JSON.stringify(candidates)}`,
  ].join("\n"));
}

/** 保存先確定後の本文生成。候補一覧や未確認候補の説明は含めない。 */
export function buildLocalContentPrompt(
  source: string,
  target: ClipIngestTarget,
  fetchResults: UrlFetchResult[],
): string {
  const evidence = routingEvidencePayload(fetchResults);
  return redactSensitiveUrlsForPrompt([
    "保存先はコード側で既に確定しています。次の入力から保存内容だけをschema_version=4のJSON objectで生成してください。保存先を変更するフィールドは無視されます。",
    'schema: {"schema_version":4,"subject":"...","title_detail":"...",' +
      '"title_evidence":[{"source_id":"source:0","start_line":1,"end_line":1}],"content_mode":"summary|verbatim|mixed",' +
      '"knowledge_items":[],"short_literals":[{"kind":"command|setting|url|short_quote","label":"...",' +
      '"source_id":"source:0","start_line":1,"end_line":1}],"verbatim_ranges":[{"kind":"prompt|code|script|quote|formatted",' +
      '"label":"...","source_id":"source:0","start_line":1,"end_line":1}],"unconfirmed":[]}',
    "保存先関連のフィールドが返っても保存先には使用しません。",
    "",
    `確定保存先: ${JSON.stringify(redactPromptValue({ title: target.label, breadcrumb: target.breadcrumb }))}`,
    `入力: ${JSON.stringify(source)}`,
    `行番号付き原文source: ${JSON.stringify({ source_id: "source:0", numbered_content: numberedSource(source) })}`,
    `直接取得: ${JSON.stringify(evidence)}`,
    "",
    "タイトル・本文の規則:",
    "- topic/subjectはsourceから後で再利用したい中心知識（手法・知見・疑問・構図・ワークフロー・設定目的）を優先し、その識別に必要な製品/モデル名を次に選ぶ。title_detailは中心知識の自然な短い説明で、根拠行をtitle_evidenceで示す。",
    "- 使用モデル、実行環境、作者、出典などのmetadata（model: / model= / 使用モデル: / checkpoint: / provider: / author: / source: 等）は、それ自体を説明するclipでない限りtopic/subjectへ昇格させず、short_literals・setting・provenanceへ残す。『プロンプト知見』『シルル』のようなgeneric heading/作者行もtopicにしない。",
    "- title_evidenceは単一source rangeを根拠にし、subject/title_detailの中心語・主要述語がその範囲に存在する場合だけ採用する。範囲にない製品名、数値、version、能力を補わず、自由なsemantic fuzzy matchはしない。根拠のないtitle_detailは空にする。subjectとtitle_detailの区切りはコード側で付ける。",
    "- knowledge_itemsはsourceを見返さなくても直接再利用できる具体的な意味単位を1行1件、最大8件・各480字以内で返す。summary/detailsは返さず、この配列だけを正本にする。",
    "- knowledge_itemsへ『投稿文では』『投稿例では』『投稿者は』『この記事では』『添付画像では』などsourceを第三者として紹介するobserver framingを書かない。",
    "- knowledge_itemsへ『この記事では』『投稿者が説明している』などsourceの存在や第三者の説明を繰り返すmeta文・generic heading（概要、詳細、補足、理由、手順だけ等）を入れない。入力にある具体的な主張・手順・数値をそのまま再利用できる形へする。",
    "- 環境名、特定バージョン、GPU/モデル、個人の実測値・設定など条件が意味を変える場合はknowledge_itemsから削らず保持する。個人の所感を一般的な製品事実へ変換しない。",
    "- knowledge_items同士は重複させず、title_detailや原文ブロックと同じ内容を繰り返さない。入力が短くタイトルだけで意味が通る場合は空配列にし、分量を埋めるmeta説明を作らない。",
    "- 複数行プロンプト、コード、台詞、整形依存データ、500字超の行はverbatim_rangesで元入力の行範囲を指定する。原文をJSONへ転記しない。",
    "- 一行でも再利用可能な自然言語promptはkind=promptのverbatim_rangesでsource:0の行範囲を指定し、labelにprompt/プロンプト等の明示markerを含め、content_modeをverbatimまたはmixedにする。普通の説明文・所感・紹介文はverbatimにしない。",
    "- short_literalsは意味ある1行のコマンド、設定、URL、短い引用だけ。説明文をラベル階層へしない。",
    "- 原文の段落やページ全体を写さない。見出し、目次、広告、コード柵(```)、箇条書き記号、装飾記号は出力に含めない。",
    "- unconfirmedは出典から確認できなかった点。該当なしは空配列。",
    "- URLはknowledge_items等の本文へ書かない。保存時に出典として自動で付与される。",
    "- 直接取得に失敗したURLがある場合、確認できない内容は断定せずunconfirmedへ入れる。",
  ].join("\n"));
}

function cleanLine(value: unknown, limit: number): string {
  return String(value ?? "")
    .replace(/[\r\n\t]+/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim()
    .slice(0, limit);
}

/**
 * v3のsource:*アンカーを正本にしつつ、v2/legacyのinput/direct:Nを
 * lookup時だけ受ける。保存するverbatim blockにはcanonical IDだけを残す。
 */
export function canonicalSourceId(value: unknown): string {
  const sourceId = cleanLine(value, 80);
  if (sourceId === "input") return "source:0";
  const legacyDirect = /^direct:(\d+)$/.exec(sourceId);
  if (legacyDirect) return `source:${Number(legacyDirect[1])}`;
  return sourceId;
}

function normalizeNewlines(value: unknown): string {
  return String(value ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

function stringList(value: unknown, limit: number, itemLimit: number): string[] {
  return (Array.isArray(value) ? value : [])
    .map((item) => cleanLine(item, itemLimit))
    .filter(Boolean)
    .filter((item, index, items) => items.indexOf(item) === index)
    .slice(0, limit);
}

function parseSourceRange(value: unknown): SourceRange | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const sourceId = canonicalSourceId(record.source_id);
  const startLine = Number(record.start_line);
  const endLine = Number(record.end_line);
  if (
    !sourceId ||
    !Number.isInteger(startLine) ||
    !Number.isInteger(endLine) ||
    startLine < 1 ||
    endLine < startLine
  ) return null;
  return { sourceId, startLine, endLine };
}

function parseJsonRecord(raw: string): Record<string, unknown> {
  const text = String(raw ?? "").trim();
  const fenced = /^```(?:json)?\s*([\s\S]*?)\s*```$/i.exec(text);
  const candidate = fenced ? fenced[1] : text;
  const start = candidate.indexOf("{");
  const end = candidate.lastIndexOf("}");
  if (start < 0 || end <= start) {
    throw new Error("ルーティング応答のJSONを解釈できませんでした");
  }
  const parsed = JSON.parse(candidate.slice(start, end + 1)) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("ルーティング応答はJSON objectで返してください");
  }
  return parsed as Record<string, unknown>;
}

function boundedConfidence(value: unknown, defaultValue = 0): number {
  if (value === undefined || value === null || value === "") return defaultValue;
  if (typeof value !== "number" && typeof value !== "string") {
    throw new Error("confidenceが不正です");
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    throw new Error("confidenceが不正です");
  }
  return parsed;
}

function requireExplicitConfidence(
  parsed: Record<string, unknown>,
): number {
  if (!Object.prototype.hasOwnProperty.call(parsed, "confidence")) {
    throw new Error("confidenceがありません");
  }
  if (typeof parsed.confidence !== "number") {
    throw new Error("confidenceは数値で指定してください");
  }
  return boundedConfidence(parsed.confidence);
}

function rejectUnexpectedKeys(
  parsed: Record<string, unknown>,
  allowed: readonly string[],
  label: string,
): void {
  const allowedSet = new Set(allowed);
  if (Object.keys(parsed).some((key) => !allowedSet.has(key))) {
    throw new Error(`${label}に未対応フィールドがあります`);
  }
}

export function parseLocalRoutingChoice(raw: string): LocalRoutingChoice {
  const parsed = parseJsonRecord(raw);
  rejectUnexpectedKeys(parsed, ["target_id", "confidence", "matched"], "候補選択");
  if (parsed.matched === false) {
    // 適合なしは構文上有効な安全側の結果。呼び出し側で通常候補へ
    // 進まず、設定済みfallbackへコード側で落とす。
    return { targetId: "", confidence: 0 };
  }
  if (parsed.matched !== undefined) {
    throw new Error("候補選択のmatchedが不正です");
  }
  if (typeof parsed.target_id !== "string") {
    throw new Error("候補IDは文字列で指定してください");
  }
  const targetId = cleanLine(parsed.target_id, 600);
  if (!targetId) throw new Error("候補IDがありません");
  return {
    targetId,
    confidence: requireExplicitConfidence(parsed),
  };
}

export function parseLocalRoutingAction(raw: string): LocalRoutingAction {
  const parsed = parseJsonRecord(raw);
  rejectUnexpectedKeys(
    parsed,
    ["action", "current_target_id", "next_target_id", "confidence", "reason", "ambiguous"],
    "ルーティングaction",
  );
  const action = cleanLine(parsed.action, 30) as LocalRoutingActionName;
  if (action !== "accept" && action !== "inspect" && action !== "fallback") {
    throw new Error("ルーティングactionが不正です");
  }
  if (
    typeof parsed.current_target_id !== "string" ||
    (parsed.next_target_id !== undefined &&
      typeof parsed.next_target_id !== "string")
  ) {
    throw new Error("ルーティング候補IDは文字列で指定してください");
  }
  const currentTargetId = cleanLine(parsed.current_target_id, 600);
  if (!currentTargetId) throw new Error("current_target_idがありません");
  const nextTargetId = cleanLine(parsed.next_target_id, 600);
  if (action === "inspect" && !nextTargetId) {
    throw new Error("inspectにはnext_target_idが必要です");
  }
  const ambiguous = parsed.ambiguous === undefined ? false : parsed.ambiguous;
  if (typeof ambiguous !== "boolean") {
    throw new Error("ルーティングactionのambiguousが不正です");
  }
  return {
    action,
    currentTargetId,
    nextTargetId,
    confidence: requireExplicitConfidence(parsed),
    reason: cleanLine(parsed.reason, 400),
    ambiguous,
  };
}

export function parseLocalRoutingComparison(raw: string): LocalRoutingChoice & {
  matched: boolean;
  ambiguous: boolean;
} {
  const parsed = parseJsonRecord(raw);
  rejectUnexpectedKeys(
    parsed,
    ["target_id", "matched", "ambiguous", "confidence"],
    "ルーティング比較",
  );
  const targetId = cleanLine(parsed.target_id, 600);
  if (typeof parsed.matched !== "boolean" || typeof parsed.ambiguous !== "boolean") {
    throw new Error("比較結果のmatched/ambiguousが不正です");
  }
  if (typeof parsed.target_id !== "string") {
    throw new Error("比較候補IDは文字列で指定してください");
  }
  if (!targetId && parsed.matched) throw new Error("比較結果の候補IDがありません");
  return {
    targetId,
    confidence: requireExplicitConfidence(parsed),
    matched: parsed.matched,
    ambiguous: parsed.ambiguous,
  };
}

/** LLM 応答から JSON object だけを取り出す。取り出せなければ throw。 */
export function parseLocalPlan(raw: string): LlmPlan {
  let parsed: Record<string, unknown>;
  try {
    parsed = parseJsonRecord(raw);
  } catch {
    throw new Error("保存計画のJSONを解釈できませんでした");
  }
  const hasSchemaVersion = parsed.schema_version !== undefined && parsed.schema_version !== null;
  if (
    hasSchemaVersion
    && (typeof parsed.schema_version !== "number" || !Number.isInteger(parsed.schema_version))
  ) {
    throw new Error("保存計画のschema_versionが不正です");
  }
  const schemaVersion = hasSchemaVersion ? parsed.schema_version as number : 1;
  const isCanonicalV4 = schemaVersion === 4;
  const legacySchema = schemaVersion !== 2
    && schemaVersion !== 3
    && !isCanonicalV4
    && !(parsed.schema_version == null && typeof parsed.subject === "string");
  if (parsed.schema_version !== undefined && schemaVersion !== 2 && schemaVersion !== 3 && !isCanonicalV4) {
    throw new Error("保存計画のschema_versionが不正です");
  }
  if (isCanonicalV4 && !Array.isArray(parsed.knowledge_items)) {
    throw new Error("保存計画のknowledge_itemsが配列ではありません");
  }
  if (isCanonicalV4 && (parsed.knowledge_items as unknown[]).length > KNOWLEDGE_ITEM_LIMIT) {
    throw new Error("保存計画のknowledge_itemsが上限を超えています");
  }
  if (
    isCanonicalV4
    && (parsed.knowledge_items as unknown[]).some((item) => typeof item !== "string")
  ) {
    throw new Error("保存計画のknowledge_itemsに文字列以外があります");
  }
  if (isCanonicalV4 && (typeof parsed.subject !== "string" || !parsed.subject.trim())) {
    throw new Error("保存計画のsubjectが空です");
  }
  const excerptsRaw = Array.isArray(parsed.excerpts) ? parsed.excerpts : [];
  const excerpts: LlmPlan["excerpts"] = [];
  for (const item of excerptsRaw) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const label = cleanLine(record.label, 200);
    const lines = (Array.isArray(record.lines) ? record.lines : [])
      .map((line) => cleanLine(line, 500))
      .filter((line) => line.length > 0)
      .slice(0, EXCERPT_LINE_LIMIT);
    if (!label || lines.length === 0) continue;
    excerpts.push({ label, lines });
    if (excerpts.length >= EXCERPT_LIMIT) break;
  }
  const titleEvidence = (Array.isArray(parsed.title_evidence) ? parsed.title_evidence : [])
    .map(parseSourceRange)
    .filter((item): item is SourceRange => item !== null)
    .slice(0, 8);
  const shortLiterals = (Array.isArray(parsed.short_literals) ? parsed.short_literals : [])
    .flatMap((item): ShortLiteral[] => {
      const range = parseSourceRange(item);
      if (!range || !item || typeof item !== "object" || Array.isArray(item)) return [];
      const record = item as Record<string, unknown>;
      const kind = cleanLine(record.kind, 40) as ShortLiteral["kind"];
      const label = cleanLine(record.label, 200);
      if (!SHORT_LITERAL_KINDS.has(kind) || !label || range.startLine !== range.endLine) return [];
      return [{ ...range, kind, label }];
    })
    .slice(0, 12);
  const verbatimRanges = (Array.isArray(parsed.verbatim_ranges) ? parsed.verbatim_ranges : [])
    .flatMap((item): VerbatimRange[] => {
      const range = parseSourceRange(item);
      if (!range || !item || typeof item !== "object" || Array.isArray(item)) return [];
      const record = item as Record<string, unknown>;
      const kind = cleanLine(record.kind, 40) as VerbatimRange["kind"];
      const label = cleanLine(record.label, 200);
      if (!VERBATIM_KINDS.has(kind) || !label) return [];
      return [{ ...range, kind, label }];
    })
    .slice(0, 16);
  const rawMode = cleanLine(parsed.content_mode, 30) as LlmPlan["contentMode"];
  if (isCanonicalV4 && !CONTENT_MODES.has(rawMode)) {
    throw new Error("保存計画のcontent_modeが不正です");
  }
  const contentMode = CONTENT_MODES.has(rawMode) ? rawMode : "summary";
  // v4 is canonical. Older plans are read-only compatibility input and are
  // normalized immediately so the rest of the local writer never branches on
  // summary/details versus knowledge_items.
  const knowledgeItems = stringList(
    isCanonicalV4
      ? parsed.knowledge_items
      : [cleanLine(parsed.summary, SUMMARY_LIMIT), ...stringList(parsed.details, KNOWLEDGE_ITEM_LIMIT, SUMMARY_LIMIT)],
    KNOWLEDGE_ITEM_LIMIT,
    SUMMARY_LIMIT,
  );
  return {
    targetId: cleanLine(parsed.target_id, 200),
    matched: parsed.matched !== false,
    topic: cleanLine(parsed.topic, 200),
    subject: cleanLine(parsed.subject, TITLE_LIMIT),
    titleDetail: cleanLine(parsed.title_detail, TITLE_LIMIT),
    titleEvidence,
    contentMode,
    knowledgeItems,
    // Keep the old names as a local compatibility view.  New tree generation
    // uses knowledgeItems directly, while legacy callers/tests can still read
    // summary/details without changing the wire contract.
    summary: knowledgeItems[0] ?? "",
    details: knowledgeItems.slice(1),
    unconfirmed: stringList(parsed.unconfirmed, 10, 300),
    shortLiterals,
    verbatimRanges,
    excerpts,
    legacySchema,
  };
}

function identity(value: unknown): string {
  return String(value ?? "")
    .toLocaleLowerCase()
    .replace(/[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+/g, "");
}

function rangeContent(source: string, range: SourceRange, strict = true): string | null {
  if (canonicalSourceId(range.sourceId) !== "source:0") {
    if (strict) throw new Error("原文範囲が存在しないsourceを参照しています");
    return null;
  }
  const lines = normalizeNewlines(source).split("\n");
  if (range.startLine < 1 || range.endLine > lines.length) {
    if (strict) throw new Error("原文範囲がsourceの行数を超えています");
    return null;
  }
  return lines.slice(range.startLine - 1, range.endLine).join("\n");
}

function hasGroundingOverlap(value: string, evidence: string): boolean {
  const left = identity(value);
  const right = identity(evidence);
  if (!left || !right) return false;
  if (left.includes(right) || right.includes(left)) return true;
  const latinTokens = value.toLocaleLowerCase().match(/[a-z0-9][a-z0-9._+-]{2,}/g) ?? [];
  return latinTokens.some((token) => evidence.toLocaleLowerCase().includes(token));
}

function titleExactPhraseOverlap(value: string, evidence: string): boolean {
  // hasGroundingOverlap is intentionally broad for legacy context checks and
  // must not make a title succeed on one shared Latin token (Fooを削除する
  // against a source that only explains Foo).  Keep whitespace normalization,
  // strict identifier boundaries, and complete phrase containment only.
  const rawCandidate = normalizeNewlines(value).trim();
  const candidate = rawCandidate.replace(/\s+/g, "");
  const source = normalizeNewlines(evidence).replace(/\s+/g, "");
  if (!candidate || !source) return false;
  // Include short identifiers too (SD must not match SDXL).  The regular
  // knowledge-token helper intentionally ignores tiny fragments, but a title
  // exact path must apply strict identifier boundaries to all Latin runs.
  const identifiers = rawCandidate.match(/[A-Za-z][A-Za-z0-9._-]*/g) ?? [];
  if (!identifiers.every((token) => strictEntityInText(token, evidence))) return false;
  for (const match of rawCandidate.matchAll(/\d+(?:\.\d+)+|\d+/g)) {
    const number = match[0];
    const escaped = number.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (!new RegExp(`(?<![0-9.])${escaped}(?![0-9.])`).test(evidence)) return false;
  }
  return source.includes(candidate);
}

function latinTokens(text: string): string[] {
  return text.match(/[A-Za-z][A-Za-z0-9._-]{2,}/g) ?? [];
}

function cjkGroundingGrams(text: string): Set<string> {
  const grams = new Set<string>();
  for (const segment of text.match(/[\u3040-\u30ff\u3400-\u9fff]+/g) ?? []) {
    for (let size = 2; size <= Math.min(4, segment.length); size += 1) {
      for (let index = 0; index <= segment.length - size; index += 1) {
        grams.add(segment.slice(index, index + size));
      }
    }
  }
  return grams;
}

const VERSION_TOKEN_PATTERN = /v(\d+(?:\.\d+)*)/gi;
const VERSION_ATOM_PATTERN = /v(\d+(?:\.\d+)*)|(?:version|ver\.?)\s*(\d+(?:\.\d+)*)/gi;
const BARE_VERSION_PATTERN = /([A-Za-z][A-Za-z0-9._-]+)\s+(\d+\.\d+(?:\.\d+)*)/g;
const NEGATION_SCOPE_PATTERN = /(?:ない|ません|ぬ|非対応|未対応|不可|できない|できません|cannot|can't|does\s+not|do\s+not|unsupported|not\s+supported)/i;
const EVIDENCE_WINDOW_SPLIT_PATTERN = /(?<=[。！？!?])\s*|\n+/;
const MEASUREMENT_TOKEN_PATTERN = /(\d+(?:\.\d+)?)\s*(秒|fps|フレーム|ms|枚|回|%|MB|GB|Hz|KB)/gi;

function topicContextLabel(subject: string, topic: string, detail: string): string {
  return [subject, topic, detail].map((part) => cleanLine(part, TITLE_LIMIT)).filter(Boolean).join(" ");
}

function topicContextEntities(topicContext: string): string[] {
  const entities = [...latinTokens(topicContext)];
  for (const match of topicContext.matchAll(/[\u3040-\u9fff]{2,}/g)) {
    const fragment = match[0];
    if (["漫画", "ワークフロー", "手順", "方法", "概要", "作例"].includes(fragment)) continue;
    entities.push(fragment);
  }
  return [...new Set(entities)];
}

function claimHasExplicitSubject(claim: string): boolean {
  const stripped = claim.trim();
  if (/^[A-Za-z][A-Za-z0-9._-]+(?:は|が)/.test(stripped)) return true;
  return /^[\u3040-\u9fff]{2,}(?:は|が)/.test(stripped);
}

function topicAnchorInWindow(entities: string[], window: string): boolean {
  if (!entities.length || !window) return false;
  return entities.some((entity) => strictEntityInText(entity, window));
}

function sourceEvidenceWindows(source: string): string[] {
  if (!source) return [];
  const parts = source.trim().split(EVIDENCE_WINDOW_SPLIT_PATTERN).map((part) => part.trim()).filter(Boolean);
  if (!parts.length) return [source.trim()];
  const windows = [...new Set(parts)];
  for (let index = 0; index < parts.length - 1; index += 1) {
    if (parts[index].length < 120) windows.push(`${parts[index]}${parts[index + 1]}`);
  }
  return [...new Set(windows)];
}

function claimPolarityPositive(text: string): boolean {
  const normalized = text.replace(/(?:たくない|したくない|ほしくない|なくて)/g, "");
  return !NEGATION_SCOPE_PATTERN.test(normalized);
}

function polarityConsistent(claim: string, window: string): boolean {
  if (!claim || !window) return false;
  return claimPolarityPositive(claimPredicateText(claim)) === claimPolarityPositive(window);
}

const CJK_SUBJECT_BODY_PATTERN = /[\u3040-\u9fff]{2,}(?:の[\u3040-\u9fff]{2,})*/;
const CJK_SUBJECT_PARTICLES = ["では", "でも", "は", "が"] as const;

function parseLatinSubjectWithParticle(
  text: string,
  start = 0,
): [string, number] | null {
  const sub = text.slice(start);
  for (const particle of CJK_SUBJECT_PARTICLES) {
    const match = sub.match(
      new RegExp(`^([A-Za-z][A-Za-z0-9._-]+)${particle}`),
    );
    if (match?.[1]) return [match[1], start + match[0].length];
  }
  return null;
}

function parseCjkSubjectWithParticle(
  text: string,
  start = 0,
): [string, number] | null {
  const sub = text.slice(start);
  for (const particle of CJK_SUBJECT_PARTICLES) {
    const match = sub.match(
      new RegExp(`^(${CJK_SUBJECT_BODY_PATTERN.source})${particle}`),
    );
    if (match?.[1]) return [match[1], start + match[0].length];
  }
  return null;
}

function parseBetsuNoSubjectWithParticle(
  text: string,
  start = 0,
): [string, number] | null {
  const sub = text.slice(start);
  for (const particle of CJK_SUBJECT_PARTICLES) {
    const match = sub.match(
      new RegExp(`^(別の[A-Za-z\\u3040-\\u9fff0-9._-]+)${particle}`),
    );
    if (match?.[1]) return [match[1], start + match[0].length];
  }
  return null;
}

function claimPredicateText(claim: string): string {
  let predicate = claim;
  for (const token of latinTokens(claim)) {
    predicate = predicate.replace(new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), "");
  }
  return predicate;
}

function claimPredicateBody(claim: string): string {
  const stripped = claim.trim();
  for (const parser of [
    parseLatinSubjectWithParticle,
    parseCjkSubjectWithParticle,
    parseBetsuNoSubjectWithParticle,
  ]) {
    const parsed = parser(stripped, 0);
    if (parsed) {
      return stripped.slice(parsed[1]).replace(/^[、, \t]+/, "");
    }
  }
  return stripped;
}

function extractVersionNumbers(text: string): string[] {
  const versions: string[] = [];
  for (const match of text.matchAll(VERSION_ATOM_PATTERN)) {
    const version = match[1] || match[2];
    if (version) versions.push(version);
  }
  for (const match of text.matchAll(BARE_VERSION_PATTERN)) {
    if (match[2]) versions.push(match[2]);
  }
  return [...new Set(versions)];
}

function extractMeasurementTokens(text: string): Array<[string, string]> {
  return [...text.matchAll(MEASUREMENT_TOKEN_PATTERN)].map((match) => [
    match[1],
    match[2].toLocaleLowerCase(),
  ]);
}

function factualAtomsConsistent(claim: string, source: string): boolean {
  if (!claim || !source) return false;
  const claimVersions = extractVersionNumbers(claim);
  if (claimVersions.length) {
    const sourceVersions = extractVersionNumbers(source);
    if (!sourceVersions.length) return false;
    if (claimVersions.some((version) => !sourceVersions.includes(version))) return false;
  }
  for (const [number, unit] of extractMeasurementTokens(claim)) {
    const pattern = new RegExp(`${number}\\s*${unit}`, "i");
    if (!pattern.test(source)) return false;
  }
  return true;
}

const OPERATION_TOKEN_PATTERN = /(?:吹き出し|番号|整え|白塗り|文字入れ|文字|生成|認識|手順|方法|設定|配置|調整)/g;

function operationTokenInSource(token: string, source: string): boolean {
  if (source.includes(token)) return true;
  const alternate = token
    .replace(/入れ/g, "いれ")
    .replace(/付け/g, "づけ")
    .replace(/付ける/g, "づけ");
  return alternate !== token && source.includes(alternate);
}

function operationTokens(text: string): string[] {
  return [...new Set(text.match(OPERATION_TOKEN_PATTERN) ?? [])];
}

const CROSS_SCRIPT_LATIN_ALIASES: Record<string, readonly string[]> = {
  illustrious: ["イラストリアス"],
  gemini: ["geimini", "ジェミニ"],
};

const CJK_PREDICATE_ENTITY_DE_SUFFIXES = [
  "モデル",
  "ツール",
  "サービス",
  "方式",
  "エンジン",
  "アプリ",
  "プラットフォーム",
] as const;

const NON_ENTITY_DE_FRAGMENTS = new Set([
  "高速",
  "高速処理",
  "この",
  "その",
  "ここ",
  "ローカル",
  "番号",
  "文字",
  "画像",
  "漫画",
  "手順",
  "方法",
]);

function cjkSuffixEntityPhrases(text: string): string[] {
  const suffixPattern = CJK_PREDICATE_ENTITY_DE_SUFFIXES.join("|");
  const pattern = new RegExp(
    `([\\u3040-\\u9fff]{1,}(?:の[\\u3040-\\u9fff]{2,})*(?:${suffixPattern}))`,
    "g",
  );
  const phrases: string[] = [];
  for (const match of text.matchAll(pattern)) {
    const phrase = match[1];
    if (!phrase || NON_ENTITY_DE_FRAGMENTS.has(phrase)) continue;
    phrases.push(phrase);
  }
  return [...new Set(phrases)];
}

function cjkPredicateEntityAnchors(claim: string): string[] {
  const excluded = new Set(primaryExplicitSubjects(claim));
  const parsed = parseCjkSubjectWithParticle(claim.trim(), 0);
  if (parsed?.[0]) excluded.add(parsed[0]);
  const predicateBody = claimPredicateBody(claim);
  const anchors: string[] = [];
  for (const phrase of cjkSuffixEntityPhrases(predicateBody)) {
    if ([...excluded].some((subject) => subjectsEquivalent(phrase, subject))) continue;
    anchors.push(phrase);
  }
  return [...new Set(anchors)];
}

function latinTokenAliasGrounded(token: string, source: string): boolean {
  const aliases = CROSS_SCRIPT_LATIN_ALIASES[token.toLocaleLowerCase()] ?? [];
  for (const alias of aliases) {
    if (/[A-Za-z]/.test(alias)) {
      if (strictEntityInText(alias, source)) return true;
    } else if (source.includes(alias) || source.toLocaleLowerCase().includes(alias.toLocaleLowerCase())) {
      return true;
    }
  }
  return false;
}

function latinTokenFactuallyGrounded(token: string, source: string): boolean {
  if (strictEntityInText(token, source)) return true;
  return latinTokenAliasGrounded(token, source);
}

function latinTokenCollisionGrounded(token: string, source: string): boolean {
  return latinTokenGrounded(token, source) && !strictEntityInText(token, source);
}

function predicateGrounded(claim: string, source: string): boolean {
  if (!claim || !source) return false;
  for (const token of latinTokens(claim)) {
    if (latinTokenCollisionGrounded(token, source)) return false;
    if (!latinTokenFactuallyGrounded(token, source)) return false;
  }
  for (const anchor of cjkPredicateEntityAnchors(claim)) {
    if (!strictEntityInText(anchor, source)) return false;
  }
  const normalizedSource = source.replace(/\s+/g, "");
  const predicate = claimPredicateText(claim);
  const residual = predicate.replace(/[\s、。.!！:：についてでははがをの]+/g, "");
  if (residual.length >= 4 && normalizedSource.includes(residual)) return true;
  const ops = operationTokens(claim);
  if (ops.length) {
    const matchedOps = ops.filter((token) => operationTokenInSource(token, source)).length;
    if (matchedOps === ops.length) return true;
    if (matchedOps >= 2 && matchedOps / ops.length >= 0.67) return true;
  }
  const grams = [...cjkGroundingGrams(predicate)].filter((gram) => gram.length >= 2);
  if (!grams.length) {
    const latin = latinTokens(claim);
    return latin.length > 0 && latin.every((token) => latinTokenFactuallyGrounded(token, source));
  }
  const matched = grams.filter((gram) => source.includes(gram)).length;
  const total = grams.length;
  const latin = latinTokens(claim);
  if (latin.length > 0 && latin.every((token) => latinTokenFactuallyGrounded(token, source))) {
    if (matched >= 3 && matched / total >= 0.33) return true;
  }
  if (total >= 3 && matched / total >= 0.5) return true;
  if (matched >= 2 && matched / total >= 0.4) return true;
  return false;
}

function claimMeaningfulOverlap(claim: string, source: string): boolean {
  if (!claim || !source) return false;
  if (!factualAtomsConsistent(claim, source)) return false;
  return predicateGrounded(claim, source);
}

function strictEntityInText(entity: string, text: string): boolean {
  if (!entity || !text) return false;
  if (/[A-Za-z]/.test(entity)) {
    const escaped = entity.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(`(?<![A-Za-z0-9._-])${escaped}(?![A-Za-z0-9._-])`, "i");
    return pattern.test(text);
  }
  if (text.includes(entity)) return true;
  return text.toLocaleLowerCase().includes(entity.toLocaleLowerCase());
}

/**
 * Topic prefilter専用のidentifier equivalence。
 *
 * strictEntityInText() は本文中の既存identifierを厳密に照合する防御で
 * あり、ここでは決して緩めない。タイトルのtopic anchorをsource bodyへ
 * 事前に対応付ける場合だけ、空白・ハイフン・アンダースコアの表記差を
 * 吸収する。ドットは残すため v1.1 と v11 は別identifierのままになる。
 */
function topicAnchorPattern(anchor: string): RegExp | null {
  const value = String(anchor ?? "").trim();
  if (!value) {
    return null;
  }
  const parts = value.match(/[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*/g) ?? [];
  if (!parts.length) return null;
  const remainder = value.replace(/[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*/g, "");
  if (remainder && !/^[\s_-]+$/.test(remainder)) return null;
  const escaped = parts.map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return new RegExp(
    `(?<![A-Za-z0-9._-])${escaped.join("[\\s_-]+")}(?![A-Za-z0-9._-])`,
    "i",
  );
}

function topicAnchorInText(anchor: string, text: string): boolean {
  const pattern = topicAnchorPattern(anchor);
  return pattern ? pattern.test(String(text ?? "")) : false;
}

function latinTokenGrounded(token: string, source: string): boolean {
  const folded = source.toLocaleLowerCase();
  const normalized = token.toLocaleLowerCase();
  if (folded.includes(normalized)) return true;
  if (normalized.length >= 4) {
    for (let index = 0; index < normalized.length; index += 1) {
      const variant = normalized.slice(0, index) + normalized.slice(index + 1);
      if (folded.includes(variant)) return true;
    }
  }
  return false;
}

function explicitLatinSubjects(claim: string): Set<string> {
  const subjects = new Set<string>();
  for (const particle of CJK_SUBJECT_PARTICLES) {
    for (const match of claim.matchAll(
      new RegExp(`([A-Za-z][A-Za-z0-9._-]+)${particle}`, "g"),
    )) {
      if (match[1]) subjects.add(match[1].toLocaleLowerCase());
    }
  }
  return subjects;
}

function explicitSubjects(text: string): string[] {
  const subjects: string[] = [];
  for (const particle of CJK_SUBJECT_PARTICLES) {
    for (const match of text.matchAll(
      new RegExp(`([A-Za-z][A-Za-z0-9._-]+)${particle}`, "g"),
    )) {
      if (match[1]) subjects.push(match[1]);
    }
  }
  const stripped = text.trim();
  for (const parser of [parseCjkSubjectWithParticle, parseBetsuNoSubjectWithParticle]) {
    const parsed = parser(stripped, 0);
    if (parsed?.[0]) subjects.push(parsed[0]);
  }
  for (const match of text.matchAll(/(?:^|[。．\n])/g)) {
    const parsed = parseCjkSubjectWithParticle(text, match.index! + match[0].length);
    if (parsed?.[0]) subjects.push(parsed[0]);
  }
  for (const particle of CJK_SUBJECT_PARTICLES) {
    for (const match of text.matchAll(
      new RegExp(`(別の[A-Za-z\\u3040-\\u9fff0-9._-]+)${particle}`, "g"),
    )) {
      if (match[1]) subjects.push(match[1]);
    }
  }
  for (const match of text.matchAll(/(?:^|[。．\n])([A-Za-z][A-Za-z0-9._-]+)で/g)) {
    if (match[1]) subjects.push(match[1]);
  }
  const deHead = stripped.match(/^([A-Za-z][A-Za-z0-9._-]+)で/);
  if (deHead?.[1]) subjects.push(deHead[1]);
  return [...new Set(subjects)];
}

function primaryExplicitSubjects(text: string): string[] {
  const stripped = text.trim();
  if (!stripped) return [];
  for (const parser of [
    parseLatinSubjectWithParticle,
    parseCjkSubjectWithParticle,
    parseBetsuNoSubjectWithParticle,
  ]) {
    const parsed = parser(stripped, 0);
    if (parsed?.[0]) return [parsed[0]];
  }
  return [];
}

const NON_COMPETING_SUBJECTS = new Set([
  "投稿者",
  "著者",
  "記事",
  "本文",
  "内容",
  "ページ",
  "投稿",
  "投稿文",
  "投稿例",
  "個人",
  "ユーザー",
  "読者",
  "レビュアー",
  "作者",
]);

function entitySubjects(text: string): string[] {
  return explicitSubjects(text).filter(
    (subject) => !NON_COMPETING_SUBJECTS.has(subject) && !subject.startsWith("この"),
  );
}

function subjectsEquivalent(left: string, right: string): boolean {
  if (left === right) return true;
  return left.toLocaleLowerCase() === right.toLocaleLowerCase();
}

function subjectInWindow(subject: string, window: string): boolean {
  return strictEntityInText(subject, window);
}

function claimIntroducesUnsupportedFacts(claim: string, source: string): boolean {
  if (!claim || !source) return true;
  const sourceFolded = source.toLocaleLowerCase();
  const explicitSubjects = explicitLatinSubjects(claim);
  for (const token of latinTokens(claim)) {
    if (latinTokenFactuallyGrounded(token, source)) continue;
    if (latinTokenCollisionGrounded(token, source)) return true;
    if (explicitSubjects.has(token.toLocaleLowerCase())) return true;
    return true;
  }
  for (const anchor of cjkPredicateEntityAnchors(claim)) {
    if (!strictEntityInText(anchor, source)) return true;
  }
  for (const match of claim.matchAll(UNSUPPORTED_CLAIM_MATERIAL_PATTERN)) {
    const fragment = match[0];
    if (!source.includes(fragment) && !sourceFolded.includes(fragment.toLocaleLowerCase())) {
      return true;
    }
  }
  for (const gram of cjkGroundingGrams(claim)) {
    if (gram.length < 3 || source.includes(gram)) continue;
    if (/(?:自動認識|自動検出|有料|無料|品質が|性能|対応OS|機能がある|機能を持つ)/.test(gram)) {
      return true;
    }
  }
  return false;
}

function windowConflictsWithTopic(window: string, topicEntities: string[]): boolean {
  if (!window || !topicEntities.length) return false;
  if (window.includes("別の")) return true;
  for (const subject of entitySubjects(window)) {
    if (!topicEntities.some((entity) => subjectsEquivalent(entity, subject))) {
      return true;
    }
  }
  return false;
}

function windowConflictsWithClaimSubjects(
  claim: string,
  window: string,
  topicEntities: string[] = [],
): boolean {
  const claimSubjects = primaryExplicitSubjects(claim);
  if (claimSubjects.length) {
    if (!claimSubjects.some((subject) => subjectInWindow(subject, window))) {
      return true;
    }
    for (const subject of entitySubjects(window)) {
      if (claimSubjects.some((claimSubject) => subjectsEquivalent(claimSubject, subject))) {
        continue;
      }
      return true;
    }
    return false;
  }
  if (topicEntities.length) {
    return windowConflictsWithTopic(window, topicEntities);
  }
  return false;
}

function singleSourceSupportsKnowledgeClaim(
  claim: string,
  source: string,
  topicEntities: string[] = [],
): boolean {
  if (!claim || !source) return false;
  for (const window of sourceEvidenceWindows(source)) {
    if (windowConflictsWithClaimSubjects(claim, window, topicEntities)) {
      continue;
    }
    if (!factualAtomsConsistent(claim, window)) continue;
    if (!polarityConsistent(claim, window)) continue;
    if (!predicateGrounded(claim, window)) continue;
    if (claimIntroducesUnsupportedFacts(claim, window)) continue;
    return true;
  }
  return false;
}

export function knowledgeSupportedBySources(
  claim: string,
  sources: readonly string[],
  topicContext = "",
  topicAnchor = "",
): boolean {
  const bodies = sources.map((item) => normalizeNewlines(item)).filter(Boolean);
  if (!bodies.length) return true;
  const topicEntities = topicContextEntities(topicContext);
  const anchorPattern = topicAnchorPattern(topicAnchor);
  const anchorMatches = (body: string): boolean => topicAnchorInText(topicAnchor, body);
  let candidateBodies = bodies;
  if (topicEntities.length) {
    const topicBodies = bodies.filter((body) => topicEntities.some(
      (entity) => strictEntityInText(entity, body),
    ) || (Boolean(anchorPattern) && anchorMatches(body)));
    if (!topicBodies.length) return false;
    candidateBodies = topicBodies;
  } else if (anchorPattern) {
    const topicBodies = bodies.filter(anchorMatches);
    if (!topicBodies.length) return false;
    candidateBodies = topicBodies;
  }
  return candidateBodies.some(
    (body) => singleSourceSupportsKnowledgeClaim(claim, body, topicEntities),
  );
}

function hasKnowledgeSourceOverlap(text: string, sourceText: string): boolean {
  return claimMeaningfulOverlap(text, sourceText);
}

function isSubstantiveExtractedClaim(claim: string): boolean {
  if (GENERIC_KNOWLEDGE_ITEM_PATTERN.test(claim)) return false;
  if (/(?:手順|方法|使い方|設定|番号|白塗り|文字入れ|比較|結果|条件|必要)/.test(claim)) {
    return true;
  }
  if (latinTokens(claim).length > 0) return true;
  const residual = claim.replace(/[\s、。.!！:：についてでははがをの]+/g, "");
  return residual.length >= 8;
}

function extractObserverWrappedClaim(value: string): string {
  const match = value.trim().match(OBSERVER_WRAPPED_CLAIM_PATTERN);
  if (!match?.[1]) return "";
  const claim = cleanLine(match[1], SUMMARY_LIMIT);
  if (!isSubstantiveExtractedClaim(claim)) return "";
  const residual = claim.replace(/[\s、。.!！:：についてでははがをの]+/g, "");
  if (residual.length < 4 || !CONCRETE_KNOWLEDGE_TOKEN_PATTERN.test(residual)) return "";
  return claim;
}

export function normalizeKnowledgeItemText(
  item: string,
  sourceBodies: string | readonly string[],
  topicContext = "",
  topicAnchor = "",
): string {
  const sources = typeof sourceBodies === "string"
    ? [sourceBodies]
    : [...sourceBodies];
  const joinedSource = sources.filter(Boolean).join("\n");
  const cleaned = cleanLine(item, SUMMARY_LIMIT);
  if (!cleaned || GENERIC_KNOWLEDGE_ITEM_PATTERN.test(cleaned)) return "";
  const hadPosterPrefix = /^投稿者(?:は|が)/.test(cleaned);
  let candidate = cleaned;
  if (OBSERVER_WRAPPED_CLAIM_PATTERN.test(cleaned.trim())) {
    const extracted = extractObserverWrappedClaim(cleaned);
    if (extracted) {
      candidate = extracted;
    } else {
      return "";
    }
  } else {
    for (const pattern of [
      META_SUMMARY_PATTERN,
      META_SUMMARY_CONTEXT_PATTERN,
      OBSERVER_ONLY_ATTRIBUTION_PATTERN,
    ]) {
      if (pattern.test(cleaned.trim())) {
        return "";
      }
    }
    const stripped = candidate.replace(SOURCE_OBSERVER_PREFIX_PATTERN, "").replace(/^[、,]\s*/, "");
    if (stripped && stripped !== candidate) {
      const residual = stripped.replace(/[\s、。.!！:：についてでははがをの]+/g, "");
      if (residual.length < 4 || !CONCRETE_KNOWLEDGE_TOKEN_PATTERN.test(residual)) return "";
      candidate = stripped.replace(OBSERVER_SUFFIX_PATTERN_GLOBAL, "");
    } else if (SOURCE_OBSERVER_PREFIX_PATTERN.test(candidate)) {
      return "";
    }
  }
  candidate = preserveMeasurementScope(candidate, joinedSource, hadPosterPrefix);
  if (!candidate) return "";
  if (sources.length && !knowledgeSupportedBySources(candidate, sources, topicContext, topicAnchor)) return "";
  return candidate;
}

function preserveMeasurementScope(
  text: string,
  sourceText: string,
  hadPosterPrefix: boolean,
): string {
  if (!text || !hadPosterPrefix) return text;
  if (text.includes("個人実測") || text.includes("実測")) return text;
  if (!PERSONAL_MEASUREMENT_SOURCE_PATTERN.test(sourceText)) return text;
  if (!PERFORMANCE_MEASUREMENT_PATTERN.test(text)) return text;
  const envMatch = text.match(/^((?:RTX\d+|GPU)[^。]*?環境?)で/i);
  if (envMatch) {
    return text.replace(envMatch[0], `${envMatch[1]}での個人実測では`);
  }
  if (ENVIRONMENT_SCOPE_PATTERN.test(text)) {
    return `${text.replace(/[。.!！]+$/, "")}（個人実測）。`;
  }
  return text;
}

function topicLineText(value: string): string {
  return String(value ?? "")
    .replace(/[「」『』【】]/g, "")
    .replace(/[。.!！?？:：]+$/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function isTopicMetadataLine(value: string): boolean {
  const text = topicLineText(value);
  return Boolean(text) && (
    TOPIC_METADATA_PREFIX_PATTERN.test(text)
    || TOPIC_AUTHOR_ATTRIBUTION_PATTERN.test(text)
    || /^(?:by|投稿者|作者|著者)\s*[:：=]/i.test(text)
  );
}

function isTopicGenericHeading(value: string): boolean {
  return TOPIC_GENERIC_HEADING_PATTERN.test(topicLineText(value));
}

function titleCandidateOccursInLine(candidate: string, line: string): boolean {
  const value = cleanLine(candidate, TITLE_LIMIT);
  const text = topicLineText(line);
  if (!value || !text) return false;
  // Topic-anchor equivalence is used only to identify a model/metadata line;
  // it does not alter strict knowledge grounding.
  if (topicAnchorInText(value, text)) return true;
  const identifiers = value.match(/[A-Za-z][A-Za-z0-9._-]*/g) ?? [];
  if (identifiers.length && identifiers.every((token) => strictEntityInText(token, text))) {
    return true;
  }
  return strictEntityInText(value, text) || hasGroundingOverlap(value, text);
}

function titleGroundedInRange(
  value: string,
  evidence: string,
  windowChecked = false,
): boolean {
  const candidate = cleanLine(value, TITLE_LIMIT);
  const text = normalizeNewlines(evidence).trim();
  if (!candidate || !text) return false;
  if (!windowChecked) {
    const windows = sourceEvidenceWindows(text);
    if (windows.length > 1) {
      return windows.some((window) => titleGroundedInRange(candidate, window, true));
    }
  }
  if (windowConflictsWithClaimSubjects(candidate, text, [])) return false;

  // Reuse the same fact/polarity/unsupported-material gates as knowledge
  // normalization.  Title paraphrase is narrower than free summarization and
  // must not become a second way to introduce a number, version, capability,
  // or polarity mutation.
  if (
    !factualAtomsConsistent(candidate, text)
    || !polarityConsistent(candidate, text)
    || claimIntroducesUnsupportedFacts(candidate, text)
  ) return false;

  // Every identifier and number/version in a title must be present in the
  // single evidence range.  In particular v1.1 must not be accepted from
  // v1.10, and an invented product token cannot ride on a Japanese overlap.
  const identifiers = candidate.match(/[A-Za-z][A-Za-z0-9._-]*/g) ?? [];
  for (const token of identifiers) {
    if (!strictEntityInText(token, text)) return false;
  }
  for (const match of candidate.matchAll(/\d+(?:\.\d+)+|\d+/g)) {
    const number = match[0];
    const escaped = number.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (!new RegExp(`(?<![0-9.])${escaped}(?![0-9.])`).test(text)) return false;
  }
  if (TITLE_UNSUPPORTED_MATERIAL_PATTERN.test(candidate)) {
    const unsupported = candidate.match(/(?:万能|高性能|最速|最高|自動認識|自動検出|対応している|対応する|機能がある|機能を持つ|有料|無料|おすすめ|能力|性能)/gi) ?? [];
    for (const phrase of unsupported) {
      if (!text.toLocaleLowerCase().includes(phrase.toLocaleLowerCase())) return false;
    }
  }

  // Require a major predicate/structure marker in the same evidence range
  // before accepting exact overlap.  hasGroundingOverlap intentionally allows
  // a small shared entity token, so accepting it first would let a title such
  // as "Fooは猫である。" pass against a source that only explains Foo.
  const exactOverlap = titleExactPhraseOverlap(candidate, text);
  if (exactOverlap) return true;
  const majorPredicateGrounded = TITLE_PREDICATE_GROUPS.some((group) =>
    group.some((token) => candidate.includes(token))
    && group.some((token) => text.includes(token)),
  ) || [
    "全体",
    "設計",
    "構図",
    "姿勢",
    "視点",
    "ワークフロー",
    "手法",
    "方法",
    "設定",
    "目的",
    "手順",
  ].some((token) => candidate.includes(token) && text.includes(token));
  if (!majorPredicateGrounded) {
    return false;
  }

  const grams = [...cjkGroundingGrams(candidate)];
  if (!grams.some((gram) => text.includes(gram))) {
    return false;
  }
  if (grams.length) {
    const matched = grams.filter((gram) => text.includes(gram)).length;
    if (matched < 3 || matched / grams.length < 0.25) return false;
  }
  return true;
}

function metadataOnlyTitleSubject(
  candidate: string,
  source: string,
): boolean {
  const value = topicLineText(candidate);
  if (!value || isTopicMetadataLine(value) || isTopicGenericHeading(value)) return true;
  const lines = normalizeNewlines(source).split("\n");
  let metadataOccurrence = false;
  let substantiveOccurrence = false;
  for (const line of lines) {
    if (!titleCandidateOccursInLine(value, line)) continue;
    if (isTopicMetadataLine(line) || isTopicGenericHeading(line) || isResearchControlWrapper(line)) {
      metadataOccurrence = true;
    } else {
      substantiveOccurrence = true;
    }
  }
  return metadataOccurrence && !substantiveOccurrence;
}

function fallbackSubjectScore(line: string, index: number): number {
  const text = topicLineText(line);
  let score = index === 0 ? 2 : 0;
  if (/(?:全体として|構図|設計)/i.test(text)) score += 6;
  else if (TOPIC_CENTER_MARKER_PATTERN.test(text)) score += 2;
  if (text.length >= 8) score += Math.min(text.length, 120) / 40;
  if (/[。.!！?？]$/.test(text)) score += 1;
  if ((text.match(/[A-Za-z][A-Za-z0-9._-]*/g) ?? []).length) score += 0.5;
  return score;
}

function fallbackSubject(source: string, urls: string[]): string {
  const candidates = normalizeNewlines(source)
    .split("\n")
    .map((rawLine, index) => ({ line: cleanLine(rawLine, 120), index }))
    .filter(({ line }) => (
      Boolean(line)
      && !/^https?:\/\/\S+$/i.test(line)
      && !isTopicMetadataLine(line)
      && !isTopicGenericHeading(line)
      && !isResearchControlWrapper(line)
      && !/^["'「『].*["'」』]$/.test(line)
    ));
  if (candidates.length) {
    candidates.sort((left, right) => {
      const score = fallbackSubjectScore(right.line, right.index) - fallbackSubjectScore(left.line, left.index);
      return score || left.index - right.index;
    });
    return candidates[0].line;
  }
  const url = urls[0];
  if (url) {
    try {
      const parsed = new URL(url);
      const slug = decodeURIComponent(parsed.pathname.replace(/\/$/, "").split("/").pop() || "");
      return cleanLine(slug || parsed.hostname, 120) || "取り込みメモ";
    } catch {
      return cleanLine(url, 120) || "取り込みメモ";
    }
  }
  return "取り込みメモ";
}

export function composeLocalClipTitle(subject: string, detail: string): string {
  const cleanSubject = cleanLine(subject, TITLE_LIMIT) || "取り込みメモ";
  let cleanDetail = cleanLine(detail, TITLE_LIMIT);
  const subjectIdentity = identity(cleanSubject);
  const detailIdentity = identity(cleanDetail);
  if (!cleanDetail || (detailIdentity && subjectIdentity.includes(detailIdentity))) {
    return cleanSubject.slice(0, TITLE_LIMIT);
  }
  if (subjectIdentity && detailIdentity.includes(subjectIdentity)) {
    cleanDetail = cleanDetail
      .replace(new RegExp(cleanSubject.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"), "")
      .replace(/^[\s\-:：、。]+|[\s\-:：、。]+$/g, "");
  }
  if (!cleanDetail) return cleanSubject.slice(0, TITLE_LIMIT);
  const available = TITLE_LIMIT - cleanSubject.length - 3;
  return available > 0
    ? `${cleanSubject} - ${cleanDetail.slice(0, available)}`
    : cleanSubject.slice(0, TITLE_LIMIT);
}

function normalizedPlanTitle(
  plan: LlmPlan,
  source: string,
  urls: string[],
): { subject: string; detail: string; topic: string; evidence: string[] } {
  if (plan.legacySchema) {
    const topic = cleanLine(plan.topic, TITLE_LIMIT) || fallbackSubject(source, urls);
    return { subject: topic, detail: "", topic, evidence: [] };
  }
  const evidence = plan.titleEvidence
    .map((range) => rangeContent(source, range, false))
    .filter((item): item is string => item !== null);
  const singleEvidence = evidence.length === 1 ? evidence : [];
  // Validate each title field against one declared range instead of joining
  // unrelated ranges.  This keeps title_evidence from becoming a cross-line
  // chimera channel while still allowing a natural paraphrase of the central
  // words/predicate inside that range.
  const subjectCandidate = cleanLine(plan.subject, TITLE_LIMIT);
  const detailCandidate = cleanLine(plan.titleDetail, TITLE_LIMIT);
  const subjectGrounded = Boolean(subjectCandidate)
    && singleEvidence.some((item) => titleGroundedInRange(subjectCandidate, item));
  const detailGrounded = Boolean(detailCandidate)
    && singleEvidence.some((item) => titleGroundedInRange(detailCandidate, item));
  const subjectUsable = subjectGrounded
    && !metadataOnlyTitleSubject(subjectCandidate, source)
    && !isTopicGenericHeading(subjectCandidate);
  const detailUsable = detailGrounded
    && !metadataOnlyTitleSubject(detailCandidate, source)
    && !isTopicGenericHeading(detailCandidate);

  // A model/environment/author heading is metadata, not the topic.  If the
  // planner supplied a grounded central detail, promote it to subject;
  // otherwise choose a substantive center line from the input.
  const subject = subjectUsable
    ? subjectCandidate
    : detailUsable
      ? detailCandidate
      : fallbackSubject(source, urls);
  const detail = subjectUsable && detailUsable && detailCandidate !== subjectCandidate
    ? detailCandidate
    : "";
  return {
    subject,
    detail,
    topic: composeLocalClipTitle(subject, detail),
    evidence: singleEvidence,
  };
}

function deduplicateSummary(
  summary: string,
  topic: string,
  detail: string,
  evidence: string[],
  sourceText = "",
): string {
  const normalized = normalizeKnowledgeItemText(summary, sourceText);
  if (!normalized) return "";
  const topicIdentity = identity(topic);
  if (topicIdentity && (identity(normalized) === topicIdentity || topicIdentity.includes(identity(normalized)))) {
    return "";
  }
  const comparisons = detail ? [detail, ...evidence] : [];
  for (const comparison of comparisons) {
    const other = identity(comparison);
    if (other && (identity(normalized).includes(other) || other.includes(identity(normalized)))) return "";
  }
  return normalized;
}

function deduplicateKnowledgeItem(
  item: string,
  topic: string,
  detail: string,
  evidence: string[],
  sourceText: string,
): string {
  // Apply only the source/meta guard from deduplicateSummary.  Knowledge
  // items may intentionally repeat a title token while adding a condition
  // (for example "RTX5090環境では…"); substring-based summary deduplication
  // would incorrectly discard that reusable condition.
  const retained = deduplicateSummary(item, "", "", [], sourceText);
  if (!retained) return "";
  const itemIdentity = identity(retained);
  const exactDuplicates = [topic, detail, ...evidence]
    .map(identity)
    .filter(Boolean);
  return exactDuplicates.includes(itemIdentity) ? "" : retained;
}

/**
 * Normalize the canonical knowledge list just before persistence.
 *
 * The planner output is untrusted even when it advertises schema v4.  Keep
 * only non-empty semantic lines, remove exact generic headings, and
 * de-duplicate items by the same identity rule used for title/summary guards.
 * Source refs and verbatim blocks are handled independently and are never
 * folded into this list.
 */
function knowledgeSourceBodies(
  inputSource: string,
  fetchResults: UrlFetchResult[] = [],
): string[] {
  const bodies = [normalizeNewlines(inputSource)];
  for (const item of fetchResults) {
    if (item.success && item.body?.trim()) {
      bodies.push(normalizeNewlines(item.body));
    }
  }
  return bodies;
}

function groundedTopicContext(
  subject: string,
  detail: string,
  sourceBodies: string | readonly string[],
): string {
  const bodies = typeof sourceBodies === "string" ? [sourceBodies] : [...sourceBodies];
  const joined = bodies.filter(Boolean).join("\n");
  const parts: string[] = [];
  if (subject && hasGroundingOverlap(subject, joined)) {
    parts.push(cleanLine(subject, TITLE_LIMIT));
  }
  if (detail && hasGroundingOverlap(detail, joined)) {
    parts.push(cleanLine(detail, TITLE_LIMIT));
  }
  return parts.filter(Boolean).join(" ");
}

function normalizeKnowledgeItems(
  plan: LlmPlan,
  topic: string,
  detail: string,
  evidence: string[],
  sourceBodies: string | readonly string[],
  topicAnchor = "",
): string[] {
  const sources = typeof sourceBodies === "string"
    ? [sourceBodies]
    : [...sourceBodies];
  const joinedSource = sources.join("\n");
  const topicContext = groundedTopicContext(plan.subject, plan.titleDetail, sources);
  const result: string[] = [];
  const seen = new Set<string>();
  for (const raw of plan.knowledgeItems) {
    const item = normalizeKnowledgeItemText(raw, sources, topicContext, topicAnchor);
    if (!item) continue;
    const retained = deduplicateKnowledgeItem(item, topic, detail, evidence, joinedSource);
    if (!retained) continue;
    const key = identity(retained);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    result.push(retained);
    if (result.length >= KNOWLEDGE_ITEM_LIMIT) break;
  }
  return result;
}

function explanatoryLabel(value: string): boolean {
  return /(?:概要|要約|説明|詳細|特徴|理由|ポイント|メリット|所感|感想)$/.test(
    value.replace(/[\s:：]+/g, ""),
  );
}

function inferShortLiteralKind(value: string): ShortLiteral["kind"] | null {
  const text = value.trim();
  if (/^https?:\/\/\S+$/i.test(text)) return "url";
  if (/^(?:model(?:_name)?|使用モデル|利用モデル|checkpoint|provider|environment|env|実行環境|使用環境|環境|利用環境)\s*[:：=]\s*\S+/i.test(text)) {
    return "setting";
  }
  if (/^[A-Za-z_][\w.-]*\s*=/.test(text) || (/^[{[]/.test(text) && /[}\]]$/.test(text))) {
    return "setting";
  }
  if (/^(?:\$\s*)?(?:python(?:\d+(?:\.\d+)?)?|pip|uv|npm|pnpm|yarn|bun|git|docker|curl|wget|pwsh|powershell|cmd|bash|sh|node|npx|deno|java|go|cargo|make)\b/i.test(text)) {
    return "command";
  }
  if (/^[A-Za-z_][\w.-]{1,80}\s*[:：]\s*\S+/.test(text)) return "setting";
  return null;
}

function normalizedShortLiterals(
  plan: LlmPlan,
  source: string,
): { excerpts: Array<{ label: string; lines: string[] }>; promotedSummary: string } {
  const excerpts: Array<{ label: string; lines: string[] }> = [];
  let promotedSummary = "";
  for (const item of plan.shortLiterals) {
    const content = rangeContent(source, item, false);
    if (content === null || !content.trim() || content.length > 500) continue;
    if (item.kind === "short_quote" && explanatoryLabel(item.label)) {
      promotedSummary ||= content;
      continue;
    }
    if (item.kind !== "short_quote" && inferShortLiteralKind(content) !== item.kind) continue;
    excerpts.push({ label: cleanLine(item.label, 120) || "原文", lines: [content.trim()] });
    if (excerpts.length >= EXCERPT_LIMIT) break;
  }
  for (const excerpt of plan.excerpts) {
    if (excerpts.length >= EXCERPT_LIMIT) break;
    for (const rawLine of excerpt.lines) {
      const text = normalizeNewlines(rawLine);
      if (text.includes("\n") || !text.trim() || text.length > 500 || !source.includes(text)) continue;
      const kind = inferShortLiteralKind(text);
      if (!kind) {
        if (!promotedSummary && explanatoryLabel(excerpt.label)) promotedSummary = text;
        continue;
      }
      excerpts.push({ label: cleanLine(excerpt.label, 120) || "原文", lines: [text.trim()] });
      if (excerpts.length >= EXCERPT_LIMIT) break;
    }
  }
  // Keep explicit model/environment metadata visible even if a local planner
  // omitted short_literals.  This repair is intentionally limited to known
  // metadata prefixes; ordinary prose is never promoted to a setting.
  if (excerpts.length < EXCERPT_LIMIT) {
    const metadataPrefix = /^(?:model(?:_name)?|使用モデル|利用モデル|checkpoint|provider|environment|env|実行環境|使用環境|環境|利用環境)\s*[:：=]/i;
    const existingLines = new Set(excerpts.flatMap((excerpt) => excerpt.lines.map((line) => line.trim())));
    for (const rawLine of normalizeNewlines(source).split("\n")) {
      if (excerpts.length >= EXCERPT_LIMIT) break;
      const text = rawLine.trim();
      const match = metadataPrefix.exec(text);
      if (!text || existingLines.has(text) || !match || inferShortLiteralKind(text) !== "setting") continue;
      excerpts.push({
        label: match[0].replace(/\s*[:：=]\s*$/, "").trim() || "設定",
        lines: [text],
      });
      existingLines.add(text);
    }
  }
  return { excerpts, promotedSummary };
}

function topicAnchorForPlan(plan: LlmPlan, subject: string, source: string): string {
  if (plan.legacySchema) return "";
  if (subject && !metadataOnlyTitleSubject(subject, source) && topicAnchorPattern(subject)) {
    return subject;
  }
  const metadataPrefix = /^(?:model(?:_name)?|使用モデル|利用モデル|checkpoint|provider|environment|env|実行環境|使用環境|環境|利用環境)\s*[:：=]/i;
  const lines = [
    ...plan.excerpts.flatMap((excerpt) => excerpt.lines),
    ...normalizeNewlines(source).split("\n"),
  ];
  for (const rawLine of lines) {
    const line = rawLine.trim();
    const match = metadataPrefix.exec(line);
    if (!match) continue;
    const value = line.slice(match[0].length).trim();
    if (topicAnchorPattern(value) && topicAnchorInText(value, line)) return value;
  }
  return "";
}

function utf8Bytes(value: string): number[] {
  const bytes: number[] = [];
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    if (code <= 0x7f) bytes.push(code);
    else if (code <= 0x7ff) bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    else if (code <= 0xffff) bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    else bytes.push(0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
  }
  return bytes;
}

function sha256(value: string): string {
  const rightRotate = (word: number, amount: number) => (word >>> amount) | (word << (32 - amount));
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const bytes = utf8Bytes(value);
  const bitLength = bytes.length * 8;
  bytes.push(0x80);
  while (bytes.length % 64 !== 56) bytes.push(0);
  for (let shift = 56; shift >= 0; shift -= 8) {
    bytes.push(Math.floor(bitLength / 2 ** shift) & 0xff);
  }
  const hash = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  for (let offset = 0; offset < bytes.length; offset += 64) {
    const words = new Array<number>(64).fill(0);
    for (let index = 0; index < 16; index += 1) {
      const base = offset + index * 4;
      words[index] = ((bytes[base] << 24) | (bytes[base + 1] << 16) | (bytes[base + 2] << 8) | bytes[base + 3]) >>> 0;
    }
    for (let index = 16; index < 64; index += 1) {
      const s0 = rightRotate(words[index - 15], 7) ^ rightRotate(words[index - 15], 18) ^ (words[index - 15] >>> 3);
      const s1 = rightRotate(words[index - 2], 17) ^ rightRotate(words[index - 2], 19) ^ (words[index - 2] >>> 10);
      words[index] = (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index += 1) {
      const s1 = rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + choose + constants[index] + words[index]) >>> 0;
      const s0 = rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + majority) >>> 0;
      h = g; g = f; f = e; e = (d + temp1) >>> 0; d = c; c = b; b = a; a = (temp1 + temp2) >>> 0;
    }
    hash[0] = (hash[0] + a) >>> 0; hash[1] = (hash[1] + b) >>> 0;
    hash[2] = (hash[2] + c) >>> 0; hash[3] = (hash[3] + d) >>> 0;
    hash[4] = (hash[4] + e) >>> 0; hash[5] = (hash[5] + f) >>> 0;
    hash[6] = (hash[6] + g) >>> 0; hash[7] = (hash[7] + h) >>> 0;
  }
  return hash.map((word) => word.toString(16).padStart(8, "0")).join("");
}

function looksLikeVerbatim(value: string): boolean {
  const lines = normalizeNewlines(value).split("\n");
  return lines.some((line) => line.length > 500)
    || lines.some((line, index) => !line && index > 0 && index < lines.length - 1)
    || lines.some((line) => line.startsWith("\t") || line.startsWith("  "))
    || lines.some((line) => /^\s*(?:```|~~~)/.test(line))
    || lines.length > 12;
}

/**
 * Semantic normalization can legitimately discard every planner item while
 * short literals (for example a model setting) still survive.  Keep the full
 * input in that case only when it has multiple substantive lines; a one-line
 * title-only clip must remain a normal title instead of being promoted to
 * verbatim content.
 */
function hasSubstantiveMultilineInput(value: string): boolean {
  const lines = normalizeNewlines(value)
    .split("\n")
    .map((line) => line.trim())
    .filter(
      (line) =>
        Boolean(line)
        && !/^https?:\/\/\S+$/i.test(line)
        && !isResearchControlWrapper(line)
        && !isTopicMetadataLine(line)
        && !isTopicGenericHeading(line)
        && !TOPIC_AUTHOR_ATTRIBUTION_PATTERN.test(line)
        && !/^(?:投稿者|著者|作者|author|by)\b/i.test(line)
        && /[A-Za-z0-9\u3040-\u9fff]/.test(line),
    );
  return lines.length >= 2;
}

function isResearchControlWrapper(value: string): boolean {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text || text.length > 500) return false;
  if (/^この内容はChatGPT側で.+(?:調査|検索|事実確認).+(?:完了|済み)/.test(text)) {
    return true;
  }
  const hasActor = text.includes("AoiTalk側") || text.includes("ChatGPT側");
  const hasOperation = [
    "追加検索不要",
    "追加調査不要",
    "追加検索・追加調査を行わず",
    "追加調査を行わず",
    "ノードへ取り込んで",
    "取り込み操作",
  ].some((token) => text.includes(token));
  return hasActor && hasOperation;
}

function inputSourceFallbackBlock(source: string): LocalVerbatimBlock | null {
  const normalized = normalizeNewlines(source);
  if (!normalized.trim() || !hasSubstantiveMultilineInput(normalized)) return null;
  return makeVerbatimBlock(
    {
      sourceId: "source:0",
      startLine: 1,
      endLine: normalized.split("\n").length,
      kind: "formatted",
      label: "入力本文",
    },
    normalized,
  );
}

function isExplicitPromptLabel(label: string): boolean {
  return PROMPT_MARKER_PATTERN.test(String(label ?? ""));
}

function repairLocalOneLinePromptRange(plan: LlmPlan, source: string): void {
  if (plan.verbatimRanges.length) return;
  const lines = normalizeNewlines(source).split("\n");
  const candidates = lines
    .map((line, index) => ({ line, lineNo: index + 1 }))
    .filter(({ line }) => line.trim() && !/^https?:\/\/\S+$/i.test(line.trim()));
  if (candidates.length !== 1) return;
  const [{ line, lineNo }] = candidates;
  if (PROMPT_MARKER_ONLY_PATTERN.test(line)) return;
  const labels = [plan.subject, plan.titleDetail, plan.summary, ...plan.shortLiterals.flatMap((item) => [item.label, item.kind])];
  const marker = PROMPT_LINE_MARKER_PATTERN.test(line) || labels.some(isExplicitPromptLabel);
  if (!marker && plan.contentMode !== "verbatim" && plan.contentMode !== "mixed") return;
  plan.verbatimRanges = [{
    sourceId: "source:0",
    startLine: lineNo,
    endLine: lineNo,
    kind: "prompt",
    label: "プロンプト原文",
  }];
}

/**
 * A one-line prompt is preserved only when the planner made the intent
 * explicit. Ordinary one-line prose must remain summary material even if a
 * weak model labels it as `prompt`.
 */
function makeVerbatimBlock(
  range: VerbatimRange,
  content: string,
): LocalVerbatimBlock {
  const normalized = normalizeNewlines(content);
  return {
    kind: range.kind,
    label: cleanLine(range.label, 120) || "原文",
    content: normalized,
    sha256: sha256(normalized),
    char_count: Array.from(normalized).length,
    line_count: normalized.split("\n").length,
    blank_line_count: normalized.split("\n").filter((line) => line === "").length,
    source_id: range.sourceId,
    source_type: "input",
    source_url: "",
    start_line: range.startLine,
    end_line: range.endLine,
  };
}

function localSourceRefs(
  urls: string[],
  blocks: LocalVerbatimBlock[],
): Array<Record<string, unknown>> {
  // Re-validate at the last boundary before SQLite.  `urls` normally comes
  // from extractUrls, but keeping this guard here prevents a future caller
  // from reintroducing credentials or secret query keys into provenance.
  const safeUrls = urls
    .map((url) => canonicalizeIngestUrl(url))
    .filter((url): url is string => Boolean(url));
  const refs: Array<Record<string, unknown>> = [
    { source_id: "source:0", source_type: "input", used: true },
    ...safeUrls.map((url, index) => ({
      source_id: `source:${index + 1}`,
      source_type: "direct",
      url,
      used: false,
      acquisition_status: "empty_body",
    })),
    ...blocks.map((block) => ({
      source_id: canonicalSourceId(block.source_id),
      source_type: block.source_type,
      used: true,
      kind: block.kind,
      label: block.label,
      sha256: block.sha256,
      start_line: block.start_line,
      end_line: block.end_line,
      char_count: block.char_count,
      line_count: block.line_count,
      blank_line_count: block.blank_line_count,
    })),
  ];
  return refs.slice(0, 24);
}

function localDocBlockType(kind: LocalVerbatimBlock["kind"]): "markdown" | "code" {
  return kind === "code" || kind === "script" ? "code" : "markdown";
}

function verbatimBlocks(plan: LlmPlan, source: string): LocalVerbatimBlock[] {
  const blocks: LocalVerbatimBlock[] = [];
  const intervals: Array<[number, number]> = [];
  for (const range of plan.verbatimRanges) {
    const content = rangeContent(source, range, true);
    if (!content) throw new Error("原文ブロックが空です");
    if (intervals.some(([start, end]) => range.startLine <= end && range.endLine >= start)) {
      throw new Error("原文ブロックの範囲が重複しています");
    }
    intervals.push([range.startLine, range.endLine]);
    blocks.push(makeVerbatimBlock(range, content));
  }
  const normalized = normalizeNewlines(source);
  if (!blocks.length && plan.legacySchema && looksLikeVerbatim(normalized)) {
    blocks.push(makeVerbatimBlock({
      sourceId: "source:0",
      startLine: 1,
      endLine: normalized.split("\n").length,
      kind: "formatted",
      label: "原文",
    }, normalized));
  }
  if ((plan.contentMode === "verbatim" || plan.contentMode === "mixed") && !blocks.length) {
    throw new Error("原文保存モードですが、有効な原文範囲がありません");
  }
  return blocks;
}

async function chooseLocalRoutingTarget(
  settings: NonNullable<Awaited<ReturnType<typeof getConfiguredClipIngestMobileLlmSettings>>>,
  source: string,
  targets: ClipIngestTarget[],
  fetchResults: UrlFetchResult[],
): Promise<LocalRoutingResult> {
  const fallbackTarget = safeFallbackTarget(targets);
  const normalTargets = uniqueNormalTargets(targets);

  // fallbackだけが設定されている場合、候補選択LLMを呼び出す意味がない。
  if (!normalTargets.length) {
    if (!fallbackTarget) {
      throw new LocalClipIngestUnavailableError("保存先を判定できませんでした");
    }
    return { target: fallbackTarget, matched: false };
  }

  const firstReply = await beforeSideEffects("端末ルーターLLMの呼び出しに失敗", () =>
    generateMobileLlmReply(
      settings,
      [],
      buildLocalRoutingPrompt(source, normalTargets, fetchResults),
    ),
  );

  const choice = await beforeSideEffects("端末ルーター応答の解釈に失敗", () =>
    parseLocalRoutingChoice(firstReply.content),
  );
  const initialTarget = targetForId(normalTargets, choice.targetId);
  if (!initialTarget) {
    if (fallbackTarget) return { target: fallbackTarget, matched: false };
    throw new LocalClipIngestUnavailableError("保存先を判定できませんでした");
  }

  const history: LocalRoutingHistoryEntry[] = [];
  const visited = new Set<string>();
  let current = initialTarget;
  let selectedConfidence = choice.confidence;

  while (history.length < MAX_ROUTING_INSPECTIONS) {
    const currentId = stableTargetId(current);
    if (!currentId || visited.has(currentId)) {
      if (fallbackTarget) return { target: fallbackTarget, matched: false };
      throw new LocalClipIngestUnavailableError("ルーティング候補が不正です");
    }
    visited.add(currentId);
    const historyEntry: LocalRoutingHistoryEntry = {
      targetId: currentId,
      title: current.label,
      breadcrumb: current.breadcrumb,
      routingHint: current.routingHint,
      confidence: selectedConfidence,
      action: "selected",
      reason: "",
    };
    history.push(historyEntry);

    const reply = await beforeSideEffects("端末ルーターLLMの呼び出しに失敗", () =>
      generateMobileLlmReply(
        settings,
        [],
        buildLocalRoutingInspectionPrompt(
          source,
          current,
          history,
          fetchResults,
          normalTargets.filter(
            (target) => !visited.has(stableTargetId(target)),
          ),
        ),
      ),
    );
    const action = await beforeSideEffects("端末ルーター応答の解釈に失敗", () =>
      parseLocalRoutingAction(reply.content),
    );
    const currentMatches =
      action.currentTargetId === currentId ||
      targetForId([current], action.currentTargetId) !== null;
    if (!currentMatches) {
      if (fallbackTarget) return { target: fallbackTarget, matched: false };
      throw new LocalClipIngestUnavailableError("ルーティング候補が不正です");
    }
    historyEntry.action = action.action;
    historyEntry.confidence = action.confidence;
    historyEntry.reason = action.reason;

    if (action.action === "accept") {
      if (!action.ambiguous && action.confidence >= MIN_ROUTING_CONFIDENCE) {
        return { target: current, matched: true };
      }
      if (fallbackTarget) return { target: fallbackTarget, matched: false };
      throw new LocalClipIngestUnavailableError("保存先の確信度が不足しています");
    }
    if (action.action === "fallback") {
      if (fallbackTarget) return { target: fallbackTarget, matched: false };
      throw new LocalClipIngestUnavailableError("保存先を判定できませんでした");
    }

    // 3件目を確認し終えたら、next_target_idの妥当性にかかわらず
    // 4件目へ進まず、確認済み3件の最終比較へ移る。
    if (history.length >= MAX_ROUTING_INSPECTIONS) break;

    const next = targetForId(normalTargets, action.nextTargetId);
    if (!next || visited.has(stableTargetId(next))) {
      if (fallbackTarget) return { target: fallbackTarget, matched: false };
      throw new LocalClipIngestUnavailableError("ルーティング候補が不正です");
    }
    current = next;
    selectedConfidence = action.confidence;
  }

  // 最大数まで確認した後は、最後の候補を盲目的に採用せず、確認済み候補
  // 全体へ最終比較を行う。比較プロンプトには未確認候補を一切含めない。
  const compareReply = await beforeSideEffects("端末ルーターLLMの呼び出しに失敗", () =>
    generateMobileLlmReply(
      settings,
      [],
      buildLocalRoutingComparePrompt(source, history, fetchResults),
    ),
  );
  const comparison = await beforeSideEffects("端末ルーター応答の解釈に失敗", () =>
    parseLocalRoutingComparison(compareReply.content),
  );
  const compared = targetForId(normalTargets, comparison.targetId);
  if (
    compared &&
    visited.has(stableTargetId(compared)) &&
    comparison.matched &&
    !comparison.ambiguous &&
    comparison.confidence >= MIN_ROUTING_CONFIDENCE
  ) {
    return { target: compared, matched: true };
  }
  if (fallbackTarget) return { target: fallbackTarget, matched: false };
  throw new LocalClipIngestUnavailableError("保存先の確信度が不足しています");
}

async function resolveTargetNode(
  target: ClipIngestTarget,
): Promise<DocsNode | null> {
  if (target.nodeId) {
    const node = await docsRepo.getNode(target.nodeId);
    if (node && !node.archived_at) return node;
  }
  if (target.nodeSystemKey) {
    return docsRepo.getNodeBySystemKey(target.nodeSystemKey);
  }
  return null;
}

const FILM_ROOT_SYSTEM_KEY = "foam_source_grounded_v1:root.Film";
const MAX_TARGET_ANCESTRY_HOPS = 64;

/**
 * 設定JSONのbreadcrumbを信用せず、同期済みDocsのroot/親階層でFilm配下を拒否する。
 * 祖先が端末に無く判定不能な場合も、誤保存せずサーバー再送用の保留へ回す。
 */
async function isAllowedLocalClipTarget(node: DocsNode): Promise<boolean> {
  const pending: DocsNode[] = [node];
  const seen = new Set<string>();
  while (pending.length > 0 && seen.size < MAX_TARGET_ANCESTRY_HOPS) {
    const current = pending.pop()!;
    if (seen.has(current.id)) continue;
    seen.add(current.id);
    if (current.system_key === FILM_ROOT_SYSTEM_KEY) return false;

    const relatedIds = [current.root_page_id, current.parent_id]
      .filter(
        (id): id is string =>
          typeof id === "string" && id.length > 0 && !seen.has(id),
      );
    for (const relatedId of new Set(relatedIds)) {
      const related = await docsRepo.getNode(relatedId);
      if (!related || related.workspace_id !== node.workspace_id) return false;
      pending.push(related);
    }
  }
  return pending.length === 0;
}

// 生成構造は `target → topic → (summary / excerpt / 出典) → URL` で、URLは
// target から見て曾孫（相対深さ3）にある。topic 配下をそこまで辿らないと
// 重複判定が一度も当たらないため、相対深さ3まで走査する。
const DUPLICATE_SCAN_DEPTH = 3;
// listChildren の N+1 が青天井にならないよう、1回の判定で読むノード数を上限で抑える。
const DUPLICATE_SCAN_NODE_LIMIT = 400;

/**
 * 取り込み先のサブツリー（深さ4相当）に同じURLが既にあるかを見る重複判定。
 * 見つかった場合は開く対象として target 直下の topic ノードを返す。
 */
async function findDuplicateChild(
  parentId: string,
  urls: string[],
): Promise<DocsNode | null> {
  const wanted = urls.filter((url) => Boolean(url));
  if (!wanted.length) return null;

  const children = await docsRepo.listChildren(parentId);
  let budget = DUPLICATE_SCAN_NODE_LIMIT - children.length;

  const matches = (node: DocsNode): boolean => {
    const joined = `${node.title ?? ""}\n${node.description ?? ""}`;
    const existingUrls = extractUrls(joined);
    return wanted.some((url) => joined.includes(url) || existingUrls.includes(url));
  };

  for (const child of children) {
    if (matches(child)) return child;
    // topic 直下から相対深さ DUPLICATE_SCAN_DEPTH まで幅優先で辿る。
    let level = [child];
    for (let depth = 1; depth <= DUPLICATE_SCAN_DEPTH && budget > 0; depth += 1) {
      const next: DocsNode[] = [];
      for (const node of level) {
        if (budget <= 0) break;
        const grandChildren = await docsRepo.listChildren(node.id);
        budget -= grandChildren.length;
        for (const grandChild of grandChildren) {
          if (matches(grandChild)) return child;
          next.push(grandChild);
        }
      }
      if (!next.length) break;
      level = next;
    }
  }
  return null;
}

/** 既存の先頭より前へ差し込む sort_order（新しい取り込みほど上に出す）。 */
async function firstSortOrder(parentId: string): Promise<number> {
  const children = await docsRepo.listChildren(parentId);
  const orders = children
    .map((child) => child.sort_order)
    .filter((order): order is number => typeof order === "number");
  if (!orders.length) return 1024;
  return Math.min(...orders) - 1024;
}

export interface LocalClipIngestOptions {
  // 現在のモバイルcaller/APIには明示target_node_idが存在しないため、
  // 自動ルーターをskipするoptional指定は追加していない。
  /**
   * URL本文を取得できないまま保存してよいか。
   *
   * 端末がオフラインのときだけ true にする。オンラインでサーバーだけ落ちている
   * 場合は、まもなく到達できるサーバー版の方が良い結果になるため保留へ回す。
   */
  allowUnfetchedUrls?: boolean;
  /**
   * LLMを使わずに保存してよいか。
   *
   * オフラインではクラウドLLMを呼べないため、分類も要約もせずに入力をそのまま
   * 未分類の保存先へ残す。入力を失わないことを優先する。
   */
  allowWithoutLlm?: boolean;
}

const OFFLINE_TOPIC_LIMIT = 120;

/**
 * LLMを使わない保存計画。入力に書かれていないことは一切足さない。
 * 分類は行わないので matched=false とし、fallback の取り込み先へ落とす。
 */
function buildOfflinePlan(source: string, urls: string[]): LlmPlan {
  const lines = String(source || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  const textLines = lines.filter((line) => !/^https?:\/\//i.test(line));
  const topicSource = textLines[0] || urls[0] || lines[0] || "";
  const topic =
    cleanLine(topicSource, OFFLINE_TOPIC_LIMIT) || "オフライン取り込み";
  const unconfirmed = [
    "オフラインのため自動分類と要約を行わず、入力を原文のまま保存しました。",
  ];
  if (urls.length) {
    unconfirmed.push(
      `リンク先の本文は未取得です（内容は未確認）: ${urls.join(" / ")}`,
    );
  }
  return {
    targetId: "",
    matched: false,
    topic,
    subject: topic,
    titleDetail: "",
    titleEvidence: [],
    contentMode: "verbatim",
    knowledgeItems: [],
    summary: "",
    details: [],
    unconfirmed,
    shortLiterals: [],
    verbatimRanges: [{
      sourceId: "source:0",
      startLine: 1,
      endLine: normalizeNewlines(source).split("\n").length,
      kind: "formatted",
      label: "オフライン取り込み原文",
    }],
    excerpts: [],
    legacySchema: false,
  };
}

/**
 * モバイルLLMだけでクリップ取り込みを完結させる。
 * 実行条件（LLM設定・取り込み先・LLM応答）が満たせない場合は
 * `LocalClipIngestUnavailableError` を投げ、呼び出し側で保留キューへ回す。
 */
export async function runLocalClipIngest(
  source: string,
  options: LocalClipIngestOptions = {},
): Promise<ClipIngestResult> {
  const allowUnfetchedUrls = options.allowUnfetchedUrls === true;
  const allowWithoutLlm = options.allowWithoutLlm === true;

  // ---- ここから下、ノード作成までは副作用が無い。失敗はすべて「実行不可」。 ----
  const settings = await beforeSideEffects("モバイルLLM設定の読み取りに失敗", () =>
    getConfiguredClipIngestMobileLlmSettings(),
  );
  if (!settings && !allowWithoutLlm) {
    throw new LocalClipIngestUnavailableError(
      "モバイル側のLLMが未設定のため端末だけで取り込めません",
    );
  }
  // 端末だけで完結させる経路なので、取り込み先のリモート取得は試さない。
  const targets = await beforeSideEffects(
    "取り込み先キャッシュの読み取りに失敗",
    () => loadClipIngestTargets({ allowRemote: false }),
  );
  if (!targets.length) {
    throw new LocalClipIngestUnavailableError(
      "取り込み先設定のキャッシュがありません",
    );
  }

  const urls = extractUrls(source);
  // React Native の fetch は接続先IPを固定できず、DNS解決結果とredirect先を
  // サーバー版と同じ強度で検証できない。端末/LAN内URLのSSRFと、その本文を
  // クラウドLLMへ送る漏えいを避けるため、ローカル経路ではURL本文を取得しない。
  // オンラインでサーバーだけ落ちている間は、本文付きで取り込めるサーバー版へ
  // 保留再送する。オフラインでは再送先が無く入力が宙に浮くので、本文未取得の
  // まま「内容は未確認」と明示して保存する。
  if (urls.length && !allowUnfetchedUrls) {
    throw new LocalClipIngestUnavailableError(
      "URL本文は端末から安全に取得できないため、サーバー接続後に自動で取り込みます",
    );
  }
  const fetchResults: UrlFetchResult[] = [];

  let plan: LlmPlan | null = null;
  let resolvedTarget: ClipIngestTarget | null = null;
  let routingMatched = false;
  if (settings) {
    try {
      const routed = await chooseLocalRoutingTarget(
        settings,
        source,
        targets,
        fetchResults,
      );
      resolvedTarget = routed.target;
      routingMatched = routed.matched;
    } catch (error) {
      // オフラインではクラウドLLMへ届かないのが通常。保留にすると入力が
      // 見えない場所へ消えるため、LLM無しの保存へ落として必ず残す。
      if (!allowWithoutLlm) throw error;
    }
  }
  // routing完了後に、保存内容だけを別呼び出しで生成する。返却された
  // target_id/matched等は保存先決定には使用しない。
  if (!plan && settings && resolvedTarget) {
    try {
      const reply = await beforeSideEffects("端末コンテンツLLMの呼び出しに失敗", () =>
        generateMobileLlmReply(
          settings,
          [],
          buildLocalContentPrompt(source, resolvedTarget!, fetchResults),
        ),
      );
      plan = await beforeSideEffects("保存計画の解釈に失敗", () =>
        parseLocalPlan(reply.content),
      );
    } catch (error) {
      if (!allowWithoutLlm) throw error;
      // コンテンツ生成だけが失敗した場合も、オフライン経路と同じく
      // 安全なfallbackへ原文を残す（選択候補へ推測保存しない）。
      resolvedTarget = safeFallbackTarget(targets);
      routingMatched = false;
      plan = null;
    }
  }
  if (!plan) {
    plan = buildOfflinePlan(source, urls);
    resolvedTarget = safeFallbackTarget(targets);
    routingMatched = false;
  }
  if (!resolvedTarget) {
    throw new LocalClipIngestUnavailableError("保存先を判定できませんでした");
  }
  // 保存先はrouterで確定したコード側の値を常に使用する。LLMの
  // targetId/matched/action/confidenceの返却値でここを再解決してはいけない。
  plan.targetId = stableTargetId(resolvedTarget);
  // matchedはrouterの結果をコード側で固定する。fallback/オフライン計画を
  // content plannerのmatched=trueで分類済みに昇格させない。
  plan.matched = routingMatched;
  const title = await beforeSideEffects("保存計画のタイトル検証に失敗", () =>
    normalizedPlanTitle(plan!, source, urls),
  );
  plan.subject = title.subject;
  plan.titleDetail = title.detail;
  plan.topic = title.topic;
  const literalResult = await beforeSideEffects("短い原文要素の検証に失敗", () =>
    normalizedShortLiterals(plan!, normalizeNewlines(source)),
  );
  plan.excerpts = literalResult.excerpts;
  if (!plan.knowledgeItems.length && literalResult.promotedSummary) {
    plan.knowledgeItems = [cleanLine(literalResult.promotedSummary, SUMMARY_LIMIT)];
  }
  plan.knowledgeItems = normalizeKnowledgeItems(
    plan,
    plan.topic,
    plan.titleDetail,
    title.evidence,
    knowledgeSourceBodies(source, fetchResults),
    topicAnchorForPlan(plan!, title.subject, source),
  );
  // Keep compatibility aliases synchronized for prompt-repair and callers
  // that still inspect summary/details while making the tree from v4 items.
  plan.summary = plan.knowledgeItems[0] ?? "";
  plan.details = plan.knowledgeItems.slice(1);
  repairLocalOneLinePromptRange(plan, source);
  const losslessFallback = !plan.knowledgeItems.length && !plan.verbatimRanges.length
    ? inputSourceFallbackBlock(source)
    : null;
  const blocks = await beforeSideEffects("原文ブロックの検証に失敗", () =>
    losslessFallback ? [losslessFallback] : verbatimBlocks(plan!, source),
  );
  if (!plan.knowledgeItems.length && !blocks.length) {
    const fallbackBlock = inputSourceFallbackBlock(source);
    if (fallbackBlock) blocks.push(fallbackBlock);
  }
  if (blocks.length && plan.contentMode === "summary") {
    plan.contentMode = plan.knowledgeItems.length ? "mixed" : "verbatim";
  }
  // A grounded topic alone is a valid short clip.  In particular, when the
  // planner's meta-summary is removed by the guard, do not replace it with a
  // knowledge-free sentence or lose the clip entirely.
  if (!plan.topic) {
    throw new LocalClipIngestUnavailableError("保存計画の内容が不足しています");
  }
  const target = resolvedTarget;
  const targetNode = await beforeSideEffects(
    "取り込み先ノードの解決に失敗",
    () => resolveTargetNode(target),
  );
  if (!targetNode) {
    throw new LocalClipIngestUnavailableError(
      "取り込み先ノードが端末に同期されていません",
    );
  }
  const targetAllowed = await beforeSideEffects(
    "取り込み先ノードの階層確認に失敗",
    () => isAllowedLocalClipTarget(targetNode),
  );
  if (!targetAllowed) {
    throw new LocalClipIngestUnavailableError(
      "Film配下または階層を確認できないノードは端末だけで取り込めません",
    );
  }

  const failedUrls = urls.map((url) => ({
    url,
    error: "端末ではリンク先本文を取得していません",
    acquisition_status: "empty_body",
  }));
  const targetLabel = target.label || targetNode.title || "取り込み先";
  const sourceRefs = localSourceRefs(urls, blocks);

  const duplicate = await beforeSideEffects("重複判定に失敗", () =>
    findDuplicateChild(targetNode.id, urls),
  );
  if (duplicate) {
    return {
      target_id: targetNode.id,
      target_label: targetLabel,
      action: "duplicate_skip",
      changed_node_id: null,
      changed_node_title: null,
      open_node_id: duplicate.id,
      open_node_title: duplicate.title,
      direct_urls: [],
      supplemental_urls: [],
      failed_urls: failedUrls,
      used_urls: [],
      unconfirmed: plan.unconfirmed,
    };
  }

  const sortOrder = await beforeSideEffects("挿入位置の決定に失敗", () =>
    firstSortOrder(targetNode.id),
  );

  // ---- ここから下は副作用あり。失敗しても実行不可へ寄せず素の例外で投げる ----
  // （保留キューへ回すと、作りかけのノードに加えて再送分が重複するため）。
  const outline: Array<{ text: string; children?: string[] }> = [
    // v4 knowledge_items are already semantic, reusable units.  Keep them as
    // siblings immediately below topic; do not recreate a summary/details
    // wrapper that forces readers to open a source-dependent heading first.
    ...plan.knowledgeItems.map((item) => ({ text: item })),
    ...plan.excerpts.flatMap((excerpt) =>
      GENERIC_KNOWLEDGE_ITEM_PATTERN.test(excerpt.label)
        ? excerpt.lines.map((line) => ({ text: line }))
        : [{ text: excerpt.label, children: excerpt.lines }],
    ),
  ];
  if (urls.length) {
    outline.push({ text: "元リンク（本文を取得できず内容は未確認）", children: urls });
  }
  const node = await docsRepo.createClipIngestTree({
    parentId: targetNode.id,
    rootPageId: targetNode.root_page_id ?? targetNode.id,
    projectId: targetNode.project_id ?? null,
    title: plan.topic,
    sortOrder,
    bodyJson: {
      clip_ingest: {
        schema_version: 4,
        content_mode: plan.contentMode,
      },
    },
    sourceRefs,
    outline,
    blocks: blocks.map<ClipIngestDocBlockInput>((block) => ({
      label: block.label,
      blockType: localDocBlockType(block.kind),
      content: block.content,
      clipIngest: {
        schema_version: 4,
        source_id: canonicalSourceId(block.source_id),
        source_type: block.source_type,
        kind: block.kind,
        label: block.label,
        sha256: block.sha256,
        start_line: block.start_line,
        end_line: block.end_line,
        char_count: block.char_count,
        line_count: block.line_count,
        blank_line_count: block.blank_line_count,
      },
    })),
  });
  // `createClipIngestTree` materializes each block as an ordinary child node.
  // Re-check the exact input before returning so future callers cannot pass a
  // non-LF or accidentally empty block into the local writer.  The hash is
  // retained in clip_ingest/source_refs as provenance, never as immutable UI
  // content.
  for (const expected of blocks) {
    const normalized = normalizeNewlines(expected.content);
    if (
      normalized !== expected.content
      || sha256(normalized) !== expected.sha256
      || Array.from(normalized).length !== expected.char_count
      || normalized.split("\n").length !== expected.line_count
      || normalized.split("\n").filter((line) => line === "").length !== expected.blank_line_count
    ) {
      throw new Error("原文ブロックの完全性検証に失敗しました");
    }
  }

  return {
    target_id: targetNode.id,
    target_label: targetLabel,
    action: "create",
    changed_node_id: node.id,
    changed_node_title: node.title,
    open_node_id: node.id,
    open_node_title: node.title,
    direct_urls: [],
    supplemental_urls: [],
    failed_urls: failedUrls,
    used_urls: [],
    unconfirmed: plan.unconfirmed,
  };
}
