"use client";

import Link from "next/link";
import {
  Activity,
  BarChart3,
  BookOpen,
  Bot,
  CalendarDays,
  CheckCircle2,
  CircleDollarSign,
  Images,
  LayoutDashboard,
  ListChecks,
  PlugZap,
  UserRound,
  Workflow,
} from "lucide-react";

import { HelpPopover } from "@/components/ui/help-popover";

export type OperationsSection =
  | "studio"
  | "overview"
  | "agents"
  | "work"
  | "activity"
  | "personas"
  | "pipeline"
  | "calendar"
  | "results"
  | "research"
  | "automation"
  | "generation"
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
  "automation",
  "generation",
  "pipeline",
  "calendar",
  "results",
  "connections",
  "opportunities",
  "actions",
];

/** Existing graph deep links retain their view; a bare entry opens persona production. */
export function operationsSectionFromQuery(query: Pick<URLSearchParams, "get" | "has"> | null): OperationsSection {
  const requested = query?.get("tab") as OperationsSection | null;
  if (requested && OPERATIONS_SECTION_IDS.includes(requested)) return requested;
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

export const COMMAND_CENTER_SECTIONS: ReadonlyArray<OperationsSectionDefinition> =
  [
    {
      id: "overview",
      label: "Overview",
      description: "会社構造と稼働状況",
      icon: LayoutDashboard,
    },
    {
      id: "agents",
      label: "Agent",
      description: "Agent・役割・所属",
      icon: UserRound,
    },
    {
      id: "work",
      label: "Work",
      description: "Agentが処理している仕事",
      icon: ListChecks,
    },
    {
      id: "activity",
      label: "Activity",
      description: "実行・判断の履歴",
      icon: Activity,
    },
  ];

export const MEDIA_OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> =
  [
    { id: "studio", label: "人格workspace", description: "人格から制作・preview・公開", icon: UserRound },
    {
      id: "personas",
      label: "Personas",
      description: "発信人格を管理",
      icon: UserRound,
    },
    {
      id: "research",
      label: "Research",
      description: "調査とEvidence",
      icon: BookOpen,
    },
    {
      id: "automation",
      label: "Automation",
      description: "Theme→Research→生成を自動化",
      icon: Bot,
    },
    {
      id: "generation",
      label: "Generation",
      description: "画像生成とProvenance",
      icon: Images,
    },
    {
      id: "pipeline",
      label: "パイプライン",
      description: "ContentVariantとQA / Rights",
      icon: Workflow,
    },
    {
      id: "calendar",
      label: "カレンダー",
      description: "調査・生成・公開・レビュー",
      icon: CalendarDays,
    },
    {
      id: "results",
      label: "結果",
      description: "Metrics・Revenue・Experiment",
      icon: BarChart3,
    },
  ];

export const ENGAGEMENT_OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> =
  [
    {
      id: "connections",
      label: "Connections",
      description: "CrowdWorks等の外部サービス",
      icon: PlugZap,
    },
    {
      id: "opportunities",
      label: "Opportunities",
      description: "見つけた外部案件を判断",
      icon: CircleDollarSign,
    },
  ];

export const GOVERNANCE_OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> =
  [
    {
      id: "actions",
      label: "Approvals / Actions",
      description: "応募等の承認・外部操作",
      icon: CheckCircle2,
    },
  ];

/** All addressable Operations areas. */
export const OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  MEDIA_OPERATIONS_SECTIONS[0],
  ...COMMAND_CENTER_SECTIONS,
  ...MEDIA_OPERATIONS_SECTIONS.slice(1),
  ...ENGAGEMENT_OPERATIONS_SECTIONS,
  ...GOVERNANCE_OPERATIONS_SECTIONS,
];

/** Backwards-compatible alias for callers that include detail links. */
export const OPERATIONS_DETAIL_SECTIONS: ReadonlyArray<OperationsSectionDefinition> =
  [];

export const OPERATIONS_ALL_SECTIONS = [
  ...OPERATIONS_SECTIONS,
  ...OPERATIONS_DETAIL_SECTIONS,
] as const;

function operationsHref(section: OperationsSection): string {
  return section === "studio"
    ? "/operations"
    : `/operations?tab=${encodeURIComponent(section)}`;
}

