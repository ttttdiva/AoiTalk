import { CHAT_WAVE_GLYPHS } from "./glyph-atlas";

export const MAX_RIPPLES = 8;
export const RIPPLE_LIFETIME_SECONDS = 7.0;
export const RIPPLE_CONTOUR_SAMPLES = 256;
export const RIPPLE_CONTOUR_MIN_RADIUS_SCALE = 0.65;
export const RIPPLE_CONTOUR_MAX_RADIUS_SCALE = 1.35;
/** Keep the idle background gentle while click-wave age stays in real time. */
export const CHAT_WAVE_BACKGROUND_TIME_SCALE = 0.75;
/** Apply the slowdown after the legacy 85px/s floor so it remains visible. */
export const RIPPLE_SPEED_SCALE = 0.66;
const ORDINAL_ENTROPY_STEP = 0.6180339887498949;
const TWO_PI = Math.PI * 2;
// Keep the shared seed small enough for mediump fragment arithmetic. The hash
// below also folds cell coordinates before its non-linear step, so large
// canvases do not feed huge arguments into sin() on mobile GPUs.
export const DEFAULT_WAVE_SEED = 0.41731;

export interface Ripple {
  readonly x: number;
  readonly y: number;
  readonly startedAt: number;
  /** Stable per-ripple event seed used by the shader's shape parameters. */
  readonly seed: number;
  /** Encoded samples of a smooth, event-fixed closed contour. */
  readonly contour: Uint8Array;
}

function finite(value: number): boolean {
  return Number.isFinite(value);
}

function fract(value: number): number {
  return value - Math.floor(value);
}

function normalizeUnitInterval(value: number, fallback = 0): number {
  return finite(value) ? fract(value) : fract(fallback);
}

function mix(first: number, second: number, amount: number): number {
  return first + (second - first) * amount;
}

function rippleEventNoise(seed: number, salt: number): number {
  const folded = fract(normalizeUnitInterval(seed) * 43.17 + salt * 0.173);
  return fract(folded * (folded + 19.19) + salt * 0.731);
}

export interface RippleDynamics {
  /** Event salt multiplier before the renderer's 85..260px/s clamp. */
  readonly eventSpeedScale: number;
  /** Effective multiplier applied after that clamp. */
  readonly speedScale: number;
  readonly frontWidthScale: number;
  readonly reachScale: number;
  readonly lifetimeSeconds: number;
  readonly strengthScale: number;
  readonly wavelength: number;
  readonly followerCount: number;
  readonly followerDelays: readonly [number, number, number, number, number];
  readonly followerGaps: readonly [number, number, number, number, number];
  readonly followerWidthScales: readonly [
    number,
    number,
    number,
    number,
    number,
  ];
  readonly followerStrengths: readonly [number, number, number, number, number];
}

/** Reference model for the shader's event-fixed, independently salted parameters. */
export function rippleDynamicsForSeed(seed: number): RippleDynamics {
  const noise = (salt: number): number => rippleEventNoise(seed, salt);
  const wavelength = mix(62, 105, noise(10.4));
  const widthNoise = noise(4.5);
  const frontWidthScale =
    widthNoise >= 0.86
      ? mix(0.22, 0.32, noise(52.1))
      : mix(0.36, 0.58, widthNoise);
  const followerGaps: [number, number, number, number, number] = [
    wavelength * mix(0.62, 1.18, noise(18.1)),
    wavelength * mix(0.38, 1.08, noise(25.6)),
    wavelength * mix(0.34, 1.16, noise(33.2)),
    wavelength * mix(0.32, 1.24, noise(40.1)),
    wavelength * mix(0.3, 1.32, noise(47.3)),
  ];
  const followerDelays: [number, number, number, number, number] = [
    followerGaps[0],
    followerGaps[0] + followerGaps[1],
    followerGaps[0] + followerGaps[1] + followerGaps[2],
    followerGaps[0] + followerGaps[1] + followerGaps[2] + followerGaps[3],
    followerGaps[0] +
      followerGaps[1] +
      followerGaps[2] +
      followerGaps[3] +
      followerGaps[4],
  ];
  const followerWidthScales: [number, number, number, number, number] = [
    mix(1.04, 1.3, noise(15.3)),
    mix(1.1, 1.52, noise(27.7)),
    mix(1.16, 1.72, noise(35.8)),
    mix(1.2, 1.86, noise(42.6)),
    mix(1.24, 2.0, noise(49.1)),
  ];
  const eventSpeedScale = mix(0.86, 1.18, noise(3.7));
  return {
    eventSpeedScale,
    speedScale: eventSpeedScale * RIPPLE_SPEED_SCALE,
    frontWidthScale,
    reachScale: mix(0.78, 1.38, noise(7.9)),
    lifetimeSeconds: mix(4.6, RIPPLE_LIFETIME_SECONDS, noise(14.1)),
    strengthScale: mix(0.75, 1.22, noise(8.9)),
    wavelength,
    followerCount: Math.floor(noise(42.7) * 6),
    followerDelays,
    followerGaps,
    followerWidthScales,
    followerStrengths: [
      mix(0.3, 0.46, noise(12.3)),
      mix(0.2, 0.29, noise(22.9)),
      mix(0.13, 0.19, noise(31.4)),
      mix(0.08, 0.12, noise(39.2)),
      mix(0.045, 0.075, noise(46.8)),
    ],
  };
}

