"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import {
  CheckCircle2,
  ClipboardCheck,
  FileCheck2,
  Loader2,
  Plus,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";

import {
  mediaContentApi,
  type AssessmentResult,
  type ContentVariant,
  type ContentVariantCreateInput,
  type ContentVariantRevision,
  type DlsiteContentPayload,
  type InstagramContentPayload,
  type MediaContentPlatform,
  type PatreonContentPayload,
  type PixivContentPayload,
  type PlatformContentPayload,
  type RightsAssessmentResult,
  type VariantReadiness,
  type YoutubeContentPayload,
} from "@/lib/media-operations-content-api";
import { AppSelect } from "@/components/ui/app-select";
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

const PLATFORMS: readonly MediaContentPlatform[] = [
  "x",
  "pixiv",
  "dlsite",
  "patreon",
  "youtube",
  "instagram",
];

const PLATFORM_LABELS: Record<MediaContentPlatform, string> = {
  x: "X",
  pixiv: "pixiv",
  dlsite: "DLsite",
  patreon: "Patreon",
  youtube: "YouTube",
  instagram: "Instagram",
};

type PayloadForm = {
  mode: string;
  format: string;
  text: string;
  title: string;
  description: string;
  body: string;
  caption: string;
  public_preview: string;
  media_asset_ids: string;
  alt_text: string;
  links: string;
  hashtags: string;
  tags: string;
  sensitive: boolean;
  thread_order: string;
  scheduled_at: string;
  ai_generated: boolean;
  rating: string;
  series: string;
  category: string;
  age_rating: string;
  price: string;
  currency: string;
  preview_asset_ids: string;
  thumbnail_asset_id: string;
  package_ref: string;
  rights_checklist: string;
  audience: string;
  tier_refs: string;
  media_asset_id: string;
  captions_ref: string;
  visibility: string;
  audience_setting: string;
  disclosure: string;
  tier_scope: string;
};

function emptyPayloadForm(): PayloadForm {
  return {
    mode: "post",
    format: "feed",
    text: "",
    title: "",
    description: "",
    body: "",
    caption: "",
    public_preview: "",
    media_asset_ids: "",
    alt_text: "",
    links: "",
    hashtags: "",
    tags: "",
    sensitive: false,
    thread_order: "1",
    scheduled_at: "",
    ai_generated: false,
    rating: "safe",
    series: "",
    category: "",
    age_rating: "all_ages",
    price: "",
    currency: "JPY",
    preview_asset_ids: "",
    thumbnail_asset_id: "",
    package_ref: "",
    rights_checklist: "",
    audience: "public",
    tier_refs: "",
    media_asset_id: "",
    captions_ref: "",
    visibility: "private",
    audience_setting: "unknown",
    disclosure: "none",
    tier_scope: "",
  };
}

function splitList(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[\n,]/u)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function readableError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "ContentVariant APIでエラーが発生しました";
}

function statusLabel(value: string | null | undefined): string {
  const status = value?.trim().toLowerCase();
  if (!status) return "未設定";
  const labels: Record<string, string> = {
    draft: "下書き",
    ready: "準備完了",
    blocked: "ブロック",
    publishable: "公開準備可",
    passed: "合格",
    failed: "不合格",
    review_required: "レビュー要",
    not_run: "未実行",
    cleared: "権利クリア",
    unknown: "不明",
  };
  return labels[status] ?? value ?? "未設定";
}

