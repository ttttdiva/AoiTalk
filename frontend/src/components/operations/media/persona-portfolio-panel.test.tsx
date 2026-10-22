// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaPortfolioPanel } from "./persona-portfolio-panel";

const mocks = vi.hoisted(() => ({ get: vi.fn(), definition: vi.fn(), setup: vi.fn() }));
vi.mock("@/lib/media-brand-portfolio-api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/media-brand-portfolio-api")>(), mediaBrandPortfolioApi: mocks }));
const source = { sha256: "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd", filename: "SNS_v6_fixture.md" };
const definition = { source, brands: [{ brand_key: "un_real", display_name: "UN//REAL", channels: [{ platform: "youtube" }] }] };
const portfolio = { source, brands: [], owner_user_id: "owner", status: "not_configured" };
function mount(setupBlockedReason?: string) { return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><PersonaPortfolioPanel onSaved={async () => undefined} setupBlockedReason={setupBlockedReason} /></SWRConfig>); }
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  Object.defineProperty(navigator, "locks", { configurable: true, value: { request: (_: string, action: () => Promise<unknown>) => action() } });
  mocks.get.mockResolvedValue(portfolio); mocks.definition.mockResolvedValue(definition);
  mocks.setup.mockResolvedValue({ ...portfolio, status: "ready" });
});
describe("owner-pinned SNS v6 setup", () => {
  it("blocks a duplicate setup while a saved portfolio belongs to another owner", async () => {
    mount("保存済み人格の所有者のログインを確認してください。");
    const button = await screen.findByRole("button", { name: "この8人格の初期構成を用意" });
    await screen.findByText("このログインでは未登録");
    expect(button).toBeDisabled(); fireEvent.click(button);
    expect(mocks.setup).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(screen.getByRole("status")).toHaveTextContent("保存済み人格の所有者のログインを確認してください。");
  });
  it("requires a user click and sends one request with the canonical body and persisted idempotency key", async () => {
    mocks.get.mockResolvedValueOnce(portfolio).mockResolvedValueOnce(portfolio).mockResolvedValue({ ...portfolio, status: "ready" });
    mount(); const button = await screen.findByRole("button", { name: "この8人格の初期構成を用意" });
    await waitFor(() => expect(button).toBeEnabled()); expect(mocks.setup).not.toHaveBeenCalled();
    fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(mocks.setup).toHaveBeenCalledTimes(1));
    const [body, key] = mocks.setup.mock.calls[0];
    expect(body).toEqual({ expected_owner_user_id: "owner", expected_source_sha256: source.sha256, project_bindings: {} });
    expect(JSON.parse(localStorage.getItem(`aoitalk-sns-v6-setup:owner:${source.sha256}`)!)).toMatchObject({ owner: "owner", sha: source.sha256, key });
    await screen.findByText(/登録済み/);
  });
  it("rejects an owner change before any setup mutation", async () => {
    mocks.get.mockResolvedValueOnce(portfolio).mockResolvedValue({ ...portfolio, owner_user_id: "other" });
    mount(); const button = await screen.findByRole("button", { name: "この8人格の初期構成を用意" });
    await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("認証ownerまたは原文SHAが変わりました"));
    expect(mocks.setup).not.toHaveBeenCalled();
  });
  it("never automatically retries unknown, and explicitly resumes the same body/key after rollback and reload", async () => {
    mocks.setup.mockRejectedValue(new Error("保存結果不明fixture"));
    const view = mount(); const button = await screen.findByRole("button", { name: "この8人格の初期構成を用意" });
    await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
    await screen.findByText("保存結果不明fixture"); expect(mocks.setup).toHaveBeenCalledTimes(1);
    const [originalBody, originalKey] = mocks.setup.mock.calls[0];
    view.unmount(); mount(); const reloaded = await screen.findByRole("button", { name: "同じ登録操作を照合して再開" });
    await waitFor(() => expect(reloaded).toBeEnabled());
    expect(mocks.setup).toHaveBeenCalledTimes(1);
    mocks.setup.mockImplementation(async () => { mocks.get.mockResolvedValue({ ...portfolio, status: "ready" }); return { ...portfolio, status: "ready" }; });
    fireEvent.click(reloaded); fireEvent.click(reloaded);
    await screen.findByText(/登録済み/);
    expect(mocks.setup).toHaveBeenCalledTimes(2);
    expect(mocks.setup.mock.calls[1]).toEqual([originalBody, originalKey]);
  });
});
