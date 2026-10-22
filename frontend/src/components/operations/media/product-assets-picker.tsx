"use client";
import { useEffect, useId, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { assetInScope, mediaAssetsApi } from "@/lib/media-assets-api";
import { operationsApi } from "@/lib/operations-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import type { ProductDraft } from "@/lib/media-product-draft";
import { ProductFilePicker } from "./product-file-picker";

export function ProductAssetsPicker({ scope, contentItemId, value, onChange }: {
  scope: PersonaWorkspaceScope; contentItemId: string; value: ProductDraft; onChange: (value: ProductDraft) => void;
}) {
  const headingId = useId();
  const assets = useSWR(["product-assets", scope.ownerUserId, scope.projectId, scope.personaId], () => mediaAssetsApi.list(scope.personaId), { shouldRetryOnError: false, revalidateOnFocus: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(true);
  const current = useRef({ scope, contentItemId, value });
  useEffect(() => { current.current = { scope, contentItemId, value }; });
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const add = (key: "preview_assets" | "thumbnail_assets", id: string) => onChange({ ...value, [key]: [...new Set([...value[key].split(/\r?\n/u).filter(Boolean), id])].join("\n") });
  const upload = async (file: File) => {
    if (busy) return;
    if (file.size > 25 * 1024 * 1024) { setError("配布パッケージは25 MiB以下を指定してください。"); return; }
    setBusy(true); setError("");
    try {
      const artifact = await operationsApi.uploadArtifact({ file, project_id: scope.projectId }, crypto.randomUUID());
      if (artifact.owner_user_id !== scope.ownerUserId || artifact.project_id !== scope.projectId || !artifact.id || !/^[a-f0-9]{64}$/u.test(String(artifact.sha256))) throw new Error("保存された配布物の所属・hashを確認できません。");
      const latest = current.current;
      if (active.current && latest.scope.ownerUserId === scope.ownerUserId && latest.scope.projectId === scope.projectId && latest.scope.personaId === scope.personaId && latest.contentItemId === contentItemId) onChange({ ...latest.value, deliverable_package_ref: `artifact:${artifact.id}` });
    } catch (e) { if (active.current) setError(e instanceof Error ? e.message : "配布物を保存できません。"); }
    finally { if (active.current) setBusy(false); }
  };
  return <div className="space-y-2 rounded-md border p-3"><div className="flex items-center gap-1 text-xs font-medium"><span id={headingId}>登録済み素材・配布物</span><HelpPopover label="登録済み素材・配布物">素材の工程で登録した同じ人格・Projectの実体を選択します。Action準備時に商品用ArtifactVersionへ固定します。</HelpPopover></div>
    <fieldset aria-labelledby={headingId} className="space-y-2">
    <Button type="button" size="sm" variant="outline" onClick={() => void assets.mutate()}>登録素材を更新</Button>
    {assets.error && <p role="alert" className="text-xs">登録素材を取得できません。更新して再確認してください。</p>}
    {(assets.data ?? []).filter(a => assetInScope(a, scope) && (!a.content_item_id || a.content_item_id === contentItemId)).map(a => <div key={a.generation_output_id} className="flex flex-wrap items-center gap-2 text-xs"><span className="min-w-0 break-all">{a.mime_type} · {a.size_bytes} bytes · {a.sha256.slice(0, 12)}</span><Button type="button" size="sm" variant="outline" onClick={() => add("preview_assets", a.generation_output_id)}>Previewに追加</Button><Button type="button" size="sm" variant="outline" onClick={() => add("thumbnail_assets", a.generation_output_id)}>Thumbnailに追加</Button></div>)}
    <label className="block space-y-1 text-xs">配布物を内部保存（25 MiB以下）<Input type="file" disabled={busy} onChange={e => { const file = e.target.files?.[0]; if (file) void upload(file); e.target.value = ""; }} /></label>
    <ProductFilePicker scope={scope} onSelect={reference => onChange({ ...current.current.value, deliverable_package_ref: reference })} />
    {busy && <p role="status" className="text-xs">配布物を保存中…</p>}{error && <p role="alert" className="text-xs">{error}</p>}
    </fieldset>
  </div>;
}
