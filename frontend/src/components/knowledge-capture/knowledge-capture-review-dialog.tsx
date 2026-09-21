"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Send, Sparkles, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  isUuid,
  normalizeKnowledgeCaptureCandidateDetail,
  type KnowledgeCaptureCandidate,
  type KnowledgeCaptureNotificationTarget,
  type KnowledgeCaptureReviewExchange,
  type KnowledgeCaptureReviewResponse,
} from "@/lib/knowledge-capture";

type Props = {
  open: boolean;
  notificationId: string | null;
  target: KnowledgeCaptureNotificationTarget | null;
  candidate: KnowledgeCaptureCandidate | null;
  onOpenChange(open: boolean): void;
  onDidOpen(notificationId: string): void;
  onResolved(): void;
};

type PendingAction = "answer" | "review" | "dismiss" | null;

function candidateUrl(target: KnowledgeCaptureNotificationTarget): string {
  return `/api/projects/${encodeURIComponent(target.projectId)}/knowledge-capture/candidates/${encodeURIComponent(target.candidateId)}`;
}

function answerUrl(
  target: KnowledgeCaptureNotificationTarget,
  questionId: string,
): string {
  return `${candidateUrl(target)}/questions/${encodeURIComponent(questionId)}/answer`;
}

function reviewUrl(
  target: KnowledgeCaptureNotificationTarget,
  questionId: string,
): string {
  return `${candidateUrl(target)}/questions/${encodeURIComponent(questionId)}/review`;
}

function dismissUrl(target: KnowledgeCaptureNotificationTarget): string {
  return `${candidateUrl(target)}/dismiss`;
}

function statusLabel(status: string): string {
  switch (status) {
    case "needs_user":
      return "回答待ち";
    case "draft_ready":
      return "Docs候補";
    default:
      return status;
  }
}

function taskHref(id: string): string {
  return `/tasks/${encodeURIComponent(id)}`;
}

function chatHref(id: string): string {
  return `/chat?s=${encodeURIComponent(id)}`;
}

function docsHref(id: string): string {
  return `/docs/${encodeURIComponent(id)}`;
}

function errorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return fallback;
}

async function responseError(response: Response, fallback: string): Promise<Error> {
  return new Error(
    errorDetail(await response.json().catch(() => null), fallback),
  );
}

function normalizeReviewResponse(
  body: unknown,
  expectedCandidateVersion: number,
): KnowledgeCaptureReviewResponse | null {
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const value = body as Record<string, unknown>;
  const action = value.action;
  if (
    action !== "keep_question" &&
    action !== "rephrase_question" &&
    action !== "discard_candidate"
  ) {
    return null;
  }
  const candidateVersion = value.candidate_version;
  const reply = typeof value.reply === "string" ? value.reply.trim() : "";
  if (
    typeof candidateVersion !== "number" ||
    !Number.isInteger(candidateVersion) ||
    candidateVersion !== expectedCandidateVersion ||
    !reply
  ) {
    return null;
  }
  const rawRephrasedQuestion = value.rephrased_question;
  const rephrasedQuestionValue =
    typeof rawRephrasedQuestion === "string"
      ? rawRephrasedQuestion.trim().slice(0, 2000) || null
      : rawRephrasedQuestion === null || rawRephrasedQuestion === undefined
        ? null
        : null;
  if (
    (action === "rephrase_question" && !rephrasedQuestionValue) ||
    (action !== "rephrase_question" && rawRephrasedQuestion !== null && rawRephrasedQuestion !== undefined)
  ) {
    return null;
  }
  return {
    candidate_version: candidateVersion,
    action,
    reply: reply.slice(0, 2000),
    rephrased_question: rephrasedQuestionValue,
  };
}

