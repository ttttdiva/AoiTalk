// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaWorkspace } from "./persona-workspace";

const mocks = vi.hoisted(() => ({
  query: "tab=studio&persona=x-persona",
  personas: vi.fn(),
  detail: vi.fn(),
  portfolio: vi.fn(),
  accounts: vi.fn(),
  credential: vi.fn(),
  oauth: vi.fn(),
  stock: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), useSearchParams: () => new URLSearchParams(mocks.query) }));
vi.mock("next/link", () => ({ default: ({ href, children, ...props }: React.ComponentProps<"a">) => <a href={href} {...props}>{children}</a> }));
vi.mock("@/lib/media-operations-api", () => ({ mediaOperationsApi: { listPersonas: mocks.personas, getPersona: mocks.detail, listStockItems: mocks.stock } }));
vi.mock("@/lib/media-operations-setup-api", () => ({ mediaOperationsSetupApi: { listPlatformAccounts: mocks.accounts, getPlatformCredential: mocks.credential } }));
vi.mock("@/lib/media-persona-publication-api", () => ({ personaPublicationApi: { oauth: mocks.oauth, startOAuth: vi.fn() } }));
vi.mock("@/lib/media-brand-portfolio-api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/media-brand-portfolio-api")>(), mediaBrandPortfolioApi: { get: mocks.portfolio } }));
vi.mock("./persona-portfolio-panel", () => ({ PersonaPortfolioPanel: () => null }));
vi.mock("./persona-team-panel", () => ({ PersonaTeamPanel: () => null }));
vi.mock("./persona-brief-panel", () => ({ PersonaBriefPanel: () => null }));
vi.mock("./generation-panel", () => ({ MediaGenerationPanel: () => null }));
vi.mock("./content-variant-panel", () => ({ MediaContentVariantPanel: () => null }));
vi.mock("./persona-publish-panel", () => ({ PersonaPublishPanel: () => null }));
vi.mock("./persona-asset-panel", () => ({ PersonaAssetPanel: () => null }));
vi.mock("./persona-bridge-connection", () => ({ PersonaBridgeConnectionPanel: () => null }));

const persona = {
  id: "x-persona",
  owner_user_id: "owner",
  project_id: "x-project",
  state: "active",
  current_revision: {
    id: "persona-revision",
    persona_id: "x-persona",
    owner_user_id: "owner",
    project_id: "x-project",
    display_name: "OFFBY1",
    platforms: ["x"],
    summary: "OFFBY1 strategy",
    kpi_objectives: [],
  },
};
const source = { sha256: "8f3cade315b6866743748e0ff8e2567759793685b89a2cf7b5e1300e0ca30edd" };
const account = {
  id: "x-account",
  platform: "x",
  account_ref: "@XDouphe9619",
  status: "active",
  connection_id: "connection",
  persona_id: persona.id,
  project_id: persona.project_id,
  owner_user_id: persona.owner_user_id,
  current_revision: {
    id: "account-revision",
    version: 2,
    platform_account_id: "x-account",
    project_id: persona.project_id,
    owner_user_id: persona.owner_user_id,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.query = "tab=studio&persona=x-persona";
  mocks.portfolio.mockResolvedValue({ owner_user_id: persona.owner_user_id, status: "ready", source, headquarters_project_id: "hq", brands: [{ project_id: persona.project_id, persona: { id: persona.id }, definition: { brand_key: "offby1", display_name: "OFFBY1" }, channels: [] }] });
  mocks.personas.mockResolvedValue([persona]);
  mocks.detail.mockResolvedValue(persona);
  mocks.accounts.mockResolvedValue([account]);
  mocks.stock.mockResolvedValue([{ id: "qa-item", project_id: persona.project_id, owner_user_id: "owner", brand_name: "OFFBY1", title: "QA Operations Saved Content 20261010", brief: "", status: "draft", version: 1, content_hash: "h", stock_status: "awaiting_adoption", blockers: ["qa_missing"], findings: [], variants: [], latest_decision: null }, { id: "real-item", project_id: persona.project_id, owner_user_id: "owner", brand_name: "OFFBY1", title: "OFFBY1 初期制作設計", brief: "", status: "draft", version: 1, content_hash: "h2", stock_status: "awaiting_adoption", blockers: ["analysis_evidence_missing"], findings: [], variants: [], latest_decision: null }]);
  mocks.credential.mockResolvedValue({
    id: "credential",
    platform_account_id: account.id,
    connection_id: account.connection_id,
    owner_user_id: account.owner_user_id,
    project_id: account.project_id,
    platform: "x",
    account_ref: account.account_ref,
    connection_type: "cookie_export",
    revision: 1,
    status: "verification_pending",
    capabilities: { identity: "unknown", publish: "unknown", media: "unknown", analytics: "unknown" },
    verification_code: null,
    verification_started_at: null,
    verification_completed_at: null,
    last_verified_at: null,
    disabled_at: null,
    created_by: account.owner_user_id,
    created_at: "2026-10-11T07:49:26Z",
    updated_at: "2026-10-11T07:49:26Z",
  });
  mocks.oauth.mockResolvedValue({
    platform_account_id: account.id,
    project_id: account.project_id,
    platform: "x",
    account_ref: account.account_ref,
    account_revision: account.current_revision.version,
    credential_revision: 1,
    configured: false,
    blockers: ["oauth_client_id_missing", "oauth_client_secret_missing", "oauth_redirect_uri_missing"],
    required_scopes: ["tweet.read", "users.read"],
    redirect_uri: null,
    client_ref: null,
    external_checks: [],
  });
});

describe("PersonaWorkspace X slot connection", () => {
  it("shows the cookie pending state and a disabled per-slot OAuth entry without a saved variant", async () => {
    render(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, revalidateOnFocus: false }}><PersonaWorkspace /></SWRConfig>);

    expect(await screen.findByText("認証状態: Cookie・本人確認待ち")).toBeVisible();
    expect(screen.getByText("Provider本人確認のreceiptはありません。")).toBeVisible();
    expect(screen.queryByText("接続設定あり・公開時に準備状態を確認")).not.toBeInTheDocument();
    expect(mocks.credential).toHaveBeenCalledWith(account.id);
    expect(mocks.oauth).toHaveBeenCalledWith(account.id);

    expect(screen.getByText("X の本人接続 · @XDouphe9619")).toBeVisible();
    expect(screen.getByRole("button", { name: "本人が公式画面で接続する" })).toBeVisible();
    expect(screen.getByRole("button", { name: "本人が公式画面で接続する" })).toBeDisabled();
    expect(await screen.findByText(/OAuth client ID\/secret.*未設定/)).toBeVisible();
  });

  it("puts the 本人接続 card before products and hides QA products", async () => {
    render(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, revalidateOnFocus: false }}><PersonaWorkspace /></SWRConfig>);
    const connections = await screen.findByTestId("persona-connections");
    expect(await screen.findAllByText("OFFBY1 初期制作設計")).toHaveLength(2);
    const products = screen.getByTestId("content-stock-panel");
    const stages = screen.getByRole("navigation", { name: "人格の制作工程" });
    expect(connections.compareDocumentPosition(products) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(connections.compareDocumentPosition(stages) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText("QA Operations Saved Content 20261010")).not.toBeInTheDocument();
    expect(await screen.findByText(/X で本人接続する/)).toBeVisible();
  });
});
