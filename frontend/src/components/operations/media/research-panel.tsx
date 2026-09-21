"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  BookOpen,
  Loader2,
  Plus,
  RefreshCw,
} from "lucide-react";

import {
  mediaResearchApi,
  type ResearchCandidate,
  type ResearchCandidateDecision,
  type ResearchCandidateDecisionPage,
  type EditorialProgram,
  type ResearchFinding,
  type ResearchFindingKind,
  type ResearchRoutine,
  type ResearchRun,
} from "@/lib/media-operations-research-api";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";


function newIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }

  return `media-research-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`;
}

function readableError(error: unknown): string {
  if (
    error instanceof Error &&
    error.message
  ) {
    return error.message;
  }
  return "Research APIでエラーが発生しました";
}

function splitLines(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/\r?\n/u)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function splitIds(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[,\r\n]/u)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function candidateStatusLabel(status: string): string {
  switch (status) {
    case "discovered":
    case "triaged":
      return "Pending";
    case "accepted":
      return "Accepted";
    case "rejected":
      return "Rejected";
    case "promoted":
      return "Promoted";
    case "expired":
      return "Expired";
    default:
      return status || "Unknown";
  }
}

function candidateStatusClass(status: string): string {
  switch (status) {
    case "accepted":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
    case "rejected":
      return "border-destructive/40 bg-destructive/10 text-destructive";
    case "promoted":
      return "border-primary/40 bg-primary/10 text-primary";
    default:
      return "border-border bg-muted/40 text-muted-foreground";
  }
}

const CANDIDATE_DECISION_PAGE_SIZE = 100;

function decisionItems(
  value: ResearchCandidateDecisionPage,
): ResearchCandidateDecision[] {
  // The API returns sequence ASC.  Sorting defensively keeps the immutable
  // history readable even when a proxy or fixture changes the wire order.
  return [...value.items].sort((left, right) =>
    left.sequence - right.sequence || left.id.localeCompare(right.id),
  );
}

