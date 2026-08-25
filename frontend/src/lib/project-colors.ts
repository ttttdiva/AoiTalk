export type ProjectColorTheme = "light" | "dark";

export type ProjectColorTokens = {
  accent: string;
  surface: string;
  surfaceAlt: string;
  surfaceHover: string;
  surfaceGradient: string;
  border: string;
  text: string;
  mutedText: string;
  stripe: string;
};

type Rgb = {
  r: number;
  g: number;
  b: number;
};

const DEFAULT_TEXT_COLOR = "var(--foreground)";
const DEFAULT_MUTED_TEXT_COLOR = "var(--muted-foreground)";

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function normalizeProjectColor(
  color: string | null | undefined,
): string | null {
  const normalized = color?.trim();
  return normalized ? normalized : null;
}

function parseHexColor(color: string): Rgb | null {
  const normalized = color.trim();
  const short = normalized.match(/^#([0-9a-fA-F]{3})$/);
  if (short) {
    const [r, g, b] = short[1]
      .split("")
      .map((part) => parseInt(part + part, 16));
    return { r, g, b };
  }

  const full = normalized.match(/^#([0-9a-fA-F]{6})$/);
  if (!full) return null;

  const value = full[1];
  return {
    r: parseInt(value.slice(0, 2), 16),
    g: parseInt(value.slice(2, 4), 16),
    b: parseInt(value.slice(4, 6), 16),
  };
}

function rgbToHsl({ r, g, b }: Rgb): { h: number; s: number; l: number } {
  const nr = r / 255;
  const ng = g / 255;
  const nb = b / 255;
  const max = Math.max(nr, ng, nb);
  const min = Math.min(nr, ng, nb);
  const l = (max + min) / 2;

  if (max === min) {
    return { h: 210, s: 0, l };
  }

  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h: number;

  switch (max) {
    case nr:
      h = (ng - nb) / d + (ng < nb ? 6 : 0);
      break;
    case ng:
      h = (nb - nr) / d + 2;
      break;
    default:
      h = (nr - ng) / d + 4;
      break;
  }

  return { h: h * 60, s, l };
}

function hslCss(h: number, s: number, l: number): string {
  return `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;
}

/** 文字列を 32bit の決定的ハッシュへ（FNV-1a）。 */
function hashString(input: string): number {
  let hash = 2166136261;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function hslToHex(h: number, s: number, l: number): string {
  const hue = ((h % 360) + 360) % 360;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = hue / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  let r = 0;
  let g = 0;
  let b = 0;
  if (hp < 1) {
    [r, g, b] = [c, x, 0];
  } else if (hp < 2) {
    [r, g, b] = [x, c, 0];
  } else if (hp < 3) {
    [r, g, b] = [0, c, x];
  } else if (hp < 4) {
    [r, g, b] = [0, x, c];
  } else if (hp < 5) {
    [r, g, b] = [x, 0, c];
  } else {
    [r, g, b] = [c, 0, x];
  }
  const m = l - c / 2;
  const toHex = (value: number) =>
    Math.round((value + m) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

/**
 * プロジェクト/サーバー ID から決定的にフォールバック色（hex）を生成する。
 * 同じ ID は常に同じ色。ゴールデンアングルで色相を分散させる。
 * project_color 未設定でもプロジェクトごとに識別可能な色を得るために使う。
 */
export function fallbackColorFromId(
  id: string | null | undefined,
): string | undefined {
  const seed = id?.trim();
  if (!seed) return undefined;
  const hash = hashString(seed);
  const hue = (hash * 137.508) % 360; // golden angle
  return hslToHex(hue, 0.55, 0.55);
}

function fallbackTokens(
  color: string,
  theme: ProjectColorTheme,
): ProjectColorTokens {
  if (theme === "dark") {
    return {
      accent: color,
      surface: `color-mix(in srgb, var(--surface-container) 68%, ${color} 32%)`,
      surfaceAlt: `color-mix(in srgb, var(--surface-container-low) 66%, ${color} 34%)`,
      surfaceHover: `color-mix(in srgb, var(--surface-container-high) 62%, ${color} 38%)`,
      surfaceGradient: `linear-gradient(135deg, color-mix(in srgb, var(--surface-container) 68%, ${color} 32%), color-mix(in srgb, var(--surface-container-low) 66%, ${color} 34%))`,
      border: `color-mix(in srgb, var(--outline) 44%, ${color} 56%)`,
      text: DEFAULT_TEXT_COLOR,
      mutedText: DEFAULT_MUTED_TEXT_COLOR,
      stripe: color,
    };
  }

  return {
    accent: color,
    surface: `color-mix(in srgb, var(--surface-container-lowest) 76%, ${color} 24%)`,
    surfaceAlt: `color-mix(in srgb, var(--surface-container-low) 68%, ${color} 32%)`,
    surfaceHover: `color-mix(in srgb, var(--surface-container) 62%, ${color} 38%)`,
    surfaceGradient: `linear-gradient(135deg, color-mix(in srgb, var(--surface-container-lowest) 76%, ${color} 24%), color-mix(in srgb, var(--surface-container-low) 68%, ${color} 32%))`,
    border: `color-mix(in srgb, var(--outline-variant) 42%, ${color} 58%)`,
    text: DEFAULT_TEXT_COLOR,
    mutedText: DEFAULT_MUTED_TEXT_COLOR,
    stripe: color,
  };
}

export function resolveProjectColorTokens(
  color: string | null | undefined,
  theme: ProjectColorTheme,
  fallbackColor?: string,
): ProjectColorTokens | null {
  const normalizedColor =
    normalizeProjectColor(color) ?? normalizeProjectColor(fallbackColor);
  if (!normalizedColor) return null;

  const parsed = parseHexColor(normalizedColor);
  if (!parsed) return fallbackTokens(normalizedColor, theme);

  const { h, s, l } = rgbToHsl(parsed);
  const usableSaturation = Math.max(s, 0.18);

  if (theme === "dark") {
    // 本文面 --card(#12233d, l≈14%) と十分な明度差をつけて識別できるよう強化。
    const surfaceSaturation = clamp(usableSaturation * 0.64, 0.35, 0.62);
    const accentSaturation = clamp(usableSaturation * 0.92, 0.45, 0.84);

    return {
      accent: hslCss(h, accentSaturation, clamp(Math.max(l, 0.58), 0.58, 0.68)),
      surface: hslCss(h, surfaceSaturation, 0.32),
      surfaceAlt: hslCss(h, clamp(surfaceSaturation * 0.94, 0.32, 0.58), 0.27),
      surfaceHover: hslCss(h, surfaceSaturation, 0.36),
      surfaceGradient: `linear-gradient(135deg, ${hslCss(
        h,
        surfaceSaturation,
        0.33,
      )}, ${hslCss(h, clamp(surfaceSaturation * 0.94, 0.32, 0.58), 0.27)})`,
      border: hslCss(h, clamp(usableSaturation * 0.78, 0.45, 0.72), 0.54),
      text: DEFAULT_TEXT_COLOR,
      mutedText: DEFAULT_MUTED_TEXT_COLOR,
      stripe: hslCss(h, accentSaturation, clamp(Math.max(l, 0.6), 0.6, 0.7)),
    };
  }

  // ライト: 白い本文面から識別できるよう、色味をもう少し乗せる。
  const surfaceSaturation = clamp(usableSaturation * 0.7, 0.38, 0.7);
  const accentSaturation = clamp(usableSaturation * 0.92, 0.44, 0.82);

  return {
    accent: hslCss(h, accentSaturation, clamp(l, 0.34, 0.48)),
    surface: hslCss(h, surfaceSaturation, 0.9),
    surfaceAlt: hslCss(h, clamp(surfaceSaturation * 1.08, 0.42, 0.74), 0.86),
    surfaceHover: hslCss(h, clamp(surfaceSaturation * 1.12, 0.44, 0.76), 0.82),
    surfaceGradient: `linear-gradient(135deg, ${hslCss(
      h,
      surfaceSaturation,
      0.91,
    )}, ${hslCss(h, clamp(surfaceSaturation * 1.08, 0.42, 0.74), 0.86)})`,
    border: hslCss(h, clamp(usableSaturation * 0.58, 0.4, 0.62), 0.62),
    text: DEFAULT_TEXT_COLOR,
    mutedText: DEFAULT_MUTED_TEXT_COLOR,
    stripe: hslCss(h, accentSaturation, clamp(l, 0.34, 0.48)),
  };
}
