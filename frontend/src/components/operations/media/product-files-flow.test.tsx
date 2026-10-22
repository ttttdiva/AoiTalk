// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProductFilePicker } from "./product-file-picker";
import { ProductDeliverablePanel } from "./product-deliverable-panel";
const api = vi.hoisted(() => ({ list: vi.fn(), import: vi.fn(), manifest: vi.fn() }));
vi.mock("@/lib/base-path", () => ({ withBasePath: (path: string) => `/fixture${path}` }));
vi.mock("@/lib/media-product-files-api", async original => ({ ...await original<typeof import("@/lib/media-product-files-api")>(), productFilesApi: api }));
const scope = { ownerUserId: "owner", projectId: "project", personaId: "persona", personaRevisionId: "persona-rev", displayName: "Fixture" };
const file = { source_id: "source", owner_user_id: "owner", project_id: "project", filename: "landscape.bin", mime_type: "application/octet-stream", size_bytes: 28 * 1024 * 1024, etag: "version" };
const artifact = { artifact_version_id: "artifact", owner_user_id: "owner", project_id: "project", filename: "landscape.bin", mime_type: "application/octet-stream", size_bytes: file.size_bytes, sha256: "a".repeat(64) };
const manifest = { schema_version: "product-deliverable/v1", action: { id: "action", action_version: 3, payload_hash: "b".repeat(64) }, source: {}, deliverable: artifact, transport: "verified_chunk_stream", max_bytes: 1024 ** 3 };
beforeEach(() => { vi.resetAllMocks(); api.list.mockResolvedValue({ max_bytes: 1024 ** 3, files: [file] }); api.import.mockResolvedValue(artifact); api.manifest.mockResolvedValue({ manifest, manifest_json: JSON.stringify(manifest), manifest_sha256: "c".repeat(64) }); });
afterEach(() => vi.unstubAllEnvs());

it("pins a selected Files import and recovers from stale selection without changing the draft", async () => {
  const select = vi.fn(); render(<ProductFilePicker scope={scope} onSelect={select} />);
  fireEvent.change(screen.getByLabelText("Filesの配布物名で絞込"), { target: { value: "landscape" } });
  fireEvent.click(screen.getByRole("button", { name: "Filesの配布物を更新" }));
  const button = await screen.findByRole("button", { name: "固定版として取り込む" });
  expect(api.list).toHaveBeenCalledWith("persona", expect.any(AbortSignal), "landscape");
  api.import.mockRejectedValueOnce(new Error("Files selection changed")); fireEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent("changed"); expect(select).not.toHaveBeenCalled();
  fireEvent.click(button); await waitFor(() => expect(select).toHaveBeenCalledWith("artifact:artifact"));
  expect(api.import).toHaveBeenCalledWith("persona", file, expect.any(AbortSignal));
});
it("aborts import and rejects a late response after cancellation", async () => {
  let finish: (value: typeof artifact) => void = () => {};
  api.import.mockImplementation((_persona, _file, signal: AbortSignal) => new Promise(resolve => { finish = resolve; expect(signal.aborted).toBe(false); }));
  const select = vi.fn(); render(<ProductFilePicker scope={scope} onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Filesの配布物を更新" }));
  fireEvent.click(await screen.findByRole("button", { name: "固定版として取り込む" }));
  fireEvent.click(await screen.findByRole("button", { name: "中止" }));
  finish(artifact); await waitFor(() => expect(api.import.mock.calls[0][2].aborted).toBe(true));
  expect(select).not.toHaveBeenCalled();
});
it("offers an exact browser stream URL and clears stale manifest data on mismatch", async () => {
  vi.stubEnv("NEXT_PUBLIC_BASE_PATH", "/fixture");
  render(<ProductDeliverablePanel scope={scope} actionId="action" version={3} payloadHash={"b".repeat(64)} />);
  fireEvent.click(screen.getByRole("button", { name: "配布物の固定版を確認" }));
  const link = await screen.findByRole("link", { name: "配布物を直接ダウンロード" });
  expect(link.getAttribute("href")).toContain("/fixture/api/python-proxy/operations/media/product-actions/action/deliverable/content?");
  const url = new URL(link.getAttribute("href")!, "https://example.test");
  expect(url.searchParams.get("expected_action_version")).toBe("3"); expect(url.searchParams.get("expected_sha256")).toBe(artifact.sha256);
  api.manifest.mockResolvedValue({ manifest: { ...manifest, deliverable: { ...artifact, project_id: "foreign" } }, manifest_json: "{}", manifest_sha256: "c".repeat(64) });
  fireEvent.click(screen.getByRole("button", { name: "配布物の固定版を確認" }));
  await screen.findByRole("alert"); expect(screen.queryByRole("link", { name: "配布物を直接ダウンロード" })).toBeNull();
});