function StatusPill({
  status,
  tone = "default",
}: {
  status: string | null | undefined;
  tone?: "default" | "success" | "danger";
}) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${
        tone === "success"
          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : tone === "danger"
            ? "border-destructive/30 bg-destructive/10 text-destructive"
            : "border-border bg-muted/50 text-muted-foreground"
      }`}
      data-content-status={status ?? "unknown"}
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

function FieldLabel({
  htmlFor,
  children,
}: {
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={htmlFor} className="text-xs font-medium text-foreground">
      {children}
    </label>
  );
}

function TextField({
  id,
  label,
  value,
  onChange,
  placeholder,
  required = false,
  ariaLabel,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
  ariaLabel?: string;
}) {
  return (
    <div className="space-y-1.5">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        required={required}
        aria-label={ariaLabel}
      />
    </div>
  );
}

function TextAreaField({
  id,
  label,
  value,
  onChange,
  placeholder,
  required = false,
  help,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
  help?: string;
}) {
  return (
    <div className="space-y-1.5">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Textarea
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        required={required}
        rows={3}
      />
      {help ? <p className="text-[10px] text-muted-foreground">{help}</p> : null}
    </div>
  );
}

function checkPayloadRequired(
  platform: MediaContentPlatform,
  form: PayloadForm,
): string | null {
  if (platform === "x" && !form.text.trim()) return "X本文を入力してください";
  if (platform === "pixiv" && (!form.title.trim() || !splitList(form.media_asset_ids).length)) return "pixivタイトルとMedia Assetを入力してください";
  if (platform === "dlsite") {
    if (!form.title.trim() || !form.description.trim() || !form.package_ref.trim() || !form.category.trim()) return "DLsite商品タイトル・説明・カテゴリ・配布パッケージ参照を入力してください";
    if (!splitList(form.preview_asset_ids).length) return "DLsite Preview Assetを1件以上入力してください";
    if (!form.price.trim() || !Number.isFinite(Number(form.price))) return "DLsite価格を入力してください";
    if (!splitList(form.rights_checklist).length) return "DLsite Rights checklistを1件以上入力してください";
  }
  if (platform === "patreon" && (!form.title.trim() || !form.body.trim())) {
    return "Patreonタイトルと本文を入力してください";
  }
  if (platform === "patreon" && form.audience === "tier" && !splitList(form.tier_refs).length) {
    return "Tier audienceにはTier参照が必要です";
  }
  if (platform === "youtube" && (!form.title.trim() || !form.media_asset_id.trim())) {
    return "YouTubeタイトルと動画Asset IDを入力してください";
  }
  if (platform === "youtube" && splitList(form.media_asset_id).length !== 1) {
    return "YouTube Video Asset IDは1件だけ入力してください";
  }
  if (platform === "instagram" && (!form.caption.trim() || !splitList(form.media_asset_ids).length)) {
    return "Instagramキャプションを入力してください";
  }
  if (platform === "instagram" && form.format === "carousel" && splitList(form.media_asset_ids).length < 2) {
    return "Instagram CarouselにはMedia Assetを2件以上入力してください";
  }
  if (platform === "instagram" && form.format !== "carousel" && splitList(form.media_asset_ids).length !== 1) {
    return "Instagram Feed / ReelにはMedia Assetを1件入力してください";
  }
  return null;
}

function payloadFromForm(
  platform: MediaContentPlatform,
  form: PayloadForm,
): PlatformContentPayload {
  const scheduledAt = form.scheduled_at.trim() || null;
  switch (platform) {
    case "x":
      if (form.mode === "thread") {
        return {
          type: "x_thread",
          posts: [{
            text: form.text.trim(),
            ...(splitList(form.media_asset_ids).length ? { media: splitList(form.media_asset_ids) } : {}),
            ...(form.alt_text.trim() ? { alt_text: form.alt_text.trim() } : {}),
          }],
          ...(scheduledAt ? { scheduled_at: scheduledAt } : {}),
        };
      }
      return {
        type: "x_post",
        text: form.text.trim(),
        ...(splitList(form.media_asset_ids).length ? { media: splitList(form.media_asset_ids) } : {}),
        ...(form.alt_text.trim() ? { alt_text: form.alt_text.trim() } : {}),
        ...(splitList(form.links).length ? { links: splitList(form.links) } : {}),
        ...(splitList(form.hashtags).length ? { hashtags: splitList(form.hashtags) } : {}),
        ...(form.sensitive ? { sensitive_content: true } : {}),
        ...(scheduledAt ? { scheduled_at: scheduledAt } : {}),
      };
    case "pixiv":
      return {
        type: "pixiv_work",
        title: form.title.trim(),
        caption: form.description.trim(),
        tags: splitList(form.tags),
        media: splitList(form.media_asset_ids),
        ...(form.ai_generated ? { ai_generated: true } : {}),
        ...(form.rating !== "safe" ? { rating: form.rating } : {}),
        ...(form.series.trim() ? { series_id: form.series.trim() } : {}),
      } satisfies PixivContentPayload;
    case "dlsite": {
      const parsedPrice = Number(form.price);
      return {
        type: "dlsite_release",
        title: form.title.trim(),
        description: form.description.trim(),
        category: form.category.trim(),
        age_rating: form.age_rating,
        price: Number.isFinite(parsedPrice) ? parsedPrice : 0,
        sales: { currency: form.currency.trim() || "JPY", tax_included: true, distribution: "dlsite" },
        preview_assets: splitList(form.preview_asset_ids),
        deliverable_package_ref: form.package_ref.trim(),
        rights_checklist: splitList(form.rights_checklist).map((code) => ({ code, status: "not_run" as const, note: null })),
        ...(splitList(form.thumbnail_asset_id).length ? { thumbnail_assets: splitList(form.thumbnail_asset_id) } : {}),
      } satisfies DlsiteContentPayload;
    }
    case "patreon":
      return {
        type: "patreon_post",
        audience:
          form.audience === "paid" || form.audience === "tier"
            ? form.audience
            : "public",
        title: form.title.trim(),
        body: form.body.trim(),
        ...(form.public_preview.trim() ? { public_preview: form.public_preview.trim() } : {}),
        ...(splitList(form.media_asset_ids).length ? { attachments: splitList(form.media_asset_ids) } : {}),
        ...(splitList(form.tier_refs).length ? { tier_refs: splitList(form.tier_refs) } : {}),
        ...(scheduledAt ? { scheduled_at: scheduledAt } : {}),
      } satisfies PatreonContentPayload;
    case "youtube":
      return {
        type: form.format === "shorts" ? "youtube_short" : "youtube_video",
        title: form.title.trim(),
        description: form.description.trim(),
        tags: splitList(form.tags),
        media_asset: splitList(form.media_asset_id),
        visibility: form.visibility,
        ...(splitList(form.thumbnail_asset_id).length ? { thumbnail: splitList(form.thumbnail_asset_id) } : {}),
        ...(splitList(form.captions_ref).length ? { captions: splitList(form.captions_ref) } : {}),
        ...(scheduledAt ? { scheduled_at: scheduledAt } : {}),
        ...(form.audience_setting !== "unknown" ? { audience: form.audience_setting } : {}),
        ...(form.disclosure !== "none" ? { disclosure: form.disclosure } : {}),
      } satisfies YoutubeContentPayload;
    case "instagram":
      return {
        type: form.format === "carousel" ? "instagram_carousel" : form.format === "reel" ? "instagram_reel" : "instagram_feed",
        caption: form.caption.trim(),
        media: splitList(form.media_asset_ids),
        ...(form.alt_text.trim() ? { alt_text: form.alt_text.trim() } : {}),
        ...(scheduledAt ? { scheduled_at: scheduledAt } : {}),
      } satisfies InstagramContentPayload;
  }
}

function sourceEvidenceFromText(value: string) {
  return splitList(value).map((ref) => {
    if (/^https?:\/\//iu.test(ref)) {
      return { type: "url" as const, url: ref, label: null, note: null };
    }
    return {
      type: "artifact" as const,
      sha256: ref.replace(/^sha256:/iu, ""),
      mime_type: "application/octet-stream",
      label: null,
      note: null,
    };
  });
}

function outputRefsFromText(value: string): Array<{
  generation_output_id: string;
  sha256: string;
}> {
  return splitList(value).map((line) => {
    const [generationOutputId, sha256 = ""] = line.split(/\s*[|,]\s*/u);
    return { generation_output_id: generationOutputId, sha256 };
  });
}

function checksFromText(value: string) {
  return splitList(value).map((code) => ({
    code,
    status: "not_run" as const,
    mandatory: true,
    message: null,
  }));
}

function findingsFromText(value: string) {
  return splitList(value).map((message, index) => ({
    code: `finding-${index + 1}`,
    severity: "warning" as const,
    message,
  }));
}

function evidenceFromText(value: string) {
  return splitList(value).map((ref) => {
    if (/^https?:\/\//iu.test(ref)) {
      return { type: "url" as const, url: ref, label: null, note: null };
    }
    return {
      type: "artifact" as const,
      sha256: ref.replace(/^sha256:/iu, ""),
      mime_type: "application/octet-stream",
      label: null,
      note: null,
    };
  });
}

function evidenceInputError(value: string): string | null {
  for (const ref of splitList(value)) {
    const sha = ref.replace(/^sha256:/iu, "");
    if (!/^https?:\/\//iu.test(ref) && !/^[0-9a-f]{64}$/iu.test(sha)) {
      return `Evidenceは公開URLまたは64桁sha256で入力してください: ${ref}`;
    }
  }
  return null;
}

function VariantPayloadFields({
  platform,
  form,
  setForm,
  idPrefix,
}: {
  platform: MediaContentPlatform;
  form: PayloadForm;
  setForm: (update: (current: PayloadForm) => PayloadForm) => void;
  idPrefix: string;
}) {
  const field = (key: keyof PayloadForm) => (value: string) =>
    setForm((current) => ({ ...current, [key]: value }));

  if (platform === "x") {
    return (
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`${idPrefix}-mode`}>形式</FieldLabel>
            <AppSelect id={`${idPrefix}-mode`} value={form.mode} onValueChange={field("mode")}>
              <option value="post">Single post</option>
              <option value="thread">Thread</option>
            </AppSelect>
          </div>
          <TextField id={`${idPrefix}-thread-order`} label="Thread順序" value={form.thread_order} onChange={field("thread_order")} />
        </div>
        <TextAreaField id={`${idPrefix}-text`} label="本文" value={form.text} onChange={field("text")} required />
        <TextField id={`${idPrefix}-media-assets`} label="添付Asset ID（1行1件）" value={form.media_asset_ids} onChange={field("media_asset_ids")} />
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField id={`${idPrefix}-alt-text`} label="Alt text" value={form.alt_text} onChange={field("alt_text")} />
          <TextField id={`${idPrefix}-scheduled-at`} label="予定時刻（任意）" value={form.scheduled_at} onChange={field("scheduled_at")} placeholder="2026-09-01T12:00:00Z" />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField id={`${idPrefix}-links`} label="リンク（1行1件）" value={form.links} onChange={field("links")} />
          <TextField id={`${idPrefix}-hashtags`} label="Hashtag（1行1件）" value={form.hashtags} onChange={field("hashtags")} />
        </div>
        <label className="flex items-center gap-2 text-xs">
          <Checkbox checked={form.sensitive} onCheckedChange={(checked) => setForm((current) => ({ ...current, sensitive: checked === true }))} />
          Sensitive content
        </label>
      </div>
    );
  }

  if (platform === "pixiv") {
    return (
      <div className="space-y-3">
        <TextField id={`${idPrefix}-title`} label="タイトル" value={form.title} onChange={field("title")} required />
        <TextAreaField id={`${idPrefix}-description`} label="キャプション / 説明" value={form.description} onChange={field("description")} />
        <div className="grid gap-3 sm:grid-cols-2">
          <TextField id={`${idPrefix}-tags`} label="Tags（1行1件）" value={form.tags} onChange={field("tags")} />
          <TextField id={`${idPrefix}-series`} label="シリーズ参照（任意）" value={form.series} onChange={field("series")} />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-rating`}>Rating</FieldLabel><AppSelect id={`${idPrefix}-rating`} value={form.rating} onValueChange={field("rating")}><option value="safe">Safe</option><option value="r-18">R-18</option><option value="r-18g">R-18G</option></AppSelect></div>
          <TextField id={`${idPrefix}-media-assets`} label="Media Asset ID（1行1件）" value={form.media_asset_ids} onChange={field("media_asset_ids")} />
        </div>
        <label className="flex items-center gap-2 text-xs"><Checkbox checked={form.ai_generated} onCheckedChange={(checked) => setForm((current) => ({ ...current, ai_generated: checked === true }))} /> AI生成表示を付ける</label>
      </div>
    );
  }

  if (platform === "dlsite") {
    return (
      <div className="space-y-3">
        <TextField id={`${idPrefix}-title`} label="商品タイトル" value={form.title} onChange={field("title")} required />
        <TextAreaField id={`${idPrefix}-description`} label="商品説明" value={form.description} onChange={field("description")} />
        <div className="grid gap-3 sm:grid-cols-2"><TextField id={`${idPrefix}-category`} label="カテゴリ" value={form.category} onChange={field("category")} /><div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-age-rating`}>年齢区分</FieldLabel><AppSelect id={`${idPrefix}-age-rating`} value={form.age_rating} onValueChange={field("age_rating")}><option value="all_ages">全年齢</option><option value="r-15">R-15</option><option value="r-18">R-18</option></AppSelect></div></div>
        <div className="grid gap-3 sm:grid-cols-3"><TextField id={`${idPrefix}-price`} label="価格（任意）" value={form.price} onChange={field("price")} /><TextField id={`${idPrefix}-currency`} label="通貨" value={form.currency} onChange={field("currency")} /><TextField id={`${idPrefix}-thumbnail`} label="Thumbnail Asset ID" value={form.thumbnail_asset_id} onChange={field("thumbnail_asset_id")} /></div>
        <TextField id={`${idPrefix}-preview-assets`} label="Preview Asset ID（1行1件）" value={form.preview_asset_ids} onChange={field("preview_asset_ids")} />
        <TextField id={`${idPrefix}-package-ref`} label="配布パッケージ参照" value={form.package_ref} onChange={field("package_ref")} required />
        <TextAreaField id={`${idPrefix}-rights-checklist`} label="Rights checklist（1行1項目）" value={form.rights_checklist} onChange={field("rights_checklist")} />
      </div>
    );
  }

  if (platform === "patreon") {
    return (
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-audience`}>Audience</FieldLabel><AppSelect id={`${idPrefix}-audience`} value={form.audience} onValueChange={field("audience")}><option value="public">Public</option><option value="paid">Paid</option><option value="tier">Tier</option></AppSelect></div><TextField id={`${idPrefix}-tier-refs`} label="Tier参照（1行1件）" value={form.tier_refs} onChange={field("tier_refs")} /></div>
        <TextField id={`${idPrefix}-title`} label="タイトル" value={form.title} onChange={field("title")} required />
        <TextAreaField id={`${idPrefix}-body`} label="本文" value={form.body} onChange={field("body")} required />
        <TextAreaField id={`${idPrefix}-public-preview`} label="Public preview" value={form.public_preview} onChange={field("public_preview")} />
        <div className="grid gap-3 sm:grid-cols-2"><TextField id={`${idPrefix}-media-assets`} label="Media Asset ID（1行1件）" value={form.media_asset_ids} onChange={field("media_asset_ids")} /><TextField id={`${idPrefix}-scheduled-at`} label="予定時刻（任意）" value={form.scheduled_at} onChange={field("scheduled_at")} /></div>
      </div>
    );
  }

  if (platform === "youtube") {
    return (
      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-format`}>形式</FieldLabel><AppSelect id={`${idPrefix}-format`} value={form.format} onValueChange={field("format")}><option value="long_form">Long-form</option><option value="shorts">Shorts</option></AppSelect></div><TextField id={`${idPrefix}-media-asset`} label="Video Asset ID" value={form.media_asset_id} onChange={field("media_asset_id")} required /></div>
        <TextField id={`${idPrefix}-title`} label="タイトル" value={form.title} onChange={field("title")} required />
        <TextAreaField id={`${idPrefix}-description`} label="説明" value={form.description} onChange={field("description")} />
        <div className="grid gap-3 sm:grid-cols-2"><TextField id={`${idPrefix}-tags`} label="Tags（1行1件）" value={form.tags} onChange={field("tags")} /><TextField id={`${idPrefix}-thumbnail`} label="Thumbnail Asset ID" value={form.thumbnail_asset_id} onChange={field("thumbnail_asset_id")} /></div>
        <div className="grid gap-3 sm:grid-cols-2"><TextField id={`${idPrefix}-captions`} label="Captions参照（任意）" value={form.captions_ref} onChange={field("captions_ref")} /><TextField id={`${idPrefix}-scheduled-at`} label="予定時刻（任意）" value={form.scheduled_at} onChange={field("scheduled_at")} /></div>
        <div className="grid gap-3 sm:grid-cols-3"><div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-visibility`}>Visibility</FieldLabel><AppSelect id={`${idPrefix}-visibility`} value={form.visibility} onValueChange={field("visibility")}><option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option></AppSelect></div><div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-audience-setting`}>Audience</FieldLabel><AppSelect id={`${idPrefix}-audience-setting`} value={form.audience_setting} onValueChange={field("audience_setting")}><option value="unknown">Unknown</option><option value="not_made_for_kids">Not made for kids</option><option value="made_for_kids">Made for kids</option></AppSelect></div><div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-disclosure`}>Disclosure</FieldLabel><AppSelect id={`${idPrefix}-disclosure`} value={form.disclosure} onValueChange={field("disclosure")}><option value="none">なし</option><option value="sponsored">Sponsored</option><option value="ai_assisted">AI assisted</option></AppSelect></div></div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-format`}>形式</FieldLabel><AppSelect id={`${idPrefix}-format`} value={form.format} onValueChange={field("format")}><option value="feed">Feed image</option><option value="carousel">Carousel</option><option value="reel">Reel</option></AppSelect></div><TextField id={`${idPrefix}-scheduled-at`} label="予定時刻（任意）" value={form.scheduled_at} onChange={field("scheduled_at")} /></div>
      <TextAreaField id={`${idPrefix}-caption`} label="キャプション" value={form.caption} onChange={field("caption")} required />
      <TextField id={`${idPrefix}-media-assets`} label="Media Asset ID（順序通りに1行1件）" value={form.media_asset_ids} onChange={field("media_asset_ids")} />
      <TextField id={`${idPrefix}-alt-text`} label="Accessibility / Alt text" value={form.alt_text} onChange={field("alt_text")} />
    </div>
  );
}

function revisionHash(value: ContentVariantRevision | null | undefined): string {
  return value?.content_hash || "—";
}

function RevisionSummary({ revision }: { revision: ContentVariantRevision | null | undefined }) {
  if (!revision) return <p className="text-xs text-muted-foreground">Revisionはまだありません。</p>;
  return (
    <div className="grid gap-2 rounded-md border border-border/70 bg-muted/20 p-3 text-xs sm:grid-cols-2">
      <div><span className="text-muted-foreground">Immutable revision</span><p className="font-semibold">v{revision.version}</p></div>
      <div><span className="text-muted-foreground">Content hash</span><p className="break-all font-mono">{revisionHash(revision)}</p></div>
      <div><span className="text-muted-foreground">ContentItem</span><p>コンテンツ項目に固定されています</p></div>
      <div><span className="text-muted-foreground">Persona方針</span><p>v{revision.version} · {revision.persona_revision_hash}</p></div>
      <div><span className="text-muted-foreground">PlatformAccount</span><p>{revision.platform_account_id ? "接続先に固定されています" : "接続先未設定"}</p></div>
      <div><span className="text-muted-foreground">Platform</span><p>{PLATFORM_LABELS[revision.platform]}</p></div>
    </div>
  );
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `media-content-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function MediaContentVariantPanel() {
  const [variants, setVariants] = useState<ContentVariant[]>([]);
  const [selectedVariantId, setSelectedVariantId] = useState("");
  const [selectedVariant, setSelectedVariant] = useState<ContentVariant | null>(null);
  const [revisions, setRevisions] = useState<ContentVariantRevision[]>([]);
  const [readiness, setReadiness] = useState<VariantReadiness | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState<"variant" | "revision" | "qa" | "rights" | null>(null);
  const [error, setError] = useState<unknown>(null);

  const [contentItemId, setContentItemId] = useState("");
  const [personaRevisionId, setPersonaRevisionId] = useState("");
  const [platformAccountId, setPlatformAccountId] = useState("");
  const [platformAccountRevisionId, setPlatformAccountRevisionId] = useState("");
  const [platform, setPlatform] = useState<MediaContentPlatform>("x");
  const [payloadForm, setPayloadForm] = useState<PayloadForm>(emptyPayloadForm);
  const [generationOutputRefs, setGenerationOutputRefs] = useState("");
  const [sourceEvidence, setSourceEvidence] = useState("");

  const [qaResult, setQaResult] = useState<AssessmentResult>("review_required");
  const [qaPolicyRevisionId, setQaPolicyRevisionId] = useState("");
  const [qaPolicyRevisionHash, setQaPolicyRevisionHash] = useState("");
  const [qaChecks, setQaChecks] = useState("");
  const [qaFindings, setQaFindings] = useState("");
  const [qaEvidence, setQaEvidence] = useState("");
  const [rightsResult, setRightsResult] = useState<RightsAssessmentResult>("review_required");
  const [rightsPolicyRevisionId, setRightsPolicyRevisionId] = useState("");
  const [rightsPolicyRevisionHash, setRightsPolicyRevisionHash] = useState("");
  const [rightsChecks, setRightsChecks] = useState("");
  const [rightsFindings, setRightsFindings] = useState("");
  const [rightsEvidence, setRightsEvidence] = useState("");

  const currentRevision = useMemo(
    () => selectedVariant?.current_revision ?? revisions[revisions.length - 1] ?? null,
    [revisions, selectedVariant],
  );

  const loadVariants = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await mediaContentApi.listVariants();
      setVariants(next);
      setSelectedVariantId((current) => next.some((item) => item.id === current) ? current : next[0]?.id ?? "");
    } catch (nextError) {
      setError(nextError);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (variantId: string) => {
    if (!variantId) {
      setSelectedVariant(null);
      setRevisions([]);
      setReadiness(null);
      return;
    }
    setDetailLoading(true);
    setError(null);
    try {
      const [detail, nextRevisions, nextReadiness] = await Promise.all([
        mediaContentApi.getVariant(variantId),
        mediaContentApi.listRevisions(variantId),
        mediaContentApi.getReadiness(variantId),
      ]);
      setSelectedVariant(detail);
      setRevisions(nextRevisions);
      setReadiness(nextReadiness);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    const task = window.setTimeout(() => void loadVariants(), 0);
    return () => window.clearTimeout(task);
  }, [loadVariants]);

  useEffect(() => {
    const task = window.setTimeout(() => void loadDetail(selectedVariantId), 0);
    return () => window.clearTimeout(task);
  }, [loadDetail, selectedVariantId]);

  const createInput = useCallback((): ContentVariantCreateInput => ({
    content_item_id: contentItemId.trim(),
    persona_revision_id: personaRevisionId.trim(),
    platform_account_id: platformAccountId.trim(),
    platform_account_revision_id: platformAccountRevisionId.trim() || null,
    platform,
    payload: payloadFromForm(platform, payloadForm),
    generation_output_refs: outputRefsFromText(generationOutputRefs),
    source_evidence: sourceEvidenceFromText(sourceEvidence),
  }), [contentItemId, generationOutputRefs, payloadForm, personaRevisionId, platform, platformAccountId, platformAccountRevisionId, sourceEvidence]);

  const createVariant = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!contentItemId.trim() || !personaRevisionId.trim() || !platformAccountId.trim()) {
      setError(new Error("ContentItem・Persona revision・PlatformAccountのIDが必要です"));
      return;
    }
    const payloadError = checkPayloadRequired(platform, payloadForm);
    if (payloadError) {
      setError(new Error(payloadError));
      return;
    }
    const evidenceError = evidenceInputError(sourceEvidence);
    if (evidenceError) {
      setError(new Error(evidenceError));
      return;
    }
    setBusy("variant");
    setError(null);
    try {
      const created = await mediaContentApi.createVariant(createInput(), newIdempotencyKey());
      setVariants((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setSelectedVariantId(created.id);
      setPayloadForm(emptyPayloadForm());
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const appendRevision = async () => {
    if (!selectedVariant || !currentRevision) return;
    const payloadError = checkPayloadRequired(platform, payloadForm);
    if (payloadError) {
      setError(new Error(payloadError));
      return;
    }
    const evidenceError = evidenceInputError(sourceEvidence);
    if (evidenceError) {
      setError(new Error(evidenceError));
      return;
    }
    setBusy("revision");
    setError(null);
    try {
      await mediaContentApi.appendRevision(selectedVariant.id, {
        expected_version: currentRevision.version,
        persona_revision_id: personaRevisionId.trim() || currentRevision.persona_revision_id,
        platform_account_id: platformAccountId.trim() || currentRevision.platform_account_id,
        platform_account_revision_id: platformAccountRevisionId.trim() || currentRevision.platform_account_revision_id || null,
        payload: payloadFromForm(platform, payloadForm),
        generation_output_refs: outputRefsFromText(generationOutputRefs),
        source_evidence: sourceEvidenceFromText(sourceEvidence),
      }, newIdempotencyKey());
      await loadDetail(selectedVariant.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const recordQa = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!currentRevision) return;
    const evidenceError = evidenceInputError(qaEvidence);
    if (evidenceError) {
      setError(new Error(evidenceError));
      return;
    }
    setBusy("qa");
    setError(null);
    try {
      await mediaContentApi.recordQa({
        variant_revision_id: currentRevision.id,
        policy_revision_id: qaPolicyRevisionId.trim() || null,
        policy_revision_hash: qaPolicyRevisionHash.trim() || null,
        result: qaResult,
        checks: checksFromText(qaChecks),
        findings: findingsFromText(qaFindings),
        evidence: evidenceFromText(qaEvidence),
      }, newIdempotencyKey());
      if (selectedVariant) await loadDetail(selectedVariant.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const recordRights = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!currentRevision) return;
    const evidenceError = evidenceInputError(rightsEvidence);
    if (evidenceError) {
      setError(new Error(evidenceError));
      return;
    }
    setBusy("rights");
    setError(null);
    try {
      await mediaContentApi.recordRights({
        variant_revision_id: currentRevision.id,
        result: rightsResult,
        policy_revision_id: rightsPolicyRevisionId.trim() || null,
        policy_revision_hash: rightsPolicyRevisionHash.trim() || null,
        checks: checksFromText(rightsChecks),
        findings: findingsFromText(rightsFindings),
        evidence: evidenceFromText(rightsEvidence),
      }, newIdempotencyKey());
      if (selectedVariant) await loadDetail(selectedVariant.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-4" data-testid="media-content-variants-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">Media Operations / Pipeline</p>
          <h2 className="mt-1 text-xl font-semibold tracking-tight">ContentVariant</h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">ContentItemを各Platformのtyped payloadへ展開し、immutable revision・QA・Rightsを一つの監査可能な流れで確認します。</p>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={() => void loadVariants()} disabled={loading}><RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} /> 更新</Button>
      </div>

      <ErrorNotice error={error} />

      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(15rem,23rem)_minmax(0,1fr)]">
        <Card size="sm" className="min-w-0">
          <CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Variant一覧</CardTitle><CardDescription>{variants.length}件 · 既存Revisionは上書きされません</CardDescription></CardHeader>
          <CardContent className="space-y-1.5 pt-2">
            {loading && !variants.length ? <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div> : variants.length ? variants.map((variant) => <button key={variant.id} type="button" className={`w-full rounded-md border-l-2 px-3 py-2.5 text-left transition-colors ${selectedVariantId === variant.id ? "border-primary bg-primary/5" : "border-transparent hover:border-border hover:bg-muted/40"}`} onClick={() => setSelectedVariantId(variant.id)} aria-current={selectedVariantId === variant.id ? "page" : undefined}><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{PLATFORM_LABELS[variant.platform]}</span><StatusPill status={variant.status} /></div><p className="mt-1 text-[10px] text-muted-foreground">ContentItemに紐づく · {variant.current_revision ? `v${variant.current_revision.version}` : "Revision未設定"}</p></button>) : <div className="rounded-lg border border-dashed border-border px-3 py-7 text-center text-sm text-muted-foreground">まだVariantがありません。右のフォームからtyped variantを作成してください。</div>}
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4">
          <Card size="sm">
            <CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-sm"><Plus className="size-4" /> Typed Variantを追加</CardTitle><CardDescription>Provider固有のJSONやCredentialは扱いません。Platformごとの入力欄だけを送信します。</CardDescription></CardHeader>
            <CardContent className="pt-3"><form className="space-y-4" onSubmit={createVariant} aria-label="ContentVariant create form">
              <details className="rounded-md border border-border/70 bg-muted/10 px-3 py-2"><summary className="cursor-pointer text-xs font-medium">詳細参照（内部ID）</summary><p className="mt-1 text-[11px] text-muted-foreground">通常はPersona・ContentItem・接続先の選択UIから解決します。移行中のデータや高度な診断では内部参照を入力できます。</p><div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><TextField id="content-variant-content-item-id" label="ContentItem（内部参照）" ariaLabel="ContentItem ID" value={contentItemId} onChange={setContentItemId} required /><TextField id="content-variant-persona-revision-id" label="Persona Revision（内部参照）" ariaLabel="Persona Revision ID" value={personaRevisionId} onChange={setPersonaRevisionId} required /><TextField id="content-variant-platform-account-id" label="PlatformAccount（内部参照）" ariaLabel="PlatformAccount ID" value={platformAccountId} onChange={setPlatformAccountId} required /><TextField id="content-variant-platform-account-revision-id" label="Account Revision（内部参照・任意）" ariaLabel="Account Revision ID（任意）" value={platformAccountRevisionId} onChange={setPlatformAccountRevisionId} /></div></details>
              <div className="space-y-1.5"><FieldLabel htmlFor="content-variant-platform">Platform</FieldLabel><AppSelect id="content-variant-platform" value={platform} onValueChange={(value) => { if (PLATFORMS.includes(value as MediaContentPlatform)) setPlatform(value as MediaContentPlatform); }}><option value="x">X</option><option value="pixiv">pixiv</option><option value="dlsite">DLsite</option><option value="patreon">Patreon</option><option value="youtube">YouTube</option><option value="instagram">Instagram</option></AppSelect></div>
              <VariantPayloadFields platform={platform} form={payloadForm} setForm={setPayloadForm} idPrefix="content-variant-payload" />
              <div className="grid gap-3 sm:grid-cols-2"><TextAreaField id="content-variant-output-refs" label="Generation output参照（id | sha256、1行1件）" value={generationOutputRefs} onChange={setGenerationOutputRefs} help="出力を添付しないVariantも作成できますが、Readinessはfail-closedになります。" /><TextAreaField id="content-variant-source-evidence" label="Source / Evidence参照（1行1件）" value={sourceEvidence} onChange={setSourceEvidence} /></div>
              <div className="flex justify-end"><Button type="submit" size="sm" disabled={busy !== null}>{busy === "variant" ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />} Variantを作成</Button></div>
            </form></CardContent>
          </Card>

          {detailLoading ? <Card size="sm"><CardContent className="flex items-center gap-2 py-10 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Variant detailを読み込み中…</CardContent></Card> : selectedVariant ? <>
            <Card size="sm"><CardHeader className="border-b border-border/70"><div className="flex flex-wrap items-center justify-between gap-2"><div><CardTitle className="flex items-center gap-2 text-sm"><ClipboardCheck className="size-4" /> {PLATFORM_LABELS[selectedVariant.platform]} Variant detail</CardTitle><CardDescription className="mt-1">immutable revisionと公開前チェックを確認します</CardDescription></div><StatusPill status={selectedVariant.status} /></div></CardHeader><CardContent className="space-y-3 pt-3"><RevisionSummary revision={currentRevision} /><div className="grid gap-2 text-xs sm:grid-cols-2"><div><span className="text-muted-foreground">ContentItem固定値</span><p className="break-all font-mono">hash: {selectedVariant.content_item_hash ?? "hash unavailable"}</p></div><div><span className="text-muted-foreground">QA / Rights</span><p><StatusPill status={readiness?.qa?.result} /> <StatusPill status={readiness?.rights?.result} /></p></div></div>{readiness ? <div className={`rounded-md border px-3 py-2.5 text-xs ${readiness.ready ? "border-emerald-500/30 bg-emerald-500/5" : "border-amber-500/30 bg-amber-500/5"}`} data-testid="content-variant-readiness"><div className="flex items-center gap-2 font-medium">{readiness.ready ? <CheckCircle2 className="size-4 text-emerald-600" /> : <ShieldCheck className="size-4 text-amber-600" />} {readiness.ready ? "Readiness: 準備完了" : "Readiness: 未完了（fail-closed）"}</div>{!readiness.ready ? <p className="mt-1 text-muted-foreground">{readiness.blocking_reasons.join(" / ") || "QA・Rights・必須Assetを確認してください。"}</p> : null}</div> : <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs">Readiness unavailable — 安全側に倒して処理を進めません。</div>}</CardContent></Card>

            <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="text-sm">Revision履歴</CardTitle><CardDescription>各RevisionはContentItem / Persona / Account / payload hashを固定します。</CardDescription></CardHeader><CardContent className="space-y-2 pt-3">{revisions.length ? revisions.map((revision) => <div key={revision.id} className="rounded-md border border-border/70 px-3 py-2 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold">v{revision.version}</span><span className="font-mono text-muted-foreground">{revision.content_hash}</span></div><p className="mt-1 text-muted-foreground">{revision.platform} · {revision.created_at ?? "created time unavailable"}</p></div>) : <p className="text-sm text-muted-foreground">Revision履歴はありません。</p>}<div className="border-t border-border/70 pt-3"><p className="mb-2 text-xs font-medium">新しいRevisionをappend（上書き不可）</p><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-[11px] text-muted-foreground">現在 v{currentRevision?.version ?? "—"} · platform {PLATFORM_LABELS[platform]}</span><Button type="button" size="sm" variant="outline" onClick={() => void appendRevision()} disabled={!currentRevision || busy !== null}>{busy === "revision" ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />} Revisionを追加</Button></div></div></CardContent></Card>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-sm"><FileCheck2 className="size-4" /> QA Assessment</CardTitle><CardDescription>Policy Revisionと対象Variant Revisionを固定して記録します。</CardDescription></CardHeader><CardContent className="pt-3"><form className="space-y-3" onSubmit={recordQa} aria-label="ContentVariant QA form"><div className="space-y-1.5"><FieldLabel htmlFor="content-variant-qa-result">判定</FieldLabel><AppSelect id="content-variant-qa-result" value={qaResult} onValueChange={(value) => setQaResult(value as AssessmentResult)}><option value="review_required">レビュー要</option><option value="passed">合格</option><option value="failed">不合格</option></AppSelect></div><div className="grid gap-3 sm:grid-cols-2"><TextField id="content-variant-qa-policy-id" label="QA Policy Revision ID" value={qaPolicyRevisionId} onChange={setQaPolicyRevisionId} /><TextField id="content-variant-qa-policy-hash" label="QA Policy hash" value={qaPolicyRevisionHash} onChange={setQaPolicyRevisionHash} /></div><TextAreaField id="content-variant-qa-checks" label="Checks（codeを1行1件）" value={qaChecks} onChange={setQaChecks} /><TextAreaField id="content-variant-qa-findings" label="Findings（1行1件）" value={qaFindings} onChange={setQaFindings} /><TextAreaField id="content-variant-qa-evidence" label="Evidence URL / sha256（1行1件）" value={qaEvidence} onChange={setQaEvidence} /><div className="flex justify-end"><Button type="submit" size="sm" variant="outline" disabled={!currentRevision || busy !== null}>{busy === "qa" ? <Loader2 className="size-3.5 animate-spin" /> : <FileCheck2 className="size-3.5" />} QAを記録</Button></div></form></CardContent></Card>
              <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-sm"><ShieldCheck className="size-4" /> Rights Assessment</CardTitle><CardDescription>権利状態を別Assessmentとしてappend-onlyで記録します。</CardDescription></CardHeader><CardContent className="pt-3"><form className="space-y-3" onSubmit={recordRights} aria-label="ContentVariant Rights form"><div className="space-y-1.5"><FieldLabel htmlFor="content-variant-rights-result">判定</FieldLabel><AppSelect id="content-variant-rights-result" value={rightsResult} onValueChange={(value) => setRightsResult(value as RightsAssessmentResult)}><option value="review_required">レビュー要</option><option value="cleared">権利クリア</option><option value="blocked">ブロック</option></AppSelect></div><div className="grid gap-3 sm:grid-cols-2"><TextField id="content-variant-rights-policy-id" label="Rights Policy Revision ID" value={rightsPolicyRevisionId} onChange={setRightsPolicyRevisionId} /><TextField id="content-variant-rights-policy-hash" label="Rights Policy hash" value={rightsPolicyRevisionHash} onChange={setRightsPolicyRevisionHash} /></div><TextAreaField id="content-variant-rights-checks" label="Checks（codeを1行1件）" value={rightsChecks} onChange={setRightsChecks} /><TextAreaField id="content-variant-rights-findings" label="Findings（1行1件）" value={rightsFindings} onChange={setRightsFindings} /><TextAreaField id="content-variant-rights-evidence" label="Evidence URL / sha256（1行1件）" value={rightsEvidence} onChange={setRightsEvidence} /><div className="flex justify-end"><Button type="submit" size="sm" variant="outline" disabled={!currentRevision || busy !== null}>{busy === "rights" ? <Loader2 className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />} Rightsを記録</Button></div></form></CardContent></Card>
            </div>
          </> : <Card size="sm"><CardContent className="py-10"><div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">左のVariantを選択すると、Revision・QA・Rights・Readinessを表示します。</div></CardContent></Card>}
        </div>
      </div>
    </div>
  );
}
