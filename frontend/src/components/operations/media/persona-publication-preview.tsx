"use client";

import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { SavedVariantPreview } from "./content-variant-panel";
import { PersonaAssetPreview } from "./persona-asset-preview";

export function PersonaPublicationPreview({ scope, revision, account }: {
  scope: PersonaWorkspaceScope; revision: ContentVariantRevision; account: PlatformAccount;
}) {
  const payload = revision.payload;
  const visibility = "visibility" in payload ? payload.visibility
    : "privacy" in payload ? payload.privacy : "audience" in payload ? payload.audience : "public";
  const scheduledAt = "scheduled_at" in payload ? payload.scheduled_at : null;
  return <section aria-label="公開する内容と接続先" className="space-y-3 rounded-xl border bg-card p-4">
    <h3 className="text-base font-semibold">公開する内容を確認</h3>
    <dl className="grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-xs text-muted-foreground">公開人格</dt><dd className="mt-1 break-words font-medium">{scope.displayName}</dd></div>
      <div><dt className="text-xs text-muted-foreground">選択したアカウント</dt><dd className="mt-1 break-words font-medium">{account.platform} · {account.current_revision.display_name || account.account_ref}</dd><dd className="mt-1 break-words text-xs text-muted-foreground">{account.account_ref}</dd></div>
      <div><dt className="text-xs text-muted-foreground">公開範囲</dt><dd className="mt-1">{visibility}</dd></div>
      <div><dt className="text-xs text-muted-foreground">予定時刻</dt><dd className="mt-1 break-words">{scheduledAt || "予約なし"}</dd></div>
    </dl>
    <SavedVariantPreview revision={revision} />
    <PersonaAssetPreview key={`${revision.id}:${revision.content_hash}`} scope={scope} revision={revision} />
    <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">公開対象の版と内容hash</summary><dl className="mt-2 space-y-1 break-all"><div><dt className="inline">Project: </dt><dd className="inline">{scope.projectId}</dd></div><div><dt className="inline">人格revision: </dt><dd className="inline">{revision.persona_revision_id}</dd></div><div><dt className="inline">保存版: </dt><dd className="inline">{revision.version}</dd></div><div><dt className="inline">内容hash: </dt><dd className="inline font-mono">{revision.content_hash}</dd></div><div><dt className="inline">内容revision: </dt><dd className="inline">{revision.id}</dd></div><div><dt className="inline">接続先revision: </dt><dd className="inline">{revision.platform_account_revision_id || "未固定"}</dd></div></dl></details>
    <p className="text-xs text-muted-foreground">内容・接続先・公開範囲・予定時刻を変更する場合は、QA・媒体別内容で新しい版を保存してからpreviewを確認してください。</p>
  </section>;
}
