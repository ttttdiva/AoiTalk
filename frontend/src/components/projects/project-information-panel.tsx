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
  Database,
  ListChecks,
  Loader2,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Table2,
} from "lucide-react";

type ProjectInfo = {
  id: string;
  name: string;
  description?: string | null;
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
  status: string;
  review_state: string;
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

export function ProjectInformationPanel({ project }: { project: ProjectInfo }) {
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
  const requestGenerationRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);

  const data = loadedData?.projectId === project.id ? loadedData.response : null;
  const error = errorState?.projectId === project.id ? errorState.message : "";
  const loading =
    loadingProjectId === project.id || (data === null && error.length === 0);
  const selectedTableId =
    selectedTableState?.projectId === project.id
      ? selectedTableState.tableId
      : null;

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
    void load();
    return () => {
      requestControllerRef.current?.abort();
    };
  }, [load]);

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
    <div className="flex min-h-0 flex-1 flex-col gap-4 pb-8">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
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
        <section className="min-h-[560px] overflow-hidden rounded-md border border-border bg-card">
          <DocsWorkspace
            key={`${project.id}:${data.node.id}`}
            initialNodeId={data.node.id}
          />
        </section>
      )}

      <DailyIntakeSection key={project.id} project={project} onApplied={load} />

      <div className="grid gap-4 xl:grid-cols-2">
          <section className="rounded-md border border-border bg-card">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <div className="text-sm font-medium">Q&A</div>
              <Badge variant="secondary">{data?.qa_entries.length ?? 0}</Badge>
            </div>
            <div className="max-h-72 overflow-auto p-3">
              {(data?.qa_entries ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">Q&Aはまだありません。</p>
              ) : (
                <div className="space-y-3">
                  {data?.qa_entries.map((entry) => (
                    <div key={entry.id} className="rounded-md border p-3">
                      <div className="text-sm font-medium">{entry.question}</div>
                      {entry.answer && (
                        <p className="mt-2 text-sm text-muted-foreground">{entry.answer}</p>
                      )}
                      <div className="mt-2 flex gap-2">
                        <Badge variant="outline">{entry.status}</Badge>
                        <Badge variant="outline">{entry.review_state}</Badge>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>

          <section className="rounded-md border border-border bg-card">
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
                        {table.name}
                      </div>
                      {table.description && (
                        <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
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

      {selectedTable && (
        <div className="rounded-md border border-border bg-card p-3">
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
