"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  BarChart3,
  CalendarDays,
  Check,
  CircleDot,
  CircleDollarSign,
  FileText,
  FlaskConical,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Save,
  Sparkles,
  Upload,
  Users,
} from "lucide-react";
import { toast } from "sonner";

import {
  mediaOperationsApi,
  MediaOperationsApiError,
  type CharacterCreateInput,
  type CharacterPatchInput,
  type CharacterDashboardCalendarEntry,
  type CharacterDashboardConnectedAccount,
  type CharacterDashboardLearningItem,
  type CharacterDashboardMetricItem,
  type CharacterDashboardPage,
  type CharacterDashboardRecipe,
  type CharacterDashboardRevenueItem,
  type CharacterDashboardResearchCandidate,
  type CharacterDashboardRun,
  type MediaCharacter,
  type MediaCharacterDashboard,
  type MediaCharacterDetail,
  type MediaPersonaResource,
  type MediaPlatform,
} from "@/lib/media-operations-api";
import { operationsApi } from "@/lib/operations-api";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { PlatformAccountPanel } from "@/components/operations/media/platform-account-panel";

const PLATFORMS: readonly MediaPlatform[] = [
  "x",
  "pixiv",
  "dlsite",
  "patreon",
  "youtube",
  "instagram",
];

const PLATFORM_LABELS: Record<MediaPlatform, string> = {
  x: "X",
  pixiv: "Pixiv",
  dlsite: "DLsite",
  patreon: "Patreon",
  youtube: "YouTube",
  instagram: "Instagram",
};

const SAFE_CAPABILITY_KEYS = new Set([
  "identity",
  "publish",
  "media",
  "analytics",
  "text",
  "image",
  "video",
  "schedule",
  "edit",
  "delete",
  "revenue",
  "refresh",
  "revoke",
]);

const SAFE_CAPABILITY_STATUSES = new Set([
  "available",
  "automatable",
  "unavailable",
  "unsupported",
  "manual",
  "unverified",
  "verified",
  "unknown",
]);

type CharacterForm = {
  display_name: string;
  summary: string;
  voice: string;
  audience: string;
  niche: string;
  positioning: string;
  visual_identity: string;
  creative_direction: string;
  allowed_subjects: string;
  prohibited_subjects: string;
  adult_policy: string;
  sensitive_policy: string;
  ip_policy: string;
  disclosure_policy: string;
  monetization_policy: string;
  kpi_objectives: string;
  default_language: string;
  locale: string;
  timezone: string;
  research_policy: string;
  image_production_policy: string;
  video_production_policy: string;
  public_aliases: string;
  platforms: MediaPlatform[];
  content_pillars: string;
};

const CHARACTER_REVISION_FIELDS = [
  "display_name",
  "summary",
  "voice",
  "audience",
  "niche",
  "positioning",
  "visual_identity",
  "creative_direction",
  "allowed_subjects",
  "prohibited_subjects",
  "adult_policy",
  "sensitive_policy",
  "ip_policy",
  "disclosure_policy",
  "monetization_policy",
  "kpi_objectives",
  "default_language",
  "locale",
  "timezone",
  "research_policy",
  "image_production_policy",
  "video_production_policy",
  "public_aliases",
  "platforms",
  "content_pillars",
] as const;

type CharacterRevisionField = (typeof CHARACTER_REVISION_FIELDS)[number];

const CHARACTER_LIST_PAGE_SIZE = 100;
const MAX_AVATAR_BYTES = 25 * 1024 * 1024;

function emptyForm(): CharacterForm {
  return {
    display_name: "",
    summary: "",
    voice: "",
    audience: "",
    niche: "",
    positioning: "",
    visual_identity: "{}",
    creative_direction: "",
    allowed_subjects: "",
    prohibited_subjects: "",
    adult_policy: "",
    sensitive_policy: "",
    ip_policy: "",
    disclosure_policy: "",
    monetization_policy: "{}",
    kpi_objectives: "",
    default_language: "",
    locale: "",
    timezone: "",
    research_policy: "{}",
    image_production_policy: "{}",
    video_production_policy: "{}",
    public_aliases: "",
    platforms: [],
    content_pillars: "",
  };
}

function stringifyPolicy(value: Record<string, unknown> | null | undefined): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

function formFromDetail(detail: MediaCharacterDetail): CharacterForm {
  const revision = detail.current_revision;
  return {
    display_name: revision.display_name ?? "",
    summary: revision.summary ?? "",
    voice: revision.voice ?? "",
    audience: revision.audience ?? "",
    niche: revision.niche ?? "",
    positioning: revision.positioning ?? "",
    visual_identity: stringifyPolicy(revision.visual_identity),
    creative_direction: revision.creative_direction ?? "",
    allowed_subjects: (revision.allowed_subjects ?? []).join("\n"),
    prohibited_subjects: (revision.prohibited_subjects ?? []).join("\n"),
    adult_policy: revision.adult_policy ?? "",
    sensitive_policy: revision.sensitive_policy ?? "",
    ip_policy: revision.ip_policy ?? "",
    disclosure_policy: revision.disclosure_policy ?? "",
    monetization_policy: stringifyPolicy(revision.monetization_policy),
    kpi_objectives: (revision.kpi_objectives ?? []).join("\n"),
    default_language: revision.default_language ?? "",
    locale: revision.locale ?? "",
    timezone: revision.timezone ?? "",
    research_policy: stringifyPolicy(revision.research_policy),
    image_production_policy: stringifyPolicy(revision.image_production_policy),
    video_production_policy: stringifyPolicy(revision.video_production_policy),
    public_aliases: (revision.public_aliases ?? []).join("\n"),
    platforms: [...(revision.platforms ?? [])],
    content_pillars: (revision.content_pillars ?? []).join("\n"),
  };
}

function newIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }

  return `media-character-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function safeUploadLabel(fileName: string): string {
  const baseName = fileName.split(/[\\/]/u).pop() ?? "avatar";
  const cleaned = baseName.replace(/[\u0000-\u001f\u007f]/gu, "").trim();
  return (cleaned || "avatar").slice(0, 255);
}

function readableError(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Character Kernel APIでエラーが発生しました";
}

function splitPillars(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/\r?\n/u)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ).slice(0, 20);
}

function splitList(value: string, maxLength = 40): string[] {
  return Array.from(
    new Set(
      value
        .split(/\r?\n/u)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ).slice(0, maxLength);
}

function parsePolicy(value: string, field: CharacterRevisionField): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed) return {};

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed) as unknown;
  } catch {
    throw new Error(`${field} はJSONオブジェクトで入力してください`);
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${field} はJSONオブジェクトで入力してください`);
  }
  return parsed as Record<string, unknown>;
}

function serializeRevisionField(
  key: CharacterRevisionField,
  form: CharacterForm,
): unknown {
  const value = form[key];
  switch (key) {
    case "display_name":
      return form.display_name.trim();
    case "summary":
    case "voice":
    case "audience":
    case "niche":
    case "positioning":
    case "creative_direction":
    case "adult_policy":
    case "sensitive_policy":
    case "ip_policy":
    case "disclosure_policy":
    case "default_language":
    case "locale":
    case "timezone": {
      const text = form[key].trim();
      return text || null;
    }
    case "allowed_subjects":
      return splitList(form.allowed_subjects);
    case "prohibited_subjects":
      return splitList(form.prohibited_subjects);
    case "kpi_objectives":
      return splitList(form.kpi_objectives, 20);
    case "public_aliases":
      return splitList(form.public_aliases, 20);
    case "content_pillars":
      return splitPillars(form.content_pillars);
    case "platforms":
      return [...form.platforms];
    case "visual_identity":
    case "monetization_policy":
    case "research_policy":
    case "image_production_policy":
    case "video_production_policy":
      return parsePolicy(form[key], key);
    default:
      return value;
  }
}

