"""Resolve a pinned managed image for a validated delegation reviewer.

The caller authenticates the owner and authorizes the project or order; this
boundary rechecks reviewer Agent authority and reads only the scoped verified asset.
The Work/AgentRun association is plan-based, not proof of which attempt submitted
the generation. Callers must not treat it as a verified producer or adoption proof.
"""
from dataclasses import dataclass, asdict
from hashlib import sha256
from typing import Any
from uuid import UUID

from src.memory.models.agent_runs import AgentRun
from src.memory.models.agent_work import AgentWorkItem
from src.memory.models.media_operations import PersonaRevision
from src.memory.models.media_operations_generation import GenerationOutput, GenerationPlan, GenerationRun
from src.services.agent_authority import AgentAuthorityResolver
from src.services.media_asset_service import IMAGE_MAX_BYTES, VIDEO_MAX_BYTES, MIME_EXTENSIONS, MediaAssetService


class MediaInputError(ValueError):
    """The requested image or its provenance did not match the pins."""


@dataclass(frozen=True, slots=True)
class MediaInputPin:
    asset_revision_id: UUID
    output_id: UUID
    bytes_sha256: str
    revision_hash: str
    work_id: UUID
    agent_run_id: UUID
    plan_id: UUID
    plan_hash: str
    generation_run_id: UUID
    generation_run_hash: str
    output_provenance_hash: str



@dataclass(frozen=True, slots=True)
class ResolvedMediaInput:
    data: bytes
    mime_type: str
    pin: MediaInputPin
    work_state: str
    agent_run_status: str
    generation_run_status: str

    def provenance(self) -> dict[str, str]:
        return {**{key: str(value) for key, value in asdict(self.pin).items()},
                "work_state": self.work_state,
                "agent_run_status": self.agent_run_status,
                "generation_run_status": self.generation_run_status,
                "producer_binding": "plan_association_only"}


async def resolve_media_input(session: Any, order: Any, *,
                              reviewer_agent_id: UUID, reviewer_revision_id: UUID,
                              pin: MediaInputPin,
                              media_service: MediaAssetService | None = None,
                              authority_resolver: AgentAuthorityResolver | None = None) -> ResolvedMediaInput:
    """Resolve an image for an already authorized delegation order."""
    if (order.project_id is None or order.persona_id is None or
            order.reviewer_agent_id != reviewer_agent_id or
            order.reviewer_revision_id != reviewer_revision_id):
        raise MediaInputError("media input binding mismatch")
    return await resolve_media_input_for_reviewer(
        session, owner_user_id=order.created_by, project_id=order.project_id,
        persona_id=order.persona_id, reviewer_agent_id=reviewer_agent_id,
        reviewer_revision_id=reviewer_revision_id, pin=pin,
        media_service=media_service, authority_resolver=authority_resolver,
    )


async def resolve_media_input_for_reviewer(
    session: Any, *, owner_user_id: UUID, project_id: UUID, persona_id: UUID,
    reviewer_agent_id: UUID, reviewer_revision_id: UUID, pin: MediaInputPin,
    media_service: MediaAssetService | None = None,
    authority_resolver: AgentAuthorityResolver | None = None,
) -> ResolvedMediaInput:
    """Load one bounded image after caller authentication and project authorization."""
    return await _resolve_for_reviewer(
        session, owner_user_id=owner_user_id, project_id=project_id, persona_id=persona_id,
        reviewer_agent_id=reviewer_agent_id, reviewer_revision_id=reviewer_revision_id,
        pin=pin, media_service=media_service, authority_resolver=authority_resolver,
        media_prefix="image/", max_bytes=IMAGE_MAX_BYTES,
    )


async def resolve_video_input_for_reviewer(
    session: Any, *, owner_user_id: UUID, project_id: UUID, persona_id: UUID,
    reviewer_agent_id: UUID, reviewer_revision_id: UUID, pin: MediaInputPin,
    media_service: MediaAssetService | None = None,
    authority_resolver: AgentAuthorityResolver | None = None,
) -> ResolvedMediaInput:
    """Load one bounded video with the same authority and provenance checks as images."""
    return await _resolve_for_reviewer(
        session, owner_user_id=owner_user_id, project_id=project_id, persona_id=persona_id,
        reviewer_agent_id=reviewer_agent_id, reviewer_revision_id=reviewer_revision_id,
        pin=pin, media_service=media_service, authority_resolver=authority_resolver,
        media_prefix="video/", max_bytes=VIDEO_MAX_BYTES,
    )


