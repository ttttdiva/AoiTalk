// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MediaContentVariantPanel } from "./content-variant-panel";
import { mediaContentApi, type ContentVariant, type ContentVariantRevision } from "@/lib/media-operations-content-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import type { PlatformAccount } from "@/lib/media-operations-setup-api";

vi.mock("@/lib/media-operations-content-api", () => ({
  mediaContentApi: {
    listVariants: vi.fn(), getVariant: vi.fn(), listRevisions: vi.fn(), getReadiness: vi.fn(),
    createVariant: vi.fn(), appendRevision: vi.fn(), recordQa: vi.fn(), recordRights: vi.fn(),
  },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function fixture(id: string, contentItemId: string, text = id, scope?: PersonaWorkspaceScope) {
  const revision: ContentVariantRevision = {
    id: `revision-${id}`, content_variant_id: id, version: 1, content_item_id: contentItemId,
    content_item_hash: "a".repeat(64), persona_revision_id: scope?.personaRevisionId ?? "persona-revision",
    persona_revision_hash: "b".repeat(64), platform_account_id: "account", platform: "x",
    payload: { type: "x_post", text }, generation_output_refs: [], source_evidence: [], content_hash: "c".repeat(64),
    ...(scope ? { owner_user_id: scope.ownerUserId, project_id: scope.projectId } : {}),
  };
  return {
    id, content_item_id: contentItemId, platform: "x", status: "draft", current_revision: revision,
    ...(scope ? { owner_user_id: scope.ownerUserId, project_id: scope.projectId } : {}),
  } satisfies ContentVariant;
}

function respondWith(variants: ReturnType<typeof fixture>[]) {
  vi.mocked(mediaContentApi.listVariants).mockResolvedValue(variants);
  vi.mocked(mediaContentApi.getVariant).mockImplementation(async id => variants.find(item => item.id === id)!);
  vi.mocked(mediaContentApi.listRevisions).mockImplementation(async id => [variants.find(item => item.id === id)!.current_revision]);
}

const preview = () => screen.queryByRole("region", { name: "保存済みコンテンツのプレビュー" });

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(mediaContentApi.getReadiness).mockResolvedValue({ ready: false, status: "blocked", blocking_reasons: ["qa_missing"] } as never);
});

describe("ContentItem deeplink scope", () => {
  it("queries and selects only the URL ContentItem, independent of the editable create field", async () => {
    respondWith([fixture("wrong-first", "other", "OTHER CONTENT"), fixture("target", "wanted", "TARGET CONTENT")]);
    render(<MediaContentVariantPanel initialContentItemId="wanted" />);
    await screen.findByText("TARGET CONTENT");
    expect(mediaContentApi.listVariants).toHaveBeenLastCalledWith({ contentItemId: "wanted" });
    expect(mediaContentApi.getVariant).toHaveBeenCalledTimes(1);
    expect(mediaContentApi.getVariant).toHaveBeenCalledWith("target");
    expect(screen.getAllByRole("button", { name: /X 下書き ContentItem/u })).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("ContentItem ID"), { target: { value: "edited-create-content" } });
    fireEvent.click(screen.getByRole("button", { name: "更新" }));
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalledTimes(2));
    expect(mediaContentApi.listVariants).toHaveBeenLastCalledWith({ contentItemId: "wanted" });
    expect(preview()).toHaveTextContent("TARGET CONTENT");
    expect(screen.queryByText("OTHER CONTENT")).not.toBeInTheDocument();
  });

  it.each([{ variants: [] }, { variants: [fixture("wrong", "other")] }])("does not escape to another content when no exact variant is returned ($variants)", async ({ variants }) => {
    respondWith(variants);
    render(<MediaContentVariantPanel initialContentItemId="missing" />);
    await screen.findByText(/まだVariantがありません/u);
    expect(mediaContentApi.getVariant).not.toHaveBeenCalled();
    expect(preview()).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("clears old rows and preview immediately while the new content is loading or fails", async () => {
    respondWith([fixture("a", "content-a", "SAVED A")]);
    const pending = deferred<ContentVariant[]>();
    vi.mocked(mediaContentApi.listVariants).mockResolvedValueOnce([fixture("a", "content-a", "SAVED A")]).mockReturnValue(pending.promise);
    const view = render(<MediaContentVariantPanel initialContentItemId="content-a" initialOutputRef="output-a | hash-a" />);
    await screen.findByText("SAVED A");
    fireEvent.change(screen.getByLabelText("本文"), { target: { value: "UNSAVED A" } });
    view.rerender(<MediaContentVariantPanel initialContentItemId="content-b" initialOutputRef="output-b | hash-b" />);
    expect(preview()).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /X 下書き ContentItem/u })).not.toBeInTheDocument();
    expect(screen.getByLabelText("ContentItem ID")).toHaveValue("content-b");
    expect(screen.getByLabelText("本文")).toHaveValue("");
    expect(screen.getByLabelText("添付Asset ID（1行1件）")).toHaveValue("output-b");
    expect(screen.getByLabelText("Generation output参照（id | sha256、1行1件）")).toHaveValue("output-b | hash-b");
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenLastCalledWith({ contentItemId: "content-b" }));
    await act(async () => pending.reject(new Error("CURRENT CONTENT UNAVAILABLE")));
    expect(screen.getByRole("alert")).toHaveTextContent("CURRENT CONTENT UNAVAILABLE");
    expect(preview()).not.toBeInTheDocument();
    expect(mediaContentApi.getVariant).toHaveBeenCalledTimes(1);
  });

  it("updates the output defaults when the output URL changes within the same content", async () => {
    respondWith([]);
    const view = render(<MediaContentVariantPanel initialContentItemId="content" initialOutputRef="output-a | hash-a" />);
    await screen.findByText(/まだVariantがありません/u);
    view.rerender(<MediaContentVariantPanel initialContentItemId="content" initialOutputRef="output-b | hash-b" />);
    expect(screen.getByLabelText("添付Asset ID（1行1件）")).toHaveValue("output-b");
    expect(screen.getByLabelText("Generation output参照（id | sha256、1行1件）")).toHaveValue("output-b | hash-b");
  });

  it.each(["resolve", "reject"] as const)("ignores a stale list %s after content A→B→A", async result => {
    const old = deferred<ContentVariant[]>();
    const a = fixture("current-a", "a", "CURRENT A");
    respondWith([a]);
    vi.mocked(mediaContentApi.listVariants).mockReturnValueOnce(old.promise).mockResolvedValueOnce([]).mockResolvedValueOnce([a]);
    const view = render(<MediaContentVariantPanel initialContentItemId="a" />);
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalledTimes(1));
    view.rerender(<MediaContentVariantPanel initialContentItemId="b" />);
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalledTimes(2));
    view.rerender(<MediaContentVariantPanel initialContentItemId="a" />);
    await screen.findByText("CURRENT A");
    await act(async () => {
      if (result === "resolve") old.resolve([fixture("old-a", "a", "OLD A")]);
      else old.reject(new Error("OLD LIST ERROR"));
    });
    expect(preview()).toHaveTextContent("CURRENT A");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(mediaContentApi.getVariant).toHaveBeenCalledTimes(1);
    expect(mediaContentApi.getVariant).toHaveBeenCalledWith("current-a");
  });

  it.each(["resolve", "reject"] as const)("ignores a stale detail %s after content A→B→A with the same variant ID", async result => {
    const old = deferred<ContentVariant>();
    const current = fixture("variant-a", "a", "CURRENT A");
    respondWith([current]);
    vi.mocked(mediaContentApi.listVariants).mockResolvedValueOnce([current]).mockResolvedValueOnce([]).mockResolvedValueOnce([current]);
    vi.mocked(mediaContentApi.getVariant).mockReturnValueOnce(old.promise).mockResolvedValue(current);
    const view = render(<MediaContentVariantPanel initialContentItemId="a" />);
    await waitFor(() => expect(mediaContentApi.getVariant).toHaveBeenCalledTimes(1));
    view.rerender(<MediaContentVariantPanel initialContentItemId="b" />);
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenCalledTimes(2));
    view.rerender(<MediaContentVariantPanel initialContentItemId="a" />);
    await screen.findByText("CURRENT A");
    await act(async () => {
      if (result === "resolve") old.resolve(fixture("variant-a", "a", "OLD A"));
      else old.reject(new Error("OLD DETAIL ERROR"));
    });
    expect(preview()).toHaveTextContent("CURRENT A");
    expect(screen.queryByText("OLD A")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["detail", "current revision", "revision history", "variant ID"] as const)("rejects a mismatched server %s without exposing its preview", async mismatch => {
    const target = fixture("target", "wanted", "TARGET");
    const wrong = fixture("target", "other", "WRONG CONTENT");
    respondWith([target]);
    if (mismatch === "detail") vi.mocked(mediaContentApi.getVariant).mockResolvedValue(wrong);
    if (mismatch === "current revision") vi.mocked(mediaContentApi.getVariant).mockResolvedValue({ ...target, current_revision: wrong.current_revision });
    if (mismatch === "revision history") vi.mocked(mediaContentApi.listRevisions).mockResolvedValue([wrong.current_revision]);
    if (mismatch === "variant ID") vi.mocked(mediaContentApi.getVariant).mockResolvedValue({ ...target, id: "different" });
    render(<MediaContentVariantPanel initialContentItemId="wanted" />);
    await screen.findByRole("alert");
    expect(preview()).not.toBeInTheDocument();
    expect(screen.queryByText("WRONG CONTENT")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "QAを記録" })).not.toBeInTheDocument();
  });

  it("clears a selected preview when refreshing the current list fails", async () => {
    respondWith([fixture("a", "a", "SAVED A")]);
    const view = render(<MediaContentVariantPanel initialContentItemId="a" />);
    await screen.findByText("SAVED A");
    vi.mocked(mediaContentApi.listVariants).mockRejectedValue(new Error("REFRESH FAILED"));
    fireEvent.click(screen.getByRole("button", { name: "更新" }));
    await screen.findByText("REFRESH FAILED");
    expect(preview()).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /X 下書き ContentItem/u })).not.toBeInTheDocument();
    view.unmount();
  });

  it.each(["other", ""])("blocks creation before POST when the edited ContentItem differs from the URL (%j)", async contentItemId => {
    respondWith([]);
    render(<MediaContentVariantPanel initialContentItemId="wanted" />);
    await screen.findByText(/まだVariantがありません/u);
    fireEvent.change(screen.getByLabelText("ContentItem ID"), { target: { value: contentItemId } });
    fireEvent.change(screen.getByLabelText("Persona Revision ID"), { target: { value: "persona-revision" } });
    fireEvent.change(screen.getByLabelText("PlatformAccount ID"), { target: { value: "account" } });
    fireEvent.change(screen.getByLabelText("本文"), { target: { value: "Requested content" } });
    fireEvent.submit(screen.getByRole("form", { name: "ContentVariant create form" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("選択中のContentItemと作成先が一致しません");
    expect(mediaContentApi.createVariant).not.toHaveBeenCalled();
    expect(mediaContentApi.getVariant).not.toHaveBeenCalled();
    expect(preview()).not.toBeInTheDocument();
  });

  it("never inserts or selects a creation response for another ContentItem", async () => {
    respondWith([]);
    vi.mocked(mediaContentApi.createVariant).mockResolvedValue(fixture("wrong-created", "other", "WRONG CREATED CONTENT"));
    render(<MediaContentVariantPanel initialContentItemId="wanted" />);
    await screen.findByText(/まだVariantがありません/u);
    fireEvent.change(screen.getByLabelText("Persona Revision ID"), { target: { value: "persona-revision" } });
    fireEvent.change(screen.getByLabelText("PlatformAccount ID"), { target: { value: "account" } });
    fireEvent.change(screen.getByLabelText("本文"), { target: { value: "Requested content" } });
    fireEvent.submit(screen.getByRole("form", { name: "ContentVariant create form" }));
    await screen.findByRole("alert");
    expect(mediaContentApi.createVariant).toHaveBeenCalledTimes(1);
    expect(vi.mocked(mediaContentApi.createVariant).mock.calls[0][0]).toEqual(expect.objectContaining({ content_item_id: "wanted" }));
    expect(mediaContentApi.getVariant).not.toHaveBeenCalled();
    expect(preview()).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /X 下書き ContentItem/u })).not.toBeInTheDocument();
  });

  it.each(["project", "persona"] as const)("resets immediately and fences a pending detail when the %s scope changes", async changed => {
    const scope: PersonaWorkspaceScope = { ownerUserId: "owner", projectId: "project-a", personaId: "persona-a", personaRevisionId: "revision-a", displayName: "Persona A" };
    const nextScope = { ...scope, ...(changed === "project" ? { projectId: "project-b" } : { personaId: "persona-b", personaRevisionId: "revision-b" }) };
    const account = (current: PersonaWorkspaceScope) => ({ id: "account", platform: "x", account_ref: "test", persona_id: current.personaId, project_id: current.projectId, owner_user_id: current.ownerUserId, current_revision: { id: "account-revision", platform_account_id: "account", project_id: current.projectId, owner_user_id: current.ownerUserId } } as PlatformAccount);
    const a = fixture("shared-variant", "content", "SCOPE A", scope);
    const b = fixture("shared-variant", "content", "SCOPE B", nextScope);
    const old = deferred<ContentVariant>();
    const next = deferred<ContentVariant[]>();
    respondWith([a]);
    vi.mocked(mediaContentApi.listVariants).mockResolvedValueOnce([a]).mockReturnValueOnce(next.promise);
    vi.mocked(mediaContentApi.getVariant).mockReturnValueOnce(old.promise).mockResolvedValue(b);
    vi.mocked(mediaContentApi.listRevisions).mockResolvedValueOnce([a.current_revision]).mockResolvedValue([b.current_revision]);
    const view = render(<MediaContentVariantPanel scope={scope} accounts={[account(scope)]} initialContentItemId="content" />);
    await waitFor(() => expect(mediaContentApi.getVariant).toHaveBeenCalledTimes(1));
    view.rerender(<MediaContentVariantPanel scope={nextScope} accounts={[account(nextScope)]} initialContentItemId="content" />);
    expect(preview()).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /X 下書き ContentItem/u })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Persona Revision ID")).toHaveValue(nextScope.personaRevisionId);
    await act(async () => old.resolve(a));
    expect(preview()).not.toBeInTheDocument();
    await waitFor(() => expect(mediaContentApi.listVariants).toHaveBeenLastCalledWith({ projectId: nextScope.projectId, contentItemId: "content" }));
    await act(async () => next.resolve([a, b]));
    await screen.findByText("SCOPE B");
    expect(preview()).not.toHaveTextContent("SCOPE A");
    expect(screen.getAllByRole("button", { name: /X 下書き ContentItem/u })).toHaveLength(1);
  });
});