function comparableFieldValue(key: CharacterRevisionField, form: CharacterForm): unknown {
  try {
    return serializeRevisionField(key, form);
  } catch {
    // Invalid JSON remains dirty and is surfaced by submit validation.
    return form[key];
  }
}

function equalFieldValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function dirtyFieldsFor(base: CharacterForm, current: CharacterForm): Set<CharacterRevisionField> {
  return new Set(
    CHARACTER_REVISION_FIELDS.filter((key) =>
      !equalFieldValue(comparableFieldValue(key, base), comparableFieldValue(key, current)),
    ),
  );
}

function createInputFromForm(form: CharacterForm): CharacterCreateInput {
  const displayName = String(serializeRevisionField("display_name", form));
  const input: Record<string, unknown> = {
    display_name: displayName,
    summary: serializeRevisionField("summary", form),
    voice: serializeRevisionField("voice", form),
    audience: serializeRevisionField("audience", form),
    platforms: serializeRevisionField("platforms", form),
    content_pillars: serializeRevisionField("content_pillars", form),
  };

  // Keep the historical create payload compact for untouched optional fields,
  // while still allowing every advanced revision field to be authored.
  for (const key of CHARACTER_REVISION_FIELDS) {
    if (key === "display_name" || key === "summary" || key === "voice" || key === "audience" || key === "platforms" || key === "content_pillars") continue;
    const value = serializeRevisionField(key, form);
    const isEmpty = value === null || (Array.isArray(value) && value.length === 0) || (typeof value === "object" && value !== null && Object.keys(value).length === 0);
    if (!isEmpty) input[key] = value;
  }

  return input as CharacterCreateInput;
}

function patchInputFromForm(
  detail: MediaCharacterDetail,
  form: CharacterForm,
  dirtyFields: ReadonlySet<CharacterRevisionField>,
): CharacterPatchInput {
  const revision = detail.current_revision;
  const input: Record<string, unknown> = {
    expected_revision_id: revision.id,
    expected_revision_version: revision.version,
    expected_revision_content_hash: revision.content_hash,
  };
  for (const key of CHARACTER_REVISION_FIELDS) {
    if (dirtyFields.has(key)) input[key] = serializeRevisionField(key, form);
  }
  return input as CharacterPatchInput;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ja-JP", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function statusLabel(value: string | null | undefined): string {
  const status = value?.trim().toLowerCase();
  if (!status) return "未設定";

  const labels: Record<string, string> = {
    active: "Active",
    available: "利用可能",
    automatable: "利用可能",
    configured: "設定済み",
    connected: "接続済み",
    disabled: "無効",
    draft: "Draft",
    pending: "保留",
    triaged: "保留",
    discovered: "保留",
    pending_review: "レビュー待ち",
    proposed: "提案",
    running: "実行中",
    succeeded: "成功",
    failed: "失敗",
    unavailable: "利用不可",
    unsupported: "非対応",
    manual: "手動",
    unverified: "未検証",
    verified: "検証済み",
    unknown: "不明",
    expired: "期限切れ",
    accepted: "採用",
    rejected: "却下",
  };

  return labels[status] ?? value ?? "未設定";
}

