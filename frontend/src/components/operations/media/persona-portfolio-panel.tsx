"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { mediaBrandPortfolioApi, portfolioReadOptions } from "@/lib/media-brand-portfolio-api";

export function PersonaPortfolioPanel({ onSaved, setupBlockedReason }: { onSaved: () => Promise<unknown>; setupBlockedReason?: string }) {
  const options = portfolioReadOptions;
  const portfolio = useSWR("sns-v6-portfolio", mediaBrandPortfolioApi.get, options);
  const definition = useSWR("sns-v6-definition", mediaBrandPortfolioApi.definition, options);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const source = definition.data;
  const current = portfolio.data;
  const matching = current && source && current.source.sha256 === source.source.sha256;
  const storageKey = matching ? `aoitalk-sns-v6-setup:${encodeURIComponent(current.owner_user_id)}:${source.source.sha256}` : null;
  const reload = async () => { await portfolio.mutate(); await onSaved(); };

  useEffect(() => {
    if (!storageKey || current?.status === "ready") return;
    const update = () => { try { setUncertain(Boolean(localStorage.getItem(storageKey))); } catch { setError("登録操作の記録を確認できません。ブラウザの保存設定を確認してください。"); } };
    const task = window.setTimeout(update, 0); window.addEventListener("storage", update);
    return () => { window.clearTimeout(task); window.removeEventListener("storage", update); };
  }, [storageKey, current?.status]);

  const setup = async (resume: boolean) => {
    if (lock.current || busy || setupBlockedReason || !matching || !storageKey || !current.owner_user_id || current.status === "ready") return;
    lock.current = true; setBusy(true); setError("");
    const owner = current.owner_user_id;
    const sha = source.source.sha256;
    try {
      if (!navigator.locks) throw new Error("同時登録を防ぐ機能を利用できません。対応したブラウザで初期構成を用意してください。");
      await navigator.locks.request(storageKey, async () => {
        // A second tab checks persisted server state before making any mutation.
        const fresh = await mediaBrandPortfolioApi.get();
        if (fresh.owner_user_id !== owner || fresh.source.sha256 !== sha) throw new Error("認証ownerまたは原文SHAが変わりました。構成を再確認してください。");
        if (fresh.status === "ready") { await portfolio.mutate(fresh, { revalidate: false }); await onSaved(); return; }
        const pending = localStorage.getItem(storageKey);
        if (pending && !resume) { setUncertain(true); throw new Error("初期構成の保存結果を確認する必要があります。保存状態を確認し、同じ登録操作を照合して再開してください。"); }
        let key: string;
        if (pending) {
          const record = JSON.parse(pending) as Record<string, unknown>;
          if (!record || Object.keys(record).length !== 3 || record.owner !== owner || record.sha !== sha || typeof record.key !== "string" || !/^[0-9a-f-]{36}$/iu.test(record.key)) throw new Error("保存された初期登録操作のowner・原文SHA・冪等キーを確認できません。再取得して確認してください。");
          key = record.key;
        } else {
          if (resume) throw new Error("再開する初期登録操作の記録を確認できません。構成を再取得してください。");
          key = crypto.randomUUID();
          const record = JSON.stringify({ owner, sha, key });
          localStorage.setItem(storageKey, record);
          if (localStorage.getItem(storageKey) !== record) throw new Error("登録操作の記録を保存できません。ブラウザの保存設定を確認してください。");
        }
        const saved = await mediaBrandPortfolioApi.setup({ expected_owner_user_id: owner, expected_source_sha256: sha, project_bindings: {} }, key);
        if (saved.owner_user_id !== owner || saved.source.sha256 !== sha || saved.status !== "ready") throw new Error("保存された構成のowner・原文SHA・状態を確認できません。");
        const readback = await mediaBrandPortfolioApi.get();
        if (readback.owner_user_id !== owner || readback.source.sha256 !== sha || readback.status !== "ready") throw new Error("登録後の読戻しでowner・原文SHA・登録状態が一致しません。構成を再取得して確認してください。");
        await portfolio.mutate(readback, { revalidate: false }); await onSaved();
      });
    } catch (cause) { try { setUncertain(Boolean(localStorage.getItem(storageKey))); } catch { setUncertain(true); } setError(cause instanceof Error ? cause.message : "初期構成の保存結果を確認できません。構成を再取得してください。"); }
    finally { lock.current = false; setBusy(false); }
  };

  return <div className="relative rounded-xl border bg-card p-4">
    <details open={Boolean(error || portfolio.error || definition.error) || current?.status !== "ready"} aria-label="SNS v6の人格構成">
      <summary className="cursor-pointer pr-7 text-sm font-semibold">SNS v6 · 8人格のProject構成{current?.status === "ready" ? "（登録済み）" : ""}</summary>
      <div className="mt-3 space-y-3">
      {(error || portfolio.error || definition.error || (current && source && !matching)) && <p role="alert" className="text-sm text-destructive">{error || (current && source && !matching ? "構成と原文のSHAが一致しません。構成を再取得してください。" : "SNS v6の構成を取得できませんでした。構成を再取得してください。")}</p>}
      {portfolio.isValidating || definition.isValidating ? <p role="status" className="text-sm">SNS v6の構成を確認中…</p> : null}
      {source && <ul className="grid gap-2 text-sm sm:grid-cols-2 xl:grid-cols-4">{source.brands.map(brand => {
        const configured = current?.brands.find(b => b.definition.brand_key === brand.brand_key);
        return <li key={brand.brand_key} className="min-w-0 rounded-lg border p-3"><span className="block break-words font-medium">{brand.display_name}</span><span className="mt-1 block text-xs text-muted-foreground">{portfolio.error || !current ? "登録状態を確認中" : configured?.persona && configured.project_id ? "人格・Project登録済み" : "このログインでは未登録"}</span><span className="mt-1 block break-words text-xs text-muted-foreground">{brand.channels.map(c => c.platform).join(" / ")} · 媒体候補</span></li>;
      })}</ul>}
      <div className="flex flex-wrap gap-2"><Button variant="outline" size="sm" disabled={busy} onClick={() => { setError(""); void Promise.all([reload(), definition.mutate()]); }}>構成を再取得</Button>
        {current?.status !== "ready" && <Button size="sm" disabled={busy || Boolean(setupBlockedReason) || !matching || Boolean(portfolio.error || definition.error)} onClick={() => void setup(uncertain)}>{busy ? "保存・照合中…" : uncertain ? "同じ登録操作を照合して再開" : "この8人格の初期構成を用意"}</Button>}</div>
      {setupBlockedReason && current?.status !== "ready" && <p role="status" className="text-sm">{setupBlockedReason}</p>}
      {uncertain && current?.status !== "ready" && <p role="status" className="text-sm">保存結果は未確認です。本人の再開操作では保存状態を確認し、未登録の場合だけ同じbody・冪等キーで登録を照合します。</p>}
      {source && <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">確認済み原文</summary><p className="mt-1 break-words">{source.source.filename}</p><p className="break-all">SHA-256: {source.source.sha256}</p></details>}
      </div>
    </details>
    <span className="absolute right-4 top-4"><HelpPopover label="SNS v6 · 8人格のProject構成">確認済みの原文から8つの公開人格と非公開のSNS事業本部を用意します。既存の同名Project・人格・制作計画はbackendで照合します。アカウントの接続は本人が設定します。</HelpPopover></span>
  </div>;
}
