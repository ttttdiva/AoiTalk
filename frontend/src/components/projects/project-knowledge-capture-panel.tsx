"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BookOpen,
  Check,
  FileText,
  HelpCircle,
  Loader2,
  RefreshCw,
  Save,
  Send,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  isUuid,
  normalizeKnowledgeCaptureCandidateDetail,
  normalizeKnowledgeCaptureCandidateList,
  type KnowledgeCaptureCandidate,
  type KnowledgeCaptureDraft,
} from "@/lib/knowledge-capture";

type ProjectKnowledgeCapturePanelProps = {
  projectId: string;
  canManageSettings: boolean;
};

type DraftEditState = {
  title: string;
  sections: Array<{ key?: string; title: string; body: string }>;
};

function errorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return fallback;
}

async function readError(response: Response, fallback: string): Promise<string> {
  return errorDetail(await response.json().catch(() => null), fallback);
}

function statusLabel(status: string): string {
  switch (status) {
    case "needs_user":
      return "回答待ち";
    case "draft_ready":
      return "Docs候補";
    case "published":
      return "公開済み";
    case "dismissed":
      return "保存しない";
    case "researching":
      return "整理中";
    default:
      return status;
  }
}

function candidateTitle(candidate: KnowledgeCaptureCandidate): string {
  return candidate.draft?.title || `ナレッジ候補 ${candidate.id.slice(0, 8)}`;
}

function draftToEditState(draft: KnowledgeCaptureDraft | null): DraftEditState {
  return {
    title: draft?.title ?? "",
    sections:
      draft?.sections.map((section) => ({ ...section })) ?? [],
  };
}

function actionUrl(projectId: string, candidateId: string, action: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/knowledge-capture/candidates/${encodeURIComponent(candidateId)}/${action}`;
}

function detailUrl(projectId: string, candidateId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/knowledge-capture/candidates/${encodeURIComponent(candidateId)}`;
}

function draftUrl(projectId: string, candidateId: string): string {
  return `${detailUrl(projectId, candidateId)}/draft`;
}

function answerUrl(projectId: string, candidateId: string, questionId: string): string {
  return `${detailUrl(projectId, candidateId)}/questions/${encodeURIComponent(questionId)}/answer`;
}

function draftFieldForSection(section: { key?: string; title: string }): string | null {
  if (section.key) return section.key;
  const labels: Record<string, string> = {
    課題: "problem",
    前提: "preconditions",
    症状: "symptoms",
    原因: "root_cause",
    解決: "resolution",
    手順: "procedure",
    確認: "verification",
    確認方法: "verification",
    注意点: "pitfalls",
    環境条件: "environment_constraints",
    既知の不確実性: "known_uncertainty",
  };
  return labels[section.title] ?? null;
}

function draftPatchFromEdit(draft: DraftEditState): Record<string, unknown> {
  const arrayFields = new Set([
    "preconditions",
    "symptoms",
    "environment_constraints",
    "known_uncertainty",
  ]);
  const procedureFields = new Set(["procedure", "verification", "pitfalls"]);
  const result: Record<string, unknown> = { title: draft.title };
  for (const section of draft.sections) {
    const field = draftFieldForSection(section);
    if (!field) continue;
    const lines = section.body
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (arrayFields.has(field)) result[field] = lines;
    else if (procedureFields.has(field)) {
      result[field] = lines.map((text, index) => ({ step: index + 1, text }));
    } else result[field] = section.body;
  }
  return result;
}

