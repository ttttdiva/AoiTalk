/**
 * A small, byte based glyph atlas shared by the Web and Native chat wave
 * renderers.  Keeping the atlas in source avoids loading a platform specific
 * font or generating thousands of text nodes for the background.
 */

export const CHAT_WAVE_GLYPHS =
  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+-*/=<>[]{}#@$%&!?;:.,_";

// The shared shader samples a contiguous 5x7 cell for each glyph. Keep this
// byte atlas deliberately flat; the renderer expands it to a small RGBA
// Uint8Array because RGBA is available on both browser WebGL and expo-gl ES2.
export const CHAT_WAVE_GLYPH_WIDTH = 5;
export const CHAT_WAVE_GLYPH_HEIGHT = 7;
export const CHAT_WAVE_ATLAS_COLUMNS = CHAT_WAVE_GLYPHS.length;
export const CHAT_WAVE_ATLAS_ROWS = 1;
export const CHAT_WAVE_ATLAS_WIDTH =
  CHAT_WAVE_GLYPHS.length * CHAT_WAVE_GLYPH_WIDTH;
export const CHAT_WAVE_ATLAS_HEIGHT = CHAT_WAVE_GLYPH_HEIGHT;

type GlyphRows = readonly string[];

// A compact 5x7 bitmap font.  Missing glyphs use a deterministic fallback so
// the atlas still has a stable byte layout when the character set changes.
const FONT: Record<string, GlyphRows> = {
  "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
  "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
  "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
  "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
  "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
  "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
  "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
  "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
  "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
  "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
  A: ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
  B: ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
  C: ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
  D: ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
  E: ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
  F: ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
  G: ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
  H: ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
  I: ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
  J: ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
  K: ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
  L: ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
  M: ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
  N: ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
  O: ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
  P: ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
  Q: ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
  R: ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
  S: ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
  T: ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
  U: ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
  V: ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
  W: ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
  X: ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
  Y: ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
  Z: ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
  "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
  "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
  "*": ["00000", "10101", "01110", "11111", "01110", "10101", "00000"],
  "/": ["00001", "00010", "00100", "01000", "10000", "00000", "00000"],
  "=": ["00000", "11111", "00000", "11111", "00000", "00000", "00000"],
  "<": ["00010", "00100", "01000", "10000", "01000", "00100", "00010"],
  ">": ["01000", "00100", "00010", "00001", "00010", "00100", "01000"],
  "[": ["01110", "01000", "01000", "01000", "01000", "01000", "01110"],
  "]": ["01110", "00010", "00010", "00010", "00010", "00010", "01110"],
  "{": ["00110", "00100", "00100", "01000", "00100", "00100", "00110"],
  "}": ["01100", "00100", "00100", "00010", "00100", "00100", "01100"],
  "#": ["01010", "11111", "01010", "01010", "11111", "01010", "00000"],
  "@": ["01110", "10001", "10111", "10101", "10111", "10000", "01110"],
  "$": ["00100", "01111", "10100", "01110", "00101", "11110", "00100"],
  "%": ["11001", "11010", "00010", "00100", "01000", "01011", "10011"],
  "&": ["01100", "10010", "10100", "01000", "10101", "10010", "01101"],
  "!": ["00100", "00100", "00100", "00100", "00100", "00000", "00100"],
  "?": ["01110", "10001", "00001", "00010", "00100", "00000", "00100"],
  ";": ["00000", "00100", "00100", "00000", "00100", "01000", "00000"],
  ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
  ".": ["00000", "00000", "00000", "00000", "00000", "00110", "00110"],
  ",": ["00000", "00000", "00000", "00000", "00110", "00110", "00100"],
  "_": ["00000", "00000", "00000", "00000", "00000", "00000", "11111"],
};

function fallbackGlyph(char: string): GlyphRows {
  const code = char.codePointAt(0) ?? 0;
  return Array.from({ length: 7 }, (_, row) => {
    const bits = ((code * 17 + row * 29) ^ (code >>> (row % 4))) & 0x1f;
    return bits.toString(2).padStart(5, "0");
  });
}

export function buildGlyphAtlas(): Uint8Array {
  const data = new Uint8Array(CHAT_WAVE_ATLAS_WIDTH * CHAT_WAVE_ATLAS_HEIGHT);
  CHAT_WAVE_GLYPHS.split("").forEach((char, glyphIndex) => {
    const rows = FONT[char] ?? FONT[char.toUpperCase()] ?? fallbackGlyph(char);
    const cellX = glyphIndex * CHAT_WAVE_GLYPH_WIDTH;
    const cellY = 0;
    rows.forEach((row, y) => {
      for (let x = 0; x < row.length; x += 1) {
        if (row[x] !== "1") continue;
        const pixelX = cellX + x;
        const pixelY = cellY + y;
        data[pixelY * CHAT_WAVE_ATLAS_WIDTH + pixelX] = 255;
      }
    });
  });
  return data;
}

/** Single channel byte texture data; the renderer expands it to RGBA. */
export const CHAT_WAVE_GLYPH_ATLAS = Object.freeze({
  width: CHAT_WAVE_ATLAS_WIDTH,
  height: CHAT_WAVE_ATLAS_HEIGHT,
  columns: CHAT_WAVE_ATLAS_COLUMNS,
  rows: CHAT_WAVE_ATLAS_ROWS,
  glyphWidth: CHAT_WAVE_GLYPH_WIDTH,
  glyphHeight: CHAT_WAVE_GLYPH_HEIGHT,
  count: CHAT_WAVE_GLYPHS.length,
  data: buildGlyphAtlas(),
});

// Names kept intentionally small and platform-neutral for the worker module.
export function glyphIndexForCharacter(char: string): number {
  const direct = CHAT_WAVE_GLYPHS.indexOf(char);
  if (direct >= 0) return direct;
  const upper = char.length > 0 ? char[0].toUpperCase() : "";
  const fallback = CHAT_WAVE_GLYPHS.indexOf(upper);
  return fallback >= 0 ? fallback : 0;
}
