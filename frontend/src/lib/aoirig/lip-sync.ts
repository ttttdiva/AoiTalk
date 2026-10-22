/** Observes existing players; never creates a second playback graph. */
export type Viseme = { start: number; end: number; open: number; shape: number };
type Playback = {
  session: string; generation: string; segment: string; context: AudioContext;
  buffer: AudioBuffer; start: number; visemes: Viseme[];
};
type ObservedPlayback = Omit<Playback, "context" | "start"> & { time: () => number; running: () => boolean };
const playing = new Map<string, ObservedPlayback>();

export function parseVisemes(value: unknown): Viseme[] {
  if (!Array.isArray(value) || value.length > 2048) return [];
  const result: Viseme[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") return [];
    const { start, end, open, shape } = item;
    if (![start, end, open, shape].every((v) => typeof v === "number" && Number.isFinite(v)) || start < 0 || end <= start || end > 600 || open < 0 || open > 1 || Math.abs(shape) > 1) return [];
    if (result.length && start < result[result.length - 1].end) return [];
    result.push({ start, end, open, shape });
  }
  return result;
}
function observe(character: string, playback: ObservedPlayback): () => void {
  playing.set(character, playback);
  return () => { if (playing.get(character) === playback) playing.delete(character); };
}
export function observePlayback(character: string, playback: Playback): () => void {
  return observe(character, { ...playback, time: () => playback.context.currentTime - playback.start,
    running: () => playback.context.state === "running" });
}
export function observeMediaPlayback(character: string, playback: Omit<Playback, "context" | "start"> & { media: HTMLMediaElement }): () => void {
  return observe(character, { ...playback, time: () => playback.media.currentTime,
    running: () => !playback.media.paused && !playback.media.ended && playback.media.readyState >= 2 });
}
/** Decode for metering only; existing HTMLAudioElement still owns all playback. */
export async function decodeLipAudio(blob: Blob): Promise<AudioBuffer | null> {
  if (blob.size > 8 * 1024 * 1024 || typeof OfflineAudioContext === "undefined") return null;
  try {
    const decoder = new OfflineAudioContext(1, 1, 24000);
    return await decoder.decodeAudioData(await blob.arrayBuffer());
  } catch { return null; } // Metering failure must never interrupt speech.
}
export function sampleLip(character: string): { open: number; shape: number; speaking: boolean } {
  const playback = playing.get(character);
  const silent = { open: 0, shape: 0, speaking: false };
  if (!playback || !playback.running()) return silent;
  const seconds = playback.time();
  if (seconds < 0 || seconds >= playback.buffer.duration) return silent;
  if (playback.visemes.length) {
    const current = playback.visemes.find((v) => seconds >= v.start && seconds < v.end);
    return { open: current?.open ?? 0, shape: current?.shape ?? 0, speaking: true };
  }
  const samples = playback.buffer.getChannelData(0);
  const offset = Math.floor(seconds * playback.buffer.sampleRate);
  let sum = 0, count = 0;
  for (let i = offset; i < Math.min(samples.length, offset + 512); i++) { sum += samples[i] * samples[i]; count++; }
  const rms = Math.sqrt(sum / Math.max(count, 1));
  return { open: Math.min(1, Math.max(0, (rms - .012) * 7)), shape: 0, speaking: true };
}
