// @vitest-environment jsdom

import {
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

import { PlatformAccountPanel } from "@/components/operations/media/platform-account-panel";
import { mediaOperationsSetupApi } from "@/lib/media-operations-setup-api";

vi.mock(
  "@/lib/media-operations-setup-api",
  async () => {
    const actual =
      await vi.importActual<
        typeof import("@/lib/media-operations-setup-api")
      >("@/lib/media-operations-setup-api");

    return {
      ...actual,
      mediaOperationsSetupApi: {
        importPersonaBulkDraft: vi.fn(),
        getPersonaBulkDraft: vi.fn(),
        correctPersonaBulkDraft: vi.fn(),
        applyPersonaBulkDraft: vi.fn(),
        listPlatformAccounts: vi.fn(),
        createPlatformAccount: vi.fn(),
        getPlatformAccount: vi.fn(),
        appendPlatformAccountRevision: vi.fn(),
        addCharacterPlatformConnection: vi.fn(),
        getPlatformCredential: vi.fn(),
        createPlatformCredential: vi.fn(),
        rotatePlatformCredential: vi.fn(),
        verifyPlatformCredential: vi.fn(),
        disablePlatformCredential: vi.fn(),
        listPlatformCredentialAudit: vi.fn(),
      },
    };
  },
);

const account = {
  id: "account-1",
  platform: "x",
  account_ref: "x-user",
  status: "active",
  current_revision: {
    display_name: "X account",
    publish_capability: "available",
    media_capability: "unknown",
    analytics_capability: "unsupported",
    credential_status: "configured",
  },
};

const credential = {
  id: "credential-1",
  platform_account_id: "account-1",
  connection_id: "connection-1",
  owner_user_id: "owner-1",
  project_id: null,
  connection_type: "cookie_export",
  revision: 3,
  state_hash: "safe-state-hash",
  status: "configured",
  capabilities: {
    identity: "available",
    publish: "available",
    media: "unknown",
    analytics: "unsupported",
  },
  verification_code: "verified",
  last_verification_attempt_at: "2026-09-02T00:00:00Z",
  last_verified_at: "2026-09-02T00:00:00Z",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-02T00:00:00Z",
};

function setEmptyAccounts() {
  vi.mocked(mediaOperationsSetupApi.listPlatformAccounts).mockResolvedValue([]);
  vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockResolvedValue(null as never);
}

describe("PlatformAccountPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setEmptyAccounts();
    vi.mocked(mediaOperationsSetupApi.addCharacterPlatformConnection).mockResolvedValue({
      platform_account: account,
      credential,
    } as never);
    vi.mocked(mediaOperationsSetupApi.createPlatformCredential).mockResolvedValue(credential as never);
    vi.mocked(mediaOperationsSetupApi.verifyPlatformCredential).mockResolvedValue(credential as never);
    vi.mocked(mediaOperationsSetupApi.rotatePlatformCredential).mockResolvedValue(credential as never);
    vi.mocked(mediaOperationsSetupApi.disablePlatformCredential).mockResolvedValue({
      ...credential,
      status: "disabled",
      revision: 4,
    } as never);
    vi.mocked(mediaOperationsSetupApi.listPlatformCredentialAudit).mockResolvedValue([]);
  });

  it("loads accounts for the selected Character and never renders a secret textbox", async () => {
    render(<PlatformAccountPanel characterId="character-1" />);

    await waitFor(() => {
      expect(mediaOperationsSetupApi.listPlatformAccounts).toHaveBeenCalledWith(
        null,
        "character-1",
      );
    });

    expect(screen.queryByRole("textbox", { name: /password|token|secret/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/password|token|secret/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText("接続パッケージファイルを選択")).toHaveAttribute("type", "file");
    expect(screen.queryByText(/safe-state-hash/i)).not.toBeInTheDocument();
  });

  it("uploads a bounded package through the Character connection endpoint and clears the file", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    render(<PlatformAccountPanel characterId="character-1" onChanged={onChanged} />);

    await waitFor(() => {
      expect(mediaOperationsSetupApi.listPlatformAccounts).toHaveBeenCalled();
    });

    await user.type(screen.getByLabelText("Account reference"), "x-user");
    await user.type(screen.getByLabelText("表示名"), "X account");
    const file = new File(["{\"cookie\":\"redacted\"}"], "package.json", { type: "application/json" });
    await user.upload(screen.getByLabelText("接続パッケージファイルを選択"), file);
    await user.click(screen.getByRole("button", { name: "接続を追加" }));

    await waitFor(() => {
      expect(mediaOperationsSetupApi.addCharacterPlatformConnection).toHaveBeenCalledTimes(1);
    });

    const [characterId, input, key] = vi.mocked(mediaOperationsSetupApi.addCharacterPlatformConnection).mock.calls[0];
    expect(characterId).toBe("character-1");
    expect(input.platform).toBe("x");
    expect(input.account_ref).toBe("x-user");
    expect(input.display_name).toBe("X account");
    expect(input.connection_type).toBe("cookie_export");
    expect(input.package).toBe(file);
    expect(key).toEqual(expect.any(String));
    expect(screen.getByLabelText("接続パッケージファイルを選択")).toHaveValue("");
    expect(onChanged).toHaveBeenCalled();
  });

  it("rejects packages larger than 2MB before making a request", async () => {
    const user = userEvent.setup();
    render(<PlatformAccountPanel characterId="character-1" />);

    await waitFor(() => {
      expect(mediaOperationsSetupApi.listPlatformAccounts).toHaveBeenCalled();
    });

    const oversized = new File([new Uint8Array(2 * 1024 * 1024 + 1)], "too-large.json", {
      type: "application/json",
    });
    await user.upload(screen.getByLabelText("接続パッケージファイルを選択"), oversized);

    expect(await screen.findByRole("alert")).toHaveTextContent("2MB以下");
    expect(screen.getByRole("button", { name: "接続を追加" })).toBeDisabled();
    expect(mediaOperationsSetupApi.addCharacterPlatformConnection).not.toHaveBeenCalled();
  });

  it("uses revisioned verify, rotate, disable, and audit handlers", async () => {
    const user = userEvent.setup();
    vi.mocked(mediaOperationsSetupApi.listPlatformAccounts).mockResolvedValue([account] as never);
    vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockResolvedValue(credential as never);
    render(<PlatformAccountPanel characterId="character-1" />);

    await waitFor(() => {
      expect(screen.getByTestId("platform-account-list")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "検証" }));
    await waitFor(() => {
      expect(mediaOperationsSetupApi.verifyPlatformCredential).toHaveBeenCalledWith(
        "account-1",
        3,
        expect.any(String),
      );
    });

    await user.click(screen.getByRole("button", { name: "無効化" }));
    await waitFor(() => {
      expect(mediaOperationsSetupApi.disablePlatformCredential).toHaveBeenCalledWith(
        "account-1",
        3,
        expect.any(String),
      );
    });

    const rotateFile = new File(["next package"], "rotate.json", { type: "application/json" });
    await user.upload(screen.getByLabelText("接続パッケージファイルをローテーション"), rotateFile);
    await waitFor(() => {
      expect(mediaOperationsSetupApi.rotatePlatformCredential).toHaveBeenCalledWith(
        "account-1",
        expect.objectContaining({
          expected_revision: 4,
          package: rotateFile,
          connection_type: "cookie_export",
        }),
        expect.any(String),
      );
    });

    await user.click(screen.getByRole("button", { name: "監査履歴" }));
    await waitFor(() => {
      expect(mediaOperationsSetupApi.listPlatformCredentialAudit).toHaveBeenCalledWith("account-1");
    });
  });

  it("attaches a package to an existing PlatformAccount without exposing its contents", async () => {
    const user = userEvent.setup();
    vi.mocked(mediaOperationsSetupApi.listPlatformAccounts).mockResolvedValue([account] as never);
    vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockRejectedValue({ status: 404 });
    const onChanged = vi.fn();
    render(<PlatformAccountPanel characterId="character-1" onChanged={onChanged} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "認証情報を追加" })).toBeEnabled();
    });
    const file = new File(["{\"access_token\":\"redacted\"}"], "package.json", { type: "application/json" });
    await user.upload(screen.getByLabelText("接続パッケージファイルを添付"), file);

    await waitFor(() => {
      expect(mediaOperationsSetupApi.createPlatformCredential).toHaveBeenCalledWith(
        "account-1",
        { package: file, connection_type: "cookie_export" },
        expect.any(String),
      );
    });
    expect(onChanged).toHaveBeenCalled();
    expect(screen.queryByText(file.name)).not.toBeInTheDocument();
  });

  it("clears the previous Character projection before loading the next one", async () => {
    vi.mocked(mediaOperationsSetupApi.listPlatformAccounts).mockImplementation(
      async (_projectId, personaId) => (personaId === "character-1" ? [account] as never : []),
    );
    vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockResolvedValue(credential as never);

    const { rerender } = render(<PlatformAccountPanel characterId="character-1" />);
    await waitFor(() => {
      expect(screen.getByText("X account")).toBeInTheDocument();
    });

    rerender(<PlatformAccountPanel characterId="character-2" />);
    expect(screen.queryByText("X account")).not.toBeInTheDocument();
    expect(screen.getByText("0件のCharacter接続")).toBeInTheDocument();
  });

  it("ignores a credential action that completes after switching Characters", async () => {
    const user = userEvent.setup();
    let resolveVerify: (value: typeof credential) => void = () => undefined;
    const verifyPromise = new Promise<typeof credential>((resolve) => {
      resolveVerify = resolve;
    });
    vi.mocked(mediaOperationsSetupApi.listPlatformAccounts).mockImplementation(
      async (_projectId, personaId) => (personaId === "character-1" ? [account] as never : []),
    );
    vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockResolvedValue(credential as never);
    vi.mocked(mediaOperationsSetupApi.verifyPlatformCredential).mockReturnValue(verifyPromise as never);
    const onChanged = vi.fn();

    const { rerender } = render(<PlatformAccountPanel characterId="character-1" onChanged={onChanged} />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "検証" })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: "検証" }));

    rerender(<PlatformAccountPanel characterId="character-2" onChanged={onChanged} />);
    resolveVerify(credential);
    await waitFor(() => {
      expect(mediaOperationsSetupApi.verifyPlatformCredential).toHaveBeenCalledTimes(1);
    });
    expect(onChanged).not.toHaveBeenCalled();
  });
});
