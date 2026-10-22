"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { mediaResearchApi } from "@/lib/media-operations-research-api";
import { mediaAssetsApi, assetInScope, assetPreviewUrl, type MediaAssetReceipt, type MediaAssetSource } from "@/lib/media-assets-api";
import { withPublicationLock } from "@/lib/media-publication-submission";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { hashMediaFile } from "@/lib/media-file-hash";

const options = { shouldRetryOnError: false, revalidateOnFocus: false };
type Marker = { key: string; fingerprint: string; operation: "upload" | "register"; outputId: string | null; sha256: string | null; state: "submitting" | "unknown" | "recorded" };
const fail = (error: unknown) => error instanceof Error ? error.message : "制作物の登録記録を確認できません。保存状態を照合してください。";
const sourceKey = (source: MediaAssetSource) => `${source.source_kind}:${source.source_id}`;
const sourceNames = { generated_media: "生成済み画像・動画", library_file: "保存済みファイル", generation_output: "既存制作output" };

export function PersonaAssetPanel({ scope, contentItemId, onBack, onSelected }: { scope: PersonaWorkspaceScope; contentItemId: string; onBack: () => void; onSelected: (asset: MediaAssetReceipt) => void }) {
  const brief = useSWR(contentItemId ? ["persona-asset-brief", scope.ownerUserId, scope.projectId, scope.personaId, scope.personaRevisionId, contentItemId] : null, () => mediaResearchApi.getContentItem(contentItemId), options);
  const item = brief.data;
  const valid = item && item.id === contentItemId && item.owner_user_id === scope.ownerUserId && item.project_id === scope.projectId && item.persona_revision_id === scope.personaRevisionId && /^[a-f0-9]{64}$/.test(item.content_hash ?? "");
  return <section aria-label="既存制作物の登録" className="min-w-0 space-y-3">
    <div><h2 className="inline-flex items-center gap-1 text-lg font-semibold">既存の画像・動画を使う<HelpPopover label="既存の画像・動画を使う">この人格の制作briefに、本人のファイル・保存済み画像や動画・既存outputを登録します。登録後にQA・権利・媒体別内容を確認します。</HelpPopover></h2></div>
    {!contentItemId ? <div className="space-y-2 rounded-xl border p-4"><p className="text-sm">先に制作briefを選択してください。</p><Button variant="outline" onClick={onBack}>制作briefへ</Button></div> : brief.error ? <p role="alert" className="text-sm text-destructive">この人格の制作briefを取得できません。制作briefから保存状態を確認してください。</p> : brief.isLoading ? <p role="status">制作briefを確認中…</p> : !valid || !item ? <p role="alert" className="text-sm text-destructive">制作briefの人格・Project・保存hashを確認できません。制作briefから選択してください。</p> : <AssetCommands key={`${scope.ownerUserId}:${scope.personaId}:${item.id}:${item.content_hash}`} scope={scope} briefId={item.id} briefHash={item.content_hash!} title={item.title} onSelected={onSelected} />}
  </section>;
}

