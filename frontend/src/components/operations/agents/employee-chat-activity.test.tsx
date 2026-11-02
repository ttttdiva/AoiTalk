// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { SWRConfig } from "swr";
import { EmployeeChatActivity } from "./employee-chat-activity";

const mocks = vi.hoisted(() => ({
  userId: "account-a" as string | null | undefined,
  forbidden: false,
  listSessions: vi.fn(),
  listRuns: vi.fn(),
  getRun: vi.fn(),
}));

vi.mock("@/components/providers/swr-global-provider", () => ({
  useCurrentUserId: () => mocks.userId,
}));
vi.mock("@/lib/chat-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/chat-api")>("@/lib/chat-api");
  return {
    ...actual,
    chatApi: { ...actual.chatApi, getAgentRun: (...args: unknown[]) => mocks.getRun(...args) },
  };
});
vi.mock("@/contexts/chat-session-context", () => ({
  useChatSessionsOptional: () => null,
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/lib/conversation-agent-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/conversation-agent-api")>("@/lib/conversation-agent-api");
  return {
    ...actual,
    conversationAgentApi: {
      ...actual.conversationAgentApi,
      listAgentConversations: (...args: Parameters<typeof actual.conversationAgentApi.listAgentConversations>) => {
        void args;
        return mocks.listSessions();
      },
      listAgentRuns: (...args: Parameters<typeof actual.conversationAgentApi.listAgentRuns>) => {
        void args;
        return mocks.listRuns();
      },
    },
  };
});

const oldSession = {
  id: "old-private-chat",
  title: "旧AccountのPrivate Chat",
  character_name: "aoi",
  last_activity: "2026-10-11T00:00:00Z",
};
const oldRun = {
  id: "old-private-run",
  title: "旧AccountのPrivate Run",
  status: "succeeded",
  created_at: "2026-10-11T00:00:00Z",
};

function activityTree(agentId = "agent-1") {
  return (
    <SWRConfig
      value={{
        provider: () => new Map(),
        dedupingInterval: 0,
        keepPreviousData: true,
        shouldRetryOnError: false,
      }}
    >
      <EmployeeChatActivity agentId={agentId} agentName="Agent 1" />
    </SWRConfig>
  );
}

function renderActivity() {
  return render(activityTree());
}

describe("EmployeeChatActivity auth scope", () => {
  beforeEach(() => {
    mocks.userId = "account-a";
    mocks.forbidden = false;
    mocks.listSessions.mockImplementation(async () => {
      if (mocks.forbidden) throw new Error("403");
      return { sessions: [oldSession] };
    });
    mocks.listRuns.mockImplementation(async () => {
      if (mocks.forbidden) throw new Error("403");
      return { agent_runs: [oldRun] };
    });
    mocks.getRun.mockResolvedValue({
      agent_run: { ...oldRun, objective: "旧ownerだけの詳細本文" },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders Chat hrefs with the configured prefix before any click", async () => {
    const base = process.env.NEXT_PUBLIC_BASE_PATH || "";
    const sessionId = "session/space ?#";
    const runSessionId = "run/space ?#";
    mocks.listSessions.mockResolvedValue({ sessions: [{ ...oldSession, id: sessionId }] });
    mocks.listRuns.mockResolvedValue({ agent_runs: [{ ...oldRun, session_id: runSessionId }] });
    expect(renderToString(<EmployeeChatActivity agentId="agent-1" />))
      .toContain(`href="${base}/chat?agent_id=agent-1"`);

    renderActivity();
    expect(screen.getByRole("link", { name: "Chatで開く" }))
      .toHaveAttribute("href", `${base}/chat?agent_id=agent-1`);
    expect(await screen.findByRole("link", { name: /旧AccountのPrivate Chat/ }))
      .toHaveAttribute("href", `${base}/chat?s=${encodeURIComponent(sessionId)}`);
    expect(await screen.findByRole("link", { name: "関連Chatを開く" }))
      .toHaveAttribute("href", `${base}/chat?s=${encodeURIComponent(runSessionId)}`);
  });

  it("renders all Operations links with the configured prefix in SSR and direct DOM", async () => {
    const base = process.env.NEXT_PUBLIC_BASE_PATH || "";
    const rootAgentId = "agent/root ?#";
    const runAgentId = "agent/run ?#";
    const workItemId = "work/item ?#";
    mocks.listSessions.mockResolvedValue({ sessions: [] });
    mocks.listRuns.mockResolvedValue({
      agent_runs: [{
        ...oldRun,
        agent_id: runAgentId,
        work_item_id: workItemId,
      }],
    });

    const expectedAgentHref = `${base}/operations?tab=agents&agent=${encodeURIComponent(rootAgentId)}&panel=activity`;
    const expectedRunAgentHref = `${base}/operations?tab=agents&agent=${encodeURIComponent(runAgentId)}&panel=activity`;
    const expectedWorkHref = `${base}/operations?tab=work&work_id=${encodeURIComponent(workItemId)}`;
    const ssrHref = (href: string) => href.replaceAll("&", "&amp;");
    const serverHtml = renderToString(
      <EmployeeChatActivity agentId={rootAgentId} agentName="Agent root" />,
    );
    expect(serverHtml).toContain(`href="${ssrHref(expectedAgentHref)}"`);

    render(
      <SWRConfig
        value={{
          provider: () => new Map(),
          dedupingInterval: 0,
          keepPreviousData: true,
          shouldRetryOnError: false,
        }}
      >
        <EmployeeChatActivity agentId={rootAgentId} agentName="Agent root" />
      </SWRConfig>,
    );

    expect(await screen.findByRole("link", { name: "Operationsで開く" })).toHaveAttribute(
      "href",
      expectedAgentHref,
    );
    expect(screen.getByRole("link", { name: "Agentを開く" })).toHaveAttribute(
      "href",
      expectedRunAgentHref,
    );
    expect(screen.getByRole("link", { name: "Workを開く" })).toHaveAttribute(
      "href",
      expectedWorkHref,
    );
  });

  it("does not claim a paused Agent has no Chat when verified activity links to its history", async () => {
    mocks.listSessions.mockResolvedValue({ sessions: [] });
    mocks.listRuns.mockResolvedValue({ agent_runs: [{ ...oldRun, session_id: "existing-readable-chat" }] });
    render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}>
        <EmployeeChatActivity agentId="agent-1" agentState="paused" />
      </SWRConfig>,
    );
    expect(await screen.findByText("履歴のあるChatは関連活動から開けます。")).toBeVisible();
    expect(screen.queryByText("このAgentのChatはまだありません。")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "関連Chatを開く" })).toHaveAttribute(
      "href", `${process.env.NEXT_PUBLIC_BASE_PATH || ""}/chat?s=existing-readable-chat`,
    );
    expect(screen.getByRole("button", { name: "Agentが有効ではありません" })).toBeDisabled();
  });

  it("does not claim there is no Chat when activity permission cannot be verified", async () => {
    mocks.listSessions.mockResolvedValue({ sessions: [] });
    mocks.listRuns.mockRejectedValue(new Error("403"));
    renderActivity();
    await screen.findByRole("alert");
    expect(screen.queryByText("このAgentのChatはまだありません。")).not.toBeInTheDocument();
    expect(screen.queryByText("履歴のあるChatは関連活動から開けます。")).not.toBeInTheDocument();
  });

  it("does not render the previous account's private Chat or run after same-Agent URL returns 403", async () => {
    const view = renderActivity();
    expect(await screen.findByText("旧AccountのPrivate Chat")).toBeVisible();
    expect(screen.getByText("旧AccountのPrivate Run")).toBeVisible();

    mocks.userId = "account-b";
    mocks.forbidden = true;
    view.rerender(
      <SWRConfig
        value={{
          provider: () => new Map(),
          dedupingInterval: 0,
          keepPreviousData: true,
          shouldRetryOnError: false,
        }}
      >
        <EmployeeChatActivity agentId="agent-1" agentName="Agent 1" />
      </SWRConfig>,
    );

    await waitFor(() => expect(screen.getByRole("alert")).toBeVisible());
    expect(screen.queryByText("旧AccountのPrivate Chat")).not.toBeInTheDocument();
    expect(screen.queryByText("旧AccountのPrivate Run")).not.toBeInTheDocument();
    expect(mocks.listSessions).toHaveBeenCalledTimes(2);
    expect(mocks.listRuns).toHaveBeenCalledTimes(2);
  });

  it.each(["account-b", null])("closes fetched private Run details when owner changes to %s", async (nextUserId) => {
    const view = renderActivity();
    fireEvent.click(await screen.findByRole("button", { name: /旧AccountのPrivate Run/ }));
    expect(await within(await screen.findByRole("dialog")).findByText("旧ownerだけの詳細本文")).toBeVisible();

    mocks.userId = nextUserId;
    mocks.forbidden = true;
    view.rerender(activityTree());

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("旧ownerだけの詳細本文")).not.toBeInTheDocument();
    if (nextUserId) await screen.findByRole("alert");
    expect(mocks.getRun).toHaveBeenCalledTimes(1);
  });

  it("closes the previous Agent's Run details even when the next Agent list succeeds", async () => {
    const view = renderActivity();
    fireEvent.click(await screen.findByRole("button", { name: /旧AccountのPrivate Run/ }));
    expect(await within(await screen.findByRole("dialog")).findByText("旧ownerだけの詳細本文")).toBeVisible();

    mocks.listRuns.mockResolvedValue({ agent_runs: [{ ...oldRun, id: "other-run", title: "別AgentのRun" }] });
    mocks.listSessions.mockResolvedValue({ sessions: [] });
    view.rerender(activityTree("agent-2"));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /別AgentのRun/ })).toBeVisible();
    expect(screen.queryByText("旧ownerだけの詳細本文")).not.toBeInTheDocument();
    expect(mocks.getRun).toHaveBeenCalledTimes(1);
  });

  it("removes fetched Run details when the same owner's activity permission is revoked", async () => {
    renderActivity();
    fireEvent.click(await screen.findByRole("button", { name: /旧AccountのPrivate Run/ }));
    expect(await within(await screen.findByRole("dialog")).findByText("旧ownerだけの詳細本文")).toBeVisible();

    mocks.forbidden = true;
    fireEvent(window, new Event("online"));

    await screen.findByText("Chat活動を取得できませんでした。権限またはAgentの状態を確認してください。");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("旧ownerだけの詳細本文")).not.toBeInTheDocument();

    mocks.forbidden = false;
    mocks.getRun.mockResolvedValue({ agent_run: { ...oldRun, objective: "再確認した現在の詳細本文" } });
    fireEvent(window, new Event("online"));
    expect(await within(await screen.findByRole("dialog")).findByText("再確認した現在の詳細本文")).toBeVisible();
    expect(screen.queryByText("旧ownerだけの詳細本文")).not.toBeInTheDocument();
    expect(mocks.getRun).toHaveBeenCalledTimes(2);
  });

  it("removes details when revalidation no longer includes the selected conversation's Run", async () => {
    renderActivity();
    fireEvent.click(await screen.findByRole("button", { name: /旧AccountのPrivate Run/ }));
    expect(await within(await screen.findByRole("dialog")).findByText("旧ownerだけの詳細本文")).toBeVisible();

    mocks.listRuns.mockResolvedValue({ agent_runs: [] });
    fireEvent(window, new Event("online"));

    await waitFor(() => expect(mocks.listRuns).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("旧ownerだけの詳細本文")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("ignores an old owner's detail response arriving after the scope changes", async () => {
    let resolveDetail!: (value: unknown) => void;
    mocks.getRun.mockReturnValue(new Promise((resolve) => { resolveDetail = resolve; }));
    const view = renderActivity();
    fireEvent.click(await screen.findByRole("button", { name: /旧AccountのPrivate Run/ }));
    expect(await screen.findByRole("dialog")).toBeVisible();

    mocks.userId = "account-b";
    mocks.forbidden = true;
    view.rerender(activityTree());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => {
      resolveDetail({ agent_run: { ...oldRun, objective: "遅延した旧owner専用本文" } });
    });

    expect(screen.queryByText("遅延した旧owner専用本文")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.getRun).toHaveBeenCalledTimes(1);
  });

  it("opens the exact Run and Chat for each of two conversations of the same Agent", async () => {
    mocks.listRuns.mockResolvedValue({ agent_runs: [
      { ...oldRun, id: "run-a", title: "会話Aの履歴", session_id: "chat-a", agent_id: "agent-1" },
      { ...oldRun, id: "run-b", title: "会話Bの履歴", session_id: "chat-b", agent_id: "agent-1" },
    ] });
    mocks.getRun.mockImplementation(async (runId: string) => ({
      agent_run: { ...oldRun, id: runId, objective: `${runId}だけの本文` },
    }));
    renderActivity();
    const firstRunButton = await screen.findByRole("button", { name: /会話Aの履歴/ });
    const secondRunButton = screen.getByRole("button", { name: /会話Bの履歴/ });
    const base = process.env.NEXT_PUBLIC_BASE_PATH || "";
    expect(screen.getAllByRole("link", { name: "関連Chatを開く" }).map((link) => link.getAttribute("href")))
      .toEqual([`${base}/chat?s=chat-a`, `${base}/chat?s=chat-b`]);

    fireEvent.click(firstRunButton);
    const firstDialog = await screen.findByRole("dialog");
    expect(await within(firstDialog).findByText("run-aだけの本文")).toBeVisible();
    fireEvent.click(within(firstDialog).getByRole("button", { name: /閉じる|Close/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(secondRunButton);
    const secondDialog = await screen.findByRole("dialog");
    expect(await within(secondDialog).findByText("run-bだけの本文")).toBeVisible();
    expect(within(secondDialog).queryByText("run-aだけの本文")).not.toBeInTheDocument();
    expect(mocks.getRun.mock.calls.map(([runId]) => runId)).toEqual(["run-a", "run-b"]);
  });

});
