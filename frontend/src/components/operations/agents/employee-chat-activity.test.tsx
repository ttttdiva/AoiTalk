// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { SWRConfig } from "swr";
import { EmployeeChatActivity } from "./employee-chat-activity";

const mocks = vi.hoisted(() => ({
  userId: "account-a" as string | null | undefined,
  forbidden: false,
  listSessions: vi.fn(),
  listRuns: vi.fn(),
}));

vi.mock("@/components/providers/swr-global-provider", () => ({
  useCurrentUserId: () => mocks.userId,
}));
vi.mock("@/components/chat/agent-run-detail-sheet", () => ({
  AgentRunDetailSheet: () => null,
}));
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

function renderActivity() {
  return render(
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

});
