// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CharacterVoicePreview } from "./voice-preview";

const mocks = vi.hoisted(() => ({ decode: vi.fn(), observe: vi.fn(), stop: vi.fn() }));
vi.mock("@/lib/aoirig/lip-sync", () => ({ decodeLipAudio: mocks.decode, observeMediaPlayback: mocks.observe }));
beforeEach(() => {
  mocks.decode.mockResolvedValue({ duration: 1 });
  mocks.observe.mockReturnValue(mocks.stop);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ audio_base64: "AA==", mime_type: "audio/wav" }) }));
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it("stops existing playback and releases its observer when leaving the preview", async () => {
  const view = render(<CharacterVoicePreview characterName="kotonoha_aoi" />);
  fireEvent.click(screen.getByRole("button", { name: "声と口パクを確認" }));
  await waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalledOnce());
  expect(mocks.observe).toHaveBeenCalledWith("kotonoha_aoi", expect.objectContaining({ session: "character-preview" }));
  view.unmount();
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledOnce();
  expect(mocks.stop).toHaveBeenCalledOnce();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:preview");
});

it("never starts late audio if unmounted while decoding", async () => {
  let finish!: (value: object) => void;
  mocks.decode.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const view = render(<CharacterVoicePreview characterName="kotonoha_aoi" />);
  fireEvent.click(screen.getByRole("button", { name: "声と口パクを確認" }));
  await waitFor(() => expect(mocks.decode).toHaveBeenCalledOnce());
  view.unmount(); finish({ duration: 1 });
  await Promise.resolve();
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});
