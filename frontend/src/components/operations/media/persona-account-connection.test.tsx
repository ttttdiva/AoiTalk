// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaAccountConnection } from "./persona-account-connection";
import type {
  PlatformAccount,
  PlatformCredentialDetail,
} from "@/lib/media-operations-setup-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";

const api = vi.hoisted(() => ({ oauth: vi.fn(), startOAuth: vi.fn() }));
const setupApi = vi.hoisted(() => ({ getPlatformCredential: vi.fn() }));
vi.mock("@/lib/media-persona-publication-api", () => ({ personaPublicationApi: api }));
vi.mock("@/lib/media-operations-setup-api", () => ({ mediaOperationsSetupApi: setupApi }));

const scope = {
  ownerUserId: "owner",
  projectId: "project",
  personaId: "persona",
  personaRevisionId: "persona-revision",
  displayName: "Example Persona",
} as PersonaWorkspaceScope;
const account = {
  id: "account",
  project_id: "project",
  persona_id: "persona",
  platform: "x",
  account_ref: "@example",
  status: "active",
  current_revision: { id: "account-revision", version: 2 },
} as PlatformAccount;

function mount(variantId: string | undefined = "variant") {
  return render(
    <SWRConfig
      value={{
        provider: () => new Map(),
        revalidateOnFocus: false,
        shouldRetryOnError: false,
        dedupingInterval: 0,
      }}
    >
      <PersonaAccountConnection scope={scope} account={account} {...(variantId === undefined ? {} : { variantId })} />
    </SWRConfig>,
  );
}

function metadata(blockers: string[], configured = true) {
  return {
    platform_account_id: account.id,
    project_id: scope.projectId,
    platform: "x",
    account_ref: account.account_ref,
    account_revision: account.current_revision.version,
    credential_revision: 1,
    configured,
    blockers,
    required_scopes: ["tweet.read", "users.read"],
    redirect_uri: configured ? "https://example.test/oauth/callback" : null,
    client_ref: configured ? "nonsecret-ref" : null,
    external_checks: [],
  };
}

function credential(overrides: Partial<PlatformCredentialDetail> = {}): PlatformCredentialDetail {
  return {
    id: "credential",
    platform_account_id: account.id,
    connection_id: "connection",
    owner_user_id: scope.ownerUserId,
    project_id: scope.projectId,
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
    created_by: scope.ownerUserId,
    created_at: "2026-10-11T07:49:26Z",
    updated_at: "2026-10-11T07:49:26Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  setupApi.getPlatformCredential.mockRejectedValue({ status: 404 });
});

