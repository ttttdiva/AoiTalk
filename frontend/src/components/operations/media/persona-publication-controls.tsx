"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ActionReceiptPanel } from "../action-receipt-panel";
import { mediaContentApi, type ContentVariantRevision } from "@/lib/media-operations-content-api";
import { mediaOperationsSetupApi, type PlatformAccount } from "@/lib/media-operations-setup-api";
import { accountInScope, type PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { personaPublicationApi, providerBlocker, providerOperationStates, publicationActionMatches, publicationPackageMatches, publicationReceiptMatches, type PublicationAction, type PublicationPackage } from "@/lib/media-persona-publication-api";
import { readSubmissionMarker, writeSubmissionMarker, submissionStorageKey, withPublicationLock, type PublicationSubmissionMarker } from "@/lib/media-publication-submission";
import { SavedVariantPreview } from "./content-variant-panel";
import { PersonaAssetPreview } from "./persona-asset-preview";
import { publicationAssetRefs } from "@/lib/media-assets-api";

const options = { shouldRetryOnError: false, revalidateOnFocus: false };
const fail = (error: unknown) => error instanceof Error ? error.message : "公開処理の状態を確認できません。保存記録を照合してください。";

/** This child is keyed by immutable persona/content/account pins by its caller. */
export function PersonaPublicationControls({ scope, revision, account }: { scope: PersonaWorkspaceScope; revision: ContentVariantRevision; account: PlatformAccount }) {
  const storageKey = submissionStorageKey(scope, account.id, revision.content_hash);
  const [action, setAction] = useState<PublicationAction | null>(null);
  const [preview, setPreview] = useState<PublicationPackage | null>(null);
  const [marker, setMarker] = useState<PublicationSubmissionMarker | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [mediaVerified, setMediaVerified] = useState(() => publicationAssetRefs(revision.payload).length === 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(false);
  const pending = useRef(false);
  const connection = useSWR(["persona-provider", scope.ownerUserId, scope.personaId, account.id, revision.platform_account_revision_id], async () => {
    const savedAccount = await mediaOperationsSetupApi.getPlatformAccount(account.id);
    if (!accountInScope(savedAccount, scope) || savedAccount.current_revision.id !== revision.platform_account_revision_id || savedAccount.current_revision.content_hash !== revision.platform_account_revision_hash || savedAccount.connection_id !== account.connection_id) throw new Error("接続先の版・所属が変わりました。QA・媒体別内容で現在の接続先を選び直してください。");
    const readiness = await personaPublicationApi.readiness(account.id);
    return { account: savedAccount, readiness };
  }, options);
  const history = useSWR(["persona-publications", scope.ownerUserId, scope.personaId, scope.projectId, account.id, revision.id], () => personaPublicationApi.list(scope.projectId, revision.platform), options);
  const records = (history.data ?? []).filter(r => r.owner_user_id === scope.ownerUserId && r.project_id === scope.projectId && r.persona_revision_id === revision.persona_revision_id && r.content_variant_revision_id === revision.id && r.platform_account_id === account.id && r.platform_account_revision_id === revision.platform_account_revision_id && r.execution_mode === "provider");
  const blocker = connection.error ? fail(connection.error) : connection.data ? providerBlocker(connection.data.readiness, scope, revision, connection.data.account) : "接続先の準備状態を確認中…";
  const assetRefs = publicationAssetRefs(revision.payload);
  const receiptMatches = Boolean(action && publicationReceiptMatches(action));
  const packageUsable = Boolean(preview && (preview.manifest.status === "verified" || (preview.manifest.status === "partial" && assetRefs.length > 0 && preview.manifest.reasons.length > 0 && preview.manifest.assets.every(a => a.reference_type === "generation_output" && assetRefs.some(ref => ref.id === a.reference_id)) && preview.manifest.reasons.every(reason => ["generation_output_artifact_binding_unavailable", "package_total_size_exceeded"].includes(String(reason.code))))));

  useEffect(() => {
    active.current = true;
    const read = () => { try { const value = readSubmissionMarker(storageKey); if (value && (value.revisionId !== revision.id || value.contentHash !== revision.content_hash || value.accountId !== account.id)) throw new Error("ブラウザの公開記録と選択内容が一致しません。保存記録を照合してください。"); setMarker(value); } catch (e) { setError(fail(e)); } };
    const timer = window.setTimeout(read, 0);
    const update = (event: StorageEvent) => { if (event.key === storageKey) read(); };
    window.addEventListener("storage", update);
    return () => { active.current = false; window.clearTimeout(timer); window.removeEventListener("storage", update); };
  }, [storageKey, revision.id, revision.content_hash, account.id]);

  const persist = (value: PublicationSubmissionMarker) => { writeSubmissionMarker(storageKey, value); if (active.current) setMarker(value); };
  const validate = (value: PublicationAction, expectedId?: string, expectedKey?: string) => { if (!publicationActionMatches(value, scope, revision, account) || (expectedId && value.id !== expectedId) || (expectedKey && value.execution_key !== expectedKey)) throw new Error("保存された公開記録のID・操作key・人格・接続先・内容版・hashが一致しません。"); return value; };
  const load = async (value: PublicationAction) => {
    const valid = validate(value);
    if (active.current) { setAction(valid); setPreview(null); setReviewed(false); }
    const result = await personaPublicationApi.package(valid);
    if (!publicationPackageMatches(result, valid, revision)) throw new Error("公開previewの版・内容hashが保存内容と一致しません。");
    if (active.current) { setAction(valid); setPreview(result); setReviewed(false); }
    return valid;
  };
  const fresh = async () => {
    const result = await connection.mutate();
    if (!active.current) throw new Error("選択内容が変わったため公開操作を停止しました。");
    if (!result) throw new Error("接続先の準備状態を取得できません。");
    const message = providerBlocker(result.readiness, scope, revision, result.account);
    if (message) throw new Error(message);
    const qa = await mediaContentApi.getReadiness(revision.content_variant_id);
    if (!qa.ready || qa.content_variant_id !== revision.content_variant_id || qa.revision_id !== revision.id || qa.revision_hash !== revision.content_hash) throw new Error("公開直前のQA・権利確認または保存版が変わりました。QAから確認してください。");
    if (!active.current) throw new Error("選択内容が変わったため公開操作を停止しました。");
  };
  const run = async (operation: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError("");
    try { await withPublicationLock(storageKey, async () => { if (active.current) await operation(); }); }
    catch (e) { if (active.current) setError(fail(e)); }
    finally { pending.current = false; if (active.current) setBusy(false); }
  };

  const prepare = async () => {
    const existing = readSubmissionMarker(storageKey);
    if (existing || records.length) throw new Error("この内容の公開記録があります。下の保存記録を照合してください。");
    await fresh();
    const before = await personaPublicationApi.list(scope.projectId, revision.platform);
    if (before.some(r => r.owner_user_id === scope.ownerUserId && r.content_variant_revision_id === revision.id && r.platform_account_id === account.id)) { await history.mutate(); throw new Error("この内容の公開記録があります。保存記録を選択して照合してください。"); }
    if (!active.current || readSubmissionMarker(storageKey)) return;
    // The same immutable content/account has one proposal key across tabs/devices.
    const intent: PublicationSubmissionMarker = { idempotencyKey: `persona-publish-v1:${revision.id}:${revision.content_hash}:${account.id}`, revisionId: revision.id, contentHash: revision.content_hash, accountId: account.id, publicationId: null, state: "submitting", phase: "intent" };
    persist(intent);
    try {
      const saved = validate(await personaPublicationApi.create({ action_type: "media.publish_content", execution_mode: "provider", platform: revision.platform,
        content_item_id: revision.content_item_id, content_variant_id: revision.content_variant_id, content_variant_revision_id: revision.id,
        persona_revision_id: revision.persona_revision_id, platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id,
        connection_id: account.connection_id!, project_id: scope.projectId, payload: revision.payload,
        content_item_hash: revision.content_item_hash, content_variant_revision_hash: revision.content_hash, content_variant_revision_version: revision.version,
        persona_revision_hash: revision.persona_revision_hash, platform_account_revision_hash: revision.platform_account_revision_hash,
        artifact_hashes: [...new Set(revision.generation_output_refs.map(r => r.sha256).filter((v): v is string => typeof v === "string"))],
        idempotency_key: intent.idempotencyKey, execution_key: intent.idempotencyKey }), undefined, intent.idempotencyKey);
      persist({ ...intent, publicationId: saved.id, state: "recorded", phase: saved.attempts?.length || saved.receipt ? "execute" : "intent" });
      if (active.current) { await load(saved); await history.mutate(); }
    } catch (e) { const latest = readSubmissionMarker(storageKey); if (latest?.idempotencyKey === intent.idempotencyKey) persist({ ...latest, state: "unknown" }); throw e; }
  };
  const inspect = async (actionId?: string) => {
    const stored = readSubmissionMarker(storageKey);
    let value: PublicationAction;
    if (actionId || stored?.publicationId) { const requestedId = actionId || stored!.publicationId!; value = validate(await personaPublicationApi.get(requestedId), requestedId, stored?.idempotencyKey); }
    else {
      if (!stored) throw new Error("照合する公開記録がありません。");
      const rows = await personaPublicationApi.list(scope.projectId, revision.platform);
      const exact = rows.filter(r => r.execution_key === stored.idempotencyKey && r.owner_user_id === scope.ownerUserId && r.project_id === scope.projectId && r.content_variant_revision_id === revision.id && r.platform_account_id === account.id);
      if (exact.length !== 1) throw new Error("保存記録を一意に照合できません。公開操作は再送せず、Operations の Actions で記録を確認してください。");
      value = validate(await personaPublicationApi.get(exact[0].id), exact[0].id, stored.idempotencyKey);
    }
    validate(value);
    if (stored && value.execution_key !== stored.idempotencyKey) throw new Error("ブラウザの公開操作keyと保存記録が一致しません。");
    persist({ idempotencyKey: value.execution_key!, revisionId: revision.id, contentHash: revision.content_hash, accountId: account.id, publicationId: value.id,
      state: "recorded", phase: value.attempts?.length || value.receipt ? "execute" : stored ? stored.phase ?? "execute" : "intent" });
    await load(value);
  };
  const publish = async () => {
    const stored = readSubmissionMarker(storageKey);
    if (!stored || stored.state !== "recorded" || stored.publicationId !== action?.id || stored.phase === "execute" || !reviewed || !preview || !packageUsable || !mediaVerified || !action || !publicationPackageMatches(preview, action, revision)) throw new Error("凍結preview・添付素材と保存記録を確認してください。実行済み・不明な操作は再送できません。");
    await fresh();
    const current = validate(await personaPublicationApi.get(action.id), action.id, stored.idempotencyKey);
    if (current.version !== action.version || current.action_version !== action.action_version || current.payload_hash !== action.payload_hash || current.attempts?.length || current.receipt) throw new Error("公開記録が変わりました。保存記録を照合してpreviewを確認してください。");
    if (!active.current) return;
    persist({ ...stored, state: "submitting", phase: "approve" });
    try {
      const approved = current.status === "approved" ? current : validate(await personaPublicationApi.approve(current.id, { expected_version: current.version!, action_version: current.action_version, payload_hash: current.payload_hash, artifact_hashes: current.artifact_hashes ?? [] }), current.id, stored.idempotencyKey);
      if (approved.status !== "approved" || approved.action_version !== current.action_version || approved.payload_hash !== current.payload_hash) throw new Error("承認された内容がpreviewと一致しません。");
      if (!active.current) { persist({ ...stored, phase: "approve", state: "unknown" }); return; }
      persist({ ...stored, state: "submitting", phase: "execute" });
      const attempt = await personaPublicationApi.attempt(approved.id, { expected_version: approved.version!, executor_type: "provider", execution_mode: "provider", execution_key: stored.idempotencyKey });
      if (attempt.action_id !== approved.id || attempt.execution_key !== stored.idempotencyKey) throw new Error("実行結果の保存先・操作keyを照合できません。");
      const saved = validate(await personaPublicationApi.get(approved.id), approved.id, stored.idempotencyKey);
      persist({ ...stored, state: "recorded", phase: "execute" });
      if (active.current) { setAction(saved); setReviewed(false); await history.mutate(); }
    } catch (e) { const latest = readSubmissionMarker(storageKey); if (latest) persist({ ...latest, state: "unknown" }); throw e; }
  };
  const recover = async (resume: boolean) => {
    const stored = readSubmissionMarker(storageKey);
    if (!action || stored?.publicationId !== action.id || stored.phase !== "execute") throw new Error("保存された実行記録を先に照合してください。");
    const current = validate(await personaPublicationApi.get(action.id), action.id, stored.idempotencyKey);
    if (resume && (revision.platform !== "youtube" || !current.attempts?.some(a => a.provider_recovery_available === true))) throw new Error("同じ動画の再開可能な保存checkpointがありません。");
    const saved = validate(await personaPublicationApi.recover(current.id, { expected_version: current.version!, resume }), current.id, stored.idempotencyKey);
    persist({ ...stored, state: "recorded" }); if (active.current) { setAction(saved); setReviewed(false); }
  };

  return <section aria-label="本人による公開操作" className="min-w-0 space-y-3 rounded-xl border p-4">
    <h3 className="text-sm font-semibold">本人による公開</h3>
    <p className="text-xs text-muted-foreground">公開準備で内容を凍結し、previewを確認してから本人が公開ボタンを押します。</p>
    {connection.data && !connection.error && <div aria-label="公式APIの対応範囲" className="space-y-2 rounded-lg border p-3 text-xs"><p className="font-medium">{account.platform} · {account.account_ref} の公式API準備状態</p><dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">{providerOperationStates(connection.data.readiness, scope, connection.data.account).map(row => <div key={row.operation}><dt className="text-muted-foreground">{row.label}</dt><dd className="mt-1 break-words">{row.state}</dd></div>)}</dl><p className="text-muted-foreground">利用可能な権限と、この保存版の本文・素材・公開設定を照合して実行します。Bridgeの下書き準備は別の経路です。</p></div>}
    {(error || blocker) && <p role="alert" className="break-words text-sm text-destructive">{error || blocker}</p>}
    <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy || Boolean(blocker) || Boolean(marker) || records.length > 0 || Boolean(history.error) || history.isLoading || !account.connection_id} onClick={() => void run(prepare)}>この保存版の公開準備</Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(async () => { await personaPublicationApi.refreshReadiness(account.id); if (active.current) await connection.mutate(); })}>本人接続後に準備状態を更新</Button>
      <Link className="self-center text-xs text-primary underline" href={`/operations?tab=overview&persona=${encodeURIComponent(scope.personaId)}`}>この人格の接続設定</Link></div>
    {history.error && !error && <p className="text-xs text-destructive">保存済み公開記録を取得できません。公開準備を停止しています。</p>}
    {marker && <div className="space-y-2 rounded-lg border p-3 text-xs"><p>{marker.phase === "execute" ? receiptMatches ? "この版の保存結果を照合済みです。実際の公開範囲は下の外部結果を確認してください。" : "この版の公開操作は送信済み、または送信結果が不明です。新しい投稿は送信しません。" : marker.state !== "recorded" ? "公開準備・承認の結果を確認できません。保存記録を照合するまで公開操作を停止しています。" : "この版の公開準備記録があります。保存記録を照合してください。"}</p><Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => inspect())}>保存記録・receiptを照合</Button></div>}
    {!marker && records.length > 0 && <div className="space-y-2" aria-label="保存済み公開記録">{records.map(r => <Button key={r.id} className="max-w-full whitespace-normal break-all" size="sm" variant="outline" disabled={busy} onClick={() => void run(() => inspect(r.id))}>{r.status} · 公開記録 {r.id}</Button>)}</div>}
    {action && preview && <div className="min-w-0 space-y-3 rounded-lg border p-3" aria-label="凍結した公開preview"><p className="break-all text-xs">公開記録 {action.id} · 版 {action.action_version} · 内容hash {action.payload_hash}</p>
      <SavedVariantPreview revision={{ ...revision, payload: preview.manifest.payload as ContentVariantRevision["payload"] }} />
      <PersonaAssetPreview key={`${action.id}:${action.payload_hash}`} scope={scope} revision={revision} onVerified={setMediaVerified} />
      <p className="text-xs">素材 {assetRefs.length}件 · {assetRefs.length ? mediaVerified ? "登録素材の実体previewを確認済み" : "下の添付素材previewで実体を確認してください。" : "添付素材なし"}{!packageUsable ? " · 公開対象の素材確認が不足しています。" : ""}</p>
      <ul className="space-y-1 text-xs">{preview.manifest.assets.map(a => <li className="break-all" key={`${a.ordinal}:${a.role}`}>{a.role} · {a.filename || a.reference_id} · {a.mime_type} · SHA {a.sha256 || "未確認"}{a.reason ? ` · ${a.reason}` : ""}</li>)}</ul>
      <label className="flex items-start gap-2 text-sm"><Checkbox checked={reviewed} disabled={busy || marker?.state !== "recorded" || marker?.phase === "execute"} onCheckedChange={setReviewed} /><span className="min-w-0 break-words">{scope.displayName} / {account.account_ref} / 保存済み内容・公開範囲・予定時刻を確認しました</span></label>
      <Button disabled={busy || Boolean(blocker) || !reviewed || !packageUsable || !mediaVerified || marker?.state !== "recorded" || marker?.phase === "execute" || Boolean(action.attempts?.length) || Boolean(action.receipt) || !["proposed", "pending_approval", "approved"].includes(action.status ?? "")} onClick={() => void run(publish)}>{busy ? "保存・送信状態を確認中…" : revision.platform === "youtube" ? "本人が YouTube にupload" : "本人が X に投稿"}</Button>
    </div>}
    {action && <div aria-label="保存された公開結果" className="min-w-0 space-y-3"><p className="break-all text-xs">公開記録 {action.id} · 版 {action.action_version} · 状態: {action.status}。公開成否・実際の公開範囲は保存された receipt の確認結果を参照してください。</p>
      {action.receipt && !receiptMatches && <p role="alert" className="text-sm text-destructive">外部結果のAction版・実行記録が保存した公開内容と一致しません。公開成功として扱わず、同じ実行の外部状態を照合してください。</p>}
      {marker?.phase === "execute" && <div className="space-y-2"><p className="text-xs text-muted-foreground">結果が不明な場合は同じ実行を照会します。YouTubeの再開は保存済みsessionがある場合だけ利用でき、サーバーで承認・素材・実行中状態を再確認します。</p><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => recover(false))}>同じ実行の外部状態を再照合</Button>{revision.platform === "youtube" && action.attempts?.some(a => a.provider_recovery_available === true) && <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => recover(true))}>保存済みの同じ動画uploadを再開</Button>}</div></div>}
      <ActionReceiptPanel action={{ id: action.id, receipt: receiptMatches ? action.receipt : null }} />
    </div>}
  </section>;
}
