// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaWorkspace } from "./persona-workspace";

const mocks = vi.hoisted(() => ({ query: "tab=studio", replace: vi.fn(), list: vi.fn(), detail: vi.fn(), accounts: vi.fn(), portfolio: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: mocks.replace }), useSearchParams: () => new URLSearchParams(mocks.query) }));
vi.mock("next/link", () => ({ default: ({ href, children, ...props }: React.ComponentProps<"a">) => <a href={href} {...props}>{children}</a> }));
vi.mock("@/lib/media-operations-api", () => ({ mediaOperationsApi: { listPersonas: mocks.list, getPersona: mocks.detail } }));
vi.mock("@/lib/media-brand-portfolio-api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/media-brand-portfolio-api")>(), mediaBrandPortfolioApi: { get: mocks.portfolio } }));
vi.mock("@/lib/media-operations-setup-api", () => ({ mediaOperationsSetupApi: { listPlatformAccounts: mocks.accounts } }));
vi.mock("./persona-portfolio-panel", () => ({ PersonaPortfolioPanel: ({ setupBlockedReason }: { setupBlockedReason?: string }) => <div data-testid="portfolio-fixture" data-setup-blocked={Boolean(setupBlockedReason)}>構成fixture</div> }));
vi.mock("./persona-team-panel", () => ({ PersonaTeamPanel: () => <div>担当fixture</div> }));
vi.mock("./persona-brief-panel", () => ({ PersonaBriefPanel: ({ scope }: { scope: { displayName: string } }) => <div>制作brief: {scope.displayName}</div> }));
vi.mock("./generation-panel", () => ({ MediaGenerationPanel: () => <div>計画fixture</div> }));
vi.mock("./content-variant-panel", () => ({ MediaContentVariantPanel: () => <div>QA fixture</div> }));

