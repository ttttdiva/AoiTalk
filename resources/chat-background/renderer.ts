import {
  CHAT_WAVE_ATLAS_COLUMNS,
  CHAT_WAVE_ATLAS_HEIGHT,
  CHAT_WAVE_ATLAS_WIDTH,
  CHAT_WAVE_GLYPH_ATLAS,
} from "./glyph-atlas";
import {
  CHAT_WAVE_BACKGROUND_TIME_SCALE,
  DEFAULT_WAVE_SEED,
  MAX_RIPPLES,
  RIPPLE_CONTOUR_MAX_RADIUS_SCALE,
  RIPPLE_CONTOUR_MIN_RADIUS_SCALE,
  RIPPLE_CONTOUR_SAMPLES,
  RIPPLE_LIFETIME_SECONDS,
  RIPPLE_SPEED_SCALE,
  RipplePool,
} from "./wave-core";

/**
 * A deliberately structural GL type. WebGLRenderingContext and
 * ExpoWebGLRenderingContext expose the same calls used here, while keeping
 * this shared module free of either platform's type package.
 */
export type ChatWaveGL = { [key: string]: any };

const MOTION_FREQUENCIES = Object.freeze([
  0.37, 0.53, 0.71, 0.89, 1.07, 1.29, 1.51, 1.79,
]);
// Slots 3 and 5 do not carry a wrapped oscillator phase. They carry a monotone
// drift of the idle noise field (in noise cells) so the background keeps
// receiving new content instead of sloshing around a bounded orbit. The noise
// lattice repeats every FLOW_LATTICE_PERIOD cells, so wrapping the drift there
// is seamless, and the whole field only repeats after several minutes.
const FLOW_DRIFT_SLOTS = Object.freeze([3, 5]);
const FLOW_DRIFT_CELLS_PER_SECOND = Object.freeze([0.071, 0.047]);
const FLOW_LATTICE_PERIOD = 24;
const TWO_PI = Math.PI * 2;
const RIPPLE_CONTOUR_MAX_DEVIATION = Math.max(
  Math.abs(1 / RIPPLE_CONTOUR_MIN_RADIUS_SCALE - 1),
  Math.abs(1 / RIPPLE_CONTOUR_MAX_RADIUS_SCALE - 1),
);

export function glslFloatLiteral(value: number): string {
  if (!Number.isFinite(value)) throw new Error("GLSL float literal must be finite");
  const literal = String(value);
  return /[.eE]/.test(literal) ? literal : `${literal}.0`;
}

function motionPhase(elapsedSeconds: number, frequency: number): number {
  if (
    !Number.isFinite(elapsedSeconds) ||
    !Number.isFinite(frequency) ||
    frequency <= 0
  )
    return 0;
  const period = TWO_PI / frequency;
  const wrappedSeconds = elapsedSeconds % period;
  const phase = wrappedSeconds * frequency;
  return phase >= TWO_PI ? 0 : Math.max(0, phase);
}

function writeMotionPhaseData(
  elapsedSeconds: number,
  target: Float32Array,
): void {
  if (target.length < MOTION_FREQUENCIES.length)
    throw new Error("motion phase target is too small");
  MOTION_FREQUENCIES.forEach((frequency, index) => {
    target[index] = motionPhase(elapsedSeconds, frequency);
  });
  FLOW_DRIFT_SLOTS.forEach((slot, index) => {
    target[slot] = Number.isFinite(elapsedSeconds)
      ? Math.max(
          0,
          (elapsedSeconds * FLOW_DRIFT_CELLS_PER_SECOND[index]) %
            FLOW_LATTICE_PERIOD,
        )
      : 0;
  });
}

export interface ChatWaveRendererOptions {
  /** Logical/CSS width and height. */
  width: number;
  height: number;
  dark?: boolean;
  /** Optional physical drawing-buffer dimensions when the host caps DPR. */
  drawingBufferWidth?: number;
  drawingBufferHeight?: number;
  /** Exposed for deterministic visual tests; production callers can omit it. */
  seed?: number;
  /** Event-only entropy source; production uses Math.random when omitted. */
  rippleEntropy?: () => number;
}

export interface ChatWaveRenderer {
  resize(
    width: number,
    height: number,
    drawingBufferWidth?: number,
    drawingBufferHeight?: number,
  ): void;
  render(elapsedSeconds: number): void;
  addRipple(x: number, y: number, timeSeconds: number): boolean;
  setDark(dark: boolean): void;
  dispose(): void;
}

export const CHAT_WAVE_CELL_SIZE = Object.freeze({ width: 7, height: 11 });

