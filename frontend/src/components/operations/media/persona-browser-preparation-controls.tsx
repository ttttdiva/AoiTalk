"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { AppSelect } from "@/components/ui/app-select";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { mediaContentApi, type ContentVariantRevision } from "@/lib/media-operations-content-api";
import { mediaOperationsSetupApi, type PlatformAccount } from "@/lib/media-operations-setup-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { personaPublicationApi, publicationActionMatches, publicationPackageMatches, type PublicationAction, type PublicationPackage } from "@/lib/media-persona-publication-api";
import { browserPreparationApi, browserPreparationDeviceReady, browserPreparationBlocker, browserPreparationContentHash, browserPreparationMatches, browserPreparationReadinessMessage, type BrowserPreparation, type BrowserPreparationTarget } from "@/lib/media-browser-preparation-api";
import { readSubmissionMarker, writeSubmissionMarker, submissionStorageKey, withPublicationLock, type PublicationSubmissionMarker } from "@/lib/media-publication-submission";
import { mediaAssetsApi, assetInScope, assetPin, publicationAssetRefs } from "@/lib/media-assets-api";
import { SavedVariantPreview } from "./content-variant-panel";
import { PersonaAssetPreview } from "./persona-asset-preview";

type Marker = PublicationSubmissionMarker & { deviceId?: string; tabId?: number; preparationId?: string };
const options = { shouldRetryOnError: false, revalidateOnFocus: false };
const fail = (error: unknown) => {
  const message = error instanceof Error ? error.message : "Bridge準備の保存状態を照合できません。";
  const code = ["browser_preparation_new_document_required", "browser_preparation_legacy_revision_unverified"].find(code => message.includes(code));
  return code ? browserPreparationReadinessMessage([code]) : message;
};
const stateLabel = { preparing: "下書きの準備中", awaiting_final_confirmation: "本人の最終操作待ち", uncertain: "準備結果が不明・再入力を停止", blocked: "準備停止" };