async def _resolve_for_reviewer(
    session: Any, *, owner_user_id: UUID, project_id: UUID, persona_id: UUID,
    reviewer_agent_id: UUID, reviewer_revision_id: UUID, pin: MediaInputPin,
    media_service: MediaAssetService | None, authority_resolver: AgentAuthorityResolver | None,
    media_prefix: str, max_bytes: int,
) -> ResolvedMediaInput:
    def require(condition: bool) -> None:
        if not condition:
            raise MediaInputError("media input binding mismatch")

    require(isinstance(pin, MediaInputPin))
    require(owner_user_id is not None and project_id is not None and persona_id is not None)
    for value in (pin.bytes_sha256, pin.revision_hash, pin.plan_hash,
                  pin.generation_run_hash, pin.output_provenance_hash):
        require(isinstance(value, str) and len(value) == 64 and
                all(c in "0123456789abcdef" for c in value))
    decision = await (authority_resolver or AgentAuthorityResolver()).resolve(
        agent_id=reviewer_agent_id, revision_id=reviewer_revision_id,
        project_id=project_id, persona_id=persona_id,
        required_capability="workspace_read",
    )
    require(decision.allowed)
    service = media_service or MediaAssetService()
    prepared = await service._verified(
        session, pin.output_id, owner_user_id, project_id, pin.bytes_sha256
    )
    require(prepared is not None)
    revision, root, relative, identity = prepared
    output = await session.get(GenerationOutput, pin.output_id)
    generation_run = await session.get(GenerationRun, pin.generation_run_id)
    plan = await session.get(GenerationPlan, pin.plan_id)
    work = await session.get(AgentWorkItem, pin.work_id)
    agent_run = await session.get(AgentRun, pin.agent_run_id)
    require(all(row is not None for row in (output, generation_run, plan, work, agent_run)))
    persona_revision = await session.get(PersonaRevision, plan.persona_revision_id)
    require(persona_revision is not None)
    scope = (owner_user_id, project_id)
    require(all((row.owner_user_id, row.project_id) == scope for row in
                (revision, output, generation_run, plan, persona_revision)))
    require(revision.persona_id == persona_id == persona_revision.persona_id)
    require(persona_revision.content_hash == plan.persona_revision_hash)
    require((work.project_id, work.persona_id) == (project_id, persona_id))
    require(agent_run.project_id == project_id and agent_run.work_item_id == work.id)
    require(agent_run.agent_id == work.assigned_agent_id and
            agent_run.agent_revision_id == work.agent_revision_id and
            work.assigned_agent_id is not None and work.agent_revision_id is not None)
    require(work.source_type == "media.generation.plan" and
            work.source_id == str(plan.id) and work.source_revision == pin.plan_hash == plan.plan_hash)
    require(generation_run.plan_id == plan.id and generation_run.run_hash == pin.generation_run_hash)
    require(output.generation_run_id == generation_run.id and output.provenance_hash == pin.output_provenance_hash)
    require(revision.source_kind == "generation_output")
    require(revision.id == pin.asset_revision_id and revision.generation_output_id == output.id and
            revision.revision_hash == pin.revision_hash and revision.sha256 == output.sha256 == pin.bytes_sha256)
    require(output.mime_type == revision.mime_type and revision.mime_type in MIME_EXTENSIONS and
            revision.mime_type.startswith(media_prefix) and 0 < revision.size_bytes <= max_bytes)
    chunks = []
    size = 0
    digest = sha256()
    async for chunk in service.stream_preview(root, relative, identity, 0, revision.size_bytes - 1):
        size += len(chunk)
        require(size <= revision.size_bytes and size <= max_bytes)
        digest.update(chunk)
        chunks.append(chunk)
    require(size == revision.size_bytes and digest.hexdigest() == pin.bytes_sha256)
    return ResolvedMediaInput(b"".join(chunks), revision.mime_type, pin,
                              str(work.state), str(agent_run.status), str(generation_run.status))
