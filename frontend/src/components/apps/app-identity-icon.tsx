import { Code2, FileSpreadsheet, type LucideProps } from "lucide-react";
import type { ForwardRefExoticComponent, RefAttributes } from "react";
import {
  getAppVisualIdentity,
  type AppVisualIdentityInput,
  type AppVisualIdentityKind,
} from "@/lib/app-visual-identity";

export type AppIdentityIconVariant = "compact" | "detail";

export interface AppIdentityIconProps extends LucideProps {
  app: AppVisualIdentityInput;
  variant?: AppIdentityIconVariant;
}

const ICONS: Record<AppVisualIdentityKind, ForwardRefExoticComponent<LucideProps & RefAttributes<SVGSVGElement>>> = {
  generic: Code2,
  spreadsheet: FileSpreadsheet,
};

/**
 * Render an App's semantic identity glyph.  Palette and icon kind are exposed
 * as data attributes so the sidebar/detail contract can be regression-tested
 * without relying on Lucide's implementation details.
 */
export function AppIdentityIcon({ app, variant = "compact", className, ...props }: AppIdentityIconProps) {
  const identity = getAppVisualIdentity(app);
  const Icon = ICONS[identity.kind];

  return (
    <Icon
      {...props}
      className={className}
      data-app-identity-kind={identity.kind}
      data-app-identity-palette={identity.paletteKey}
      data-app-identity-palette-key={identity.paletteKey}
      data-app-identity-icon={identity.kind === "spreadsheet" ? "FileSpreadsheet" : "Code2"}
      data-app-identity-variant={variant}
    />
  );
}
