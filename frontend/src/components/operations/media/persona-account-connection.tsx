"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { personaPublicationApi } from "@/lib/media-persona-publication-api";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { withBasePath } from "@/lib/base-path";

export function PersonaAccountConnection({ scope, account, variantId }: { scope: PersonaWorkspaceScope; account: PlatformAccount; variantId: string }) {
  const metadata = useSWR(["persona-oauth", scope.ownerUserId, scope.personaId, account.id, account.current_revision.id], () => personaPublicationApi.oauth(account.id), { shouldRetryOnError: false, revalidateOnFocus: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(false);
  const pending = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const data = metadata.data;
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
      const returnPath = withBasePath(`/operations?${new URLSearchParams({ tab: "studio", persona: scope.personaId, stage: "publish", variant: variantId })}`);
      sessionStorage.setItem("aoitalk-media-oauth-return-v1", JSON.stringify({ path: returnPath, accountId: account.id }));
      window.location.assign(url.href);
    } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : "本人接続を開始できません。"); }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  };
  return <details className="min-w-0 rounded-xl border p-3 text-xs"><summary className="cursor-pointer font-medium">{account.platform === "youtube" ? "YouTube" : "X"} の本人接続 · {account.account_ref}</summary>
    <div className="mt-3 space-y-2"><p className="break-words">{scope.displayName} のこの接続先だけを公式画面で確認します。許可後に公開前の準備状態を更新してください。</p>
      {metadata.isLoading ? <p>接続設定を確認中…</p> : metadata.error ? <p>接続設定を取得できません。接続設定を再取得してください。</p> : data && !valid ? <p>接続設定の人格・Project・account版が一致しません。設定を再取得してください。</p> : valid && data ? <>
        {!data.configured && <p>このProjectの公式 OAuth client と callback URL が未設定です。管理者の接続設定後に本人接続できます。</p>}
        {data.blockers.length > 0 && <p className="break-words">未確認の接続条件: {data.blockers.join(" / ")}</p>}
        <p className="break-all">確認する権限: {data.required_scopes.join(" / ")}</p>
        {data.external_checks.length > 0 && <p>外部設定での確認事項: {data.external_checks.join(" / ")}</p>}
      </> : null}
      {error && <p className="break-words text-destructive">{error}</p>}
      <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy || !valid || !data?.configured || Boolean(data?.blockers.length)} onClick={() => void connect()}>本人が公式画面で接続する</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void metadata.mutate()}>接続設定を再取得</Button></div>
    </div></details>;
}
