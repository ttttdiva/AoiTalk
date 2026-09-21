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

export type OperationsSection =
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

type OperationsSectionDefinition = {
  id: OperationsSection;
  label: string;
  description: string;
  icon: typeof PlugZap;
};

export const COMMAND_CENTER_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  {
    id: "overview",
    label: "Overview",
    description: "会社全体の状態",
    icon: LayoutDashboard,
  },
  {
    id: "agents",
    label: "AI社員",
    description: "職務・所属・自動化を管理",
    icon: UserRound,
  },
  {
    id: "work",
    label: "Work",
    description: "実行中のWorkItem",
    icon: ListChecks,
  },
  {
    id: "activity",
    label: "Activity",
    description: "監査可能な活動履歴",
    icon: Activity,
  },
];

export const MEDIA_OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
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

export const ENGAGEMENT_OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  {
    id: "connections",
    label: "Connections",
    description: "接続先を管理",
    icon: PlugZap,
  },
  {
    id: "opportunities",
    label: "Opportunities",
    description: "案件を取り込む",
    icon: CircleDollarSign,
  },
];

export const GOVERNANCE_OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  {
    id: "actions",
    label: "Approvals / Actions",
    description: "承認と実行履歴",
    icon: CheckCircle2,
  },
];

/** All addressable Operations areas. */
export const OPERATIONS_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [
  ...COMMAND_CENTER_SECTIONS,
  ...MEDIA_OPERATIONS_SECTIONS,
  ...ENGAGEMENT_OPERATIONS_SECTIONS,
  ...GOVERNANCE_OPERATIONS_SECTIONS,
];

/** Backwards-compatible alias for callers that include detail links. */
export const OPERATIONS_DETAIL_SECTIONS: ReadonlyArray<OperationsSectionDefinition> = [];

export const OPERATIONS_ALL_SECTIONS = [
  ...OPERATIONS_SECTIONS,
  ...OPERATIONS_DETAIL_SECTIONS,
] as const;

function operationsHref(section: OperationsSection): string {
  return section === "overview"
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
      label: "Command Center",
      items: COMMAND_CENTER_SECTIONS.filter(
        (item) => companyRuntimeEnabled || item.id !== "work",
      ),
    },
    {
      label: "SNS / MediaOps",
      items: MEDIA_OPERATIONS_SECTIONS,
    },
    {
      label: "案件 / EngagementOps",
      items: ENGAGEMENT_OPERATIONS_SECTIONS,
    },
    {
      label: "Governance",
      items: GOVERNANCE_OPERATIONS_SECTIONS,
    },
  ];

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
              <Link
                key={id}
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
                    : `group relative flex min-h-9 items-center gap-2 rounded-[4px] border-l-2 px-2.5 py-2 text-left text-[13px] leading-4 transition-colors ${
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
                  <span className="block truncate">{label}</span>

                  {!compact ? (
                    <span className="block truncate text-[10px] leading-4 text-muted-foreground">
                      {description}
                    </span>
                  ) : null}
                </span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