describe("PersonaAccountConnection OAuth blockers", () => {
  it("explains manual Bridge identity-only binding and keeps OAuth start disabled", async () => {
    api.oauth.mockResolvedValue(metadata(["oauth_manual_bridge_identity_only"]));
    mount();

    expect(
      await screen.findByText(
        "未解決の接続条件: この接続はmanual Bridgeの本人確認専用です。OAuth grantは開始できません。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "本人が公式画面で接続する" }),
    ).toBeDisabled();
    expect(screen.queryByText(/OAuth client ID\/secret.*未設定/)).not.toBeInTheDocument();
    expect(api.startOAuth).not.toHaveBeenCalled();
  });

  it("reports an inactive account without claiming its OAuth client configuration is missing", async () => {
    api.oauth.mockResolvedValue(metadata(["platform_account_inactive"]));
    mount();

    expect(
      await screen.findByText(
        "未解決の接続条件: この接続先は停止中です。実在するaccount/channelを登録してから有効化してください。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/OAuth client ID\/secret.*未設定/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "本人が公式画面で接続する" }),
    ).toBeDisabled();
  });

  it("distinguishes an imported cookie binding that is still waiting for identity verification", async () => {
    setupApi.getPlatformCredential.mockResolvedValue(credential());
    api.oauth.mockResolvedValue(metadata(["oauth_client_id_missing", "oauth_client_secret_missing", "oauth_redirect_uri_missing"], false));
    mount();

    expect(await screen.findByText("認証状態: Cookie・本人確認待ち")).toBeInTheDocument();
    expect(screen.getByText("Provider本人確認のreceiptはありません。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "本人が公式画面で接続する" })).toBeDisabled();
  });

  it("shows pending state and the slot-specific OAuth entry without a saved variant", async () => {
    setupApi.getPlatformCredential.mockResolvedValue(credential());
    api.oauth.mockResolvedValue(metadata(["oauth_client_id_missing", "oauth_client_secret_missing", "oauth_redirect_uri_missing"], false));
    mount(undefined);

    expect(await screen.findByText("認証状態: Cookie・本人確認待ち")).toBeVisible();
    expect(screen.getByText("Provider本人確認のreceiptはありません。")).toBeVisible();
    const oauthSummary = screen.getByText("X の本人接続 · @example");
    expect(oauthSummary).toBeInTheDocument();
    fireEvent.click(oauthSummary);
    expect(screen.getByRole("button", { name: "本人が公式画面で接続する" })).toBeDisabled();
    expect(screen.getByText(/OAuth client ID\/secret.*未設定/)).toBeVisible();
    expect(api.startOAuth).not.toHaveBeenCalled();
  });

  it("does not treat a pending-state timestamp as a provider identity receipt", async () => {
    setupApi.getPlatformCredential.mockResolvedValue(
      credential({ verification_started_at: "2026-10-11T07:50:26Z" }),
    );
    api.oauth.mockResolvedValue(metadata(["oauth_client_id_missing", "oauth_client_secret_missing", "oauth_redirect_uri_missing"], false));
    mount();

    expect(await screen.findByText("認証状態: Cookie・本人確認待ち")).toBeInTheDocument();
    expect(screen.getByText(/確認待ち状態の更新時刻:.*Provider本人確認receiptなし/)).toBeInTheDocument();
    expect(screen.queryByText(/Provider本人確認を開始/)).not.toBeInTheDocument();
  });

  it("distinguishes an unbound slot from a pending credential", async () => {
    api.oauth.mockResolvedValue(metadata(["oauth_client_id_missing", "oauth_client_secret_missing", "oauth_redirect_uri_missing"], false));
    mount();

    expect(await screen.findByText("認証状態: 未接続")).toBeInTheDocument();
    expect(screen.getByText("Provider本人確認のreceiptはありません。")).toBeInTheDocument();
  });

  it("shows a verified identity and an expired provider credential as distinct states", async () => {
    setupApi.getPlatformCredential.mockResolvedValueOnce(
      credential({
        connection_type: "oauth",
        status: "verified",
        verification_code: "identity_match",
        capabilities: { identity: "available", publish: "unknown", media: "unknown", analytics: "unknown" },
        last_verified_at: "2026-10-11T07:49:26Z",
      }),
    );
    api.oauth.mockResolvedValue(metadata([]));
    const view = mount();
    expect(await screen.findByText("認証状態: OAuth・本人確認済み")).toBeInTheDocument();
    expect(screen.getByText(/Provider本人確認済み/)).toBeInTheDocument();

    view.unmount();
    vi.clearAllMocks();
    setupApi.getPlatformCredential.mockResolvedValue(
      credential({ status: "invalid", verification_code: "credential_expired", verification_completed_at: "2026-10-11T07:50:26Z" }),
    );
    api.oauth.mockResolvedValue(metadata([]));
    mount();
    expect(await screen.findByText("認証状態: 認証期限切れ")).toBeInTheDocument();
    expect(screen.getByText(/最終確認結果: 認証期限切れ/)).toBeInTheDocument();
  });

  it("fails closed when verified state has no matching provider identity receipt", async () => {
    setupApi.getPlatformCredential.mockResolvedValue(
      credential({ connection_type: "cookie_export", status: "verified", last_verified_at: "2026-10-11T07:49:26Z" }),
    );
    api.oauth.mockResolvedValue(metadata([]));
    mount();

    expect(await screen.findByText("認証状態: Cookie・確認receipt要照合")).toBeInTheDocument();
    expect(screen.getByText("Provider本人確認のreceiptを確認できません。接続状態を再取得してください。")).toBeInTheDocument();
    expect(screen.queryByText(/Provider本人確認済み/)).not.toBeInTheDocument();
  });

  const receiptMismatches: Array<[string, Partial<PlatformCredentialDetail>]> = [
    ["non-OAuth credential", { connection_type: "cookie_export" }],
    ["missing identity match", { verification_code: null }],
    ["unavailable identity capability", { capabilities: { identity: "unknown", publish: "unknown", media: "unknown", analytics: "unknown" } }],
    ["missing verification time", { last_verified_at: null }],
  ];

  it.each(receiptMismatches)("does not mark a verified record as identity-confirmed with %s", async (_label, mismatch) => {
    setupApi.getPlatformCredential.mockResolvedValue(credential({
      connection_type: "oauth",
      status: "verified",
      verification_code: "identity_match",
      capabilities: { identity: "available", publish: "unknown", media: "unknown", analytics: "unknown" },
      last_verified_at: "2026-10-11T07:49:26Z",
      ...mismatch,
    }));
    api.oauth.mockResolvedValue(metadata([]));
    mount();

    const credentialState = await screen.findByTestId("persona-account-credential-state");
    expect(credentialState).not.toHaveTextContent("本人確認済み");
    expect(screen.queryByText(/Provider本人確認済み/)).not.toBeInTheDocument();
  });
});
