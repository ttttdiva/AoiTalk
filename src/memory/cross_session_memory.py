"""
Cross-session memory service for retrieving relevant past conversations.

This service enables the AI to recall and utilize information from past conversation
sessions to provide more contextual and informed responses.
"""

import re
import logging
import asyncio
import threading
import uuid
import hashlib
import json
import math
from typing import Optional, List, Dict, Any

from .search_errors import MemorySearchUnavailable
from ..security.field_crypto import FieldCryptoError, decrypt_text_if_needed

logger = logging.getLogger(__name__)

_CORRUPT_SOURCE_FIELD_ERRORS = {
    "unsupported encrypted field format",
    "encrypted field authentication failed",
}

# Qdrant equality filters cannot express SQL-style NULL safely. Messages that
# are not associated with a project therefore use an explicit scope value.
NO_PROJECT_SCOPE = "__aoitalk_no_project__"
PROJECT_MANAGER_SLUG = "project_manager"
LEGACY_PROJECT_MANAGER_SLUG = "project_management_assistant"


def _canonical_character_slug(character_name: Optional[str]) -> str:
    value = str(character_name or "unknown")
    if value == LEGACY_PROJECT_MANAGER_SLUG:
        return PROJECT_MANAGER_SLUG
    return value


def _character_slug_candidates(character_name: Optional[str]) -> tuple[str, ...]:
    canonical = _canonical_character_slug(character_name)
    if canonical == PROJECT_MANAGER_SLUG:
        return (PROJECT_MANAGER_SLUG, LEGACY_PROJECT_MANAGER_SLUG)
    return (canonical,)


def _character_slugs_match(left: Optional[str], right: Optional[str]) -> bool:
    return _canonical_character_slug(left) == _canonical_character_slug(right)


def _memory_embedding_hash(content, session_id, user_id, character_name, project_id):
    from ..rag.embedding_identity import document_input
    payload = {"input": document_input(content, f"Conversation {session_id}"),
               "session_id": str(session_id), "user_id": str(user_id),
               "character_name": _canonical_character_slug(character_name),
               "project_id": str(project_id) if project_id else NO_PROJECT_SCOPE}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _is_corrupt_source_field_error(error: FieldCryptoError) -> bool:
    """Classify malformed/tampered source ciphertext without logging it."""

    return str(error) in _CORRUPT_SOURCE_FIELD_ERRORS


def _memory_inventory_statement(actor, *, include_masked=False):
    """Current owner/joined-user message relation, before selecting a scope.

    Callers establish the actor's current active/Project/Space boundary with
    SearchSourceResolver. Migration repeats that boundary for each original.
    """
    from sqlalchemy import select, or_, exists
    from .models import ConversationSession, ConversationMessage, ConversationParticipant
    from ..services.privacy_masking_sql import privacy_masking_source_sql
    joined = exists(select(ConversationParticipant.id).where(
        ConversationParticipant.session_id == ConversationSession.id,
        ConversationParticipant.participant_type == "user",
        ConversationParticipant.participant_id == str(actor),
        ConversationParticipant.status == "joined"))
    stmt = select(ConversationMessage).join(ConversationSession,
        ConversationMessage.session_id == ConversationSession.id).where(
        or_(ConversationSession.user_id == str(actor), joined),
        ConversationSession.deleted_at.is_(None),
        ConversationMessage.deleted_at.is_(None),
        ConversationMessage.is_active_branch.is_not(False))
    if not include_masked:
        stmt = stmt.where(~privacy_masking_source_sql(ConversationMessage.message_metadata))
    return stmt


def _memory_statement(actor, character_name, project, *, message_ids=None, exclude_session=None,
                      created_after=None, created_before=None):
    from .models import ConversationSession, ConversationMessage
    stmt = _memory_inventory_statement(actor).where(
        ConversationSession.project_id == project,
        ConversationSession.character_name.in_(_character_slug_candidates(character_name)))
    if message_ids is not None:
        stmt = stmt.where(ConversationMessage.id.in_(message_ids))
    if exclude_session:
        stmt = stmt.where(ConversationSession.id != uuid.UUID(str(exclude_session)))
    if created_after is not None:
        stmt = stmt.where(ConversationMessage.created_at >= created_after)
    if created_before is not None:
        stmt = stmt.where(ConversationMessage.created_at < created_before)
    return stmt