export function OperationsWorkspaceNavigation({
  activeSection,
  onSelect,
  compact = false,
  companyRuntimeEnabled = true,
}: {
  activeSection: OperationsSection;
  onSelect?: (section: OperationsSection) => void;
  compact?: boolean;
  /** Hide company-autonomous Agent/Work links when the feature is disabled. */
  companyRuntimeEnabled?: boolean;
}) {
  const groups = [
    {
      label: "Command Center — 会社 / Agent",
      items: COMMAND_CENTER_SECTIONS.filter(
        (item) => companyRuntimeEnabled || item.id !== "work",
      ),
    },
    {
      label: "Media Operations — 発信 / 制作",
      items: MEDIA_OPERATIONS_SECTIONS,
    },
    {
      label: "Engagement Operations — 外部案件",
      items: ENGAGEMENT_OPERATIONS_SECTIONS,
    },
    {
      label: "Governance — 承認 / 外部操作",
      items: GOVERNANCE_OPERATIONS_SECTIONS,
    },
  ];

  if (compact) {
    const renderLink = ({
      id,
      label,
      description,
      icon: Icon,
    }: OperationsSectionDefinition) => (
      <div key={id} className="inline-flex min-w-0 items-center gap-1">
        <Link
          href={operationsHref(id)}
          aria-current={activeSection === id ? "page" : undefined}
          data-operations-section={id}
          onClick={(event) => {
            if (!onSelect) return;
            event.preventDefault();
            onSelect(id);
          }}
          className={`inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2 py-1.5 text-xs ${activeSection === id ? "border-primary/40 bg-primary/10 font-medium text-primary" : "border-transparent text-muted-foreground hover:bg-muted/50"}`}
        >
          <Icon className="size-3.5 shrink-0" aria-hidden="true" />
          <span>{label}</span>
        </Link>
        <HelpPopover label={label}>{description}</HelpPopover>
      </div>
    );
    return (
      <nav
        aria-label="Operationsワークスペース"
        data-operations-navigation="true"
        className="flex min-w-0 flex-wrap items-start gap-1"
      >
        {groups[0].items.map(renderLink)}
        {GOVERNANCE_OPERATIONS_SECTIONS.map(renderLink)}
        {[
          { label: "発信・制作", items: MEDIA_OPERATIONS_SECTIONS },
          { label: "外部案件", items: ENGAGEMENT_OPERATIONS_SECTIONS },
        ].map(({ label, items }) => (
          <details
            key={label}
            className="min-w-0 rounded-md border border-border/70 open:basis-full"
            open={items.some((item) => item.id === activeSection) || undefined}
          >
            <summary className="min-h-8 cursor-pointer px-2 py-1.5 text-xs text-muted-foreground">
              {label}
              {items.some((item) => item.id === activeSection)
                ? ` · ${items.find((item) => item.id === activeSection)?.label}`
                : ""}
            </summary>
            <div className="flex flex-wrap gap-1 border-t border-border/60 p-2">
              {items.map(renderLink)}
            </div>
          </details>
        ))}
      </nav>
    );
  }

  return (
    <nav
      aria-label="Operationsワークスペース"
      data-operations-navigation="true"
      className={
        compact
          ? "flex min-w-0 gap-1 overflow-x-auto"
          : "flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-2 py-3"
      }
    >
      {groups.map(({ label: groupLabel, items }) => (
        <div
          key={groupLabel}
          className={compact ? "contents" : "flex flex-col gap-1"}
        >
          {!compact ? (
            <p className="px-2.5 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground first:pt-0">
              {groupLabel}
            </p>
          ) : null}

          {items.map(({ id, label, description, icon: Icon }) => {
            const active = activeSection === id;

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
                className={
                  compact
                    ? `inline-flex shrink-0 items-center gap-1.5 rounded-[4px] border px-2.5 py-1.5 text-xs ${
                        active
                          ? "border-primary/50 bg-primary/10 text-primary"
                          : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/50"
                      }`
                    : `group relative flex min-h-9 flex-1 items-center gap-2 rounded-[4px] border-l-2 px-2.5 py-2 text-left text-[13px] leading-4 transition-colors ${
                        active
                          ? "border-primary bg-muted/60 text-foreground"
                          : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/40 hover:text-foreground"
                      }`
                }
              >
                <Icon
                  className={
                    compact
                      ? "size-3.5 shrink-0"
                      : `size-4 shrink-0 ${
                          active
                            ? "text-primary"
                            : "text-muted-foreground group-hover:text-foreground"
                        }`
                  }
                  aria-hidden="true"
                />

                <span className="min-w-0 truncate">
                  <span className="truncate">{label}</span>
                </span>
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
