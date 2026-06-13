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

function fallbackTokens(
  color: string,
  theme: ProjectColorTheme,
): ProjectColorTokens {
  if (theme === "dark") {
    return {
      accent: color,
      surface: `color-mix(in srgb, #1a2233 68%, ${color} 32%)`,
      surfaceAlt: `color-mix(in srgb, #111827 66%, ${color} 34%)`,
      surfaceHover: `color-mix(in srgb, #202a3d 62%, ${color} 38%)`,
      surfaceGradient: `linear-gradient(135deg, color-mix(in srgb, #1a2233 68%, ${color} 32%), color-mix(in srgb, #111827 66%, ${color} 34%))`,
      border: `color-mix(in srgb, #94a3b8 44%, ${color} 56%)`,
      text: DEFAULT_TEXT_COLOR,
      mutedText: DEFAULT_MUTED_TEXT_COLOR,
      stripe: color,
    };
  }

  return {
    accent: color,
    surface: `color-mix(in srgb, white 76%, ${color} 24%)`,
    surfaceAlt: `color-mix(in srgb, white 68%, ${color} 32%)`,
    surfaceHover: `color-mix(in srgb, white 62%, ${color} 38%)`,
    surfaceGradient: `linear-gradient(135deg, color-mix(in srgb, white 76%, ${color} 24%), color-mix(in srgb, white 68%, ${color} 32%))`,
    border: `color-mix(in srgb, white 42%, ${color} 58%)`,
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
    const surfaceSaturation = clamp(usableSaturation * 0.58, 0.26, 0.6);
    const accentSaturation = clamp(usableSaturation * 0.92, 0.45, 0.84);

    return {
      accent: hslCss(h, accentSaturation, clamp(Math.max(l, 0.58), 0.58, 0.68)),
      surface: hslCss(h, surfaceSaturation, 0.26),
      surfaceAlt: hslCss(h, clamp(surfaceSaturation * 0.92, 0.24, 0.56), 0.21),
      surfaceHover: hslCss(h, surfaceSaturation, 0.3),
      surfaceGradient: `linear-gradient(135deg, ${hslCss(
        h,
        surfaceSaturation,
        0.27,
      )}, ${hslCss(h, clamp(surfaceSaturation * 0.92, 0.24, 0.56), 0.21)})`,
      border: hslCss(h, clamp(usableSaturation * 0.72, 0.36, 0.68), 0.48),
      text: DEFAULT_TEXT_COLOR,
      mutedText: DEFAULT_MUTED_TEXT_COLOR,
      stripe: hslCss(h, accentSaturation, clamp(Math.max(l, 0.6), 0.6, 0.7)),
    };
  }

  const surfaceSaturation = clamp(usableSaturation * 0.62, 0.24, 0.66);
  const accentSaturation = clamp(usableSaturation * 0.92, 0.44, 0.82);

  return {
    accent: hslCss(h, accentSaturation, clamp(l, 0.34, 0.48)),
    surface: hslCss(h, surfaceSaturation, 0.92),
    surfaceAlt: hslCss(h, clamp(surfaceSaturation * 1.08, 0.28, 0.72), 0.88),
    surfaceHover: hslCss(h, clamp(surfaceSaturation * 1.12, 0.3, 0.74), 0.84),
    surfaceGradient: `linear-gradient(135deg, ${hslCss(
      h,
      surfaceSaturation,
      0.93,
    )}, ${hslCss(h, clamp(surfaceSaturation * 1.08, 0.28, 0.72), 0.88)})`,
    border: hslCss(h, clamp(usableSaturation * 0.5, 0.24, 0.56), 0.68),
    text: DEFAULT_TEXT_COLOR,
    mutedText: DEFAULT_MUTED_TEXT_COLOR,
    stripe: hslCss(h, accentSaturation, clamp(l, 0.34, 0.48)),
  };
}
