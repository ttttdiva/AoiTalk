"""Prepare sampled visual frames from a verified, pinned video input.

Sampling does not establish audio or full-motion review or producer authorship.
"""
import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from src.services.agent_authority import AgentAuthorityResolver
from src.services.agent_delegation_media_inputs import MediaInputPin, resolve_video_input_for_reviewer
from src.services.agent_media_review_frames import extract_video_review_frames
from src.services.media_asset_service import MediaAssetService


@dataclass(frozen=True, slots=True)
class DerivedVideoFrame:
    data: bytes = field(repr=False)
    mime_type: str
    source_pin: MediaInputPin
    source_video_sha256: str
    frame_sha256: str
    timestamp_seconds: float

    def provenance(self) -> dict[str, Any]:
        """Return metadata for the original video and this sampled frame."""
        return {
            "source_pin": {key: str(value) for key, value in asdict(self.source_pin).items()},
            "source_video_sha256": self.source_video_sha256,
            "frame_sha256": self.frame_sha256,
            "mime_type": self.mime_type,
            "timestamp_seconds": self.timestamp_seconds,
            "scope": "sampled_visual_only",
        }


async def resolve_video_review_frames(
    session: Any, *, owner_user_id: UUID, project_id: UUID, persona_id: UUID,
    reviewer_agent_id: UUID, reviewer_revision_id: UUID, pin: MediaInputPin,
    media_service: MediaAssetService | None = None,
    authority_resolver: AgentAuthorityResolver | None = None,
    max_frames: int = 3,
) -> tuple[DerivedVideoFrame, ...]:
    """Resolve the original asset first, then derive bounded JPEG samples."""
    source = await resolve_video_input_for_reviewer(
        session, owner_user_id=owner_user_id, project_id=project_id, persona_id=persona_id,
        reviewer_agent_id=reviewer_agent_id, reviewer_revision_id=reviewer_revision_id,
        pin=pin, media_service=media_service, authority_resolver=authority_resolver,
    )
    frames = await asyncio.to_thread(
        extract_video_review_frames, source.data, expected_sha256=source.pin.bytes_sha256,
        mime_type=source.mime_type, max_frames=max_frames,
    )
    return tuple(DerivedVideoFrame(
        data=frame.data, mime_type=frame.mime_type, source_pin=source.pin,
        source_video_sha256=frame.source_sha256, frame_sha256=frame.frame_sha256,
        timestamp_seconds=frame.timestamp_seconds,
    ) for frame in frames)