function StatusPill({ status }: { status: string | null | undefined }) {
  return (
    <span
      className="inline-flex items-center rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
      data-character-status={status ?? "unknown"}
    >
      {statusLabel(status)}
    </span>
  );
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;

  return (
    <div
      role="alert"
      className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
    >
      {readableError(error)}
    </div>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-7 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function CharacterCard({
  character,
  selected,
  onSelect,
}: {
  character: MediaCharacter;
  selected: boolean;
  onSelect: () => void;
}) {
  const revision = character.current_revision;

  return (
    <button
      type="button"
      data-testid={`character-card-${character.id}`}
      aria-pressed={selected}
      onClick={onSelect}
      className={`group rounded-lg border p-3 text-left transition-colors ${
        selected
          ? "border-primary bg-primary/5"
          : "border-border bg-card/40 hover:bg-muted/40"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
            <Users className="size-3.5" aria-hidden="true" />
          </span>
          <span className="min-w-0 truncate text-sm font-semibold">
            {revision.display_name}
          </span>
        </div>
        <StatusPill status={character.state} />
      </div>

      <p className="mt-2 line-clamp-2 min-h-8 text-xs text-muted-foreground">
        {revision.summary || "概要はまだ登録されていません。"}
      </p>

      <div className="mt-2 flex flex-wrap gap-1.5">
        {(revision.platforms ?? []).slice(0, 4).map((platform) => (
          <span
            key={platform}
            className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] text-muted-foreground"
          >
            {PLATFORM_LABELS[platform]}
          </span>
        ))}
      </div>

      <span className="mt-3 block text-[10px] text-muted-foreground group-hover:text-foreground">
        Character ID: {character.id}
      </span>
    </button>
  );
}

function DashboardSection({
  title,
  icon: Icon,
  testId,
  children,
}: {
  title: string;
  icon: typeof Users;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className="rounded-lg border border-border/70 bg-background/40 p-3"
      data-testid={testId}
    >
      <div className="flex items-center gap-2">
        <Icon className="size-3.5 text-primary" aria-hidden="true" />
        <h4 className="text-xs font-semibold">{title}</h4>
      </div>
      <div className="mt-2">{children}</div>
    </section>
  );
}

function ConnectedAccounts({
  accounts,
}: {
  accounts: CharacterDashboardConnectedAccount[];
}) {
  if (!accounts.length) return <p className="text-xs text-muted-foreground">接続済みアカウントはありません。</p>;

  return (
    <ul className="space-y-1.5">
      {accounts.map((account) => {
        const capabilityEntries = Object.entries(account.capabilities ?? {})
          .filter(([key, value]) =>
            SAFE_CAPABILITY_KEYS.has(key) &&
            typeof value === "string" &&
            SAFE_CAPABILITY_STATUSES.has(value.trim().toLowerCase()),
          )
          .slice(0, 4);
        const observedCapabilityEntries = Object.entries(account.observed_capabilities ?? {})
          .filter(([key, value]) =>
            SAFE_CAPABILITY_KEYS.has(key) &&
            typeof value === "string" &&
            SAFE_CAPABILITY_STATUSES.has(value.trim().toLowerCase()),
          )
          .slice(0, 4);
        return (
          <li
            key={account.id}
            className="rounded-md border border-border/60 px-2.5 py-2 text-xs"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="min-w-0 truncate">
                {PLATFORM_LABELS[account.platform as MediaPlatform] ?? account.platform}
                {account.account_ref ? ` · ${account.account_ref}` : ""}
              </span>
              <StatusPill status={account.capability_status ?? account.connection_status ?? account.status} />
            </div>
            {capabilityEntries.length ? (
              <p className="mt-1 line-clamp-2 text-[10px] text-muted-foreground">
                {capabilityEntries.map(([key, value]) => `${key}: ${statusLabel(value)}`).join(" · ")}
                {account.adapter_ready === true ? " · adapter: ready" : ""}
              </p>
            ) : account.adapter_ready === true ? (
              <p className="mt-1 text-[10px] text-muted-foreground">adapter: ready</p>
            ) : null}
            {observedCapabilityEntries.length ? (
              <p className="mt-1 line-clamp-2 text-[10px] text-muted-foreground">
                観測: {observedCapabilityEntries.map(([key, value]) => `${key}: ${statusLabel(value)}`).join(" · ")}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function ResearchCandidates({
  candidates,
}: {
  candidates: CharacterDashboardResearchCandidate[];
}) {
  if (!candidates.length) return <p className="text-xs text-muted-foreground">Research candidateはありません。</p>;

  return (
    <ul className="space-y-2">
      {candidates.map((candidate) => (
        <li key={candidate.id} className="rounded-md border border-border/60 px-2.5 py-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium">{candidate.title}</span>
            <StatusPill status={candidate.review_state ?? candidate.status} />
          </div>
          {candidate.summary ? (
            <p className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">
              {candidate.summary}
            </p>
          ) : null}
          {candidate.reason ? (
            <p className="mt-1 line-clamp-2 text-[10px] text-muted-foreground">
              判定理由: {candidate.reason}
            </p>
          ) : null}
          <p className="mt-1 text-[10px] text-muted-foreground">
            発見 {formatDate(candidate.discovered_at)}
            {candidate.expires_at ? ` · 期限 ${formatDate(candidate.expires_at)}` : ""}
            {candidate.decision_version ? ` · decision v${candidate.decision_version}` : ""}
          </p>
        </li>
      ))}
    </ul>
  );
}

function GenerationSummary({
  generation,
}: {
  generation: MediaCharacterDashboard["generation"];
}) {
  const recipes: CharacterDashboardRecipe[] = generation?.recipes ?? [];
  const runs: CharacterDashboardRun[] = generation?.runs ?? [];

  if (!recipes.length && !runs.length) {
    return <p className="text-xs text-muted-foreground">Generation履歴はありません。</p>;
  }

  return (
    <div className="space-y-2">
      {recipes.length ? (
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Recipes</p>
          <ul className="mt-1 space-y-1">
            {recipes.map((recipe) => (
              <li key={recipe.id} className="flex items-center justify-between gap-2 text-xs">
                <span className="truncate">{recipe.name}</span>
                <span className="shrink-0 text-[10px] text-muted-foreground">{formatDate(recipe.created_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {runs.length ? (
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Runs</p>
          <ul className="mt-1 space-y-1">
            {runs.map((run) => (
              <li key={run.id} className="flex items-center justify-between gap-2 text-xs">
                <span className="font-mono text-[10px]">{run.id}</span>
                <span className="flex shrink-0 items-center gap-1.5">
                  <StatusPill status={run.status} />
                  <span className="text-[10px] text-muted-foreground">{formatDate(run.finished_at ?? run.started_at)}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function CalendarSummary({
  entries,
}: {
  entries: CharacterDashboardCalendarEntry[];
}) {
  if (!entries.length) return <p className="text-xs text-muted-foreground">予定はありません。</p>;

  return (
    <ul className="space-y-1.5">
      {entries.map((entry) => (
        <li key={entry.id} className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="min-w-0 truncate">
            <span className="mr-1.5 rounded border border-border px-1 py-0.5 text-[10px] text-muted-foreground">{entry.kind}</span>
            {entry.title}
          </span>
          <span className="flex shrink-0 items-center gap-1.5">
            <StatusPill status={entry.status} />
            <span className="text-[10px] text-muted-foreground">{formatDate(entry.starts_at)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function LearningSummary({
  learning,
}: {
  learning: MediaCharacterDashboard["learning"];
}) {
  const items: CharacterDashboardLearningItem[] = learning?.items ?? [];

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5">{learning?.count ?? 0}件</span>
        <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5">レビュー待ち {learning?.pending_review_count ?? 0}件</span>
      </div>
      {items.length ? (
        <ul className="space-y-1.5">
          {items.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="min-w-0 truncate">{item.title}</span>
              <span className="flex shrink-0 items-center gap-1.5"><StatusPill status={item.status} /><span className="text-[10px] text-muted-foreground">{formatDate(item.created_at)}</span></span>
            </li>
          ))}
        </ul>
      ) : <p className="text-xs text-muted-foreground">Learning proposalはありません。</p>}
    </div>
  );
}

const MAX_DASHBOARD_BUCKETS = 20;
const MAX_DASHBOARD_METRIC_KEYS = 8;
const MAX_DASHBOARD_TEXT_LENGTH = 164;
const SAFE_DASHBOARD_METRIC_KEYS = new Set([
  "impressions",
  "reach",
  "views",
  "likes",
  "comments",
  "shares",
  "saves",
  "clicks",
  "conversions",
  "followers",
  "watch_time_seconds",
  "engagement_rate",
  "click_through_rate",
  "conversion_rate",
  "sample_size",
  "gross_revenue",
  "net_revenue",
  "refunds",
  "cost",
]);

function safeDashboardNumber(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (Math.abs(value) > 1_000_000_000_000) return null;
  return value;
}

function safeDashboardText(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const normalized = value
    .replace(/[\u0000-\u001f\u007f]/gu, "")
    .trim();
  if (!normalized || normalized.length > MAX_DASHBOARD_TEXT_LENGTH) {
    return fallback;
  }
  return normalized;
}

function shortDashboardRef(value: unknown, fallback: string): string {
  const text = safeDashboardText(value, fallback);
  if (text === fallback || text.length <= 40) return text;
  return `${text.slice(0, 36)}…`;
}

function dashboardPageItems<T>(
  page: CharacterDashboardPage<T> | null | undefined,
): T[] {
  return Array.isArray(page?.items) ? page.items.slice(0, 100) : [];
}

function formatMetricEntries(metrics: Record<string, number> | undefined): string {
  if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return "値なし";
  const entries = Object.entries(metrics)
    .map(([key, value]) => [safeDashboardText(key, ""), safeDashboardNumber(value)] as const)
    .filter(([key, value]) => Boolean(key) && SAFE_DASHBOARD_METRIC_KEYS.has(key) && value !== null)
    .slice(0, MAX_DASHBOARD_METRIC_KEYS);
  if (!entries.length) return "値なし";
  return entries
    .map(([key, value]) => `${key}: ${new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 2 }).format(value as number)}`)
    .join(" · ");
}

function safeDashboardMetricMap(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const result: Record<string, number> = {};
  for (const [key, rawValue] of Object.entries(value)) {
    if (!SAFE_DASHBOARD_METRIC_KEYS.has(key)) continue;
    const number = safeDashboardNumber(rawValue);
    if (number !== null) result[key] = number;
  }
  return result;
}

type DashboardMetricBucket = {
  key: string;
  label: string;
  snapshotCount: number;
  metrics: Record<string, number>;
};

type MetricDimension = "platform" | "account" | "content";

function dashboardAccountMap(
  accounts: CharacterDashboardConnectedAccount[],
): Map<string, CharacterDashboardConnectedAccount> {
  const result = new Map<string, CharacterDashboardConnectedAccount>();
  for (const account of accounts) {
    const ref = safeDashboardText(account.account_ref, "");
    if (ref) result.set(ref, account);
    const id = safeDashboardText(account.id, "");
    if (id) result.set(id, account);
  }
  return result;
}

function metricDimension(
  item: CharacterDashboardMetricItem,
  dimension: MetricDimension,
  accountsByRef: Map<string, CharacterDashboardConnectedAccount>,
): { key: string; label: string } {
  const accountRef = safeDashboardText(item.platform_account_ref, "");
  const account = accountRef ? accountsByRef.get(accountRef) : undefined;
  if (dimension === "platform") {
    const platform = safeDashboardText(item.platform ?? account?.platform, "");
    return platform
      ? { key: platform, label: PLATFORM_LABELS[platform as MediaPlatform] ?? platform }
      : { key: "__unknown_platform", label: "媒体未設定" };
  }
  if (dimension === "account") {
    return accountRef
      ? { key: accountRef, label: shortDashboardRef(accountRef, "アカウント未設定") }
      : { key: "__unknown_account", label: "アカウント未設定" };
  }
  const contentRef = safeDashboardText(item.content_variant_ref, "");
  return contentRef
    ? { key: contentRef, label: shortDashboardRef(contentRef, "コンテンツ未設定") }
    : { key: "__unknown_content", label: "コンテンツ未設定" };
}

function metricBuckets(
  items: CharacterDashboardMetricItem[],
  dimension: MetricDimension,
  accountsByRef: Map<string, CharacterDashboardConnectedAccount>,
): DashboardMetricBucket[] {
  const buckets = new Map<string, DashboardMetricBucket>();
  for (const item of items) {
    const { key, label } = metricDimension(item, dimension, accountsByRef);
    const bucket = buckets.get(key) ?? { key, label, snapshotCount: 0, metrics: {} };
    bucket.snapshotCount += 1;
    const rawMetrics = item.metrics;
    if (!rawMetrics || typeof rawMetrics !== "object" || Array.isArray(rawMetrics)) {
      buckets.set(key, bucket);
      continue;
    }
    for (const [metricKey, rawValue] of Object.entries(rawMetrics)) {
      const normalizedKey = safeDashboardText(metricKey, "");
      const value = safeDashboardNumber(rawValue);
      if (!normalizedKey || !SAFE_DASHBOARD_METRIC_KEYS.has(normalizedKey) || value === null) continue;
      const next = (bucket.metrics[normalizedKey] ?? 0) + value;
      if (Number.isFinite(next) && Math.abs(next) <= 1_000_000_000_000) {
        bucket.metrics[normalizedKey] = next;
      }
    }
    buckets.set(key, bucket);
  }
  return Array.from(buckets.values())
    .sort((left, right) => right.snapshotCount - left.snapshotCount || left.label.localeCompare(right.label, "ja"))
    .slice(0, MAX_DASHBOARD_BUCKETS);
}

type DashboardRevenueBucket = {
  key: string;
  label: string;
  eventCount: number;
  gross: number;
  net: number;
};

type RevenueDimension = "platform" | "account" | "currency" | "content";

function revenueDimension(
  item: CharacterDashboardRevenueItem,
  dimension: RevenueDimension,
  accountsByRef: Map<string, CharacterDashboardConnectedAccount>,
): { key: string; label: string } {
  const currency = safeDashboardText(item.currency, "").toUpperCase();
  const withCurrency = (key: string, label: string) =>
    currency ? { key: `${key}|${currency}`, label: `${label} · ${currency}` } : { key, label };
  const accountRef = safeDashboardText(item.platform_account_ref, "");
  const account = accountRef ? accountsByRef.get(accountRef) : undefined;
  if (dimension === "platform") {
    const platform = safeDashboardText(item.platform ?? account?.platform, "");
    return platform
      ? withCurrency(platform, PLATFORM_LABELS[platform as MediaPlatform] ?? platform)
      : withCurrency("__unknown_platform", "媒体未設定");
  }
  if (dimension === "account") {
    return accountRef
      ? withCurrency(accountRef, shortDashboardRef(accountRef, "アカウント未設定"))
      : withCurrency("__unknown_account", "アカウント未設定");
  }
  if (dimension === "currency") {
    return currency
      ? { key: currency, label: currency }
      : { key: "__unknown_currency", label: "通貨未設定" };
  }
  const contentRef = safeDashboardText(item.content_ref, "");
  return contentRef
    ? withCurrency(contentRef, shortDashboardRef(contentRef, "コンテンツ未設定"))
    : withCurrency("__unknown_content", "コンテンツ未設定");
}

function revenueBuckets(
  items: CharacterDashboardRevenueItem[],
  dimension: RevenueDimension,
  accountsByRef: Map<string, CharacterDashboardConnectedAccount>,
): DashboardRevenueBucket[] {
  const buckets = new Map<string, DashboardRevenueBucket>();
  for (const item of items) {
    const { key, label } = revenueDimension(item, dimension, accountsByRef);
    const gross = safeDashboardNumber(item.gross_amount) ?? 0;
    const net = safeDashboardNumber(item.net_amount) ?? 0;
    const bucket = buckets.get(key) ?? { key, label, eventCount: 0, gross: 0, net: 0 };
    bucket.eventCount += 1;
    bucket.gross += gross;
    bucket.net += net;
    if (!Number.isFinite(bucket.gross) || Math.abs(bucket.gross) > 1_000_000_000_000) bucket.gross = 0;
    if (!Number.isFinite(bucket.net) || Math.abs(bucket.net) > 1_000_000_000_000) bucket.net = 0;
    buckets.set(key, bucket);
  }
  return Array.from(buckets.values())
    .sort((left, right) => right.eventCount - left.eventCount || left.label.localeCompare(right.label, "ja"))
    .slice(0, MAX_DASHBOARD_BUCKETS);
}

function MetricBucketList({ buckets }: { buckets: DashboardMetricBucket[] }) {
  if (!buckets.length) return <p className="text-[11px] text-muted-foreground">該当する観測はありません。</p>;
  return (
    <ul className="space-y-1.5">
      {buckets.map((bucket) => (
        <li key={bucket.key} className="rounded-md border border-border/60 px-2.5 py-2">
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="min-w-0 truncate font-medium">{bucket.label}</span>
            <span className="shrink-0 text-[10px] text-muted-foreground">{bucket.snapshotCount}件</span>
          </div>
          <p className="mt-1 text-[10px] text-muted-foreground">{formatMetricEntries(bucket.metrics)}</p>
        </li>
      ))}
    </ul>
  );
}

function RevenueBucketList({ buckets }: { buckets: DashboardRevenueBucket[] }) {
  if (!buckets.length) return <p className="text-[11px] text-muted-foreground">該当するRevenue eventはありません。</p>;
  return (
    <ul className="space-y-1.5">
      {buckets.map((bucket) => (
        <li key={bucket.key} className="rounded-md border border-border/60 px-2.5 py-2">
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="min-w-0 truncate font-medium">{bucket.label}</span>
            <span className="shrink-0 text-[10px] text-muted-foreground">{bucket.eventCount}件</span>
          </div>
          <p className="mt-1 text-[10px] tabular-nums text-muted-foreground">
            Gross {new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 2 }).format(bucket.gross)} · Net {new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 2 }).format(bucket.net)}
          </p>
        </li>
      ))}
    </ul>
  );
}

function CharacterResultsSummary({ dashboard }: { dashboard: MediaCharacterDashboard }) {
  const results = dashboard.results ?? { snapshot_count: 0, metrics: {}, last_observed_at: null };
  const accountsByRef = dashboardAccountMap(dashboard.connected_accounts ?? []);
  const metricPage = dashboard.metrics ?? dashboard.metrics_page;
  const revenuePage = dashboard.revenue ?? dashboard.revenue_page;
  const experimentPage = dashboard.experiments ?? dashboard.experiments_page;
  const metricItems = dashboardPageItems(metricPage);
  const revenueItems = dashboardPageItems(revenuePage);
  const experimentItems = dashboardPageItems(experimentPage);
  const fallbackMetrics = safeDashboardMetricMap(results.metrics);
  const metricCount = Object.keys(fallbackMetrics).length;
  const metricTotal = metricPage?.count ?? results.snapshot_count;
  const revenueTotal = revenuePage?.count ?? 0;
  const experimentTotal = experimentPage?.count ?? 0;
  const dimensions: Array<{ key: MetricDimension; label: string }> = [
    { key: "platform", label: "媒体" },
    { key: "account", label: "アカウント" },
    { key: "content", label: "コンテンツ" },
  ];
  const revenueDimensions: Array<{ key: RevenueDimension; label: string }> = [
    { key: "platform", label: "媒体" },
    { key: "account", label: "アカウント" },
    { key: "currency", label: "通貨" },
    { key: "content", label: "コンテンツ" },
  ];

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div><p className="text-[10px] text-muted-foreground">Snapshots</p><p className="mt-0.5 font-semibold">{results.snapshot_count ?? 0}</p></div>
        <div><p className="text-[10px] text-muted-foreground">Metrics</p><p className="mt-0.5 font-semibold">{metricCount}</p></div>
        <div><p className="text-[10px] text-muted-foreground">最終観測</p><p className="mt-0.5 font-semibold">{formatDate(results.last_observed_at)}</p></div>
      </div>

      <section data-testid="character-dashboard-metrics" className="rounded-md border border-border/60 p-2.5">
        <div className="flex items-center justify-between gap-2">
          <h5 className="flex items-center gap-1.5 text-xs font-semibold"><BarChart3 className="size-3" aria-hidden="true" />Metrics（媒体 / アカウント / コンテンツ）</h5>
          <span className="text-[10px] text-muted-foreground">{metricTotal}件{metricPage?.has_more ? "（続きあり）" : ""}</span>
        </div>
        {metricItems.length ? (
          <div className="mt-2 grid gap-2 md:grid-cols-3">
            {dimensions.map(({ key, label }) => (
              <div key={key}>
                <p className="mb-1 text-[10px] font-medium text-muted-foreground">{label}別</p>
                <MetricBucketList buckets={metricBuckets(metricItems, key, accountsByRef)} />
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-2 text-[11px] text-muted-foreground">
            {metricCount ? `集計値: ${formatMetricEntries(fallbackMetrics)}` : "詳細なMetric snapshotはまだありません。"}
          </p>
        )}
      </section>

      <section data-testid="character-dashboard-revenue" className="rounded-md border border-border/60 p-2.5">
        <div className="flex items-center justify-between gap-2">
          <h5 className="flex items-center gap-1.5 text-xs font-semibold"><CircleDollarSign className="size-3" aria-hidden="true" />Revenue（媒体 / アカウント / 通貨 / コンテンツ）</h5>
          <span className="text-[10px] text-muted-foreground">{revenueTotal}件{revenuePage?.has_more ? "（続きあり）" : ""}</span>
        </div>
        {revenueItems.length ? (
          <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            {revenueDimensions.map(({ key, label }) => (
              <div key={key}>
                <p className="mb-1 text-[10px] font-medium text-muted-foreground">{label}別</p>
                <RevenueBucketList buckets={revenueBuckets(revenueItems, key, accountsByRef)} />
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-2 text-[11px] text-muted-foreground">Characterに紐づくRevenue eventはまだありません。</p>
        )}
      </section>

      <section data-testid="character-dashboard-experiments" className="rounded-md border border-border/60 p-2.5">
        <div className="flex items-center justify-between gap-2">
          <h5 className="flex items-center gap-1.5 text-xs font-semibold"><FlaskConical className="size-3" aria-hidden="true" />Experiments</h5>
          <span className="text-[10px] text-muted-foreground">{experimentTotal}件{experimentPage?.has_more ? "（続きあり）" : ""}</span>
        </div>
        {experimentItems.length ? (
          <ul className="mt-2 space-y-1.5">
            {experimentItems.map((experiment) => (
              <li key={experiment.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/60 px-2.5 py-2 text-xs">
                <span className="min-w-0 truncate font-medium">{safeDashboardText(experiment.name, "Experiment")}</span>
                <span className="flex shrink-0 items-center gap-1.5"><StatusPill status={experiment.status} /><span className="text-[10px] text-muted-foreground">結果 {Array.isArray(experiment.results) ? experiment.results.length : 0}件</span></span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-[11px] text-muted-foreground">Characterに紐づくExperimentはまだありません。</p>
        )}
      </section>
    </div>
  );
}

function CharacterDashboard({
  dashboard,
}: {
  dashboard: MediaCharacterDashboard;
}) {
  return (
    <div className="space-y-3" data-testid="character-dashboard">
      <div className="grid gap-3 sm:grid-cols-2">
        <DashboardSection title="接続アカウント" icon={CircleDot} testId="character-dashboard-connected-accounts">
          <ConnectedAccounts accounts={dashboard.connected_accounts ?? []} />
        </DashboardSection>
        <DashboardSection title="Research candidates" icon={Sparkles} testId="character-dashboard-research-candidates">
          <ResearchCandidates candidates={dashboard.research_candidates ?? []} />
        </DashboardSection>
        <DashboardSection title="Generation" icon={FileText} testId="character-dashboard-generation">
          <GenerationSummary generation={dashboard.generation ?? { recipes: [], runs: [] }} />
        </DashboardSection>
        <DashboardSection title="カレンダー" icon={CalendarDays} testId="character-dashboard-calendar">
          <CalendarSummary entries={dashboard.calendar ?? []} />
        </DashboardSection>
        <DashboardSection title="Results" icon={BarChart3} testId="character-dashboard-results">
          <CharacterResultsSummary dashboard={dashboard} />
        </DashboardSection>
        <DashboardSection title="Learning" icon={Check} testId="character-dashboard-learning">
          <LearningSummary learning={dashboard.learning ?? { count: 0, pending_review_count: 0, items: [] }} />
        </DashboardSection>
      </div>
    </div>
  );
}

function CharacterFormPanel({
  detail,
  form,
  editing,
  saving,
  dirtyFields,
  onChange,
  onSubmit,
  onCancel,
}: {
  detail: MediaCharacterDetail | null;
  form: CharacterForm;
  editing: boolean;
  saving: boolean;
  dirtyFields: ReadonlySet<CharacterRevisionField>;
  onChange: (next: CharacterForm) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
}) {
  const textField = (
    key: Exclude<CharacterRevisionField, "platforms" | "visual_identity" | "monetization_policy" | "research_policy" | "image_production_policy" | "video_production_policy" | "allowed_subjects" | "prohibited_subjects" | "kpi_objectives" | "public_aliases" | "content_pillars">,
    label: string,
    options: { multiline?: boolean; maxLength?: number } = {},
  ) => {
    const id = key === "display_name"
      ? "media-character-name"
      : `media-character-${key.replaceAll("_", "-")}`;
    const value = form[key] as string;
    const control = options.multiline ? (
      <Textarea
        id={id}
        data-field-key={key}
        value={value}
        onChange={(event) => onChange({ ...form, [key]: event.target.value })}
        maxLength={options.maxLength}
        rows={3}
      />
    ) : (
      <Input
        id={id}
        data-field-key={key}
        value={value}
        onChange={(event) => onChange({ ...form, [key]: event.target.value })}
        maxLength={options.maxLength}
      />
    );
    return (
      <div key={key} className="space-y-1.5">
        <label htmlFor={id} className="flex items-center gap-1.5 text-xs font-medium">
          {label}
          {dirtyFields.has(key) ? <span className="text-primary" aria-label="未保存">●</span> : null}
        </label>
        {control}
      </div>
    );
  };

  const listField = (
    key: "allowed_subjects" | "prohibited_subjects" | "kpi_objectives" | "public_aliases" | "content_pillars",
    label: string,
    rows = 3,
  ) => {
    const id = key === "content_pillars"
      ? "media-character-pillars"
      : `media-character-${key.replaceAll("_", "-")}`;
    return (
      <div key={key} className="space-y-1.5">
        <label htmlFor={id} className="flex items-center gap-1.5 text-xs font-medium">
          {label}
          {dirtyFields.has(key) ? <span className="text-primary" aria-label="未保存">●</span> : null}
        </label>
        <Textarea
          id={id}
          data-field-key={key}
          value={form[key]}
          onChange={(event) => onChange({ ...form, [key]: event.target.value })}
          placeholder="1行につき1項目"
          rows={rows}
        />
      </div>
    );
  };

  const jsonField = (
    key: "visual_identity" | "monetization_policy" | "research_policy" | "image_production_policy" | "video_production_policy",
    label: string,
  ) => {
    const id = `media-character-${key.replaceAll("_", "-")}`;
    return (
      <div key={key} className="space-y-1.5">
        <label htmlFor={id} className="flex items-center gap-1.5 text-xs font-medium">
          {label}
          {dirtyFields.has(key) ? <span className="text-primary" aria-label="未保存">●</span> : null}
        </label>
        <Textarea
          id={id}
          data-field-key={key}
          value={form[key]}
          onChange={(event) => onChange({ ...form, [key]: event.target.value })}
          rows={4}
          spellCheck={false}
          className="font-mono text-xs"
        />
      </div>
    );
  };

  return (
    <Card size="sm" data-testid="character-form-card">
      <CardHeader className="border-b border-border/70">
        <CardTitle className="flex items-center gap-2 text-sm">
          {editing ? <Pencil className="size-3.5" /> : <Plus className="size-3.5" />}
          {editing ? "Characterを編集" : "Characterを作成"}
        </CardTitle>
        <CardDescription>
          {editing
            ? "更新は新しいRevisionとして保存され、過去の内容は上書きされません。"
            : "必要な情報だけ入力してCharacterを追加します。作成時に初期データは生成しません。"}
        </CardDescription>
      </CardHeader>

      <CardContent className="pt-4">
        <form className="space-y-3" aria-label="Character form" data-testid="character-form" onSubmit={onSubmit}>
          <fieldset disabled={saving} className="space-y-3">
          <details open className="rounded-md border border-border/70 px-3 py-2">
            <summary className="cursor-pointer text-xs font-semibold">基本情報</summary>
            <div className="mt-3 space-y-3">
              {textField("display_name", "表示名", { maxLength: 120 })}
              {textField("summary", "概要", { multiline: true, maxLength: 4000 })}
              <div className="grid gap-3 sm:grid-cols-2">
                {textField("voice", "文体・話し方", { multiline: true, maxLength: 4000 })}
                {textField("audience", "想定読者", { multiline: true, maxLength: 4000 })}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {textField("niche", "Niche", { multiline: true, maxLength: 1000 })}
                {textField("positioning", "Positioning", { multiline: true, maxLength: 2000 })}
              </div>
              {textField("creative_direction", "Creative direction", { multiline: true, maxLength: 4000 })}
            </div>
          </details>

          <details className="rounded-md border border-border/70 px-3 py-2">
            <summary className="cursor-pointer text-xs font-semibold">発信設定</summary>
            <div className="mt-3 space-y-3">
              <fieldset className="space-y-2">
                <legend className="flex items-center gap-1.5 text-xs font-medium">
                  プラットフォーム
                  {dirtyFields.has("platforms") ? <span className="text-primary" aria-label="未保存">●</span> : null}
                </legend>
                <div className="grid grid-cols-2 gap-2">
                  {PLATFORMS.map((platform) => (
                    <label key={platform} className="flex items-center gap-2 rounded-md border border-border/70 px-2.5 py-2 text-xs">
                      <Checkbox
                        checked={form.platforms.includes(platform)}
                        aria-label={`platform-${platform}`}
                        onCheckedChange={(checked) => onChange({ ...form, platforms: checked === true ? Array.from(new Set([...form.platforms, platform])) : form.platforms.filter((item) => item !== platform) })}
                      />
                      {PLATFORM_LABELS[platform]}
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="grid gap-3 sm:grid-cols-2">
                {listField("content_pillars", "コンテンツ軸", 4)}
                {listField("public_aliases", "Public aliases")}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {listField("allowed_subjects", "Allowed subjects")}
                {listField("prohibited_subjects", "Prohibited subjects")}
              </div>
              {listField("kpi_objectives", "KPI objectives")}
            </div>
          </details>

          <details className="rounded-md border border-border/70 px-3 py-2">
            <summary className="cursor-pointer text-xs font-semibold">ポリシー・ローカライズ</summary>
            <div className="mt-3 space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                {textField("adult_policy", "Adult policy")}
                {textField("sensitive_policy", "Sensitive policy")}
                {textField("ip_policy", "IP policy")}
                {textField("disclosure_policy", "Disclosure policy")}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {textField("default_language", "Default language")}
                {textField("locale", "Locale")}
                {textField("timezone", "Timezone")}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {jsonField("visual_identity", "Visual identity (JSON)")}
                {jsonField("monetization_policy", "Monetization policy (JSON)")}
                {jsonField("research_policy", "Research policy (JSON)")}
                {jsonField("image_production_policy", "Image production policy (JSON)")}
                {jsonField("video_production_policy", "Video production policy (JSON)")}
              </div>
            </div>
          </details>

          <div className="flex justify-end gap-2">
            {editing ? <Button type="button" variant="ghost" size="sm" onClick={onCancel}>キャンセル</Button> : null}
            <Button type="submit" size="sm" disabled={saving || (editing && dirtyFields.size === 0)}>
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
              {editing ? "更新" : "作成"}
            </Button>
          </div>
          </fieldset>
        </form>

        {detail ? <p className="mt-3 text-[10px] text-muted-foreground">更新対象: {detail.id}</p> : null}
      </CardContent>
    </Card>
  );
}

export function MediaPersonaPanel() {
  const [characters, setCharacters] = useState<MediaCharacter[]>([]);
  const [selectedCharacterId, setSelectedCharacterId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MediaCharacterDetail | null>(null);
  const [dashboard, setDashboard] = useState<MediaCharacterDashboard | null>(null);
  const [form, setForm] = useState<CharacterForm>(emptyForm);
  const [baseForm, setBaseForm] = useState<CharacterForm>(emptyForm);
  const [dirtyFields, setDirtyFields] = useState<Set<CharacterRevisionField>>(new Set());
  const [conflict, setConflict] = useState<{ canonical: MediaCharacterDetail; dirtyFields: CharacterRevisionField[] } | null>(null);
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [resources, setResources] = useState<MediaPersonaResource[]>([]);
  const [resourceLoading, setResourceLoading] = useState(false);
  const [avatarUploading, setAvatarUploading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const characterRequestRef = useRef(0);
  const resourceRequestRef = useRef(0);
  const activeCharacterIdRef = useRef<string | null>(null);
  const listRequestRef = useRef(0);
  const avatarInputRef = useRef<HTMLInputElement>(null);
  const charactersRef = useRef<MediaCharacter[]>([]);
  const nextOffsetRef = useRef(0);
  const searchRef = useRef("");

  const loadCharacters = useCallback(async ({ reset = true, searchTerm } : { reset?: boolean; searchTerm?: string } = {}) => {
    const requestId = ++listRequestRef.current;
    if (reset) setLoading(true);
    else setLoadingMore(true);
    setError(null);

    try {
      const effectiveSearch = searchTerm ?? searchRef.current;
      const offset = reset ? 0 : nextOffsetRef.current;
      const nextCharacters = await mediaOperationsApi.listCharacters({
        search: effectiveSearch.trim() || undefined,
        limit: CHARACTER_LIST_PAGE_SIZE,
        offset,
      });
      if (requestId !== listRequestRef.current) return;
      const deduped = Array.from(new Map((reset ? nextCharacters : [...charactersRef.current, ...nextCharacters]).map((item) => [item.id, item])).values());
      charactersRef.current = deduped;
      setCharacters(deduped);
      searchRef.current = effectiveSearch;
      nextOffsetRef.current = offset + nextCharacters.length;
      setHasMore(nextCharacters.length >= CHARACTER_LIST_PAGE_SIZE);
      if (reset) {
        setSelectedCharacterId((current) => {
          if (current && deduped.some((item) => item.id === current)) return current;
          const nextId = deduped[0]?.id ?? null;
          if (!nextId) {
            setDetail(null);
            setDashboard(null);
            setEditing(false);
            setResources([]);
          }
          return nextId;
        });
      }
    } catch (loadError) {
      if (requestId === listRequestRef.current) setError(loadError);
    } finally {
      if (requestId === listRequestRef.current) {
        if (reset) setLoading(false);
        else setLoadingMore(false);
      }
    }
  }, []);

  const loadResources = useCallback(async (characterId: string) => {
    const requestId = ++resourceRequestRef.current;
    setResourceLoading(true);
    try {
      const nextResources = await mediaOperationsApi.listPersonaResources(characterId, { limit: 100, offset: 0 });
      if (requestId !== resourceRequestRef.current || activeCharacterIdRef.current !== characterId) return;
      setResources(nextResources ?? []);
    } catch (resourceError) {
      if (requestId === resourceRequestRef.current && activeCharacterIdRef.current === characterId) {
        setError(resourceError);
      }
    } finally {
      if (requestId === resourceRequestRef.current && activeCharacterIdRef.current === characterId) {
        setResourceLoading(false);
      }
    }
  }, []);

  const loadCharacter = useCallback(async (characterId: string) => {
    const requestId = ++characterRequestRef.current;
    activeCharacterIdRef.current = characterId;
    setDetailLoading(true);
    setError(null);

    try {
      const [nextDetail, nextDashboard] = await Promise.all([
        mediaOperationsApi.getCharacter(characterId),
        mediaOperationsApi.getCharacterDashboard(characterId),
      ]);
      if (requestId !== characterRequestRef.current) return;
      setDetail(nextDetail);
      setDashboard(nextDashboard);
      const nextForm = formFromDetail(nextDetail);
      setForm(nextForm);
      setBaseForm(nextForm);
      setDirtyFields(new Set());
      setConflict(null);
      setEditing(true);
      void loadResources(characterId);
    } catch (loadError) {
      if (requestId === characterRequestRef.current) setError(loadError);
    } finally {
      if (requestId === characterRequestRef.current) setDetailLoading(false);
    }
  }, [loadResources]);

  useEffect(() => {
    const task = window.setTimeout(() => {
      void loadCharacters();
    }, 0);
    return () => window.clearTimeout(task);
  }, [loadCharacters]);

  useEffect(() => {
    if (!selectedCharacterId) return;
    const task = window.setTimeout(() => {
      void loadCharacter(selectedCharacterId);
    }, 0);
    return () => window.clearTimeout(task);
  }, [loadCharacter, selectedCharacterId]);

  const beginCreate = () => {
    characterRequestRef.current += 1;
    resourceRequestRef.current += 1;
    activeCharacterIdRef.current = null;
    setSelectedCharacterId(null);
    setDetail(null);
    setDashboard(null);
    const nextForm = emptyForm();
    setForm(nextForm);
    setBaseForm(nextForm);
    setDirtyFields(new Set());
    setConflict(null);
    setResources([]);
    setEditing(false);
    setError(null);
  };

  const updateForm = (next: CharacterForm) => {
    setForm(next);
    setDirtyFields(dirtyFieldsFor(baseForm, next));
    setConflict(null);
    setError(null);
  };

  const refreshCanonicalAfterConflict = async (
    characterId: string,
    pendingFields: ReadonlySet<CharacterRevisionField>,
  ) => {
    const canonical = await mediaOperationsApi.getCharacter(characterId);
    const canonicalForm = formFromDetail(canonical);
    setDetail(canonical);
    setBaseForm(canonicalForm);
    setForm((current) => {
      const merged = { ...canonicalForm };
      for (const field of pendingFields) merged[field] = current[field] as never;
      return merged;
    });
    const pending = Array.from(pendingFields);
    setDirtyFields(new Set(pending));
    setConflict({ canonical, dirtyFields: pending });
  };

  const submitCharacter = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const displayName = form.display_name.trim();
    if (!displayName) {
      setError(new Error("表示名を入力してください"));
      return;
    }

    setSaving(true);
    setError(null);
    try {
      let saved: MediaCharacterDetail;
      if (editing && detail) {
        if (!dirtyFields.size) return;
        const patch = patchInputFromForm(detail, form, dirtyFields);
        saved = await mediaOperationsApi.patchCharacter(detail.id, patch, newIdempotencyKey());
        toast.success("Characterを更新しました");
      } else {
        const input: CharacterCreateInput = createInputFromForm(form);
        saved = await mediaOperationsApi.createCharacter(
          input,
          newIdempotencyKey(),
        );
        toast.success("Characterを作成しました");
      }

      setDetail(saved);
      const nextForm = formFromDetail(saved);
      setForm(nextForm);
      setBaseForm(nextForm);
      setDirtyFields(new Set());
      setConflict(null);
      setEditing(true);
      setSelectedCharacterId(saved.id);
      await loadCharacters({ reset: true });
    } catch (saveError) {
      if (editing && detail && saveError instanceof MediaOperationsApiError && saveError.status === 409) {
        const pendingFields = new Set(dirtyFields);
        try {
          await refreshCanonicalAfterConflict(detail.id, pendingFields);
          setError(saveError);
        } catch (reloadError) {
          setError(reloadError);
        }
      } else {
      setError(saveError);
      }
    } finally {
      setSaving(false);
    }
  };

  const uploadAvatar = async (file: File) => {
    if (!detail) return;
    if (!file.type.toLowerCase().startsWith("image/")) {
      setError(new Error("アバターには画像ファイルを選択してください"));
      return;
    }
    if (file.size > MAX_AVATAR_BYTES) {
      setError(new Error("アバター画像は25MB以下にしてください"));
      return;
    }
    setAvatarUploading(true);
    setError(null);
    const label = safeUploadLabel(file.name);
    try {
      const artifact = await operationsApi.uploadArtifact(
        {
          file,
          project_id: detail.project_id,
          label,
        },
        newIdempotencyKey(),
      );
      const artifactId = artifact.id?.trim();
      if (!artifactId) {
        throw new Error("アップロード結果に保存済みArtifact IDがありません");
      }
      const resource = await mediaOperationsApi.attachPersonaResource(
        detail.id,
        {
          resource_kind: "reference_image",
          label,
          provenance: {
            type: "stored_artifact",
            artifact_id: artifactId,
          },
        },
        newIdempotencyKey(),
      );
      // Only render the server's persisted resource.  Never create an object
      // URL or preview from the local File, which could outlive authorization.
      if (activeCharacterIdRef.current === detail.id) {
        setResources((current) => [resource, ...current.filter((item) => item.id !== resource.id)]);
      }
      toast.success("アバター素材を保存しました");
    } catch (uploadError) {
      setError(uploadError);
    } finally {
      setAvatarUploading(false);
    }
  };

  const onAvatarInput = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void uploadAvatar(file);
  };

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void loadCharacters({ reset: true, searchTerm: searchInput });
  };

  const loadMoreCharacters = () => {
    if (loadingMore || !hasMore) return;
    void loadCharacters({ reset: false });
  };

  return (
    <div className="space-y-5" data-testid="media-persona-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">Characters</h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            Character Kernelで発信人格を管理します。Character数に固定枠はなく、接続・調査・生成・予定・結果・学習の状態をCharacter単位で確認できます。
          </p>
        </div>
        <div className="flex gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => void loadCharacters()} disabled={loading}>
            <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
            更新
          </Button>
          <Button type="button" size="sm" onClick={beginCreate} data-testid="character-create-button">
            <Plus className="size-3.5" /> Characterを追加
          </Button>
        </div>
      </div>

      <ErrorNotice error={error} />

      <Card size="sm" data-testid="character-list-card">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="text-sm">Characters</CardTitle>
          <CardDescription>{characters.length}件 · Character Kernelの一覧</CardDescription>
        </CardHeader>
        <CardContent className="pt-4">
          <form className="mb-4 flex gap-2" aria-label="Character search" onSubmit={submitSearch}>
            <Input
              value={searchInput}
              aria-label="Characterを検索"
              placeholder="名前・概要で検索"
              onChange={(event) => setSearchInput(event.target.value)}
            />
            <Button type="submit" variant="outline" size="sm" disabled={loading}>
              <Search className="size-3.5" /> 検索
            </Button>
          </form>
          {loading && !characters.length ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div>
          ) : characters.length ? (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {characters.map((character) => (
                <CharacterCard key={character.id} character={character} selected={selectedCharacterId === character.id} onSelect={() => setSelectedCharacterId(character.id)} />
              ))}
            </div>
          ) : (
            <EmptyState>Characterはまだありません。「Characterを追加」から作成してください。</EmptyState>
          )}
          {hasMore ? (
            <div className="mt-4 flex justify-center">
              <Button type="button" variant="outline" size="sm" onClick={loadMoreCharacters} disabled={loadingMore}>
                {loadingMore ? <Loader2 className="size-3.5 animate-spin" /> : null}
                {loadingMore ? "読み込み中…" : "さらに読み込む"}
              </Button>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(18rem,26rem)_minmax(0,1fr)]">
        <CharacterFormPanel detail={detail} form={form} editing={editing} saving={saving} dirtyFields={dirtyFields} onChange={updateForm} onSubmit={submitCharacter} onCancel={beginCreate} />

        <Card size="sm" data-testid="character-detail-card">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="flex items-center gap-2 text-sm"><Users className="size-3.5" /> Character dashboard</CardTitle>
            <CardDescription>選択したCharacterの状態を関連するMedia Operationsから集約します。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 pt-4">
            {detailLoading ? (
              <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div>
            ) : detail && dashboard ? (
              <>
                {conflict ? (
                  <div role="alert" data-testid="character-conflict" className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
                    サーバー側でRevisionが更新されました。最新値を読み込みましたが、未保存の変更（{conflict.dirtyFields.join(", ")}）は保持しています。内容を確認して再度保存してください。
                  </div>
                ) : null}
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-base font-semibold">{detail.current_revision.display_name}</h3>
                    <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">{detail.current_revision.summary || "概要なし"}</p>
                  </div>
                  <StatusPill status={detail.state} />
                </div>
                <CharacterDashboard dashboard={dashboard} />
                <section className="rounded-lg border border-border/70 bg-background/40 p-3" data-testid="character-resources">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <h4 className="text-xs font-semibold">Avatar / Persona resources</h4>
                      <p className="mt-0.5 text-[10px] text-muted-foreground">アップロード済みのサーバー素材のみ表示します。</p>
                    </div>
                    <input ref={avatarInputRef} type="file" accept="image/*" className="hidden" onChange={onAvatarInput} />
                    <Button type="button" size="sm" variant="outline" onClick={() => avatarInputRef.current?.click()} disabled={avatarUploading}>
                      {avatarUploading ? <Loader2 className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />}
                      {avatarUploading ? "アップロード中…" : "画像を追加"}
                    </Button>
                  </div>
                  {resourceLoading ? (
                    <p className="mt-3 text-xs text-muted-foreground">素材を読み込み中…</p>
                  ) : resources.length ? (
                    <ul className="mt-3 space-y-1.5">
                      {resources.map((resource) => (
                        <li key={resource.id} data-testid={`character-resource-${resource.id}`} className="rounded-md border border-border/60 px-2.5 py-2 text-xs">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="font-medium">{resource.label || resource.resource_kind}</span>
                            <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px]">{resource.resource_kind}</span>
                          </div>
                          {resource.provenance.type === "stored_artifact" ? (
                            <p className="mt-1 font-mono text-[10px] text-muted-foreground">stored artifact: {resource.provenance.artifact_id}</p>
                          ) : resource.provenance.type === "artifact" ? (
                            <p className="mt-1 font-mono text-[10px] text-muted-foreground">sha256:{resource.provenance.sha256.slice(0, 16)}… · {resource.provenance.mime_type}</p>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-3 text-xs text-muted-foreground">保存済み素材はありません。</p>
                  )}
                </section>
                <PlatformAccountPanel
                  characterId={detail.id}
                  onChanged={() => void loadCharacter(detail.id)}
                />
              </>
            ) : detail ? (
              <div className="py-8 text-center text-sm text-muted-foreground">Dashboardを読み込めませんでした。</div>
            ) : (
              <div className="py-8 text-center text-sm text-muted-foreground">Characterを選択するか、新しいCharacterを作成してください。</div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

/** Character-oriented alias; keep MediaPersonaPanel for existing workspace imports. */
export const MediaCharacterPanel = MediaPersonaPanel;
