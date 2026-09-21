import { ATLAS, MOTIONS, frameAt, frameCell, type PetMotion } from "./codex-pet";
export type PetRenderState = { motion: PetMotion; look: number; reduced: boolean };

/** Browser-native engine shared by the React canvas and focused browser tests. */
export function mountPetRenderer(canvas: HTMLCanvasElement, image: Blob, onError: () => void) {
  let config: PetRenderState = { motion: "idle", look: 0, reduced: false };
  let disposed = false;
  let bitmap: ImageBitmap | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let currentMotion: PetMotion | null = null;
  let started = 0;
  canvas.width = ATLAS.cellWidth; canvas.height = ATLAS.cellHeight;
  const draw = () => {
    clearTimeout(timer);
    if (!bitmap || disposed || document.hidden) return;
    const now = performance.now();
    if (config.motion !== currentMotion) { currentMotion = config.motion; started = now; }
    const frame = config.reduced ? 0 : config.motion === "look" ? config.look : frameAt(config.motion, now - started);
    const cell = frameCell(config.motion, frame);
    const context = canvas.getContext("2d");
    if (!context) { onError(); return; }
    context.clearRect(0, 0, ATLAS.cellWidth, ATLAS.cellHeight);
    context.imageSmoothingEnabled = false;
    context.drawImage(bitmap, cell.x, cell.y, ATLAS.cellWidth, ATLAS.cellHeight, 0, 0, ATLAS.cellWidth, ATLAS.cellHeight);
    canvas.dataset.petMotion = config.motion;
    canvas.dataset.petFrame = String(frame);
    if (!config.reduced && config.motion !== "look") {
      const durations = MOTIONS[config.motion].durations;
      const elapsed = (now - started) % durations.reduce((sum, n) => sum + n, 0);
      const frameEnd = durations.slice(0, frame + 1).reduce((sum, n) => sum + n, 0);
      timer = setTimeout(draw, Math.max(16, frameEnd - elapsed));
    }
  };
  document.addEventListener("visibilitychange", draw);
  void createImageBitmap(image).then((decoded) => {
    if (disposed) { decoded.close(); return; }
    if (decoded.width !== ATLAS.width || decoded.height !== ATLAS.height) { decoded.close(); onError(); return; }
    bitmap = decoded; draw();
  }).catch(() => { if (!disposed) onError(); });
  return {
    update(state: PetRenderState) { config = state; draw(); },
    dispose() {
      disposed = true; clearTimeout(timer); bitmap?.close();
      document.removeEventListener("visibilitychange", draw);
    },
  };
}