function randomForSeed(seed: number): () => number {
  let state =
    (Math.floor(normalizeUnitInterval(seed) * 0x100000000) ^ 0xa511e9b3) >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 0x100000000;
  };
}

function angleDistance(first: number, second: number): number {
  const wrapped =
    ((((first - second + Math.PI) % TWO_PI) + TWO_PI) % TWO_PI) - Math.PI;
  return Math.abs(wrapped);
}

function smoothBump(angle: number, center: number, width: number): number {
  const distance = angleDistance(angle, center);
  return Math.exp(-0.5 * (distance / width) ** 2);
}

function encodedRadiusScale(radiusScale: number): number {
  const normalized =
    (radiusScale - RIPPLE_CONTOUR_MIN_RADIUS_SCALE) /
    (RIPPLE_CONTOUR_MAX_RADIUS_SCALE - RIPPLE_CONTOUR_MIN_RADIUS_SCALE);
  return Math.max(0, Math.min(255, Math.round(normalized * 255)));
}

/**
 * Build a periodic smooth radial contour from uneven control angles. The
 * profile combines a soft aspect stretch, one broad swell, one offset neck,
 * and a lightly filtered control-point variation; radial bounds keep every
 * sample positive and the closed curve free of self-intersection.
 */
export function generateRippleContour(seed: number): Uint8Array {
  const random = randomForSeed(seed);
  const controlCount = 7 + Math.floor(random() * 3);
  const sector = TWO_PI / controlCount;
  const angleOffset = random() * TWO_PI;
  const points = Array.from({ length: controlCount }, (_, index) => ({
    angle:
      (((angleOffset + index * sector + (random() - 0.5) * sector * 0.42) %
        TWO_PI) +
        TWO_PI) %
      TWO_PI,
    radius: 1,
  })).sort((first, second) => first.angle - second.angle);

  const raw = Array.from({ length: controlCount }, () => random() * 2 - 1);
  const aspectAngle = random() * TWO_PI;
  const aspectStrength = 0.055 + random() * 0.085;
  const swellCenter = random() * TWO_PI;
  const swellWidth = 0.58 + random() * 0.52;
  const swellStrength = 0.12 + random() * 0.12;
  const neckDirection = random() < 0.5 ? -1 : 1;
  const neckCenter = swellCenter + neckDirection * (0.95 + random() * 1.2);
  const neckWidth = 0.34 + random() * 0.3;
  const neckStrength = 0.08 + random() * 0.095;
  const secondaryCenter = random() * TWO_PI;
  const secondaryWidth = 0.44 + random() * 0.4;
  const secondaryStrength = (random() - 0.35) * 0.18;

  points.forEach((point, index) => {
    const filteredNoise =
      raw[(index + controlCount - 2) % controlCount] * 0.08 +
      raw[(index + controlCount - 1) % controlCount] * 0.22 +
      raw[index] * 0.4 +
      raw[(index + 1) % controlCount] * 0.22 +
      raw[(index + 2) % controlCount] * 0.08;
    point.radius =
      1 +
      Math.cos((point.angle - aspectAngle) * 2) * aspectStrength +
      smoothBump(point.angle, swellCenter, swellWidth) * swellStrength -
      smoothBump(point.angle, neckCenter, neckWidth) * neckStrength +
      smoothBump(point.angle, secondaryCenter, secondaryWidth) *
        secondaryStrength +
      filteredNoise * 0.08;
  });

  const meanRadius =
    points.reduce((total, point) => total + point.radius, 0) / controlCount;
  const contourEmphasis = 1.34 + random() * 0.42;
  points.forEach((point) => {
    const emphasizedRadius =
      1 + (point.radius / meanRadius - 1) * contourEmphasis;
    point.radius = Math.max(
      RIPPLE_CONTOUR_MIN_RADIUS_SCALE,
      Math.min(RIPPLE_CONTOUR_MAX_RADIUS_SCALE, emphasizedRadius),
    );
  });

  const slopeAt = (index: number): number => {
    const previous = (index + controlCount - 1) % controlCount;
    const next = (index + 1) % controlCount;
    let previousAngle = points[previous].angle;
    let nextAngle = points[next].angle;
    if (previous > index) previousAngle -= TWO_PI;
    if (next < index) nextAngle += TWO_PI;
    return (
      (points[next].radius - points[previous].radius) /
      (nextAngle - previousAngle)
    );
  };

  const contour = new Uint8Array(RIPPLE_CONTOUR_SAMPLES);
  let segment = controlCount - 1;
  for (let sample = 0; sample < RIPPLE_CONTOUR_SAMPLES; sample += 1) {
    const angle = (sample / RIPPLE_CONTOUR_SAMPLES) * TWO_PI;
    if (angle < points[0].angle) {
      segment = controlCount - 1;
    } else {
      if (segment === controlCount - 1) segment = 0;
      while (segment < controlCount - 1 && points[segment + 1].angle <= angle)
        segment += 1;
    }
    const next = (segment + 1) % controlCount;
    const startAngle = points[segment].angle;
    let endAngle = points[next].angle;
    let sampleAngle = angle;
    if (next === 0) endAngle += TWO_PI;
    if (sampleAngle < startAngle) sampleAngle += TWO_PI;
    const width = endAngle - startAngle;
    const t = Math.max(0, Math.min(1, (sampleAngle - startAngle) / width));
    const t2 = t * t;
    const t3 = t2 * t;
    const radius =
      (2 * t3 - 3 * t2 + 1) * points[segment].radius +
      (t3 - 2 * t2 + t) * width * slopeAt(segment) +
      (-2 * t3 + 3 * t2) * points[next].radius +
      (t3 - t2) * width * slopeAt(next);
    contour[sample] = encodedRadiusScale(
      Math.max(
        RIPPLE_CONTOUR_MIN_RADIUS_SCALE,
        Math.min(RIPPLE_CONTOUR_MAX_RADIUS_SCALE, finite(radius) ? radius : 1),
      ),
    );
  }
  return contour;
}

