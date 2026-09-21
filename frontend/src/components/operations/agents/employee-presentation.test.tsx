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

describe("AI社員のラベルと因果イベント表示", () => {
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
    render(<><EmployeeField label="業務指示"><textarea id="existing-instructions" value="事前入力" readOnly /></EmployeeField><EmployeeSelect label="Agent Team" value="employee" onChange={() => undefined} options={[{ value: "employee", label: "社員チーム" }]} /></>);
    expect(screen.getByLabelText("業務指示", { exact: true })).toHaveAttribute("id", "existing-instructions");
    expect(screen.getByLabelText("Agent Team", { exact: true })).toBe(screen.getByRole("combobox", { name: "Agent Team" }));
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
    fireEvent.click(screen.getByRole("button", { name: "活動状態を更新" }));
    await waitFor(() => expect(within(panel).getAllByRole("listitem")[0]).toHaveTextContent("action_attempt"));
    expect(within(panel).getAllByRole("listitem")).toHaveLength(2);
    expect(errors.mock.calls.filter(args => args.some(value => typeof value === "string" && /same key|unique.*key|duplicate.*key/iu.test(value)))).toEqual([]);
  });
});
