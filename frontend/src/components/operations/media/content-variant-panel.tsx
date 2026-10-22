"use client";

import { accountInScope, variantInScope, type PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import { PersonaAssetPreview } from "./persona-asset-preview";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
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
  type TikTokContentPayload,
  type InstagramContentPayload,
  type MediaContentPlatform,
  type PatreonContentPayload,
  type PixivContentPayload,
  type PlatformContentPayload,
  type RightsAssessmentResult,
  type VariantReadiness,
  type YoutubeContentPayload,
} from "@/lib/media-operations-content-api";
import { emptyProductDraft, isProductPlatform, productDraftError, productDraftFromPayload, productPayloadFromDraft } from "@/lib/media-product-draft";
import { ProductDraftEditor } from "./product-draft-editor";
import { ProductAssetsPicker } from "./product-assets-picker";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const PLATFORMS: readonly MediaContentPlatform[] = [
  "x",
  "pixiv",
  "dlsite",
  "patreon",
  "youtube",
  "instagram",
  "fanza",
  "tiktok",
];

const PLATFORM_LABELS: Record<MediaContentPlatform, string> = {
  x: "X",
  pixiv: "pixiv",
  dlsite: "DLsite",
  patreon: "Patreon",
  youtube: "YouTube",
  instagram: "Instagram",
  fanza: "FANZA",
  tiktok: "TikTok",
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
  thumbnail_asset_id: string;
  audience: string;
  tier_refs: string;
  media_asset_id: string;
  captions_ref: string;
  visibility: string;
  audience_setting: string;
  disclosure: string;
  tier_scope: string;
  tiktok_privacy: TikTokContentPayload["privacy"] | "";
  music_rights: TikTokContentPayload["music_rights"];
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
    thumbnail_asset_id: "",
    audience: "public",
    tier_refs: "",
    media_asset_id: "",
    captions_ref: "",
    visibility: "private",
    audience_setting: "unknown",
    disclosure: "none",
    tier_scope: "",
    tiktok_privacy: "",
    music_rights: "review_required",
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
  disabled = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
  ariaLabel?: string;
  disabled?: boolean;
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
        disabled={disabled}
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
      <span className="inline-flex items-center gap-1">
        <FieldLabel htmlFor={id}>{label}</FieldLabel>
        {help ? <HelpPopover label={label}>{help}</HelpPopover> : null}
      </span>
      <Textarea
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        required={required}
        rows={3}
      />
    </div>
  );
}

