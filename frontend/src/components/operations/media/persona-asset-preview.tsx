"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { mediaAssetsApi, assetInScope, assetPin, assetPreviewUrl, publicationAssetRefs } from "@/lib/media-assets-api";
import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";

/** Backend verifies bytes before serving each authenticated immutable preview. */
export function PersonaAssetPreview({ scope, revision, onVerified }: { scope: PersonaWorkspaceScope; revision: ContentVariantRevision; onVerified?: (ready: boolean) => void }) {
  const refs = useMemo(() => publicationAssetRefs(revision.payload), [revision.payload]);
  const ids = useMemo(() => [...new Set(refs.map(r => r.id))], [refs]);
  const [loaded, setLoaded] = useState<string[]>([]);
  const [failed, setFailed] = useState(false);
  const [previewEpoch, setPreviewEpoch] = useState(0);
  const currentEpoch = useRef(0);
  const assets = useSWR(ids.length ? ["persona-asset-preview", scope.ownerUserId, scope.projectId, scope.personaId, revision.id, revision.content_hash, ids.join(":")] : null, async () => {
    return Promise.all(ids.map(async outputId => {
      const sha = assetPin(revision, outputId);
      if (!sha) throw new Error("添付素材の保存hashが固定されていません。QA・媒体別内容で素材のhashを保存してください。");
      const asset = await mediaAssetsApi.get(outputId);
      if (!assetInScope(asset, scope, outputId, sha)) throw new Error("添付素材の人格・Project・保存hashが一致しません。素材の登録記録を確認してください。");
      return asset;
    }));
  }, { shouldRetryOnError: false, revalidateOnFocus: false });
  const ready = !refs.length || Boolean(assets.data && !assets.error && !assets.isValidating && !failed && ids.every(outputId => loaded.includes(outputId)));
  useEffect(() => { onVerified?.(ready); return () => onVerified?.(false); }, [ready, onVerified]);
  if (!refs.length) return null;
  return <section aria-label="添付画像・動画の実体preview" className="min-w-0 space-y-3 rounded-lg border p-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-sm font-semibold">添付画像・動画</h4><Button size="sm" variant="outline" disabled={assets.isValidating} onClick={() => { currentEpoch.current += 1; onVerified?.(false); setFailed(false); setLoaded([]); setPreviewEpoch(currentEpoch.current); void assets.mutate(); }}>素材previewを再確認</Button></div>
    {assets.error || failed ? <p role="alert" className="text-xs text-destructive">{assets.error instanceof Error && assets.error.message.startsWith("添付素材") ? assets.error.message : "添付画像・動画の実体を表示できません。制作工程で既存output・画像・動画を登録し、保存hashと閲覧権限を確認してください。"}</p> : assets.isValidating ? <p role="status" className="text-xs">添付素材の保存版を確認中…</p> : null}
    {!assets.error && !assets.isValidating && assets.data && <div className="grid min-w-0 gap-3 sm:grid-cols-2">{refs.map((ref, index) => {
      const asset = assets.data!.find(a => a.generation_output_id === ref.id)!;
      const done = () => { if (previewEpoch === currentEpoch.current) setLoaded(values => values.includes(ref.id) ? values : [...values, ref.id]); };
      const error = () => { if (previewEpoch === currentEpoch.current) setFailed(true); };
      return <figure className="min-w-0 space-y-2" key={`${ref.role}:${ref.id}:${index}:${previewEpoch}`}>
        {asset.mime_type.startsWith("image/") ?
          // eslint-disable-next-line @next/next/no-img-element -- immutable private bytes require the authenticated proxy.
          <img className="max-h-80 w-full rounded-md object-contain" src={assetPreviewUrl(asset)} alt={ref.role === "thumbnail" ? "公開するthumbnail" : "公開する添付画像"} onLoad={done} onError={error} />
          : <video className="max-h-80 w-full rounded-md" src={assetPreviewUrl(asset)} controls playsInline preload="metadata" onLoadedMetadata={done} onError={error} />}
        <figcaption className="break-all text-xs text-muted-foreground">{ref.role} · {asset.mime_type} · {(asset.size_bytes / 1024 / 1024).toFixed(2)} MiB · SHA {asset.sha256}</figcaption>
      </figure>;
    })}</div>}
  </section>;
}