export function MediaResearchPanel() {
  const [routines, setRoutines] =
    useState<ResearchRoutine[]>([]);
  const [runs, setRuns] =
    useState<ResearchRun[]>([]);
  const [findings, setFindings] =
    useState<ResearchFinding[]>([]);
  const [programs, setPrograms] =
    useState<EditorialProgram[]>([]);
  const [candidates, setCandidates] =
    useState<ResearchCandidate[]>([]);
  const [selectedCandidateId, setSelectedCandidateId] =
    useState("");
  const [candidateDecisions, setCandidateDecisions] =
    useState<ResearchCandidateDecision[]>([]);
  const [candidateDecisionTotal, setCandidateDecisionTotal] =
    useState(0);
  const [candidateDecisionHasMore, setCandidateDecisionHasMore] =
    useState(false);
  const [candidateDecisionOffset, setCandidateDecisionOffset] =
    useState(0);
  const [candidateReason, setCandidateReason] =
    useState("");
  const [candidateBusy, setCandidateBusy] =
    useState<"load" | "load-more" | "triaged" | "accepted" | "rejected" | "promote" | null>(null);

  // Every candidate scope has its own request generation.  A candidate can
  // change while a page or mutation is in flight; stale responses must never
  // repopulate the newly selected candidate's immutable history.
  const candidateDecisionRequestRef = useRef(0);
  const selectedCandidateIdRef = useRef("");

  const [selectedRoutineId, setSelectedRoutineId] =
    useState("");
  const [selectedRunId, setSelectedRunId] =
    useState("");
  const [selectedProgramId, setSelectedProgramId] =
    useState("");

  const [routineName, setRoutineName] = useState("");
  const [routineObjective, setRoutineObjective] = useState("");
  const [routineQuestions, setRoutineQuestions] = useState("");

  const [findingKind, setFindingKind] =
    useState<ResearchFindingKind>("fact");
  const [findingStatement, setFindingStatement] = useState("");
  const [evidenceUrl, setEvidenceUrl] = useState("");
  const [evidenceNote, setEvidenceNote] = useState("");

  const [personaId, setPersonaId] = useState("");
  const [programName, setProgramName] = useState("");
  const [programObjective, setProgramObjective] = useState("");

  const [contentTitle, setContentTitle] = useState("");
  const [contentBrief, setContentBrief] = useState("");
  const [contentFindingIds, setContentFindingIds] = useState("");

  const [loading, setLoading] = useState(true);
  const [busy, setBusy] =
    useState<
      "routine" | "run" | "finding" | "program" | "content" | null
    >(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    selectedCandidateIdRef.current = selectedCandidateId;
  }, [selectedCandidateId]);

  const selectedCandidate = useMemo(
    () =>
      candidates.find((item) => item.id === selectedCandidateId) ?? null,
    [candidates, selectedCandidateId],
  );

  const loadRoot = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const [nextRoutines, nextPrograms] =
        await Promise.all([
          mediaResearchApi.listRoutines(),
          mediaResearchApi.listPrograms(),
        ]);

      setRoutines(nextRoutines);
      setPrograms(nextPrograms);

      setSelectedRoutineId((current) =>
        nextRoutines.some((item) => item.id === current)
          ? current
          : nextRoutines[0]?.id ?? "",
      );
      setSelectedProgramId((current) =>
        nextPrograms.some((item) => item.id === current)
          ? current
          : nextPrograms[0]?.id ?? "",
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const task = window.setTimeout(() => {
      void loadRoot();
    }, 0);

    return () => window.clearTimeout(task);
  }, [loadRoot]);

  useEffect(() => {
    const task = window.setTimeout(() => {
      if (!selectedRoutineId) {
        setRuns([]);
        setSelectedRunId("");
        return;
      }

      void (async () => {
        try {
          const nextRuns =
            await mediaResearchApi.listRuns(selectedRoutineId);

          setRuns(nextRuns);
          setSelectedRunId((current) =>
            nextRuns.some((item) => item.id === current)
              ? current
              : nextRuns[0]?.id ?? "",
          );
        } catch (nextError) {
          setError(nextError);
        }
      })();
    }, 0);

    return () => window.clearTimeout(task);
  }, [selectedRoutineId]);

  useEffect(() => {
    let cancelled = false;
    const task = window.setTimeout(() => {
      // Do not briefly render a candidate (or its immutable decisions) from
      // the previous Routine while the new scoped list is loading.
      setCandidates([]);
      setSelectedCandidateId("");
      setCandidateDecisions([]);
      setCandidateDecisionTotal(0);
      setCandidateDecisionHasMore(false);
      setCandidateDecisionOffset(0);
      candidateDecisionRequestRef.current += 1;
      if (!selectedRoutineId) {
        setCandidates([]);
        setSelectedCandidateId("");
        setCandidateDecisions([]);
        setCandidateDecisionTotal(0);
        setCandidateDecisionHasMore(false);
        setCandidateDecisionOffset(0);
        return;
      }

      setCandidateBusy("load");
      void (async () => {
        try {
          const nextCandidates = await mediaResearchApi.listCandidates({
            research_routine_id: selectedRoutineId,
            limit: 100,
            offset: 0,
          });
          if (cancelled) return;
          setCandidates(nextCandidates);
          setSelectedCandidateId((current) =>
            nextCandidates.some((item) => item.id === current)
              ? current
              : nextCandidates[0]?.id ?? "",
          );
        } catch (nextError) {
          if (!cancelled) setError(nextError);
        } finally {
          if (!cancelled) setCandidateBusy(null);
        }
      })();
    }, 0);

    return () => {
      cancelled = true;
      window.clearTimeout(task);
    };
  }, [selectedRoutineId]);

  useEffect(() => {
    let cancelled = false;
    const requestGeneration = ++candidateDecisionRequestRef.current;
    const candidateId = selectedCandidateId;
    const task = window.setTimeout(() => {
      if (!candidateId) {
        setCandidateDecisions([]);
        setCandidateDecisionTotal(0);
        setCandidateDecisionHasMore(false);
        setCandidateDecisionOffset(0);
        return;
      }

      setCandidateBusy("load");
      void (async () => {
        try {
          const decisionPage = await mediaResearchApi.listCandidateDecisions(
            candidateId,
            { limit: CANDIDATE_DECISION_PAGE_SIZE, offset: 0 },
          );
          if (
            cancelled ||
            requestGeneration !== candidateDecisionRequestRef.current ||
            selectedCandidateIdRef.current !== candidateId
          ) {
            return;
          }
          setCandidateDecisions(decisionItems(decisionPage));
          setCandidateDecisionTotal(decisionPage.total);
          setCandidateDecisionHasMore(decisionPage.has_more);
          setCandidateDecisionOffset(
            decisionPage.offset + decisionPage.items.length,
          );
        } catch (nextError) {
          if (
            !cancelled &&
            requestGeneration === candidateDecisionRequestRef.current &&
            selectedCandidateIdRef.current === candidateId
          ) {
            setError(nextError);
          }
        } finally {
          if (
            !cancelled &&
            requestGeneration === candidateDecisionRequestRef.current &&
            selectedCandidateIdRef.current === candidateId
          ) {
            setCandidateBusy(null);
          }
        }
      })();
    }, 0);

    return () => {
      cancelled = true;
      window.clearTimeout(task);
    };
  }, [selectedCandidateId]);

  const loadMoreCandidateDecisions = async () => {
    const candidateId = selectedCandidateIdRef.current;
    if (
      !candidateId ||
      !candidateDecisionHasMore ||
      candidateBusy !== null
    ) {
      return;
    }

    const requestGeneration = ++candidateDecisionRequestRef.current;
    const offset = candidateDecisionOffset;
    setCandidateBusy("load-more");
    setError(null);
    try {
      const decisionPage = await mediaResearchApi.listCandidateDecisions(
        candidateId,
        { limit: CANDIDATE_DECISION_PAGE_SIZE, offset },
      );
      if (
        requestGeneration !== candidateDecisionRequestRef.current ||
        selectedCandidateIdRef.current !== candidateId
      ) {
        return;
      }
      setCandidateDecisions((current) => {
        const byId = new Map(current.map((decision) => [decision.id, decision]));
        for (const decision of decisionItems(decisionPage)) {
          byId.set(decision.id, decision);
        }
        return [...byId.values()].sort(
          (left, right) =>
            left.sequence - right.sequence || left.id.localeCompare(right.id),
        );
      });
      setCandidateDecisionTotal(decisionPage.total);
      setCandidateDecisionHasMore(decisionPage.has_more);
      setCandidateDecisionOffset(
        decisionPage.offset + decisionPage.items.length,
      );
    } catch (nextError) {
      if (
        requestGeneration === candidateDecisionRequestRef.current &&
        selectedCandidateIdRef.current === candidateId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        requestGeneration === candidateDecisionRequestRef.current &&
        selectedCandidateIdRef.current === candidateId
      ) {
        setCandidateBusy(null);
      }
    }
  };

  useEffect(() => {
    const task = window.setTimeout(() => {
      if (!selectedRunId) {
        setFindings([]);
        return;
      }

      void (async () => {
        try {
          setFindings(
            await mediaResearchApi.listFindings(
              selectedRunId,
            ),
          );
        } catch (nextError) {
          setError(nextError);
        }
      })();
    }, 0);

    return () => window.clearTimeout(task);
  }, [selectedRunId]);

  const selectedRoutine = useMemo(
    () =>
      routines.find(
        (item) =>
          item.id === selectedRoutineId,
      ) ?? null,
    [routines, selectedRoutineId],
  );

  const createRoutine = async (
    event: React.FormEvent<HTMLFormElement>,
  ) => {
    event.preventDefault();

    const questions = splitLines(
      routineQuestions,
    );

    if (
      !routineName.trim() ||
      !routineObjective.trim() ||
      !questions.length
    ) {
      setError(
        new Error(
          "名前・目的・1件以上のResearch questionが必要です",
        ),
      );
      return;
    }

    setBusy("routine");
    setError(null);

    try {
      const created =
        await mediaResearchApi.createRoutine(
          {
            project_id: null,
            name: routineName.trim(),
            objective:
              routineObjective.trim(),
            questions,
            target_platforms: [],
            state: "draft",
            enabled: false,
          },
          newIdempotencyKey(),
        );

      setRoutineName("");
      setRoutineObjective("");
      setRoutineQuestions("");

      await loadRoot();
      setSelectedRoutineId(
        created.id,
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const decideCandidate = async (
    status: "triaged" | "accepted" | "rejected",
  ) => {
    if (!selectedCandidate) return;
    const candidateId = selectedCandidate.id;
    const reason = candidateReason.trim();
    if ((status === "accepted" || status === "rejected") && !reason) {
      setError(new Error("採用・却下には理由が必要です"));
      return;
    }

    setCandidateBusy(status);
    setError(null);
    try {
      const updated = await mediaResearchApi.triageCandidate(
        candidateId,
        {
          status,
          reason: reason || null,
          expected_status: selectedCandidate.status === "accepted"
            ? "accepted"
            : selectedCandidate.status === "triaged"
              ? "triaged"
              : "discovered",
          expected_decision_version: selectedCandidate.decision_version,
          expected_candidate_hash: selectedCandidate.candidate_hash,
        },
        newIdempotencyKey(),
      );
      if (selectedCandidateIdRef.current !== candidateId) return;
      setCandidates((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setCandidateReason("");
      const decisionPage = await mediaResearchApi.listCandidateDecisions(updated.id, {
        limit: CANDIDATE_DECISION_PAGE_SIZE,
        offset: 0,
      });
      if (selectedCandidateIdRef.current !== candidateId) return;
      // A fresh mutation starts history pagination over at page zero.  This
      // prevents an old page window from being reused for promotion lookup.
      candidateDecisionRequestRef.current += 1;
      setCandidateDecisions(decisionItems(decisionPage));
      setCandidateDecisionTotal(decisionPage.total);
      setCandidateDecisionHasMore(decisionPage.has_more);
      setCandidateDecisionOffset(
        decisionPage.offset + decisionPage.items.length,
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      if (selectedCandidateIdRef.current === candidateId) {
        setCandidateBusy(null);
      }
    }
  };

  const promoteCandidate = async () => {
    if (!selectedCandidate || selectedCandidate.status !== "accepted") return;
    const candidateId = selectedCandidate.id;
    const expectedVersion = selectedCandidate.decision_version;
    if (expectedVersion < 1) {
      setError(new Error("採用Decisionが見つからないため昇格できません"));
      return;
    }
    // Fetch the page containing the current decision version rather than
    // trusting whichever history pages happen to be rendered.  For version
    // N, pages are zero-based windows of 100 rows.
    const latestOffset =
      Math.floor((expectedVersion - 1) / CANDIDATE_DECISION_PAGE_SIZE) *
      CANDIDATE_DECISION_PAGE_SIZE;
    const requestGeneration = ++candidateDecisionRequestRef.current;
    setCandidateBusy("promote");
    setError(null);
    try {
      const decisionPage = await mediaResearchApi.listCandidateDecisions(
        candidateId,
        {
          limit: CANDIDATE_DECISION_PAGE_SIZE,
          offset: latestOffset,
        },
      );
      if (
        requestGeneration !== candidateDecisionRequestRef.current ||
        selectedCandidateIdRef.current !== candidateId
      ) {
        return;
      }
      if (
        decisionPage.current_status !== "accepted" ||
        decisionPage.current_decision_version !== expectedVersion
      ) {
        // The candidate may have been revoked in another tab while this
        // panel was open.  Do not send a promotion command for stale state.
        setError(new Error("Candidateが変更されたため昇格を中止しました"));
        return;
      }
      const acceptedDecision = decisionPage.items.find(
        (decision) =>
          decision.sequence === expectedVersion &&
          decision.event_type === "accept" &&
          decision.to_status === "accepted",
      );
      if (!acceptedDecision) {
        setError(new Error("最新の採用Decisionが見つからないため昇格できません"));
        return;
      }
      await mediaResearchApi.promoteCandidate(
        candidateId,
        { accepted_decision_id: acceptedDecision.id },
        newIdempotencyKey(),
      );
      if (
        requestGeneration !== candidateDecisionRequestRef.current ||
        selectedCandidateIdRef.current !== candidateId
      ) {
        return;
      }
      const updated = await mediaResearchApi.getCandidate(candidateId);
      if (
        requestGeneration !== candidateDecisionRequestRef.current ||
        selectedCandidateIdRef.current !== candidateId
      ) {
        return;
      }
      setCandidates((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      const refreshedPage = await mediaResearchApi.listCandidateDecisions(updated.id, {
        limit: CANDIDATE_DECISION_PAGE_SIZE,
        offset: 0,
      });
      if (
        requestGeneration !== candidateDecisionRequestRef.current ||
        selectedCandidateIdRef.current !== candidateId
      ) {
        return;
      }
      setCandidateDecisions(decisionItems(refreshedPage));
      setCandidateDecisionTotal(refreshedPage.total);
      setCandidateDecisionHasMore(refreshedPage.has_more);
      setCandidateDecisionOffset(
        refreshedPage.offset + refreshedPage.items.length,
      );
    } catch (nextError) {
      if (
        requestGeneration === candidateDecisionRequestRef.current &&
        selectedCandidateIdRef.current === candidateId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        requestGeneration === candidateDecisionRequestRef.current &&
        selectedCandidateIdRef.current === candidateId
      ) {
        setCandidateBusy(null);
      }
    }
  };

  const startRun = async () => {
    if (!selectedRoutine) return;

    setBusy("run");
    setError(null);

    try {
      const run =
        await mediaResearchApi.startRun(
          {
            research_routine_id:
              selectedRoutine.id,
            routine_version:
              selectedRoutine.current_revision.version,
            focus_note: null,
          },
          newIdempotencyKey(),
        );

      const nextRuns =
        await mediaResearchApi.listRuns(
          selectedRoutine.id,
        );
      setRuns(nextRuns);
      setSelectedRunId(run.id);
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const appendFinding = async (
    event: React.FormEvent<HTMLFormElement>,
  ) => {
    event.preventDefault();

    if (
      !selectedRunId ||
      !findingStatement.trim() ||
      !evidenceUrl.trim()
    ) {
      setError(
        new Error(
          "Run・Finding・Evidence URLが必要です",
        ),
      );
      return;
    }

    setBusy("finding");
    setError(null);

    try {
      await mediaResearchApi.appendFinding(
        selectedRunId,
        {
          kind: findingKind,
          statement:
            findingStatement.trim(),
          evidence: [
            {
              type: "url",
              url: evidenceUrl.trim(),
              label: null,
              note:
                evidenceNote.trim() ||
                null,
            },
          ],
        },
        newIdempotencyKey(),
      );

      setFindingStatement("");
      setEvidenceUrl("");
      setEvidenceNote("");

      setFindings(
        await mediaResearchApi.listFindings(
          selectedRunId,
        ),
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const createProgram = async (
    event: React.FormEvent<HTMLFormElement>,
  ) => {
    event.preventDefault();

    if (
      !personaId.trim() ||
      !programName.trim() ||
      !programObjective.trim()
    ) {
      setError(
        new Error(
          "Persona ID・Program名・目的が必要です",
        ),
      );
      return;
    }

    setBusy("program");
    setError(null);

    try {
      const created =
        await mediaResearchApi.createProgram(
          {
            persona_id:
              personaId.trim(),
            name:
              programName.trim(),
            objective:
              programObjective.trim(),
            content_type: "article",
            cadence: "manual",
            draft_generation_policy:
              "human_review",
            state: "draft",
            enabled: false,
          },
          newIdempotencyKey(),
        );

      setProgramName("");
      setProgramObjective("");

      const nextPrograms =
        await mediaResearchApi.listPrograms();
      setPrograms(nextPrograms);
      setSelectedProgramId(
        created.id,
      );
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  const createContentItem = async (
    event: React.FormEvent<HTMLFormElement>,
  ) => {
    event.preventDefault();

    const findingIds = splitIds(
      contentFindingIds,
    );

    if (
      !selectedProgramId ||
      !contentTitle.trim() ||
      !contentBrief.trim() ||
      !findingIds.length
    ) {
      setError(
        new Error(
          "Program・タイトル・Brief・Finding IDが必要です",
        ),
      );
      return;
    }

    setBusy("content");
    setError(null);

    try {
      await mediaResearchApi.createContentItem(
        {
          editorial_program_id:
            selectedProgramId,
          title:
            contentTitle.trim(),
          brief:
            contentBrief.trim(),
          finding_ids:
            findingIds,
          content_type: "article",
        },
        newIdempotencyKey(),
      );

      setContentTitle("");
      setContentBrief("");
      setContentFindingIds("");
    } catch (nextError) {
      setError(nextError);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      className="space-y-5"
      data-testid="media-research-panel"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">
            Research
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            ResearchRoutineの定義、Run snapshot、Evidence付きFinding、
            Findingを参照するContentItemまでを記録します。
            検索・生成・投稿は実行しません。
          </p>
        </div>

        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() =>
            void loadRoot()
          }
          disabled={loading}
        >
          <RefreshCw
            className={`size-3.5 ${
              loading
                ? "animate-spin"
                : ""
            }`}
          />
          更新
        </Button>
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {readableError(error)}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-2">
        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">
              ResearchRoutine
            </CardTitle>
            <CardDescription>
              繰り返し使うResearch questionをversioned definitionとして保存します。
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-4 pt-4">
            <form
              className="space-y-2"
              aria-label="ResearchRoutine作成フォーム"
              onSubmit={createRoutine}
            >
              <Input
                aria-label="ResearchRoutine name"
                value={routineName}
                onChange={(event) =>
                  setRoutineName(
                    event.target.value,
                  )
                }
                placeholder="Routine name"
              />

              <Textarea
                aria-label="ResearchRoutine objective"
                value={routineObjective}
                onChange={(event) =>
                  setRoutineObjective(
                    event.target.value,
                  )
                }
                placeholder="Research objective"
                rows={3}
              />

              <Textarea
                aria-label="Research questions"
                value={routineQuestions}
                onChange={(event) =>
                  setRoutineQuestions(
                    event.target.value,
                  )
                }
                placeholder="1行につき1 question"
                rows={5}
              />

              <Button
                type="submit"
                size="sm"
                disabled={busy !== null}
              >
                {busy === "routine" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <Plus className="size-3.5" />
                )}
                Routineを作成
              </Button>
            </form>

            <div className="space-y-2">
              <AppSelect
                aria-label="ResearchRoutine"
                value={selectedRoutineId}
                onChange={(event) =>
                  setSelectedRoutineId(
                    event.target.value,
                  )
                }
              >
                <option value="">
                  Routineを選択
                </option>
                {routines.map(
                  (routine) => (
                    <option
                      key={routine.id}
                      value={routine.id}
                    >
                      {
                        routine
                          .current_revision
                          .name
                      }
                      {" · v"}
                      {
                        routine
                          .current_revision
                          .version
                      }
                    </option>
                  ),
                )}
              </AppSelect>

              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={
                  !selectedRoutine ||
                  busy !== null
                }
                onClick={() =>
                  void startRun()
                }
              >
                {busy === "run" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <BookOpen className="size-3.5" />
                )}
                このrevisionからRunを開始
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card size="sm">
          <CardHeader className="border-b border-border/70">
            <CardTitle className="text-sm">
              ResearchRun / Findings
            </CardTitle>
            <CardDescription>
              RunはRoutine revisionのhash snapshotを保持し、
              Findingは1件以上のtyped Evidenceを必須とします。
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-4 pt-4">
            <AppSelect
              aria-label="ResearchRun"
              value={selectedRunId}
              onChange={(event) =>
                setSelectedRunId(
                  event.target.value,
                )
              }
            >
              <option value="">
                Runを選択
              </option>
              {runs.map((run) => (
                <option
                  key={run.id}
                  value={run.id}
                >
                  {run.id}
                </option>
              ))}
            </AppSelect>

            <form
              className="space-y-2"
              aria-label="Finding作成フォーム"
              onSubmit={appendFinding}
            >
              <AppSelect
                aria-label="Finding kind"
                value={findingKind}
                onChange={(event) =>
                  setFindingKind(
                    event.target
                      .value as ResearchFindingKind,
                  )
                }
              >
                <option value="fact">
                  fact
                </option>
                <option value="signal">
                  signal
                </option>
                <option value="hypothesis">
                  hypothesis
                </option>
              </AppSelect>

              <Textarea
                aria-label="Finding statement"
                value={findingStatement}
                onChange={(event) =>
                  setFindingStatement(
                    event.target.value,
                  )
                }
                rows={3}
                placeholder="Finding"
              />

              <Input
                aria-label="Evidence URL"
                value={evidenceUrl}
                onChange={(event) =>
                  setEvidenceUrl(
                    event.target.value,
                  )
                }
                placeholder="https://..."
              />

              <Textarea
                aria-label="Evidence note"
                value={evidenceNote}
                onChange={(event) =>
                  setEvidenceNote(
                    event.target.value,
                  )
                }
                rows={2}
                placeholder="Evidence note（任意）"
              />

              <Button
                type="submit"
                size="sm"
                disabled={
                  !selectedRunId ||
                  busy !== null
                }
              >
                {busy === "finding" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <Plus className="size-3.5" />
                )}
                Findingを追加
              </Button>
            </form>

            <div
              className="space-y-2"
              data-testid="research-findings"
            >
              {findings.map(
                (finding) => (
                  <div
                    key={finding.id}
                    className="rounded-md border border-border/70 p-3"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold">
                        {finding.kind}
                      </span>
                      <code className="text-[10px] text-muted-foreground">
                        {finding.id}
                      </code>
                    </div>
                    <p className="mt-1 text-sm">
                      {finding.statement}
                    </p>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Evidence{" "}
                      {finding.evidence.length}
                      件
                    </p>
                  </div>
                ),
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card size="sm" data-testid="research-candidates">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="text-sm">Research candidates</CardTitle>
          <CardDescription>
            Source-backed候補は未信頼のまま保留し、Decision ledgerを確認してから人間が採用・却下します。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4 pt-4">
          {!selectedRoutineId ? (
            <p className="text-xs text-muted-foreground">Routineを選択すると候補を表示します。</p>
          ) : candidateBusy === "load" && !candidates.length ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" /> 候補を読み込み中…
            </div>
          ) : !candidates.length ? (
            <p className="text-xs text-muted-foreground">Research candidateはありません。</p>
          ) : (
            <>
              <div className="grid gap-2 md:grid-cols-2" role="list" aria-label="Research candidates">
                {candidates.map((candidate) => {
                  const selected = candidate.id === selectedCandidateId;
                  return (
                    <button
                      key={candidate.id}
                      type="button"
                      aria-pressed={selected}
                      onClick={() => setSelectedCandidateId(candidate.id)}
                      className={`rounded-md border p-3 text-left transition-colors ${
                        selected
                          ? "border-primary bg-primary/5"
                          : "border-border/70 hover:bg-muted/40"
                      }`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <span className="line-clamp-2 text-xs font-semibold">{candidate.title}</span>
                        <span
                          className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${candidateStatusClass(candidate.status)}`}
                          data-testid={`candidate-status-${candidate.id}`}
                        >
                          {candidateStatusLabel(candidate.status)}
                        </span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">{candidate.summary}</p>
                    </button>
                  );
                })}
              </div>

              {selectedCandidate ? (
                <div className="space-y-3 rounded-md border border-border/70 p-3" data-testid="research-candidate-detail">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="text-xs font-semibold">{selectedCandidate.title}</p>
                      <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{selectedCandidate.id}</p>
                    </div>
                    <span className={`rounded-full border px-2 py-0.5 text-[10px] ${candidateStatusClass(selectedCandidate.status)}`}>
                      {candidateStatusLabel(selectedCandidate.status)}
                    </span>
                  </div>
                  {selectedCandidate.reason ? (
                    <p className="text-xs text-muted-foreground">理由: {selectedCandidate.reason}</p>
                  ) : null}
                  <Textarea
                    aria-label="Candidate decision reason"
                    value={candidateReason}
                    onChange={(event) => setCandidateReason(event.target.value)}
                    rows={2}
                    placeholder="採用・却下の理由（採用/却下では必須）"
                    disabled={candidateBusy !== null}
                  />
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={candidateBusy !== null || selectedCandidate.status !== "discovered"}
                      onClick={() => void decideCandidate("triaged")}
                    >
                      {candidateBusy === "triaged" ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      保留
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      disabled={
                        candidateBusy !== null ||
                        !["discovered", "triaged", "accepted"].includes(selectedCandidate.status)
                      }
                      onClick={() => void decideCandidate("accepted")}
                    >
                      {candidateBusy === "accepted" ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      採用
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={
                        candidateBusy !== null ||
                        !["discovered", "triaged", "accepted"].includes(selectedCandidate.status)
                      }
                      onClick={() => void decideCandidate("rejected")}
                    >
                      {candidateBusy === "rejected" ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      却下
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={candidateBusy !== null || selectedCandidate.status !== "accepted"}
                      onClick={() => void promoteCandidate()}
                    >
                      {candidateBusy === "promote" ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      Draftへ昇格
                    </Button>
                  </div>

                  <div className="space-y-1.5" data-testid="research-candidate-decisions">
                    <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                      Decision ledger ({candidateDecisions.length}/{candidateDecisionTotal})
                    </p>
                    {candidateDecisions.length ? candidateDecisions.map((decision) => (
                      <div
                        key={decision.id}
                        data-testid={`research-candidate-decision-${decision.sequence}`}
                        className="flex flex-wrap items-center justify-between gap-2 text-[11px]"
                      >
                        <span>
                          {decision.from_status ?? "—"} → {decision.to_status}
                          {decision.reason ? ` · ${decision.reason}` : ""}
                        </span>
                        <span className="text-[10px] text-muted-foreground">{decision.actor_type} · {new Date(decision.created_at).toLocaleString("ja-JP")}</span>
                      </div>
                    )) : <p className="text-xs text-muted-foreground">Decisionはまだありません。</p>}
                    {candidateDecisionHasMore ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="mt-1"
                        data-testid="research-candidate-decisions-load-more"
                        disabled={candidateBusy !== null}
                        onClick={() => void loadMoreCandidateDecisions()}
                      >
                        {candidateBusy === "load-more" ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : null}
                        さらに読み込む
                      </Button>
                    ) : null}
                  </div>
                </div>
              ) : null}
            </>
          )}
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader className="border-b border-border/70">
          <CardTitle className="text-sm">
            Editorial trace boundary
          </CardTitle>
          <CardDescription>
            PersonaにEditorialProgramを結び、
            ContentItemからimmutable Findingへtraceします。
            本文生成・予約・投稿は行いません。
          </CardDescription>
        </CardHeader>

        <CardContent className="grid gap-4 pt-4 xl:grid-cols-2">
          <form
            className="space-y-2"
            aria-label="EditorialProgram作成フォーム"
            onSubmit={createProgram}
          >
            <Input
              aria-label="Editorial Persona ID"
              value={personaId}
              onChange={(event) =>
                setPersonaId(
                  event.target.value,
                )
              }
              placeholder="Persona ID"
            />
            <Input
              aria-label="EditorialProgram name"
              value={programName}
              onChange={(event) =>
                setProgramName(
                  event.target.value,
                )
              }
              placeholder="Program name"
            />
            <Textarea
              aria-label="EditorialProgram objective"
              value={programObjective}
              onChange={(event) =>
                setProgramObjective(
                  event.target.value,
                )
              }
              rows={3}
              placeholder="Editorial objective"
            />
            <Button
              type="submit"
              size="sm"
              disabled={busy !== null}
            >
              {busy === "program" ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Plus className="size-3.5" />
              )}
              Programを作成
            </Button>
          </form>

          <form
            className="space-y-2"
            aria-label="ContentItem作成フォーム"
            onSubmit={createContentItem}
          >
            <AppSelect
              aria-label="EditorialProgram"
              value={selectedProgramId}
              onChange={(event) =>
                setSelectedProgramId(
                  event.target.value,
                )
              }
            >
              <option value="">
                Programを選択
              </option>
              {programs.map(
                (program) => (
                  <option
                    key={program.id}
                    value={program.id}
                  >
                    {
                      program
                        .current_revision
                        .name
                    }
                  </option>
                ),
              )}
            </AppSelect>

            <Input
              aria-label="ContentItem title"
              value={contentTitle}
              onChange={(event) =>
                setContentTitle(
                  event.target.value,
                )
              }
              placeholder="Content title"
            />

            <Textarea
              aria-label="ContentItem brief"
              value={contentBrief}
              onChange={(event) =>
                setContentBrief(
                  event.target.value,
                )
              }
              rows={3}
              placeholder="Editorial brief"
            />

            <Textarea
              aria-label="Source Finding IDs"
              value={contentFindingIds}
              onChange={(event) =>
                setContentFindingIds(
                  event.target.value,
                )
              }
              rows={3}
              placeholder="Finding IDをカンマまたは改行区切り"
            />

            <Button
              type="submit"
              size="sm"
              disabled={
                !selectedProgramId ||
                busy !== null
              }
            >
              {busy === "content" ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Plus className="size-3.5" />
              )}
              ContentItemを作成
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
