"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { personaPublicationApi } from "@/lib/media-persona-publication-api";
import {
  mediaOperationsSetupApi,
  type PlatformAccount,
  type PlatformCredentialDetail,
} from "@/lib/media-operations-setup-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { withBasePath } from "@/lib/base-path";
import { OAUTH_RETURN_STORAGE_KEY } from "@/lib/media-oauth-result";

const OAUTH_BLOCKER_MESSAGES: Record<string, string> = {
  oauth_client_id_missing: "このProjectのOAuth client IDが未設定です。",
  oauth_client_secret_missing: "このProjectのOAuth client secretがサーバー設定にありません。値は画面に表示されません。",
  oauth_redirect_uri_missing: "このProjectのHTTPS callback URLが未設定です。",
  oauth_redirect_uri_invalid: "登録済みのHTTPS callback URL（query/fragmentなし）が必要です。",
  platform_account_inactive: "この接続先は停止中です。実在するaccount/channelを登録してから有効化してください。",
  oauth_manual_bridge_identity_only: "この接続はmanual Bridgeの本人確認専用です。OAuth grantは開始できません。",
};

function blockerMessage(blocker: string): string {
  return OAUTH_BLOCKER_MESSAGES[blocker] ?? blocker;
}

function responseStatus(error: unknown): number | null {
  if (typeof error !== "object" || error === null || !("status" in error)) {
    return null;
  }
  const status = Number((error as { status?: unknown }).status);
  return Number.isFinite(status) ? status : null;
}

/** Shared by every surface that shows a slot's connection, so one read serves all of them. A missing credential is `null`. */
export function usePlatformCredential(ownerUserId: string, accountId: string) {
  return useSWR(["persona-oauth-credential", ownerUserId, accountId], async () => {
    try {
      return await mediaOperationsSetupApi.getPlatformCredential(accountId);
    } catch (error) {
      if (responseStatus(error) === 404) return null;
      throw error;
    }
  }, { shouldRetryOnError: false, revalidateOnFocus: false });
}

export function hasProviderIdentityReceipt(credential: PlatformCredentialDetail): boolean {
  return credential.connection_type === "oauth"
    && credential.status === "verified"
    && credential.verification_code === "identity_match"
    && credential.capabilities.identity === "available"
    && Boolean(credential.last_verified_at);
}

function credentialStatusMessage(credential: PlatformCredentialDetail | null): string {
  if (!credential) return "未接続";
  const type = credential.connection_type === "cookie_export"
    ? "Cookie"
    : credential.connection_type === "oauth"
      ? "OAuth"
      : credential.connection_type === "api_token"
        ? "API token"
        : "認証情報";
  if (credential.verification_code === "credential_expired") return "認証期限切れ";
  if (credential.status === "verified") {
    return hasProviderIdentityReceipt(credential) ? `${type}・本人確認済み` : `${type}・確認receipt要照合`;
  }
  if (credential.status === "verification_pending") return `${type}・本人確認待ち`;
  if (credential.status === "invalid") return `${type}・確認失敗`;
  if (credential.status === "disabled") return `${type}・無効化済み`;
  return `${type}・${credential.status}`;
}

function verificationReceiptMessage(credential: PlatformCredentialDetail | null): string {
  if (!credential) return "Provider本人確認のreceiptはありません。";
  if (credential.verification_code === "credential_expired") {
    return credential.verification_completed_at
      ? `最終確認結果: 認証期限切れ（${new Date(credential.verification_completed_at).toLocaleString("ja-JP")}）`
      : "最終確認結果: 認証期限切れ";
  }
  if (credential.status === "verified") {
    if (!hasProviderIdentityReceipt(credential)) return "Provider本人確認のreceiptを確認できません。接続状態を再取得してください。";
    return `Provider本人確認済み（${new Date(credential.last_verified_at!).toLocaleString("ja-JP")}）`;
  }
  if (credential.status === "verification_pending" && credential.verification_started_at) {
    return `確認待ち状態の更新時刻: ${new Date(credential.verification_started_at).toLocaleString("ja-JP")}（Provider本人確認receiptなし）`;
  }
  if (credential.verification_completed_at) {
    return `最終確認結果: 認証失敗（${new Date(credential.verification_completed_at).toLocaleString("ja-JP")}）`;
  }
  return "Provider本人確認のreceiptはありません。";
}

