"""
Semantic memory tools using Mem0 for knowledge storage and retrieval
Note: Function calling tools removed - Mem0 now operates in automatic mode only
"""
from typing import Dict, Any, List, Optional
# Conditionally import semantic memory to avoid SQLite issues
try:
    from src.memory.semantic_memory import SemanticMemoryManager
    SEMANTIC_MEMORY_AVAILABLE = True
except (ImportError, Exception) as e:
    print(f"[semantic_memory_tools] Semantic memory not available: {e}")
    SEMANTIC_MEMORY_AVAILABLE = False
    SemanticMemoryManager = None


# Global semantic memory manager
_semantic_memory_manager: Optional[SemanticMemoryManager] = None


def get_semantic_memory_manager() -> Optional[SemanticMemoryManager]:
    """Get global semantic memory manager instance"""
    global _semantic_memory_manager
    
    if not SEMANTIC_MEMORY_AVAILABLE or SemanticMemoryManager is None:
        return None
    
    if _semantic_memory_manager is None:
        try:
            _semantic_memory_manager = SemanticMemoryManager()
        except Exception as e:
            print(f"[semantic_memory_tools] Failed to initialize semantic memory manager: {e}")
            return None
    
    return _semantic_memory_manager


# Note: All @function_tool decorators removed
# Mem0 now operates automatically through:
# 1. Auto-extraction in _auto_extract_semantic_facts()
# 2. Auto-injection in _retrieve_relevant_memories() 
# No manual function calling tools needed