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
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

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


async def _legacy_message_matches_scope(
    metadata: Dict[str, Any],
    *,
    user_id: str,
    character_name: str,
    project_id: Optional[str],
) -> bool:
    """Validate pre-project-metadata Qdrant points against the SQL canonical row."""
    session_id = metadata.get("session_id")
    if not session_id:
        return False
    try:
        from .database import get_db_session
        from .models import ConversationSession

        async with await get_db_session() as database:
            conversation = await database.get(
                ConversationSession,
                uuid.UUID(str(session_id)),
            )
        if conversation is None:
            return False
        expected_project = str(project_id) if project_id else None
        actual_project = (
            str(conversation.project_id) if conversation.project_id else None
        )
        return (
            str(conversation.user_id) == str(user_id)
            and _character_slugs_match(conversation.character_name, character_name)
            and actual_project == expected_project
        )
    except Exception:
        logger.warning(
            "[CrossSessionMemory] legacy scope validation failed",
            exc_info=True,
        )
        return False

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
            from ..rag.embedding import BgeM3Embedding
            from ..rag.qdrant_client import QdrantManager
            from ..rag.config import QdrantConfig, get_rag_config

            rag_config = get_rag_config()

            # A failed shared object is discarded rather than becoming a
            # sticky cache entry; successful BGE instances may be reused.
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
                        shared_embedding = BgeM3Embedding(rag_config.embedding)

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
                qdrant_config, vector_size=1024
            )  # BGE-M3 uses 1024 dim
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
                return []
        
        limit = limit or self.max_results
        
        try:
            # Generate query embedding
            query_embedding = await self.embedding.embed_query(query)
            
            if not query_embedding:
                return []
            
            # Search in Qdrant
            scoped_results = []
            for scoped_character in _character_slug_candidates(character_name):
                filter_conditions = {
                    "user_id": user_id,
                    "character_name": scoped_character,
                    "project_id": str(project_id) if project_id else NO_PROJECT_SCOPE,
                }
                scoped_results.extend(
                    await self.qdrant.search(
                        query_embedding=query_embedding,
                        top_k=limit * 2,  # Get more to filter
                        filter_conditions=filter_conditions,
                    )
                )
            result_sources = [(result, False) for result in scoped_results]
            # Older points predate project_id metadata.  Always fetch a bounded
            # broad supplement because scoped hits may later be discarded as
            # current-session, below-threshold, or duplicate.  Every broad hit
            # is still scope-validated below; missing metadata is never guessed.
            legacy_results = await self.qdrant.search(
                query_embedding=query_embedding,
                top_k=limit * 10,
                filter_conditions={"user_id": user_id},
            )
            result_sources.extend(
                (result, True) for result in legacy_results
            )
            
            # Filter and format results
            deduped_results: dict[tuple[str, str], Dict[str, Any]] = {}
            for result, legacy_fallback in result_sources:
                if legacy_fallback:
                    metadata = result.metadata or {}
                    stored_project = metadata.get("project_id")
                    stored_character = metadata.get("character_name")
                    if stored_project is not None:
                        if (
                            stored_project
                            != (
                                str(project_id)
                                if project_id
                                else NO_PROJECT_SCOPE
                            )
                            or not _character_slugs_match(
                                stored_character,
                                character_name,
                            )
                        ):
                            continue
                    elif not await _legacy_message_matches_scope(
                        metadata,
                        user_id=user_id,
                        character_name=character_name or "unknown",
                        project_id=project_id,
                    ):
                        continue
                # Skip current session messages
                if current_session_id and result.metadata.get("session_id") == current_session_id:
                    continue
                
                # Check relevance threshold
                if result.score < self.min_relevance_score:
                    continue
                dedupe_key = (
                    str(result.metadata.get("session_id") or ""),
                    str(result.text or ""),
                )
                candidate = {
                    "content": result.text,
                    "role": result.metadata.get("role", "unknown"),
                    "session_id": result.metadata.get("session_id"),
                    "timestamp": result.metadata.get("timestamp"),
                    "relevance_score": result.score,
                    "character_name": _canonical_character_slug(
                        result.metadata.get("character_name")
                    ),
                    "project_id": (
                        None
                        if result.metadata.get("project_id") == NO_PROJECT_SCOPE
                        else result.metadata.get("project_id")
                    ),
                }
                existing = deduped_results.get(dedupe_key)
                if (
                    existing is None
                    or candidate["relevance_score"]
                    > existing["relevance_score"]
                ):
                    deduped_results[dedupe_key] = candidate

            formatted_results = sorted(
                deduped_results.values(),
                key=lambda item: item["relevance_score"],
                reverse=True,
            )[:limit]
            
            logger.info(f"[CrossSessionMemory] Found {len(formatted_results)} relevant messages for user {user_id}")
            return formatted_results
            
        except Exception as e:
            logger.error(f"[CrossSessionMemory] Search failed: {e}")
            return []
    
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
    
    async def index_message(
        self,
        message_id: str,
        session_id: str,
        user_id: str,
        role: str,
        content: str,
        character_name: Optional[str] = None,
        project_id: Optional[str] = None,
        timestamp: Optional[datetime] = None
    ) -> bool:
        """Index a conversation message for future retrieval.
        
        Args:
            message_id: Unique message identifier
            session_id: Session identifier
            user_id: User identifier
            role: Message role (user/assistant)
            content: Message content
            character_name: Optional character name
            project_id: Optional project identifier. None is stored explicitly.
            timestamp: Optional timestamp
            
        Returns:
            True if indexing successful
        """
        if not self._initialized:
            if not await self.initialize():
                return False
        
        # Skip empty or very short messages
        if not content or len(content.strip()) < 10:
            return False
        
        # Skip certain types of content
        skip_patterns = [
            r"^\[.*\]$",  # System tags like [GENERATED_IMAGE:...]
            r"^(ok|はい|うん|そうだね|なるほど)$",  # Very short responses
        ]
        for pattern in skip_patterns:
            if re.match(pattern, content.strip(), re.IGNORECASE):
                return False
        
        try:
            # Generate embedding
            embedding = await self.embedding.embed_query(content)
            
            if not embedding:
                return False
            
            # Prepare metadata
            metadata = {
                "session_id": session_id,
                "user_id": user_id,
                "role": role,
                "character_name": _canonical_character_slug(character_name),
                "project_id": str(project_id) if project_id else NO_PROJECT_SCOPE,
                "timestamp": timestamp.isoformat() if timestamp else datetime.now().isoformat()
            }
            
            # Add to Qdrant
            success = await self.qdrant.add_documents(
                ids=[message_id],
                embeddings=[embedding],
                texts=[content],
                metadata_list=[metadata]
            )
            
            if success:
                logger.debug(f"[CrossSessionMemory] Indexed message {message_id}")
            
            return success
            
        except Exception as e:
            logger.error(f"[CrossSessionMemory] Failed to index message: {e}")
            return False
    
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
