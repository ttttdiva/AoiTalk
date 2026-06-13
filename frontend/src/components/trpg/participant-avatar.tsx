"use client";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { participantInitials, type Participant } from "@/lib/trpg-room-utils";

function ParticipantAvatar({
  participant,
  name,
  avatarUrl,
  size = "default",
}: {
  participant?: Participant | null;
  name?: string;
  avatarUrl?: string;
  size?: "sm" | "default" | "lg";
}) {
  const displayName = name || participant?.display_name || "";
  const url = avatarUrl || participant?.avatar_url || "";
  const color = participant?.color || "#64748b";
  return (
    <Avatar size={size}>
      {url ? <AvatarImage src={url} alt={displayName || "participant avatar"} /> : null}
      <AvatarFallback
        className="font-semibold text-white"
        style={{ backgroundColor: color }}
      >
        {participantInitials(displayName)}
      </AvatarFallback>
    </Avatar>
  );
}

export { ParticipantAvatar };
