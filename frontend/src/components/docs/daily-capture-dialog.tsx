"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, Loader2, RefreshCw, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { withBasePath } from "@/lib/base-path";
import {
  dailyCaptureApi, dailyCaptureDay, OPEN_DAILY_CAPTURE_EVENT,
  type DailyCapture, type DailyCaptureHistory, type DailyCaptureSubmission,
} from "@/lib/daily-capture";

const STATUS_LABELS: Record<DailyCapture["status"], string> = {
  queued: "保存済み・整理待ち",
  running: "保存済み・整理中",
  succeeded: "整理済み",
  failed: "保存済み・整理に失敗",
  undone: "整理を元に戻しました",
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/** Kept outside route children: opening/closing never unmounts another editor. */
export function DailyCaptureDialog() {
  const [open, setOpen] = useState(false);
  const [day, setDay] = useState(dailyCaptureDay);
  const [draft, setDraft] = useState("");
  const [history, setHistory] = useState<DailyCaptureHistory | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedMessage, setSavedMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [unconfirmedSave, setUnconfirmedSave] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const draftRef = useRef(draft);
  const pendingRef = useRef<DailyCaptureSubmission | null>(null);
  const savingRef = useRef(false);
  const actionRef = useRef(false);
  const currentDayRef = useRef(day);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const mountedRef = useRef(true);

  const loadHistory = useCallback(async (requestedDay: string) => {
    const generation = ++generationRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    try {
      const result = await dailyCaptureApi.history(requestedDay, controller.signal);
      if (!mountedRef.current || controller.signal.aborted || generation !== generationRef.current) return;
      // Never display another day's or a mixed library's records from a stale response.
      if (result.day !== requestedDay || result.items.some((item) => item.day !== requestedDay || item.docs_library_id !== result.docs_library_id)) {
        throw new Error("保存履歴の保存先を確認できませんでした");
      }
      setHistory(result);
      setHistoryError(null);
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted || generation !== generationRef.current) return;
      setHistoryError(errorMessage(error, "保存履歴を読み込めませんでした"));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      if (mountedRef.current && generation === generationRef.current) setLoading(false);
    }
  }, []);

  const openToday = useCallback(() => {
    if (!open) returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const today = dailyCaptureDay();
    if (today !== currentDayRef.current) setHistory(null);
    currentDayRef.current = today;
    setDay(today);
    setOpen(true);
    void loadHistory(today);
    // Repeated invocation focuses the existing input instead of toggling or resetting it.
    textareaRef.current?.focus();
  }, [loadHistory, open]);

  useEffect(() => {
    window.addEventListener(OPEN_DAILY_CAPTURE_EVENT, openToday);
    return () => window.removeEventListener(OPEN_DAILY_CAPTURE_EVENT, openToday);
  }, [openToday]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const checkDate = () => {
      const today = dailyCaptureDay();
      if (today !== currentDayRef.current) {
        currentDayRef.current = today;
        setDay(today);
        setHistory(null);
        void loadHistory(today);
      }
    };
    const timer = window.setInterval(checkDate, 1000);
    window.addEventListener("focus", checkDate);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", checkDate); };
  }, [loadHistory, open]);

  const hasPendingJobs = history?.items.some((item) => item.status === "queued" || item.status === "running") ?? false;
  useEffect(() => {
    if (!open || !hasPendingJobs) return;
    const timer = window.setInterval(() => {
      // Let slow reads finish; only explicit reloads or mutations replace them.
      if (!document.hidden && !savingRef.current && !actionRef.current && !abortRef.current) void loadHistory(currentDayRef.current);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [hasPendingJobs, loadHistory, open]);

  useEffect(() => {
    const protectDraft = (event: BeforeUnloadEvent) => {
      if (!draftRef.current.trim() && !pendingRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protectDraft);
    return () => window.removeEventListener("beforeunload", protectDraft);
  }, []);

  const mergeCapture = (capture: DailyCapture) => {
    generationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    if (capture.day !== currentDayRef.current) return;
    setHistory((current) => ({
      docs_library_id: capture.docs_library_id,
      library_name: current?.library_name || "Personal Library",
      day: capture.day,
      day_node_id: capture.day_node_id,
      items: [capture, ...(current?.items ?? []).filter((item) => item.id !== capture.id && item.docs_library_id === capture.docs_library_id && item.day === capture.day)]
        .sort((left, right) => right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id)),
    }));
  };

  const save = async () => {
    if (savingRef.current || (!pendingRef.current && !draftRef.current.trim())) return;
    const today = dailyCaptureDay();
    if (today !== currentDayRef.current) {
      currentDayRef.current = today;
      setDay(today);
      setHistory(null);
    }
    // Keep the exact request on transport failure. Retry cannot duplicate or move
    // an uncertain save across midnight, even if the server already committed it.
    const submission = pendingRef.current ?? {
      content: draftRef.current,
      day: today,
      idempotency_key: crypto.randomUUID(),
      ...(history?.docs_library_id ? { docs_library_id: history.docs_library_id } : {}),
    };
    pendingRef.current = submission;
    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    setSavedMessage(null);
    try {
      const saved = await dailyCaptureApi.save(submission);
      if (!mountedRef.current) return;
      pendingRef.current = null;
      setUnconfirmedSave(false);
      draftRef.current = "";
      setDraft("");
      mergeCapture(saved);
      setSavedMessage(`${saved.day}に原文を保存しました。整理はバックグラウンドで進みます`);
      textareaRef.current?.focus();
    } catch (error) {
      if (!mountedRef.current) return;
      const status = (error as { status?: number } | null)?.status;
      // Definite client rejections did not save; keep the text editable. All
      // ambiguous failures retain the same payload and key for a safe retry.
      const rejected = typeof status === "number" && status >= 400 && status < 500 && status !== 408;
      if (rejected) pendingRef.current = null;
      setUnconfirmedSave(!rejected);
      setSaveError(errorMessage(error, "保存を確認できませんでした。入力は残っています"));
    } finally {
      savingRef.current = false;
      if (mountedRef.current) setSaving(false);
    }
  };

  const runAction = async (item: DailyCapture, action: "retry" | "undo") => {
    if (actionRef.current) return;
    actionRef.current = true;
    setBusyId(item.id);
    setActionError(null);
    try {
      const result = await dailyCaptureApi[action](item.id);
      if (mountedRef.current) mergeCapture(result);
    } catch (error) {
      if (mountedRef.current) {
        setActionError(errorMessage(error, "整理の更新に失敗しました。原文は保存済みです"));
        void loadHistory(currentDayRef.current);
      }
    } finally {
      actionRef.current = false;
      if (mountedRef.current) setBusyId(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent data-daily-capture="true" size="2xl" className="max-h-[90dvh] overflow-y-auto" initialFocus={textareaRef} finalFocus={returnFocusRef}>
        <DialogHeader>
          <DialogTitle>Today · 今日のメモ</DialogTitle>
          <DialogDescription>
            {day}（日本時間） · 保存先: {history?.library_name || "Personal Library"}（自分のライブラリ）
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <label className="sr-only" htmlFor="daily-capture-input">今日のメモに追記</label>
          <Textarea
            id="daily-capture-input"
            ref={textareaRef}
            value={draft}
            readOnly={saving || unconfirmedSave}
            maxLength={100000}
            placeholder="思いついたことを、そのまま書く…"
            className="min-h-32 resize-y"
            onChange={(event) => { draftRef.current = event.target.value; setDraft(event.target.value); setSavedMessage(null); }}
          />
          <p className="text-xs text-muted-foreground">原文を先に保存し、同じライブラリにあるタグと関連ノートを後から追加します</p>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-muted-foreground">下書きは閉じても残ります</span>
            <Button disabled={saving || (!draft.trim() && !unconfirmedSave)} onClick={() => void save()}>
              {saving && <Loader2 className="mr-1 size-4 animate-spin" />}
              {saving ? "原文を保存中…" : unconfirmedSave ? "保存を再試行" : "今日のメモに保存"}
            </Button>
          </div>
          {savedMessage && <p role="status" className="text-sm text-emerald-600 dark:text-emerald-400">{savedMessage}</p>}
          {saveError && <div role="alert" className="text-sm text-destructive"><p>{saveError}</p>{unconfirmedSave && <p>保存結果を確認できません。入力を保持しています。「保存を再試行」で重複せずに確認できます</p>}</div>}
        </div>
        <section aria-label="今日の保存履歴" className="space-y-3 border-t pt-3">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="mr-auto text-sm font-medium">今日の保存履歴</h2>
            {history?.day_node_id && <a href={withBasePath(`/docs/${encodeURIComponent(history.day_node_id)}`)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-primary underline-offset-4 hover:underline">今日のノート全体を開く（新しいタブ）<ExternalLink className="size-3" /></a>}
            <Button size="icon-sm" variant="ghost" aria-label="保存履歴を更新" disabled={loading} onClick={() => void loadHistory(day)}><RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} /></Button>
          </div>
          {historyError && <p role="alert" className="text-sm text-destructive">{historyError}。原文の保存は試せます</p>}
          {actionError && <p role="alert" className="text-sm text-destructive">{actionError}</p>}
          {loading && !history && <p role="status" className="text-sm text-muted-foreground">保存履歴を読み込み中…</p>}
          {!loading && history?.items.length === 0 && <p className="text-sm text-muted-foreground">今日の追記はまだありません。最初のメモから始めましょう</p>}
          <div className="space-y-3">
            {history?.items.map((item) => (
              <article key={item.id} className="space-y-2 rounded-lg border p-3" aria-label={`保存したメモ ${item.id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span role="status" className="text-xs font-medium">{STATUS_LABELS[item.status]}</span>
                  <time dateTime={item.created_at} className="text-xs text-muted-foreground">{new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit" }).format(new Date(item.created_at))}</time>
                  <a href={withBasePath(`/docs/${encodeURIComponent(item.node_id)}`)} target="_blank" rel="noopener noreferrer" className="ml-auto text-xs text-muted-foreground hover:underline">原文を開く<ExternalLink className="ml-1 inline size-3" /></a>
                </div>
                <pre className="max-h-44 overflow-y-auto whitespace-pre-wrap break-words font-sans text-sm">{item.content}</pre>
                {item.changes.length > 0 && <ul className="space-y-1 text-xs text-muted-foreground">{item.changes.map((change, index) => <li key={`${change.kind}:${change.target_id}:${index}`}>{change.removed ? "取り消し済み: " : "追加: "}{change.kind === "tag" ? "タグ " : "関連ノート "}{change.label}</li>)}</ul>}
                {item.status === "succeeded" && item.changes.length === 0 && <p className="text-xs text-muted-foreground">追加できる既存のタグ・関連ノートはありませんでした</p>}
                {item.error_message && item.status === "failed" && <p className="text-xs text-destructive">{item.error_message}。原文は保存済みです</p>}
                {item.undo_skipped_count > 0 && <p className="text-xs text-muted-foreground">後から変更された関連付け {item.undo_skipped_count} 件は残しました</p>}
                {item.status === "failed" && <Button size="sm" variant="outline" disabled={busyId !== null} onClick={() => void runAction(item, "retry")}><RefreshCw className={`mr-1 size-3 ${busyId === item.id ? "animate-spin" : ""}`} />整理を再試行</Button>}
                {item.status === "succeeded" && item.changes.some((change) => !change.removed) && <Button size="sm" variant="ghost" disabled={busyId !== null} onClick={() => void runAction(item, "undo")}><Undo2 className="mr-1 size-3" />この整理を元に戻す</Button>}
              </article>
            ))}
          </div>
        </section>
      </DialogContent>
    </Dialog>
  );
}