export function decodeRippleContourSample(sample: number): number {
  const normalized =
    Math.max(0, Math.min(255, Number.isFinite(sample) ? sample : 128)) / 255;
  return (
    RIPPLE_CONTOUR_MIN_RADIUS_SCALE +
    normalized *
      (RIPPLE_CONTOUR_MAX_RADIUS_SCALE - RIPPLE_CONTOUR_MIN_RADIUS_SCALE)
  );
}

/** A deterministic [0, 1) hash shared by the CPU-side tests and GLSL. */
export function stableHash01(
  x: number,
  y: number,
  seed = DEFAULT_WAVE_SEED,
): number {
  let foldedX = fract(x * 0.1031 + seed * 0.017);
  let foldedY = fract(y * 0.11369 + seed * 0.013);
  const dot = foldedX * (foldedY + 19.19) + foldedY * (foldedX + 19.19);
  // Re-fold before the final product so every shader/CPU intermediate stays
  // in a mediump-safe range even on a large grid.
  foldedX = fract(foldedX + dot);
  foldedY = fract(foldedY + dot);
  return fract((foldedX + foldedY) * foldedX * foldedY * 31.23);
}

export function glyphIndexForCell(
  cellX: number,
  cellY: number,
  seed = DEFAULT_WAVE_SEED,
  glyphCount = CHAT_WAVE_GLYPHS.length,
): number {
  if (
    !Number.isInteger(cellX) ||
    !Number.isInteger(cellY) ||
    !Number.isInteger(glyphCount) ||
    glyphCount <= 0
  ) {
    return 0;
  }
  return Math.min(
    glyphCount - 1,
    Math.floor(stableHash01(cellX, cellY, seed) * glyphCount),
  );
}

export function rippleSeed(
  x: number,
  y: number,
  startedAt: number,
  entropy = 0,
): number {
  // Quantizing time avoids making a different seed for insignificant timer
  // jitter. Entropy is captured once when the event is created so every
  // frame derives the same wave shape from the stable vec4 seed.
  const safeEntropy = normalizeUnitInterval(entropy);
  return stableHash01(
    x * 0.071 + safeEntropy * 17.17,
    y * 0.053 + safeEntropy * 11.37,
    Math.round(startedAt * 30) + DEFAULT_WAVE_SEED + safeEntropy * 5.31,
  );
}