/** 本人が承認したmanual Actionから準備記録だけを保存する。 */
export function PersonaBrowserPreparationControls({ scope, revision, account }: { scope: PersonaWorkspaceScope; revision: ContentVariantRevision; account: PlatformAccount }) {
  const storageKey = `${submissionStorageKey(scope, account.id, revision.content_hash)}:bridge-prepare-only`;
  const [action, setAction] = useState<PublicationAction | null>(null);
  const [preview, setPreview] = useState<PublicationPackage | null>(null);
  const [preparation, setPreparation] = useState<BrowserPreparation | null>(null);
  const [marker, setMarker] = useState<Marker | null>(null);
  const [deviceId, setDeviceId] = useState("");
  const [tabId, setTabId] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [mediaVerified, setMediaVerified] = useState(() => publicationAssetRefs(revision.payload).length === 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(false); const pending = useRef(false);
  const tab = Number(tabId);
  const target: BrowserPreparationTarget | undefined = deviceId && revision.platform_account_revision_id && Number.isSafeInteger(tab) && tab > 0 ? {
    owner_user_id: scope.ownerUserId, project_id: scope.projectId, persona_id: scope.personaId,
    platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id,
    platform: revision.platform, device_id: deviceId, tab_id: tab,
  } : undefined;
  const queryReadiness = async (selected: BrowserPreparationTarget) => {
    const current = await mediaOperationsSetupApi.getPlatformAccount(account.id);
    const reason = browserPreparationBlocker(scope, revision, current);
    if (reason || current.connection_id !== account.connection_id || current.account_ref !== account.account_ref) throw new Error(reason || "接続先のaccountが保存previewから変わりました。版を選び直してください。");
    return browserPreparationApi.availability(revision.platform, selected);
  };
  const binding = useSWR(target ? ["persona-browser-binding", scope.ownerUserId, scope.projectId, scope.personaId, account.id, revision.platform_account_revision_id, revision.platform, deviceId, tab] : null,
    () => queryReadiness(target!), options);
  const devices = useSWR(["persona-browser-devices", scope.ownerUserId], browserPreparationApi.devices, options);
  const history = useSWR(["persona-browser-actions", scope.ownerUserId, scope.personaId, scope.projectId, account.id, revision.id], () => personaPublicationApi.list(scope.projectId, revision.platform), options);
  const records = (history.data ?? []).filter(r => r.owner_user_id === scope.ownerUserId && r.project_id === scope.projectId && r.persona_revision_id === revision.persona_revision_id && r.content_variant_revision_id === revision.id && r.platform_account_id === account.id && r.platform_account_revision_id === revision.platform_account_revision_id && r.execution_mode === "manual");
  const selectedDevice = !devices.error ? devices.data?.find(d => d.id === deviceId) : undefined;
  const deviceReady = Boolean(browserPreparationDeviceReady(selectedDevice) && (!publicationAssetRefs(revision.payload).length || selectedDevice?.capabilities.includes("media_asset")));
  const blocker = binding.error ? fail(binding.error) : devices.error ? fail(devices.error) : history.error ? "保存済みActionを取得できません。準備操作を停止しています。"
    : !target ? browserPreparationReadinessMessage(["browser_explicit_owner_device_tab_required"])
    : !deviceReady ? "選択した本人のPC・専用ブラウザの接続状態を確認してください。"
    : !binding.data || binding.isValidating ? "選択したaccount・PC・タブのBridge準備状態を確認中…" : !binding.data.source_available ? "この媒体のBridge準備機能を利用できません。"
    : !binding.data.available ? browserPreparationReadinessMessage(binding.data.blockers) : "";
  const refs = publicationAssetRefs(revision.payload);
  const packageUsable = Boolean(preview && (preview.manifest.status === "verified" || (preview.manifest.status === "partial" && refs.length > 0 && preview.manifest.reasons.length > 0 && preview.manifest.assets.every(a => a.reference_type === "generation_output" && refs.some(ref => ref.id === a.reference_id)) && preview.manifest.reasons.every(reason => ["generation_output_artifact_binding_unavailable", "package_total_size_exceeded"].includes(String(reason.code))))));
  const read = (): Marker | null => {
    const value = readSubmissionMarker(storageKey) as Marker | null;
    if (value && (value.revisionId !== revision.id || value.contentHash !== revision.content_hash || value.accountId !== account.id || (value.phase === "execute" && (typeof value.deviceId !== "string" || !Number.isInteger(value.tabId) || Number(value.tabId) <= 0)))) throw new Error("ブラウザの準備記録と人格・内容・対象PCが一致しません。");
    return value;
  };
  useEffect(() => {
    active.current = true;
    const update = () => { try { const value = read(); setMarker(value); if (value?.deviceId) setDeviceId(value.deviceId); if (value?.tabId) setTabId(String(value.tabId)); } catch (e) { setError(fail(e)); } };
    const timer = window.setTimeout(update, 0); const onStorage = (event: StorageEvent) => { if (event.key === storageKey) update(); };
    window.addEventListener("storage", onStorage);
    return () => { active.current = false; window.clearTimeout(timer); window.removeEventListener("storage", onStorage); };
    // この子componentはowner・人格・内容・accountの固定版をkeyにする。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);
  const persist = (value: Marker) => { writeSubmissionMarker(storageKey, value); if (active.current) setMarker(value); };
  const validate = (value: PublicationAction, expectedId?: string, key?: string) => {
    const snapshot = value?.payload?.connection;
    const connection = snapshot && typeof snapshot === "object" && !Array.isArray(snapshot) ? snapshot as Record<string, unknown> : null;
    if (!publicationActionMatches(value, scope, revision, account, "manual") || connection?.id !== account.connection_id || connection.provider_key !== revision.platform || connection.remote_account_ref !== account.account_ref || expectedId && value.id !== expectedId || key && value.execution_key !== key || value.attempts?.length || value.receipt) throw new Error("準備Actionの人格・account・接続snapshot・内容版・hash・経路が一致しません。");
    return value;
  };
  const validatePreparation = async (value: BrowserPreparation, current: PublicationAction, target?: Marker) => {
    const digest = await browserPreparationContentHash(revision);
    if (!browserPreparationMatches(value, current, scope, revision, account, digest) || target?.deviceId && value.device_id !== target.deviceId || target?.tabId && value.tab_id !== target.tabId || target?.preparationId && value.id !== target.preparationId) throw new Error("準備証跡のAction・account・内容SHA・PC・タブが一致しません。");
    return value;
  };
  const fresh = async () => {
    if (!target || !deviceReady) throw new Error(browserPreparationReadinessMessage(["browser_explicit_owner_device_tab_required"]));
    const current = await queryReadiness(target);
    await binding.mutate(current, { revalidate: false });
    if (!current.available || !current.source_available) throw new Error(browserPreparationReadinessMessage(current.blockers));
    const qa = await mediaContentApi.getReadiness(revision.content_variant_id);
    if (!qa.ready || qa.content_variant_id !== revision.content_variant_id || qa.revision_id !== revision.id || qa.revision_hash !== revision.content_hash) throw new Error("準備直前のQA・権利確認と保存版が一致しません。");
    for (const ref of refs) {
      const asset = await mediaAssetsApi.get(ref.id); const pin = assetPin(revision, ref.id);
      const allowed = revision.platform === "x" ? ["image/png", "image/jpeg", "image/webp"] : ["video/mp4", "video/webm"];
      if (!pin || !assetInScope(asset, scope, ref.id, pin) || !allowed.includes(asset.mime_type) || asset.size_bytes > (revision.platform === "x" ? 25 : 64) * 1024 * 1024) throw new Error("Bridge準備用の保存素材を照合できません。Xは単一画像25MiB、YouTubeはMP4/WebM 64MiBまでです。");
    }
    if (!active.current) throw new Error("選択内容が変わったため準備を停止しました。");
  };
  const run = async (work: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError("");
    try { await withPublicationLock(storageKey, async () => { if (active.current) await work(); }); }
    catch (e) { if (active.current) setError(fail(e)); }
    finally { pending.current = false; if (active.current) setBusy(false); }
  };
  const load = async (value: PublicationAction, stored?: Marker) => {
    const current = validate(value, stored?.publicationId ?? undefined, stored?.idempotencyKey);
    if (active.current) { setAction(current); setPreview(null); setPreparation(null); setReviewed(false); }
    const saved = await browserPreparationApi.get(current.id);
    if (!saved || saved.publication_confirmed !== false) throw new Error("Bridge準備の記録を公開成功として扱えません。");
    const preparation = saved.preparation ? await validatePreparation(saved.preparation, current, stored) : null;
    const next: Marker = { ...(stored ?? {}), idempotencyKey: current.execution_key!, revisionId: revision.id, contentHash: revision.content_hash, accountId: account.id, publicationId: current.id, state: preparation?.status === "uncertain" || stored?.phase === "execute" && !preparation ? "unknown" : "recorded", phase: preparation || stored?.phase === "execute" ? "execute" : "intent", ...(preparation ? { deviceId: preparation.device_id, tabId: preparation.tab_id, preparationId: preparation.id } : {}) };
    persist(next);
    if (active.current) { setAction(current); setPreparation(preparation); if (preparation) { setDeviceId(preparation.device_id); setTabId(String(preparation.tab_id)); } }
    // preview素材が取得不能でも、照合済みの過去の準備証跡は読戻せる。
    const result = await personaPublicationApi.package(current);
    if (!publicationPackageMatches(result, current, revision)) throw new Error("凍結previewの内容版・hashが一致しません。");
    if (active.current) setPreview(result);
  };
  const create = async () => {
    if (read() || records.length) throw new Error("この保存版の準備Actionがあります。保存記録を照合してください。");
    await fresh(); const rows = await personaPublicationApi.list(scope.projectId, revision.platform);
    if (rows.some(r => r.owner_user_id === scope.ownerUserId && r.project_id === scope.projectId && r.content_variant_revision_id === revision.id && r.platform_account_id === account.id)) { await history.mutate(); throw new Error("この保存版に別の公開・準備Actionがあります。保存記録を確認してください。"); }
    if (!active.current || read()) return;
    const next: Marker = { idempotencyKey: `persona-bridge-action-v1:${revision.id}:${revision.content_hash}:${account.id}`, revisionId: revision.id, contentHash: revision.content_hash, accountId: account.id, publicationId: null, state: "submitting", phase: "intent" };
    persist(next);
    try {
      const value = validate(await personaPublicationApi.create({ action_type: "media.publish_content", execution_mode: "manual", platform: revision.platform, content_item_id: revision.content_item_id, content_variant_id: revision.content_variant_id, content_variant_revision_id: revision.id, persona_revision_id: revision.persona_revision_id, platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id, connection_id: account.connection_id!, project_id: scope.projectId, payload: revision.payload, content_item_hash: revision.content_item_hash, content_variant_revision_hash: revision.content_hash, content_variant_revision_version: revision.version, persona_revision_hash: revision.persona_revision_hash, platform_account_revision_hash: revision.platform_account_revision_hash, artifact_hashes: [...new Set(revision.generation_output_refs.map(r => r.sha256).filter((v): v is string => typeof v === "string"))], idempotency_key: next.idempotencyKey, execution_key: next.idempotencyKey }), undefined, next.idempotencyKey);
      persist({ ...next, publicationId: value.id, state: "recorded" }); await load(value); await history.mutate();
    } catch (e) { const latest = read(); if (latest) persist({ ...latest, state: "unknown" }); throw e; }
  };
  const readSaved = async (requestedId?: string) => {
    const stored = read(); let target = requestedId || stored?.publicationId;
    if (!target && stored) { const rows = await personaPublicationApi.list(scope.projectId, revision.platform); const exact = rows.filter(r => r.owner_user_id === scope.ownerUserId && r.project_id === scope.projectId && r.content_variant_revision_id === revision.id && r.platform_account_id === account.id && r.execution_mode === "manual" && r.execution_key === stored.idempotencyKey); if (exact.length === 1) target = exact[0].id; }
    if (!target) throw new Error("保存記録を一意に照合できません。再入力せずActionsで確認してください。");
    await load(validate(await personaPublicationApi.get(target), target, stored?.idempotencyKey), stored ?? undefined);
  };
  const prepare = async () => {
    const stored = read(); const tab = Number(tabId);
    if (!stored || stored.publicationId !== action?.id || stored.phase === "execute" || stored.state === "unknown" || !action || !preview || !reviewed || !mediaVerified || !packageUsable || !deviceReady || !Number.isSafeInteger(tab) || tab <= 0) throw new Error("凍結preview・本人のPC・既存タブと準備内容を確認してください。");
    await fresh(); const currentDevices = await devices.mutate();
    const device = currentDevices?.find(d => d.id === deviceId);
    if (!browserPreparationDeviceReady(device) || refs.length && !device?.capabilities.includes("media_asset")) throw new Error("選択した本人のPC・専用ブラウザの接続状態を確認できません。");
    if (!active.current) return;
    const current = validate(await personaPublicationApi.get(action.id), action.id, stored.idempotencyKey);
    if (!active.current) return;
    if (current.version !== action.version || !["proposed", "pending_approval", "approved"].includes(current.status ?? "")) throw new Error("準備Actionの承認状態が変わりました。保存記録を照合してください。");
    persist({ ...stored, state: "submitting", phase: "approve" });
    try {
      const approved = current.status === "approved" ? current : validate(await personaPublicationApi.approve(current.id, { expected_version: current.version!, action_version: current.action_version, payload_hash: current.payload_hash, artifact_hashes: current.artifact_hashes ?? [] }), current.id, stored.idempotencyKey);
      if (approved.status !== "approved" || approved.action_version !== current.action_version || approved.payload_hash !== current.payload_hash) throw new Error("承認された準備内容とpreviewが一致しません。");
      if (!active.current) { persist({ ...stored, state: "unknown", phase: "approve" }); return; }
      const claimed: Marker = { ...stored, state: "submitting", phase: "execute", deviceId, tabId: tab }; persist(claimed);
      const response = await browserPreparationApi.prepare(approved.id, { expected_version: approved.version!, action_version: approved.action_version!, payload_hash: approved.payload_hash!, device_id: deviceId, tab_id: tab, idempotency_key: `persona-bridge-prepare-v1:${approved.id}:${approved.action_version}:${approved.payload_hash}` });
      const savedAction = validate(await personaPublicationApi.get(approved.id), approved.id, stored.idempotencyKey);
      const result = await validatePreparation(response, savedAction, claimed);
      persist({ ...claimed, preparationId: result.id, state: result.status === "uncertain" ? "unknown" : "recorded" });
      if (active.current) await readSaved();
    } catch (e) { const latest = read(); if (latest) persist({ ...latest, state: "unknown" }); throw e; }
  };
  const inspect = async () => {
    if (!action || !preparation) throw new Error("保存された同じ準備証跡を先に取得してください。");
    const current = validate(await personaPublicationApi.get(action.id), action.id, read()?.idempotencyKey);
    await validatePreparation(await browserPreparationApi.inspect(current.id, { preparation_id: preparation.id, expected_version: preparation.version }), current, read() ?? undefined);
    if (active.current) await readSaved();
  };

  return <section aria-label="Bridgeで準備のみ" className="min-w-0 space-y-3 rounded-xl border p-4">
    <h3 className="inline-flex items-center gap-1 text-sm font-semibold">Bridgeで下書きを準備<HelpPopover label="Bridgeで下書きを準備">BridgeでこのProject・人格・accountの専用ブラウザを開き、本人が制作画面を開いてください。そのタブを選択した状態で既存タブIDを指定してください。照会はタブを切り替えたり、新しいタブを開いたりしません。</HelpPopover></h3>
    <p className="text-sm">{revision.platform === "youtube" ? "private動画の準備を照合し、最後のSaveは本人がYouTubeで操作します。" : "投稿下書きを照合し、最後のPostは本人がXで操作します。"}準備証跡は投稿・公開の成功receiptではありません。</p>
    <div className="grid min-w-0 gap-3 sm:grid-cols-2"><label className="min-w-0 space-y-1 text-sm"><span>本人の接続済みPC</span><AppSelect className="w-full min-w-0 [&_[data-slot=select-value]]:min-w-0" aria-label="本人の接続済みPC" value={deviceId} triggerContent={<span className="min-w-0 truncate">{selectedDevice?.name || "PCを明示選択"}</span>} disabled={busy || marker?.phase === "execute"} onValueChange={value => { setDeviceId(value); setReviewed(false); setError(""); }}><option value="">PCを明示選択</option>{!devices.error && devices.data?.map(d => <option key={d.id} value={d.id}><span className="min-w-0 whitespace-normal break-words">{d.name} · {browserPreparationDeviceReady(d) ? "ブラウザ接続済み" : "接続・ブラウザを確認"}</span></option>)}</AppSelect></label><label className="min-w-0 space-y-1 text-sm"><span>本人が開いた既存タブID</span><Input className="min-w-0" aria-label="本人が開いた既存タブID" type="number" min={1} step={1} value={tabId} disabled={busy || marker?.phase === "execute"} onChange={event => { setTabId(event.target.value); setReviewed(false); setError(""); }} /></label></div>
    {revision.platform === "youtube" && <p className="text-xs text-muted-foreground">本人確認のため、同じ専用ブラウザウィンドウで対象チャンネルの公開画面を開き、StudioのAccountメニューを表示して準備可否を確認してください。照合は3分で失効します。既存draftには新しい素材を入力できません。</p>}
    {selectedDevice && <p className="break-all text-xs text-muted-foreground">選択した本人のPC: {selectedDevice.name} · {selectedDevice.id}</p>}
    {(error || blocker) && <p role="alert" className="break-words text-sm text-destructive">{error || blocker}</p>}
    <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy || Boolean(blocker) || Boolean(marker) || records.length > 0 || history.isLoading} onClick={() => void run(create)}>この保存版の準備Actionを作成</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void run(async () => { await Promise.all([binding.mutate(), devices.mutate(), history.mutate()]); })}>Bridgeの利用状態を更新</Button><Link className="self-center text-xs underline" href="/settings#pc-bridge">PC接続を確認</Link></div>
    {marker && <div className="rounded-lg border p-3 text-xs"><p>{marker.phase === "execute" || marker.state === "unknown" ? "準備操作は送信済み、または結果が不明です。保存証跡を読み取りで照合してください。" : "保存された準備Actionを確認してください。"}</p><Button className="mt-2" size="sm" variant="outline" disabled={busy} onClick={() => void run(() => readSaved())}>準備記録を読み取って照合</Button></div>}
    {!marker && records.map(r => <Button key={r.id} size="sm" variant="outline" className="max-w-full whitespace-normal break-all" disabled={busy} onClick={() => void run(() => readSaved(r.id))}>準備Action {r.id} · {r.status}</Button>)}
    {action && preview && <div aria-label="Bridgeの凍結preview" className="min-w-0 space-y-3 rounded-lg border p-3">
      <p className="break-all text-xs">Action {action.id} · 版 {action.action_version} · 内容hash {action.payload_hash}</p><SavedVariantPreview revision={{ ...revision, payload: preview.manifest.payload as ContentVariantRevision["payload"] }} />
      <PersonaAssetPreview key={`${action.id}:${action.payload_hash}`} scope={scope} revision={revision} onVerified={setMediaVerified} />
      <label className="flex items-start gap-2 text-sm"><Checkbox checked={reviewed} disabled={busy || marker?.phase === "execute"} onCheckedChange={setReviewed} /><span className="min-w-0 break-words">{scope.displayName} / {account.account_ref} / 内容・{revision.platform === "youtube" ? "private設定・" : ""}対象PCと既存タブを確認しました</span></label>
      <Button disabled={busy || Boolean(blocker) || !reviewed || !mediaVerified || !packageUsable || !deviceReady || !Number.isSafeInteger(Number(tabId)) || Number(tabId) <= 0 || marker?.phase === "execute" || marker?.state === "unknown" || !["proposed", "pending_approval", "approved"].includes(action.status ?? "")} onClick={() => void run(prepare)}>{busy ? "準備の保存状態を確認中…" : "本人が承認して下書きだけ準備"}</Button>
    </div>}
    {preparation && <div aria-label="Bridge準備の保存証跡" className="min-w-0 space-y-2 rounded-lg border p-3 text-sm"><p className="font-medium">{stateLabel[preparation.status]}</p><p>{revision.platform === "youtube" ? "公開設定: private · 最終Saveは本人の操作待ち" : "最終Postは本人の操作待ち"} · 公開結果は未確認</p>
      {(!preparation.current_action_matches || !preparation.current_approval_valid) && <p className="text-destructive">現在のAction・承認は有効ではありません。過去の準備済み状態は公開の許可になりません。</p>}
      <dl className="space-y-1 text-xs"><div><dt>準備記録</dt><dd className="break-all">{preparation.id} · 版 {preparation.version}</dd></div><div><dt>account / PC / タブ</dt><dd className="break-all">{account.account_ref} / {preparation.device_id} / {preparation.tab_id}</dd></div><div><dt>{revision.platform === "youtube" ? "metadata SHA" : "本文 SHA"}</dt><dd className="break-all">{preparation.content_sha256}</dd></div><div><dt>保存段階</dt><dd className="break-all">{preparation.phase}{preparation.error_code ? ` · ${preparation.error_code}` : ""}</dd></div></dl>
      {preparation.error_code && ["browser_preparation_new_document_required", "browser_preparation_legacy_revision_unverified"].includes(preparation.error_code) && <p className="text-xs">{browserPreparationReadinessMessage([preparation.error_code])}</p>}
      <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(inspect)}>同じ下書きを読み取り専用で再照合</Button>
    </div>}
  </section>;
}
