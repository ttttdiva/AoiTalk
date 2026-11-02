// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { YouTubeChannelSlot } from "@/components/operations/media/youtube-channel-slot-panel";
import { personaPublicationApi } from "@/lib/media-persona-publication-api";
import { mediaOperationsSetupApi, type PlatformAccount } from "@/lib/media-operations-setup-api";

vi.mock("@/lib/media-operations-setup-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/media-operations-setup-api")>(
    "@/lib/media-operations-setup-api",
  );
  return {
    ...actual,
    mediaOperationsSetupApi: {
      getPlatformCredential: vi.fn(),
      updateYouTubeChannelSlot: vi.fn(),
    },
  };
});

vi.mock("@/lib/media-persona-publication-api", () => ({
  personaPublicationApi: { oauth: vi.fn(), startOAuth: vi.fn() },
}));

const scope = {
  personaId: "persona-offby1",
  personaRevisionId: "persona-revision-1",
  ownerUserId: "owner-1",
  projectId: "project-offby1",
  displayName: "OFFBY1",
};

const placeholderSlot = {
  id: "youtube-account-1",
  owner_user_id: "owner-1",
  project_id: "project-offby1",
  persona_id: "persona-offby1",
  platform: "youtube",
  account_ref: "sns-v6:offby1:youtube",
  status: "paused",
  connection_id: null,
  current_revision: { version: 1 },
};

let currentAccount: typeof placeholderSlot & Record<string, unknown>;

function Harness() {
  const [account, setAccount] = useState(currentAccount);
  return <YouTubeChannelSlot scope={scope} account={account as unknown as PlatformAccount} onUpdated={async () => setAccount(currentAccount)} />;
}

function renderSlot() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <Harness />
    </SWRConfig>,
  );
}

function oauthMetadata(accountRef: string, accountRevision: number, configured: boolean) {
  return {
    platform_account_id: "youtube-account-1",
    project_id: "project-offby1",
    platform: "youtube",
    account_ref: accountRef,
    account_revision: accountRevision,
    credential_revision: 0,
    configured,
    blockers: configured ? [] : ["oauth_client_id_missing"],
    required_scopes: ["https://www.googleapis.com/auth/youtube.readonly"],
    redirect_uri: configured ? "https://example.test/operations/media/oauth/callback" : null,
    client_ref: configured ? "configured-project-client" : null,
    external_checks: [],
  } as never;
}

describe("YouTubeChannelSlot", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentAccount = { ...placeholderSlot };
    vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockRejectedValue({ status: 404 });
    vi.mocked(mediaOperationsSetupApi.updateYouTubeChannelSlot).mockImplementation(async (_id, input) => {
      currentAccount = {
        ...currentAccount,
        account_ref: input.channel_id,
        status: input.activate ? "active" : currentAccount.status,
        current_revision: { version: currentAccount.current_revision.version + 1 },
      };
      return currentAccount as never;
    });
  });

  it("registers the channel, then activates the slot, then offers 本人接続", async () => {
    const user = userEvent.setup();
    const channelId = "UC" + "a".repeat(22);
    vi.mocked(personaPublicationApi.oauth).mockResolvedValue(oauthMetadata(channelId, 3, false));
    renderSlot();

    expect(await screen.findByText("YouTube の本人接続 · channel ID 未登録")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "slotを有効化" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText(/YouTube channel ID/)).toBeEnabled());

    await user.type(screen.getByLabelText(/YouTube channel ID/), channelId);
    await user.click(screen.getByRole("button", { name: "channel IDを登録" }));
    await waitFor(() => expect(mediaOperationsSetupApi.updateYouTubeChannelSlot).toHaveBeenLastCalledWith(
      "youtube-account-1",
      { expected_version: 1, channel_id: channelId, activate: false },
      expect.any(String),
    ));

    await user.click(await screen.findByRole("button", { name: "slotを有効化" }));
    await waitFor(() => expect(mediaOperationsSetupApi.updateYouTubeChannelSlot).toHaveBeenLastCalledWith(
      "youtube-account-1",
      { expected_version: 2, channel_id: channelId, activate: true },
      expect.any(String),
    ));
    expect(await screen.findByText("YouTube の本人接続 · " + channelId)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "本人が公式画面で接続する" })).toBeDisabled();
    expect(personaPublicationApi.startOAuth).not.toHaveBeenCalled();
  });

  it("shows an enabled 本人接続 button directly for an active registered slot", async () => {
    const channelId = "UC" + "b".repeat(22);
    currentAccount = { ...placeholderSlot, account_ref: channelId, status: "active", current_revision: { id: "youtube-revision-2", version: 2 } } as never;
    vi.mocked(personaPublicationApi.oauth).mockResolvedValue(oauthMetadata(channelId, 2, true));
    renderSlot();

    expect(await screen.findByText("YouTube の本人接続 · " + channelId)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "本人が公式画面で接続する" })).toBeEnabled();
    expect(screen.getByText("確認する権限: https://www.googleapis.com/auth/youtube.readonly")).toBeInTheDocument();
    expect(personaPublicationApi.oauth).toHaveBeenCalledWith("youtube-account-1");
    expect(personaPublicationApi.startOAuth).not.toHaveBeenCalled();
  });

  it("keeps OAuth unavailable for a registered but paused slot", async () => {
    currentAccount = { ...placeholderSlot, account_ref: "UC" + "e".repeat(22), current_revision: { id: "youtube-revision-2", version: 2 } } as never;
    renderSlot();

    expect(await screen.findByRole("button", { name: "slotを有効化" })).toBeInTheDocument();
    expect(screen.getByText("本人接続の前に、この枠を有効化してください。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "本人が公式画面で接続する" })).not.toBeInTheDocument();
    expect(personaPublicationApi.oauth).not.toHaveBeenCalled();
  });

  it("disables registration for malformed channel ids", async () => {
    const user = userEvent.setup();
    renderSlot();
    await waitFor(() => expect(screen.getByLabelText(/YouTube channel ID/)).toBeEnabled());
    await user.type(screen.getByLabelText(/YouTube channel ID/), "UCshort");
    expect(screen.getByRole("button", { name: "channel IDを登録" })).toBeDisabled();
  });

  it("locks identity changes when a credential row exists", async () => {
    vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockResolvedValue({ id: "credential-1" } as never);
    renderSlot();
    expect(await screen.findByText("この枠には既存の認証情報があるため、この画面から変更できません。")).toBeInTheDocument();
    expect(screen.getByLabelText(/YouTube channel ID/)).toBeDisabled();
  });

  it("locks identity changes when the credential state cannot be read", async () => {
    vi.mocked(mediaOperationsSetupApi.getPlatformCredential).mockRejectedValue({ status: 503 });
    renderSlot();
    expect(await screen.findByText("認証状態を取得できません。状態を再取得してください。")).toBeInTheDocument();
    expect(screen.getByLabelText(/YouTube channel ID/)).toBeDisabled();
  });
});
