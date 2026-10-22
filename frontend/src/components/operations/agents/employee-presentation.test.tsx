// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EmployeeField, EmployeeSelect } from "./employee-fields";
import { OperationsCommandCenter, __operationsCommandCenterTestUtils as projection } from "../operations-command-center";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), useSearchParams: () => new URLSearchParams("tab=agents&agent=employee&panel=activity"), usePathname: () => "/operations" }));
vi.mock("@/contexts/runtime-context", () => ({ useOptionalRuntimeContext: () => ({ runtimeFeatures: { application_features: { virtual_company: true, autonomous_agent_runtime: true } } }) }));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const causalRows = [
  { id: "same-attempt-id", kind: "action_authorization", action_id: "action-id", authorization_mode: "bounded_policy", created_at: "2026-09-08T01:00:00Z" },
  { id: "same-attempt-id", kind: "action_attempt", action_id: "action-id", status: "succeeded", started_at: "2026-09-08T01:00:00Z" },
];

describe("Agentのラベルと因果イベント表示", () => {
  it("keeps an exact label independent of a controlled textarea's prefilled content after remount", () => {
    const field = (version: number, value: string) => <EmployeeField key={version} label="ミッション"><textarea value={value} readOnly /></EmployeeField>;
    const { rerender } = render(field(1, ""));
    expect(screen.getByLabelText("ミッション", { exact: true })).toHaveValue("");
    rerender(field(2, "保存済みの職務 v2"));
    const control = screen.getByLabelText("ミッション", { exact: true }) as HTMLTextAreaElement;
    expect(control).toHaveValue("保存済みの職務 v2");
    expect(control.labels?.[0].textContent).toBe("ミッション");
    expect(control.labels?.[0].htmlFor).toBe(control.id);
    expect(control.closest("label")).toBeNull();
  });

  it("preserves a caller-provided id and the composite AppSelect accessible name", () => {
    render(<><EmployeeField label="業務指示"><textarea id="existing-instructions" value="事前入力" readOnly /></EmployeeField><EmployeeSelect label="Execution Team" value="employee" onChange={() => undefined} options={[{ value: "employee", label: "Agentチーム" }]} /></>);
    expect(screen.getByLabelText("業務指示", { exact: true })).toHaveAttribute("id", "existing-instructions");
    expect(screen.getByLabelText("Execution Team", { exact: true })).toBe(screen.getByRole("combobox", { name: "Execution Team" }));
  });

  it("retains causal kind in the normalized snapshot even when authorization and attempt share an ID", () => {
    const rows = projection.normaliseSnapshot({ activity: causalRows }).activity;
    expect(rows).toHaveLength(2);
    expect(rows.map(row => ({ id: row.id, kind: row.kind }))).toEqual([
      { id: "same-attempt-id", kind: "action_authorization" },
      { id: "same-attempt-id", kind: "action_attempt" },
    ]);
    expect(projection.normaliseSnapshot({ activity: [{ id: "legacy", type: "legacy_event" }] }).activity[0].kind).toBe("legacy_event");
  });

  it("keeps hierarchy entities and unassigned agents in the operations map model", () => {
    const snapshot = {
      organization: { display_name: "AoiTalk" },
      summary: { active_agents: 2, working: 1 },
      spaces: [{ id: "space-1", name: "受託運用" }],
      projects: [{ id: "project-1", name: "案件運用", space_id: "space-1" }],
      agents: [
        {
          id: "agent-1",
          display_name: "案件担当",
          state: "active",
          job_title: "Operator",
          responsibility_summary: "案件を処理する",
          primary_space_id: "space-1",
          project_grants: [{ project_id: "project-1", state: "active" }],
          space_assignments: [],
          current_work: { id: "work-1", source_type: "task", state: "running" },
        },
        {
          id: "agent-2",
          display_name: "CrowdWorks担当",
          state: "active",
          job_title: "CrowdWorks担当",
          responsibility_summary: "案件探索からレビュー待ちまで",
          project_grants: [],
          space_assignments: [],
        },
        { id: "space-only", display_name: "Space専任", primary_space_id: "space-1" },
        { id: "missing-space", display_name: "表示範囲外の所属担当", primary_space_id: "space-not-returned" },
        { id: "missing-project", display_name: "表示範囲外のProject担当", project_grants: [{ project_id: "project-not-returned" }] },
        { id: "orphan-agent", display_name: "独立Project担当", project_grants: [{ project_id: "orphan-project" }] },
      ],
      current_work: [{ id: "work-1", source_type: "task", state: "running", task_title: "案件レビュー" }],
      attention: [],
      activity: [],
    };
    snapshot.projects.push({ id: "orphan-project", name: "独立Project", space_id: "" });
    const data = projection.normaliseSnapshot(snapshot);
    const graph = projection.buildOperationsMap(data);
    const label = (entityId: string) => graph.nodes.find(node => node.entityId === entityId)?.label;
    expect(label("space-1")).toBe("受託運用");
    expect(label("project-1")).toBe("案件運用");
    expect(label("agent-1")).toBe("案件担当");
    expect(label("agent-2")).toBe("CrowdWorks担当");
    expect(label("space-only")).toBe("Space専任");
    expect(label("missing-space")).toBe("表示範囲外の所属担当");
    expect(label("missing-project")).toBe("表示範囲外のProject担当");
    expect(label("orphan-agent")).toBe("独立Project担当");
    expect(label("orphan-project")).toBe("独立Project");
    expect(graph.nodes.some(node => node.id === "work:work-1")).toBe(true);
  });

  it("renders and reorders both same-ID causal rows without duplicate React keys", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    let activity = causalRows;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ activity }), { status: 200 })));
    render(<OperationsCommandCenter view="activity" agentId="employee" />);
    const panel = await screen.findByTestId("operations-command-center-activity");
    await waitFor(() => expect(within(panel).getAllByRole("listitem")).toHaveLength(2));
    expect(within(panel).getByText("action_authorization", { exact: true })).toBeVisible();
    expect(within(panel).getByText("action_attempt", { exact: true })).toBeVisible();
    activity = [...causalRows].reverse();
    fireEvent.click(screen.getByRole("button", { name: "更新" }));
    await waitFor(() => expect(within(panel).getAllByRole("listitem")[0]).toHaveTextContent("action_attempt"));
    expect(within(panel).getAllByRole("listitem")).toHaveLength(2);
    expect(errors.mock.calls.filter(args => args.some(value => typeof value === "string" && /same key|unique.*key|duplicate.*key/iu.test(value)))).toEqual([]);
  });
});
