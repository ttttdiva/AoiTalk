import type { AppSummary } from "@/lib/apps-api";

/**
 * The fields that are available in both the Apps list and the detail page.
 *
 * App targets deliberately are not part of this contract.  An App's identity
 * must not change merely because a detail page has finished loading its
 * target/context data.
 */
export type AppVisualIdentityInput = Pick<AppSummary, "id" | "name" | "slug" | "description">;

export type AppVisualIdentityKind = "generic" | "spreadsheet";

export type AppVisualIdentityPaletteKey =
  | "emerald"
  | "rose"
  | "blue"
  | "green"
  | "amber"
  | "purple"
  | "cyan";

export interface AppVisualIdentity {
  kind: AppVisualIdentityKind;
  paletteKey: AppVisualIdentityPaletteKey;
  /** Styles used by the compact/sidebar representation. */
  compactClass: string;
  /** Styles used by the larger detail-header representation. */
  detailClass: string;
}

type AppVisualIdentityPalette = Pick<AppVisualIdentity, "paletteKey" | "compactClass" | "detailClass">;

// Keep these class names as literals.  Constructing `bg-${color}-...` at
// runtime would make the classes invisible to Tailwind's production scanner.
const APP_VISUAL_IDENTITY_PALETTES: readonly AppVisualIdentityPalette[] = [
  {
    paletteKey: "emerald",
    compactClass: "bg-emerald-500/15 text-emerald-400",
    detailClass: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  },
  {
    paletteKey: "rose",
    compactClass: "bg-rose-500/15 text-rose-400",
    detailClass: "border-rose-500/40 bg-rose-500/10 text-rose-400",
  },
  {
    paletteKey: "blue",
    compactClass: "bg-blue-500/15 text-blue-400",
    detailClass: "border-blue-500/40 bg-blue-500/10 text-blue-400",
  },
  {
    paletteKey: "green",
    compactClass: "bg-green-500/15 text-green-400",
    detailClass: "border-green-500/40 bg-green-500/10 text-green-400",
  },
  {
    paletteKey: "amber",
    compactClass: "bg-amber-500/15 text-amber-400",
    detailClass: "border-amber-500/40 bg-amber-500/10 text-amber-400",
  },
  {
    paletteKey: "purple",
    compactClass: "bg-purple-500/15 text-purple-400",
    detailClass: "border-purple-500/40 bg-purple-500/10 text-purple-400",
  },
  {
    paletteKey: "cyan",
    compactClass: "bg-cyan-500/15 text-cyan-400",
    detailClass: "border-cyan-500/40 bg-cyan-500/10 text-cyan-400",
  },
] as const;

/**
 * One intentionally broad classifier shared by every App identity surface.
 * It only uses AppSummary fields that are available in list and detail data.
 */
// Use explicit spreadsheet terminology only.  Generic words such as
// "file", "request", or "conversion" are common in non-spreadsheet Apps;
// target metadata (office/VBA) is intentionally excluded from this shared
// contract because it is not present on embedded AppSummary values.  ASCII
// terms use token boundaries so `calc` does not match `calculator` and `ods`
// does not match the end of `methods`.
const SPREADSHEET_ASCII_TOKEN_PATTERN =
  /(?:^|[^a-z0-9])(?:csv|tsv|xls(?:x|m|b)?|excel|spreadsheet|ods|numbers|calc)(?:$|[^a-z0-9])/i;
const SPREADSHEET_JAPANESE_PATTERN = /表計算|スプレッドシート/;

export function isSpreadsheetApp(app: AppVisualIdentityInput): boolean {
  const metadata = `${app.name} ${app.slug} ${app.description || ""}`
    .normalize("NFKC")
    .toLowerCase();
  return (
    SPREADSHEET_ASCII_TOKEN_PATTERN.test(metadata) ||
    SPREADSHEET_JAPANESE_PATTERN.test(metadata)
  );
}

/**
 * A small deterministic FNV-1a hash.  Palette selection depends only on the
 * immutable App id, never on list order, active state, or target/context data.
 */
function hashAppId(appId: string): number {
  let hash = 2166136261;
  const normalized = appId.normalize("NFKC").trim().toLowerCase();
  for (let index = 0; index < normalized.length; index += 1) {
    hash ^= normalized.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

export function getAppVisualIdentity(app: AppVisualIdentityInput): AppVisualIdentity {
  const palette = APP_VISUAL_IDENTITY_PALETTES[hashAppId(app.id) % APP_VISUAL_IDENTITY_PALETTES.length];
  return {
    kind: isSpreadsheetApp(app) ? "spreadsheet" : "generic",
    paletteKey: palette.paletteKey,
    compactClass: palette.compactClass,
    detailClass: palette.detailClass,
  };
}

/** Alias suitable for call sites that use resolver terminology. */
export const resolveAppVisualIdentity = getAppVisualIdentity;

export { APP_VISUAL_IDENTITY_PALETTES };