const persona = (id: string, name: string) => ({ id, owner_user_id: "owner", project_id: `project-${id}`, state: "active", current_revision: { id: `revision-${id}`, persona_id: id, owner_user_id: "owner", project_id: `project-${id}`, display_name: name, platforms: ["x", "youtube"], summary: `戦略 ${name}`, kpi_objectives: ["保存率"] } });
const a = persona("a", "刺されても死なない大きなクチバシ");
const b = persona("b", "UN//REAL");
function mount() { return render(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, dedupingInterval: 0 }}><PersonaWorkspace /></SWRConfig>); }
const source = { sha256: "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd" };
beforeEach(() => { vi.clearAllMocks(); mocks.query = "tab=studio"; mocks.portfolio.mockResolvedValue({ owner_user_id: "owner", status: "ready", source, headquarters_project_id: "hq", brands: [a,b].map(p => ({ project_id: p.project_id, persona: p, definition: { brand_key: p.id, display_name: p.current_revision.display_name }, channels: [] })) }); mocks.list.mockResolvedValue([a,b]); mocks.detail.mockImplementation(async id => id === "a" ? a : b); mocks.accounts.mockResolvedValue([]); });
describe("persona workspace selection", () => {
  it("explains an existing portfolio under another owner without selecting it or offering duplicate setup", async () => {
    const saved = Array.from({ length: 8 }, (_, index) => {
      const p = persona(`saved-${index}`, `保存人格${index}`);
      return { ...p, owner_user_id: "other", parent_brand_ref: `sns-v6:brand-${index}`,
        current_revision: { ...p.current_revision, owner_user_id: "other", research_policy: { source_sha256: source.sha256 } } };
    });
    mocks.portfolio.mockResolvedValue({ owner_user_id: "owner", status: "not_configured", source, brands: [] });
    mocks.list.mockResolvedValue(saved); mocks.query = "tab=studio&persona=saved-0"; mount();
    await screen.findByText(/保存済みのSNS v6人格が別の所有者に8件/);
    expect(screen.queryByText(/公開人格がまだ登録されていません/)).not.toBeInTheDocument();
    expect(screen.getByTestId("portfolio-fixture")).toHaveAttribute("data-setup-blocked", "true");
    expect(screen.queryByRole("link", { name: /保存人格/ })).not.toBeInTheDocument();
    expect(mocks.detail).not.toHaveBeenCalled(); expect(mocks.accounts).not.toHaveBeenCalled();
  });
  it("does not treat an unrelated authorized persona as a saved SNS v6 portfolio", async () => {
    const p = persona("foreign", "一般の人格");
    mocks.portfolio.mockResolvedValue({ owner_user_id: "owner", status: "not_configured", source, brands: [] });
    mocks.list.mockResolvedValue([{ ...p, owner_user_id: "other", current_revision: { ...p.current_revision, owner_user_id: "other" } }]);
    mount(); await screen.findByText(/このログインの所有者には公開人格がまだ登録されていません/);
    expect(screen.getByTestId("portfolio-fixture")).toHaveAttribute("data-setup-blocked", "false");
    expect(screen.queryByText(/保存済みのSNS v6人格が別の所有者/)).not.toBeInTheDocument();
  });
  it("uses canonical personas and shows only the authenticated portfolio owner/project bindings", async () => {
    const foreign = { ...a, id: "foreign", owner_user_id: "other", current_revision: { ...a.current_revision, display_name: "別ownerの人格" } };
    const wrongProject = { ...b, project_id: "other-project", current_revision: { ...b.current_revision, display_name: "別Projectの人格" } };
    mocks.list.mockResolvedValue([a, foreign, wrongProject]); mount();
    await screen.findByRole("link", { name: /刺されても死なない大きなクチバシ/ });
    expect(mocks.list).toHaveBeenCalledWith();
    expect(screen.queryByRole("link", { name: /別owner|別Project/ })).not.toBeInTheDocument();
    expect(mocks.detail).not.toHaveBeenCalled();
  });
  it("does not use a persona list to infer an owner when authenticated portfolio lookup fails", async () => {
    mocks.portfolio.mockRejectedValue(new Error("503")); mocks.query = "tab=studio&persona=b"; mount();
    await waitFor(() => expect(mocks.portfolio).toHaveBeenCalled());
    expect(mocks.list).not.toHaveBeenCalled(); expect(mocks.detail).not.toHaveBeenCalled(); expect(mocks.accounts).not.toHaveBeenCalled();
  });
  it("recovers an initial read failure without creating a portfolio or selecting another owner", async () => {
    const ready = await mocks.portfolio();
    mocks.portfolio.mockRejectedValueOnce(Object.assign(new Error("temporarily unavailable"), { status: 503 })).mockResolvedValue(ready);
    mount();
    await screen.findByRole("button", { name: "制作構成を再取得" });
    expect(mocks.list).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "制作構成を再取得" }));
    await screen.findByRole("link", { name: /^UN\/\/REAL/ });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mocks.detail).not.toHaveBeenCalled();
  });

  it("requires a persona selection and preserves the eight-brand naming surface", async () => {
    mount(); await screen.findByRole("link", { name: /刺されても死なない大きなクチバシ/ });
    expect(screen.getByRole("link", { name: /^UN\/\/REAL/ })).toHaveAttribute("href", "/operations?tab=studio&persona=b");
    expect(screen.getByText(/公開人格を選択すると/)).toBeInTheDocument();
    expect(mocks.detail).not.toHaveBeenCalled(); expect(mocks.accounts).not.toHaveBeenCalled();
  });
  it("uses the selected persona for account reads and stage navigation", async () => {
    mocks.query = "tab=studio&persona=b"; mount(); await screen.findByText("制作brief: UN//REAL");
    expect(mocks.accounts).toHaveBeenCalledWith("project-b", "b");
    fireEvent.click(screen.getByRole("button", { name: /4\. preview・公開/ }));
    expect(mocks.replace).toHaveBeenCalledWith("/operations?tab=studio&persona=b&stage=publish", { scroll: false });
  });
  it("does not fall back to the first persona when the requested persona is missing", async () => {
    mocks.query = "tab=studio&persona=missing"; mount(); await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("指定された人格を確認できません");
    expect(mocks.detail).not.toHaveBeenCalled(); expect(mocks.accounts).not.toHaveBeenCalled();
  });
  it("shows a concrete error for a failed or cross-owner detail and never fetches another account", async () => {
    mocks.query = "tab=studio&persona=b"; mocks.detail.mockResolvedValue({ ...b, owner_user_id: "other" }); mount();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("所属が選択したProjectと一致しません"));
    expect(mocks.accounts).not.toHaveBeenCalled(); expect(mocks.detail).toHaveBeenCalledWith("b");
  });
  it("clears the previous persona while a different persona detail is pending", async () => {
    mocks.query = "tab=studio&persona=a"; const result = mount(); await screen.findByText(`制作brief: ${a.current_revision.display_name}`);
    mocks.detail.mockImplementation(() => new Promise(() => {})); mocks.query = "tab=studio&persona=b";
    result.rerender(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }}><PersonaWorkspace /></SWRConfig>);
    await screen.findByText("選択した人格を確認中…");
    expect(screen.queryByText(`制作brief: ${a.current_revision.display_name}`)).not.toBeInTheDocument();
  });
});
