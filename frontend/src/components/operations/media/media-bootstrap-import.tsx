"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { BootstrapRunner, journalKey, newJournal, parseBootstrapPlan,
  type BootstrapJournal, type BootstrapPlan, type BootstrapResult } from "@/lib/mediaops-bootstrap";

export function MediaBootstrapImportPanel() {
  const [plan, setPlan] = useState<BootstrapPlan | null>(null);
  const [rows, setRows] = useState<BootstrapResult[]>([]);
  const [previewed, setPreviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [savedCount, setSavedCount] = useState(0);
  const runner = useRef<BootstrapRunner | null>(null);
  const selection = useRef(0);
  const stop = useRef(false);
  const busyRef = useRef(false);
  useEffect(() => () => { stop.current = true; }, []);

  const loadFile = async (file: File | undefined) => {
    if (!file || busyRef.current) return;
    const version = ++selection.current;
    setPlan(null); setRows([]); setPreviewed(false); setError(""); setMessage(""); setSavedCount(0);
    runner.current = null;
    try {
      if (file.size > 16 * 1024 * 1024) throw new Error("ファイルは16 MiB以下にしてください");
      const parsed = await parseBootstrapPlan(await file.text());
      if (selection.current !== version) return;
      const stored = localStorage.getItem(journalKey(parsed));
      const journal: BootstrapJournal = stored ? JSON.parse(stored) : newJournal(parsed);
      const next = new BootstrapRunner(parsed, journal, undefined, value => {
        localStorage.setItem(journalKey(parsed), JSON.stringify(value));
        setSavedCount(Object.keys(value.bindings).length);
      });
      runner.current = next; setPlan(parsed); setSavedCount(Object.keys(journal.bindings).length);
      setMessage("ファイル検証済み。次に認証済みowner・現在のモデル経路・既存データとの差分を確認します");
    } catch (cause) {
      if (selection.current === version) setError(cause instanceof Error ? cause.message : "ファイルを検証できませんでした");
    }
  };
  const execute = async (mode: "preview" | "apply" | "readback" | "initial" | "pause") => {
    const active = runner.current;
    if (!active || busyRef.current || (mode === "apply" && !previewed)) return;
    busyRef.current = true; stop.current = false; setBusy(true); setError(""); setMessage("");
    try {
      if (!navigator.locks) throw new Error("このブラウザは同時登録の防止に対応していません");
      await navigator.locks.request(journalKey(active.plan), { ifAvailable: true }, async lock => {
        if (!lock) throw new Error("別のタブで同じ登録を処理しています");
        // Re-read the journal after acquiring the cross-tab lock.
        const saved = localStorage.getItem(journalKey(active.plan));
        if (saved) {
          const value = JSON.parse(saved) as BootstrapJournal;
          if (value.owner !== active.plan.expected_owner_user_id || value.scope_key !== active.plan.scope_key) throw new Error("再開記録のowner/scopeが一致しません");
          Object.assign(active.journal, value);
        }
        if (mode === "initial") {
          const result = await active.initialRun();
          setMessage(`初回Research: ${result.state} / ${result.id}。実行状態と調査充足・モデル実推論は別判定です`);
        } else if (mode === "pause") {
          const count = await active.pauseCreated();
          setMessage(`${count}件を一時停止しました。資料・根拠・revisionは保持しています`);
        } else {
          const result = await active.run(mode, setRows, () => stop.current);
          if (mode === "preview") setPreviewed(result.length === active.plan.actions.length);
          setMessage(stop.current ? "現在のAPI操作と再読込を終えて停止しました。次回は保存済み位置から確認します" : mode === "preview" ? "差分確認済み。適用を押すまで登録しません" : "保存済みデータを正規APIから再読込して照合しました");
        }
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "処理を継続できませんでした");
      if (active.journal.pending) setMessage(`結果の照合が必要です: ${active.journal.pending.key}。再開時も同じ冪等キーを維持します`);
    } finally { busyRef.current = false; setBusy(false); }
  };

  return <Card data-testid="media-bootstrap-import">
    <CardHeader>
      <CardTitle className="inline-flex items-center gap-1">
        MediaOps 初期登録
        <HelpPopover label="MediaOps 初期登録">検証済みplan JSONから原本・Agent定義・調査データを取り込みます。research_onlyで登録し、公開・購入・権限変更・モデル代替・定期起動は行いません。</HelpPopover>
      </CardTitle>
    </CardHeader>
    <CardContent className="space-y-3">
      <label className="block text-sm" htmlFor="media-bootstrap-file">Bootstrap plan JSON（16 MiBまで）</label>
      <input id="media-bootstrap-file" type="file" accept="application/json,.json" disabled={busy}
        onChange={event => { void loadFile(event.target.files?.[0]); }} />
      {plan ? <div className="space-y-2 text-sm">
        <p>Scope: {plan.scope_key} / {plan.actions.length}件 / 保存済み {savedCount}件</p>
        <p className="break-all text-xs text-muted-foreground">Owner: {plan.expected_owner_user_id} / SHA-256: {plan.plan_hash}</p>
        {plan.blocked.length ? <details><summary>未解決の項目（{plan.blocked.length}件）</summary><ul className="list-disc pl-5">{plan.blocked.map((item, index) => <li key={index}>{String(item.key)}: {String(item.reason)}</li>)}</ul></details> : null}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={busy} onClick={() => void execute("preview")}>差分を確認</Button>
          <Button type="button" disabled={busy || !previewed} onClick={() => void execute("apply")}>{savedCount ? "照合して残りを適用" : "この内容を適用"}</Button>
          <Button type="button" variant="outline" disabled={busy || !savedCount} onClick={() => void execute("readback")}>保存結果を再読込</Button>
          <Button type="button" variant="outline" disabled={busy || !runner.current?.journal.bindings["automation:bootstrap-research"]} onClick={() => void execute("initial")}>初回Researchを実行</Button>
          <Button type="button" variant="outline" disabled={busy || !savedCount} onClick={() => void execute("pause")}>今回登録した稼働対象を停止</Button>
          {busy ? <Button type="button" variant="outline" onClick={() => { stop.current = true; }}>現在の処理後に止める</Button> : null}
        </div>
        <p className="inline-flex items-center gap-1 text-xs">
          再開記録の保存範囲
          <HelpPopover label="再開記録の保存範囲">再開記録にはID・hash・処理位置だけをこのブラウザに保存します。稼働中Agentの停止は今回の新規登録分だけに限定します。</HelpPopover>
        </p>
      </div> : null}
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
      {message ? <p role="status" className="text-sm">{message}</p> : null}
      {rows.length ? <details open={rows.length < 10}><summary>照合結果 {rows.length} / {plan?.actions.length}</summary><ol className="max-h-64 space-y-1 overflow-y-auto text-xs">{rows.map(row => <li key={row.key} className="break-all">{row.status}: {row.key}{row.id ? ` / ${row.id}` : ""}</li>)}</ol></details> : null}
    </CardContent>
  </Card>;
}