export function PersonaAccountConnection({ scope, account, variantId }: { scope: PersonaWorkspaceScope; account: PlatformAccount; variantId?: string }) {
  const metadata = useSWR(["persona-oauth", scope.ownerUserId, scope.personaId, account.id, account.current_revision.id], () => personaPublicationApi.oauth(account.id), { shouldRetryOnError: false, revalidateOnFocus: false });
  const credentialState = usePlatformCredential(scope.ownerUserId, account.id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(false);
  const pending = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const data = metadata.data;
  const credential = credentialState.data ?? null;
  const valid = data && data.platform_account_id === account.id && data.project_id === scope.projectId && data.platform === account.platform && data.account_ref === account.account_ref && data.account_revision === account.current_revision.version;
  const connect = async () => {
    if (pending.current || !data || !valid || !data.configured || data.blockers.length) return;
    pending.current = true; setBusy(true); setError("");
    try {
      const result = await personaPublicationApi.startOAuth(account.id, { expected_account_revision: data.account_revision, expected_credential_revision: data.credential_revision });
      if (!mounted.current) return;
      if (result.platform_account_id !== account.id || result.project_id !== scope.projectId || result.platform !== account.platform || result.account_ref !== account.account_ref) throw new Error("本人接続の対象が選択した人格・接続先と一致しません。");
      const url = new URL(result.authorization_url);
      const official = account.platform === "youtube" ? url.hostname === "accounts.google.com" : ["x.com", "twitter.com"].includes(url.hostname);
      if (url.protocol !== "https:" || url.username || url.password || !official || !Number.isFinite(Date.parse(result.expires_at)) || Date.parse(result.expires_at) <= Date.now()) throw new Error("本人接続の公式URLまたは有効期限を確認できません。");
      const returnParams = new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: variantId ? "publish" : "brief" });
      if (variantId) returnParams.set("variant", variantId);
      const returnPath = withBasePath(`/operations?${returnParams}`);
      sessionStorage.setItem(OAUTH_RETURN_STORAGE_KEY, JSON.stringify({ path: returnPath, accountId: account.id, accountRef: account.account_ref, platform: account.platform }));
      window.location.assign(url.href);
    } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : "本人接続を開始できません。"); }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  };
  const blocked = !valid || !data?.configured || Boolean(data?.blockers.length);
  return <div className="min-w-0 space-y-2" data-testid="persona-account-connection">
    <p className="break-words text-xs font-medium">{account.platform === "youtube" ? "YouTube" : "X"} の本人接続 · {account.account_ref}</p>
    {credentialState.isLoading ? <p role="status" className="text-xs">認証状態を確認中…</p> : credentialState.error ? <p role="alert" className="text-xs">認証状態を取得できません。状態を再取得してください。</p> : <div className="space-y-0.5 text-xs" data-testid="persona-account-credential-state"><p>認証状態: {credentialStatusMessage(credential)}</p><p className="text-muted-foreground">{verificationReceiptMessage(credential)}</p></div>}
    {metadata.isLoading ? <p className="text-xs">接続設定を確認中…</p> : metadata.error ? <p className="text-xs">接続設定を取得できません。接続設定を再取得してください。</p> : data && !valid ? <p className="text-xs">接続設定の人格・Project・account版が一致しません。設定を再取得してください。</p> : valid && data ? <>
      {!data.configured && <p className="text-xs">このProjectの公式 OAuth client ID/secret とHTTPS callback URLが未設定です。サーバー側の接続設定後に本人接続できます。</p>}
      {data.blockers.length > 0 && <p className="break-words text-xs">未解決の接続条件: {data.blockers.map(blockerMessage).join(" / ")}</p>}
    </> : null}
    {error && <p className="break-words text-xs text-destructive">{error}</p>}
    <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy || blocked} onClick={() => void connect()}>本人が公式画面で接続する</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void Promise.all([metadata.mutate(), credentialState.mutate()])}>接続状態を再取得</Button></div>
    {valid && data ? <details className="text-[11px] text-muted-foreground"><summary className="cursor-pointer">権限と外部設定</summary>
      <p className="mt-1 break-all">確認する権限: {data.required_scopes.join(" / ")}</p>
      {data.external_checks.length > 0 && <p>外部設定での確認事項: {data.external_checks.join(" / ")}</p>}
    </details> : null}
  </div>;
}
