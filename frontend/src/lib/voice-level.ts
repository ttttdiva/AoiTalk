/**
 * Convert the backend's int16 RMS value into a display level.
 *
 * `voice_status.rms` is calculated from 16-bit PCM samples (0..32768), not a
 * CSS-ready 0..1 ratio.  A logarithmic mapping makes quiet speech visible
 * while keeping loud samples from saturating the meter immediately.  The
 * returned value is always safe to use as a percentage.
 */
export function normalizeVoiceRms(
  value: number | null | undefined,
  {
    floorDb = -60,
    peak = 32768,
  }: { floorDb?: number; peak?: number } = {},
): number {
  if (!Number.isFinite(value) || !Number.isFinite(peak) || peak <= 0) return 0;
  const linear = Math.max(0, Math.min(1, Number(value) / peak));
  if (linear <= 0) return 0;
  const db = 20 * Math.log10(linear);
  const normalized = (db - floorDb) / -floorDb;
  return Math.max(0, Math.min(1, normalized));
}
