"use client";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  Loader2,
  Pencil,
  Plus,
  Save,
  Upload,
} from "lucide-react";
import { toast } from "sonner";

import {
  mediaOperationsApi,
  MediaOperationsApiError,
  type CharacterPatchInput,
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
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { workspaceHref } from "@/lib/persona-workspace-model";
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
  "fanza",
  "tiktok",
];

const PLATFORM_LABELS: Record<MediaPlatform, string> = {
  x: "X",
  pixiv: "Pixiv",
  dlsite: "DLsite",
  patreon: "Patreon",
  youtube: "YouTube",
  instagram: "Instagram",
  fanza: "FANZA",
  tiktok: "TikTok",
};

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
  return "Persona APIでエラーが発生しました";
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

function CharacterFormPanel({
  form,
  editing,
  saving,
  dirtyFields,
  onChange,
  onSubmit,
  onCancel,
}: {
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
          <span className="inline-flex items-center gap-1">
            {editing ? "Personaを編集" : "Personaを作成"}
            <HelpPopover label={editing ? "Personaを編集" : "Personaを作成"}>
              {editing
                ? "更新は新しいRevisionとして保存され、過去の内容は上書きされません。"
                : "必要な情報だけ入力してPersonaを追加します。作成時に初期データは生成しません。"}
            </HelpPopover>
          </span>
        </CardTitle>
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
            {editing ? <Button type="button" variant="ghost" size="sm" disabled={dirtyFields.size === 0} onClick={onCancel}>変更を戻す</Button> : null}
            <Button type="submit" size="sm" disabled={saving || (editing && dirtyFields.size === 0)}>
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
              {editing ? "設定を保存" : "作成"}
            </Button>
          </div>
          </fieldset>
        </form>

      </CardContent>
    </Card>
  );
}

/**
 * Settings of one persona (reached from its 人格 view). The persona list and
 * its production status live only in the 人格 view, so neither is repeated here.
 */
export function MediaPersonaPanel({ personaId }: { personaId: string }) {
  const [detail, setDetail] = useState<MediaCharacterDetail | null>(null);
  const [form, setForm] = useState<CharacterForm>(emptyForm);
  const [baseForm, setBaseForm] = useState<CharacterForm>(emptyForm);
  const [dirtyFields, setDirtyFields] = useState<Set<CharacterRevisionField>>(new Set());
  const [conflict, setConflict] = useState<{ canonical: MediaCharacterDetail; dirtyFields: CharacterRevisionField[] } | null>(null);
  const [detailLoading, setDetailLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [resources, setResources] = useState<MediaPersonaResource[]>([]);
  const [resourceLoading, setResourceLoading] = useState(false);
  const [avatarUploading, setAvatarUploading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const characterRequestRef = useRef(0);
  const resourceRequestRef = useRef(0);
  const activeCharacterIdRef = useRef<string | null>(null);
  const avatarInputRef = useRef<HTMLInputElement>(null);

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
      const nextDetail = await mediaOperationsApi.getCharacter(characterId);
      if (requestId !== characterRequestRef.current) return;
      setDetail(nextDetail);
      const nextForm = formFromDetail(nextDetail);
      setForm(nextForm);
      setBaseForm(nextForm);
      setDirtyFields(new Set());
      setConflict(null);
      void loadResources(characterId);
    } catch (loadError) {
      if (requestId === characterRequestRef.current) setError(loadError);
    } finally {
      if (requestId === characterRequestRef.current) setDetailLoading(false);
    }
  }, [loadResources]);

  useEffect(() => {
    const task = window.setTimeout(() => {
      void loadCharacter(personaId);
    }, 0);
    return () => window.clearTimeout(task);
  }, [loadCharacter, personaId]);

  const updateForm = (next: CharacterForm) => {
    setForm(next);
    setDirtyFields(dirtyFieldsFor(baseForm, next));
    setConflict(null);
    setError(null);
  };

  const revertForm = () => {
    setForm(baseForm);
    setDirtyFields(new Set());
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
    if (!detail || !dirtyFields.size) return;
    if (!form.display_name.trim()) {
      setError(new Error("表示名を入力してください"));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const patch = patchInputFromForm(detail, form, dirtyFields);
      const saved = await mediaOperationsApi.patchCharacter(detail.id, patch, newIdempotencyKey());
      toast.success("人格の設定を保存しました");
      setDetail(saved);
      const nextForm = formFromDetail(saved);
      setForm(nextForm);
      setBaseForm(nextForm);
      setDirtyFields(new Set());
      setConflict(null);
    } catch (saveError) {
      if (saveError instanceof MediaOperationsApiError && saveError.status === 409) {
        try {
          await refreshCanonicalAfterConflict(detail.id, new Set(dirtyFields));
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

  return (
    <div className="space-y-4" data-testid="media-persona-panel">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="min-w-0 break-words text-xl font-semibold">{detail?.current_revision.display_name ?? "人格"} の設定</h1>
        <Link href={workspaceHref(personaId)} className="text-xs text-primary hover:underline">人格に戻る</Link>
      </div>

      <ErrorNotice error={error} />
      {conflict ? (
        <div role="alert" data-testid="character-conflict" className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
          サーバー側でRevisionが更新されました。最新値を読み込みましたが、未保存の変更（{conflict.dirtyFields.join(", ")}）は保持しています。内容を確認して保存してください。
        </div>
      ) : null}

      {detailLoading && !detail ? (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div>
      ) : !detail ? (
        <EmptyState>この人格を読み込めませんでした。人格の画面から開き直してください。</EmptyState>
      ) : (
        <div className="grid min-w-0 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,24rem)]">
          <CharacterFormPanel form={form} editing saving={saving} dirtyFields={dirtyFields} onChange={updateForm} onSubmit={submitCharacter} onCancel={revertForm} />
          <div className="min-w-0 space-y-4">
            <section className="rounded-xl border bg-card p-4" data-testid="character-resources">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="inline-flex items-center gap-1 text-sm font-semibold">
                  参照画像
                  <HelpPopover label="参照画像">アップロード済みのサーバー素材だけを表示します。</HelpPopover>
                </h2>
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
                    <li key={resource.id} data-testid={`character-resource-${resource.id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/60 px-2.5 py-2 text-xs">
                      <span className="font-medium">{resource.label || resource.resource_kind}</span>
                      <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px]">{resource.resource_kind}</span>
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
          </div>
        </div>
      )}
    </div>
  );
}

/** Character-oriented alias; keep MediaPersonaPanel for existing workspace imports. */
export const MediaCharacterPanel = MediaPersonaPanel;
