"use client";

import {
  createRef,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from "react";
import { AnimatedBeam } from "@/components/magicui/animated-beam";
import { cn } from "@/lib/utils";
import type { AgentTeamSubagent, AgentTeamTeam } from "./llm-model-section-types";

const MAX_VISIBLE_SUBAGENTS = 6;

export type AgentTeamTopologyPreviewProps = {
  team: AgentTeamTeam;
  subagents: Record<string, AgentTeamSubagent>;
};

function TopologyNode({
  children,
  className,
  muted = false,
  nodeRef,
}: {
  children: ReactNode;
  className?: string;
  muted?: boolean;
  nodeRef?: Ref<HTMLDivElement>;
}) {
  return (
    <div
      ref={nodeRef}
      className={cn(
        "relative z-10 min-w-0 rounded-md border px-2 py-1.5 text-[10px] shadow-none",
        muted
          ? "border-border/60 bg-muted/45 text-muted-foreground"
          : "border-primary/30 bg-card text-foreground",
        className,
      )}
      title={typeof children === "string" ? children : undefined}
      aria-disabled={muted || undefined}
    >
      <span className="block truncate">{children}</span>
    </div>
  );
}

/** Read-only Main → Team → Subagent topology.  It only consumes the draft. */
export function AgentTeamTopologyPreview({
  team,
  subagents,
}: AgentTeamTopologyPreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const teamRef = useRef<HTMLDivElement>(null);
  const members = useMemo(
    () =>
      team.subagent_ids
        .map((id) => ({ id, subagent: subagents[id] }))
        .filter(
          (entry): entry is { id: string; subagent: AgentTeamSubagent } =>
            Boolean(entry.subagent),
        ),
    [subagents, team.subagent_ids],
  );
  // Prefer enabled members so an inactive entry cannot consume the six
  // topology slots and hide a live connection. Disabled entries still fill
  // remaining slots as muted context when room is available.
  const visibleMembers = [
    ...members.filter(({ subagent }) => subagent.enabled),
    ...members.filter(({ subagent }) => !subagent.enabled),
  ].slice(0, MAX_VISIBLE_SUBAGENTS);
  const hiddenMemberCount = Math.max(0, members.length - visibleMembers.length);
  const canAnimateTeam = team.enabled;
  // A fixed pool keeps refs stable without reading ref.current during render;
  // member order is capped at six and is sufficient for the read-only preview.
  const [memberRefs] = useState(() =>
    Array.from({ length: MAX_VISIBLE_SUBAGENTS }, () =>
      createRef<HTMLDivElement>(),
    ),
  );

  const beamProps = {
    className: "z-0",
    containerRef,
    curvature: 0,
    pathColor: "var(--border)",
    pathWidth: 1,
    pathOpacity: 0.55,
    gradientStartColor: "var(--primary)",
    gradientStopColor: "var(--chart-2)",
    duration: 5,
    repeat: Infinity,
  } as const;

  return (
    <section
      className="space-y-2 rounded border border-border bg-background/35 p-2"
      aria-label="Agent Team topology preview"
      data-testid="agent-team-topology-preview"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="text-[10px] font-medium">Topology preview</div>
        <span className="text-[9px] text-muted-foreground">Read-only</span>
      </div>
      <div
        ref={containerRef}
        className="relative grid min-h-[144px] min-w-0 grid-cols-[minmax(44px,0.7fr)_minmax(60px,1fr)_minmax(0,1.4fr)] items-center gap-2 overflow-hidden rounded-md border border-border/70 bg-muted/20 p-2 sm:gap-3"
        data-topology-active={canAnimateTeam ? "true" : "false"}
      >
        <div className="min-w-0">
          <TopologyNode nodeRef={mainRef} className="text-center">
            Main
          </TopologyNode>
        </div>
        <div className="min-w-0">
          <TopologyNode
            nodeRef={teamRef}
            className="text-center"
            muted={!team.enabled}
          >
            {team.name || "名称未設定"}
          </TopologyNode>
        </div>
        <div className="grid min-w-0 grid-cols-1 gap-1 sm:grid-cols-2">
          {visibleMembers.map(({ id, subagent }, index) => (
            <TopologyNode
              key={id}
              nodeRef={memberRefs[index]}
              muted={!subagent.enabled}
            >
              {subagent.name || "名称未設定"}
            </TopologyNode>
          ))}
          {hiddenMemberCount > 0 && (
            <TopologyNode className="text-center text-muted-foreground">
              +{hiddenMemberCount}
            </TopologyNode>
          )}
          {!visibleMembers.length && !hiddenMemberCount && (
            <div className="col-span-full rounded border border-dashed border-border/70 px-2 py-1.5 text-[10px] text-muted-foreground">
              Subagentなし
            </div>
          )}
        </div>

        {canAnimateTeam && (
          <span
            aria-hidden="true"
            className="pointer-events-none contents"
            data-testid="agent-team-topology-beam"
          >
            <AnimatedBeam fromRef={mainRef} toRef={teamRef} {...beamProps} />
          </span>
        )}
        {canAnimateTeam &&
          visibleMembers
            .map(({ id, subagent }, index) => ({ id, subagent, index }))
            .filter(({ subagent }) => subagent.enabled)
            .map(({ id, index }) => (
              <span
                key={`beam-${id}`}
                aria-hidden="true"
                className="pointer-events-none contents"
                data-testid="agent-team-topology-beam"
              >
                <AnimatedBeam
                  fromRef={teamRef}
                  toRef={memberRefs[index]}
                  {...beamProps}
                />
              </span>
            ))}
      </div>
    </section>
  );
}