export function KnowledgeCaptureReviewDialog({
  open,
  notificationId,
  target,
  candidate,
  onOpenChange,
  onDidOpen,
  onResolved,
}: Props) {
  const [currentCandidate, setCurrentCandidate] =
    useState<KnowledgeCaptureCandidate | null>(candidate);
  const [composer, setComposer] = useState("");
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewResponse, setReviewResponse] =
    useState<KnowledgeCaptureReviewResponse | null>(null);
  const [thread, setThread] = useState<KnowledgeCaptureReviewExchange[]>([]);
  const didOpenKeyRef = useRef<string | null>(null);

  useEffect(() => {
    setCurrentCandidate(candidate);
    setComposer("");
    setError(null);
    setReviewResponse(null);
    setThread([]);
  }, [candidate]);

  useEffect(() => {
    if (!open) {
      didOpenKeyRef.current = null;
      return;
    }
    if (!notificationId || !candidate || didOpenKeyRef.current === notificationId) {
      return;
    }
    didOpenKeyRef.current = notificationId;
    onDidOpen(notificationId);
  }, [candidate, notificationId, onDidOpen, open]);

  const reloadCandidate = async (): Promise<KnowledgeCaptureCandidate> => {
    if (!target) throw new Error("ナレッジ候補を再読み込みできませんでした。");
    const response = await fetch(candidateUrl(target), {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) {
      throw await responseError(response, "ナレッジ候補を再読み込みできませんでした。");
    }
    const next = normalizeKnowledgeCaptureCandidateDetail(
      await response.json(),
    );
    if (
      !next ||
      next.id !== target.candidateId ||
      next.projectId?.toLowerCase() !== target.projectId.toLowerCase() ||
      next.version <= 0
    ) {
      throw new Error("ナレッジ候補の詳細が不正です。");
    }
    setCurrentCandidate(next);
    setComposer("");
    setReviewResponse(null);
    setThread([]);
    return next;
  };

  const finishResolved = (next: KnowledgeCaptureCandidate | null) => {
    if (next) setCurrentCandidate(next);
    onResolved();
    onOpenChange(false);
  };

  const answer = async (optionId?: string, text?: string) => {
    if (!target || !currentCandidate?.question || pendingAction) return;
    const question = currentCandidate.question;
    if (optionId && !question.options.some((option) => option.id === optionId)) {
      setError("選択肢を確認できませんでした。");
      return;
    }
    const answerText = text?.trim() || "";
    if (!optionId && !answerText) return;
    if (!optionId && !question.allowFreeText) {
      setError("この質問は自由入力に対応していません。");
      return;
    }

    setPendingAction("answer");
    setError(null);
    try {
      const response = await fetch(answerUrl(target, question.id), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_candidate_version: currentCandidate.version,
          ...(optionId ? { option_id: optionId } : {}),
          ...(answerText ? { text: answerText.slice(0, 4000) } : {}),
        }),
      });
      if (response.status === 409) {
        await reloadCandidate();
        setError("別の操作で更新されました。最新状態を読み込みました。");
        return;
      }
      if (!response.ok) {
        throw await responseError(response, "回答を保存できませんでした。");
      }
      const next = normalizeKnowledgeCaptureCandidateDetail(
        await response.json(),
      );
      if (
        !next ||
        next.id !== target.candidateId ||
        next.projectId?.toLowerCase() !== target.projectId.toLowerCase() ||
        next.version <= 0
      ) {
        throw new Error("回答後の候補データが不正です。");
      }
      finishResolved(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "回答を保存できませんでした。");
    } finally {
      setPendingAction(null);
    }
  };

  const askAi = async () => {
    if (!target || !currentCandidate?.question || pendingAction) return;
    const text = composer.trim();
    if (!text) return;
    const question = currentCandidate.question;
    const userExchange: KnowledgeCaptureReviewExchange = {
      role: "user",
      text: text.slice(0, 2000),
    };
    const nextThread = [...thread, userExchange].slice(-8);
    setPendingAction("review");
    setError(null);
    try {
      const response = await fetch(reviewUrl(target, question.id), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_candidate_version: currentCandidate.version,
          text: userExchange.text,
          thread: nextThread,
        }),
      });
      if (!response.ok) {
        if (response.status === 409) {
          await reloadCandidate();
          setError("別の操作で更新されました。最新状態を読み込みました。");
          return;
        }
        throw await responseError(response, "AIへの聞き返しに失敗しました。");
      }
      const parsed = normalizeReviewResponse(
        await response.json(),
        currentCandidate.version,
      );
      if (!parsed) throw new Error("AIの回答形式を確認できませんでした。");
      setReviewResponse(parsed);
      const assistantExchange: KnowledgeCaptureReviewExchange = {
        role: "assistant",
        text: parsed.reply,
      };
      setThread(
        [...nextThread, assistantExchange].slice(-8),
      );
      setComposer("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "AIへの聞き返しに失敗しました。");
    } finally {
      setPendingAction(null);
    }
  };

  const dismiss = async () => {
    if (!target || !currentCandidate || pendingAction) return;
    setPendingAction("dismiss");
    setError(null);
    try {
      const response = await fetch(dismissUrl(target), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_candidate_version: currentCandidate.version,
        }),
      });
      if (response.status === 409) {
        await reloadCandidate();
        setError("別の操作で更新されました。最新状態を読み込みました。");
        return;
      }
      if (!response.ok) {
        throw await responseError(response, "候補を保存しない設定にできませんでした。");
      }
      const next = normalizeKnowledgeCaptureCandidateDetail(
        await response.json(),
      );
      if (
        !next ||
        next.id !== target.candidateId ||
        next.projectId?.toLowerCase() !== target.projectId.toLowerCase() ||
        next.version <= 0
      ) {
        throw new Error("保存しない操作後の候補データが不正です。");
      }
      finishResolved(next);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "候補を保存しない設定にできませんでした.",
      );
    } finally {
      setPendingAction(null);
    }
  };

  const context = currentCandidate?.reviewContext;
  const question = currentCandidate?.question;
  const canAskAi = Boolean(question && composer.trim() && !pendingAction);
  const canAnswerWithText = Boolean(
    question?.allowFreeText && composer.trim() && !pendingAction,
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        size="2xl"
        className="max-h-[min(90vh,50rem)] overflow-y-auto"
        data-testid="knowledge-capture-review-dialog"
      >
        <DialogHeader>
          <div className="flex flex-wrap items-center gap-2 pr-8">
            <DialogTitle>解決ナレッジの確認</DialogTitle>
            {currentCandidate && (
              <Badge variant="secondary">{statusLabel(currentCandidate.status)}</Badge>
            )}
          </div>
          <DialogDescription>
            自動整理だけでは確定できない情報を確認して、保存方法を選んでください。
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
          >
            {error}
          </div>
        ) : null}

        {currentCandidate ? (
          <div className="space-y-4">
            <section className="grid gap-3 rounded-md border bg-muted/30 p-3 text-sm sm:grid-cols-3">
              <div className="min-w-0">
                <p className="text-xs text-muted-foreground">元のTask</p>
                {currentCandidate.seedTask && isUuid(currentCandidate.seedTask.id) ? (
                  <a
                    href={taskHref(currentCandidate.seedTask.id)}
                    target="_blank"
                    rel="noreferrer"
                    className="break-words font-medium underline"
                  >
                    {currentCandidate.seedTask.title || "Taskを開く"}
                  </a>
                ) : (
                  <span>元Task情報なし</span>
                )}
                {currentCandidate.seedTask?.status ? (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {currentCandidate.seedTask.status}
                  </p>
                ) : null}
              </div>
              <div className="min-w-0">
                <p className="text-xs text-muted-foreground">関連チャット</p>
                {context?.chatSessions.length ? (
                  <div className="space-y-1">
                    {context.chatSessions.map((session) => (
                      <a
                        key={session.id}
                        href={chatHref(session.id)}
                        target="_blank"
                        rel="noreferrer"
                        className="block break-words underline"
                      >
                        {session.title || "関連チャット"}
                      </a>
                    ))}
                  </div>
                ) : (
                  <span>関連チャットなし</span>
                )}
              </div>
              <div className="min-w-0">
                <p className="text-xs text-muted-foreground">保存先Docs</p>
                {context?.publicationTarget?.action === "update" &&
                context.publicationTarget.nodeId ? (
                  <a
                    href={docsHref(context.publicationTarget.nodeId)}
                    target="_blank"
                    rel="noreferrer"
                    className="break-words underline"
                  >
                    {context.publicationTarget.title || "追記予定Docs"}
                  </a>
                ) : context?.publicationTarget?.action === "create" ? (
                  <span>新規Docsとして作成予定</span>
                ) : context?.publicationTarget?.action === "no_change" ? (
                  <span>Docsの変更予定なし</span>
                ) : (
                  <span>保存先は未確定</span>
                )}
              </div>
            </section>

            <section className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
              <h3 className="text-sm font-semibold">なぜ確認が必要？</h3>
              <p className="whitespace-pre-wrap break-words text-sm">
                {context?.reviewReason || "人間にしか判断できない情報が残っています。"}
              </p>
            </section>

            {question ? (
              <section className="space-y-3">
                <div>
                  <h3 className="text-sm font-semibold">確認したいこと</h3>
                  <p className="mt-1 whitespace-pre-wrap break-words text-sm">
                    {question.prompt}
                  </p>
                </div>
                {question.options.length > 0 ? (
                  <fieldset className="space-y-2">
                    <legend className="text-xs text-muted-foreground">選択肢</legend>
                    <div className="flex flex-wrap gap-2">
                      {question.options.map((option) => (
                        <Button
                          key={option.id}
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => void answer(option.id)}
                          disabled={pendingAction !== null}
                          data-testid={`knowledge-capture-quick-option-${option.id}`}
                        >
                          {pendingAction === "answer" ? (
                            <Loader2 className="mr-2 size-3.5 animate-spin" />
                          ) : null}
                          {option.label}
                        </Button>
                      ))}
                    </div>
                  </fieldset>
                ) : null}
                {question.allowFreeText ? (
                  <label className="block text-sm">
                    <span className="text-xs text-muted-foreground">自由入力</span>
                    <Textarea
                      value={composer}
                      onChange={(event) => setComposer(event.target.value)}
                      placeholder="確認したい内容を入力"
                      className="mt-1 min-h-24"
                      maxLength={2000}
                      disabled={pendingAction !== null}
                      data-testid="knowledge-capture-review-composer"
                    />
                  </label>
                ) : null}
              </section>
            ) : currentCandidate.draft ? (
              <section className="space-y-2 rounded-md border p-3">
                <h3 className="text-sm font-semibold">候補の下書き</h3>
                <p className="break-words text-sm">{currentCandidate.draft.title || "タイトル未設定"}</p>
              </section>
            ) : null}

            {thread.length > 0 ? (
              <section className="space-y-2" aria-label="AIとの確認履歴">
                <h3 className="text-sm font-semibold">確認履歴</h3>
                <div className="space-y-2">
                  {thread.map((exchange, index) => (
                    <div
                      key={`${exchange.role}-${index}`}
                      className={`rounded-md border px-3 py-2 text-sm ${
                        exchange.role === "user" ? "bg-muted/30" : "bg-primary/5"
                      }`}
                    >
                      <p className="text-xs font-medium text-muted-foreground">
                        {exchange.role === "user" ? "あなた" : "AI"}
                      </p>
                      <p className="mt-1 whitespace-pre-wrap break-words">{exchange.text}</p>
                    </div>
                  ))}
                </div>
              </section>
            ) : null}

            {reviewResponse ? (
              <section
                className="space-y-2 rounded-md border border-sky-500/40 bg-sky-500/5 p-3"
                data-testid="knowledge-capture-review-response"
              >
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <Sparkles className="size-4" />
                  AIからの補足（保存状態は変わりません）
                </div>
                <p className="whitespace-pre-wrap break-words text-sm">
                  {reviewResponse.reply}
                </p>
                {reviewResponse.action === "rephrase_question" &&
                reviewResponse.rephrased_question ? (
                  <div className="rounded-md border border-border/70 bg-background/60 p-2 text-sm">
                    <p className="text-xs font-medium text-muted-foreground">AIからの言い換え案</p>
                    <p className="mt-1 whitespace-pre-wrap break-words">
                      {reviewResponse.rephrased_question}
                    </p>
                  </div>
                ) : null}
                {reviewResponse.action === "discard_candidate" ? (
                  <p className="text-xs text-muted-foreground">
                    AIは今回は保存しない判断も可能と提案しています。候補を閉じるには下のボタンを明示的に押してください。
                  </p>
                ) : null}
              </section>
            ) : null}

            <DialogFooter className="-mx-0 -mb-0 rounded-none border-t-0 bg-transparent p-0 pt-2">
              {question ? (
                <>
                  <Button
                    type="button"
                    onClick={() => void answer(undefined, composer)}
                    disabled={!canAnswerWithText}
                    data-testid="knowledge-capture-answer-button"
                  >
                    {pendingAction === "answer" ? (
                      <Loader2 className="mr-2 size-4 animate-spin" />
                    ) : (
                      <Send className="mr-2 size-4" />
                    )}
                    回答として送る
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => void askAi()}
                    disabled={!canAskAi}
                    data-testid="knowledge-capture-ask-ai-button"
                  >
                    {pendingAction === "review" ? (
                      <Loader2 className="mr-2 size-4 animate-spin" />
                    ) : (
                      <Sparkles className="mr-2 size-4" />
                    )}
                    AIに聞き返す
                  </Button>
                </>
              ) : null}
              <Button
                type="button"
                variant="ghost"
                onClick={() => void dismiss()}
                disabled={pendingAction !== null}
                data-testid="knowledge-capture-dismiss-button"
              >
                {pendingAction === "dismiss" ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <X className="mr-2 size-4" />
                )}
                今回は保存しない
              </Button>
            </DialogFooter>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
