"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { AppSelect } from "@/components/ui/app-select";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { browserPreparationApi, browserPreparationDeviceReady } from "@/lib/media-browser-preparation-api";
import { bridgeConnectionApi, bridgeConnectionLabelsValid, bridgeConnectionPreviewMatches, bridgeConnectionRefValid, bridgeConnectionSaveMatches, type BridgeConnectionPreview, type BridgeConnectionPreviewRequest } from "@/lib/media-bridge-connection-api";
import { accountInScope, type PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";

type Props = { scope: PersonaWorkspaceScope; accounts: PlatformAccount[]; onSaved: () => unknown | Promise<unknown> };
const options = { shouldRetryOnError: false, revalidateOnFocus: false };
const message = (error: unknown) => error instanceof Error
  ? error.message.includes("bridge_connection_identity_unverified")
    ? "現在のタブで本人の接続先を確認できません。接続は未確認です。対象アカウントの可視表示とBridge接続を確認してください。"
    : error.message
  : "接続を確認できません。現在の接続先を更新してください。";

/** Connects one existing persona-owned slot. Preview evidence never grants API publication. */
export function PersonaBridgeConnectionPanel({ scope, accounts, onSaved }: Props) {
  const [accountId, setAccountId] = useState("");
  const [notice, setNotice] = useState<{ scopeKey: string; accountId: string } | null>(null);
  const scopeKey = `${scope.ownerUserId}:${scope.projectId}:${scope.personaId}:${scope.personaRevisionId}`;
  const selectionEpoch = useRef(0);
  useEffect(() => {
    selectionEpoch.current += 1;
    return () => { selectionEpoch.current += 1; };
  }, [scope.ownerUserId, scope.projectId, scope.personaId, scope.personaRevisionId]);
  const scoped = accounts.filter(account => accountInScope(account, scope));
  const supported = scoped.filter(account => account.platform === "x" || account.platform === "youtube");
  const selected = supported.find(account => account.id === accountId);
  return <section aria-label="Bridge手動接続" className="min-w-0 space-y-3 rounded-xl border bg-card p-4">
    <h2 className="inline-flex items-center gap-1 text-sm font-semibold">
      Bridgeで本人の接続先を設定
      <HelpPopover label="Bridgeで本人の接続先を設定">
        <p>{scope.displayName}の既存媒体枠を、本人が開いたEdgeのアカウントへ結びます。X・YouTubeの下書き準備用です。</p>
        <p>X・YouTube以外の媒体はBridge手動接続に未対応です。</p>
      </HelpPopover>
    </h2>
    {supported.length ? <label className="block min-w-0 space-y-1 text-sm"><span>接続する既存媒体枠</span><AppSelect aria-label="接続する既存媒体枠" className="w-full min-w-0" value={accountId} onValueChange={value => { if (value === accountId) return; selectionEpoch.current += 1; setAccountId(value); setNotice(null); }} triggerContent={<span className="min-w-0 truncate">{selected ? `${selected.platform} · ${selected.current_revision.display_name || selected.account_ref}` : "媒体枠を明示選択"}</span>}><option value="">媒体枠を明示選択</option>{supported.map(account => <option key={account.id} value={account.id}>{account.platform} · {account.current_revision.display_name || account.account_ref}</option>)}</AppSelect></label>
      : <p className="text-sm">この人格にはBridge接続に対応する既存X・YouTube枠がありません。</p>}
    {notice?.scopeKey === scopeKey && notice.accountId === accountId && <p role="status" className="text-sm">Bridge手動接続を同じ媒体枠に保存しました。QA・媒体別内容で現在の接続先を選び、新しい版を保存してください。公開結果は未確認です。</p>}
    {selected && <BridgeConnectionForm key={`${scope.ownerUserId}:${scope.projectId}:${scope.personaId}:${scope.personaRevisionId}:${selected.id}:${selected.current_revision.id}:${selected.current_revision.content_hash}`} scope={scope} account={selected} captureSelection={() => { const epoch = selectionEpoch.current; return () => selectionEpoch.current === epoch; }} onSaved={() => { setNotice({ scopeKey, accountId: selected.id }); return onSaved(); }} onRefresh={onSaved} />}
  </section>;
}

function BridgeConnectionForm({ scope, account, onSaved, onRefresh, captureSelection }: { scope: PersonaWorkspaceScope; account: PlatformAccount; onSaved: Props["onSaved"]; onRefresh: Props["onSaved"]; captureSelection: () => () => boolean }) {
  const [accountRef, setAccountRef] = useState(bridgeConnectionRefValid(account.platform, account.account_ref) ? account.account_ref : "");
  const [deviceId, setDeviceId] = useState("");
  const [tabId, setTabId] = useState("");
  const [browserContextLabel, setBrowserContextLabel] = useState("");
  const [googleAccountLabel, setGoogleAccountLabel] = useState("");
  const [preview, setPreview] = useState<BridgeConnectionPreview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [saveAttempted, setSaveAttempted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(false);
  const pending = useRef(false);
  const mountEpoch = useRef(0);
  const requestEpoch = useRef(0);
  const devices = useSWR(["persona-browser-devices", scope.ownerUserId], browserPreparationApi.devices, options);
  const selectedDevice = !devices.error ? devices.data?.find(device => device.id === deviceId) : undefined;
  const deviceReady = browserPreparationDeviceReady(selectedDevice);
  const tab = Number(tabId);
  const input: BridgeConnectionPreviewRequest = { owner_user_id: scope.ownerUserId, project_id: scope.projectId, persona_id: scope.personaId,
    expected_account_revision_id: account.current_revision.id, expected_account_revision_hash: account.current_revision.content_hash,
    account_ref: account.platform === "x" ? accountRef.toLowerCase() : accountRef, device_id: deviceId, tab_id: tab,
    browser_context_label: browserContextLabel.trim(), google_account_label: account.platform === "youtube" ? googleAccountLabel.trim() : null };
  const inputValid = accountInScope(account, scope) && bridgeConnectionRefValid(account.platform, accountRef) && deviceReady && Number.isSafeInteger(tab) && tab > 0 && bridgeConnectionLabelsValid(account.platform, input.browser_context_label, input.google_account_label);
  const clearPreview = () => { setPreview(null); setConfirmed(false); setError(""); };
  useEffect(() => { active.current = true; mountEpoch.current += 1; return () => { active.current = false; mountEpoch.current += 1; }; }, []);
  useEffect(() => {
    if (!preview) return;
    const timer = window.setTimeout(() => { setPreview(null); setConfirmed(false); setError("接続previewの有効期限が切れました。既存タブを再照合してください。"); }, Math.max(0, Date.parse(preview.expires_at) - Date.now()));
    return () => window.clearTimeout(timer);
  }, [preview]);
  const run = async (work: (isCurrent: () => boolean) => Promise<void>) => {
    if (pending.current || !active.current) return;
    const selectionCurrent = captureSelection();
    const mount = mountEpoch.current;
    const request = ++requestEpoch.current;
    const isCurrent = () => active.current && mountEpoch.current === mount && requestEpoch.current === request && selectionCurrent();
    pending.current = true; setBusy(true); setError("");
    try { await work(isCurrent); } catch (cause) { if (isCurrent()) setError(message(cause)); }
    finally { if (requestEpoch.current === request) pending.current = false; if (isCurrent()) setBusy(false); }
  };
  const check = async (isCurrent: () => boolean) => {
    clearPreview();
    if (!inputValid || saveAttempted) throw new Error("本人の接続先・接続済みPC・既存タブを明示選択してください。");
    const value = await bridgeConnectionApi.preview(account, input);
    if (!bridgeConnectionPreviewMatches(value, account, input) || value.persona_display_name !== scope.displayName) throw new Error("接続previewが選択した人格・枠・接続先と一致しません。workspaceを更新してください。");
    if (isCurrent()) setPreview(value);
  };
  const save = async (isCurrent: () => boolean) => {
    if (!preview || !confirmed || !inputValid || saveAttempted || !bridgeConnectionPreviewMatches(preview, account, input)) throw new Error("本人が接続previewを確認してから保存してください。");
    setSaveAttempted(true);
    try {
      const value = await bridgeConnectionApi.save(account, preview, `bridge-connection:${crypto.randomUUID()}`);
      if (!isCurrent()) return;
      if (!bridgeConnectionSaveMatches(value, account, preview)) throw new Error("接続の保存結果を照合できません。");
      setPreview(null); setConfirmed(false);
      await onSaved();
    } catch {
      if (!isCurrent()) return;
      setPreview(null); setConfirmed(false);
      throw new Error("接続の保存結果を確認できません。保存状態を更新して接続先と版を確認してください。同じpreviewは再送しません。");
    }
  };

  return <div className="min-w-0 space-y-3">
    <p className="break-all text-xs text-muted-foreground">既存枠: {account.id} · 現在の接続先: {account.account_ref} · {account.status}</p>
    <div className="grid min-w-0 gap-3 sm:grid-cols-2">
      <label className="min-w-0 space-y-1 text-sm"><span>{account.platform === "x" ? "本人が接続するXの@handle" : "本人が接続するYouTubeのUC channel ID"}</span><Input aria-label={account.platform === "x" ? "本人が接続するXの@handle" : "本人が接続するYouTubeのUC channel ID"} value={accountRef} maxLength={account.platform === "x" ? 16 : 24} disabled={busy || saveAttempted} onChange={event => { setAccountRef(event.target.value); clearPreview(); }} placeholder={account.platform === "x" ? "@brand_handle" : "UCから始まる24文字のchannel ID"} /></label>
      <label className="min-w-0 space-y-1 text-sm"><span>接続確認に使う本人のPC</span><AppSelect aria-label="接続確認に使う本人のPC" className="w-full min-w-0" value={deviceId} disabled={busy || saveAttempted} onValueChange={value => { setDeviceId(value); clearPreview(); }} triggerContent={<span className="min-w-0 truncate">{selectedDevice?.name || "PCを明示選択"}</span>}><option value="">PCを明示選択</option>{!devices.error && devices.data?.map(device => <option key={device.id} value={device.id}>{device.name} · {browserPreparationDeviceReady(device) ? "ブラウザ接続済み" : "接続を確認"}</option>)}</AppSelect></label>
      <label className="min-w-0 space-y-1 text-sm"><span>接続確認する既存タブID</span><Input aria-label="接続確認する既存タブID" type="number" min={1} step={1} value={tabId} disabled={busy || saveAttempted} onChange={event => { setTabId(event.target.value); clearPreview(); }} /></label>
      <label className="min-w-0 space-y-1 text-sm"><span>本人が使うEdge profileの識別ラベル</span><Input aria-label="本人が使うEdge profileの識別ラベル" maxLength={80} value={browserContextLabel} disabled={busy || saveAttempted} placeholder="例: UN//REAL用profile" onChange={event => { setBrowserContextLabel(event.target.value); clearPreview(); }} /></label>
      {account.platform === "youtube" && <label className="min-w-0 space-y-1 text-sm"><span>本人が使うGoogleアカウントの識別ラベル</span><Input aria-label="本人が使うGoogleアカウントの識別ラベル" maxLength={80} value={googleAccountLabel} disabled={busy || saveAttempted} placeholder="例: UN//REAL用Googleアカウント" onChange={event => { setGoogleAccountLabel(event.target.value); clearPreview(); }} /></label>}
    </div>
    <p className="inline-flex items-center gap-1 text-xs">
      Bridge接続手順
      <HelpPopover label="Bridge接続手順">
        <p>対応版BridgeでこのProject・人格・アカウントを選び「専用ブラウザを開く」を押してください。本人がそのブラウザへログインし、{account.platform === "x" ? "空の投稿入力欄" : "対象チャンネルのYouTube Studioで空の動画アップロード画面"}を開いてください。そのタブをBridgeで選択し、既存タブIDを指定します。タブの照合は読み取り専用です。</p>
        <p>この接続ではOAuth・token・認証パッケージを追加しません。公式APIの認証状態は未確認のままです。</p>
        <p>複数人格のprofileは同じPCでも本人が1つずつ切り替えて接続します。profile切替やBridge再接続の後は、現在のタブで新しいpreviewと接続を保存してください。</p>
        {account.platform === "youtube" ? (
          <>
            <p>Googleアカウントのラベルは本人が区別するために付けた名前です。Google本人確認済みの情報ではありません。確認する公開先は画面のUCチャンネルIDです。</p>
            <p>YouTubeはStudioのAccountメニューに表示されたチャンネル名・@handleと、同じEdgeウィンドウで開いた公開チャンネルの「動画を管理」「チャンネルをカスタマイズ」を照合します。Accountメニューを表示して読み取り確認し、3分以内に空のUpload画面で再確認してください。期限切れやチャンネル・画面の変更で停止したら、Accountメニューを表示して確認し直してください。</p>
          </>
        ) : null}
      </HelpPopover>
    </p>
    {(error || devices.error) && <p role="alert" className="break-words text-sm text-destructive">{error || "本人の接続済みPCを取得できません。PC接続を確認してください。"}</p>}
    <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy || !inputValid || saveAttempted} onClick={() => void run(check)}>既存タブの本人接続を読み取りで確認</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void run(async isCurrent => { clearPreview(); await onRefresh(); if (!isCurrent()) return; await devices.mutate(); if (isCurrent()) setSaveAttempted(false); })}>接続の保存状態を更新</Button><Link className="self-center text-xs underline" href="/settings#pc-bridge">PC接続を確認</Link></div>
    {preview && <div aria-label="Bridge本人接続preview" className="min-w-0 space-y-3 rounded-lg border p-3 text-sm">
      <dl className="grid min-w-0 gap-2 sm:grid-cols-2"><div><dt className="text-xs text-muted-foreground">Edge profileの識別ラベル（本人の申告）</dt><dd className="break-words">{preview.browser_context_label}</dd></div>{preview.platform === "youtube" && <div><dt className="text-xs text-muted-foreground">Googleアカウントの識別ラベル（本人の申告）</dt><dd className="break-words">{preview.google_account_label}</dd></div>}<div className="sm:col-span-2"><dt className="text-xs text-muted-foreground">Bridgeが照合したprofile context</dt><dd className="break-all">context {preview.browser_context.context_id} · epoch {preview.browser_context.context_epoch}</dd></div></dl>
      <dl className="grid min-w-0 gap-2 sm:grid-cols-2"><div><dt className="text-xs text-muted-foreground">人格・接続先</dt><dd className="break-words">{preview.persona_display_name} · {preview.platform} · {preview.account_ref}</dd></div><div><dt className="text-xs text-muted-foreground">本人PC・既存タブ</dt><dd className="break-all">{selectedDevice?.name} · {preview.device_id} · タブ {preview.tab_id}</dd></div><div><dt className="text-xs text-muted-foreground">所属・既存媒体枠</dt><dd className="break-all">Owner {preview.owner_user_id} · Project {preview.project_id} · 人格 {preview.persona_id} · 枠 {preview.platform_account_id}</dd></div><div><dt className="text-xs text-muted-foreground">接続の保存版</dt><dd className="break-all">{preview.platform_account_revision_id} · hash {preview.platform_account_revision_hash}</dd></div></dl>
      <p className="text-xs">保存すると接続の版が変わり、以前の内容版・Action・承認は使えません。公式APIは未確認、投稿・公開も未実行です。</p>
      <label className="flex items-start gap-2 text-sm"><Checkbox checked={confirmed} disabled={busy || saveAttempted} onCheckedChange={setConfirmed} /><span className="min-w-0 break-words">この人格・Project・既存媒体枠を、表示された接続先・本人のPC・profile・タブ{preview.platform === "youtube" ? "・本人申告のGoogleアカウントラベル" : ""}へ結ぶことを確認しました</span></label>
      <Button disabled={busy || !confirmed || !inputValid || saveAttempted} onClick={() => void run(save)}>本人が確認してBridge手動接続を保存</Button>
    </div>}
  </div>;
}
