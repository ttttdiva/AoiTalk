"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PersonalDraftPanel } from "./personal-draft-panel";
import { PersonalReminderPanel } from "./personal-reminder-panel";
import {
  addWrite,
  completeWrite,
  isAmbiguousWriteFailure,
  ledgerApi,
  verifyWrite,
  type LedgerDeadline,
  type LedgerEntry,
  type LedgerState,
  type LedgerWrite,
} from "./personal-ledger-api";

// 入力元（誰が入れたか）。確認状態（本人が確認したか）とは別の情報。
const ORIGIN_LABEL: Record<string, string> = {
  agent_observation: "Agentの観測",
  user_entered: "本人入力",
  user_confirmed: "本人が確認",
};
const TYPE_LABEL: Record<string, string> = {
  deadline_candidate: "期限（候補）",
  deadline_verification: "期限の確認",
  deadline_completion: "完了",
  opening_balance: "初期残数",
  purchase: "購入",
  consumption: "消費",
  void: "取消",
};
const SUBJECT_HELP = "半角英小文字・数字・「-」「_」で1〜64文字。先頭は英小文字か数字にしてください。";

// ambiguousSeen: いずれかの試行で結果が不明になった。以後は、同じキーでの成功か、利用者の明示的な
// 破棄まで、送信内容と冪等キーを保持する（再送が認証・レート制限などで拒否されても消さない）。
// この保留はメモリ上だけで、画面の再読み込みや閉じる操作で失われる。
type PendingWrite = { write: LedgerWrite; done: string; ambiguousSeen?: boolean };

function describeReviewReason(reason: string): string {
  switch (reason) {
    case "multiple_effective_candidates":
      return "期限候補が複数あります。履歴と元の書類で正しい日付を確認してください。";
    case "negative_balance":
      return "在庫がマイナスです。初期残数・購入・消費の記録を確認し、誤りを訂正してください。";
    default:
      return "確認が必要な記録があります。履歴の内容を確認してください。";
  }
}

function describePayload(entry: LedgerEntry): string {
  const p = entry.payload as Record<string, unknown>;
  if (entry.entry_type === "deadline_candidate") return `${p.due_date}${p.label ? `（${p.label}）` : ""}`;
  if (entry.entry_type === "deadline_completion") return `完了日 ${p.completed_on}`;
  if (entry.entry_type === "deadline_verification") return "表示された内容を確認";
  if (entry.entry_type === "void") return String(p.reason ?? "");
  return `${p.quantity} ${p.unit}（${p.occurred_on}）${p.note ? ` ${p.note}` : ""}`;
}