async def _authorized_memory_ids(user_id, character_name, project_id, *, exclude_session=None,
                                 created_after=None, created_before=None):
    """One current SQL ACL query supplies the complete vector candidate filter."""
    from .database import get_db_session
    from .models import ConversationMessage
    from ..services.search_sources import SearchSourceResolver, SearchScope
    actor = uuid.UUID(str(user_id))
    project = uuid.UUID(str(project_id)) if project_id else None
    async with await get_db_session() as database:
        await SearchSourceResolver(database, SearchScope(actor, project))._project(project)
        stmt = _memory_statement(actor, character_name, project, exclude_session=exclude_session,
                                 created_after=created_after, created_before=created_before).with_only_columns(ConversationMessage.id)
        return [str(value) for value in (await database.execute(stmt)).scalars()]


async def _read_current_memories(message_ids, *, user_id, character_name, project_id,
                                 created_after=None, created_before=None,
                                 raise_on_corrupt=False):
    """One bounded canonical SQL read rechecks ACL, branch and deletion state.

    Indexed text and metadata never authorize or supply user-visible originals.
    This read uses a fresh session, not an ORM identity map from candidate search.
    """
    from .database import get_db_session
    from ..services.search_sources import SearchSourceResolver, SearchScope
    from .models import ConversationMessage, ConversationSession
    actor = uuid.UUID(str(user_id))
    project = uuid.UUID(str(project_id)) if project_id else None
    identifiers = []
    for value in message_ids:
        try:
            identifier = uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError):
            # Qdrant payloads are untrusted.  One malformed locator must not
            # prevent valid canonical IDs from being hydrated.
            continue
        if identifier not in identifiers:
            identifiers.append(identifier)
    if len(identifiers) > 400:
        raise ValueError("Memory original candidate limit exceeded")
    if not identifiers:
        return {}
    async with await get_db_session() as database:
        await SearchSourceResolver(database, SearchScope(actor, project))._project(project)
        statement = _memory_statement(
            actor,
            character_name,
            project,
            message_ids=identifiers,
            created_after=created_after,
            created_before=created_before,
        ).with_only_columns(
            ConversationMessage.id,
            ConversationMessage.session_id,
            ConversationMessage.role,
            ConversationMessage.__table__.c.content.label("content_ciphertext"),
            ConversationMessage.created_at,
            ConversationSession.user_id,
            ConversationSession.character_name,
            ConversationSession.project_id,
            maintain_column_froms=True,
        )
        rows = (await database.execute(statement)).mappings().all()
        current = {}
        corrupt_count = 0
        for row in rows:
            try:
                content = decrypt_text_if_needed(
                    row["content_ciphertext"],
                    aad="conversation_messages.content",
                )
                if not isinstance(content, str):
                    raise TypeError("conversation content is not text")
            except FieldCryptoError as exc:
                if not _is_corrupt_source_field_error(exc):
                    raise
                corrupt_count += 1
                continue
            except (TypeError, UnicodeDecodeError):
                corrupt_count += 1
                continue
            current[str(row["id"])] = {
                "id": str(row["id"]),
                "content": content,
                "role": row["role"],
                "session_id": str(row["session_id"]),
                "timestamp": row["created_at"].isoformat()
                if row["created_at"]
                else None,
                "character_name": _canonical_character_slug(
                    row["character_name"]
                ),
                "project_id": (
                    str(row["project_id"])
                    if row["project_id"]
                    else None
                ),
                # The indexed payload is accepted only when it matches this
                # fresh canonical source identity/content hash.
                "_embedding_input_hash": _memory_embedding_hash(
                    content,
                    row["session_id"],
                    row["user_id"],
                    row["character_name"],
                    row["project_id"],
                ),
            }
        if raise_on_corrupt and corrupt_count and not current:
            raise FieldCryptoError("encrypted field authentication failed")
        return current

# Keywords that trigger past conversation lookup
TRIGGER_KEYWORDS_JA = [
    "前に", "以前", "また", "覚えて", "話した", "言った", "教えた", 
    "約束", "頼んだ", "お願いした", "聞いた", "質問した", "前回",
    "続き", "だっけ",
]
TRIGGER_KEYWORDS_EN = [
    "before", "previously", "again", "remember", "told you", "said",
    "mentioned", "promised", "asked"
]

