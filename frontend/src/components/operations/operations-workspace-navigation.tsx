"use client";

import Link from "next/link";
import {
  Activity,
  BookOpen,
  CalendarDays,
  CheckCircle2,
  CircleDollarSign,
  LayoutDashboard,
  ListChecks,
  PlugZap,
  Settings2,
  UserRound,
} from "lucide-react";

import { HelpPopover } from "@/components/ui/help-popover";

export type OperationsSection =
  | "studio"
  | "overview"
  | "agents"
  | "work"
  | "activity"
  | "personas"
  | "calendar"
  | "research"
  | "connections"
  | "opportunities"
  | "actions";

export const OPERATIONS_SECTION_IDS: ReadonlyArray<OperationsSection> = [
  "studio",
  "overview",
  "agents",
  "work",
  "activity",
  "personas",
  "research",
  "calendar",
  "connections",
  "opportunities",
  "actions",
];

/** Removed tabs whose work now happens inside a persona's production stages. */
const STUDIO_ALIASES = ["generation", "pipeline"];

/** Existing graph deep links retain their view; a bare entry opens persona production. */
export function operationsSectionFromQuery(query: Pick<URLSearchParams, "get" | "has"> | null): OperationsSection {
  const requested = query?.get("tab") as OperationsSection | null;
  // Persona settings always belong to one persona; without it the persona list (studio) opens.
  if (requested === "personas" && !query?.get("persona")) return "studio";
  if (requested && OPERATIONS_SECTION_IDS.includes(requested)) return requested;
  if (requested && STUDIO_ALIASES.includes(requested)) return "studio";
  if (!requested && ["graph_node", "graph_work", "execution_focus", "execution_node", "work_view"].some(key => query?.has(key))) return "overview";
  return "studio";
}

/** Preserve existing Map/execution deep links without changing the bare Studio entry. */
export function usesOperationsMap(query: Pick<URLSearchParams, "get" | "has"> | null): boolean {
  return query?.get("overview_view") === "map" ||
    ["graph_node", "graph_work", "execution_focus", "execution_node", "work_view"].some(key => query?.has(key));
}

type OperationsSectionDefinition = {
  id: OperationsSection;
  label: string;
  description: string;
  icon: typeof PlugZap;
};

const MEDIA_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  { id: "studio", label: "人格", description: "人格ごとの本人接続・次にやること・制作物と制作工程", icon: UserRound },
  { id: "research", label: "調査", description: "Research routine・Finding・候補", icon: BookOpen },
  { id: "calendar", label: "予定", description: "調査・生成・公開・レビューの予定", icon: CalendarDays },
];

const COMPANY_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  { id: "overview", label: "Overview", description: "会社構造と稼働状況", icon: LayoutDashboard },
  { id: "agents", label: "Agent", description: "Agent・役割・所属", icon: UserRound },
  { id: "work", label: "Work", description: "Agentが処理している仕事", icon: ListChecks },
  { id: "activity", label: "Activity", description: "実行・判断の履歴", icon: Activity },
];

const ENGAGEMENT_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  { id: "connections", label: "Connections", description: "CrowdWorks等の外部サービス", icon: PlugZap },
  { id: "opportunities", label: "Opportunities", description: "見つけた外部案件を判断", icon: CircleDollarSign },
  { id: "actions", label: "承認・Action", description: "応募等の承認・外部操作", icon: CheckCircle2 },
];

/** One persona's settings, opened from that persona's 人格 view (not listed in the navigation). */
const PERSONA_SETTINGS_SECTION: OperationsSectionDefinition = { id: "personas", label: "人格の設定", description: "人格の設定・参照画像・接続枠", icon: Settings2 };

/** All addressable Operations areas. */
export const OPERATIONS_ALL_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  ...MEDIA_SECTIONS,
  PERSONA_SETTINGS_SECTION,
  ...COMPANY_SECTIONS,
  ...ENGAGEMENT_SECTIONS,
];

function operationsHref(section: OperationsSection): string {
  return section === "studio"
    ? "/operations"
    : `/operations?tab=${encodeURIComponent(section)}`;
}

export function OperationsWorkspaceNavigation({
  activeSection,
  onSelect,
  companyRuntimeEnabled = true,
}: {
  activeSection: OperationsSection;
  onSelect?: (section: OperationsSection) => void;
  /** Hide company-autonomous Work links when the feature is disabled. */
  companyRuntimeEnabled?: boolean;
}) {
  const groups = [
    { label: "発信", items: MEDIA_SECTIONS },
    { label: "会社・Agent", items: COMPANY_SECTIONS.filter((item) => companyRuntimeEnabled || item.id !== "work") },
    { label: "外部案件", items: ENGAGEMENT_SECTIONS },
  ];

  return (
    <nav
      aria-label="Operationsワークスペース"
      data-operations-navigation="true"
      className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-2 py-3"
    >
      {groups.map(({ label: groupLabel, items }) => (
        <div key={groupLabel} className="flex flex-col gap-1">
          <p className="px-2.5 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground first:pt-0">
            {groupLabel}
          </p>
          {items.map(({ id, label, description, icon: Icon }) => {
            const active = activeSection === id || (activeSection === "personas" && id === "studio");
            return (
              <div key={id} className="flex min-w-0 items-center gap-1">
                <Link
                  href={operationsHref(id)}
                  aria-current={active ? "page" : undefined}
                  data-operations-section={id}
                  onClick={(event) => {
                    if (!onSelect) return;
                    event.preventDefault();
                    onSelect(id);
                  }}
                  className={`group relative flex min-h-9 flex-1 items-center gap-2 rounded-[4px] border-l-2 px-2.5 py-2 text-left text-[13px] leading-4 transition-colors ${
                    active
                      ? "border-primary bg-muted/60 text-foreground"
                      : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/40 hover:text-foreground"
                  }`}
                >
                  <Icon
                    className={`size-4 shrink-0 ${active ? "text-primary" : "text-muted-foreground group-hover:text-foreground"}`}
                    aria-hidden="true"
                  />
                  <span className="min-w-0 truncate">{label}</span>
                </Link>
                <HelpPopover label={label}>{description}</HelpPopover>
              </div>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