function AssetCommands({ scope, briefId, briefHash, title, onSelected }: { scope: PersonaWorkspaceScope; briefId: string; briefHash: string; title: string; onSelected: (asset: MediaAssetReceipt) => void }) {
  const storageKey = `aoitalk-persona-asset-v1:${scope.ownerUserId}:${scope.projectId}:${scope.personaId}:${briefId}:${briefHash}`;
  const [file, setFile] = useState<File | null>(null);
  const [selectedSource, setSelectedSource] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [marker, setMarker] = useState<Marker | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [hashProgress, setHashProgress] = useState<number | null>(null);
  const mounted = useRef(false); const pending = useRef(false);
  const capabilities = useSWR(["persona-asset-capabilities", scope.ownerUserId], mediaAssetsApi.capabilities, options);
  const assets = useSWR(["persona-assets", scope.ownerUserId, scope.projectId, scope.personaId], async () => { const values = await mediaAssetsApi.list(scope.personaId); if (!Array.isArray(values)) throw new Error("登録済み素材を照合できません。"); return values.filter(a => assetInScope(a, scope)); }, options);
  const sources = useSWR(["persona-asset-sources", scope.ownerUserId, scope.projectId, scope.personaId], () => mediaAssetsApi.sources(scope.personaId), options);
  const savedAsset = assets.data?.find(a => a.generation_output_id === selectedId);
  const source = sources.data?.sources.find(s => sourceKey(s) === selectedSource);
  const read = () => {
    const raw = localStorage.getItem(storageKey); if (!raw) return null;
    const value = JSON.parse(raw) as Partial<Marker>;
    if (!value || typeof value.key !== "string" || typeof value.fingerprint !== "string" || !["upload", "register"].includes(String(value.operation)) || !["submitting", "unknown", "recorded"].includes(String(value.state)) || !(value.outputId === null || typeof value.outputId === "string") || !(value.sha256 === null || typeof value.sha256 === "string")) throw new Error("ブラウザの素材登録記録を照合できません。再登録せず保存状態を確認してください。");
    return value as Marker;
  };
  const persist = (value: Marker) => { const json = JSON.stringify(value); localStorage.setItem(storageKey, json); if (localStorage.getItem(storageKey) !== json) throw new Error("素材登録の操作記録を保存できません。ブラウザの保存設定を確認してください。"); if (mounted.current) setMarker(value); };
  useEffect(() => {
    mounted.current = true;
    const sync = () => { try { setMarker(read()); } catch (e) { setError(fail(e)); } };
    const timer = window.setTimeout(sync, 0); const update = (event: StorageEvent) => { if (event.key === storageKey) sync(); };
    window.addEventListener("storage", update);
    return () => { mounted.current = false; window.clearTimeout(timer); window.removeEventListener("storage", update); };
    // This component is keyed by the entire immutable scope/brief/storage key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);
  const validate = (value: MediaAssetReceipt, expectedId?: string, sha?: string) => { if (!assetInScope(value, scope, expectedId, sha)) throw new Error("登録素材の人格・Project・保存hashが一致しません。"); return value; };
  const run = async (operation: () => Promise<void>) => { if (pending.current) return; pending.current = true; setBusy(true); setError(""); try { await withPublicationLock(storageKey, async () => { if (mounted.current) await operation(); }); } catch (e) { if (mounted.current) setError(fail(e)); } finally { pending.current = false; if (mounted.current) { setBusy(false); setHashProgress(null); } } };
  const verifyBrief = async () => { const current = await mediaResearchApi.getContentItem(briefId); if (current.id !== briefId || current.owner_user_id !== scope.ownerUserId || current.project_id !== scope.projectId || current.persona_revision_id !== scope.personaRevisionId || current.content_hash !== briefHash) throw new Error("制作briefの保存版が変わりました。制作briefから選択し直してください。"); };
  const inspect = async () => {
    const current = read(); if (!current?.outputId || !current.sha256) throw new Error("登録結果のIDが不明です。同じ制作物を選択して同じ登録操作を明示再開してください。");
    const saved = validate(await mediaAssetsApi.get(current.outputId), current.outputId, current.sha256);
    persist({ ...current, state: "recorded" }); if (!mounted.current) return; await assets.mutate(); if (mounted.current) setSelectedId(saved.generation_output_id);
  };
  const submit = async (operation: Marker["operation"]) => {
    const selectedFile = file; const selected = source;
    if (operation === "upload" && !selectedFile || operation === "register" && !selected) return;
    const cap = capabilities.data;
    if (!cap || cap.upload_transport !== "binary_stream" || !Array.isArray(cap.supported_mime_types)) throw new Error("素材登録のMIME・サイズ上限を確認できません。");
    const mime = operation === "upload" ? selectedFile!.type : selected!.mime_type;
    const size = operation === "upload" ? selectedFile!.size : selected!.size_bytes;
    const maximum = mime.startsWith("image/") ? cap.image_max_bytes : cap.video_max_bytes;
    if (!cap.supported_mime_types.includes(mime) || size !== null && (size <= 0 || size > maximum)) throw new Error("画像・動画の種類またはサイズが登録上限に合いません。対応するファイルを選択してください。");
    const byteHash = operation === "upload" ? await hashMediaFile(selectedFile!, () => mounted.current, percent => { if (mounted.current) setHashProgress(percent); }) : null;
    const fingerprint = operation === "register" ? `${sourceKey(selected!)}:${selected!.sha256 || "server-hash"}` : `upload:${byteHash}:${mime}:${size}`;
    if (!mounted.current) return;
    const previous = read();
    if (previous && previous.state !== "recorded" && (previous.fingerprint !== fingerprint || previous.operation !== operation)) throw new Error("前回の登録結果が不明です。前回と同じ制作物を選択し、同じ操作を明示再開してください。");
    if (previous?.state !== "recorded" && previous?.outputId) { await inspect(); return; }
    await verifyBrief(); if (!mounted.current) return;
    const command: Marker = previous && previous.state !== "recorded" ? { ...previous, state: "submitting" } : { key: crypto.randomUUID(), fingerprint, operation, outputId: null, sha256: byteHash, state: "submitting" };
    persist(command);
    try {
      const body = { persona_id: scope.personaId, content_item_id: briefId, expected_content_item_hash: briefHash };
      const saved = validate(operation === "upload" ? await mediaAssetsApi.upload(selectedFile!, body, command.key, byteHash!) : await mediaAssetsApi.register({ ...body, source_kind: selected!.source_kind, source_id: selected!.source_id, ...(selected!.sha256 ? { expected_sha256: selected!.sha256 } : {}) }, command.key));
      if (saved.content_item_id !== briefId || saved.mime_type !== mime || (operation === "upload" && (saved.size_bytes !== selectedFile!.size || saved.sha256 !== byteHash)) || (operation === "register" && (saved.source_kind !== selected!.source_kind || saved.source_id !== selected!.source_id || selected!.sha256 && saved.sha256 !== selected!.sha256))) throw new Error("登録素材と選択した制作物・briefが一致しません。");
      persist({ ...command, outputId: saved.generation_output_id, sha256: saved.sha256 });
      const actual = validate(await mediaAssetsApi.get(saved.generation_output_id), saved.generation_output_id, saved.sha256);
      persist({ ...command, outputId: actual.generation_output_id, sha256: actual.sha256, state: "recorded" });
      if (!mounted.current) return; await assets.mutate(); if (mounted.current) setSelectedId(actual.generation_output_id);
    } catch (e) { const current = read(); if (current?.key === command.key) persist({ ...current, state: "unknown" }); throw e; }
  };
  const message = error || (capabilities.error ? "対応する画像・動画の種類とサイズ上限を取得できません。登録情報を再取得してください。" : assets.error ? "この人格の登録済み制作物を取得できません。登録情報を再取得してください。" : sources.error ? "この人格の既存制作物を取得できません。登録情報を再取得してください。" : "");
  return <div className="min-w-0 space-y-4"><p className="break-words text-sm font-medium">制作brief: {title}</p>
    {message && <p role="alert" className="text-sm text-destructive">{message}</p>}
    {!sources.error && sources.data?.partial && <p role="status" className="rounded-lg border p-3 text-xs text-muted-foreground">一部の保存先を取得できませんでした。現在利用できる制作物を表示しています。</p>}
    {hashProgress !== null && <p role="status" className="text-xs">制作物の実bytes hashを確認中… {hashProgress}%</p>}
    <div className="flex flex-wrap gap-2"><Button variant="outline" size="sm" disabled={busy} onClick={() => { void capabilities.mutate(); void assets.mutate(); void sources.mutate(); }}>登録情報を再取得</Button>{marker && <Button variant="outline" size="sm" disabled={busy || !marker.outputId} onClick={() => void run(inspect)}>前回の登録記録を照合</Button>}</div>
    {marker && marker.state !== "recorded" && <p className="rounded-lg border p-3 text-xs">前回の登録結果を確認できません。自動再登録せず、保存記録を照合するか、前回と同じ制作物を選択して同じ操作を明示再開してください。</p>}
    <div className="grid min-w-0 gap-4 md:grid-cols-2"><section className="min-w-0 space-y-3 rounded-xl border p-4"><h3 className="inline-flex items-center gap-1 text-sm font-semibold">本人のファイル<HelpPopover label="本人のファイル">画像 PNG/JPEG/WebP/GIF は25 MiB、動画 MP4/WebM/QuickTime は512 MiBまで。</HelpPopover></h3><Input type="file" aria-label="登録する画像・動画" accept={capabilities.data?.supported_mime_types.join(",")} disabled={busy} onChange={e => setFile(e.target.files?.[0] ?? null)} />
      <Button disabled={busy || !file || !capabilities.data || Boolean(capabilities.error)} onClick={() => void run(() => submit("upload"))}>{marker?.state !== "recorded" && marker?.operation === "upload" ? "同じファイルの登録を明示再開" : "このファイルを登録"}</Button></section>
      <section className="min-w-0 space-y-3 rounded-xl border p-4"><h3 className="text-sm font-semibold">保存済み制作物・既存output</h3><Select value={selectedSource} onValueChange={value => setSelectedSource(value ?? "")} disabled={busy || Boolean(sources.error)}><SelectTrigger className="w-full min-w-0" aria-label="登録する既存制作物"><SelectValue placeholder="既存制作物を選択" /></SelectTrigger><SelectContent>{(sources.data?.sources ?? []).map(s => <SelectItem key={sourceKey(s)} value={sourceKey(s)}><span className="min-w-0 whitespace-normal break-all">{sourceNames[s.source_kind]} · {s.mime_type} · {s.source_id}</span></SelectItem>)}</SelectContent></Select>
        <Button disabled={busy || !source || !capabilities.data || Boolean(capabilities.error) || Boolean(sources.error)} onClick={() => void run(() => submit("register"))}>{marker?.state !== "recorded" && marker?.operation === "register" ? "同じ制作物の登録を明示再開" : "選択した制作物を登録"}</Button></section></div>
    <section aria-label="この人格の登録済み制作物" className="min-w-0 space-y-3 rounded-xl border p-4"><h3 className="text-sm font-semibold">登録済みの制作物</h3>{assets.isLoading ? <p role="status">登録記録を確認中…</p> : !assets.error && !assets.data?.length ? <p className="text-sm text-muted-foreground">この人格の登録済み画像・動画はありません。</p> : null}
      <div className="flex flex-wrap gap-2">{assets.data?.map(a => <Button key={a.generation_output_id} variant={selectedId === a.generation_output_id ? "default" : "outline"} size="sm" className="h-auto max-w-full whitespace-normal break-all" onClick={() => setSelectedId(a.generation_output_id)}>{a.mime_type} · {(a.size_bytes / 1024 / 1024).toFixed(2)} MiB · {a.generation_output_id}</Button>)}</div>
      {savedAsset && <RegisteredAsset key={`${savedAsset.generation_output_id}:${savedAsset.sha256}`} asset={savedAsset} busy={busy} onSelected={onSelected} />}
    </section>
  </div>;
}

function RegisteredAsset({ asset, busy, onSelected }: { asset: MediaAssetReceipt; busy: boolean; onSelected: (asset: MediaAssetReceipt) => void }) {
  const [loaded, setLoaded] = useState(false); const [failed, setFailed] = useState(false);
  return <div className="min-w-0 space-y-3"><div className="max-w-xl">{asset.mime_type.startsWith("image/") ?
    // eslint-disable-next-line @next/next/no-img-element -- immutable private bytes require the authenticated proxy, without image optimization.
    <img className="max-h-96 w-full rounded-lg object-contain" src={assetPreviewUrl(asset)} alt="登録した制作画像" onLoad={() => setLoaded(true)} onError={() => setFailed(true)} />
    : <video className="max-h-96 w-full rounded-lg" src={assetPreviewUrl(asset)} controls playsInline preload="metadata" onLoadedMetadata={() => setLoaded(true)} onError={() => setFailed(true)} />}</div>
    {failed && <p role="alert" className="text-xs text-destructive">制作物の実体previewを表示できません。素材の保存状態と閲覧権限を確認してください。</p>}
    {!loaded && !failed && <p role="status" className="text-xs">制作物の実体を確認中…</p>}
    <p className="break-all text-xs text-muted-foreground">保存hash: {asset.sha256}</p><Button disabled={failed || !loaded || busy} onClick={() => onSelected(asset)}>この制作物でQA・媒体別内容へ</Button>
  </div>;
}