function checkPayloadRequired(
  platform: MediaContentPlatform,
  form: PayloadForm,
): string | null {
  if (platform === "x" && !form.text.trim()) return "X本文を入力してください";
  if (platform === "pixiv" && (!form.title.trim() || !splitList(form.media_asset_ids).length)) return "pixivタイトルとMedia Assetを入力してください";
  if (platform === "tiktok" && (!form.tiktok_privacy || splitList(form.media_asset_id).length !== 1)) return "TikTok動画Asset 1件と公開範囲の明示選択が必要です";
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
    case "dlsite":
    case "fanza":
      throw new Error("商品は専用フォームから保存してください");
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
    case "tiktok":
      if (!form.tiktok_privacy) throw new Error("公開範囲を選択してください");
      return {
        type: "tiktok_video", caption: form.caption.trim(),
        media_asset: splitList(form.media_asset_id), privacy: form.tiktok_privacy,
        music_rights: form.music_rights, ai_disclosure: form.ai_generated,
        cover: splitList(form.thumbnail_asset_id),
        ...(scheduledAt ? { scheduled_at: scheduledAt } : {}),
      } satisfies TikTokContentPayload;
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

  if (platform === "tiktok") {
    return <div className="space-y-3">
      <TextAreaField id={`${idPrefix}-caption`} label="TikTokキャプション" value={form.caption} onChange={field("caption")} />
      <TextField id={`${idPrefix}-video`} label="Video Asset ID" value={form.media_asset_id} onChange={field("media_asset_id")} required />
      <TextField id={`${idPrefix}-cover`} label="Cover Asset ID（任意）" value={form.thumbnail_asset_id} onChange={field("thumbnail_asset_id")} />
      <div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-privacy`}>公開範囲（選択必須）</FieldLabel><AppSelect id={`${idPrefix}-privacy`} value={form.tiktok_privacy} onValueChange={(value) => setForm((current) => ({ ...current, tiktok_privacy: value as PayloadForm["tiktok_privacy"] }))}><option value="">選択してください</option><option value="SELF_ONLY">自分のみ</option><option value="MUTUAL_FOLLOW_FRIENDS">相互フォロー</option><option value="FOLLOWER_OF_CREATOR">フォロワー</option><option value="PUBLIC_TO_EVERYONE">公開</option></AppSelect></div>
      <div className="space-y-1.5"><FieldLabel htmlFor={`${idPrefix}-music`}>音源の権利</FieldLabel><AppSelect id={`${idPrefix}-music`} value={form.music_rights} onValueChange={(value) => setForm((current) => ({ ...current, music_rights: value as PayloadForm["music_rights"] }))}><option value="review_required">確認が必要</option><option value="owned">自作・保有</option><option value="licensed">許諾済み</option><option value="no_music">音楽なし</option></AppSelect></div>
      <label className="flex items-center gap-2 text-xs"><Checkbox checked={form.ai_generated} onCheckedChange={(checked) => setForm((current) => ({ ...current, ai_generated: checked === true }))} />AI生成表示</label>
      <div className="flex items-center gap-1 text-xs">
        <p>API監査・アカウント適格性・投稿前同意が未確認の間は自動投稿しません。</p>
        <HelpPopover label="TikTok下書きの扱い">手動引継ぎ用の下書きです。</HelpPopover>
      </div>
    </div>;
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
      <div><span className="text-muted-foreground">Persona方針</span><p className="break-all">v{revision.version} · {revision.persona_revision_hash}</p></div>
      <div><span className="text-muted-foreground">PlatformAccount</span><p>{revision.platform_account_id ? "接続先に固定されています" : "接続先未設定"}</p></div>
      <div><span className="text-muted-foreground">Platform</span><p>{PLATFORM_LABELS[revision.platform]}</p></div>
    </div>
  );
}

const SAVED_FORMAT_LABELS: Record<PlatformContentPayload["type"], string> = {
  x_post: "X / 単独投稿", x_thread: "X / スレッド", pixiv_work: "pixiv / 作品",
  dlsite_release: "DLsite / 販売用パッケージ", fanza_release: "FANZA / 販売用パッケージ",
  patreon_post: "Patreon / 投稿", youtube_video: "YouTube / Long-form",
  youtube_short: "YouTube / Shorts", instagram_feed: "Instagram / Feed image",
  instagram_carousel: "Instagram / Carousel", instagram_reel: "Instagram / Reel",
  tiktok_video: "TikTok / 動画",
};

export function SavedVariantPreview({ revision }: { revision: ContentVariantRevision }) {
  const payload = revision.payload;
  const title = "title" in payload ? payload.title : "";
  const body = "text" in payload ? payload.text : "caption" in payload ? payload.caption
    : "body" in payload ? payload.body : "description" in payload ? payload.description : "";
  const mediaCount = "media_asset" in payload ? payload.media_asset.length
    : "media" in payload ? (payload.media?.length ?? 0)
    : "preview_assets" in payload ? payload.preview_assets.length
    : "attachments" in payload ? (payload.attachments?.length ?? 0) : 0;
  return <section aria-label="保存済みコンテンツのプレビュー" className="space-y-3 rounded-md border border-border/70 bg-muted/10 p-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm font-semibold">{SAVED_FORMAT_LABELS[payload.type]}</p><span className="text-xs text-muted-foreground">保存済み v{revision.version}</span></div>
    {title ? <p className="font-medium">{title}</p> : null}
    {body ? <p className="whitespace-pre-wrap break-words text-sm">{body}</p> : null}
    {payload.type === "x_thread" ? <ol className="list-decimal space-y-2 pl-5 text-sm">{payload.posts.map((post, index) => <li key={index} className="whitespace-pre-wrap break-words">{post.text}</li>)}</ol> : <p className="text-xs text-muted-foreground">素材: {mediaCount}件</p>}
    {(payload.type === "youtube_video" || payload.type === "youtube_short") ? <p className="text-xs text-muted-foreground">公開設定: {payload.visibility}</p> : null}
    {(payload.type === "x_post" || payload.type === "x_thread") && <div className="space-y-1 break-words text-xs text-muted-foreground">
      {payload.reply_to && <p>返信先: {payload.reply_to}</p>}
      {payload.type === "x_post" && <>
        {!!payload.hashtags?.length && <p>本文に追加するhashtags: {payload.hashtags.join(" ")}</p>}
        {!!payload.links?.length && <p className="break-all">本文に追加するlinks: {payload.links.join(" / ")}</p>}
        {payload.alt_text && <p>代替テキスト: {payload.alt_text}</p>}
        <p>センシティブな内容: {payload.sensitive_content ? "指定あり" : "指定なし"}</p>
      </>}
      {payload.type === "x_thread" && payload.posts.map((post, i) => <p key={i}>投稿{i + 1}: 添付{post.media?.length ?? 0}件{post.alt_text ? ` · 代替テキスト: ${post.alt_text}` : ""}</p>)}
    </div>}
    {(payload.type === "youtube_video" || payload.type === "youtube_short") && <div className="space-y-1 break-words text-xs text-muted-foreground">
      <p>Tags: {payload.tags.join(" / ") || "なし"}</p>
      <p>Audience: {payload.audience || "未指定"} · AI利用の開示: {payload.disclosure || "未指定"}</p>
      <p className="break-all">Thumbnail: {payload.thumbnail?.join(" / ") || "指定なし"}</p>
      <p className="break-all">Captions: {payload.captions?.join(" / ") || "指定なし"}</p>
    </div>}
    {"scheduled_at" in payload && payload.scheduled_at && <p className="break-words text-xs">予定時刻: {payload.scheduled_at}</p>}
    <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">保存済みの全公開設定</summary><pre className="mt-2 whitespace-pre-wrap break-all">{JSON.stringify(payload, null, 2)}</pre></details>
    {payload.type === "tiktok_video" ? <p className="text-xs text-muted-foreground">公開範囲: {payload.privacy} · 音源の権利: {payload.music_rights}</p> : null}
    {(payload.type === "dlsite_release" || payload.type === "fanza_release") ? <p className="text-xs text-muted-foreground">販売設定: {payload.price} {payload.sales.currency} · 審査・発売・販売の結果はこの下書きから確定しません。</p> : null}
    {payload.type === "fanza_release" ? <p className="text-xs text-muted-foreground">売場: {payload.storefront === "commercial" ? "商業" : "同人"} · {payload.submission_state === "ready_for_review" ? "審査提出の準備" : "商品下書き"} · AI利用: {payload.ai_usage}</p> : null}
  </section>;
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `media-content-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const EMPTY_PLATFORM_ACCOUNTS: PlatformAccount[] = [];

type MediaContentVariantPanelProps = { scope?: PersonaWorkspaceScope; accounts?: PlatformAccount[]; initialContentItemId?: string; initialOutputRef?: string };

export function MediaContentVariantPanel(props: MediaContentVariantPanelProps = {}) {
  // A new URL scope must never render the previous scope's selection or draft,
  // even while its list request is pending. Separate instances also fence A→B→A.
  const scopeKey = JSON.stringify([
    props.scope?.ownerUserId,
    props.scope?.projectId,
    props.scope?.personaId,
    props.scope?.personaRevisionId,
    props.initialContentItemId?.trim() || null,
    props.initialOutputRef ?? null,
  ]);
  return <ScopedMediaContentVariantPanel key={scopeKey} {...props} />;
}

function ScopedMediaContentVariantPanel({ scope, accounts = EMPTY_PLATFORM_ACCOUNTS, initialContentItemId, initialOutputRef }: MediaContentVariantPanelProps) {
  // Browsing is bound to the URL, never to the editable create-form field.
  const contentScopeId = initialContentItemId?.trim() || undefined;
  const [variants, setVariants] = useState<ContentVariant[]>([]);
  const [selectedVariantId, setSelectedVariantId] = useState("");
  const selectedVariantIdRef = useRef("");
  const detailRequest = useRef(0);
  const listRequest = useRef(0);
  const mounted = useRef(true);
  const [selectedVariant, setSelectedVariant] = useState<ContentVariant | null>(null);
  const [revisions, setRevisions] = useState<ContentVariantRevision[]>([]);
  const [readiness, setReadiness] = useState<VariantReadiness | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState<"variant" | "revision" | "qa" | "rights" | null>(null);
  const [error, setError] = useState<unknown>(null);

  const [contentItemId, setContentItemId] = useState(contentScopeId ?? "");
  const [personaRevisionId, setPersonaRevisionId] = useState(scope?.personaRevisionId ?? "");
  const [platformAccountId, setPlatformAccountId] = useState("");
  const [platformAccountRevisionId, setPlatformAccountRevisionId] = useState("");
  const [platform, setPlatform] = useState<MediaContentPlatform>("x");
  const [payloadForm, setPayloadForm] = useState<PayloadForm>(() => {
    const form = emptyPayloadForm();
    const selected = (initialOutputRef ?? "").split(/\r?\n/).map(line => line.split("|")[0].trim()).filter(id => /^[A-Za-z0-9_-]{1,128}$/.test(id));
    return selected.length ? { ...form, media_asset_ids: selected.join("\n"), media_asset_id: selected[0] } : form;
  });
  const [productDraft, setProductDraft] = useState(emptyProductDraft);
  const [generationOutputRefs, setGenerationOutputRefs] = useState(initialOutputRef ?? "");
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

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      detailRequest.current += 1;
      listRequest.current += 1;
    };
  }, []);

  const selectVariant = useCallback((id: string) => {
    if (selectedVariantIdRef.current === id) return;
    selectedVariantIdRef.current = id;
    detailRequest.current += 1;
    setSelectedVariantId(id);
    setSelectedVariant(null);
    setRevisions([]);
    setReadiness(null);
    setDetailLoading(Boolean(id));
    setError(null);
  }, []);

  const loadVariants = useCallback(async () => {
    if (!mounted.current) return;
    const request = ++listRequest.current;
    setLoading(true);
    setError(null);
    try {
      const response = await mediaContentApi.listVariants({
        ...(scope ? { projectId: scope.projectId } : {}),
        ...(contentScopeId ? { contentItemId: contentScopeId } : {}),
      });
      const next = response.filter(item =>
        (!contentScopeId || item.content_item_id === contentScopeId) &&
        (!scope || variantInScope(item, scope, accounts)),
      );
      if (!mounted.current || request !== listRequest.current) return;
      setVariants(next);
      const current = selectedVariantIdRef.current;
      selectVariant(next.some((item) => item.id === current) ? current : next[0]?.id ?? "");
    } catch (nextError) {
      if (mounted.current && request === listRequest.current) {
        setVariants([]);
        selectVariant("");
        setError(nextError);
      }
    } finally {
      if (mounted.current && request === listRequest.current) setLoading(false);
    }
  }, [selectVariant, scope, accounts, contentScopeId]);

  const loadDetail = useCallback(async (variantId: string) => {
    if (!mounted.current || selectedVariantIdRef.current !== variantId) return;
    const request = ++detailRequest.current;
    const current = () => mounted.current && request === detailRequest.current && selectedVariantIdRef.current === variantId;
    if (!variantId) {
      setSelectedVariant(null);
      setRevisions([]);
      setReadiness(null);
      return;
    }
    setDetailLoading(true);
    setSelectedVariant(null);
    setRevisions([]);
    setReadiness(null);
    setError(null);
    try {
      const [detail, nextRevisions, nextReadiness] = await Promise.all([
        mediaContentApi.getVariant(variantId),
        mediaContentApi.listRevisions(variantId),
        mediaContentApi.getReadiness(variantId),
      ]);
      if (!current()) return;
      if (detail.id !== variantId || (contentScopeId && detail.content_item_id !== contentScopeId)) throw new Error("選択中のContentItemに属するVariantを確認できませんでした。");
      if (scope && !variantInScope(detail, scope, accounts)) throw new Error("この人格に属するVariantを確認できませんでした。");
      const revisionMatches = (revision: ContentVariantRevision) => revision.content_variant_id === variantId && revision.content_item_id === detail.content_item_id;
      if ((detail.current_revision && !revisionMatches(detail.current_revision)) || !nextRevisions.every(revisionMatches)) throw new Error("選択中のContentItemに属するRevisionを確認できませんでした。");
      setSelectedVariant(detail);
      setRevisions(nextRevisions);
      setReadiness(nextReadiness);
    } catch (nextError) {
      if (current()) setError(nextError);
    } finally {
      if (current()) setDetailLoading(false);
    }
  }, [scope, accounts, contentScopeId]);

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
    payload: isProductPlatform(platform) ? productPayloadFromDraft(platform, productDraft) : payloadFromForm(platform, payloadForm),
    generation_output_refs: outputRefsFromText(generationOutputRefs),
    source_evidence: sourceEvidenceFromText(sourceEvidence),
  }), [contentItemId, generationOutputRefs, productDraft, payloadForm, personaRevisionId, platform, platformAccountId, platformAccountRevisionId, sourceEvidence]);

  const createVariant = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (contentScopeId && contentItemId.trim() !== contentScopeId) {
      setError(new Error("選択中のContentItemと作成先が一致しません。作成先を確認してください。"));
      return;
    }
    if (!contentItemId.trim() || !personaRevisionId.trim() || !platformAccountId.trim()) {
      setError(new Error("ContentItem・Persona revision・PlatformAccountのIDが必要です"));
      return;
    }
    if (scope && (personaRevisionId !== scope.personaRevisionId || !accounts.some(account => account.id === platformAccountId && account.platform === platform && accountInScope(account, scope)))) {
      setError(new Error("この人格の接続先を選択してください。")); return;
    }
    const payloadError = isProductPlatform(platform) ? productDraftError(platform, productDraft) : checkPayloadRequired(platform, payloadForm);
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
      if (!mounted.current) return;
      if (contentScopeId && created.content_item_id !== contentScopeId) throw new Error("保存されたVariantは選択中のContentItemと一致しません。更新して保存結果を確認してください。");
      if (scope && !variantInScope(created, scope, accounts)) throw new Error("保存された内容の人格・接続先を確認できません。更新して保存結果を確認してください。");
      setVariants((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      selectVariant(created.id);
      setPayloadForm(emptyPayloadForm());
      if (isProductPlatform(platform)) setProductDraft(emptyProductDraft());
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const loadSavedProduct = () => {
    if (!currentRevision || !isProductPlatform(currentRevision.platform) || (currentRevision.payload.type !== "dlsite_release" && currentRevision.payload.type !== "fanza_release")) return;
    setPlatform(currentRevision.platform);
    setContentItemId(currentRevision.content_item_id);
    setPersonaRevisionId(currentRevision.persona_revision_id);
    setPlatformAccountId(currentRevision.platform_account_id);
    setPlatformAccountRevisionId(currentRevision.platform_account_revision_id ?? "");
    setProductDraft(productDraftFromPayload(currentRevision.payload));
    setGenerationOutputRefs(currentRevision.generation_output_refs.map(ref => `${ref.generation_output_id} | ${ref.sha256}`).join("\n"));
    setSourceEvidence(currentRevision.source_evidence.map(ref => ref.type === "url" ? ref.url : ref.sha256).join("\n"));
    setError(null);
  };

  const appendRevision = async () => {
    if (!selectedVariant || !currentRevision) return;
    if (scope && (personaRevisionId !== scope.personaRevisionId || platform !== selectedVariant.platform || !accounts.some(account => account.id === platformAccountId && account.platform === platform && accountInScope(account, scope)))) {
      setError(new Error("この人格の同じ媒体の接続先を選択してください。")); return;
    }
    const payloadError = isProductPlatform(platform) ? productDraftError(platform, productDraft) : checkPayloadRequired(platform, payloadForm);
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
        payload: isProductPlatform(platform) ? productPayloadFromDraft(platform, productDraft) : payloadFromForm(platform, payloadForm),
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
          <h2 className="mt-1 inline-flex items-center gap-1 text-xl font-semibold tracking-tight">
            ContentVariant
            <HelpPopover label="ContentVariant">ContentItemを各Platformのtyped payloadへ展開し、immutable revision・QA・Rightsを一つの監査可能な流れで確認します。</HelpPopover>
          </h2>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={() => void loadVariants()} disabled={loading}><RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} /> 更新</Button>
      </div>

      <ErrorNotice error={error} />

      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(15rem,23rem)_minmax(0,1fr)]">
        <Card size="sm" className="min-w-0">
          <CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Variant一覧<HelpPopover label="Variant一覧">既存Revisionは上書きされません。</HelpPopover></CardTitle><p className="text-sm text-muted-foreground">{variants.length}件</p></CardHeader>
          <CardContent className="space-y-1.5 pt-2">
            {loading && !variants.length ? <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> 読み込み中…</div> : variants.length ? variants.map((variant) => <button key={variant.id} type="button" className={`w-full rounded-md border-l-2 px-3 py-2.5 text-left transition-colors ${selectedVariantId === variant.id ? "border-primary bg-primary/5" : "border-transparent hover:border-border hover:bg-muted/40"}`} onClick={() => selectVariant(variant.id)} aria-current={selectedVariantId === variant.id ? "page" : undefined}><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">{PLATFORM_LABELS[variant.platform]}</span><StatusPill status={variant.status} /></div><p className="mt-1 text-[10px] text-muted-foreground">ContentItemに紐づく · {variant.current_revision ? `v${variant.current_revision.version}` : "Revision未設定"}</p></button>) : <div className="rounded-lg border border-dashed border-border px-3 py-7 text-center text-sm text-muted-foreground">まだVariantがありません。右のフォームからtyped variantを作成してください。</div>}
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4">
          <Card size="sm">
            <CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-sm"><Plus className="size-4" /><span className="inline-flex items-center gap-1">Typed Variantを追加<HelpPopover label="Typed Variantを追加">Provider固有のJSONやCredentialは扱いません。Platformごとの入力欄だけを送信します。</HelpPopover></span></CardTitle></CardHeader>
            <CardContent className="pt-3"><form className="space-y-4" onSubmit={createVariant} aria-label="ContentVariant create form">
              {scope && <p className="text-sm">{contentItemId ? "選択した制作briefをこの媒体の内容に展開します。" : "制作briefの工程で、内容を作る企画を選択してください。"}</p>}
              <div className="relative rounded-md border border-border/70 bg-muted/10 px-3 py-2">
                <details>
                  <summary className="cursor-pointer pr-7 text-xs font-medium">詳細参照（内部ID）</summary>
                  <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <TextField disabled={!!scope} id="content-variant-content-item-id" label="ContentItem（内部参照）" ariaLabel="ContentItem ID" value={contentItemId} onChange={setContentItemId} required />
                    <TextField disabled={!!scope} id="content-variant-persona-revision-id" label="Persona Revision（内部参照）" ariaLabel="Persona Revision ID" value={personaRevisionId} onChange={setPersonaRevisionId} required />
                    <TextField disabled={!!scope} id="content-variant-platform-account-id" label="PlatformAccount（内部参照）" ariaLabel="PlatformAccount ID" value={platformAccountId} onChange={setPlatformAccountId} required />
                    <TextField disabled={!!scope} id="content-variant-platform-account-revision-id" label="Account Revision（内部参照・任意）" ariaLabel="Account Revision ID（任意）" value={platformAccountRevisionId} onChange={setPlatformAccountRevisionId} />
                  </div>
                </details>
                <span className="absolute right-3 top-2"><HelpPopover label="詳細参照（内部ID）">通常はPersona・ContentItem・接続先の選択UIから解決します。移行中のデータや高度な診断では内部参照を入力できます。</HelpPopover></span>
              </div>
              <div className="space-y-1.5"><FieldLabel htmlFor="content-variant-platform">Platform</FieldLabel><AppSelect id="content-variant-platform" value={platform} onValueChange={(value) => { if (PLATFORMS.includes(value as MediaContentPlatform)) { setPlatform(value as MediaContentPlatform); if (scope) { setPlatformAccountId(""); setPlatformAccountRevisionId(""); } } }}><option value="x">X</option><option value="pixiv">pixiv</option><option value="dlsite">DLsite</option><option value="patreon">Patreon</option><option value="youtube">YouTube</option><option value="instagram">Instagram</option><option value="fanza">FANZA</option><option value="tiktok">TikTok</option></AppSelect></div>
              {scope && <div className="space-y-1.5"><span className="inline-flex items-center gap-1"><FieldLabel htmlFor="content-variant-account">この人格の接続先</FieldLabel><HelpPopover label="この人格の接続先">媒体別の内容を保存し、QAと権利確認を記録します。</HelpPopover></span><AppSelect id="content-variant-account" value={platformAccountId} onValueChange={id => { const account = accounts.find(a => a.id === id && a.platform === platform && accountInScope(a, scope)); setPlatformAccountId(account?.id ?? ""); setPlatformAccountRevisionId(account?.current_revision.id ?? ""); }}><option value="">接続先を選択</option>{accounts.filter(a => a.platform === platform && accountInScope(a, scope)).map(a => <option key={a.id} value={a.id}>{a.account_ref} · {a.platform}</option>)}</AppSelect></div>}
              {isProductPlatform(platform) ? <div className="space-y-3"><ProductDraftEditor platform={platform} value={productDraft} onChange={setProductDraft} idPrefix="content-variant-payload" />{scope && <ProductAssetsPicker key={`${scope.ownerUserId}:${scope.projectId}:${scope.personaId}:${contentItemId}:${platform}:${selectedVariantId}`} scope={scope} contentItemId={contentItemId} value={productDraft} onChange={setProductDraft} />}</div> : <VariantPayloadFields platform={platform} form={payloadForm} setForm={setPayloadForm} idPrefix="content-variant-payload" />}
              <div className="grid gap-3 sm:grid-cols-2"><TextAreaField id="content-variant-output-refs" label="Generation output参照（id | sha256、1行1件）" value={generationOutputRefs} onChange={setGenerationOutputRefs} help="出力を添付しないVariantも作成できますが、Readinessはfail-closedになります。" /><TextAreaField id="content-variant-source-evidence" label="Source / Evidence参照（1行1件）" value={sourceEvidence} onChange={setSourceEvidence} /></div>
              <div className="flex justify-end"><Button type="submit" size="sm" disabled={busy !== null}>{busy === "variant" ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />} Variantを作成</Button></div>
            </form></CardContent>
          </Card>

          {detailLoading ? <Card size="sm"><CardContent className="flex items-center gap-2 py-10 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Variant detailを読み込み中…</CardContent></Card> : selectedVariant ? <>
            <Card size="sm"><CardHeader className="border-b border-border/70"><div className="flex flex-wrap items-center justify-between gap-2"><div><CardTitle className="flex items-center gap-2 text-sm"><ClipboardCheck className="size-4" /><span className="inline-flex items-center gap-1">{PLATFORM_LABELS[selectedVariant.platform]} Variant detail<HelpPopover label={`${PLATFORM_LABELS[selectedVariant.platform]} Variant detail`}>immutable revisionと公開前チェックを確認します。</HelpPopover></span></CardTitle></div><StatusPill status={selectedVariant.status} /></div></CardHeader><CardContent className="space-y-3 pt-3"><RevisionSummary revision={currentRevision} /><div className="grid gap-2 text-xs sm:grid-cols-2"><div><span className="text-muted-foreground">ContentItem固定値</span><p className="break-all font-mono">hash: {selectedVariant.content_item_hash ?? "hash unavailable"}</p></div><div><span className="text-muted-foreground">QA / Rights</span><p><StatusPill status={readiness?.qa?.result} /> <StatusPill status={readiness?.rights?.result} /></p></div></div>{readiness ? <div className={`rounded-md border px-3 py-2.5 text-xs ${readiness.ready ? "border-emerald-500/30 bg-emerald-500/5" : "border-amber-500/30 bg-amber-500/5"}`} data-testid="content-variant-readiness"><div className="flex items-center gap-2 font-medium">{readiness.ready ? <CheckCircle2 className="size-4 text-emerald-600" /> : <ShieldCheck className="size-4 text-amber-600" />} {readiness.ready ? "Readiness: 準備完了" : "Readiness: 未完了（fail-closed）"}</div>{!readiness.ready ? <p className="mt-1 text-muted-foreground">{readiness.blocking_reasons.join(" / ") || "QA・Rights・必須Assetを確認してください。"}</p> : null}</div> : <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs">Readiness unavailable — 安全側に倒して処理を進めません。</div>}</CardContent></Card>

            {currentRevision ? <SavedVariantPreview revision={currentRevision} /> : null}
            {currentRevision && scope ? <PersonaAssetPreview key={`${currentRevision.id}:${currentRevision.content_hash}`} scope={scope} revision={currentRevision} /> : null}

            <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Revision履歴<HelpPopover label="Revision履歴">各RevisionはContentItem / Persona / Account / payload hashを固定します。</HelpPopover></CardTitle></CardHeader><CardContent className="space-y-2 pt-3">{revisions.length ? revisions.map((revision) => <div key={revision.id} className="rounded-md border border-border/70 px-3 py-2 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold">v{revision.version}</span><span className="font-mono text-muted-foreground">{revision.content_hash}</span></div><p className="mt-1 text-muted-foreground">{revision.platform} · {revision.created_at ?? "created time unavailable"}</p></div>) : <p className="text-sm text-muted-foreground">Revision履歴はありません。</p>}<div className="border-t border-border/70 pt-3"><p className="mb-2 text-xs font-medium">新しいRevisionをappend（上書き不可）</p>{currentRevision && isProductPlatform(currentRevision.platform) && <Button type="button" size="sm" variant="outline" onClick={loadSavedProduct} disabled={busy !== null}>保存済み商品を編集フォームに読込</Button>}<div className="flex flex-wrap items-center justify-between gap-2"><span className="text-[11px] text-muted-foreground">現在 v{currentRevision?.version ?? "—"} · platform {PLATFORM_LABELS[platform]}</span><Button type="button" size="sm" variant="outline" onClick={() => void appendRevision()} disabled={!currentRevision || busy !== null}>{busy === "revision" ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />} Revisionを追加</Button></div></div></CardContent></Card>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-sm"><FileCheck2 className="size-4" /><span className="inline-flex items-center gap-1">QA Assessment<HelpPopover label="QA Assessment">Policy Revisionと対象Variant Revisionを固定して記録します。</HelpPopover></span></CardTitle></CardHeader><CardContent className="pt-3"><form className="space-y-3" onSubmit={recordQa} aria-label="ContentVariant QA form"><div className="space-y-1.5"><FieldLabel htmlFor="content-variant-qa-result">判定</FieldLabel><AppSelect id="content-variant-qa-result" value={qaResult} onValueChange={(value) => setQaResult(value as AssessmentResult)}><option value="review_required">レビュー要</option><option value="passed">合格</option><option value="failed">不合格</option></AppSelect></div><div className="grid gap-3 sm:grid-cols-2"><TextField id="content-variant-qa-policy-id" label="QA Policy Revision ID" value={qaPolicyRevisionId} onChange={setQaPolicyRevisionId} /><TextField id="content-variant-qa-policy-hash" label="QA Policy hash" value={qaPolicyRevisionHash} onChange={setQaPolicyRevisionHash} /></div><TextAreaField id="content-variant-qa-checks" label="Checks（codeを1行1件）" value={qaChecks} onChange={setQaChecks} /><TextAreaField id="content-variant-qa-findings" label="Findings（1行1件）" value={qaFindings} onChange={setQaFindings} /><TextAreaField id="content-variant-qa-evidence" label="Evidence URL / sha256（1行1件）" value={qaEvidence} onChange={setQaEvidence} /><div className="flex justify-end"><Button type="submit" size="sm" variant="outline" disabled={!currentRevision || busy !== null}>{busy === "qa" ? <Loader2 className="size-3.5 animate-spin" /> : <FileCheck2 className="size-3.5" />} QAを記録</Button></div></form></CardContent></Card>
              <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-sm"><ShieldCheck className="size-4" /><span className="inline-flex items-center gap-1">Rights Assessment<HelpPopover label="Rights Assessment">権利状態を別Assessmentとしてappend-onlyで記録します。</HelpPopover></span></CardTitle></CardHeader><CardContent className="pt-3"><form className="space-y-3" onSubmit={recordRights} aria-label="ContentVariant Rights form"><div className="space-y-1.5"><FieldLabel htmlFor="content-variant-rights-result">判定</FieldLabel><AppSelect id="content-variant-rights-result" value={rightsResult} onValueChange={(value) => setRightsResult(value as RightsAssessmentResult)}><option value="review_required">レビュー要</option><option value="cleared">権利クリア</option><option value="blocked">ブロック</option></AppSelect></div><div className="grid gap-3 sm:grid-cols-2"><TextField id="content-variant-rights-policy-id" label="Rights Policy Revision ID" value={rightsPolicyRevisionId} onChange={setRightsPolicyRevisionId} /><TextField id="content-variant-rights-policy-hash" label="Rights Policy hash" value={rightsPolicyRevisionHash} onChange={setRightsPolicyRevisionHash} /></div><TextAreaField id="content-variant-rights-checks" label="Checks（codeを1行1件）" value={rightsChecks} onChange={setRightsChecks} /><TextAreaField id="content-variant-rights-findings" label="Findings（1行1件）" value={rightsFindings} onChange={setRightsFindings} /><TextAreaField id="content-variant-rights-evidence" label="Evidence URL / sha256（1行1件）" value={rightsEvidence} onChange={setRightsEvidence} /><div className="flex justify-end"><Button type="submit" size="sm" variant="outline" disabled={!currentRevision || busy !== null}>{busy === "rights" ? <Loader2 className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />} Rightsを記録</Button></div></form></CardContent></Card>
            </div>
          </> : <Card size="sm"><CardContent className="py-10"><div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">左のVariantを選択すると、Revision・QA・Rights・Readinessを表示します。</div></CardContent></Card>}
        </div>
      </div>
    </div>
  );
}
