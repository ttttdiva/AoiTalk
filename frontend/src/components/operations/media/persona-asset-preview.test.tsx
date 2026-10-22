// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, expect, it, vi } from "vitest";
import type { ContentVariantRevision } from "@/lib/media-operations-content-api";
import { PersonaAssetPreview } from "./persona-asset-preview";
const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/media-assets-api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/media-assets-api")>(), mediaAssetsApi: { get: mocks.get } }));
const scope = { personaId: "persona", personaRevisionId: "pr", ownerUserId: "owner", projectId: "project", displayName: "UN//REAL" };
const sha = "f".repeat(64);
const asset = { asset_revision_id: "ar", generation_output_id: "output", persona_id: "persona", owner_user_id: "owner", project_id: "project", mime_type: "image/png", size_bytes: 100, sha256: sha };
const revision: ContentVariantRevision = { id: "vr", version: 1, content_variant_id: "variant", content_item_id: "brief", content_item_hash: "a".repeat(64), persona_revision_id: "pr", persona_revision_hash: "b".repeat(64), platform_account_id: "account", platform: "x", content_hash: "c".repeat(64), source_evidence: [], generation_output_refs: [{ generation_output_id: "output", sha256: sha }], payload: { type: "x_post", text: "保存本文", media: ["output"] } };
beforeEach(() => { vi.clearAllMocks(); mocks.get.mockResolvedValue(asset); });
function mount(onVerified = vi.fn()) { render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><PersonaAssetPreview scope={scope} revision={revision} onVerified={onVerified} /></SWRConfig>); return onVerified; }
it("requires authenticated decoded bytes and remounts the same image when retrying", async () => {
  const ready = mount(); const first = await screen.findByRole("img"); expect(ready).not.toHaveBeenLastCalledWith(true);
  expect(first).toHaveAttribute("src", expect.stringContaining(`/assets/output/preview?expected_sha256=${sha}`));
  fireEvent.load(first); await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  fireEvent.click(screen.getByRole("button", { name: "素材previewを再確認" }));
  const reloaded = await screen.findByRole("img"); expect(reloaded).not.toBe(first);
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(false)); fireEvent.load(reloaded);
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
});
it("does not preview another owner or a changed hash", async () => {
  mocks.get.mockResolvedValue({ ...asset, owner_user_id: "other", sha256: "e".repeat(64) }); const ready = mount();
  await screen.findByRole("alert"); expect(screen.queryByRole("img")).not.toBeInTheDocument(); expect(ready).not.toHaveBeenCalledWith(true);
});
it("keeps a failed byte response blocked", async () => {
  const ready = mount(); fireEvent.error(await screen.findByRole("img")); await screen.findByRole("alert"); expect(ready).not.toHaveBeenCalledWith(true);
});
it("revokes verification while refreshing and rejects delayed loads from the previous preview", async () => {
  const ready = mount(); const first = await screen.findByRole("img"); fireEvent.load(first);
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  let complete!: (value: typeof asset) => void;
  mocks.get.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "素材previewを再確認" }));
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
  expect(ready).toHaveBeenLastCalledWith(false); expect(screen.queryByRole("img")).not.toBeInTheDocument(); fireEvent.load(first);
  expect(ready).toHaveBeenLastCalledWith(false);
  await act(async () => complete(asset));
  const current = await screen.findByRole("img"); expect(current).not.toBe(first); expect(ready).toHaveBeenLastCalledWith(false);
  fireEvent.load(current); await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
});
