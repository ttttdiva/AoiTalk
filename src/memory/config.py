"""
Configuration for memory management system
"""

from dataclasses import dataclass
from typing import Optional
import os


@dataclass
class MemoryConfig:
    """Configuration for conversation memory management"""
    
    # Message management
    max_active_messages: int = 50           # Trigger summarization at this count
    summary_overlap: int = 5                # Keep this many messages after summarization
    max_context_tokens: int = 8000         # Maximum context size in tokens
    
    # Embedding and search
    embedding_model: str = "all-MiniLM-L6-v2"  # Sentence transformer model
    preload_embedding_model: bool = False  # Preload model at startup to avoid first-use delay
    enable_search: bool = False            # Enable/disable memory search functionality (controls embedding model loading)
    search_timeout: float = 3.0            # Search timeout in seconds (changed from 30.0)
    max_search_results: int = 10           # Maximum search results to return (changed from 5)
    similarity_threshold: float = 0.3      # Minimum similarity score for search results
    
    # Summarization
    max_summary_retries: int = 3           # Retry count for summarization
    summary_max_tokens: int = 500          # Maximum tokens for summary
    
    # History management
    history_retention_days: int = 180      # Keep history for this many days
    enable_history_logging: bool = True    # Enable/disable history logging
    history_batch_size: int = 100         # Batch size for history operations
    
    # PostgreSQL configuration
    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_db: str = os.getenv("POSTGRES_DB", "aoitalk_memory")
    postgres_user: str = os.getenv("POSTGRES_USER", "aoitalk")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "")
    
    # Database compatibility - needed for legacy code
    database_path: Optional[str] = None  # Not used for PostgreSQL, but needed for compatibility
    
    # Conversation logging
    conversation_logging_enabled: bool = True
    
    # Cache settings
    cache_ttl: int = 3600                  # Cache TTL in seconds
    enable_hybrid_search: bool = True      # Enable hybrid search
    
    # LLM provider for summarization
    llm_provider: str = "gemini"          # "openai" or "gemini"
    llm_model: str = "gemini-3-flash-preview"  # Model for summarization
    
    # Conversation logging settings (now unified with memory_enabled)
    save_user_messages: bool = True
    save_assistant_messages: bool = True
    save_system_messages: bool = False
    save_function_calls: bool = True
    save_successful_only: bool = False
    log_retention_days: int = 365
    auto_cleanup_enabled: bool = True
    exclude_patterns: list = None
    
    def __post_init__(self):
        """Validate configuration after initialization"""
        if self.max_active_messages < 5:
            raise ValueError("max_active_messages must be at least 5")
        
        if self.summary_overlap >= self.max_active_messages:
            raise ValueError("summary_overlap must be less than max_active_messages")
        
        if self.similarity_threshold < 0 or self.similarity_threshold > 1:
            raise ValueError("similarity_threshold must be between 0 and 1")
        
        if self.exclude_patterns is None:
            self.exclude_patterns = []
        
        if self.log_retention_days < 0:
            raise ValueError("log_retention_days must be non-negative")
