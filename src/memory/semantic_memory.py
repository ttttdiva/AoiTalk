"""
Semantic memory manager using Mem0 with PostgreSQL backend
"""
import os
import asyncio
from typing import Dict, Any, List, Optional

try:
    from mem0 import Memory
except (ImportError, TypeError) as e:
    # qdrant-client newer versions use `X | None` syntax incompatible with Python 3.10
    Memory = None

from src.config import Config


class SemanticMemoryManager:
    """Semantic memory manager for fact extraction and knowledge storage using Mem0"""
    
    def __init__(self, config: Optional[Config] = None):
        """Initialize semantic memory manager
        
        Args:
            config: Configuration instance
        """
        self.config = config or Config()
        self.memory: Optional[Memory] = None
        self._initialized = False
        self._initialization_failed = False
        self._disabled_via_env = os.getenv("AOITALK_DISABLE_SEMANTIC_MEMORY", "0").lower() in ("1", "true", "yes")

    async def initialize(self) -> bool:
        """Initialize semantic memory
        
        Returns:
            True if initialization successful
        """
        # pgvector dependency removed.
        # Semantic memory is currently disabled pending migration to Qdrant.
        # This prevents errors during startup.
        if not self._initialization_failed:
            print("[SemanticMemoryManager] Semantic memory is temporarily disabled (migrating to Qdrant)")
            self._initialization_failed = True
            
        return False
    
    async def add_conversation_facts(self, user_id: str, character_name: str, 
                                   conversation_text: str, metadata: Optional[Dict] = None) -> bool:
        """Extract and store facts from conversation
        
        Args:
            user_id: User identifier
            character_name: Character name
            conversation_text: Conversation text to analyze
            metadata: Additional metadata
            
        Returns:
            True if successful
        """
        if not await self.initialize():
            return False
        return False
    
    async def search_semantic_facts(self, user_id: str, character_name: str,
                                  query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search semantic facts
        
        Args:
            user_id: User identifier  
            character_name: Character name
            query: Search query
            limit: Maximum results
            
        Returns:
            List of semantic facts
        """
        if not await self.initialize():
            return []
        return []
    
    async def get_user_facts(self, user_id: str, character_name: str) -> List[Dict[str, Any]]:
        """Get all facts for a specific user
        
        Args:
            user_id: User identifier
            character_name: Character name
            
        Returns:
            List of user facts
        """
        if not await self.initialize():
            return []
        return []
    
    async def update_fact(self, fact_id: str, new_content: str) -> bool:
        """Update a specific fact
        
        Args:
            fact_id: Fact identifier
            new_content: New content
            
        Returns:
            True if successful
        """
        if not await self.initialize():
            return False
        return False
    
    async def delete_fact(self, fact_id: str) -> bool:
        """Delete a specific fact
        
        Args:
            fact_id: Fact identifier
            
        Returns:
            True if successful
        """
        if not await self.initialize():
            return False
        return False
    
    async def cleanup(self):
        """Cleanup resources"""
        self._initialized = False
        self.memory = None
    
    async def process_conversation(self, user_input: str, assistant_response: str, user_id: str, character_name: str = None) -> bool:
        """Process conversation for automatic fact extraction (wrapper for add_conversation_facts)
        
        Args:
            user_input: User's input message
            assistant_response: Assistant's response message
            user_id: User identifier
            character_name: Character name (optional, defaults to "Assistant")
            
        Returns:
            True if successful
        """
        return False
