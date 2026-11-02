// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { OperationsMapAuthoring } from "./operations-map-authoring";
const mocks = vi.hoisted(() => ({ query: new URLSearchParams(), push: vi.fn(), manage: true }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }), useSearchParams: () => mocks.query }));
vi.mock("@/contexts/runtime-context", () => ({ useOptionalRuntimeContext: () => ({ runtimeFeatures: { application_features: { virtual_company: true, autonomous_agent_runtime: true } } }) }));
vi.mock("@/components/providers/swr-global-provider", () => ({ useCurrentUserId: () => "owner" }));
vi.mock("@/lib/agent-employees-api", () => ({ employeeApi: { list: async () => ({ agents: [] }), catalog: async () => ({ can_manage: mocks.manage }) } }));
vi.mock("./agents/team-graph-workspace", () => ({ TeamGraphWorkspace: ({ onDirtyChange }: { onDirtyChange: (value: boolean) => void }) => <button onClick={() => onDirtyChange(true)}>編集</button> }));
const mount = () => render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><OperationsMapAuthoring><p>実行の履歴</p></OperationsMapAuthoring></SWRConfig>);
beforeEach(() => { mocks.query = new URLSearchParams("tab=overview&overview_view=map"); mocks.push.mockReset(); mocks.manage = true; });
describe("Operations Map mode", () => {
  it("opens design in the map route", () => {
    mount(); fireEvent.click(screen.getByRole("button", { name: "Team設計" }));
    expect(mocks.push).toHaveBeenCalledWith("/operations?tab=overview&overview_view=map&map_mode=design", { scroll: false });
    expect(screen.getByText("実行の履歴")).toBeVisible();
  });
  it("guards a dirty design when switching", async () => {
    mocks.query.set("map_mode", "design"); mount(); fireEvent.click(await screen.findByText("編集"));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(screen.getByRole("button", { name: "実行状況" })); expect(confirm).toHaveBeenCalled(); expect(mocks.push).not.toHaveBeenCalled();
    confirm.mockReturnValue(true); fireEvent.click(screen.getByRole("button", { name: "実行状況" }));
    expect(mocks.push).toHaveBeenCalledWith("/operations?tab=overview&overview_view=map", { scroll: false }); confirm.mockRestore();
  });
  it("requires management permission", async () => {
    mocks.query.set("map_mode", "design"); mocks.manage = false; mount();
    expect(await screen.findByText("Team設計の編集には管理権限が必要です。")).toBeVisible(); expect(screen.queryByText("編集")).not.toBeInTheDocument();
  });
});
