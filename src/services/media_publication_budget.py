"""Program-wide publication admission over the existing Operations evidence ledger.

No scheduler or provider is introduced here. A committed attempt reserves one
publication; success keeps it spent, and only bound not-submitted evidence can
release it. Revisions change the current ceiling, never reset prior usage.
"""
from __future__ import annotations

from sqlalchemy import String, cast, exists, func, or_, select, update

from ..memory.models.media_operations_research import ContentItem, EditorialProgram
from ..memory.models.operations import ExternalAction, ExternalActionAttempt, ExternalActionReceipt, OperationEvent

PUBLICATION_ACTIONS = ("media.publish_content", "media.release_product")
MAX_PUBLICATIONS = 10000
NOT_SUBMITTED_EVENT = "media.publication.not_submitted"
RESERVATION_EVENT = "media.publication.reserved"


async def lock_publication_program(session, program_id):
    """Use the same transaction lock for policy revisions and child admission."""
    result = await session.execute(update(EditorialProgram).where(
        EditorialProgram.id == program_id,
    ).values(id=EditorialProgram.id).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ValueError("publication program is unavailable")


def unreleased_attempt():
    # UUID storage is hex on SQLite and hyphenated on PostgreSQL.
    released = exists(select(OperationEvent.id).where(
        OperationEvent.entity_type == "action",
        OperationEvent.entity_id == ExternalActionAttempt.action_id,
        OperationEvent.owner_user_id == ExternalActionAttempt.owner_user_id,
        OperationEvent.project_id.is_not_distinct_from(ExternalAction.project_id),
        OperationEvent.event_type == NOT_SUBMITTED_EVENT,
        OperationEvent.payload_json["remote_status"].as_string() == "not_submitted",
        OperationEvent.payload_json["attempt_key"].as_string()
        == func.replace(cast(ExternalActionAttempt.id, String), "-", ""),
        OperationEvent.payload_json["action_version"].as_integer()
        == ExternalActionAttempt.action_version,
    ).correlate(ExternalActionAttempt, ExternalAction))
    return or_(ExternalActionAttempt.status != "failed", ~released)


def occupied_action():
    return or_(
        exists(select(ExternalActionAttempt.id).where(
            ExternalActionAttempt.action_id == ExternalAction.id,
            unreleased_attempt(),
        )),
        exists(select(ExternalActionReceipt.id).where(
            ExternalActionReceipt.action_id == ExternalAction.id,
        )),
        ExternalAction.status.in_(("succeeded", "attempting", "uncertain")),
    )


def publication_units(action_payload):
    payload = action_payload.get("payload") if isinstance(action_payload, dict) else None
    if not isinstance(payload, dict):
        return MAX_PUBLICATIONS + 1  # Historical quantity cannot be established.
    if payload.get("type") == "x_thread":
        posts = payload.get("posts")
        return len(posts) if isinstance(posts, list) and 1 <= len(posts) <= 20 else MAX_PUBLICATIONS + 1
    single = {"x_post", "pixiv_work", "dlsite_release", "fanza_release", "tiktok_video", "patreon_post",
              "youtube_video", "youtube_short", "instagram_feed", "instagram_carousel", "instagram_reel"}
    return 1 if payload.get("type") in single else MAX_PUBLICATIONS + 1


async def publication_usage(session, program_id):
    reserved_units = select(OperationEvent.payload_json["units"].as_integer()).where(
        OperationEvent.entity_type == "action",
        OperationEvent.entity_id == ExternalActionAttempt.action_id,
        OperationEvent.owner_user_id == ExternalActionAttempt.owner_user_id,
        OperationEvent.project_id.is_not_distinct_from(ExternalAction.project_id),
        OperationEvent.event_type == RESERVATION_EVENT,
        OperationEvent.payload_json["attempt_key"].as_string()
        == func.replace(cast(ExternalActionAttempt.id, String), "-", ""),
        OperationEvent.payload_json["action_version"].as_integer()
        == ExternalActionAttempt.action_version,
    ).correlate(ExternalActionAttempt, ExternalAction).order_by(OperationEvent.created_at).limit(1).scalar_subquery()
    receipt = exists(select(ExternalActionReceipt.id).where(
        ExternalActionReceipt.attempt_id == ExternalActionAttempt.id,
    ))
    rows = (await session.execute(select(
        ExternalActionAttempt.action_version, ExternalAction.action_version,
        ExternalAction.payload_json, reserved_units,
    ).join(ExternalAction, ExternalAction.id == ExternalActionAttempt.action_id).join(
        ContentItem, ContentItem.id == ExternalAction.content_item_id,
    ).where(
        ContentItem.editorial_program_id == program_id,
        ExternalAction.action_type.in_(PUBLICATION_ACTIONS),
        or_(unreleased_attempt(), receipt),
    ).limit(MAX_PUBLICATIONS + 1))).all()
    total = 0
    for attempt_version, action_version, payload, units in rows:
        if units is None:
            units = publication_units(payload) if attempt_version == action_version else MAX_PUBLICATIONS + 1
        total += units if type(units) is int and units > 0 else MAX_PUBLICATIONS + 1
    # Preserve a conservative reservation for malformed historical status-only
    # records, including contradictory terminal statuses with only released
    # failed attempts. Missing counted evidence is never proof of no send.
    orphaned = await session.scalar(select(func.count(ExternalAction.id)).join(
        ContentItem, ContentItem.id == ExternalAction.content_item_id,
    ).where(
        ContentItem.editorial_program_id == program_id,
        ExternalAction.action_type.in_(PUBLICATION_ACTIONS),
        occupied_action(),
        ~exists(select(ExternalActionAttempt.id).where(
            ExternalActionAttempt.action_id == ExternalAction.id,
            or_(unreleased_attempt(), exists(select(ExternalActionReceipt.id).where(
                ExternalActionReceipt.attempt_id == ExternalActionAttempt.id,
            ))),
        )),
    ))
    orphan_receipt = await session.scalar(select(ExternalActionReceipt.id).join(
        ExternalAction, ExternalAction.id == ExternalActionReceipt.action_id,
    ).join(ContentItem, ContentItem.id == ExternalAction.content_item_id).where(
        ContentItem.editorial_program_id == program_id,
        ExternalAction.action_type.in_(PUBLICATION_ACTIONS),
        ~exists(select(ExternalActionAttempt.id).where(
            ExternalActionAttempt.id == ExternalActionReceipt.attempt_id,
            ExternalActionAttempt.action_id == ExternalActionReceipt.action_id,
            ExternalActionAttempt.owner_user_id == ExternalActionReceipt.owner_user_id,
            ExternalActionAttempt.action_version == ExternalActionReceipt.action_version,
        )),
    ).limit(1))
    return total + (MAX_PUBLICATIONS + 1 if orphaned or orphan_receipt is not None else 0)
