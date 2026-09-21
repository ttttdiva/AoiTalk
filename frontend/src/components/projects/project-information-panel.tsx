"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DocsWorkspace } from "@/components/docs/docs-workspace";
import type { DocsNode, DocsNodeSupertag, DocsSupertag } from "@/components/docs/types";
import { RecordTableEditor } from "@/components/records/record-table-editor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  BookOpen,
  Check,
  CheckCircle2,
  Database,
  ListChecks,
  Loader2,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Table2,
  Trash2,
  X,
} from "lucide-react";

type ProjectInfo = {
  id: string;
  name: string;
  description?: string | null;
  /** Project access is projected by the Projects page.  Keep optional so the
   * panel remains usable in isolated embeds and existing tests. */
  can_write?: boolean;
  can_manage_settings?: boolean;
};

type ProjectInformationNode = {
  id: string;
  title: string;
  body_text: string;
  body_json?: Record<string, unknown>;
  updated_at?: string | null;
};

type ProjectQaEntry = {
  id: string;
  question: string;
  answer: string | null;
  status?: string | null;
  review_state?: string | null;
  /** Drizzle-backed routes historically exposed camelCase fields.  Accept
   * both shapes while the BFF contract converges on snake_case. */
  reviewState?: string | null;
  origin?: string | null;
  version?: number | null;
  confidence?: number | null;
  asked_count?: number | null;
  askedCount?: number | null;
  answer_source_refs?: unknown;
  answerSourceRefs?: unknown;
};

type QaCandidateResponse = {
  items?: ProjectQaEntry[];
  candidates?: ProjectQaEntry[];
  qa_entries?: ProjectQaEntry[];
  version?: number | null;
  origin?: string | null;
};

type QaCleanupResponse = {
  matched?: number;
  matched_count?: number;
  eligible_count?: number;
  deleted?: number;
  deleted_count?: number;
  archived_count?: number;
  dry_run?: boolean;
};

type RecordTableSummary = {
  id: string;
  name: string;
  description: string | null;
  updatedAt?: string | null;
  updated_at?: string | null;
};

type ProjectInformationResponse = {
  project: {
    id: string;
    name: string;
    description: string | null;
    knowledge_node_id: string;
  };
  node: ProjectInformationNode;
  tree_nodes: DocsNode[];
  node_supertags: DocsNodeSupertag[];
  supertags: DocsSupertag[];
  qa_entries: ProjectQaEntry[];
  management_documents: Array<Record<string, unknown>>;
  record_tables: RecordTableSummary[];
};

type IntakeTaskCandidate = {
  title: string;
  description: string;
  due_date: string | null;
  priority: string | null;
};

type IntakeDocsUpdate = {
  content: string;
  section_heading: string | null;
  source_ref: string | null;
};

type DailyIntakeDraft = {
  intake_date: string;
  raw_input: string;
  summary_md: string;
  done_items: string[];
  decisions: string[];
  confirmations: string[];
  inquiries: string[];
  issues: string[];
  task_candidates: IntakeTaskCandidate[];
  docs_updates: IntakeDocsUpdate[];
  clarifying_questions: string[];
};

type DailyIntakePreviewResponse = {
  success: boolean;
  needs_clarification: boolean;
  draft: DailyIntakeDraft;
  clarifying_questions: string[];
};

type DailyIntakeApplyResult = {
  decisions: number;
  confirmations: number;
  issues: number;
  inquiries: number;
  tasks: number;
  record_rows: number;
  docs_updates: number;
  knowledge_node_id: string;
};

type DailyIntakeApplyResponse = {
  success: boolean;
  applied: boolean;
  result: DailyIntakeApplyResult;
};

type IntakePhase = "input" | "clarify" | "preview" | "done";

function todayString() {
  const now = new Date();
  const offset = now.getTimezoneOffset();
  const local = new Date(now.getTime() - offset * 60000);
  return local.toISOString().slice(0, 10);
}

