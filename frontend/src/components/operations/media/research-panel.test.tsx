// @vitest-environment jsdom

import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { MediaResearchPanel } from "@/components/operations/media/research-panel";
import {
  mediaResearchApi,
  MediaResearchApiError,
  type ResearchCandidateDecision,
  type ResearchCandidateDecisionPage,
} from "@/lib/media-operations-research-api";


vi.mock(
  "@/lib/media-operations-research-api",
  async () => {
    const actual =
      await vi.importActual<
        typeof import("@/lib/media-operations-research-api")
      >(
        "@/lib/media-operations-research-api",
      );

    return {
      ...actual,
      mediaResearchApi: {
        listRoutines: vi.fn(),
        createRoutine: vi.fn(),
        getRoutine: vi.fn(),
        listRuns: vi.fn(),
        startRun: vi.fn(),
        getRun: vi.fn(),
        listFindings: vi.fn(),
        appendFinding: vi.fn(),
        listPrograms: vi.fn(),
        createProgram: vi.fn(),
        listContentItems: vi.fn(),
        createContentItem: vi.fn(),
        getContentItem: vi.fn(),
        listCandidates: vi.fn(),
        getCandidate: vi.fn(),
        listCandidateDecisions: vi.fn(),
        triageCandidate: vi.fn(),
        promoteCandidate: vi.fn(),
      },
    };
  },
);


const routine = {
  id: "routine-id",
  owner_user_id: "owner",
  project_id: null,
  create_hash: "a".repeat(64),
  created_by: "owner",
  created_at: "2026-09-01T00:00:00",
  current_revision: {
    id: "routine-revision-id",
    research_routine_id: "routine-id",
    owner_user_id: "owner",
    project_id: null,
    version: 1,
    name: "Test Routine",
    objective: "Objective",
    questions: ["Question"],
    target_platforms: [],
    content_hash: "b".repeat(64),
    created_by: "owner",
    created_at: "2026-09-01T00:00:00",
  },
};

const run = {
  id: "run-id",
  owner_user_id: "owner",
  project_id: null,
  research_routine_id: "routine-id",
  research_routine_revision_id: "routine-revision-id",
  routine_content_hash: "b".repeat(64),
  focus_note: null,
  run_hash: "c".repeat(64),
  created_by: "owner",
  created_at: "2026-09-01T00:00:00",
};

const candidate = {
  id: "candidate-id",
  owner_user_id: "owner",
  project_id: null,
  research_routine_id: "routine-id",
  research_run_id: "run-id",
  routine_revision_id: "routine-revision-id",
  candidate_key: "candidate-key",
  title: "Candidate title",
  summary: "Candidate summary",
  source_url: "https://example.test/source",
  source_published_at: null,
  discovered_at: "2026-09-01T00:00:00",
  expires_at: null,
  relevance_score: 0.8,
  freshness_score: 0.9,
  evidence: [],
  reason: null,
  status: "discovered",
  content_item_id: null,
  candidate_hash: "d".repeat(64),
  created_by: "owner",
  created_at: "2026-09-01T00:00:00",
  updated_at: "2026-09-01T00:00:00",
  decision_version: 0,
  review_state: "pending",
};

function decisionPage(
  candidateId: string,
  overrides: Partial<ResearchCandidateDecisionPage> = {},
): ResearchCandidateDecisionPage {
  return {
    candidate_id: candidateId,
    current_status: "discovered",
    current_decision_version: 0,
    candidate_hash: "d".repeat(64),
    items: [],
    total: 0,
    limit: 100,
    offset: 0,
    has_more: false,
    ...overrides,
  };
}

