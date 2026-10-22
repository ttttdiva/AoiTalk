"use client";
import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { SavedVariantPreview } from "./content-variant-panel";
import { PersonaAssetPreview } from "./persona-asset-preview";

export function ProductPreview({ scope, revision, account }: {
  scope: PersonaWorkspaceScope; revision: ContentVariantRevision; account: PlatformAccount;
}) {
  const prepared = revision.payload.type === "fanza_release" && revision.payload.submission_state === "ready_for_review";
  return <section aria-label="商品素材と販売接続先" className="space-y-3 rounded-xl border bg-card p-4">
    <h3 className="text-base font-semibold">販売用の商品素材を確認</h3>
    <dl className="grid gap-3 text-sm sm:grid-cols-2">
      <div><dt className="text-xs text-muted-foreground">制作人格</dt><dd className="mt-1 break-words font-medium">{scope.displayName}</dd></div>
      <div><dt className="text-xs text-muted-foreground">販売接続先</dt><dd className="mt-1 break-words font-medium">{account.platform} · {account.account_ref}</dd></div>
      <div><dt className="text-xs text-muted-foreground">保存済み商品準備状態</dt><dd className="mt-1">{prepared ? "審査準備" : "商品下書き"}</dd></div>
    </dl>
    <SavedVariantPreview revision={revision} />
    <PersonaAssetPreview key={`${revision.id}:${revision.content_hash}`} scope={scope} revision={revision} />
    <details className="text-xs text-muted-foreground"><summary>商品素材の版と内容hash</summary><dl className="mt-2 space-y-1 break-all"><div><dt className="inline">内容hash: </dt><dd className="inline font-mono">{revision.content_hash}</dd></div><div><dt className="inline">内容revision: </dt><dd className="inline">{revision.id}</dd></div><div><dt className="inline">接続先revision: </dt><dd className="inline">{revision.platform_account_revision_id || "未固定"}</dd></div></dl></details>
    <p className="text-xs text-muted-foreground">外部の審査受付・発売・販売状態はManual attemptの結果と証拠で確認します。商品内容の改訂はQA・媒体別内容で新しい版を保存してください。</p>
  </section>;
}
