export type KnowledgeCaptureMode = "off" | "suggest" | "auto";

export const KNOWLEDGE_CAPTURE_MODE_OPTIONS: ReadonlyArray<{
  value: KnowledgeCaptureMode;
  label: string;
  description: string;
}> = [
  {
    value: "off",
    label: "オフ",
    description: "解決案件からナレッジ候補を作成しません。",
  },
  {
    value: "suggest",
    label: "提案",
    description: "候補を作成し、確認してからDocsへ保存します。",
  },
  {
    value: "auto",
    label: "自動",
    description: "確信度の高い候補だけ自動保存し、それ以外は提案します。",
  },
];

export type KnowledgeCaptureCandidateStatus =
  | "researching"
  | "needs_user"
  | "draft_ready"
  | "approved"
  | "published"
  | "dismissed"
  | "superseded"
  | string;

export type KnowledgeCaptureQuestionOption = {
  id: string;
  label: string;
};

export type KnowledgeCaptureQuestion = {
  id: string;
  prompt: string;
  options: KnowledgeCaptureQuestionOption[];
  allowFreeText: boolean;
  status: string | null;
  candidateVersion: number | null;
};

export type KnowledgeCaptureDraftSection = {
  key?: string;
  title: string;
  body: string;
};

export type KnowledgeCaptureDraft = {
  title: string;
  sections: KnowledgeCaptureDraftSection[];
  evidenceSummary: string;
};

export type KnowledgeCapturePublished = {
  docsNodeId: string | null;
  sourceTaskId: string | null;
  sourceTaskTitle: string | null;
};

export type KnowledgeCaptureSeedTask = {
  id: string;
  title: string | null;
  status: string | null;
  completedAt: string | null;
};

export type KnowledgeCaptureReviewChatSession = {
  id: string;
  title: string | null;
  relation: "direct_reference" | "supporting_evidence";
};

export type KnowledgeCapturePublicationTarget = {
  action: "create" | "update" | "no_change";
  nodeId: string | null;
  title: string | null;
};

export type KnowledgeCaptureReviewContext = {
  reviewReason: string | null;
  sourceTask: KnowledgeCaptureSeedTask | null;
  chatSessions: KnowledgeCaptureReviewChatSession[];
  publicationTarget: KnowledgeCapturePublicationTarget | null;
};

export type KnowledgeCaptureReviewExchange = {
  role: "user" | "assistant";
  text: string;
};

export type KnowledgeCaptureReviewResponse = {
  candidate_version: number;
  action: "keep_question" | "rephrase_question" | "discard_candidate";
  reply: string;
  rephrased_question: string | null;
};

export type KnowledgeCaptureNotificationTarget = {
  notificationType:
    | "knowledge_capture_question"
    | "knowledge_capture_draft";
  projectId: string;
  candidateId: string;
  candidateVersion: number;
  questionId: string | null;
};

export const KNOWLEDGE_CAPTURE_OPEN_MODE = "knowledge_capture_review" as const;

export type KnowledgeCaptureCandidate = {
  id: string;
  projectId: string | null;
  status: KnowledgeCaptureCandidateStatus;
  version: number;
  publicationGuardAdoptionRequired: boolean;
  question: KnowledgeCaptureQuestion | null;
  questions: KnowledgeCaptureQuestion[];
  draft: KnowledgeCaptureDraft | null;
  published: KnowledgeCapturePublished | null;
  seedTask: KnowledgeCaptureSeedTask | null;
  reviewContext: KnowledgeCaptureReviewContext | null;
};

type RecordValue = Record<string, unknown>;

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value.trim());
}