function IntakeListCard({ label, items }: { label: string; items: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <ul className="mt-2 space-y-1 text-sm">
        {items.map((item, index) => (
          <li key={index} className="flex gap-2">
            <span className="text-muted-foreground">・</span>
            <span className="min-w-0 break-words">{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function IntakeTaskCard({ items }: { items: IntakeTaskCandidate[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="text-xs font-medium text-muted-foreground">タスク候補</div>
      <div className="mt-2 space-y-2">
        {items.map((task, index) => (
          <div key={index} className="rounded-md border bg-background p-2">
            <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
              <ListChecks className="size-4 text-primary" />
              <span className="min-w-0 break-words">{task.title}</span>
              {task.priority ? <Badge variant="outline">{task.priority}</Badge> : null}
              {task.due_date ? <Badge variant="secondary">{task.due_date}</Badge> : null}
            </div>
            {task.description ? (
              <p className="mt-1 text-xs text-muted-foreground break-words">{task.description}</p>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function IntakeDocsCard({ items }: { items: IntakeDocsUpdate[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="text-xs font-medium text-muted-foreground">Docs反映候補</div>
      <div className="mt-2 space-y-2">
        {items.map((update, index) => (
          <div key={index} className="rounded-md border bg-background p-2">
            {update.section_heading ? (
              <div className="text-sm font-medium break-words">{update.section_heading}</div>
            ) : null}
            <p className="mt-1 text-sm text-muted-foreground break-words whitespace-pre-wrap">
              {update.content}
            </p>
            {update.source_ref ? (
              <p className="mt-1 text-[11px] text-muted-foreground">出典: {update.source_ref}</p>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function DailyIntakeSection({
  project,
  onApplied,
}: {
  project: ProjectInfo;
  onApplied: () => Promise<void> | void;
}) {
  const [phase, setPhase] = useState<IntakePhase>("input");
  const [rawInput, setRawInput] = useState("");
  const [intakeDate, setIntakeDate] = useState(() => todayString());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<DailyIntakeDraft | null>(null);
  const [clarifyingQuestions, setClarifyingQuestions] = useState<string[]>([]);
  const [answers, setAnswers] = useState<string[]>([]);
  const [applied, setApplied] = useState<DailyIntakeApplyResult | null>(null);

  const intakeUrl = `/api/python-proxy/projects/${project.id}/information/daily-intake`;

  const submitPreview = useCallback(
    async (options: {
      rawInput: string;
      clarificationAnswers: string;
      draft: DailyIntakeDraft | null;
    }) => {
      setSubmitting(true);
      setError("");
      try {
        const response = await apiFetch<DailyIntakePreviewResponse>(intakeUrl, {
          method: "POST",
          body: JSON.stringify({
            raw_input: options.rawInput,
            intake_date: intakeDate || undefined,
            clarification_answers: options.clarificationAnswers,
            apply: false,
            use_llm: true,
            draft: options.draft,
          }),
        });
        setDraft(response.draft);
        const questions = response.clarifying_questions ?? [];
        if (response.needs_clarification && questions.length > 0) {
          setClarifyingQuestions(questions);
          setAnswers(questions.map(() => ""));
          setPhase("clarify");
        } else {
          setClarifyingQuestions([]);
          setPhase("preview");
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "整理に失敗しました");
      } finally {
        setSubmitting(false);
      }
    },
    [intakeUrl, intakeDate],
  );

  const handleStart = useCallback(() => {
    if (!rawInput.trim()) {
      setError("その日やったことを入力してください");
      return;
    }
    void submitPreview({ rawInput, clarificationAnswers: "", draft: null });
  }, [rawInput, submitPreview]);

  const handleClarifySubmit = useCallback(() => {
    const combined = clarifyingQuestions
      .map((question, index) => `Q: ${question}\nA: ${answers[index]?.trim() ?? ""}`)
      .join("\n\n");
    void submitPreview({ rawInput, clarificationAnswers: combined, draft });
  }, [answers, clarifyingQuestions, draft, rawInput, submitPreview]);

  const handleApply = useCallback(async () => {
    if (!draft) return;
    setSubmitting(true);
    setError("");
    try {
      const response = await apiFetch<DailyIntakeApplyResponse>(intakeUrl, {
        method: "POST",
        body: JSON.stringify({
          raw_input: rawInput,
          intake_date: intakeDate || undefined,
          clarification_answers: "",
          apply: true,
          use_llm: true,
          draft,
        }),
      });
      setApplied(response.result);
      setPhase("done");
      await onApplied();
    } catch (err) {
      setError(err instanceof Error ? err.message : "案件情報への反映に失敗しました");
    } finally {
      setSubmitting(false);
    }
  }, [draft, intakeDate, intakeUrl, onApplied, rawInput]);

  const resetToInput = useCallback(() => {
    setPhase("input");
    setDraft(null);
    setClarifyingQuestions([]);
    setAnswers([]);
    setApplied(null);
    setError("");
  }, []);

  const startNew = useCallback(() => {
    resetToInput();
    setRawInput("");
    setIntakeDate(todayString());
  }, [resetToInput]);

  return (
    <section className="rounded-md border bg-background">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="size-4 text-primary" />
          日次インテーク
        </div>
        {phase !== "input" ? (
          <Badge variant="secondary">
            {phase === "clarify" ? "逆質問応答" : phase === "preview" ? "整理案の確認" : "反映済み"}
          </Badge>
        ) : null}
      </div>
      <div className="space-y-3 p-3">
        {error ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}

        {phase === "input" ? (
          <div className="space-y-3">
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">その日やったこと</label>
              <Textarea
                value={rawInput}
                onChange={(event) => setRawInput(event.target.value)}
                placeholder="今日やったこと・決めたこと・気になったことを雑に書いてください"
                className="min-h-28"
                disabled={submitting}
              />
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <div className="space-y-1">
                <label className="text-xs font-medium text-muted-foreground">対象日</label>
                <Input
                  type="date"
                  value={intakeDate}
                  onChange={(event) => setIntakeDate(event.target.value)}
                  className="w-40"
                  disabled={submitting}
                />
              </div>
              <Button onClick={handleStart} disabled={submitting}>
                {submitting ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <Sparkles className="mr-2 size-4" />
                )}
                整理する
              </Button>
            </div>
          </div>
        ) : null}

        {phase === "clarify" ? (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              内容を整理するため、以下の不明点にお答えください。
            </p>
            <div className="space-y-3">
              {clarifyingQuestions.map((question, index) => (
                <div key={index} className="space-y-1">
                  <label className="text-sm font-medium break-words">{question}</label>
                  <Textarea
                    value={answers[index] ?? ""}
                    onChange={(event) => {
                      const next = [...answers];
                      next[index] = event.target.value;
                      setAnswers(next);
                    }}
                    placeholder="回答を入力（不明な場合は空欄でも構いません）"
                    disabled={submitting}
                  />
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={handleClarifySubmit} disabled={submitting}>
                {submitting ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <Sparkles className="mr-2 size-4" />
                )}
                回答して再整理
              </Button>
              <Button variant="outline" onClick={resetToInput} disabled={submitting}>
                <RotateCcw className="mr-2 size-4" />
                やり直す
              </Button>
            </div>
          </div>
        ) : null}

        {phase === "preview" && draft ? (
          <div className="space-y-3">
            {draft.summary_md ? (
              <div className="rounded-md border bg-muted/20 p-3">
                <div className="text-xs font-medium text-muted-foreground">サマリ</div>
                <p className="mt-2 text-sm break-words whitespace-pre-wrap">{draft.summary_md}</p>
              </div>
            ) : null}
            <div className="grid gap-3 md:grid-cols-2">
              <IntakeListCard label="実施事項" items={draft.done_items} />
              <IntakeListCard label="決定事項" items={draft.decisions} />
              <IntakeListCard label="確認事項" items={draft.confirmations} />
              <IntakeListCard label="問い合わせ" items={draft.inquiries} />
              <IntakeListCard label="課題" items={draft.issues} />
            </div>
            <IntakeTaskCard items={draft.task_candidates} />
            <IntakeDocsCard items={draft.docs_updates} />
            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={() => void handleApply()} disabled={submitting}>
                {submitting ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <Check className="mr-2 size-4" />
                )}
                案件情報へ反映
              </Button>
              <Button variant="outline" onClick={resetToInput} disabled={submitting}>
                <RotateCcw className="mr-2 size-4" />
                やり直す
              </Button>
            </div>
          </div>
        ) : null}

        {phase === "done" && applied ? (
          <div className="space-y-3">
            <div className="rounded-md border border-primary/30 bg-primary/5 p-3 text-sm">
              <div className="flex items-center gap-2 font-medium text-primary">
                <Check className="size-4" />
                案件情報へ反映しました
              </div>
              <div className="mt-2 grid gap-2 text-xs text-muted-foreground sm:grid-cols-3">
                <span>決定事項 {applied.decisions}件</span>
                <span>確認事項 {applied.confirmations}件</span>
                <span>問い合わせ {applied.inquiries}件</span>
                <span>課題 {applied.issues}件</span>
                <span>タスク {applied.tasks}件</span>
                <span>台帳行 {applied.record_rows}件</span>
                <span>Docs反映 {applied.docs_updates}件</span>
              </div>
            </div>
            <Button variant="outline" onClick={startNew}>
              <RotateCcw className="mr-2 size-4" />
              新しく入力する
            </Button>
          </div>
        ) : null}
      </div>
    </section>
  );
}

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function formatDate(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

function qaReviewState(entry: ProjectQaEntry): "accepted" | "candidate" | "rejected" | string {
  const state = entry.review_state ?? entry.reviewState;
  // Older/manual responses did not include review_state.  Those rows were
  // already human-facing and should remain visible rather than disappearing
  // during the contract migration.  New inferred rows always carry an
  // explicit candidate state from the BFF.
  return typeof state === "string" && state.trim() ? state.trim().toLowerCase() : "accepted";
}

function qaStatusLabel(status: string | null | undefined) {
  switch ((status ?? "").trim().toLowerCase()) {
    case "answered":
      return "回答済み";
    case "unanswered":
      return "未回答";
    case "stale":
      return "要更新";
    case "cancelled":
      return "取消";
    case "archived":
      return "アーカイブ";
    default:
      return status?.trim() || "状態不明";
  }
}

function qaOriginLabel(origin: string | null | undefined) {
  switch ((origin ?? "").trim().toLowerCase()) {
    case "background":
    case "inferred":
    case "legacy_auto":
    case "auto":
      return "自動整理候補";
    case "manual":
    case "explicit":
      return "手動登録";
    default:
      return origin?.trim() || "由来不明";
  }
}

function qaReviewLabel(state: string) {
  switch (state) {
    case "rejected":
      return "却下済み";
    case "accepted":
      return "承認済み";
    default:
      return "レビュー待ち";
  }
}

function qaVersion(entry: ProjectQaEntry, queueVersion?: number | null) {
  const value = entry.version ?? queueVersion;
  // The persisted model defaults every row to version 1.  Supplying that
  // fallback keeps legacy queue payloads reviewable while still making the
  // server's optimistic check authoritative.
  return typeof value === "number" && Number.isFinite(value) ? Math.max(1, Math.round(value)) : 1;
}

function qaSourceLinks(entry: ProjectQaEntry) {
  const raw = entry.answer_source_refs ?? entry.answerSourceRefs;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => {
    if (typeof item !== "string") return [];
    const value = item.trim();
    if (!value) return [];
    try {
      const url = new URL(value);
      if (url.protocol !== "http:" && url.protocol !== "https:") return [];
      return [{ value, url: url.toString() }];
    } catch {
      return [];
    }
  });
}

function candidateItemsFromResponse(response: QaCandidateResponse): ProjectQaEntry[] {
  const items = response.items ?? response.candidates ?? response.qa_entries ?? [];
  if (!Array.isArray(items)) return [];
  return items.filter((item): item is ProjectQaEntry => Boolean(item && typeof item === "object" && typeof item.id === "string"));
}

function cleanupCount(response: QaCleanupResponse, key: "matched" | "deleted") {
  const value = key === "matched"
    ? response.matched ?? response.matched_count ?? response.eligible_count
    : response.deleted ?? response.deleted_count ?? response.archived_count;
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
}

export function ProjectInformationPanel({
  project,
  canManageSettings,
}: {
  project: ProjectInfo;
  /** Optional explicit projection for callers that do not include it on the Project DTO. */
  canManageSettings?: boolean;
}) {
  const [loadedData, setLoadedData] = useState<{
    projectId: string;
    response: ProjectInformationResponse;
  } | null>(null);
  const [errorState, setErrorState] = useState<{
    projectId: string;
    message: string;
  } | null>(null);
  const [selectedTableState, setSelectedTableState] = useState<{
    projectId: string;
    tableId: string;
  } | null>(null);
  const [loadingProjectId, setLoadingProjectId] = useState<string | null>(null);
  const [qaCandidatesState, setQaCandidatesState] = useState<{
    projectId: string;
    items: ProjectQaEntry[];
    version: number | null;
    origin: string | null;
  } | null>(null);
  const [qaCandidatesLoading, setQaCandidatesLoading] = useState(false);
  const [qaCandidatesError, setQaCandidatesError] = useState<string | null>(null);
  const [qaActionId, setQaActionId] = useState<string | null>(null);
  const [qaCleanupState, setQaCleanupState] = useState<{
    matched: number;
    deleted: number;
    dryRun: boolean;
  } | null>(null);
  const [qaCleanupLoading, setQaCleanupLoading] = useState(false);
  const requestGenerationRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);
  const qaCandidatesGenerationRef = useRef(0);
  const qaCandidatesControllerRef = useRef<AbortController | null>(null);

  const data = loadedData?.projectId === project.id ? loadedData.response : null;
  const error = errorState?.projectId === project.id ? errorState.message : "";
  const loading =
    loadingProjectId === project.id || (data === null && error.length === 0);
  const selectedTableId =
    selectedTableState?.projectId === project.id
      ? selectedTableState.tableId
      : null;
  const qaCandidates = qaCandidatesState?.projectId === project.id
    ? qaCandidatesState.items
    : [];
  const qaCandidateVersion = qaCandidatesState?.projectId === project.id
    ? qaCandidatesState.version
    : null;
  const qaCandidateOrigin = qaCandidatesState?.projectId === project.id
    ? qaCandidatesState.origin
    : null;
  // Candidate review promotes content into canonical Project Information and
  // therefore follows the same write permission as the explicit Q&A route.
  // Do not render controls for a settings-only member that the API would
  // correctly reject with 403.
  const canReviewQa = project.can_write === true;
  // Bulk cleanup is destructive and requires the stronger Project settings
  // permission.  Keep it separate from per-candidate review so ordinary
  // writers can still accept/reject without being offered a 403ing cleanup
  // action.
  const canCleanupQa =
    canManageSettings === true || project.can_manage_settings === true;

  const loadQaCandidates = useCallback(async () => {
    if (!canReviewQa) return;
    const generation = ++qaCandidatesGenerationRef.current;
    qaCandidatesControllerRef.current?.abort();
    const controller = new AbortController();
    qaCandidatesControllerRef.current = controller;
    setQaCandidatesLoading(true);
    setQaCandidatesError(null);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(project.id)}/information/qa-candidates`,
        { credentials: "include", signal: controller.signal },
      );
      const body = (await response.json().catch(() => ({}))) as QaCandidateResponse & { detail?: unknown };
      if (!response.ok) {
        // A deployment without the optional review route should not make the
        // canonical Project Information document unusable.  Once the route is
        // present, all other failures remain visible to operators.
        if (response.status === 404) return;
        const detail = typeof body.detail === "string" && body.detail.trim()
          ? body.detail
          : "Q&A候補を取得できませんでした";
        throw new Error(detail);
      }
      if (controller.signal.aborted || generation !== qaCandidatesGenerationRef.current) return;
      setQaCandidatesState({
        projectId: project.id,
        items: candidateItemsFromResponse(body),
        version: typeof body.version === "number" && Number.isFinite(body.version) ? body.version : null,
        origin: typeof body.origin === "string" ? body.origin : null,
      });
    } catch (cause) {
      if (controller.signal.aborted || generation !== qaCandidatesGenerationRef.current) return;
      setQaCandidatesError(cause instanceof Error ? cause.message : "Q&A候補を取得できませんでした");
    } finally {
      if (!controller.signal.aborted && generation === qaCandidatesGenerationRef.current) {
        setQaCandidatesLoading(false);
      }
    }
  }, [canReviewQa, project.id]);

  const load = useCallback(async () => {
    const requestProjectId = project.id;
    const requestGeneration = ++requestGenerationRef.current;
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    setLoadingProjectId(requestProjectId);
    setErrorState(null);
    try {
      const response = await apiFetch<ProjectInformationResponse>(
        `/api/projects/${requestProjectId}/information`,
        { signal: controller.signal },
      );
      if (requestGeneration !== requestGenerationRef.current) return;
      setLoadedData({ projectId: requestProjectId, response });
    } catch (err) {
      if (
        controller.signal.aborted ||
        requestGeneration !== requestGenerationRef.current
      ) {
        return;
      }
      setErrorState({
        projectId: requestProjectId,
        message: err instanceof Error ? err.message : "案件情報の取得に失敗しました",
      });
    } finally {
      if (requestGeneration === requestGenerationRef.current) {
        setLoadingProjectId(null);
      }
    }
  }, [project.id]);

  useEffect(() => {
    let disposed = false;
    void Promise.resolve().then(() => {
      if (!disposed) void load();
    });
    return () => {
      disposed = true;
      requestControllerRef.current?.abort();
    };
  }, [load]);

  useEffect(() => {
    if (!canReviewQa) {
      qaCandidatesControllerRef.current?.abort();
      qaCandidatesGenerationRef.current += 1;
      return;
    }
    // Defer the initial queue request one microtask so this subscription
    // effect does not synchronously cascade a loading-state render.  Guard the
    // callback because a rapid Project switch can unmount this effect before
    // the microtask runs.
    let disposed = false;
    void Promise.resolve().then(() => {
      if (!disposed) void loadQaCandidates();
    });
    return () => {
      disposed = true;
      qaCandidatesGenerationRef.current += 1;
      qaCandidatesControllerRef.current?.abort();
    };
  }, [canReviewQa, loadQaCandidates]);

  const updateQaCandidate = useCallback(
    async (entry: ProjectQaEntry, action: "accept" | "reject" | "delete") => {
      if (!canReviewQa || qaActionId) return;
      setQaActionId(entry.id);
      setQaCandidatesError(null);
      try {
        const expectedVersion = qaVersion(entry, qaCandidateVersion);
        const body: Record<string, unknown> = { action };
        if (expectedVersion !== null) body.expected_version = expectedVersion;
        const response = await fetch(
          `/api/projects/${encodeURIComponent(project.id)}/information/qa-candidates/${encodeURIComponent(entry.id)}`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        const payload = (await response.json().catch(() => ({}))) as { detail?: unknown };
        if (response.status === 409) {
          throw new Error("このQ&A候補は別の操作で更新されています。再読み込みしてください。");
        }
        if (!response.ok) {
          throw new Error(
            typeof payload.detail === "string" && payload.detail.trim()
              ? payload.detail
              : action === "accept"
                ? "Q&A候補を承認できませんでした"
                : action === "reject"
                  ? "Q&A候補を却下できませんでした"
                  : "Q&A候補を削除できませんでした",
          );
        }
        await Promise.all([
          loadQaCandidates(),
          action === "accept" ? load() : Promise.resolve(),
        ]);
      } catch (cause) {
        setQaCandidatesError(cause instanceof Error ? cause.message : "Q&A候補の更新に失敗しました");
      } finally {
        setQaActionId(null);
      }
    },
    [canReviewQa, load, loadQaCandidates, project.id, qaActionId, qaCandidateVersion],
  );

  const runQaCleanup = useCallback(
    async (dryRun: boolean) => {
      if (!canCleanupQa || qaCleanupLoading) return;
      setQaCleanupLoading(true);
      setQaCandidatesError(null);
      try {
        const response = await fetch(
          `/api/projects/${encodeURIComponent(project.id)}/information/qa-cleanup`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dry_run: dryRun }),
          },
        );
        const payload = (await response.json().catch(() => ({}))) as QaCleanupResponse & { detail?: unknown };
        if (!response.ok) {
          throw new Error(
            typeof payload.detail === "string" && payload.detail.trim()
              ? payload.detail
              : "既存Q&A候補の整理に失敗しました",
          );
        }
        setQaCleanupState({
          matched: cleanupCount(payload, "matched"),
          deleted: cleanupCount(payload, "deleted"),
          dryRun,
        });
        if (!dryRun) {
          await Promise.all([loadQaCandidates(), load()]);
        }
      } catch (cause) {
        setQaCandidatesError(cause instanceof Error ? cause.message : "既存Q&A候補の整理に失敗しました");
      } finally {
        setQaCleanupLoading(false);
      }
    },
    [canCleanupQa, load, loadQaCandidates, project.id, qaCleanupLoading],
  );

  const recordTables = useMemo(() => data?.record_tables ?? [], [data?.record_tables]);
  const selectedTable = useMemo(
    () => recordTables.find((table) => table.id === selectedTableId) ?? null,
    [recordTables, selectedTableId],
  );

  if (loading && !data) {
    return (
      <div className="flex min-h-[360px] items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 size-4 animate-spin" />
        案件情報を読み込み中...
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="rounded-md border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
        {error}
        <div className="mt-3">
          <Button variant="outline" size="sm" onClick={() => void load()}>
            <RefreshCw className="mr-2 size-4" />
            再読み込み
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 pb-8" data-testid="project-information-panel">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <BookOpen className="size-4 text-primary" />
            <h3 className="truncate text-sm font-semibold">案件情報</h3>
            <Badge variant="secondary">Docs正本</Badge>
          </div>
          <p className="mt-1 truncate text-xs text-muted-foreground">
            {data?.node.title || `${project.name} 案件情報`}
            {data?.node.updated_at ? ` / 更新 ${formatDate(data.node.updated_at)}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            <RefreshCw className="mr-2 size-4" />
            更新
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {data?.node.id && (
        <section className="min-h-[560px] w-full min-w-0 overflow-hidden rounded-md border border-border bg-card" data-testid="project-information-docs">
          <DocsWorkspace
            key={`${project.id}:${data.node.id}`}
            initialNodeId={data.node.id}
          />
        </section>
      )}

      <div className="mx-auto w-full max-w-4xl">
        <DailyIntakeSection key={project.id} project={project} onApplied={load} />
      </div>

      {(() => {
        const allQaEntries = data?.qa_entries ?? [];
        const acceptedEntries = allQaEntries.filter((entry) => qaReviewState(entry) === "accepted");
        const embeddedCandidates = allQaEntries.filter((entry) => qaReviewState(entry) !== "accepted");
        // Prefer the dedicated queue copy: it carries the optimistic version
        // token required by review mutations.  Embedded legacy candidates are
        // retained only as a rolling-deployment fallback.
        const queuedCandidates = [...qaCandidates, ...embeddedCandidates].filter((entry, index, entries) => (
          entries.findIndex((candidate) => candidate.id === entry.id) === index
        )).filter((entry) => qaReviewState(entry) !== "accepted");
        return (
          <>
            <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(32rem,1.1fr)]">
              <section className="mx-auto w-full max-w-4xl min-w-0 rounded-md border border-border bg-card" data-testid="project-qa-section">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
                  <div>
                    <div className="text-sm font-medium">Q&amp;A（承認済み）</div>
                    <p className="mt-0.5 text-xs text-muted-foreground">Projectで再利用する質問と回答だけを表示しています。</p>
                  </div>
                  <Badge variant="secondary">{acceptedEntries.length}</Badge>
                </div>
                <div className="max-h-[28rem] overflow-auto p-3">
                  {acceptedEntries.length === 0 ? (
                    <p className="text-sm text-muted-foreground">承認済みのQ&amp;Aはまだありません。</p>
                  ) : (
                    <div className="space-y-3">
                      {acceptedEntries.map((entry) => {
                        const sourceLinks = qaSourceLinks(entry);
                        return (
                          <article key={entry.id} className="min-w-0 rounded-md border p-3" data-testid={`project-qa-entry-${entry.id}`}>
                            <h4 className="whitespace-pre-wrap break-words text-sm font-medium [overflow-wrap:anywhere]">{entry.question}</h4>
                            <p className="mt-2 whitespace-pre-wrap break-words text-sm text-muted-foreground [overflow-wrap:anywhere]">
                              {entry.answer?.trim() || "回答未登録"}
                            </p>
                            {sourceLinks.length > 0 ? (
                              <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                                <span>出典:</span>
                                {sourceLinks.map((source) => (
                                  <a key={source.url} href={source.url} target="_blank" rel="noreferrer" className="max-w-full break-all text-primary underline underline-offset-2">
                                    {source.value}
                                  </a>
                                ))}
                              </div>
                            ) : null}
                            <div className="mt-2 flex flex-wrap gap-2">
                              <Badge variant="outline">{qaStatusLabel(entry.status)}</Badge>
                              <Badge variant="outline">承認済み</Badge>
                            </div>
                          </article>
                        );
                      })}
                    </div>
                  )}
                </div>
              </section>

              <section className="min-w-0 rounded-md border border-border bg-card" data-testid="project-record-tables">
                <div className="flex items-center justify-between border-b px-3 py-2">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <Database className="size-4" />
                    台帳
                  </div>
                  <Badge variant="secondary">{recordTables.length}</Badge>
                </div>
                <div className="max-h-80 overflow-auto p-3">
                  {recordTables.length === 0 ? (
                    <p className="text-sm text-muted-foreground">台帳はまだありません。</p>
                  ) : (
                    <div className="space-y-2">
                      {recordTables.map((table) => (
                        <button
                          key={table.id}
                          type="button"
                          onClick={() =>
                            setSelectedTableState({
                              projectId: project.id,
                              tableId: table.id,
                            })
                          }
                          className="w-full rounded-md border border-border p-3 text-left transition-colors hover:bg-accent"
                        >
                          <div className="flex items-center gap-2 text-sm font-medium">
                            <Table2 className="size-4" />
                            <span className="min-w-0 break-words">{table.name}</span>
                          </div>
                          {table.description && (
                            <p className="mt-1 whitespace-pre-wrap break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">
                              {table.description}
                            </p>
                          )}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </section>
            </div>

            {canReviewQa ? (
              <section className="mx-auto w-full max-w-4xl rounded-md border border-amber-500/40 bg-card" data-testid="project-qa-review">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-500/30 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <CheckCircle2 className="size-4 text-amber-600 dark:text-amber-300" />
                    <div>
                      <h3 className="text-sm font-medium">Q&amp;A候補レビュー</h3>
                      <p className="mt-0.5 text-xs text-muted-foreground">自動整理された候補は承認するまで通常のQ&amp;Aに掲載されません。</p>
                    </div>
                  </div>
                  <Badge variant="outline">{queuedCandidates.length}</Badge>
                </div>
                <div className="space-y-3 p-3">
                  {qaCandidatesError ? (
                    <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                      {qaCandidatesError}
                    </div>
                  ) : null}
                  {qaCandidatesLoading && queuedCandidates.length === 0 ? (
                    <p className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />Q&amp;A候補を読み込み中…</p>
                  ) : queuedCandidates.length === 0 ? (
                    <p className="text-sm text-muted-foreground">確認待ちのQ&amp;A候補はありません。</p>
                  ) : (
                    <ul className="space-y-3">
                      {queuedCandidates.map((entry) => {
                        const busy = qaActionId === entry.id;
                        const source = qaOriginLabel(entry.origin ?? qaCandidateOrigin);
                        return (
                          <li key={entry.id} className="min-w-0 rounded-md border border-amber-500/30 p-3" data-testid={`project-qa-candidate-${entry.id}`}>
                            <div className="flex flex-wrap items-start justify-between gap-2">
                              <div className="min-w-0">
                                <h4 className="whitespace-pre-wrap break-words text-sm font-medium [overflow-wrap:anywhere]">{entry.question}</h4>
                                <p className="mt-2 whitespace-pre-wrap break-words text-sm text-muted-foreground [overflow-wrap:anywhere]">{entry.answer?.trim() || "回答未登録"}</p>
                              </div>
                              <div className="flex shrink-0 flex-wrap gap-1.5 text-xs">
                                <Badge variant="outline">{source}</Badge>
                                <Badge variant="outline">{qaStatusLabel(entry.status)}</Badge>
                                <Badge variant={qaReviewState(entry) === "rejected" ? "destructive" : "secondary"}>
                                  {qaReviewLabel(qaReviewState(entry))}
                                </Badge>
                              </div>
                            </div>
                            <div className="mt-3 flex flex-wrap gap-2 border-t border-border pt-3">
                              <Button type="button" size="sm" onClick={() => void updateQaCandidate(entry, "accept")} disabled={busy || qaActionId !== null}>
                                {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Check className="mr-2 size-4" />}
                                承認して掲載
                              </Button>
                              <Button type="button" size="sm" variant="outline" onClick={() => void updateQaCandidate(entry, "reject")} disabled={busy || qaActionId !== null}>
                                {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <X className="mr-2 size-4" />}
                                却下
                              </Button>
                              <Button type="button" size="sm" variant="ghost" className="text-destructive hover:text-destructive" onClick={() => void updateQaCandidate(entry, "delete")} disabled={busy || qaActionId !== null}>
                                {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Trash2 className="mr-2 size-4" />}
                                削除
                              </Button>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                  {canCleanupQa ? (
                    <div className="rounded-md border border-dashed border-border p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <p className="text-xs font-medium">既存の自動候補を整理</p>
                          <p className="mt-0.5 text-xs text-muted-foreground">承認済み・手動登録のQ&amp;Aは対象外です。まず件数を確認してから削除します。</p>
                        </div>
                        <Button type="button" size="sm" variant="outline" onClick={() => void runQaCleanup(true)} disabled={qaCleanupLoading}>
                          {qaCleanupLoading ? <Loader2 className="mr-2 size-4 animate-spin" /> : <RefreshCw className="mr-2 size-4" />}
                          対象を確認
                        </Button>
                      </div>
                      {qaCleanupState ? (
                        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                          <span>対象 {qaCleanupState.matched}件</span>
                          {qaCleanupState.deleted > 0 ? <span>削除済み {qaCleanupState.deleted}件</span> : null}
                          {qaCleanupState.dryRun && qaCleanupState.matched > 0 ? (
                            <Button type="button" size="sm" variant="destructive" onClick={() => void runQaCleanup(false)} disabled={qaCleanupLoading}>
                              {qaCleanupLoading ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Trash2 className="mr-2 size-4" />}
                              確認した候補を削除
                            </Button>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </section>
            ) : null}
          </>
        );
      })()}

      {selectedTable && (
        <div className="min-w-0 rounded-md border border-border bg-card p-3" data-testid="project-record-table-editor">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <h4 className="text-sm font-semibold">{selectedTable.name}</h4>
              {selectedTable.description && (
                <p className="text-xs text-muted-foreground">{selectedTable.description}</p>
              )}
            </div>
            <Button variant="outline" size="sm" onClick={() => setSelectedTableState(null)}>
              閉じる
            </Button>
          </div>
          <RecordTableEditor
            projectId={project.id}
            tableId={selectedTable.id}
            onClose={() => setSelectedTableState(null)}
            onChanged={load}
          />
        </div>
      )}
    </div>
  );
}