function decision(
  candidateId: string,
  sequence: number,
  overrides: Partial<ResearchCandidateDecision> = {},
): ResearchCandidateDecision {
  return {
    id: `${candidateId}-decision-${sequence}`,
    candidate_id: candidateId,
    sequence,
    event_type: "triage",
    from_status: sequence === 1 ? "discovered" : "triaged",
    to_status: "triaged",
    reason: null,
    candidate_hash: "d".repeat(64),
    candidate_snapshot_hash: "d".repeat(64),
    request_hash: "e".repeat(64),
    actor_id: "owner",
    actor_type: "human",
    content_item_id: null,
    decision_hash: "f".repeat(64),
    prev_decision_hash: null,
    prev_event_hash: null,
    event_hash: "a".repeat(64),
    decided_at: `2026-09-01T00:00:${String(sequence % 60).padStart(2, "0")}`,
    created_at: `2026-09-01T00:00:${String(sequence % 60).padStart(2, "0")}`,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

describe("MediaResearchPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    vi.mocked(
      mediaResearchApi.listRoutines,
    ).mockResolvedValue(
      [routine] as never,
    );

    vi.mocked(
      mediaResearchApi.listPrograms,
    ).mockResolvedValue([]);

    vi.mocked(
      mediaResearchApi.listRuns,
    ).mockResolvedValue(
      [run] as never,
    );

    vi.mocked(
      mediaResearchApi.listFindings,
    ).mockResolvedValue([]);
    vi.mocked(mediaResearchApi.listCandidates).mockResolvedValue([] as never);
    vi.mocked(mediaResearchApi.listCandidateDecisions).mockResolvedValue(
      decisionPage("candidate-id"),
    );
  });

  it("starts a Run pinned to the selected Routine revision", async () => {
    vi.mocked(
      mediaResearchApi.startRun,
    ).mockResolvedValue(
      {
        ...run,
        routine_revision:
          routine.current_revision,
        findings: [],
      } as never,
    );

    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(
        screen.getByLabelText(
          "ResearchRoutine",
        ),
      ).toHaveTextContent(
        "Test Routine",
      );
    });

    await userEvent.click(
      screen.getByRole(
        "button",
        {
          name:
            "このrevisionからRunを開始",
        },
      ),
    );

    await waitFor(() => {
      expect(
        mediaResearchApi.startRun,
      ).toHaveBeenCalledWith(
        {
          research_routine_id:
            "routine-id",
          routine_version: 1,
          focus_note: null,
        },
        expect.any(String),
      );
    });
  });

  it("never allows a Finding submit without Evidence URL in the minimal UI", async () => {
    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(
        screen.getByLabelText(
          "ResearchRun",
        ),
      ).toHaveTextContent(
        "run-id",
      );
    });

    fireEvent.change(
      screen.getByLabelText(
        "Finding statement",
      ),
      {
        target: {
          value: "test finding",
        },
      },
    );

    await userEvent.click(
      screen.getByRole(
        "button",
        {
          name: "Findingを追加",
        },
      ),
    );

    expect(
      mediaResearchApi.appendFinding,
    ).not.toHaveBeenCalled();

    expect(
      screen.getByRole("alert"),
    ).toHaveTextContent(
      "Evidence URL",
    );
  });

  it("keeps candidates pending until a human provides an accept reason", async () => {
    vi.mocked(mediaResearchApi.listCandidates).mockResolvedValue([candidate] as never);
    vi.mocked(mediaResearchApi.listCandidateDecisions).mockResolvedValue(
      decisionPage(candidate.id),
    );
    vi.mocked(mediaResearchApi.triageCandidate).mockResolvedValue({
      ...candidate,
      status: "accepted",
      reason: "Fits the current editorial objective",
      decision_version: 1,
    } as never);

    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("candidate-status-candidate-id")).toHaveTextContent("Pending");
    });

    await userEvent.click(screen.getByRole("button", { name: "採用" }));
    expect(mediaResearchApi.triageCandidate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("理由が必要");

    await userEvent.type(
      screen.getByLabelText("Candidate decision reason"),
      "Fits the current editorial objective",
    );
    await userEvent.click(screen.getByRole("button", { name: "採用" }));

    await waitFor(() => {
      expect(mediaResearchApi.triageCandidate).toHaveBeenCalledWith(
        "candidate-id",
        {
          status: "accepted",
          reason: "Fits the current editorial objective",
          expected_status: "discovered",
          expected_decision_version: 0,
          expected_candidate_hash: "d".repeat(64),
        },
        expect.any(String),
      );
      expect(screen.getByTestId("candidate-status-candidate-id")).toHaveTextContent("Accepted");
    });
  });

  it("loads decision history in 100/100/5 pages and keeps sequence ASC", async () => {
    vi.mocked(mediaResearchApi.listCandidates).mockResolvedValue([candidate] as never);
    const first = Array.from({ length: 100 }, (_, index) =>
      decision(candidate.id, index + 1),
    );
    const second = Array.from({ length: 100 }, (_, index) =>
      decision(candidate.id, index + 101),
    );
    const third = Array.from({ length: 5 }, (_, index) =>
      decision(candidate.id, index + 201),
    );
    vi.mocked(mediaResearchApi.listCandidateDecisions).mockImplementation(
      async (_candidateId, params) => {
        const offset = params?.offset ?? 0;
        if (offset === 100) {
          return decisionPage(candidate.id, {
            items: second,
            total: 205,
            offset,
            has_more: true,
          });
        }
        if (offset === 200) {
          return decisionPage(candidate.id, {
            items: third,
            total: 205,
            offset,
            has_more: false,
          });
        }
        return decisionPage(candidate.id, {
          items: first,
          total: 205,
          has_more: true,
        });
      },
    );

    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(mediaResearchApi.listCandidateDecisions).toHaveBeenCalledWith(
        candidate.id,
        { limit: 100, offset: 0 },
      );
      expect(screen.getByTestId("research-candidate-decisions-load-more")).toBeInTheDocument();
    });
    expect(screen.getByTestId("research-candidate-decisions")).toHaveTextContent("Decision ledger (100/205)");
    expect(screen.getByTestId("research-candidate-decision-1")).toBeInTheDocument();
    expect(screen.queryByTestId("research-candidate-decision-101")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("research-candidate-decisions-load-more"));
    await waitFor(() => {
      expect(mediaResearchApi.listCandidateDecisions).toHaveBeenCalledWith(
        candidate.id,
        { limit: 100, offset: 100 },
      );
      expect(screen.getByTestId("research-candidate-decisions")).toHaveTextContent("200/205");
    });

    await userEvent.click(screen.getByTestId("research-candidate-decisions-load-more"));
    await waitFor(() => {
      expect(mediaResearchApi.listCandidateDecisions).toHaveBeenCalledWith(
        candidate.id,
        { limit: 100, offset: 200 },
      );
      expect(screen.getByTestId("research-candidate-decisions")).toHaveTextContent("205/205");
      expect(screen.queryByTestId("research-candidate-decisions-load-more")).not.toBeInTheDocument();
    });
  });

  it("resolves promotion from the current decision page and never trusts an old page", async () => {
    const acceptedCandidate = {
      ...candidate,
      status: "accepted",
      review_state: "accepted",
      reason: "Reviewed",
      decision_version: 205,
    };
    const acceptedDecision = decision(candidate.id, 205, {
      id: "accepted-decision-205",
      event_type: "accept",
      from_status: "triaged",
      to_status: "accepted",
      reason: "Reviewed",
    });
    vi.mocked(mediaResearchApi.listCandidates).mockResolvedValue([acceptedCandidate] as never);
    vi.mocked(mediaResearchApi.listCandidateDecisions).mockImplementation(
      async (_candidateId, params) =>
        decisionPage(candidate.id, {
          current_status: "accepted",
          current_decision_version: 205,
          items: params?.offset === 200 ? [acceptedDecision] : [],
          total: 205,
          offset: params?.offset ?? 0,
          has_more: false,
        }),
    );
    vi.mocked(mediaResearchApi.promoteCandidate).mockResolvedValue({} as never);
    vi.mocked(mediaResearchApi.getCandidate).mockResolvedValue({
      ...acceptedCandidate,
      status: "promoted",
      decision_version: 206,
    } as never);

    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("candidate-status-candidate-id")).toHaveTextContent("Accepted");
    });
    await userEvent.click(screen.getByRole("button", { name: "Draftへ昇格" }));

    await waitFor(() => {
      expect(mediaResearchApi.listCandidateDecisions).toHaveBeenCalledWith(
        candidate.id,
        { limit: 100, offset: 200 },
      );
      expect(mediaResearchApi.promoteCandidate).toHaveBeenCalledWith(
        candidate.id,
        { accepted_decision_id: "accepted-decision-205" },
        expect.any(String),
      );
    });
  });

  it("enables accept/reject for an accepted candidate but keeps triage discovered-only", async () => {
    const acceptedCandidate = {
      ...candidate,
      status: "accepted",
      review_state: "accepted",
      reason: "Reviewed",
      decision_version: 1,
    };
    vi.mocked(mediaResearchApi.listCandidates).mockResolvedValue([acceptedCandidate] as never);
    vi.mocked(mediaResearchApi.listCandidateDecisions).mockResolvedValue(
      decisionPage(candidate.id, {
        current_status: "accepted",
        current_decision_version: 1,
        total: 1,
        items: [decision(candidate.id, 1, {
          event_type: "accept",
          from_status: "discovered",
          to_status: "accepted",
        })],
      }),
    );

    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("candidate-status-candidate-id")).toHaveTextContent("Accepted");
    });
    expect(screen.getByRole("button", { name: "保留" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "採用" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "却下" })).toBeEnabled();
  });

  it("preserves the selected candidate and reason after a 409 without retrying", async () => {
    vi.mocked(mediaResearchApi.listCandidates).mockResolvedValue([candidate] as never);
    vi.mocked(mediaResearchApi.listCandidateDecisions).mockResolvedValue(
      decisionPage(candidate.id),
    );
    vi.mocked(mediaResearchApi.triageCandidate).mockRejectedValue(
      new MediaResearchApiError("candidate changed", { status: 409 }),
    );

    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("candidate-status-candidate-id")).toHaveTextContent("Pending");
    });
    const reason = "Keep this source for review";
    await userEvent.type(screen.getByLabelText("Candidate decision reason"), reason);
    await userEvent.click(screen.getByRole("button", { name: "採用" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("candidate changed");
      expect(screen.getByLabelText("Candidate decision reason")).toHaveValue(reason);
      expect(screen.getByTestId("candidate-status-candidate-id")).toHaveTextContent("Pending");
      expect(mediaResearchApi.triageCandidate).toHaveBeenCalledTimes(1);
    });
  });

  it("drops a stale history response when the selected candidate changes", async () => {
    const candidateB = {
      ...candidate,
      id: "candidate-b",
      candidate_key: "candidate-b-key",
      title: "Candidate B",
    };
    const firstPage = deferred<ResearchCandidateDecisionPage>();
    const secondPage = deferred<ResearchCandidateDecisionPage>();
    vi.mocked(mediaResearchApi.listCandidates).mockResolvedValue([
      candidate,
      candidateB,
    ] as never);
    vi.mocked(mediaResearchApi.listCandidateDecisions).mockImplementation(
      async (candidateId) =>
        candidateId === candidate.id ? firstPage.promise : secondPage.promise,
    );

    render(<MediaResearchPanel />);

    await waitFor(() => {
      expect(mediaResearchApi.listCandidateDecisions).toHaveBeenCalledWith(
        candidate.id,
        { limit: 100, offset: 0 },
      );
      expect(screen.getByRole("button", { name: /Candidate B/ })).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole("button", { name: /Candidate B/ }));
    await waitFor(() => {
      expect(mediaResearchApi.listCandidateDecisions).toHaveBeenCalledWith(
        candidateB.id,
        { limit: 100, offset: 0 },
      );
    });

    firstPage.resolve(
      decisionPage(candidate.id, {
        items: [decision(candidate.id, 1, { reason: "stale response" })],
        total: 1,
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByTestId("research-candidate-decisions")).not.toHaveTextContent("stale response");

    secondPage.resolve(
      decisionPage(candidateB.id, {
        items: [decision(candidateB.id, 1, { reason: "current response" })],
        total: 1,
      }),
    );
    await waitFor(() => {
      expect(screen.getByTestId("research-candidate-decisions")).toHaveTextContent("current response");
    });
  });
});
