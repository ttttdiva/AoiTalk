"""Advance proved Team media reviews from the ordinary Dialogue recovery tick."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import aliased

from ..memory.models import AgentDialogue, AgentDialogueDelivery, AgentWorkItem
from .agent_team_media_review import team_media_review_key, validate_team_media_producer, validate_team_video_producer, TeamMediaReviewInvalid
from .agent_work_runtime import WORK_TERMINAL_STATES

logger = logging.getLogger(__name__)


async def reconcile_team_media_completion(transport, task_service, *, limit=16):
    """Discover bounded candidates, then use normal admission and publisher boundaries."""
    limit = max(1, min(int(limit), 64))
    qa = aliased(AgentWorkItem)
    published = aliased(AgentDialogueDelivery)
    qa_base = (qa.parent_work_item_id == AgentWorkItem.id,
               qa.source_type == "api_agent_task", qa.execution_adapter == "api_agent_task",
               func.replace(qa.metadata_json["team_media_review_origin"]["producer_work_item_id"].as_string(), "-", "")
               == func.replace(cast(AgentWorkItem.id, String), "-", ""))
    # Normalize UUID spelling for SQLite and PostgreSQL. The publisher owns this key.
    producer_key = func.replace(cast(AgentWorkItem.id, String), "-", "")
    delivery_key = func.replace(published.idempotency_key, "-", "")
    already_published = exists(select(published.id).where(
        published.dialogue_id == AgentDialogue.id,
        delivery_key.like("teammediareturn:" + producer_key + ":%")))
    base = (select(AgentWorkItem.id, AgentWorkItem.updated_at)
        .join(AgentDialogueDelivery, AgentWorkItem.parent_work_item_id == AgentDialogueDelivery.receiver_work_item_id)
        .join(AgentDialogue, AgentDialogue.id == AgentDialogueDelivery.dialogue_id)
        .where(AgentDialogue.state.in_(("queued", "running", "waiting")),
               AgentDialogueDelivery.kind.in_(("request", "control")),
               AgentDialogueDelivery.state == "acked",
               AgentDialogueDelivery.expected_control_revision == AgentDialogue.control_revision,
               AgentWorkItem.source_type == "media.generation.plan",
               AgentWorkItem.execution_adapter == "media.generation")
        .order_by(AgentWorkItem.updated_at, AgentWorkItem.id).limit(limit))
    session = await transport._session()
    try:
        async def candidates(query, lane):
            cursor = getattr(transport, "_team_media_" + lane + "_cursor", None)
            if cursor is not None:
                query = query.where(or_(AgentWorkItem.updated_at > cursor[1],
                    and_(AgentWorkItem.updated_at == cursor[1], AgentWorkItem.id > cursor[0])))
            rows = (await session.execute(query)).all()
            setattr(transport, "_team_media_" + lane + "_cursor", tuple(rows[-1]) if rows else None)
            return [row[0] for row in rows]

        successful = base.where(AgentWorkItem.state == "succeeded")
        admissions = await candidates(successful.where(~exists(select(qa.id).where(*qa_base))), "admission")
        returns = await candidates(successful.where(
            exists(select(qa.id).where(*qa_base, qa.state == "succeeded")), ~already_published), "return")
        # Blocked and uncertain are recoverable, but must remain visible. The
        # outcome resolver reports their actual status without settling Work.
        reportable = tuple(sorted((WORK_TERMINAL_STATES - {"succeeded"}) | {"blocked", "uncertain"}))
        producer_failures = await candidates(base.where(AgentWorkItem.state.in_(reportable)), "producer_failure")
        review_failures = await candidates(successful.where(
            exists(select(qa.id).where(*qa_base, qa.state.in_(reportable)))), "review_failure")
    finally:
        await transport._close(session)

    advanced = 0
    for producer_id in admissions:
        try:
            session = await transport._session()
            try:
                try:
                    proof = await validate_team_media_producer(session, producer_work_item_id=producer_id, config=transport.config)
                    submit = task_service.submit_from_verified_media_work
                except TeamMediaReviewInvalid:
                    proof = await validate_team_video_producer(session, producer_work_item_id=producer_id, config=transport.config)
                    submit = task_service.submit_from_verified_video_work
                key = team_media_review_key(proof)
            finally:
                await transport._close(session)
            _, created = await submit(
                producer_id, workspace=None, idempotency_key=key)
            advanced += int(created)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Team media completion class=unexpected stage=admission code=review_admission_status_unproved")

    for producer_id in returns:
        try:
            session = await transport._session()
            try:
                delivery = await transport.enqueue_team_media_return(str(producer_id), session=session)
                await session.commit()
                advanced += int(delivery is not None and not delivery.get("idempotent"))
            finally:
                await transport._close(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Team media completion class=unexpected stage=return code=publication_deferred")
    # The publisher verifies terminal evidence and owns attempt/control idempotency.
    # Do not suppress a fresh attempt or control revision based on an older delivery.
    for producer_id in (*producer_failures, *review_failures):
        try:
            session = await transport._session()
            try:
                delivery = await transport.enqueue_team_media_outcome(str(producer_id), session=session)
                await session.commit()
                advanced += int(delivery is not None and not delivery.get("idempotent"))
            finally:
                await transport._close(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Team media completion class=unexpected stage=outcome code=publication_deferred")
    return advanced
