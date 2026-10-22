// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PersonaBriefPanel } from "./persona-brief-panel";

const mocks = vi.hoisted(() => ({ listPrograms: vi.fn(), listContentItems: vi.fn(), createContentItem: vi.fn(), createProgram: vi.fn() }));
vi.mock("@/lib/media-operations-research-api", () => ({ mediaResearchApi: mocks }));
vi.mock("next/link", () => ({ default: ({ children, ...props }: React.ComponentProps<"a">) => <a {...props}>{children}</a> }));
const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "revision", displayName: "UN//REAL" };
const saved = { id: "saved-brief", owner_user_id: "owner", project_id: "project", persona_revision_id: "revision", input_kind: "user_authored", brief: "  fixture本文\n\n" };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.listPrograms.mockResolvedValue([{ id: "program", persona_id: "persona", project_id: "project", owner_user_id: "owner", state: "draft", current_revision: { name: "fixture制作計画" } }]);
  mocks.listContentItems.mockResolvedValue([]);
});

describe("brief completion belongs to the current persona workspace", () => {
  it("keeps researched briefs distinct and prevents double-click or automatic retry after an unknown save", async () => {
    const user = userEvent.setup(); const onSelect = vi.fn();
    mocks.listContentItems.mockResolvedValue([{ ...saved, id: "research-brief", input_kind: "research", title: "採用済み研究企画", brief: "研究の根拠がある企画", status: "draft", content_type: "article" }]);
    mocks.createContentItem.mockRejectedValue(new Error("unknown"));
    render(<SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, dedupingInterval: 0 }}><PersonaBriefPanel scope={scope} selectedContentItemId="research-brief" onSelect={onSelect} onContinue={() => undefined} /></SWRConfig>);
    await screen.findByText("研究の根拠がある企画"); expect(screen.getByRole("button", { name: /採用済み研究企画/ })).toHaveTextContent("研究から採用");
    await user.click(await screen.findByLabelText("制作計画")); await user.click(await screen.findByRole("option", { name: "fixture制作計画" }));
    await user.type(screen.getByLabelText("企画名"), "本人の創作"); await user.type(screen.getByRole("textbox", { name: "制作brief" }), "創作本文");
    const button = screen.getByRole("button", { name: "制作briefを保存" }); fireEvent.click(button); fireEvent.click(button);
    await screen.findByRole("alert"); expect(mocks.createContentItem).toHaveBeenCalledTimes(1); expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "brief一覧を更新" })); await waitFor(() => expect(mocks.listContentItems.mock.calls.length).toBeGreaterThan(1));
    expect(mocks.createContentItem).toHaveBeenCalledTimes(1); expect(onSelect).not.toHaveBeenCalled();
  });

  for (const transition of ["current", "unmount", "scope"] as const) {
    it(`handles a delayed save after ${transition}`, async () => {
      const user = userEvent.setup(); const onSelect = vi.fn();
      let complete!: () => void;
      mocks.createContentItem.mockImplementation(() => new Promise(resolve => { complete = () => resolve(saved); }));
      const cache = new Map();
      const content = (selectedScope = scope) => <SWRConfig value={{ provider: () => cache, dedupingInterval: 0 }}><PersonaBriefPanel scope={selectedScope} selectedContentItemId="" onSelect={onSelect} onContinue={() => undefined} /></SWRConfig>;
      const view = render(content());
      await user.click(await screen.findByLabelText("制作計画"));
      await user.click(await screen.findByRole("option", { name: "fixture制作計画" }));
      await user.type(screen.getByLabelText("企画名"), "fixture企画");
      await user.type(screen.getByRole("textbox", { name: "制作brief" }), saved.brief);
      await user.click(screen.getByRole("button", { name: "制作briefを保存" }));
      await waitFor(() => expect(mocks.createContentItem).toHaveBeenCalledTimes(1));
      expect(mocks.createContentItem.mock.calls[0][0]).toEqual({ editorial_program_id: "program", title: "fixture企画", brief: saved.brief, objective: null, persona_revision_id: "revision", content_type: "x_post", input_kind: "user_authored" });
      expect(screen.getByRole("link", { name: "研究から企画を採用" })).toHaveAttribute("href", "/operations?tab=research");
      if (transition === "unmount") view.unmount();
      if (transition === "scope") view.rerender(content({ ...scope, ownerUserId: "other", projectId: "other-project", personaId: "other-persona" }));
      await act(async () => complete());
      if (transition === "current") await waitFor(() => expect(onSelect).toHaveBeenCalledWith(saved.id));
      else expect(onSelect).not.toHaveBeenCalled();
    });
  }
});
