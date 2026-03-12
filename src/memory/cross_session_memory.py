"""
Cross-session memory service for retrieving relevant past conversations.

This service enables the AI to recall and utilize information from past conversation
sessions to provide more contextual and informed responses.
"""

import re
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

# Keywords that trigger past conversation lookup
TRIGGER_KEYWORDS_JA = [
    "前に", "以前", "また", "覚えて", "話した", "言った", "教えた", 
    "約束", "頼んだ", "お願いした", "聞いた", "質問した"
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


class CrossSessionMemoryService:
    """Service for retrieving relevant past conversations across sessions."""
    
    # Class-level cache for shared components
    _shared_embedding = None
    _shared_qdrant = None
    
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
        
        # Configuration
        self.min_relevance_score = self.config.get('min_relevance_score', 0.3)
        self.max_results = self.config.get('max_results', 5)
        self.max_context_messages = self.config.get('max_context_messages', 10)
    
    async def initialize(self) -> bool:
        """Initialize the service components.
        
        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True
        
        try:
            # Use shared RAG infrastructure
            from ..rag.embedding import BgeM3Embedding
            from ..rag.qdrant_client import QdrantManager
            from ..rag.config import QdrantConfig, get_rag_config
            
            # Get RAG config
            rag_config = get_rag_config()
            
            # Use shared embedding model if available
            if CrossSessionMemoryService._shared_embedding is None:
                from ..rag.manager import _shared_embedding
                if _shared_embedding is not None:
                    CrossSessionMemoryService._shared_embedding = _shared_embedding
                else:
                    # Create new embedding model
                    CrossSessionMemoryService._shared_embedding = BgeM3Embedding(rag_config.embedding)
                    await CrossSessionMemoryService._shared_embedding.initialize()
            
            self.embedding = CrossSessionMemoryService._shared_embedding
            
            # Create Qdrant manager for conversation memory collection
            qdrant_config = QdrantConfig(
                host=rag_config.qdrant.host,
                port=rag_config.qdrant.port,
                collection_name=self.COLLECTION_NAME,
                local_path=rag_config.qdrant.local_path
            )
            
            self.qdrant = QdrantManager(qdrant_config, vector_size=1024)  # BGE-M3 uses 1024 dim
            await self.qdrant.initialize()
            
            self._initialized = True
            logger.info(f"[CrossSessionMemory] Initialized with collection: {self.COLLECTION_NAME}")
            return True
            
        except Exception as e:
            logger.error(f"[CrossSessionMemory] Initialization failed: {e}")
            return False
    
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
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Search for relevant past conversations.
        
        Args:
            user_id: User identifier
            query: Search query (usually user's current input)
            current_session_id: Current session ID to exclude from results
            limit: Maximum number of results
            
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
            filter_conditions = {"user_id": user_id}
            
            results = await self.qdrant.search(
                query_embedding=query_embedding,
                top_k=limit * 2,  # Get more to filter
                filter_conditions=filter_conditions
            )
            
            # Filter and format results
            formatted_results = []
            for result in results:
                # Skip current session messages
                if current_session_id and result.metadata.get("session_id") == current_session_id:
                    continue
                
                # Check relevance threshold
                if result.score < self.min_relevance_score:
                    continue
                
                formatted_results.append({
                    "content": result.text,
                    "role": result.metadata.get("role", "unknown"),
                    "session_id": result.metadata.get("session_id"),
                    "timestamp": result.metadata.get("timestamp"),
                    "relevance_score": result.score,
                    "character_name": result.metadata.get("character_name")
                })
                
                if len(formatted_results) >= limit:
                    break
            
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
                "character_name": character_name or "unknown",
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
    
    if _cross_session_memory is None:
        _cross_session_memory = CrossSessionMemoryService(config)
    
    return _cross_session_memory
