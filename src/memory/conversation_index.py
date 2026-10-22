"""Canonical verified conversation indexing for migration and runtime recovery.

Source reads use current actor/Project/Space/participant/privacy policy. Privacy
withdrawal reads structural metadata only, deletes derivatives, and verifies
absence before acknowledging exclusion. Original rows/files are never changed.
"""
from __future__ import annotations
import uuid
from sqlalchemy import select
from .models import ConversationMessage
from .cross_session_memory import NO_PROJECT_SCOPE, _canonical_character_slug, _memory_embedding_hash
from ..services.privacy_masking_sql import privacy_masking_source_sql
from ..services.search_sources import SourceRef, SourceUnavailable


def _mark(ledger, *args, **kwargs):
    if ledger is not None:
        ledger.mark(*args, **kwargs)


def _attempt(ledger, *args, **kwargs):
    if ledger is not None:
        ledger.attempt(*args, **kwargs)


async def migrate_memory_unit(encoder, qdrant, resolver, ref, ledger=None):
    async def current_input():
        source = await resolver.resolve(ref)
        conversation = await resolver._conversation(source.metadata["session_id"])
        digest = _memory_embedding_hash(source.text, source.metadata["session_id"], conversation.user_id,
                                        conversation.character_name, source.metadata.get("project_id"))
        return source, conversation, digest

    if ref.kind != "conversation_message":
        raise ValueError("Canonical memory requires a conversation message")
    if await resolver.privacy_masked(ref):
        await _remove_private_memory_points(qdrant, resolver, ledger, [ref.id])
        raise SourceUnavailable("privacy_masking_source", reason="privacy_masking_source")
    try:
        source, conversation, input_hash = await current_input()
    except SourceUnavailable as exc:
        if exc.reason == "privacy_masking_source":
            await _remove_private_memory_points(qdrant, resolver, ledger, [ref.id])
        raise
    if not await qdrant.has_verified_document(ref.id, input_hash):
        title = f"Conversation {source.metadata['session_id']}"
        if not await encoder.initialize():
            raise RuntimeError("Memory encoder unavailable")
        vectors = await encoder.embed([source.text], titles=[title])
        try:
            _, _, refreshed_hash = await current_input()
        except SourceUnavailable as exc:
            if exc.reason == "privacy_masking_source":
                await _remove_private_memory_points(qdrant, resolver, ledger, [ref.id])
            raise
        if refreshed_hash != input_hash:
            raise RuntimeError("Memory source changed during encoding; rerun")
        if len(vectors) != 1 or not await qdrant.add_documents(
            [ref.id], vectors, [source.text], [{**source.metadata,
                "user_id": str(conversation.user_id), "embedding_input_hash": input_hash,
                "character_name": _canonical_character_slug(conversation.character_name),
                "project_id": source.metadata.get("project_id") or NO_PROJECT_SCOPE}]):
            raise RuntimeError(encoder.last_error or "Memory write failed")
    # Persisted dimension/finite/L2/hash/identity verification applies equally
    # to a new point and a resumed point. Neither an upsert ACK nor the ledger
    # state is evidence of a complete source unit.
    if not await qdrant.has_verified_document(ref.id, input_hash):
        raise RuntimeError("Memory persisted vector verification failed")
    try:
        _, _, current_hash = await current_input()
    except SourceUnavailable as exc:
        if exc.reason == "privacy_masking_source":
            await _remove_private_memory_points(qdrant, resolver, ledger, [ref.id])
        raise
    if current_hash != input_hash:
        raise RuntimeError("Memory source changed during migration; rerun")
    _mark(ledger, ref, "ready", completed=1, total=1, verified_hash=input_hash)


async def _remove_private_memory_points(qdrant, resolver, ledger, identifiers):
    """Delete only derivatives confirmed by the current canonical bool marker.

    No original body is selected. The migration's destination/owner binding
    has already been verified, and a source row's privacy exclusion is global.
    An unrelated ACL failure never authorizes deleting another reader's point.
    """
    from qdrant_client import models

    identifiers = list(dict.fromkeys(uuid.UUID(str(value)) for value in identifiers))
    if not identifiers:
        return 0
    private = [str(value) for value in (await resolver.session.execute(select(ConversationMessage.id).where(
        ConversationMessage.id.in_(identifiers), privacy_masking_source_sql(ConversationMessage.message_metadata)))).scalars()]
    if not private:
        return 0
    for identifier in private:
        _mark(ledger, SourceRef("conversation_message", identifier), "running", error="privacy_derivative_cleanup")
    selector = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchAny(any=private))])
    try:
        await qdrant._io(qdrant.client.delete, collection_name=qdrant.config.collection_name,
                         points_selector=models.FilterSelector(filter=selector), wait=True)
        remaining = await qdrant._io(qdrant.client.count, collection_name=qdrant.config.collection_name,
                                   count_filter=selector, exact=True)
        if remaining.count:
            raise RuntimeError("Private memory derivative deletion was not verified")
    except BaseException:
        for identifier in private:
            _mark(ledger, SourceRef("conversation_message", identifier), "failed", error="privacy_derivative_cleanup_incomplete")
        raise
    for identifier in private:
        ref = SourceRef("conversation_message", identifier)
        _mark(ledger, ref, "excluded", error="privacy_masking_source")
        _attempt(ledger, ref, resolver.scope.user_id, "excluded", "privacy_masking_source")
    return len(private)


async def purge_private_memory_points(qdrant, resolver, ledger):
    """Resume privacy withdrawal using bounded metadata-only pages."""
    offset, removed = None, 0
    while True:
        points, offset = await qdrant._io(qdrant.client.scroll, collection_name=qdrant.config.collection_name,
            limit=1000, with_payload=["doc_id"], with_vectors=False, offset=offset)
        identifiers = []
        for point in points:
            try:
                identifiers.append(uuid.UUID(str((point.payload or {}).get("doc_id"))))
            except (ValueError, TypeError, AttributeError):
                continue
        removed += await _remove_private_memory_points(qdrant, resolver, ledger, identifiers)
        if offset is None:
            break
    # READY without a point is also withdrawn; do not treat stale readiness
    # as evidence that a masked original may be reused on restart.
    references = [ref.id for ref in ledger.ready() if ref.kind == "conversation_message"] if ledger is not None else []
    for start in range(0, len(references), 1000):
        removed += await _remove_private_memory_points(qdrant, resolver, ledger, references[start:start + 1000])
    return removed
