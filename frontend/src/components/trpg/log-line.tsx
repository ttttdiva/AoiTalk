"use client";

/* eslint-disable @next/next/no-img-element */

import { Dices } from "lucide-react";
import { ParticipantAvatar } from "@/components/trpg/participant-avatar";
import {
  generatedImageSrc,
  type Participant,
  type PlayLog,
} from "@/lib/trpg-room-utils";

// ─── ログ表示 ───

function LogLine({
  log,
  participants,
  myParticipantId,
}: {
  log: PlayLog;
  participants: Participant[];
  myParticipantId: string;
}) {
  const speaker = participants.find((p) => p.id === log.participant_id);
  const isMine = log.participant_id && log.participant_id === myParticipantId;

  if (log.log_type === "narration") {
    return (
      <div className="rounded-lg border border-amber-400/40 bg-amber-50/30 p-3 dark:bg-amber-950/20">
        <div className="mb-1 text-[10px] uppercase tracking-wide text-amber-700 dark:text-amber-400">
          Game Master
        </div>
        <div className="whitespace-pre-wrap text-sm leading-relaxed">
          {log.content}
        </div>
      </div>
    );
  }
  if (log.log_type === "dice") {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Dices className="h-4 w-4 text-sky-500" />
        <span>{log.content}</span>
      </div>
    );
  }
  if (log.log_type === "scene_change") {
    return (
      <div className="my-2 border-y border-dashed border-primary/40 py-1 text-center text-xs font-semibold uppercase text-primary">
        {log.content}
      </div>
    );
  }
  if (log.log_type === "system") {
    return (
      <div className="text-xs text-muted-foreground italic">
        {log.content}
      </div>
    );
  }
  if (log.log_type === "image") {
    const imagePath =
      typeof log.metadata?.path === "string" ? log.metadata.path : "";
    const imageSrc = generatedImageSrc(imagePath);
    return (
      <div className="rounded border bg-muted/20 p-2 text-xs">
        {imageSrc ? (
          <img
            src={imageSrc}
            alt={log.content || "Generated scene"}
            className="mb-2 max-h-[420px] w-full rounded object-contain"
            loading="lazy"
          />
        ) : null}
        <div className="text-muted-foreground">画像: {log.content}</div>
      </div>
    );
  }
  if (log.log_type === "bgm") {
    return (
      <div className="text-xs text-muted-foreground">
        ♪ {log.content}
      </div>
    );
  }

  // action / speech / ooc
  const color = speaker?.color || "#888";
  return (
    <div
      className={`flex gap-2 rounded p-2 text-sm ${
        isMine ? "bg-primary/10" : "bg-muted/30"
      }`}
      style={{ borderLeft: `3px solid ${color}` }}
    >
      <ParticipantAvatar participant={speaker} name={speaker?.display_name ?? "誰か"} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold" style={{ color }}>
          {speaker?.display_name ?? "誰か"}
          {log.log_type === "ooc" && " (OOC)"}
        </div>
        <div className="whitespace-pre-wrap">{log.content}</div>
      </div>
    </div>
  );
}

export { LogLine };
