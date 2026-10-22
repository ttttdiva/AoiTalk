// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/lib/api-types.gen";
import { PersonaDelegationsPanel } from "./persona-delegations-panel";

const mocks = vi.hoisted(() => ({ organization: vi.fn(), delegations: vi.fn(), delegationArtifact: vi.fn() }));
vi.mock("@/lib/persona-workspace-api", () => ({ personaWorkspaceApi: mocks }));
type Delegation = components["schemas"]["DelegationDTO"];
const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "revision", displayName: "UN//REAL" };
const plan = { id: "plan-artifact", phase: "plan" as const, sha256: "a".repeat(64), work_item_id: "plan-work", agent_run_id: "manager-run", size_bytes: 2048 };
const record = (patch: Partial<Delegation> = {}): Delegation => ({
  id: "root", title: "上司の制作計画", project_id: "project", persona_id: "persona", created_by: "owner", created_at: null, cancelled_at: null,
  delegator_agent_id: "manager", delegator_revision_id: "manager-rev", worker_agent_id: "writer", worker_revision_id: "writer-rev", reviewer_agent_id: "reviewer", reviewer_revision_id: "reviewer-rev",
  parent_delegation_id: null, linked_work_item_id: null, work_role: "writer", content_hash: "b".repeat(64), round: 1, state: "awaiting_plan",
  planning: { enabled: true }, phases: { plan: { state: "pending" }, work: null, review: null, return: null }, children: [], artifacts: {}, readiness: { ready: true }, ...patch,
});
const artifact = {
  ...plan, context_sha256: "c".repeat(64), evidence: {}, child_returns: [],
  content: { summary: "採用済み計画", body: "許可されたAgentへ割り当て", tasks: [{ key: "copy", title: "人格の原稿を作成", instruction: "確認済み資料だけを参照する", worker_key: "writer", reviewer_key: "reviewer", work_role: "writer" as const, scope_key: "current", max_output_tokens: 512, max_attempts: 1 }] },
};
function open() {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}><PersonaDelegationsPanel scope={scope} assignedIds={["writer"]} /></SWRConfig>);
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.organization.mockResolvedValue({ nodes: ["manager", "writer", "reviewer"].map(id => ({ agent: { id, display_name: `${id}Agent` } })), edges: [{ agent_id: "writer", manager_agent_id: "manager" }] });
  mocks.delegations.mockResolvedValue({ success: true, delegations: [record()] });
  mocks.delegationArtifact.mockResolvedValue({ success: true, artifact });
});

describe("採用計画と子Agentの成果は人格の既存委託から表示する", () => {
  it("renders optional plan and then children, rejected review, rework and parent return from explicit GET updates", async () => {
    const user = userEvent.setup(); open();
    expect(await screen.findByText("上司の計画待ち · 第1回")).toBeInTheDocument();
    expect(screen.getByText(/計画: 待機中/)).toBeInTheDocument();
    const child = record({ id: "child", title: "担当の原稿制作", parent_delegation_id: "root", state: "awaiting_review", planning: { enabled: false }, phases: { work: { state: "succeeded" }, review: { state: "pending" }, return: null } });
    mocks.delegations.mockResolvedValue({ delegations: [record({ state: "awaiting_children", artifacts: { plan }, phases: { plan: { state: "succeeded" } }, children: [{ delegation_id: "child", state: child.state }], readiness: { ready: false, error_code: "delegation_awaiting_children" } }), child, record({ id: "foreign", title: "別人格の成果", persona_id: "foreign" }), record({ id: "foreign-owner", title: "別所有者の成果", created_by: "foreign-owner" })] });
    await user.click(screen.getByRole("button", { name: "組織・委託を更新" }));
    expect(await screen.findByText("担当Agentからの返却 0/1")).toBeInTheDocument();
    expect(screen.getByText("上司への返却先: 上司の制作計画")).toBeInTheDocument();
    expect(screen.queryByText("別人格の成果")).not.toBeInTheDocument();
    expect(screen.queryByText("別所有者の成果")).not.toBeInTheDocument();
    expect(screen.queryByText(/確認コード: delegation_awaiting_children/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "採用計画を確認" }));
    expect(await screen.findByText("人格の原稿を作成")).toBeInTheDocument();
    expect(mocks.delegationArtifact).toHaveBeenCalledWith("root", plan.id);
    for (const [state, round, expected] of [["changes_requested", 1, "修正依頼 · 第1回"], ["work:pending", 2, "制作: 待機中 · 第2回（再作業）"], ["returned", 2, "返却済み · 第2回（再作業）"]] as const) {
      mocks.delegations.mockResolvedValue({ delegations: [record({ state, round, children: [{ delegation_id: "child", state: "returned" }], phases: { plan: { state: "succeeded" }, work: { state: "succeeded" }, review: { state: "succeeded" }, return: { state: state === "returned" ? "succeeded" : "pending" } } })] });
      await user.click(screen.getByRole("button", { name: "組織・委託を更新" }));
      expect(await screen.findByText(expected)).toBeInTheDocument();
    }
    expect(screen.getByText("担当Agentからの返却 1/1")).toBeInTheDocument();
  });

  it("hides adopted instructions when saved plan hash differs", async () => {
    mocks.delegations.mockResolvedValue({ delegations: [record({ artifacts: { plan } })] });
    mocks.delegationArtifact.mockResolvedValue({ artifact: { ...artifact, sha256: "d".repeat(64) } });
    const user = userEvent.setup(); open();
    await user.click(await screen.findByRole("button", { name: "採用計画を確認" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("成果のhash・担当実行記録が一致しません");
    expect(screen.queryByText("人格の原稿を作成")).not.toBeInTheDocument();
  });

  it("shows plan adoption blocker and recovers only through an explicit refresh", async () => {
    mocks.delegations.mockResolvedValue({ delegations: [record({ state: "planning_blocked", planning: { enabled: true, error_code: "delegation_plan_adoption_changed" }, readiness: { ready: false, error_code: "delegation_planning_blocked" } })] });
    const user = userEvent.setup(); open();
    expect(await screen.findByText("計画の採用停止 · 第1回")).toBeInTheDocument();
    expect(screen.getByText("確認コード: delegation_plan_adoption_changed")).toBeInTheDocument();
    mocks.delegations.mockRejectedValue(new Error("fixture 503"));
    await user.click(screen.getByRole("button", { name: "組織・委託を更新" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("この人格の組織・委託記録を取得できません");
    expect(screen.queryByText("上司の制作計画")).not.toBeInTheDocument();
    mocks.delegations.mockResolvedValue({ delegations: [record({ state: "awaiting_plan" })] });
    await user.click(screen.getByRole("button", { name: "組織・委託を更新" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("keeps the existing three phases for a delegation without manager planning", async () => {
    mocks.delegations.mockResolvedValue({ delegations: [record({ planning: { enabled: false }, phases: { work: { state: "retry_wait" }, review: null, return: null } })] });
    open();
    expect(await screen.findByText("制作: 再試行待ち / 確認: 未開始 / 返却: 未開始")).toBeInTheDocument();
    expect(screen.queryByText(/計画: /)).not.toBeInTheDocument();
  });
});