export function rippleAge(startedAt: number, now: number): number {
  return Math.max(0, now - startedAt);
}

export class RipplePool {
  private readonly values: Ripple[] = [];
  private nextOrdinal = 0;
  private contourRevisionValue = 0;

  constructor(
    private readonly maxRipples = MAX_RIPPLES,
    private readonly lifetimeSeconds = RIPPLE_LIFETIME_SECONDS,
    private readonly entropySource?: () => number,
  ) {
    if (!Number.isInteger(maxRipples) || maxRipples <= 0)
      throw new Error("maxRipples must be a positive integer");
    if (!finite(lifetimeSeconds) || lifetimeSeconds <= 0)
      throw new Error("lifetimeSeconds must be positive");
  }

  get size(): number {
    return this.values.length;
  }

  get contourRevision(): number {
    return this.contourRevisionValue;
  }

  add(x: number, y: number, startedAt: number): boolean {
    if (![x, y, startedAt].every(finite)) return false;
    const ordinal = this.nextOrdinal;
    this.nextOrdinal += 1;
    const sourceEntropy = this.entropySource?.();
    // A pool used directly by tests remains reproducible by ordinal. The
    // renderer supplies Math.random as its event-only entropy source for
    // production taps.
    const ordinalEntropy = fract((ordinal + 1) * ORDINAL_ENTROPY_STEP);
    const entropy =
      sourceEntropy === undefined
        ? ordinalEntropy
        : normalizeUnitInterval(sourceEntropy, ordinalEntropy);
    const normalizedStartedAt = Math.max(0, startedAt);
    const seed = rippleSeed(x, y, normalizedStartedAt, entropy);
    const ripple: Ripple = {
      x,
      y,
      startedAt: normalizedStartedAt,
      seed,
      contour: generateRippleContour(seed),
    };
    if (this.values.length >= this.maxRipples) {
      // Replace the oldest wave so a rapid series of taps remains responsive.
      let oldest = 0;
      for (let index = 1; index < this.values.length; index += 1) {
        if (this.values[index].startedAt < this.values[oldest].startedAt)
          oldest = index;
      }
      this.values.splice(oldest, 1);
    }
    this.values.push(ripple);
    this.contourRevisionValue += 1;
    return true;
  }

  prune(now: number): void {
    if (!finite(now)) return;
    let removed = false;
    for (let index = this.values.length - 1; index >= 0; index -= 1) {
      if (now - this.values[index].startedAt > this.lifetimeSeconds) {
        this.values.splice(index, 1);
        removed = true;
      }
    }
    if (removed) this.contourRevisionValue += 1;
  }

  /** Return a stable snapshot for inspection without exposing internal state. */
  snapshot(now?: number): Ripple[] {
    if (now !== undefined) this.prune(now);
    return this.values.map((ripple) => ({
      ...ripple,
      contour: ripple.contour.slice(),
    }));
  }

  /** Write x/y/bounded-age/seed vec4s for the GLSL uniform array. */
  writeUniformData(now: number, target: Float32Array): number {
    if (target.length < this.maxRipples * 4)
      throw new Error("ripple uniform target is too small");
    this.prune(now);
    target.fill(0);
    this.values.forEach((ripple, index) => {
      const offset = index * 4;
      target[offset] = ripple.x;
      target[offset + 1] = ripple.y;
      const rawAge = finite(now) ? now - ripple.startedAt : 0;
      target[offset + 2] = Math.max(
        -1,
        Math.min(this.lifetimeSeconds + 1, rawAge),
      );
      target[offset + 3] = ripple.seed;
    });
    return this.values.length;
  }

  /** Pack active event contours into RGBA rows for the shared GL lookup texture. */
  writeContourTextureData(target: Uint8Array): void {
    const pixels = this.maxRipples * RIPPLE_CONTOUR_SAMPLES;
    if (target.length < pixels * 4)
      throw new Error("ripple contour texture target is too small");
    const neutralSample = encodedRadiusScale(1);
    for (let pixel = 0; pixel < pixels; pixel += 1) {
      const offset = pixel * 4;
      target[offset] = neutralSample;
      target[offset + 1] = 0;
      target[offset + 2] = 0;
      target[offset + 3] = 255;
    }
    this.values.forEach((ripple, row) => {
      for (let sample = 0; sample < RIPPLE_CONTOUR_SAMPLES; sample += 1) {
        const offset = (row * RIPPLE_CONTOUR_SAMPLES + sample) * 4;
        target[offset] = ripple.contour[sample];
      }
    });
  }
}