# Pronoun-only patterns (may indicate reference to past context)
PRONOUN_PATTERNS = [
    r"^(それ|あれ|これ|その|あの|この|何|どこ|いつ|who|what|where|when|that|this|it)\s*[\?？]?$",
    r"^(それ|あれ|これ)って",
]


class _InitializationAttempt:
    """Thread-safe completion gate shared by callers of one init attempt."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[bool] = None


class CrossSessionMemoryService:
    """Service for retrieving relevant past conversations across sessions."""
    
    # Class-level cache for shared components
    _shared_embedding = None
    _shared_qdrant = None
    _initialization_lock = threading.Lock()
    
    COLLECTION_NAME = "aoitalk_conversation_memory"
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the cross-session memory service.
        
        Args:
            config: Optional configuration dict
        """
        self.config = config or {}
        self._initialized = False
        self.embedding = None
        self.qdrant = None
        self._initialization_attempt: Optional[_InitializationAttempt] = None
        
        # Configuration
        self.min_relevance_score = self.config.get('min_relevance_score', 0.3)
        self.max_results = self.config.get('max_results', 5)
        self.max_context_messages = self.config.get('max_context_messages', 10)
    
    async def initialize(self) -> bool:
        """Initialize the service components.

        Returns:
            True if initialization successful
        """
        lock = CrossSessionMemoryService._initialization_lock
        with lock:
            if (
                self._initialized
                and self.embedding is not None
                and self.qdrant is not None
            ):
                return True

            attempt = self._initialization_attempt
            is_owner = attempt is None
            if is_owner:
                attempt = _InitializationAttempt()
                self._initialization_attempt = attempt

        if not is_owner:
            # ``Event.wait`` is blocking, so keep it off the event loop. This
            # also allows callers from different asyncio loops to share a gate.
            await asyncio.to_thread(attempt.done.wait)
            return bool(attempt.result)

        candidate_embedding = None
        candidate_qdrant = None
        initialized = False
        try:
            # Use shared RAG infrastructure. Component references remain local
            # until both initializers have succeeded.
            from ..rag.embedding import EmbeddingGemma2Embedding, get_embedding
            from ..rag.qdrant_client import QdrantManager
            from ..rag.config import QdrantConfig, get_rag_config

            rag_config = get_rag_config()

            # A failed shared object is discarded rather than becoming a
            # sticky cache entry; successful EmbeddingGemma 2 instances may be reused.
            from ..rag import manager as rag_manager

            def _discard_failed_embedding() -> None:
                with lock:
                    if (
                        CrossSessionMemoryService._shared_embedding
                        is shared_embedding
                    ):
                        CrossSessionMemoryService._shared_embedding = None
                    if rag_manager._shared_embedding is shared_embedding:
                        rag_manager._shared_embedding = None

            with lock:
                shared_embedding = CrossSessionMemoryService._shared_embedding
                if shared_embedding is not None and not getattr(
                    shared_embedding, "_initialized", False
                ):
                    CrossSessionMemoryService._shared_embedding = None
                    shared_embedding = None

                if shared_embedding is None:
                    if rag_manager._shared_embedding is not None:
                        shared_embedding = rag_manager._shared_embedding
                    else:
                        shared_embedding = get_embedding(rag_config.embedding)

            # RagManager's cache may contain an object that failed previously.
            # Its existence alone is not success; always inspect initialize().
            initialize_embedding = getattr(shared_embedding, "initialize", None)
            if initialize_embedding is None:
                _discard_failed_embedding()
                logger.error("[CrossSessionMemory] Embedding initialization failed")
                return False
            try:
                embedding_initialized = await initialize_embedding()
            except asyncio.CancelledError:
                _discard_failed_embedding()
                raise
            except Exception:
                _discard_failed_embedding()
                raise
            if not embedding_initialized:
                _discard_failed_embedding()
                logger.error("[CrossSessionMemory] Embedding initialization failed")
                return False
            candidate_embedding = shared_embedding

            with lock:
                CrossSessionMemoryService._shared_embedding = candidate_embedding

            qdrant_config = QdrantConfig(
                host=rag_config.qdrant.host,
                port=rag_config.qdrant.port,
                collection_name=self.COLLECTION_NAME,
                local_path=rag_config.qdrant.local_path,
            )
            candidate_qdrant = QdrantManager(
                qdrant_config, vector_size=candidate_embedding.dimension
            )
            try:
                qdrant_initialized = await candidate_qdrant.initialize()
            except Exception as e:
                logger.error(
                    "[CrossSessionMemory] Qdrant initialization raised an exception: %s",
                    e,
                )
                return False
            if not qdrant_initialized:
                logger.error(
                    "[CrossSessionMemory] Qdrant initialization returned false"
                )
                return False

            initialized = True
            logger.info(
                "[CrossSessionMemory] Initialized with collection: %s",
                self.COLLECTION_NAME,
            )
            return True

        except asyncio.CancelledError:
            # Release the completion gate in finally before propagating
            # cancellation. Waiters receive the same False result.
            raise
        except Exception as e:
            logger.error(f"[CrossSessionMemory] Initialization failed: {e}")
            return False
        finally:
            with lock:
                if (
                    initialized
                    and candidate_embedding is not None
                    and candidate_qdrant is not None
                ):
                    self.embedding = candidate_embedding
                    self.qdrant = candidate_qdrant
                    self._initialized = True
                    attempt.result = True
                else:
                    self.embedding = None
                    self.qdrant = None
                    self._initialized = False
                    attempt.result = False
                if self._initialization_attempt is attempt:
                    self._initialization_attempt = None
                # Set while holding the short lock so callers cannot observe a
                # cleared attempt before its waiters are released.
                attempt.done.set()

    def should_search_past_conversations(self, user_input: str) -> bool:
        """Check if the user input suggests referencing past conversations.
        
        Args:
            user_input: User's input text
            
        Returns:
            True if past conversation search should be triggered
        """
        if not user_input or len(user_input.strip()) < 2:
            return False
        
        input_lower = user_input.lower()
        
        # Check for trigger keywords (Japanese)
        for keyword in TRIGGER_KEYWORDS_JA:
            if keyword in user_input:
                logger.debug(f"[CrossSessionMemory] Trigger keyword found: {keyword}")
                return True
        
        # Check for trigger keywords (English)
        for keyword in TRIGGER_KEYWORDS_EN:
            if keyword in input_lower:
                logger.debug(f"[CrossSessionMemory] Trigger keyword found: {keyword}")
                return True
        
        # Check for pronoun-only patterns
        for pattern in PRONOUN_PATTERNS:
            if re.match(pattern, user_input.strip(), re.IGNORECASE):
                logger.debug(f"[CrossSessionMemory] Pronoun pattern matched")
                return True
        
        return False
    
    async def search_relevant_conversations(
        self, 
        user_id: str, 
        query: str, 
        current_session_id: Optional[str] = None,
        limit: Optional[int] = None,
        character_name: Optional[str] = None,
        project_id: Optional[str] = None,
        created_after=None,
        created_before=None,
        raise_on_unavailable: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search for relevant past conversations.
        
        Args:
            user_id: User identifier
            query: Search query (usually user's current input)
            current_session_id: Current session ID to exclude from results
            limit: Maximum number of results
            character_name: Current character scope. Missing values use "unknown".
            project_id: Current project scope. None means non-project conversations.
            
        Returns:
            List of relevant conversation snippets with metadata
        """
        if not self._initialized:
            if not await self.initialize():
                if raise_on_unavailable:
                    raise MemorySearchUnavailable("index_unavailable")
                return []
        
        limit = max(1, min(int(limit or self.max_results), 100))
        time_bounds = {}
        if created_after is not None:
            time_bounds["created_after"] = created_after
        if created_before is not None:
            time_bounds["created_before"] = created_before
        
        try:
            # Generate query embedding
            try:
                query_embedding = await self.embedding.embed_query(query)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MemorySearchUnavailable("encoder_unavailable") from exc
            
            if not query_embedding:
                if raise_on_unavailable:
                    raise MemorySearchUnavailable("encoder_unavailable")
                return []
            
            authorized = await _authorized_memory_ids(user_id, character_name, project_id,
                                                      exclude_session=current_session_id, **time_bounds)
            if not authorized:
                return []
            filter_conditions = {
                "doc_id": authorized,
                "project_id": str(project_id) if project_id else NO_PROJECT_SCOPE,
            }
            initial_top_k = limit * 4
            # Invalid/stale points can occupy the first page.  Retry with a
            # bounded larger page only when that page was full and lost a
            # candidate during canonical hydration/hash validation.
            max_top_k = 400

            async def collect_valid_hits(scoped_results):
                candidates = []
                rejected = False
                for result in scoped_results:
                    try:
                        result_id = str(result.id)
                        uuid.UUID(result_id)
                        score = float(result.score)
                        if not math.isfinite(score):
                            rejected = True
                            continue
                    except (AttributeError, TypeError, ValueError):
                        rejected = True
                        continue
                    if score < self.min_relevance_score:
                        continue
                    metadata = getattr(result, "metadata", None) or {}
                    if not isinstance(metadata, dict) or not isinstance(
                        metadata.get("embedding_input_hash"), str
                    ):
                        # Legacy/unverified points cannot publish a current
                        # canonical body.  They are eligible for overfetch.
                        rejected = True
                        continue
                    candidates.append((result_id, score, metadata))

                current = await _read_current_memories(
                    [result_id for result_id, _, _ in candidates],
                    user_id=user_id,
                    character_name=character_name,
                    project_id=project_id,
                    raise_on_corrupt=raise_on_unavailable,
                    **time_bounds,
                )
                deduped_results = {}
                for result_id, score, metadata in candidates:
                    candidate = current.get(result_id)
                    if candidate is None:
                        rejected = True
                        continue
                    if current_session_id and candidate["session_id"] == current_session_id:
                        rejected = True
                        continue
                    if metadata.get("embedding_input_hash") != candidate.get(
                        "_embedding_input_hash"
                    ):
                        rejected = True
                        continue
                    candidate = dict(candidate)
                    candidate["relevance_score"] = score
                    existing = deduped_results.get(result_id)
                    if existing is None or score > existing["relevance_score"]:
                        deduped_results[result_id] = candidate

                formatted = sorted(
                    deduped_results.values(),
                    key=lambda item: item["relevance_score"],
                    reverse=True,
                )[:limit]
                return formatted, rejected

            top_k = initial_top_k
            while True:
                # Rank the full authorized set once per page. Splitting IDs
                # changed global top-K and repeated a full local vector scan
                # for every 256 IDs.
                scoped_results = await self.qdrant.search(
                    query_embedding=query_embedding,
                    top_k=top_k,
                    filter_conditions=filter_conditions,
                    **({"raise_on_error": True} if raise_on_unavailable else {}),
                )
                formatted_results, rejected = await collect_valid_hits(scoped_results)
                if (
                    len(formatted_results) >= limit
                    or not rejected
                    or len(scoped_results) < top_k
                    or top_k >= max_top_k
                ):
                    break
                top_k = min(max_top_k, max(top_k * 2, top_k + 1))

            if rejected and not formatted_results and scoped_results and top_k >= max_top_k:
                # A full/finite index page made of stale or malformed points
                # is an index failure, not a successful zero-hit query.  Keep
                # ambient callers best-effort while surfacing the condition
                # to explicit search callers.
                if raise_on_unavailable:
                    raise MemorySearchUnavailable("backend_failed")
                return []

            for result in formatted_results:
                result.pop("_embedding_input_hash", None)
            
            logger.info(f"[CrossSessionMemory] Found {len(formatted_results)} relevant messages for user {user_id}")
            return formatted_results
            
        except asyncio.CancelledError:
            raise
        except MemorySearchUnavailable:
            if raise_on_unavailable:
                raise
            logger.warning("[CrossSessionMemory] Search backend unavailable")
            return []
        except Exception as exc:
            logger.warning("[CrossSessionMemory] Search backend failed")
            if raise_on_unavailable:
                from ..rag.qdrant_client import QdrantSearchUnavailable

                code = exc.code if isinstance(exc, QdrantSearchUnavailable) else "backend_failed"
                raise MemorySearchUnavailable(code) from exc
            return []
    
    async def search_prepared(self, unit, *, session, user_id, character_name=None,
                              project_id=None, space_id=None, current_session_id=None, limit=20):
        """Shared native query with current SQL candidates and fresh originals."""
        from sqlalchemy import select
        from qdrant_client import models
        from .models import ConversationMessage, ConversationSession
        from ..rag.media_query import prepared_dense_cosine, require_prepared
        from ..rag.embedding_identity import EMBEDDING_FINGERPRINT
        from ..services.search_sources import SearchSourceResolver, SearchScope

        dense = require_prepared(unit)
        actor = uuid.UUID(str(user_id))
        project = uuid.UUID(str(project_id)) if project_id else None
        resolver = SearchSourceResolver(session, SearchScope(actor, project,
            uuid.UUID(str(space_id)) if space_id else None))
        await resolver._actor()
        await resolver._project(project)
        stmt = _memory_statement(actor, character_name, project, exclude_session=current_session_id)
        authorized = [str(value) for value in (await session.execute(stmt.with_only_columns(ConversationMessage.id))).scalars()]
        if not authorized:
            return []
        if not self._initialized and not await self.initialize():
            raise RuntimeError("conversation_index_unavailable")
        from ..rag.qdrant_client import _LocalMatchValues
        # Conversation memory is currently an unnamed dense collection, but
        # keep the prepared path schema-aware so a named native dense vector
        # cannot be queried or read as an arbitrary point score.  The regular
        # memory path below intentionally remains unchanged.
        native_dense_name = None
        with_vectors = True
        get_collection = getattr(self.qdrant.client, "get_collection", None)
        if callable(get_collection):
            try:
                collection_info = await self.qdrant._io(
                    get_collection, collection_name=self.qdrant.config.collection_name
                )
                vectors = getattr(
                    getattr(getattr(collection_info, "config", None), "params", None),
                    "vectors", None,
                )
                if isinstance(vectors, dict):
                    # Qdrant's ``vectors`` map contains dense vector params;
                    # sparse vectors live in a separate map.  Prefer the
                    # conventional native name when a collection exposes
                    # more than one named dense vector.
                    names = list(vectors)
                    if "dense" in vectors:
                        native_dense_name = "dense"
                    elif len(names) == 1:
                        native_dense_name = names[0]
                    else:
                        # There is no unambiguous native dense vector to
                        # request or score against, so publish no candidates.
                        return []
                    with_vectors = [native_dense_name]
            except Exception:
                # The initialized memory manager normally has an unnamed
                # schema.  If a lightweight client cannot expose collection
                # metadata, request the unnamed vector and still fail closed
                # below whenever Qdrant returns no usable dense vector.
                native_dense_name = None
                with_vectors = True
        match = models.MatchAny(any=authorized)
        if self.qdrant._is_local_mode:
            match.any = _LocalMatchValues(match.any)
        query_filter = models.Filter(must=[models.FieldCondition(key="doc_id", match=match),
            models.FieldCondition(key="project_id", match=models.MatchValue(value=str(project) if project else NO_PROJECT_SCOPE)),
            models.FieldCondition(key="embedding_fingerprint", match=models.MatchValue(value=EMBEDDING_FINGERPRINT))])
        query_options = {
            "collection_name": self.qdrant.config.collection_name,
            "query": dense,
            "query_filter": query_filter,
            "limit": min(limit, 100) * 4,
            "with_payload": True,
            "with_vectors": with_vectors,
        }
        if native_dense_name is not None:
            query_options["using"] = native_dense_name
        response = await self.qdrant._io(self.qdrant.client.query_points, **query_options)
        points = {str((p.payload or {}).get("doc_id")): p for p in response.points}
        if not points:
            return []
        rows = await session.execute(stmt.add_columns(ConversationSession).where(
            ConversationMessage.id.in_([uuid.UUID(value) for value in points])).execution_options(populate_existing=True))
        hits = []
        await resolver._actor()
        await resolver._project(project)
        for message, conversation in rows:
            point = points[str(message.id)]
            payload = point.payload or {}
            digest = _memory_embedding_hash(message.content, message.session_id, conversation.user_id,
                conversation.character_name, conversation.project_id)
            if payload.get("embedding_input_hash") != digest:
                continue
            vector = getattr(point, "vector", None)
            if native_dense_name is not None:
                saved_dense = vector.get(native_dense_name) if isinstance(vector, dict) else None
            elif isinstance(vector, (list, tuple)):
                saved_dense = vector
            else:
                # The initialized conversation collection is unnamed.  A
                # mapping here is therefore malformed, even if it happens to
                # contain a list under a familiar key.
                saved_dense = None
            semantic_score = prepared_dense_cosine(
                dense, saved_dense, getattr(self.embedding, "dimension", None)
            )
            if semantic_score is None:
                # A prepared candidate is publishable only when its stored
                # dense vector yields a finite cosine.  The point score may be
                # an RRF/partition-local score and is never a fallback here.
                continue
            hits.append({"id": str(message.id), "session_id": str(message.session_id), "content": message.content,
                "_query_content_hash": digest,
                "role": message.role, "character_name": _canonical_character_slug(conversation.character_name),
                "project_id": str(project) if project else None, "relevance_score": semantic_score,
                "_query_semantic_score": semantic_score,
                "citation": {"text_start": 0, "text_end": len(message.content)}})
        return sorted(hits, key=lambda hit: hit["relevance_score"], reverse=True)[:limit]

    async def authorize_prepared_hits(self, hits, *, session, user_id, character_name,
                                      project_id=None, current_session_id=None):
        from .models import ConversationSession
        stmt = _memory_statement(uuid.UUID(str(user_id)), character_name, project_id,
            message_ids=[uuid.UUID(hit["id"]) for hit in hits], exclude_session=current_session_id)
        rows = await session.execute(stmt.add_columns(ConversationSession).execution_options(populate_existing=True))
        hashes = {str(message.id): _memory_embedding_hash(message.content, message.session_id,
            conversation.user_id, conversation.character_name, conversation.project_id)
            for message, conversation in rows}
        return [hit for hit in hits if hit.get("_query_content_hash") and
            hit["_query_content_hash"] == hashes.get(hit["id"])]

    def format_memory_context(
        self, 
        results: List[Dict[str, Any]], 
        max_chars: int = 1500
    ) -> str:
        """Format search results as context for LLM.
        
        Args:
            results: Search results from search_relevant_conversations
            max_chars: Maximum characters in formatted context
            
        Returns:
            Formatted context string
        """
        if not results:
            return ""
        
        context_parts = ["## 過去の会話からの関連情報:"]
        current_length = len(context_parts[0])
        
        for i, result in enumerate(results, 1):
            role = "ユーザー" if result["role"] == "user" else "あなた"
            content = result["content"]
            score = result.get("relevance_score", 0)
            
            # Truncate long content
            if len(content) > 300:
                content = content[:297] + "..."
            
            entry = f"\n{i}. [{role}] {content} (関連度: {score:.2f})"
            
            if current_length + len(entry) > max_chars:
                break
            
            context_parts.append(entry)
            current_length += len(entry)
        
        return "".join(context_parts)
    
    async def index_current_message(self, resolver, ref):
        """Index the current canonical source, also used by durable recovery."""
        from dataclasses import replace
        from ..rag.config import get_rag_config
        from ..rag.embedding import get_embedding
        from ..rag.qdrant_client import QdrantManager
        from .conversation_index import migrate_memory_unit
        from ..services.search_sources import SourceUnavailable

        encoder, qdrant = self.embedding, self.qdrant
        if encoder is None or qdrant is None:
            config = get_rag_config()
            encoder = encoder or get_embedding(config.embedding)
            qdrant = qdrant or QdrantManager(replace(config.qdrant, collection_name=self.COLLECTION_NAME),
                                           vector_size=encoder.dimension)
        try:
            if not await qdrant.initialize():
                return {"status": "failed", "reason": "memory_index_unavailable"}
            await migrate_memory_unit(encoder, qdrant, resolver, ref)
            return {"status": "ready"}
        except SourceUnavailable as exc:
            return {"status": "excluded", "reason": exc.reason}
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"status": "failed", "reason": "memory_indexing_failed"}
    
    async def get_collection_info(self) -> Optional[Dict[str, Any]]:
        """Get information about the conversation memory collection.
        
        Returns:
            Collection info or None
        """
        if not self._initialized:
            return None
        
        try:
            return await self.qdrant.get_collection_info()
        except Exception as e:
            logger.error(f"[CrossSessionMemory] Failed to get collection info: {e}")
            return None


# Global service instance
_cross_session_memory: Optional[CrossSessionMemoryService] = None
_cross_session_memory_factory_lock = threading.Lock()


def get_cross_session_memory(
    config: Optional[Dict[str, Any]] = None
) -> CrossSessionMemoryService:
    """Get or create the global cross-session memory service.
    
    Args:
        config: Optional configuration (only used on first call)
        
    Returns:
        CrossSessionMemoryService instance
    """
    global _cross_session_memory

    with _cross_session_memory_factory_lock:
        if _cross_session_memory is None:
            _cross_session_memory = CrossSessionMemoryService(config)

        return _cross_session_memory