export const CHAT_WAVE_VERTEX_SHADER = `
attribute vec2 a_position;
void main() {
  gl_Position = vec4(a_position, 0.0, 1.0);
}
`;

// GLSL ES 1.00 is intentionally used here. It is accepted by browser WebGL
// and by expo-gl's WebGL2-equivalent context, so the visual field stays one
// shared implementation instead of maintaining two subtly different shaders.
export const CHAT_WAVE_FRAGMENT_SHADER = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

uniform vec2 u_resolution;       // logical/CSS pixels
uniform vec2 u_drawResolution;   // physical drawing-buffer pixels
uniform vec4 u_config;           // dark, background seed, ripple count, spare
uniform sampler2D u_glyphAtlas;
uniform sampler2D u_rippleContours;
uniform vec4 u_motionPhases[2];   // bounded, independently evolving flow phases
uniform vec4 u_ripples[8];       // x, y, bounded age, stable event seed
// 13 scalar/vector uniforms plus two samplers use 15 fragment uniform vectors,
// leaving one vector within ES2's minimum 16-vector budget.

const float CELL_W = 7.0;
const float CELL_H = 11.0;
const float GLYPH_W = 5.0;
const float GLYPH_H = 7.0;
const float RIPPLE_LIFE = ${glslFloatLiteral(RIPPLE_LIFETIME_SECONDS)};
const float RIPPLE_SPEED_SCALE = ${glslFloatLiteral(RIPPLE_SPEED_SCALE)};
const float FLOW_PERIOD = ${glslFloatLiteral(FLOW_LATTICE_PERIOD)};
const float ATLAS_WIDTH = ${glslFloatLiteral(CHAT_WAVE_ATLAS_WIDTH)};
const float ATLAS_HEIGHT = ${glslFloatLiteral(CHAT_WAVE_ATLAS_HEIGHT)};
const float GLYPH_COUNT = ${glslFloatLiteral(CHAT_WAVE_ATLAS_COLUMNS)};

float hashCell(vec2 cell) {
  vec2 folded = fract(cell * vec2(0.1031, 0.11369) + u_config.y * vec2(0.017, 0.013));
  float dotValue = dot(folded, folded.yx + vec2(19.19));
  // Keep all hash intermediates bounded for mediump fragment precision.
  folded = fract(folded + vec2(dotValue));
  return fract((folded.x + folded.y) * folded.x * folded.y * 31.23);
}

// Smooth value noise has no tiled sine bands. Motion uses bounded circular
// offsets on unrelated periods; wrapping a phase never resets the field.
float fieldNoise(vec2 p) {
  vec2 cell = floor(p);
  vec2 f = fract(p);
  // The lattice repeats every FLOW_LATTICE_PERIOD cells (seamless drift wrap
  // and small mediump arguments).
  vec2 c0 = mod(cell, FLOW_PERIOD);
  vec2 c1 = mod(cell + 1.0, FLOW_PERIOD);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(hashCell(c0), hashCell(vec2(c1.x, c0.y)), f.x),
             mix(hashCell(vec2(c0.x, c1.y)), hashCell(c1), f.x), f.y);
}
vec2 flowOrbit() {
  return vec2(sin(u_motionPhases[0].x), cos(u_motionPhases[0].y)) * 1.9
       + vec2(cos(u_motionPhases[1].z), sin(u_motionPhases[1].w)) * 0.55;
}
vec2 flowDrift() {
  return vec2(u_motionPhases[0].w, -u_motionPhases[1].y);
}
vec2 flowOffset() {
  return flowOrbit() + flowDrift();
}
float broadWave(vec2 p) {
  vec2 q = p * 0.007;
  vec2 orbit = flowOrbit();
  vec2 drift = flowDrift();
  vec2 offset = orbit + drift;
  vec2 bend = vec2(fieldNoise(q * 0.63 + offset + vec2(13.7, 5.2)),
                   fieldNoise(q * 0.71 - offset.yx + vec2(31.4, 17.8))) - 0.5;
  q += bend * 1.7;
  // Keep drift at whole lattice periods in every noise domain. Scaling the
  // wrapped drift by 0.71 would jump 17.04 cells when its 24-cell phase wraps.
  return fieldNoise(q + offset) * 0.68
       + fieldNoise(mat2(0.8, 0.6, -0.6, 0.8) * q * 1.93 - orbit * 0.71 - drift + vec2(8.3, 29.1)) * 0.32;
}
vec2 idleWarp(vec2 p) {
  vec2 q = p * 0.009;
  vec2 offset = vec2(cos(u_motionPhases[0].z), sin(u_motionPhases[1].x)) * 1.5;
  return (vec2(fieldNoise(q + offset + vec2(9.1, 21.3)),
               fieldNoise(q * 0.83 - offset.yx + vec2(41.7, 3.9))) - 0.5) * 10.0;
}

