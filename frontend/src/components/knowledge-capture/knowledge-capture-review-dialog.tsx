"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Loader2, Send, Sparkles, X } from "lucide-react";
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
import { withBasePath } from "@/lib/base-path";

type Props = {
  open: boolean;
  notificationId: string | null;
  target: KnowledgeCaptureNotificationTarget | null;
  candidate: KnowledgeCaptureCandidate | null;
  readOnly?: boolean;
  canPublish?: boolean;
  onOpenChange(open: boolean): void;
  onDidOpen(notificationId: string): void;
  onResolved(): void;
};

type PendingAction = "answer" | "review" | "dismiss" | "publish" | null;

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
  questionId?: string,
): string {
  return questionId
    ? `${candidateUrl(target)}/questions/${encodeURIComponent(questionId)}/review`
    : `${candidateUrl(target)}/review`;
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
    case "published":
      return "保存済み";
    case "dismissed":
    case "discarded":
      return "見送り済み";
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

export function KnowledgeCaptureReviewDialog(props: Props) {
  const { candidate, target, open } = props;
  return (
    <KnowledgeCaptureReviewSession
      key={`${target?.projectId}:${target?.candidateId}:${candidate?.id}:${candidate?.version}:${open}`}
      {...props}
    />
  );
}

function KnowledgeCaptureReviewSession({
  open,
  notificationId,
  target,
  candidate,
  readOnly = false,
  canPublish,
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
  const [publishPermission, setPublishPermission] = useState<"checking" | "allowed" | "denied" | "unavailable">("checking");
  const mayPublish = canPublish ?? publishPermission === "allowed";
  const requestEpochRef = useRef(0);
  const pendingRef = useRef(false);

  useEffect(() => () => { requestEpochRef.current += 1; }, []);

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

  useEffect(() => {
    if (!open || readOnly || canPublish !== undefined || !target || currentCandidate?.status !== "draft_ready") return;
    let active = true;
    const controller = new AbortController();
    // This existing read-only endpoint requires the same manage_settings
    // permission as publication. Unknown permissions never enable adoption.
    void fetch(`/api/projects/${encodeURIComponent(target.projectId)}/knowledge-capture/settings`, {
      credentials: "include", cache: "no-store", signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) {
        if (active) setPublishPermission(response.status === 403 ? "denied" : "unavailable");
        return;
      }
      const body: unknown = await response.json();
      const matchesProject = !!body && typeof body === "object" &&
        "project_id" in body && typeof body.project_id === "string" &&
        body.project_id.toLowerCase() === target.projectId.toLowerCase();
      if (active) setPublishPermission(matchesProject ? "allowed" : "unavailable");
    }).catch(() => { if (active) setPublishPermission("unavailable"); });
    return () => { active = false; controller.abort(); };
  }, [canPublish, currentCandidate?.status, open, readOnly, target]);

  const isCurrentRequest = (epoch: number) => requestEpochRef.current === epoch;

  const beginAction = (action: Exclude<PendingAction, null>): number | null => {
    if (
      readOnly || !open || !target || !currentCandidate || pendingRef.current ||
      currentCandidate.id !== target.candidateId ||
      currentCandidate.projectId?.toLowerCase() !== target.projectId.toLowerCase()
    ) return null;
    pendingRef.current = true;
    setPendingAction(action);
    setError(null);
    return requestEpochRef.current;
  };

  const endAction = (epoch: number) => {
    if (!isCurrentRequest(epoch)) return;
    pendingRef.current = false;
    setPendingAction(null);
  };

  const validCandidate = (next: KnowledgeCaptureCandidate | null) => Boolean(
    next && target && next.id === target.candidateId &&
    next.projectId?.toLowerCase() === target.projectId.toLowerCase() && next.version > 0,
  );

  const reloadCandidate = async (epoch: number) => {
    if (!target) return;
    const response = await fetch(candidateUrl(target), {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) {
      throw await responseError(response, "ナレッジ候補を再読み込みできませんでした。");
    }
    const next = normalizeKnowledgeCaptureCandidateDetail(await response.json());
    if (!isCurrentRequest(epoch)) return;
    if (!validCandidate(next)) throw new Error("ナレッジ候補の詳細が不正です。");
    setCurrentCandidate(next);
    setComposer("");
    setReviewResponse(null);
    setThread([]);
    setError("別の操作で更新されました。最新状態を読み込みました。内容を確認してから操作してください。");
  };

  const mutateCandidate = async (
    action: "answer" | "dismiss" | "publish",
    url: string,
    payload: Record<string, unknown>,
    fallback: string,
  ) => {
    const epoch = beginAction(action);
    if (epoch === null || !currentCandidate) return;
    try {
      const response = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_candidate_version: currentCandidate.version,
          ...payload,
        }),
      });
      if (!isCurrentRequest(epoch)) return;
      if (response.status === 409) {
        await reloadCandidate(epoch);
        return;
      }
      if (!response.ok) {
        if (action === "publish" && response.status === 403) {
          throw new Error("Docsへの採用・保存にはプロジェクトの管理権限が必要です。管理者に確認してください。");
        }
        throw await responseError(response, fallback);
      }
      const next = normalizeKnowledgeCaptureCandidateDetail(await response.json());
      if (!isCurrentRequest(epoch)) return;
      if (
        !validCandidate(next) || !next || next.version <= currentCandidate.version ||
        (action === "publish" && next.status !== "published") ||
        (action === "dismiss" && next.status !== "dismissed" && next.status !== "discarded")
      ) throw new Error("操作後の候補データが不正です。最新状態を開き直してください。");
      setCurrentCandidate(next);
      onResolved();
      onOpenChange(false);
    } catch (cause) {
      if (isCurrentRequest(epoch)) setError(cause instanceof Error ? cause.message : fallback);
    } finally {
      endAction(epoch);
    }
  };

  const answer = async (optionId?: string, text?: string) => {
    if (!target || currentCandidate?.status !== "needs_user" || !currentCandidate.question) return;
    const question = currentCandidate.question;
    if (optionId && !question.options.some((option) => option.id === optionId)) {
      setError("選択肢を確認できませんでした。");
      return;
    }
    const answerText = text?.trim() || "";
    if (!optionId && (!answerText || !question.allowFreeText)) return;
    await mutateCandidate("answer", answerUrl(target, question.id), {
      ...(optionId ? { option_id: optionId } : {}),
      ...(answerText ? { text: answerText.slice(0, 2000) } : {}),
    }, "回答を保存できませんでした。");
  };

  const askAi = async () => {
    if (!target || !currentCandidate ||
      !(currentCandidate.status === "draft_ready" ||
        (currentCandidate.status === "needs_user" && currentCandidate.question))) return;
    const text = composer.trim();
    if (!text) return;
    const epoch = beginAction("review");
    if (epoch === null) return;
    const userExchange: KnowledgeCaptureReviewExchange = { role: "user", text: text.slice(0, 2000) };
    const nextThread = [...thread, userExchange].slice(-8);
    try {
      const response = await fetch(reviewUrl(target,
        currentCandidate.status === "needs_user" ? currentCandidate.question?.id : undefined), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_candidate_version: currentCandidate.version,
          text: userExchange.text,
          thread: nextThread,
        }),
      });
      if (!isCurrentRequest(epoch)) return;
      if (response.status === 409) {
        await reloadCandidate(epoch);
        return;
      }
      if (!response.ok) throw await responseError(response, "AIへの聞き返しに失敗しました。");
      const parsed = normalizeReviewResponse(await response.json(), currentCandidate.version);
      if (!isCurrentRequest(epoch)) return;
      if (!parsed) throw new Error("AIの回答形式を確認できませんでした。");
      setReviewResponse(parsed);
      setThread([...nextThread, { role: "assistant" as const, text: parsed.reply }].slice(-8));
      setComposer("");
    } catch (cause) {
      if (isCurrentRequest(epoch)) setError(cause instanceof Error ? cause.message : "AIへの聞き返しに失敗しました。");
    } finally {
      endAction(epoch);
    }
  };

  const dismiss = async () => {
    if (!target || !currentCandidate || !["needs_user", "draft_ready"].includes(currentCandidate.status)) return;
    await mutateCandidate("dismiss", dismissUrl(target), {}, "候補を保存しない設定にできませんでした。");
  };

  const publish = async () => {
    if (!mayPublish || !target || currentCandidate?.status !== "draft_ready" || !currentCandidate.draft) return;
    await mutateCandidate("publish", `${candidateUrl(target)}/publish`, {}, "Docsへ保存できませんでした。");
  };

  const context = currentCandidate?.reviewContext;
  const question = currentCandidate?.status === "needs_user" ? currentCandidate.question : null;
  const isDraft = currentCandidate?.status === "draft_ready";
  const canReview = Boolean(question || isDraft);
  const canAskAi = Boolean(!readOnly && canReview && composer.trim() && !pendingAction);
  const canAnswerWithText = Boolean(
    !readOnly && question?.allowFreeText && composer.trim() && !pendingAction,
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
            内容と根拠を確認し、採用するか見送るかを選んでください。迷う点はAIに質問できます。
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
                    href={withBasePath(taskHref(currentCandidate.seedTask.id))}
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
                        href={withBasePath(chatHref(session.id))}
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
                    href={withBasePath(docsHref(context.publicationTarget.nodeId))}
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
                {context?.reviewReason || (isDraft
                  ? "下書きができました。内容を確認して、採用する場合は「採用してDocsへ保存」を押してください。"
                  : question ? "下の質問への回答を確認してから、候補を再整理します。"
                  : "候補の状態が変わっています。最新の状態を確認してください。") }
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
                          disabled={readOnly || pendingAction !== null}
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
              </section>
            ) : null}

            {currentCandidate.draft ? (
              <section className="space-y-3 rounded-md border p-3" data-testid="knowledge-capture-draft-preview">
                <h3 className="text-sm font-semibold">候補の下書き</h3>
                <p className="break-words text-sm font-medium">{currentCandidate.draft.title || "タイトル未設定"}</p>
                {currentCandidate.draft.sections.map((section, index) => (
                  <article key={`${section.key || section.title}-${index}`} className="space-y-1 border-t pt-3">
                    {section.title ? <h4 className="text-sm font-medium">{section.title}</h4> : null}
                    <p className="whitespace-pre-wrap break-words text-sm">{section.body}</p>
                  </article>
                ))}
                {currentCandidate.draft.evidenceSummary ? (
                  <p className="whitespace-pre-wrap break-words border-t pt-3 text-xs text-muted-foreground">
                    根拠の要約: {currentCandidate.draft.evidenceSummary}
                  </p>
                ) : null}
                {!currentCandidate.draft.sections.length ? (
                  <p className="text-sm text-muted-foreground">下書きの本文を確認できません。採用前にAIへの確認や再読み込みを行ってください。</p>
                ) : null}
              </section>
            ) : null}

            {canReview ? (
              <label className="block text-sm">
                <span className="font-medium">{isDraft ? "AIへの質問・補足" : "回答・AIへの質問"}</span>
                <Textarea
                  value={composer}
                  onChange={(event) => setComposer(event.target.value)}
                  placeholder={isDraft ? "例: 何が再利用できますか？ この補足を踏まえて採用すべきか教えて" : "確認したい内容を入力"}
                  className="mt-1 min-h-24"
                  maxLength={2000}
                  disabled={readOnly || pendingAction !== null}
                  data-testid="knowledge-capture-review-composer"
                />
                <span className="mt-1 block text-xs text-muted-foreground">
                  AIへの質問・補足だけでは、下書きの内容や採用状態は変わりません。
                  {question && !question.allowFreeText ? " 回答は上の選択肢から選んでください。" : ""}
                </span>
              </label>
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

            {readOnly ? <p className="text-xs text-muted-foreground">閲覧のみの権限です。採用・回答・見送りは権限を持つメンバーに依頼してください。</p> : isDraft && !mayPublish ? <p className="text-xs text-muted-foreground">{canPublish === undefined && publishPermission === "checking" ? "採用に必要な管理権限を確認しています。" : canPublish === undefined && publishPermission === "unavailable" ? "採用権限を確認できませんでした。いったん閉じて開き直してください。" : "Docsへの採用・保存にはプロジェクトの管理権限が必要です。"}</p> : null}
            <DialogFooter className="sticky bottom-0 -mx-0 -mb-0 z-10 flex-wrap rounded-none border-t bg-card p-0 py-3">
              {isDraft ? (
                <Button type="button" onClick={() => void publish()}
                  disabled={readOnly || !mayPublish || pendingAction !== null || !currentCandidate.draft?.sections.some((section) => section.body.trim())}
                  data-testid="knowledge-capture-publish-button">
                  {pendingAction === "publish" ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Check className="mr-2 size-4" />}
                  採用してDocsへ保存
                </Button>
              ) : null}
              {question?.allowFreeText ? (
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
                </>
              ) : null}
              {canReview ? (
                <>
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
              {canReview ? <Button
                type="button"
                variant="ghost"
                onClick={() => void dismiss()}
                disabled={readOnly || pendingAction !== null}
                data-testid="knowledge-capture-dismiss-button"
              >
                {pendingAction === "dismiss" ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <X className="mr-2 size-4" />
                )}
                今回は保存しない
              </Button> : null}
            </DialogFooter>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