function isRecord(value: unknown): value is RecordValue {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function stringValue(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function nullableString(...values: unknown[]): string | null {
  const value = stringValue(...values);
  return value || null;
}

function boundedNullableString(value: unknown, limit: number): string | null {
  const normalized = stringValue(value);
  return normalized ? normalized.slice(0, limit) : null;
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : fallback;
}

function booleanValue(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function firstRecord(...values: unknown[]): RecordValue | null {
  return values.find(isRecord) ?? null;
}

function unwrapObject(body: unknown, keys: string[]): RecordValue | null {
  if (!isRecord(body)) return null;
  for (const key of keys) {
    if (isRecord(body[key])) return body[key];
  }
  return body;
}

export function normalizeKnowledgeCaptureMode(
  body: unknown,
): KnowledgeCaptureMode {
  const root = isRecord(body) ? body : {};
  const settings = firstRecord(
    root.settings,
    root.config,
    root.knowledge_capture,
    root.knowledgeCapture,
  );
  const raw = stringValue(
    root.mode,
    root.knowledge_capture_mode,
    root.knowledgeCaptureMode,
    settings?.mode,
    settings?.knowledge_capture_mode,
  ).toLowerCase();
  return raw === "off" || raw === "auto" ? raw : "suggest";
}

function normalizeQuestion(
  value: unknown,
  fallbackId?: unknown,
  fallbackOptions?: unknown,
  fallbackAllowFreeText?: unknown,
): KnowledgeCaptureQuestion | null {
  const question = isRecord(value) ? value : null;
  const questionText = typeof value === "string" ? value.trim() : "";
  if (!question && !questionText) return null;

  const id = stringValue(
    question?.id,
    question?.question_id,
    question?.questionId,
    fallbackId,
  );
  const prompt = stringValue(
    question?.message,
    question?.prompt,
    question?.question,
    question?.text,
    question?.title,
    questionText,
  );
  if (!id || !prompt) return null;

  const status = nullableString(question?.status, question?.state)?.toLowerCase() ?? null;
  const rawCandidateVersion =
    question?.candidate_version ?? question?.candidateVersion;
  const candidateVersion =
    typeof rawCandidateVersion === "number" &&
    Number.isInteger(rawCandidateVersion) &&
    rawCandidateVersion > 0
      ? rawCandidateVersion
      : null;

  const rawOptions = Array.isArray(question?.options)
    ? question.options
    : Array.isArray(question?.choices)
      ? question.choices
      : Array.isArray(fallbackOptions)
        ? fallbackOptions
        : [];
  const options = rawOptions.slice(0, 8).map((option) => {
    if (typeof option === "string") {
      const label = option.trim();
      return label ? { id: label, label } : null;
    }
    if (!isRecord(option)) return null;
    const label = stringValue(option.label, option.text, option.value, option.title);
    return label
      ? { id: stringValue(option.id, option.option_id, option.value, label), label }
      : null;
  }).filter((option): option is { id: string; label: string } => option !== null);

  const rawAllowFreeText =
    question?.allow_free_text ??
    question?.allowFreeText ??
    question?.free_text_allowed ??
    question?.freeTextAllowed ??
    fallbackAllowFreeText;
  return {
    id,
    prompt,
    options,
    allowFreeText: rawAllowFreeText !== false,
    status,
    candidateVersion,
  };
}

function normalizeDraft(value: unknown): KnowledgeCaptureDraft | null {
  const draft = isRecord(value) ? value : null;
  if (!draft) return null;

  const title = stringValue(draft.title, draft.name);
  const rawSections = Array.isArray(draft.sections)
    ? draft.sections
    : Array.isArray(draft.structured_sections)
      ? draft.structured_sections
      : Array.isArray(draft.structuredSections)
        ? draft.structuredSections
        : [
            ["problem", "課題"],
            ["preconditions", "前提"],
            ["symptoms", "症状"],
            ["root_cause", "原因"],
            ["resolution", "解決"],
            ["procedure", "手順"],
            ["verification", "確認方法"],
            ["pitfalls", "注意点"],
            ["environment_constraints", "環境条件"],
            ["known_uncertainty", "既知の不確実性"],
          ]
            .map(([key, title]) => {
              const raw = draft[key];
              if (raw === undefined || raw === null || raw === "") return null;
              const body = Array.isArray(raw)
                ? raw
                    .map((item) => {
                      if (typeof item === "string") return item;
                      if (isRecord(item)) {
                        return stringValue(item.text, item.body, item.step);
                      }
                      return "";
                    })
                    .filter(Boolean)
                    .join("\n")
                : String(raw);
              return body.trim() ? { key, title, body: body.trim() } : null;
            })
            .filter(
              (section): section is { key: string; title: string; body: string } =>
                section !== null,
            );
  const sections = rawSections
    .slice(0, 10)
    .map((section) => {
      if (typeof section === "string") {
        return { title: "", body: section.trim() };
      }
      if (!isRecord(section)) return null;
      const body = stringValue(
        section.body,
        section.content,
        section.text,
        Array.isArray(section.items)
          ? section.items
              .filter((item): item is string => typeof item === "string")
              .join("\n")
          : "",
      );
      const sectionTitle = stringValue(
        section.title,
        section.heading,
        section.name,
      );
      return body || sectionTitle
        ? {
            key: stringValue(section.key, section.field, section.slug) || undefined,
            title: sectionTitle,
            body,
          }
        : null;
    })
    .filter((section): section is KnowledgeCaptureDraftSection => section !== null);
  const evidence = firstRecord(
    draft.evidence_summary,
    draft.evidenceSummary,
    draft.evidence_digest,
    draft.evidenceDigest,
  );
  const evidenceSummary = stringValue(
    draft.evidence_summary,
    draft.evidenceSummary,
    draft.evidence_digest,
    draft.evidenceDigest,
    evidence?.summary,
    evidence?.label,
    Array.isArray(draft.source_evidence_ids)
      ? `根拠 ${draft.source_evidence_ids.length}件`
      : "",
    Array.isArray(draft.evidence_ids)
      ? `根拠 ${draft.evidence_ids.length}件`
      : "",
    typeof evidence?.count === "number" ? `根拠 ${evidence.count}件` : "",
  );
  if (!title && sections.length === 0 && !evidenceSummary) return null;
  return { title, sections, evidenceSummary };
}

function normalizePublished(value: unknown): KnowledgeCapturePublished | null {
  const published = isRecord(value) ? value : null;
  if (!published) return null;
  const docs = firstRecord(
    published.docs,
    published.document,
    published.published_docs,
  );
  const task = firstRecord(published.source_task, published.sourceTask, published.task);
  const docsNodeId = nullableString(
    published.docs_node_id,
    published.docsNodeId,
    published.published_node_id,
    published.publishedNodeId,
    published.node_id,
    published.nodeId,
    docs?.id,
    docs?.node_id,
  );
  const sourceTaskId = nullableString(
    published.source_task_id,
    published.sourceTaskId,
    published.seed_task_id,
    published.seedTaskId,
    published.task_id,
    published.taskId,
    task?.id,
    task?.task_id,
  );
  const sourceTaskTitle = nullableString(
    published.source_task_title,
    published.sourceTaskTitle,
    task?.title,
    task?.name,
  );
  if (!docsNodeId && !sourceTaskId && !sourceTaskTitle) return null;
  return { docsNodeId, sourceTaskId, sourceTaskTitle };
}

function normalizeSeedTask(value: unknown): KnowledgeCaptureSeedTask | null {
  const task = isRecord(value) ? value : null;
  const id = stringValue(task?.id, task?.task_id, task?.taskId);
  if (!isUuid(id)) return null;
  return {
    id,
    title: boundedNullableString(task?.title, 240),
    status: boundedNullableString(task?.status, 64),
    completedAt:
      boundedNullableString(task?.completed_at, 64) ??
      boundedNullableString(task?.completedAt, 64),
  };
}

function normalizeReviewContext(value: unknown): KnowledgeCaptureReviewContext | null {
  const context = isRecord(value) ? value : null;
  if (!context) return null;

  const rawSessions = Array.isArray(context.chat_sessions)
    ? context.chat_sessions
    : Array.isArray(context.chatSessions)
      ? context.chatSessions
      : [];
  const chatSessions = rawSessions
    .slice(0, 8)
    .map((value) => {
      const session = isRecord(value) ? value : null;
      const id = stringValue(
        session?.id,
        session?.session_id,
        session?.sessionId,
      );
      const relation = stringValue(session?.relation);
      if (
        !isUuid(id) ||
        (relation !== "direct_reference" && relation !== "supporting_evidence")
      ) {
        return null;
      }
      return {
        id,
        title: boundedNullableString(session?.title, 240),
        relation,
      };
    })
    .filter(
      (session): session is KnowledgeCaptureReviewChatSession =>
        session !== null,
    );

  const rawPublicationTarget = firstRecord(
    context.publication_target,
    context.publicationTarget,
  );
  let publicationTarget: KnowledgeCapturePublicationTarget | null = null;
  if (rawPublicationTarget) {
    const action = stringValue(rawPublicationTarget.action);
    if (action === "create" || action === "no_change") {
      publicationTarget = {
        action,
        nodeId: null,
        title: boundedNullableString(rawPublicationTarget.title, 240),
      };
    } else if (action === "update") {
      const nodeId = stringValue(
        rawPublicationTarget.node_id,
        rawPublicationTarget.nodeId,
      );
      if (isUuid(nodeId)) {
        publicationTarget = {
          action,
          nodeId,
          title: boundedNullableString(rawPublicationTarget.title, 240),
        };
      }
    }
  }

  return {
    reviewReason: boundedNullableString(
      context.review_reason ?? context.reviewReason,
      1000,
    ),
    sourceTask: normalizeSeedTask(context.source_task ?? context.sourceTask),
    chatSessions,
    publicationTarget,
  };
}

export function normalizeKnowledgeCaptureCandidate(
  body: unknown,
  fallbackProjectId?: string,
): KnowledgeCaptureCandidate | null {
  const root = unwrapObject(body, ["candidate", "item", "result"]);
  if (!root) return null;
  const id = stringValue(root.id, root.candidate_id, root.candidateId);
  if (!id) return null;

  const rawQuestions = Array.isArray(root.questions) ? root.questions : [];
  const questionCandidates = [
    root.pending_question,
    root.pendingQuestion,
    root.blocking_question,
    root.blockingQuestion,
    ...rawQuestions,
    root.question,
  ];
  const pendingQuestion = questionCandidates.find((value) => {
    if (!isRecord(value)) return typeof value === "string";
    const status = stringValue(value.status, value.state).toLowerCase();
    return !status || status === "pending";
  });
  const question = normalizeQuestion(
    pendingQuestion,
    root.question_id ?? root.questionId,
    root.question_options ?? root.questionOptions ?? root.options,
    root.allow_free_text ?? root.allowFreeText,
  );
  const questions = rawQuestions
    .map((value) => normalizeQuestion(value))
    .filter((value): value is KnowledgeCaptureQuestion => value !== null);
  if (question && !questions.some((value) => value.id === question.id)) {
    questions.unshift(question);
  }
  const draft = normalizeDraft(
    firstRecord(root.draft, root.draft_json, root.draftJson, root.proposal),
  );
  const publishedContainer = firstRecord(
    root.published,
    root.publication,
    root.published_knowledge,
  );
  const rawSeedTask = firstRecord(root.seed_task, root.seedTask);
  const seedTask = normalizeSeedTask(rawSeedTask);
  const reviewContext = normalizeReviewContext(
    root.review_context ?? root.reviewContext,
  );
  const published = normalizePublished(
    publishedContainer ||
      root.published_node_id ||
      root.publishedNodeId ||
      root.seed_task_id ||
      root.seedTaskId
      ? {
          ...(publishedContainer ?? {}),
          published_node_id:
            publishedContainer?.published_node_id ?? root.published_node_id,
          publishedNodeId:
            publishedContainer?.publishedNodeId ?? root.publishedNodeId,
          seed_task_id:
            publishedContainer?.seed_task_id ?? root.seed_task_id,
          seedTaskId: publishedContainer?.seedTaskId ?? root.seedTaskId,
          source_task_id:
            publishedContainer?.source_task_id ?? seedTask?.id,
          source_task_title:
            publishedContainer?.source_task_title ?? seedTask?.title,
        }
      : null,
  );
  const sourceCount = Array.isArray(root.sources) ? root.sources.length : 0;
  const candidateDraft = draft
    ? {
        ...draft,
        evidenceSummary:
          draft.evidenceSummary ||
          (sourceCount > 0 ? `根拠 ${sourceCount}件` : ""),
      }
    : draft;
  return {
    id,
    projectId: nullableString(
      root.project_id,
      root.projectId,
      fallbackProjectId,
    ),
    status: stringValue(root.status, root.state, root.lifecycle_state) || "researching",
    version: numberValue(root.version, numberValue(root.revision, 0)),
    publicationGuardAdoptionRequired: booleanValue(
      root.publication_guard_adoption_required,
      booleanValue(root.publicationGuardAdoptionRequired, false),
    ),
    question,
    questions,
    draft: candidateDraft,
    published,
    seedTask,
    reviewContext,
  };
}

export function normalizeKnowledgeCaptureCandidateList(
  body: unknown,
  fallbackProjectId?: string,
): {
  items: KnowledgeCaptureCandidate[];
  total: number;
} {
  const root = isRecord(body) ? body : null;
  const rawItems = Array.isArray(body)
    ? body
    : Array.isArray(root?.items)
      ? root.items
      : Array.isArray(root?.candidates)
        ? root.candidates
        : Array.isArray(root?.results)
          ? root.results
          : [];
  const items = rawItems
    .slice(0, 50)
    .map((item) => normalizeKnowledgeCaptureCandidate(item, fallbackProjectId))
    .filter((item): item is KnowledgeCaptureCandidate => item !== null);
  const total =
    root && typeof root.total === "number" && Number.isFinite(root.total)
      ? root.total
      : items.length;
  return { items, total };
}

export function normalizeKnowledgeCaptureCandidateDetail(
  body: unknown,
  _fallbackProjectId?: string,
): KnowledgeCaptureCandidate | null {
  // A project-scoped detail response is authoritative.  Do not synthesize
  // its project binding from the request URL or another caller-owned value.
  void _fallbackProjectId;
  const candidate = normalizeKnowledgeCaptureCandidate(body);
  if (
    !candidate ||
    !isUuid(candidate.id) ||
    !isUuid(candidate.projectId) ||
    !Number.isInteger(candidate.version) ||
    candidate.version <= 0
  ) {
    return null;
  }
  if (
    candidate.question &&
    (!isUuid(candidate.question.id) ||
      candidate.question.status !== "pending" ||
      candidate.question.candidateVersion !== candidate.version)
  ) {
    return null;
  }
  if (
    candidate.questions.some(
      (question) =>
        question.status === "pending" &&
        (!isUuid(question.id) || question.candidateVersion !== candidate.version),
    )
  ) {
    return null;
  }
  return candidate;
}

type NotificationLike = {
  type?: unknown;
  notification_type?: unknown;
  project_id?: unknown;
  payload?: unknown;
  task_id?: unknown;
};

function notificationPayload(notification: NotificationLike): RecordValue {
  return isRecord(notification.payload) ? notification.payload : {};
}

function notificationType(notification: NotificationLike): string {
  const values = [notification.type, notification.notification_type]
    .map((value) => (typeof value === "string" ? value.trim() : ""))
    .filter(Boolean);
  if (values.length === 0 || values.some((value) => value !== values[0])) {
    return "";
  }
  return values[0] === "knowledge_capture_question" ||
    values[0] === "knowledge_capture_draft"
    ? values[0]
    : "";
}

export function getKnowledgeCaptureNotificationTarget(
  notification: NotificationLike,
): KnowledgeCaptureNotificationTarget | null {
  const type = notificationType(notification);
  if (
    type !== "knowledge_capture_question" &&
    type !== "knowledge_capture_draft"
  ) {
    return null;
  }
  const payload = notificationPayload(notification);
  if (payload.kind !== "knowledge_capture") return null;

  const projectId = stringValue(notification.project_id);
  const payloadProjectId = stringValue(payload.project_id, payload.projectId);
  const hasPayloadProjectId =
    Object.prototype.hasOwnProperty.call(payload, "project_id") ||
    Object.prototype.hasOwnProperty.call(payload, "projectId");
  if (
    !isUuid(projectId) ||
    (hasPayloadProjectId &&
      (!isUuid(payloadProjectId) ||
        payloadProjectId.toLowerCase() !== projectId.toLowerCase()))
  ) {
    return null;
  }
  const candidateId = stringValue(
    payload.candidate_id,
    payload.candidateId,
  );
  const candidateVersion = numberValue(
    payload.candidate_version ?? payload.candidateVersion,
    0,
  );
  if (
    !isUuid(candidateId) ||
    !Number.isInteger(candidateVersion) ||
    candidateVersion <= 0
  ) {
    return null;
  }

  const rawQuestionId = payload.question_id ?? payload.questionId;
  if (type === "knowledge_capture_question") {
    if (!isUuid(rawQuestionId)) return null;
    return {
      notificationType: type,
      projectId,
      candidateId,
      candidateVersion,
      questionId: rawQuestionId,
    };
  }
  if (rawQuestionId !== null && rawQuestionId !== undefined) return null;
  return {
    notificationType: type,
    projectId,
    candidateId,
    candidateVersion,
    questionId: null,
  };
}

export function matchesKnowledgeCaptureNotificationDetail(
  target: KnowledgeCaptureNotificationTarget,
  detail: KnowledgeCaptureCandidate | null,
): boolean {
  if (
    !detail ||
    detail.id !== target.candidateId ||
    detail.projectId?.toLowerCase() !== target.projectId.toLowerCase() ||
    detail.version !== target.candidateVersion
  ) {
    return false;
  }

  if (target.notificationType === "knowledge_capture_question") {
    if (
      detail.question &&
      (detail.question.status !== "pending" ||
        detail.question.candidateVersion !== target.candidateVersion)
    ) {
      return false;
    }
    const pendingQuestions = detail.questions.length
      ? detail.questions
      : detail.question
        ? [detail.question]
        : [];
    return (
      detail.status === "needs_user" &&
      target.questionId !== null &&
      pendingQuestions.some(
        (question) =>
          question.id === target.questionId &&
          question.status === "pending" &&
          question.candidateVersion === target.candidateVersion,
      )
    );
  }

  return detail.status === "draft_ready" && detail.question === null;
}

export function isKnowledgeCaptureNotificationType(
  notification: NotificationLike,
): boolean {
  return notificationType(notification) !== "";
}

export function getKnowledgeCaptureNotificationDeepLink(
  notificationId: unknown,
): string | null {
  if (!isUuid(notificationId)) return null;
  const params = new URLSearchParams();
  params.set("open_notification", notificationId.trim());
  return `/chat?${params.toString()}`;
}

export function getNotificationInternalRoute(
  notification: NotificationLike,
): string | null {
  if (isKnowledgeCaptureNotificationType(notification)) return null;
  const taskId = notification.task_id;
  return typeof taskId === "string" && taskId.trim()
    ? `/tasks/${encodeURIComponent(taskId)}`
    : null;
}
