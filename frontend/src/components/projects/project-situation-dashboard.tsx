"use client";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ArrowRight,
  BookOpen,
  Loader2,
  RefreshCw,
  UserRound,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import styles from "./project-situation-dashboard.module.css";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import { taskApi, type Task } from "@/lib/task-api";
import { KnowledgeCaptureReviewDialog } from "@/components/knowledge-capture/knowledge-capture-review-dialog";
import {
  normalizeKnowledgeCaptureCandidateList,
  normalizeKnowledgeCaptureCandidateDetail,
  type KnowledgeCaptureCandidate,
  type KnowledgeCaptureNotificationTarget,
} from "@/lib/knowledge-capture";
import {
  normalizeProjectOperations,
  projectWorkStateLabel,
  type ProjectWork,
} from "./project-situation-model";

type Source<T> = {
  key: string;
  data: T | null;
  error: string | null;
  checkedAt: string | null;
};
function useSource<T>(
  key: string,
  enabled: boolean,
  loader: (signal: AbortSignal) => Promise<T>,
) {
  const [source, setSource] = useState<Source<T>>({
    key: "",
    data: null,
    error: null,
    checkedAt: null,
  });
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void loader(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted)
          setSource({
            key,
            data,
            error: null,
            checkedAt: new Date().toISOString(),
          });
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setSource({
            key,
            data: null,
            error: "取得できませんでした",
            checkedAt: null,
          });
      });
    return () => controller.abort();
  }, [key, enabled, loader]);
  return source.key === key && enabled
    ? { ...source, loading: false }
    : { key, data: null, error: null, checkedAt: null, loading: enabled };
}
async function readJson(url: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(url, {
    signal,
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) throw new Error("取得できませんでした");
  return response.json();
}
function time(value: string | null | undefined): string {
  if (!value) return "更新日時未取得";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "更新日時未取得"
    : date.toLocaleString("ja-JP", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function taskState(status: string): string {
  return (
    (
      {
        todo: "未着手",
        open: "未着手",
        in_progress: "進行中",
        on_hold: "保留",
        review: "レビュー中",
        closed: "完了",
      } as Record<string, string>
    )[status] ?? status
  );
}
function taskAssignee(task: Task): string {
  return (
    (task.assignees ?? [])
      .map((person) => person.display_name || person.username)
      .filter(Boolean)
      .join(" / ") || "担当情報未取得"
  );
}
function preparationQuestions(task: Task): string[] {
  if (
    task.status === "closed" ||
    task.metadata?.agent_triage_status !== "needs_user"
  )
    return [];
  const questions = task.metadata.agent_triage_questions;
  return Array.isArray(questions)
    ? questions
        .filter(
          (value): value is string =>
            typeof value === "string" && Boolean(value.trim()),
        )
        .slice(0, 4)
    : [];
}

type Props = {
  projectId: string;
  projectName: string;
  description?: string | null;
  canWrite: boolean;
  taskBrowseOnly?: boolean;
  canManageSettings: boolean;
  onOpenInformation(): void;
  metrics?: ReactNode;
  children: ReactNode;
};
type AttentionRow = {
  id: string;
  kind: string;
  title: string;
  state: string;
  reason: string;
  assignee: string;
  updatedAt?: string | null;
  action: string;
  href?: string;
  candidate?: KnowledgeCaptureCandidate;
};

export function ProjectSituationDashboard(props: Props) {
  // Scope changes unmount all requests and selected review targets together.
  return <ProjectSituationContent key={props.projectId} {...props} />;
}
function ProjectSituationContent({
  projectId,
  projectName,
  description,
  canWrite,
  taskBrowseOnly = false,
  canManageSettings,
  onOpenInformation,
  metrics,
  children,
}: Props) {
  const taskBrowseSuffix = taskBrowseOnly
    ? `&browse_project_id=${encodeURIComponent(projectId)}`
    : "";
  const workSourceId = useId();
  const runtime = useOptionalRuntimeContext();
  const features = runtime?.runtimeFeatures?.application_features;
  const operationsKnown =
    typeof features?.virtual_company === "boolean" &&
    typeof features?.autonomous_agent_runtime === "boolean";
  const operationsEnabled =
    features?.virtual_company === true &&
    features?.autonomous_agent_runtime === true;
  const [revision, setRevision] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [showAllTasks, setShowAllTasks] = useState(false);
  const [review, setReview] = useState<{
    candidate: KnowledgeCaptureCandidate;
    target: KnowledgeCaptureNotificationTarget;
  } | null>(null);
  const [reviewLoading, setReviewLoading] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const reviewController = useRef<AbortController | null>(null);
  useEffect(() => () => reviewController.current?.abort(), []);
  const key = `${projectId}:${revision}`;
  const loadTasks = useCallback(
    async (signal: AbortSignal) => {
      const tasks = await taskApi.listTasks(
        // Projects also exposes explicitly readable admin-only targets.
        // Re-authorize this concrete view rather than using participation scope.
        { browse_project_id: projectId },
        { signal },
      );
      if (tasks.some((task) => task.project_id !== projectId))
        throw new Error("タスクのProjectを確認できませんでした");
      return tasks;
    },
    [projectId],
  );
  const loadOperations = useCallback(
    async (signal: AbortSignal) => {
      const query = new URLSearchParams({
        project_id: projectId,
        limit: "100",
      });
      return normalizeProjectOperations(
        await readJson(
          `/api/python-proxy/operations/command-center?${query}`,
          signal,
        ),
        projectId,
      );
    },
    [projectId],
  );
  const loadCandidates = useCallback(
    async (status: "needs_user" | "draft_ready", signal: AbortSignal) => {
      const body = await readJson(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge-capture/candidates?status=${status}&limit=50`,
        signal,
      );
      if (
        !body ||
        typeof body !== "object" ||
        !Array.isArray((body as { items?: unknown }).items)
      )
        throw new Error("候補の形式を確認できませんでした");
      const candidates = normalizeKnowledgeCaptureCandidateList(body).items;
      return {
        items: candidates.filter(
          (item) => item.projectId === projectId && item.status === status,
        ),
        limited: candidates.length >= 50,
      };
    },
    [projectId],
  );
  const loadQuestions = useCallback(
    (signal: AbortSignal) => loadCandidates("needs_user", signal),
    [loadCandidates],
  );
  const loadDrafts = useCallback(
    (signal: AbortSignal) => loadCandidates("draft_ready", signal),
    [loadCandidates],
  );
  const tasks = useSource(key, true, loadTasks);
  const operations = useSource(
    `${key}:${operationsEnabled}`,
    operationsEnabled,
    loadOperations,
  );
  const questions = useSource(key, true, loadQuestions);
  const drafts = useSource(key, true, loadDrafts);

  const openCandidate = useCallback(
    async (item: KnowledgeCaptureCandidate) => {
      reviewController.current?.abort();
      const controller = new AbortController();
      reviewController.current = controller;
      setReviewLoading(item.id);
      setReviewError(null);
      setReview(null);
      try {
        const body = await readJson(
          `/api/projects/${encodeURIComponent(projectId)}/knowledge-capture/candidates/${encodeURIComponent(item.id)}`,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        const candidate = normalizeKnowledgeCaptureCandidateDetail(body);
        if (
          !candidate ||
          candidate.projectId !== projectId ||
          candidate.id !== item.id
        )
          throw new Error("候補の対象・版を確認できませんでした");
        const isQuestion =
          candidate.status === "needs_user" &&
          candidate.question?.status === "pending";
        const isDraft =
          candidate.status === "draft_ready" && candidate.question === null;
        if (!isQuestion && !isDraft)
          throw new Error(
            "この候補は更新されています。状況を再取得してください",
          );
        setReview({
          candidate,
          target: {
            projectId,
            candidateId: candidate.id,
            candidateVersion: candidate.version,
            notificationType: isQuestion
              ? "knowledge_capture_question"
              : "knowledge_capture_draft",
            questionId: isQuestion ? candidate.question!.id : null,
          },
        });
      } catch (error) {
        if (!controller.signal.aborted)
          setReviewError(
            error instanceof Error ? error.message : "候補を開けませんでした",
          );
      } finally {
        if (!controller.signal.aborted) setReviewLoading(null);
      }
    },
    [projectId],
  );

  const attention = useMemo(() => {
    const rows: AttentionRow[] = [];
    for (const candidate of [
      ...(questions.data?.items ?? []),
      ...(drafts.data?.items ?? []),
    ]) {
      const question = candidate.status === "needs_user";
      rows.push({
        id: `knowledge:${candidate.id}`,
        kind: question ? "ナレッジの質問" : "ナレッジ候補",
        title:
          candidate.draft?.title ||
          candidate.seedTask?.title ||
          `候補 ${candidate.id.slice(0, 8)}`,
        state: question ? "回答待ち" : "採用判断待ち",
        reason: question
          ? candidate.question?.prompt || "質問内容は詳細で確認できます"
          : "Docsへ保存する前の確認が必要です",
        assignee: "担当情報未取得",
        action: question
          ? canWrite
            ? "質問に回答"
            : "質問を確認"
          : "候補を確認",
        candidate,
      });
    }
    for (const action of operations.data?.approvals ?? [])
      rows.push({
        id: `approval:${action.id}`,
        kind: "外部操作",
        title: action.title,
        state: action.humanApproval ? "承認待ち" : action.state,
        reason: action.reason || "判断内容の詳細は未取得です",
        assignee: action.assignee || "担当情報未取得",
        action:
          canWrite && action.humanApproval
            ? "承認内容を確認"
            : "操作の詳細を見る",
        href: canWrite ? action.href : `${action.href}&read_only=1`,
      });
    for (const work of operations.data?.attention ?? [])
      rows.push({
        id: `work:${work.id}`,
        kind: "実行",
        title: work.title,
        state: projectWorkStateLabel(work.state),
        reason: work.reason || "理由の詳細は未取得です",
        assignee: work.assignee || "担当情報未取得",
        updatedAt: work.updatedAt,
        action: "実行の詳細を見る",
        href: work.href,
      });
    for (const task of tasks.data ?? []) {
      const pending = preparationQuestions(task);
      if (pending.length)
        rows.push({
          id: `preparation:${task.id}`,
          kind: task.parent_task_id ? "子タスクの準備" : "準備の確認",
          title: task.title,
          state: "準備時の質問あり",
          reason: pending.join(" / "),
          assignee: taskAssignee(task),
          updatedAt:
            typeof task.metadata.agent_triage_checked_at === "string"
              ? task.metadata.agent_triage_checked_at
              : null,
          action: canWrite ? "タスクを確認" : "タスクの詳細を見る",
          href: `/tasks?detail=${encodeURIComponent(task.id)}${taskBrowseSuffix}`,
        });
    }
    return rows;
  }, [
    questions.data,
    drafts.data,
    operations.data,
    tasks.data,
    canWrite,
    taskBrowseSuffix,
  ]);
  const rootTasks = (tasks.data ?? [])
    .filter((task) => !task.parent_task_id)
    .sort(
      (left, right) =>
        Number(left.status === "closed") - Number(right.status === "closed"),
    );
  const completed = rootTasks.filter((task) => task.status === "closed").length;
  const inProgress = rootTasks.filter(
    (task) => task.status === "in_progress",
  ).length;
  const visibleTasks = showAllTasks ? rootTasks : rootTasks.slice(0, 6);
  const workByTask = new Map<string, ProjectWork>();
  // Attention is paged independently and can contain a task's Work beyond
  // current_work. Do not label that already-received association as absent.
  for (const work of [
    ...(operations.data?.work ?? []),
    ...(operations.data?.attention ?? []),
  ])
    if (work.taskId && !workByTask.has(work.taskId))
      workByTask.set(work.taskId, work);
  const noCoverage =
    !operationsEnabled ||
    !tasks.data ||
    !questions.data ||
    !drafts.data ||
    (operationsEnabled &&
      (!operations.data?.available ||
        operations.data.workPage.coverage !== "complete"));
  const hasMore =
    questions.data?.limited || drafts.data?.limited || operations.data?.hasMore;
  const sourceStatus = [
    ["タスク", tasks.loading ? "取得中" : tasks.error || "取得済み"],
    ["回答候補", questions.loading ? "取得中" : questions.error || "取得済み"],
    ["採用候補", drafts.loading ? "取得中" : drafts.error || "取得済み"],
    [
      "実行・承認",
      !operationsEnabled
        ? operationsKnown
          ? "この環境では利用できません"
          : "機能情報未取得"
        : operations.loading
          ? "取得中"
          : operations.error ||
            (!operations.data?.available ? "状態未取得" : "取得済み範囲"),
    ],
  ];

  const workSourceStatus = !operationsEnabled
    ? operationsKnown
      ? "この環境ではAI実行状況を利用できません"
      : "AI実行の機能情報を確認できていません"
    : operations.loading
      ? "AI実行状況を取得中です…"
      : operations.error
        ? "AI実行状況の取得に失敗しました。「状況を更新」で再取得できます。"
        : !operations.data?.available
          ? "AI実行状況を確認できません"
          : operations.data.workPage.coverage === "partial"
            ? "AI実行情報は一部取得です。"
            : operations.data.workPage.coverage === "unknown"
              ? "AI実行一覧の取得範囲を確認できません。表示は取得済みの情報です。"
              : "AI実行情報は取得時点の全件を取得済みです。";
  const hasUnmatchedTasks = visibleTasks.some(
    (task) => !workByTask.has(task.id),
  );
  const attentionLoading =
    tasks.loading || questions.loading || drafts.loading || operations.loading;

  return (
    <div
      className="@container/project-situation min-w-0 space-y-4"
      data-testid="project-situation-dashboard"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-6 gap-y-3 rounded-lg border border-border bg-card p-4">
        <div className="min-w-0 space-y-1">
          <h2 className="text-base font-semibold">プロジェクトの状況</h2>
          <p className="text-xs text-muted-foreground">
            取得時点の情報
            {tasks.checkedAt ? ` · 確認 ${time(tasks.checkedAt)}` : ""}
          </p>
        </div>
        <dl className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <div className="flex items-baseline gap-2">
            <dt>進行中タスク</dt>
            <dd className="text-lg font-semibold tabular-nums text-primary">
              {tasks.data ? inProgress : "未取得"}
            </dd>
          </div>
          <div className="flex items-baseline gap-2">
            <dt>完了タスク</dt>
            <dd className="text-lg font-semibold tabular-nums">
              {tasks.data ? `${completed} / ${rootTasks.length}` : "未取得"}
            </dd>
          </div>
          <div className="flex items-baseline gap-2">
            <dt>確認・対応</dt>
            <dd className="font-semibold text-amber-700 dark:text-amber-300">
              表示中 {attention.length}件{noCoverage ? "・未取得あり" : ""}
            </dd>
          </div>
        </dl>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="ml-auto h-9 text-sm"
          onClick={() => setRevision((value) => value + 1)}
        >
          <RefreshCw className="mr-1 size-3.5" />
          状況を更新
        </Button>
      </div>

      <section
        className={`@container/project-attention min-w-0 rounded-lg border bg-card px-4 py-3 ${attention.length ? "border-amber-500/30" : "border-border"}`}
        aria-label={
          canWrite ? "あなたの対応が必要" : "確認が必要な項目（閲覧のみ）"
        }
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <UserRound className="size-4 text-amber-600" />
            {canWrite ? "あなたの対応が必要" : "確認が必要な項目（閲覧のみ）"}
          </h2>
          {attention.length ? (
            <span className="text-xs text-muted-foreground">
              承認・回答と、状態の確認が必要な項目
            </span>
          ) : (
            <p className="text-sm text-muted-foreground">
              {attentionLoading
                ? "対応項目を取得中です…"
                : "取得できた範囲に対応項目はありません。"}
            </p>
          )}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {sourceStatus.map(([label, status]) => (
            <span
              key={label}
              className={
                status.includes("取得できません")
                  ? "text-destructive"
                  : undefined
              }
            >
              {label}: {status}
            </span>
          ))}
          {!operationsEnabled ? (
            <span>
              手動の外部操作は{" "}
              <Link
                href={
                  canWrite
                    ? "/operations?tab=actions"
                    : "/operations?tab=actions&read_only=1"
                }
                className="text-primary hover:underline"
              >
                外部操作の一覧
              </Link>{" "}
              で確認できます。
            </span>
          ) : null}
        </div>
        {reviewError ? (
          <p
            role="alert"
            className="mb-2 rounded border border-destructive/30 p-2 text-sm text-destructive"
          >
            {reviewError}
          </p>
        ) : null}
        {attention.length ? (
          <ul className="mt-3 divide-y divide-border rounded-md border border-border">
            {(showAll ? attention : attention.slice(0, 6)).map((item) => (
              <li
                key={item.id}
                className="grid min-w-0 gap-3 p-3 @min-[56rem]/project-attention:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
              >
                <div className="min-w-0">
                  <span className="mb-1 inline-block text-xs text-muted-foreground">
                    {item.kind}
                  </span>
                  <h3 className="text-sm font-semibold [overflow-wrap:anywhere]">
                    {item.title}
                  </h3>
                  <p className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                    {item.assignee}
                    {item.updatedAt ? ` · ${time(item.updatedAt)}` : ""}
                  </p>
                </div>
                <div className="min-w-0">
                  <Badge
                    variant="outline"
                    className="mb-1 max-w-full whitespace-normal text-xs"
                  >
                    {item.state}
                  </Badge>
                  <p className="line-clamp-3 text-sm leading-5 [overflow-wrap:anywhere]">
                    {item.reason}
                  </p>
                </div>
                <div className="flex items-center justify-start @min-[56rem]/project-attention:justify-end">
                  {item.candidate ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={reviewLoading !== null}
                      onClick={() => void openCandidate(item.candidate!)}
                    >
                      {reviewLoading === item.candidate.id ? (
                        <Loader2 className="mr-1 size-3 animate-spin" />
                      ) : null}
                      {item.action}
                      <ArrowRight className="ml-1 size-3" />
                    </Button>
                  ) : item.href ? (
                    <Link
                      href={item.href}
                      className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-sm text-primary hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {item.action}
                      <ArrowRight className="size-3" />
                    </Link>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        ) : null}
        {attention.length > 6 ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="mt-2"
            onClick={() => setShowAll((value) => !value)}
          >
            {showAll
              ? "一覧を短く表示"
              : `取得済みの${attention.length}項目をすべて見る`}
          </Button>
        ) : null}
        {hasMore ? (
          <p className="mt-2 text-xs text-muted-foreground">
            表示上限があります。ナレッジは状態ごとに50件、実行は最大100件です。続きは各正規画面で確認してください。
          </p>
        ) : null}
      </section>

      <div className="grid min-w-0 grid-cols-1 items-start gap-4 @min-[76rem]/project-situation:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-4" data-testid="project-situation-main">
          <section
            className="@container/project-work min-w-0 rounded-lg border border-border bg-card p-4"
            aria-label="作業の状況"
          >
            <div className="mb-2 flex items-center justify-between gap-2">
              <h2 className="text-base font-semibold">作業の状況</h2>
              <Link
                href={`/tasks?${taskBrowseOnly ? "browse_project_id" : "project_id"}=${encodeURIComponent(projectId)}`}
                className="text-sm text-primary"
              >
                タスク一覧を開く
              </Link>
            </div>
            <div
              id={workSourceId}
              className="mb-3 space-y-1 text-xs text-muted-foreground"
            >
              <p
                role={operations.error ? "alert" : "status"}
                className={operations.error ? "text-destructive" : undefined}
              >
                {workSourceStatus}
                {operations.data?.available &&
                operations.data.workPage.coverage !== "complete" ? (
                  <>
                    {" "}
                    <Link
                      href={`/operations?tab=work&scope=project&project_id=${encodeURIComponent(projectId)}`}
                      className="text-primary hover:underline"
                    >
                      Operationsで確認
                    </Link>
                  </>
                ) : null}
              </p>
              {operations.data?.available && hasUnmatchedTasks ? (
                <p>
                  AI実行欄のないタスクは、取得済みの一覧に関連するAI作業がありません。
                </p>
              ) : null}
            </div>
            {tasks.loading ? (
              <p role="status" className="py-3 text-sm text-muted-foreground">
                タスクを取得中…
              </p>
            ) : tasks.error ? (
              <p role="alert" className="py-3 text-sm text-destructive">
                タスクを取得できませんでした。「状況を更新」で再取得できます。
              </p>
            ) : !rootTasks.length ? (
              <p className="py-3 text-sm text-muted-foreground">
                このProjectのタスクはありません
              </p>
            ) : (
              <ul
                className="grid min-w-0 grid-cols-1 gap-3 @min-[52rem]/project-work:grid-cols-2"
                aria-describedby={workSourceId}
              >
                {visibleTasks.map((task) => {
                  const work = workByTask.get(task.id);
                  const prep = preparationQuestions(task);
                  return (
                    <li
                      key={task.id}
                      className="min-w-0 rounded-md border border-border p-3"
                    >
                      <div className="flex min-w-0 items-start gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="mb-1 flex flex-wrap items-center gap-2">
                            <Badge variant="outline" className="text-xs">
                              {taskState(task.status)}
                            </Badge>
                            <h3 className="min-w-0 text-base font-semibold [overflow-wrap:anywhere]">
                              {task.title}
                            </h3>
                          </div>
                          <p className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                            <span>{taskAssignee(task)}</span>
                            <span>更新 {time(task.updated_at)}</span>
                          </p>
                        </div>
                        <Link
                          href={`/tasks?detail=${encodeURIComponent(task.id)}${taskBrowseSuffix}`}
                          className="shrink-0 rounded-sm text-sm text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          詳細 <span aria-hidden="true">›</span>
                        </Link>
                      </div>
                      {work ? (
                        <div className="mt-2 min-w-0 border-t border-border pt-2 text-sm">
                          <p>
                            AI実行: {projectWorkStateLabel(work.state)}
                            {work.phase ? ` · ${work.phase}` : ""}
                          </p>
                          {work.reason ? (
                            <p className="mt-1 text-muted-foreground [overflow-wrap:anywhere]">
                              {work.reason}
                            </p>
                          ) : [
                              "blocked",
                              "failed",
                              "uncertain",
                              "dead_letter",
                            ].includes(work.state) ? (
                            <p className="mt-1 text-muted-foreground">
                              理由の詳細は未取得です
                            </p>
                          ) : null}
                        </div>
                      ) : null}
                      {prep.length ? (
                        <p className="mt-2 text-sm text-amber-700 dark:text-amber-300">
                          準備時の質問あり
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
            {rootTasks.length > 6 ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="mt-2"
                onClick={() => setShowAllTasks((value) => !value)}
              >
                {showAllTasks
                  ? "一覧を短く表示"
                  : `取得済みの${rootTasks.length}タスクをすべて見る`}
              </Button>
            ) : null}
            {operations.data?.available &&
            operations.data.work.some((work) => !work.taskId) ? (
              <div className="mt-3 border-t border-border pt-2">
                <h3 className="mb-1 text-sm font-semibold">タスク以外の実行</h3>
                {operations.data.work
                  .filter((work) => !work.taskId)
                  .slice(0, 4)
                  .map((work) => (
                    <Link
                      key={work.id}
                      href={work.href}
                      className="flex min-w-0 justify-between gap-2 py-1.5 text-sm"
                    >
                      <span className="truncate">{work.title}</span>
                      <span className="shrink-0 text-muted-foreground">
                        {projectWorkStateLabel(work.state)} ›
                      </span>
                    </Link>
                  ))}
              </div>
            ) : null}
          </section>
          {children}
        </div>
        <aside
          className="min-w-0 space-y-4"
          data-testid="project-situation-summary"
        >
          <section className="rounded-lg border border-border bg-card p-4">
            <div className="mb-2 flex items-center justify-between gap-2">
              <h2 className="flex items-center gap-2 text-base font-semibold">
                <BookOpen className="size-4" />
                今のプロジェクト
              </h2>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-8 px-2 text-sm"
                onClick={onOpenInformation}
              >
                案件情報 ›
              </Button>
            </div>
            <p className="text-sm font-medium">{projectName}</p>
            {description ? (
              <p className="mt-1 whitespace-pre-wrap text-sm leading-5 text-muted-foreground [overflow-wrap:anywhere]">
                {description}
              </p>
            ) : (
              <p className="mt-1 text-sm text-muted-foreground">
                概要は案件情報で確認できます
              </p>
            )}
            {tasks.data && rootTasks.length > 0 ? (
              <div className="mt-3">
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span>タスクの完了</span>
                  <span>
                    {completed} / {rootTasks.length}
                  </span>
                </div>
                <progress
                  className="h-1.5 w-full accent-[var(--primary)]"
                  value={completed}
                  max={rootTasks.length}
                  aria-label="タスクの完了"
                />
              </div>
            ) : null}
          </section>
          <div key={revision} className={styles.metrics}>
            {metrics}
          </div>
        </aside>
      </div>
      <KnowledgeCaptureReviewDialog
        open={review !== null}
        notificationId={null}
        target={review?.target ?? null}
        candidate={review?.candidate ?? null}
        readOnly={!canWrite}
        canPublish={canManageSettings}
        onOpenChange={(open) => {
          if (!open) setReview(null);
        }}
        onDidOpen={() => {}}
        onResolved={() => {
          setReview(null);
          setRevision((value) => value + 1);
        }}
      />
    </div>
  );
}