export function PersonalLedgerPanel() {
  const [state, setState] = useState<LedgerState | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  // 結果が不明な書き込み。送信内容と冪等キーを固定したまま保持し、同じものを再送する。
  const [pending, setPending] = useState<PendingWrite | null>(null);
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);
  // 書き込みは成功したが一覧の更新に失敗した状態。再送ではなく再読み込みだけを促す。
  const [stale, setStale] = useState(false);

  const [dlSubject, setDlSubject] = useState("");
  const [dlDate, setDlDate] = useState("");
  const [dlLabel, setDlLabel] = useState("");
  const [invSubject, setInvSubject] = useState("");
  const [invType, setInvType] = useState<"opening_balance" | "purchase" | "consumption">("purchase");
  const [invQty, setInvQty] = useState("");
  const [invUnit, setInvUnit] = useState("pcs");
  const [invDate, setInvDate] = useState("");
  const [fixing, setFixing] = useState<{ entry: LedgerEntry; qty: string; date: string } | null>(null);
  const [newDates, setNewDates] = useState<Record<string, string>>({});
  const reloadGeneration = useRef(0);

  const reload = useCallback(async () => {
    const generation = ++reloadGeneration.current;
    try {
      const [nextState, firstPage] = await Promise.all([ledgerApi.state(), ledgerApi.entries()]);
      if (generation !== reloadGeneration.current) return false;
      setState(nextState);
      setEntries(firstPage.entries);
      setNextCursor(firstPage.next_before_seq ?? null);
      return true;
    } catch (error) {
      if (generation !== reloadGeneration.current) return false;
      throw error;
    }
  }, []);

  async function refresh(prefix = "") {
    try {
      if (!await reload()) return;
      setStale(false);
      if (prefix) setMessage(prefix);
    } catch (error) {
      setStale(true);
      const reason = error instanceof Error ? error.message : "一覧を取得できませんでした";
      setMessage(`${prefix ? `${prefix}。ただし` : ""}一覧の更新に失敗しました（${reason}）。書き込みは再送されません。「再読み込み」を押してください`);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadMore() {
    if (nextCursor == null) return;
    const generation = reloadGeneration.current;
    setBusy(true);
    try {
      const page = await ledgerApi.entries(nextCursor);
      if (generation !== reloadGeneration.current) return;
      setEntries((current) => [...current, ...page.entries]);
      setNextCursor(page.next_before_seq ?? null);
    } catch (error) {
      if (generation === reloadGeneration.current) setMessage(error instanceof Error ? error.message : "履歴の続きを取得できませんでした");
    } finally {
      setBusy(false);
    }
  }

  /** 固定済みの要求をそのまま送る。成功と一覧更新の失敗は別々に扱う。 */
  async function submit(request: PendingWrite) {
    setBusy(true);
    setMessage("");
    try {
      await ledgerApi.send(request.write);
    } catch (error) {
      if (isAmbiguousWriteFailure(error)) {
        setPending({ ...request, ambiguousSeen: true });
        setMessage("通信結果が不明です。サーバー側で保存済みの可能性があります。「同じ内容で再送」を押すと、同じ操作として重複なく確定します");
      } else if (request.ambiguousSeen) {
        // 最初の送信が保存済みかもしれないので、認証切れ・レート制限などの拒否でも内容とキーを捨てない。
        setPending({ ...request, ambiguousSeen: true });
        const reason = error instanceof Error ? error.message : "拒否されました";
        setMessage(`再送が拒否されました（${reason}）。最初の送信が保存済みの可能性があるため、送信内容は保持しています。原因を解消してから「同じ内容で再送」を押してください`);
      } else {
        setPending(null);
        setMessage(error instanceof Error ? error.message : "操作を完了できませんでした");
      }
      setBusy(false);
      return;
    }
    setPending(null);
    setConfirmingDiscard(false);
    await refresh(request.done);
    setBusy(false);
  }

  function write(write: LedgerWrite, done: string) {
    return submit({ write, done });
  }

  const locked = busy || pending !== null;

  function addDeadline(supersedes?: string, subject = dlSubject, date = dlDate, label = dlLabel) {
    return write(
      addWrite({
        ledger: "deadline",
        entry_type: "deadline_candidate",
        subject_key: subject.trim(),
        payload: { due_date: date, ...(label.trim() ? { label: label.trim() } : {}) },
        ...(supersedes ? { supersedes_entry_id: supersedes } : {}),
      }),
      supersedes ? "日付を訂正しました（確認は外れます）" : "期限を追加しました（未確認）",
    );
  }

  function addInventory() {
    return write(
      addWrite({
        ledger: "inventory",
        entry_type: invType,
        subject_key: invSubject.trim(),
        payload: { quantity: Number(invQty), unit: invUnit.trim(), occurred_on: invDate },
      }),
      "在庫の記録を追加しました",
    );
  }

  function correctInventory() {
    if (!fixing) return Promise.resolve();
    const { entry, qty, date } = fixing;
    const p = entry.payload as Record<string, unknown>;
    setFixing(null);
    return write(
      addWrite({
        ledger: "inventory",
        entry_type: entry.entry_type as "opening_balance" | "purchase" | "consumption",
        subject_key: entry.subject_key,
        payload: { quantity: Number(qty), unit: String(p.unit), occurred_on: date },
        supersedes_entry_id: entry.id,
      }),
      "訂正を追記しました（元の記録は履歴に残ります）",
    );
  }

  function voidEntry(target: { id: string; ledger: string; subject_key: string }, done: string) {
    return write(
      addWrite({
        ledger: target.ledger as "deadline" | "inventory",
        entry_type: "void",
        subject_key: target.subject_key,
        payload: {},
        supersedes_entry_id: target.id,
      }),
      done,
    );
  }

  const currentEntries = new Map(entries.map((entry) => [entry.id, entry]));

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-4 text-sm" data-testid="personal-ledger">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">個人台帳（本人専用）</h1>
        <p className="text-muted-foreground">
          この画面の内容は本人だけが見られます。共有Project・Task・Recordsには表示されません。
          日付はご自身で確認した値を入力してください。法令上の期限や更新間隔は自動計算しません。
        </p>
      </header>
      <PersonalDraftPanel onMaterialized={() => refresh()} />
      <PersonalReminderPanel />
      {message && (
        <div role="status" className="rounded border p-2">
          {message}
        </div>
      )}
      {pending && (
        <div role="alert" className="space-y-2 rounded border p-2" data-testid="pending-write">
          <div className="flex flex-wrap items-center gap-2">
            <span>結果が不明な操作があります。</span>
            <Button size="sm" disabled={busy} onClick={() => void submit(pending)}>同じ内容で再送</Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void refresh()}>再読み込みして確認</Button>
            {!confirmingDiscard && (
              <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirmingDiscard(true)}>破棄</Button>
            )}
          </div>
          <p className="text-muted-foreground">
            この保留はこの画面だけに保持され、ページを再読み込みするか閉じると失われます。失われた後に同じ操作をやり直すと、
            最初の送信が保存済みだった場合は重複する可能性があります。必要なら先に「再読み込みして確認」で反映済みか確かめてください。
          </p>
          {confirmingDiscard && (
            <div className="flex flex-wrap items-center gap-2" data-testid="discard-warning">
              <span>
                破棄しても、すでに保存された可能性のある書き込みは取り消されません。一覧で反映済みか確認してから破棄してください。
              </span>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => {
                  setPending(null);
                  setConfirmingDiscard(false);
                  setMessage("未確定の送信を破棄しました（保存済みの書き込みは取り消されていません）。反映済みかどうかは一覧で確認してください");
                }}
              >
                それでも破棄
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirmingDiscard(false)}>戻る</Button>
            </div>
          )}
        </div>
      )}
      {stale && !pending && (
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void refresh()}>再読み込み</Button>
        </div>
      )}

      <section aria-labelledby="ledger-deadlines" className="space-y-3">
        <h2 id="ledger-deadlines" className="font-medium">期限</h2>
        {state?.deadlines.length === 0 && <p className="text-muted-foreground">期限はまだありません。</p>}
        <ul className="space-y-2">
          {state?.deadlines.map((deadline: LedgerDeadline) => {
            const newDate = newDates[deadline.candidate_entry_id] ?? "";
            return (
              <li key={deadline.candidate_entry_id} className="space-y-2 rounded border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <strong>{deadline.subject_key}</strong>
                  <span>{deadline.due_date}</span>
                  {deadline.label && <span className="text-muted-foreground">{deadline.label}</span>}
                  <span className="rounded border px-1" data-testid="deadline-origin">
                    入力元: {ORIGIN_LABEL[deadline.candidate_origin]}
                  </span>
                  <span className="rounded border px-1" data-testid="deadline-status">
                    確認状態: {deadline.status === "verified" ? "確認済み" : "未確認"}
                  </span>
                  {deadline.completed_on && <span data-testid="deadline-completed">完了 {deadline.completed_on}</span>}
                  {deadline.needs_review.length > 0 && (
                    <span role="alert" className="text-destructive" data-testid="deadline-review">
                      要確認: {deadline.needs_review.map(describeReviewReason).join(" ")}
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {deadline.status === "unverified" && (
                    <Button
                      size="sm"
                      disabled={locked}
                      onClick={() =>
                        write(
                          verifyWrite(deadline.candidate_entry_id, deadline.confirm_digest),
                          "表示されている日付を確認済みにしました",
                        )
                      }
                    >
                      この日付を自分で確認した
                    </Button>
                  )}
                  <Input
                    type="date"
                    aria-label={`${deadline.subject_key} の新しい日付`}
                    className="w-40"
                    value={newDate}
                    onChange={(event) => setNewDates({ ...newDates, [deadline.candidate_entry_id]: event.target.value })}
                  />
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={locked || !newDate}
                    onClick={() => addDeadline(deadline.candidate_entry_id, deadline.subject_key, newDate, deadline.label ?? "")}
                  >
                    期限日を訂正
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={locked || !newDate}
                    onClick={() =>
                      write(
                        completeWrite(deadline.candidate_entry_id, newDate, deadline.completion_entry_id ?? undefined),
                        deadline.completion_entry_id ? "完了日を訂正しました" : "完了日を記録しました",
                      )
                    }
                  >
                    {deadline.completion_entry_id ? "完了日を訂正" : "選択した日付で完了を記録"}
                  </Button>
                  {deadline.completion_entry_id && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={locked}
                      onClick={() =>
                        voidEntry(
                          { id: deadline.completion_entry_id as string, ledger: "deadline", subject_key: deadline.subject_key },
                          "完了を取り消しました（元の記録は履歴に残ります）",
                        )
                      }
                    >
                      完了を取り消す
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
        <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => event.preventDefault()}>
          <label className="space-y-1">書類の識別子
            <Input aria-label="書類の識別子" aria-describedby="ledger-document-id-help" value={dlSubject} maxLength={64} onChange={(e) => setDlSubject(e.target.value)} />
          </label>
          <label className="space-y-1">期限日
            <Input aria-label="期限日" type="date" value={dlDate} onChange={(e) => setDlDate(e.target.value)} />
          </label>
          <label className="space-y-1">メモ
            <Input aria-label="メモ" value={dlLabel} maxLength={120} onChange={(e) => setDlLabel(e.target.value)} />
          </label>
          <Button type="button" size="sm" disabled={locked || !dlSubject.trim() || !dlDate} onClick={() => void addDeadline()}>
            期限を追加
          </Button>
        </form>
        <p id="ledger-document-id-help" className="text-muted-foreground">{SUBJECT_HELP}</p>
      </section>

      <section aria-labelledby="ledger-inventory" className="space-y-3">
        <h2 id="ledger-inventory" className="font-medium">在庫（購入・消費の記録）</h2>
        {state?.inventory.length === 0 && <p className="text-muted-foreground">在庫の記録はまだありません。</p>}
        <ul className="space-y-1">
          {state?.inventory.map((item) => (
            <li key={item.subject_key} className="rounded border p-2" data-testid="inventory-row">
              <strong>{item.subject_key}</strong>{" "}
              {item.balances.map((b) => `${b.balance} ${b.unit}`).join(" / ")}
              {item.needs_review.length > 0 && (
                <span role="alert" className="ml-2 text-destructive">要確認: {item.needs_review.map(describeReviewReason).join(" ")}</span>
              )}
            </li>
          ))}
        </ul>
        <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => event.preventDefault()}>
          <label className="space-y-1">品目の識別子
            <Input aria-label="品目の識別子" aria-describedby="ledger-item-id-help" value={invSubject} maxLength={64} onChange={(e) => setInvSubject(e.target.value)} />
          </label>
          <label className="space-y-1">種別
            <AppSelect aria-label="種別" value={invType} onChange={(e) => setInvType(e.target.value as typeof invType)}>
              <option value="opening_balance">初期残数</option>
              <option value="purchase">購入</option>
              <option value="consumption">消費</option>
            </AppSelect>
          </label>
          <label className="space-y-1">数量
            <Input aria-label="数量" type="number" min={invType === "opening_balance" ? 0 : 1} value={invQty} onChange={(e) => setInvQty(e.target.value)} />
          </label>
          <label className="space-y-1">単位
            <Input aria-label="単位" value={invUnit} maxLength={16} onChange={(e) => setInvUnit(e.target.value)} />
          </label>
          <label className="space-y-1">日付
            <Input aria-label="日付" type="date" value={invDate} onChange={(e) => setInvDate(e.target.value)} />
          </label>
          <Button type="button" size="sm" disabled={locked || !invSubject.trim() || invQty === "" || !invDate} onClick={() => void addInventory()}>
            記録を追加
          </Button>
        </form>
        <p id="ledger-item-id-help" className="text-muted-foreground">{SUBJECT_HELP}</p>
      </section>

      {fixing && (
        <section aria-label="記録の訂正" className="space-y-2 rounded border p-3">
          <p>
            {fixing.entry.subject_key} の「{TYPE_LABEL[fixing.entry.entry_type]}」を訂正します。元の記録は履歴に残ります。
          </p>
          <div className="flex flex-wrap items-end gap-2">
            <label className="space-y-1">訂正後の数量
              <Input aria-label="訂正後の数量" type="number" value={fixing.qty} onChange={(e) => setFixing({ ...fixing, qty: e.target.value })} />
            </label>
            <label className="space-y-1">訂正後の日付
              <Input aria-label="訂正後の日付" type="date" value={fixing.date} onChange={(e) => setFixing({ ...fixing, date: e.target.value })} />
            </label>
            <Button size="sm" disabled={locked || fixing.qty === "" || !fixing.date} onClick={() => void correctInventory()}>訂正を追記</Button>
            <Button size="sm" variant="outline" onClick={() => setFixing(null)}>やめる</Button>
          </div>
        </section>
      )}

      <section aria-labelledby="ledger-history" className="space-y-2">
        <h2 id="ledger-history" className="font-medium">履歴（新しい順・削除はできません）</h2>
        <ul className="space-y-1">
          {entries.map((entry) => {
            const payload = entry.payload as Record<string, unknown>;
            const correctable =
              !entry.superseded && ["opening_balance", "purchase", "consumption"].includes(entry.entry_type);
            return (
              <li
                key={entry.id}
                data-testid="history-row"
                className={`flex flex-wrap items-center gap-2 rounded border p-2 ${entry.superseded ? "opacity-60" : ""}`}
              >
                <span>#{entry.seq}</span>
                <span>{TYPE_LABEL[entry.entry_type]}</span>
                <strong>{entry.subject_key}</strong>
                <span>{describePayload(entry)}</span>
                <span className="rounded border px-1">{ORIGIN_LABEL[entry.origin]}</span>
                {entry.superseded && <span>（訂正・取消済み）</span>}
                {entry.supersedes_entry_id && currentEntries.has(entry.supersedes_entry_id) && (
                  <span>（#{currentEntries.get(entry.supersedes_entry_id)!.seq} を訂正）</span>
                )}
                {correctable && (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={locked}
                      onClick={() => setFixing({ entry, qty: String(payload.quantity ?? ""), date: String(payload.occurred_on ?? "") })}
                    >
                      訂正
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={locked}
                      onClick={() => void voidEntry(entry, "取消を追記しました（元の記録は履歴に残ります）")}
                    >
                      取消
                    </Button>
                  </>
                )}
              </li>
            );
          })}
        </ul>
        {nextCursor != null && (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void loadMore()}>
            さらに古い履歴を読み込む
          </Button>
        )}
      </section>
    </div>
  );
}