float eventNoise(float seed, float salt) {
  // This is a deterministic lookup from the event seed, never a per-frame
  // random source. Keep every intermediate bounded for mediump fragment
  // paths; avoiding sin here keeps up to eight active ripples inexpensive.
  float folded = fract(seed * 43.17 + salt * 0.173);
  return fract(folded * (folded + 19.19) + salt * 0.731);
}

void main() {
  // gl_FragCoord is physical, but every visual parameter below is expressed
  // in logical pixels. This keeps pointer coordinates aligned when the host
  // caps DPR or uses a larger drawing buffer than its CSS box.
  vec2 physical = gl_FragCoord.xy / max(u_drawResolution, vec2(1.0));
  vec2 p = vec2(physical.x * u_resolution.x, (1.0 - physical.y) * u_resolution.y);
  // Domain warping breaks straight, regularly spaced bands while remaining
  // temporally coherent (no per-frame random flicker).
  vec2 flowDomain = p + idleWarp(p * 0.47) * 6.0;
  vec2 warped = p + idleWarp(flowDomain);

  float rippleSignal = 0.0;
  float signedWave = 0.0;
  float waveEnergy = 0.0;
  float rippleLifeWeighted = 0.0;
  float rippleLifeWeight = 0.0;
  vec2 rippleShift = vec2(0.0);
  float baseSpeed = clamp(length(u_resolution * 0.001) * 125.0, 85.0, 230.0);
  for (int i = 0; i < 8; i += 1) {
    if (float(i) >= u_config.z) continue;
    vec4 ripple = u_ripples[i];
    float age = ripple.z;
    if (age < 0.0 || age > RIPPLE_LIFE) continue;
    vec2 fromOrigin = p - ripple.xy;
    float distanceScale = max(max(abs(fromOrigin.x), abs(fromOrigin.y)), 1.0);
    float distanceFromOrigin = length(fromOrigin / distanceScale) * distanceScale;
    // Slow the travel after the legacy 85px/s floor so small canvases still
    // receive a visibly calmer wave instead of being clamped back to 85.
    float eventSpeedScale = mix(0.86, 1.18, eventNoise(ripple.w, 3.7));
    float speed = clamp(baseSpeed * eventSpeedScale, 85.0, 260.0) * RIPPLE_SPEED_SCALE;
    vec2 direction = fromOrigin / max(distanceFromOrigin, 0.001);
    float wavelength = mix(62.0, 105.0, eventNoise(ripple.w, 10.4));
    float widthNoise = eventNoise(ripple.w, 4.5);
    float narrowEvent = step(0.86, widthNoise);
    float widthScale = mix(
      mix(0.36, 0.58, widthNoise),
      mix(0.22, 0.32, eventNoise(ripple.w, 52.1)),
      narrowEvent
    );
    float frontWidth = wavelength * widthScale;
    float reachScale = mix(0.78, 1.38, eventNoise(ripple.w, 7.9));
    float eventLifetime = mix(4.6, RIPPLE_LIFE, eventNoise(ripple.w, 14.1));
    float eventStrength = mix(0.75, 1.22, eventNoise(ripple.w, 8.9));
    if (age > eventLifetime) continue;
    float radius = age * speed * reachScale;
    // Each click gets zero to five weaker followers. Their positive gaps are
    // independently salted, so a few events show a long, visible train while
    // others stay simple and quiet.
    float followerCount = floor(eventNoise(ripple.w, 42.7) * 6.0);
    float follower1Gate = step(0.5, followerCount);
    float follower2Gate = step(1.5, followerCount);
    float follower3Gate = step(2.5, followerCount);
    float follower4Gate = step(3.5, followerCount);
    float follower5Gate = step(4.5, followerCount);
    float follower1Gap = wavelength * mix(0.62, 1.18, eventNoise(ripple.w, 18.1));
    float follower2Gap = wavelength * mix(0.38, 1.08, eventNoise(ripple.w, 25.6));
    float follower3Gap = wavelength * mix(0.34, 1.16, eventNoise(ripple.w, 33.2));
    float follower4Gap = wavelength * mix(0.32, 1.24, eventNoise(ripple.w, 40.1));
    float follower5Gap = wavelength * mix(0.30, 1.32, eventNoise(ripple.w, 47.3));
    float follower1Delay = follower1Gap;
    float follower2Delay = follower1Delay + follower2Gap;
    float follower3Delay = follower2Delay + follower3Gap;
    float follower4Delay = follower3Delay + follower4Gap;
    float follower5Delay = follower4Delay + follower5Gap;
    float follower1Strength = mix(0.30, 0.46, eventNoise(ripple.w, 12.3));
    float follower2Strength = mix(0.20, 0.29, eventNoise(ripple.w, 22.9));
    float follower3Strength = mix(0.13, 0.19, eventNoise(ripple.w, 31.4));
    float follower4Strength = mix(0.08, 0.12, eventNoise(ripple.w, 39.2));
    float follower5Strength = mix(0.045, 0.075, eventNoise(ripple.w, 46.8));
    float radialBehind = radius - distanceFromOrigin;
    // Ignore distant fragments before the contour texture lookup and Gaussian
    // evaluation. The extra radius bound includes every permitted contour
    // scale, the farthest derived follower, and a warp margin.
    float maxFollowerDelay = max(
      follower1Delay * follower1Gate,
      max(
        follower2Delay * follower2Gate,
        max(
          follower3Delay * follower3Gate,
          max(follower4Delay * follower4Gate, follower5Delay * follower5Gate)
        )
      )
    );
    float contourMargin = distanceFromOrigin * ${glslFloatLiteral(RIPPLE_CONTOUR_MAX_DEVIATION)} + 2.0;
    if (radialBehind < -contourMargin - 6.0 * frontWidth
        || radialBehind > contourMargin + maxFollowerDelay + 8.0 * frontWidth + 10.0) continue;
    // Each event owns a smooth, precomputed radial profile. Sampling one
    // wrapped lookup keeps the shader independent of contour control-point
    // count and holds the same outline for every frame of this wave.
    float contourAngle = distanceFromOrigin > 0.001
      ? atan(fromOrigin.y, fromOrigin.x) * 0.1591549431 + 0.5
      : 0.5;
    float contourRow = (float(i) + 0.5) / 8.0;
    float encodedContourScale = texture2D(u_rippleContours, vec2(contourAngle, contourRow)).r;
    float contourScale = mix(${glslFloatLiteral(RIPPLE_CONTOUR_MIN_RADIUS_SCALE)}, ${glslFloatLiteral(RIPPLE_CONTOUR_MAX_RADIUS_SCALE)}, encodedContourScale);
    float bentDistance = distanceFromOrigin / max(contourScale, 0.001);
    float behindFront = radius - bentDistance;
    float fade = 1.0 - smoothstep(eventLifetime * 0.70, eventLifetime, age);
    float envelope = 1.45 * eventStrength * exp(-age * mix(0.14, 0.22, eventNoise(ripple.w, 19.9)))
      * smoothstep(0.0, 0.16, age) * fade;
    float z0 = behindFront / frontWidth;
    float z1 = (behindFront - follower1Delay) / (frontWidth * mix(1.04, 1.30, eventNoise(ripple.w, 15.3)));
    float z2 = (behindFront - follower2Delay) / (frontWidth * mix(1.10, 1.52, eventNoise(ripple.w, 27.7)));
    float z3 = (behindFront - follower3Delay) / (frontWidth * mix(1.16, 1.72, eventNoise(ripple.w, 35.8)));
    float z4 = (behindFront - follower4Delay) / (frontWidth * mix(1.20, 1.86, eventNoise(ripple.w, 42.6)));
    float z5 = (behindFront - follower5Delay) / (frontWidth * mix(1.24, 2.00, eventNoise(ripple.w, 49.1)));
    float zTrough = (behindFront - wavelength * 0.48) / (frontWidth * 1.35);
    // z*z avoids pow() in the six-band packet and is friendlier to mediump
    // mobile fragment paths while retaining the same soft Gaussian profile.
    float primary = exp(-min(z0 * z0, 25.0));
    float follower1 = exp(-min(z1 * z1, 25.0)) * follower1Strength * follower1Gate;
    float follower2 = exp(-min(z2 * z2, 25.0)) * follower2Strength * follower2Gate;
    float follower3 = exp(-min(z3 * z3, 25.0)) * follower3Strength * follower3Gate;
    float follower4 = exp(-min(z4 * z4, 25.0)) * follower4Strength * follower4Gate;
    float follower5 = exp(-min(z5 * z5, 25.0)) * follower5Strength * follower5Gate;
    float trough = exp(-min(zTrough * zTrough, 25.0)) * mix(0.18, 0.30, eventNoise(ripple.w, 54.7));
    float wave = (primary + follower1 + follower2 + follower3 + follower4 + follower5 - trough) * envelope;
    float brightness = mix(0.82, 1.12, eventNoise(ripple.w, 9.3));
    float coloredWave = wave * brightness;
    float colorWeight = abs(coloredWave);
    signedWave += coloredWave;
    waveEnergy += coloredWave * coloredWave;
    rippleLifeWeighted += colorWeight * clamp(age / eventLifetime, 0.0, 1.0);
    rippleLifeWeight += colorWeight;
    rippleShift += direction * wave * 2.1;

  }
  // Cross terms are zero for an isolated wave and appear only where packets
  // overlap. Signed superposition creates interference, not a generic flash.
  float interference = signedWave * signedWave - waveEnergy;
  rippleSignal += max(signedWave, 0.0) * 0.95 + max(interference, 0.0) * 0.42;
  float rippleLifePhase = rippleLifeWeighted / max(rippleLifeWeight, 0.0001);
  float troughDim = clamp(1.0 + min(signedWave, 0.0) * 0.85, 0.30, 1.0);
  rippleSignal *= 1.0 / (1.0 + max(-interference, 0.0) * 0.65);
  rippleSignal = 1.22 * rippleSignal / (1.0 + rippleSignal);
  float rippleShiftLength = length(rippleShift);
  float rippleShiftBlend = smoothstep(3.0, 4.0, rippleShiftLength);
  float softShiftLength = mix(rippleShiftLength, 3.5, rippleShiftBlend);
  rippleShift *= softShiftLength / max(rippleShiftLength, 0.001);
  warped += rippleShift;

  vec2 cell = floor(warped / vec2(CELL_W, CELL_H));
  float glyph = floor(hashCell(cell) * GLYPH_COUNT);
  vec2 cellPixel = fract(warped / vec2(CELL_W, CELL_H)) * vec2(CELL_W, CELL_H);
  float insideX = step(1.0, cellPixel.x) * (1.0 - step(6.0, cellPixel.x));
  float insideY = step(2.0, cellPixel.y) * (1.0 - step(9.0, cellPixel.y));
  vec2 glyphLocal = vec2((cellPixel.x - 1.0) / 5.0, (cellPixel.y - 2.0) / 7.0);
  float glyphX = glyph * GLYPH_W + floor(glyphLocal.x * GLYPH_W) + 0.5;
  float glyphY = (GLYPH_H - 1.0 - floor(glyphLocal.y * GLYPH_H)) + 0.5;
  vec2 atlasUv = vec2(glyphX / ATLAS_WIDTH, glyphY / ATLAS_HEIGHT);
  float ink = texture2D(u_glyphAtlas, atlasUv).r * insideX * insideY;

  float ambient = smoothstep(0.16, 0.84, clamp(broadWave(warped), 0.0, 1.0));
  float slowBand = fieldNoise(p * 0.0047 + flowOffset().yx + vec2(57.1, 8.4));
  float crest = pow(ambient, 1.35);
  float baseSignal = ink * (0.035 + crest * 0.58 + slowBand * 0.045) * troughDim;
  float rippleContribution = ink * rippleSignal * troughDim;

  // The canvas is an overlay over the app's own background. Keep the base
  // transparent and combine the idle field and ripple as premultiplied color;
  // this lets light, dark, and user-agent themed surfaces remain visible.
  vec3 darkInk = vec3(0.270, 0.510, 0.660);
  vec3 lightInk = vec3(0.220, 0.330, 0.400);
  vec3 glyphColor = mix(lightInk, darkInk, u_config.x);

  // A signal-local mask leaves the idle palette untouched where no wave is
  // present. Fixed neighbouring hues avoid rainbow rotation: a faint blue-
  // violet skirt yields to teal, then to a restrained blue-white dark crest.
  // The light-theme crest stays saturated and dark enough to survive white.
  float rippleMask = smoothstep(0.010, 0.56, rippleSignal);
  float rippleStrength = clamp(rippleSignal / 1.08, 0.0, 1.0);
  float rippleVitality = 1.0 - smoothstep(0.32, 1.0, rippleLifePhase);
  float bodyMix = smoothstep(0.025, 0.38, rippleSignal)
    * mix(0.78, 1.0, rippleVitality);
  float crestMix = smoothstep(0.20, 0.88, rippleStrength)
    * mix(0.68, 1.0, rippleVitality);
  vec3 rippleSkirt = mix(vec3(0.240, 0.270, 0.620), vec3(0.520, 0.400, 0.920), u_config.x);
  vec3 rippleTeal = mix(vec3(0.020, 0.430, 0.480), vec3(0.380, 0.860, 0.900), u_config.x);
  vec3 rippleCrest = mix(vec3(0.040, 0.360, 0.560), vec3(0.820, 0.940, 1.000), u_config.x);
  vec3 rippleColor = mix(rippleSkirt, rippleTeal, bodyMix);
  rippleColor = mix(rippleColor, rippleCrest, crestMix);
  rippleColor = mix(glyphColor, rippleColor, rippleMask);

  // Ripple luminance is independent of the ambient field. Both contributions
  // are accumulated premultiplied, then scaled together if the alpha ceiling
  // is reached so RGB never exceeds alpha on bright collisions.
  float baseAlpha = baseSignal * mix(0.16, 0.28, u_config.x);
  float rippleLuminance = mix(0.82, 1.05, rippleVitality)
    * mix(0.86, 1.0, rippleStrength);
  float rippleAlpha = rippleContribution * mix(0.36, 0.50, u_config.x) * rippleLuminance;
  float rawAlpha = baseAlpha + rippleAlpha;
  float alpha = clamp(rawAlpha, 0.0, 0.58);
  float premultipliedScale = alpha / max(rawAlpha, 0.0001);
  vec3 premultipliedColor = (glyphColor * baseAlpha + rippleColor * rippleAlpha)
    * premultipliedScale;
  gl_FragColor = vec4(premultipliedColor, alpha);
}
`;

function errorFromUnknown(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function finitePositive(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function expandAtlasToRgba(source: Uint8Array): Uint8Array {
  const rgba = new Uint8Array(source.length * 4);
  for (let index = 0; index < source.length; index += 1) {
    const offset = index * 4;
    const value = source[index];
    rgba[offset] = value;
    rgba[offset + 1] = value;
    rgba[offset + 2] = value;
    rgba[offset + 3] = 255;
  }
  return rgba;
}

function compileShader(gl: ChatWaveGL, type: number, source: string): any {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("チャット背景のGL shaderを確保できませんでした");
  try {
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(
        gl.getShaderInfoLog(shader) ||
          "チャット背景のGL shader compileに失敗しました",
      );
    }
    return shader;
  } catch (error) {
    try {
      gl.deleteShader(shader);
    } catch {
      // Cleanup is best-effort when the context itself is already lost.
    }
    throw errorFromUnknown(error);
  }
}

function createProgram(gl: ChatWaveGL): any {
  const program = gl.createProgram();
  if (!program)
    throw new Error("チャット背景のGL programを確保できませんでした");
  let vertexShader: any = null;
  let fragmentShader: any = null;
  try {
    vertexShader = compileShader(gl, gl.VERTEX_SHADER, CHAT_WAVE_VERTEX_SHADER);
    fragmentShader = compileShader(
      gl,
      gl.FRAGMENT_SHADER,
      CHAT_WAVE_FRAGMENT_SHADER,
    );
    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(
        gl.getProgramInfoLog(program) ||
          "チャット背景のGL program linkに失敗しました",
      );
    }
    return program;
  } catch (error) {
    try {
      gl.deleteProgram(program);
    } catch {
      // Cleanup is best-effort when the context itself is already lost.
    }
    throw errorFromUnknown(error);
  } finally {
    if (vertexShader) {
      try {
        gl.detachShader?.(program, vertexShader);
      } catch {
        // Ignore detach errors from a lost/failed context.
      }
      try {
        gl.deleteShader(vertexShader);
      } catch {
        // Ignore cleanup errors.
      }
    }
    if (fragmentShader) {
      try {
        gl.detachShader?.(program, fragmentShader);
      } catch {
        // Ignore detach errors from a lost/failed context.
      }
      try {
        gl.deleteShader(fragmentShader);
      } catch {
        // Ignore cleanup errors.
      }
    }
  }
}

class ChatWaveRendererImpl implements ChatWaveRenderer {
  private readonly gl: ChatWaveGL;
  private readonly program: any;
  private readonly buffer: any;
  private readonly atlas: any;
  private readonly contours: any;
  private readonly positionLocation: number;
  private readonly uniforms: Record<string, any>;
  private readonly ripples: RipplePool;
  private readonly motionPhaseData = new Float32Array(
    MOTION_FREQUENCIES.length,
  );
  private readonly rippleUniformData = new Float32Array(MAX_RIPPLES * 4);
  private readonly contourTextureData = new Uint8Array(
    MAX_RIPPLES * RIPPLE_CONTOUR_SAMPLES * 4,
  );
  private readonly seed: number;
  private logicalWidth = 1;
  private logicalHeight = 1;
  private drawingBufferWidth = 1;
  private drawingBufferHeight = 1;
  private dark = true;
  private lastTime = 0;
  private lastContourRevision = -1;
  private disposed = false;

  constructor(gl: ChatWaveGL, options: ChatWaveRendererOptions) {
    this.gl = gl;
    this.dark = options.dark !== false;
    this.ripples = new RipplePool(
      MAX_RIPPLES,
      RIPPLE_LIFETIME_SECONDS,
      options.rippleEntropy ?? Math.random,
    );
    this.seed = Number.isFinite(options.seed)
      ? Math.abs(Number(options.seed) % 100)
      : DEFAULT_WAVE_SEED;
    let program: any = null;
    let buffer: any = null;
    let atlas: any = null;
    let contours: any = null;
    try {
      if (
        !gl ||
        typeof gl.createShader !== "function" ||
        typeof gl.createProgram !== "function"
      ) {
        throw new Error("チャット背景にはWebGL / expo-gl contextが必要です");
      }
      program = createProgram(gl);
      buffer = gl.createBuffer();
      if (!buffer)
        throw new Error("チャット背景のGL bufferを確保できませんでした");
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(
        gl.ARRAY_BUFFER,
        new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
        gl.STATIC_DRAW,
      );

      atlas = gl.createTexture();
      if (!atlas)
        throw new Error(
          "チャット背景のglyph atlas textureを確保できませんでした",
        );
      gl.bindTexture(gl.TEXTURE_2D, atlas);
      gl.pixelStorei?.(gl.UNPACK_ALIGNMENT, 1);
      // Expo GL's ES2 contexts may expose R8/RED enum numbers even though
      // those formats are unsupported. RGBA is universally available and the
      // tiny atlas costs only about 12 KiB after expansion.
      const rgbaAtlas = expandAtlasToRgba(CHAT_WAVE_GLYPH_ATLAS.data);
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        CHAT_WAVE_ATLAS_WIDTH,
        CHAT_WAVE_ATLAS_HEIGHT,
        0,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        rgbaAtlas,
      );
      const uploadError = gl.getError();
      if (uploadError !== gl.NO_ERROR) {
        throw new Error(
          `チャット背景のglyph atlas uploadに失敗しました (${uploadError})`,
        );
      }
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

      contours = gl.createTexture();
      if (!contours)
        throw new Error(
          "チャット背景のripple contour textureを確保できませんでした",
        );
      gl.bindTexture(gl.TEXTURE_2D, contours);
      this.ripples.writeContourTextureData(this.contourTextureData);
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        RIPPLE_CONTOUR_SAMPLES,
        MAX_RIPPLES,
        0,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        this.contourTextureData,
      );
      const contourUploadError = gl.getError();
      if (contourUploadError !== gl.NO_ERROR) {
        throw new Error(
          `チャット背景のripple contour uploadに失敗しました (${contourUploadError})`,
        );
      }
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

      this.program = program;
      this.buffer = buffer;
      this.atlas = atlas;
      this.contours = contours;
      this.lastContourRevision = this.ripples.contourRevision;
      this.positionLocation = gl.getAttribLocation(program, "a_position");
      this.uniforms = {
        resolution: gl.getUniformLocation(program, "u_resolution"),
        drawResolution: gl.getUniformLocation(program, "u_drawResolution"),
        config: gl.getUniformLocation(program, "u_config"),
        atlas: gl.getUniformLocation(program, "u_glyphAtlas"),
        contours: gl.getUniformLocation(program, "u_rippleContours"),
        motionPhases: gl.getUniformLocation(program, "u_motionPhases[0]"),
        ripples: gl.getUniformLocation(program, "u_ripples[0]"),
      };
      gl.bindTexture(gl.TEXTURE_2D, null);
      gl.bindBuffer(gl.ARRAY_BUFFER, null);
      this.resize(
        options.width,
        options.height,
        options.drawingBufferWidth,
        options.drawingBufferHeight,
      );
    } catch (error) {
      if (contours) {
        try {
          gl.deleteTexture(contours);
        } catch {
          // Best-effort cleanup.
        }
      }
      if (atlas) {
        try {
          gl.deleteTexture(atlas);
        } catch {
          // Best-effort cleanup.
        }
      }
      if (buffer) {
        try {
          gl.deleteBuffer(buffer);
        } catch {
          // Best-effort cleanup.
        }
      }
      if (program) {
        try {
          gl.deleteProgram(program);
        } catch {
          // Best-effort cleanup.
        }
      }
      throw errorFromUnknown(error);
    }
  }

  resize(
    width: number,
    height: number,
    drawingBufferWidth?: number,
    drawingBufferHeight?: number,
  ): void {
    if (this.disposed) throw new Error("チャット背景rendererはdispose済みです");
    this.logicalWidth = finitePositive(width, 1);
    this.logicalHeight = finitePositive(height, 1);
    const queriedWidth = Number(this.gl.drawingBufferWidth);
    const queriedHeight = Number(this.gl.drawingBufferHeight);
    this.drawingBufferWidth = Math.max(
      1,
      Math.round(
        finitePositive(drawingBufferWidth ?? queriedWidth, this.logicalWidth),
      ),
    );
    this.drawingBufferHeight = Math.max(
      1,
      Math.round(
        finitePositive(
          drawingBufferHeight ?? queriedHeight,
          this.logicalHeight,
        ),
      ),
    );
    this.gl.viewport(0, 0, this.drawingBufferWidth, this.drawingBufferHeight);
  }

  render(elapsedSeconds: number): void {
    if (this.disposed) throw new Error("チャット背景rendererはdispose済みです");
    const requestedTime = Number.isFinite(elapsedSeconds)
      ? Math.max(0, elapsedSeconds)
      : this.lastTime;
    const time = Math.max(this.lastTime, requestedTime);
    this.lastTime = time;
    const gl = this.gl;
    // The idle field moves at a calmer pace, while RipplePool receives the
    // unscaled clock below so click-wave age and lifetime remain responsive.
    writeMotionPhaseData(
      time * CHAT_WAVE_BACKGROUND_TIME_SCALE,
      this.motionPhaseData,
    );
    const activeRippleCount = this.ripples.writeUniformData(
      time,
      this.rippleUniformData,
    );
    if (this.lastContourRevision !== this.ripples.contourRevision) {
      this.ripples.writeContourTextureData(this.contourTextureData);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, this.contours);
      gl.texSubImage2D(
        gl.TEXTURE_2D,
        0,
        0,
        0,
        RIPPLE_CONTOUR_SAMPLES,
        MAX_RIPPLES,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        this.contourTextureData,
      );
      this.lastContourRevision = this.ripples.contourRevision;
    }

    gl.viewport(0, 0, this.drawingBufferWidth, this.drawingBufferHeight);
    gl.enable?.(gl.BLEND);
    // Shader output is premultiplied RGBA; blend against the existing app
    // background instead of painting an opaque theme-colored rectangle.
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.enableVertexAttribArray(this.positionLocation);
    gl.vertexAttribPointer(this.positionLocation, 2, gl.FLOAT, false, 0, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.atlas);
    gl.uniform1i(this.uniforms.atlas, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.contours);
    gl.uniform1i(this.uniforms.contours, 1);
    gl.uniform2f(
      this.uniforms.resolution,
      this.logicalWidth,
      this.logicalHeight,
    );
    gl.uniform2f(
      this.uniforms.drawResolution,
      this.drawingBufferWidth,
      this.drawingBufferHeight,
    );
    gl.uniform4f(
      this.uniforms.config,
      this.dark ? 1 : 0,
      this.seed,
      activeRippleCount,
      0,
    );
    gl.uniform4fv(this.uniforms.motionPhases, this.motionPhaseData);
    gl.uniform4fv(this.uniforms.ripples, this.rippleUniformData);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.bindTexture(gl.TEXTURE_2D, null);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
    gl.endFrameEXP?.();
  }

  addRipple(x: number, y: number, timeSeconds: number): boolean {
    if (this.disposed) throw new Error("チャット背景rendererはdispose済みです");
    if (![x, y, timeSeconds].every(Number.isFinite)) return false;
    const clampedX = Math.max(0, Math.min(this.logicalWidth, x));
    const clampedY = Math.max(0, Math.min(this.logicalHeight, y));
    return this.ripples.add(clampedX, clampedY, timeSeconds);
  }

  setDark(dark: boolean): void {
    if (this.disposed) throw new Error("チャット背景rendererはdispose済みです");
    this.dark = dark;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    try {
      this.gl.deleteTexture(this.contours);
    } catch {
      // A context may already be lost during screen teardown.
    }
    try {
      this.gl.deleteTexture(this.atlas);
    } catch {
      // A context may already be lost during screen teardown.
    }
    try {
      this.gl.deleteBuffer(this.buffer);
    } catch {
      // A context may already be lost during screen teardown.
    }
    try {
      this.gl.deleteProgram(this.program);
    } catch {
      // A context may already be lost during screen teardown.
    }
  }
}

export function createChatWaveRenderer(
  gl: ChatWaveGL,
  options: ChatWaveRendererOptions,
): ChatWaveRenderer {
  return new ChatWaveRendererImpl(gl, options);
}