export function ProjectKnowledgeCapturePanel({
  projectId,
  canManageSettings,
}: ProjectKnowledgeCapturePanelProps) {
  const [items, setItems] = useState<KnowledgeCaptureCandidate[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedOptionId, setSelectedOptionId] = useState("");
  const [freeText, setFreeText] = useState("");
  const [editing, setEditing] = useState(false);
  const [draftEdit, setDraftEdit] = useState<DraftEditState>({
    title: "",
    sections: [],
  });

  const load = useCallback(
    async (requestedCandidateId: string | null = null) => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(
          `/api/projects/${encodeURIComponent(projectId)}/knowledge-capture/candidates?limit=50`,
          { credentials: "include", cache: "no-store" },
        );
        if (!response.ok) {
          throw new Error(
            await readError(response, "ナレッジ候補を取得できませんでした"),
          );
        }
        const list = normalizeKnowledgeCaptureCandidateList(
          await response.json(),
          projectId,
        );
        const normalizedItems = list.items;
        setItems(normalizedItems);
        setTotal(list.total);

        const targetId = requestedCandidateId || null;
        const targetInList = targetId
          ? normalizedItems.find((item) => item.id === targetId)
          : undefined;
        if (targetInList) {
          setSelectedCandidateId(targetInList.id);
        } else if (targetId && isUuid(targetId)) {
          setDetailLoading(true);
          try {
            const detailResponse = await fetch(detailUrl(projectId, targetId), {
              credentials: "include",
              cache: "no-store",
            });
            if (detailResponse.ok) {
              const detail = normalizeKnowledgeCaptureCandidateDetail(
                await detailResponse.json(),
                projectId,
              );
              if (detail) {
                setItems((current) =>
                  current.some((item) => item.id === detail.id)
                    ? current
                    : [detail, ...current].slice(0, 50),
                );
                setSelectedCandidateId(detail.id);
              }
            } else if (detailResponse.status !== 404) {
              throw new Error(
                await readError(detailResponse, "ナレッジ候補の詳細を取得できませんでした"),
              );
            }
          } finally {
            setDetailLoading(false);
          }
        } else {
          setSelectedCandidateId((current) =>
            current && normalizedItems.some((item) => item.id === current)
              ? current
              : normalizedItems[0]?.id ?? null,
          );
        }
      } catch (cause) {
        setError(
          cause instanceof Error
            ? cause.message
            : "ナレッジ候補を取得できませんでした",
        );
      } finally {
        setLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    setSelectedCandidateId(null);
    setEditing(false);
    setSelectedOptionId("");
    setFreeText("");
    void load();
    return () => undefined;
  }, [load, projectId]);

  const selectedCandidate = useMemo(
    () => items.find((item) => item.id === selectedCandidateId) ?? null,
    [items, selectedCandidateId],
  );

  useEffect(() => {
    setSelectedOptionId("");
    setFreeText("");
    setEditing(false);
    setDraftEdit(draftToEditState(selectedCandidate?.draft ?? null));
  }, [selectedCandidateId, selectedCandidate?.draft]);

  const mutate = useCallback(
    async (
      candidate: KnowledgeCaptureCandidate,
      action: "publish" | "dismiss" | "adopt-publication-guard",
      body: Record<string, unknown> = {},
    ) => {
      if (pendingAction) return;
      setPendingAction(action);
      setError(null);
      try {
        const response = await fetch(actionUrl(projectId, candidate.id, action), {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_candidate_version: candidate.version,
            ...body,
          }),
        });
        if (response.status === 409) {
          await load(candidate.id);
          setError("この候補は別の操作で更新されました。最新状態を再読み込みしました。");
          return;
        }
        if (!response.ok) {
          throw new Error(
            await readError(response, "ナレッジ候補を更新できませんでした"),
          );
        }
        await load(candidate.id);
      } catch (cause) {
        setError(
          cause instanceof Error
            ? cause.message
            : "ナレッジ候補を更新できませんでした",
        );
      } finally {
        setPendingAction(null);
      }
    },
    [load, pendingAction, projectId],
  );

  const answerCandidate = useCallback(
    async (candidate: KnowledgeCaptureCandidate, question: NonNullable<KnowledgeCaptureCandidate["question"]>) => {
      if (pendingAction) return;
      setPendingAction("answer");
      setError(null);
      const answerText = freeText.trim();
      const optionId = question.options.find((option) => option.id === selectedOptionId)?.id;
      try {
        const response = await fetch(answerUrl(projectId, candidate.id, question.id), {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_candidate_version: candidate.version,
            option_id: optionId,
            text: answerText || undefined,
          }),
        });
        if (response.status === 409) {
          await load(candidate.id);
          setError("この候補は別の操作で更新されました。最新状態を再読み込みしました。");
          return;
        }
        if (!response.ok) {
          throw new Error(
            await readError(response, "ナレッジ候補を更新できませんでした"),
          );
        }
        await load(candidate.id);
      } catch (cause) {
        setError(
          cause instanceof Error
            ? cause.message
            : "ナレッジ候補を更新できませんでした",
        );
      } finally {
        setPendingAction(null);
      }
    },
    [freeText, load, pendingAction, projectId, selectedOptionId],
  );

  const saveDraft = useCallback(async () => {
    if (!selectedCandidate || !canManageSettings || pendingAction) return;
    setPendingAction("edit");
    setError(null);
    try {
      const response = await fetch(draftUrl(projectId, selectedCandidate.id), {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_candidate_version: selectedCandidate.version,
          draft: draftPatchFromEdit(draftEdit),
        }),
      });
      if (response.status === 409) {
        await load(selectedCandidate.id);
        setError("この候補は別の操作で更新されました。最新状態を再読み込みしました。");
        return;
      }
      if (!response.ok) {
        throw new Error(
          await readError(response, "ナレッジ候補を編集できませんでした"),
        );
      }
      setEditing(false);
      await load(selectedCandidate.id);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "ナレッジ候補を編集できませんでした",
      );
    } finally {
      setPendingAction(null);
    }
  }, [canManageSettings, draftEdit, load, pendingAction, projectId, selectedCandidate]);

  const selectedQuestion = selectedCandidate?.question ?? null;
  const answer = freeText.trim() || selectedOptionId.trim();
  const canAnswer = Boolean(
    selectedCandidate?.status === "needs_user" &&
      selectedQuestion &&
      answer,
  );

  return (
    <Card
      className="border-border bg-card shadow-none"
      data-testid="project-knowledge-capture-panel"
    >
      <CardHeader className="border-b border-border">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <BookOpen className="size-4" />
            解決ナレッジ候補
            <Badge variant="secondary">{total}</Badge>
          </CardTitle>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void load(selectedCandidateId)}
            disabled={loading || detailLoading}
            aria-label="ナレッジ候補を更新"
          >
            <RefreshCw className={`mr-2 size-4${loading ? " animate-spin" : ""}`} />
            更新
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">
          解決した作業を、あとから再利用できるProject Docsとして確認・保存します。
        </p>
      </CardHeader>
      <CardContent className="space-y-4 pt-5">
        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
          >
            {error}
          </div>
        ) : null}
        {loading && items.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            ナレッジ候補を読み込み中…
          </p>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            確認待ちのナレッジ候補はありません。
          </p>
        ) : (
          <div className="grid gap-4 lg:grid-cols-[minmax(14rem,0.8fr)_minmax(0,1.8fr)]">
            <ul className="max-h-[32rem] space-y-2 overflow-auto pr-1" aria-label="ナレッジ候補一覧">
              {items.map((candidate) => (
                <li key={candidate.id}>
                  <button
                    type="button"
                    className={`w-full rounded-md border p-3 text-left transition-colors ${
                      selectedCandidateId === candidate.id
                        ? "border-primary bg-primary/5"
                        : "border-border hover:bg-muted/40"
                    }`}
                    onClick={() => setSelectedCandidateId(candidate.id)}
                    data-testid={`knowledge-capture-candidate-${candidate.id}`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="min-w-0 break-words text-sm font-medium">
                        {candidateTitle(candidate)}
                      </span>
                      <Badge variant="outline" className="shrink-0 text-[11px]">
                        {statusLabel(candidate.status)}
                      </Badge>
                    </div>
                    {candidate.draft?.evidenceSummary ? (
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                        {candidate.draft.evidenceSummary}
                      </p>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>

            {detailLoading ? (
              <div className="flex items-center gap-2 rounded-md border border-border p-4 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                候補の詳細を読み込み中…
              </div>
            ) : selectedCandidate ? (
              <CandidateDetail
                candidate={selectedCandidate}
                canManageSettings={canManageSettings}
                pendingAction={pendingAction}
                selectedQuestion={selectedQuestion}
                selectedOptionId={selectedOptionId}
                freeText={freeText}
                canAnswer={canAnswer}
                editing={editing}
                draftEdit={draftEdit}
                onSelectedOption={setSelectedOptionId}
                onFreeText={setFreeText}
                onStartEdit={() => {
                  setDraftEdit(draftToEditState(selectedCandidate.draft));
                  setEditing(true);
                }}
                onCancelEdit={() => setEditing(false)}
                onDraftTitle={(title) => setDraftEdit((current) => ({ ...current, title }))}
                onDraftSection={(index, value) =>
                  setDraftEdit((current) => ({
                    ...current,
                    sections: current.sections.map((section, sectionIndex) =>
                      sectionIndex === index ? { ...section, body: value } : section,
                    ),
                  }))
                }
                onDraftSectionTitle={(index, value) =>
                  setDraftEdit((current) => ({
                    ...current,
                    sections: current.sections.map((section, sectionIndex) =>
                      sectionIndex === index ? { ...section, title: value } : section,
                    ),
                  }))
                }
                onAnswer={() => {
                  if (selectedQuestion) {
                    void answerCandidate(selectedCandidate, selectedQuestion);
                  }
                }}
                onDismiss={() => void mutate(selectedCandidate, "dismiss")}
                onPublish={() => void mutate(selectedCandidate, "publish")}
                onAdoptPublicationGuard={() => {
                  if (
                    canManageSettings &&
                    window.confirm(
                      "現在のDocsを確認済みとして、この候補を採用しますか？",
                    )
                  ) {
                    void mutate(selectedCandidate, "adopt-publication-guard");
                  }
                }}
                onSaveDraft={() => void saveDraft()}
              />
            ) : (
              <p className="rounded-md border border-border p-4 text-sm text-muted-foreground">
                候補を選択してください。
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

type CandidateDetailProps = {
  candidate: KnowledgeCaptureCandidate;
  canManageSettings: boolean;
  pendingAction: string | null;
  selectedQuestion: KnowledgeCaptureCandidate["question"];
  selectedOptionId: string;
  freeText: string;
  canAnswer: boolean;
  editing: boolean;
  draftEdit: DraftEditState;
  onSelectedOption: (value: string) => void;
  onFreeText: (value: string) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onDraftTitle: (value: string) => void;
  onDraftSection: (index: number, value: string) => void;
  onDraftSectionTitle: (index: number, value: string) => void;
  onAnswer: () => void;
  onDismiss: () => void;
  onPublish: () => void;
  onAdoptPublicationGuard: () => void;
  onSaveDraft: () => void;
};

function CandidateDetail({
  candidate,
  canManageSettings,
  pendingAction,
  selectedQuestion,
  selectedOptionId,
  freeText,
  canAnswer,
  editing,
  draftEdit,
  onSelectedOption,
  onFreeText,
  onStartEdit,
  onCancelEdit,
  onDraftTitle,
  onDraftSection,
  onDraftSectionTitle,
  onAnswer,
  onDismiss,
  onPublish,
  onAdoptPublicationGuard,
  onSaveDraft,
}: CandidateDetailProps) {
  const draft = candidate.draft;
  return (
    <div className="min-w-0 rounded-md border border-border p-4" data-testid="knowledge-capture-detail">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">解決ナレッジ候補</p>
          <h3 className="mt-1 break-words text-base font-semibold">
            {candidateTitle(candidate)}
          </h3>
        </div>
        <Badge variant="secondary">{statusLabel(candidate.status)}</Badge>
      </div>

      {candidate.status === "needs_user" && selectedQuestion ? (
        <section className="mt-4 rounded-md border border-amber-500/40 bg-amber-500/5 p-3" data-testid="knowledge-capture-question">
          <div className="flex items-start gap-2">
            <HelpCircle className="mt-0.5 size-4 shrink-0 text-amber-600" />
            <div className="min-w-0">
              <h4 className="text-sm font-medium">確認が必要です</h4>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm">
                {selectedQuestion.prompt}
              </p>
            </div>
          </div>
          {selectedQuestion.options.length > 0 ? (
            <fieldset className="mt-3 space-y-2">
              <legend className="text-xs text-muted-foreground">選択肢</legend>
              {selectedQuestion.options.map((option) => (
                <label key={option.id} className="flex items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name={`knowledge-capture-question-${selectedQuestion.id}`}
                    value={option.id}
                    checked={selectedOptionId === option.id}
                    onChange={() => onSelectedOption(option.id)}
                    disabled={pendingAction !== null}
                    className="mt-1"
                  />
                  <span className="whitespace-pre-wrap break-words">{option.label}</span>
                </label>
              ))}
            </fieldset>
          ) : null}
          {selectedQuestion.allowFreeText ? (
            <label className="mt-3 block text-xs text-muted-foreground">
              補足・自由入力
              <Textarea
                value={freeText}
                onChange={(event) => onFreeText(event.target.value)}
                placeholder="最終的に行った操作を入力"
                className="mt-1 min-h-20 text-sm"
                disabled={pendingAction !== null}
              />
            </label>
          ) : null}
          <div className="mt-3 flex flex-wrap gap-2 border-t border-border/70 pt-3">
            <Button type="button" size="sm" onClick={onAnswer} disabled={!canAnswer || pendingAction !== null}>
              {pendingAction === "answer" ? (
                <Loader2 className="mr-2 size-4 animate-spin" />
              ) : (
                <Send className="mr-2 size-4" />
              )}
              回答を送る
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={onDismiss} disabled={pendingAction !== null}>
              {pendingAction === "dismiss" ? (
                <Loader2 className="mr-2 size-4 animate-spin" />
              ) : (
                <X className="mr-2 size-4" />
              )}
              今回は保存しない
            </Button>
          </div>
        </section>
      ) : null}

      {candidate.status === "draft_ready" && draft ? (
        <section className="mt-4 space-y-4" data-testid="knowledge-capture-draft">
          {editing ? (
            <label className="block text-xs text-muted-foreground">
              タイトル
              <Input
                value={draftEdit.title}
                onChange={(event) => onDraftTitle(event.target.value)}
                className="mt-1 text-sm"
                disabled={pendingAction !== null}
              />
            </label>
          ) : (
            <h4 className="text-sm font-semibold">{draft.title || "タイトル未設定"}</h4>
          )}
          <div className="space-y-3">
            {(editing ? draftEdit.sections : draft.sections).map((section, index) => (
              <article key={`${section.title}-${index}`} className="rounded-md border border-border/70 p-3">
                {editing ? (
                  <Input
                    value={section.title}
                    onChange={(event) => onDraftSectionTitle(index, event.target.value)}
                    placeholder="セクション名"
                    className="text-sm font-medium"
                    disabled={pendingAction !== null}
                  />
                ) : section.title ? (
                  <h5 className="text-sm font-medium">{section.title}</h5>
                ) : null}
                {editing ? (
                  <Textarea
                    value={section.body}
                    onChange={(event) => onDraftSection(index, event.target.value)}
                    className="mt-2 min-h-24 text-sm"
                    disabled={pendingAction !== null}
                  />
                ) : (
                  <p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">
                    {section.body}
                  </p>
                )}
              </article>
            ))}
          </div>
          {draft.evidenceSummary ? (
            <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">根拠の要約:</span>{" "}
              {draft.evidenceSummary}
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2 border-t border-border/70 pt-3">
            {canManageSettings ? (
              editing ? (
                <>
                  <Button type="button" size="sm" onClick={onSaveDraft} disabled={pendingAction !== null}>
                    {pendingAction === "edit" ? (
                      <Loader2 className="mr-2 size-4 animate-spin" />
                    ) : (
                      <Save className="mr-2 size-4" />
                    )}
                    編集内容を保存
                  </Button>
                  <Button type="button" size="sm" variant="outline" onClick={onCancelEdit} disabled={pendingAction !== null}>
                    キャンセル
                  </Button>
                </>
              ) : (
                <Button type="button" size="sm" variant="outline" onClick={onStartEdit} disabled={pendingAction !== null}>
                  編集
                </Button>
              )
            ) : null}
            {canManageSettings ? (
              <Button type="button" size="sm" onClick={onPublish} disabled={pendingAction !== null || editing}>
                {pendingAction === "publish" ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <Check className="mr-2 size-4" />
                )}
                Docsへ保存
              </Button>
            ) : null}
            {canManageSettings ? (
              <Button type="button" size="sm" variant="ghost" onClick={onDismiss} disabled={pendingAction !== null || editing}>
                {pendingAction === "dismiss" ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <X className="mr-2 size-4" />
                )}
                今回は保存しない
              </Button>
            ) : (
              <p className="self-center text-xs text-muted-foreground">
                編集・保存にはmanage_settings権限が必要です。
              </p>
            )}
          </div>
        </section>
      ) : null}

      {candidate.status === "published" && candidate.published ? (
        <section className="mt-4 space-y-3 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3" data-testid="knowledge-capture-published">
          <p className="text-sm font-medium">Docsに保存済みです。</p>
          {candidate.publicationGuardAdoptionRequired ? (
            <div
              className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3"
              data-testid="knowledge-capture-publication-guard-warning"
            >
              <p className="text-sm font-medium text-amber-950 dark:text-amber-100">
                現在のDocsを確認してください。内容をレビューしてから採用してください。
              </p>
              {canManageSettings ? (
                <Button
                  type="button"
                  size="sm"
                  className="mt-3"
                  onClick={onAdoptPublicationGuard}
                  disabled={pendingAction !== null}
                >
                  {pendingAction === "adopt-publication-guard" ? (
                    <Loader2 className="mr-2 size-4 animate-spin" />
                  ) : null}
                  現在のDocsを確認済みとして採用
                </Button>
              ) : null}
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2">
            {candidate.published.docsNodeId && isUuid(candidate.published.docsNodeId) ? (
              <a
                href={`/docs/${encodeURIComponent(candidate.published.docsNodeId)}`}
                className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm text-primary hover:bg-accent"
              >
                <FileText className="size-3.5" />
                Docsを開く
              </a>
            ) : null}
            {candidate.published.sourceTaskId && isUuid(candidate.published.sourceTaskId) ? (
              <a
                href={`/tasks/${encodeURIComponent(candidate.published.sourceTaskId)}`}
                className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm text-primary hover:bg-accent"
              >
                <BookOpen className="size-3.5" />
                {candidate.published.sourceTaskTitle || "元のTaskを開く"}
              </a>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}
