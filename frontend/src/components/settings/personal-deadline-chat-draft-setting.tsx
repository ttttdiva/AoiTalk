"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const KEY = "personal.deadline_chat_draft.enabled";

function readEnabled(value: unknown): boolean {
  if (!value || typeof value !== "object") throw new Error("設定の形式を確認できません");
  const settings = (value as Record<string, unknown>).settings;
  if (!settings || typeof settings !== "object") throw new Error("設定の形式を確認できません");
  const personal = (settings as Record<string, unknown>).personal;
  if (personal === undefined) return false;
  if (!personal || typeof personal !== "object") throw new Error("設定の形式を確認できません");
  const draft = (personal as Record<string, unknown>).deadline_chat_draft;
  if (draft === undefined) return false;
  if (!draft || typeof draft !== "object") throw new Error("設定の形式を確認できません");
  const enabled = (draft as Record<string, unknown>).enabled;
  if (enabled === undefined) return false;
  if (typeof enabled !== "boolean") throw new Error("設定値はJSON booleanではありません");
  return enabled;
}

async function settingsRequest(method: "GET" | "PATCH", value?: boolean): Promise<unknown> {
  const response = await fetch("/api/python-proxy/settings", {
    method,
    credentials: "include",
    cache: "no-store",
    headers: method === "PATCH" ? { "Content-Type": "application/json" } : undefined,
    body: method === "PATCH" ? JSON.stringify({ key: KEY, value, persist: true }) : undefined,
  });
  if (!response.ok) throw new Error(response.status === 403 ? "設定の変更には管理者権限が必要です" : `設定を取得できませんでした（${response.status}）`);
  return response.json();
}

export function PersonalDeadlineChatDraftSetting({ isAdmin }: { isAdmin: boolean }) {
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const epoch = useRef(0);

  const reload = useCallback(async () => {
    const current = ++epoch.current;
    setLoaded(false);
    setMessage("");
    try {
      const next = readEnabled(await settingsRequest("GET"));
      if (epoch.current !== current) return;
      setEnabled(next);
      setLoaded(true);
    } catch (error) {
      if (epoch.current === current) setMessage(error instanceof Error ? error.message : "設定を取得できませんでした");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load the server-owned setting after mount.
    void reload();
    return () => { epoch.current += 1; };
  }, [reload]);

  async function toggle() {
    if (!isAdmin || !loaded || busy) return;
    const current = ++epoch.current;
    const next = !enabled;
    setBusy(true);
    setMessage("");
    try {
      await settingsRequest("PATCH", next);
      const confirmed = readEnabled(await settingsRequest("GET"));
      if (epoch.current !== current) return;
      setEnabled(confirmed);
      setMessage(confirmed === next ? "設定を保存しました。" : "保存値を再取得しました。表示値を確認してください。");
    } catch (error) {
      if (epoch.current === current) {
        setMessage(error instanceof Error ? `${error.message}。保存状態を再取得します。` : "保存結果を確認できませんでした。保存状態を再取得します。");
        try {
          const confirmed = readEnabled(await settingsRequest("GET"));
          if (epoch.current === current) setEnabled(confirmed);
        } catch {
          if (epoch.current === current) setLoaded(false);
        }
      }
    } finally {
      if (epoch.current === current) setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border p-4" aria-labelledby="personal-deadline-draft-setting">
      <h2 id="personal-deadline-draft-setting" className="font-semibold">個人Agentの期限候補</h2>
      <p className="mt-1 text-sm text-muted-foreground">ローカル限定のprivate Chatで、本人が書いた日付を期限候補として確認用に下書きします。台帳への反映には本人の受理が必要です。既定はOFFです。</p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button type="button" role="switch" aria-checked={enabled} aria-label="個人Agentの期限候補を有効にする"
          disabled={!isAdmin || !loaded || busy} onClick={() => void toggle()}
          className="inline-flex min-h-11 items-center rounded border px-3 py-2 disabled:cursor-not-allowed disabled:opacity-60">
          {busy ? "保存中…" : enabled ? "有効" : "無効"}
        </button>
        {!isAdmin && <span className="text-xs text-muted-foreground">変更は管理者のみ可能です。</span>}
        {!loaded && !message && <span role="status" className="text-xs">設定を取得中…</span>}
        {message && <span role="status" className="text-xs">{message}</span>}
        {message && !loaded && <button type="button" className="min-h-11 underline" onClick={() => void reload()}>再取得</button>}
      </div>
    </section>
  );
}
