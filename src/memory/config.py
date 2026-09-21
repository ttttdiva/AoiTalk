"""
Configuration for memory management system
"""

from dataclasses import dataclass, field
from typing import Optional
import os
from pathlib import Path
import re

from dotenv import load_dotenv

from ..security.secret_env import load_secret_environment


load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_secret_environment()


# PostgreSQL identifiers used in ``search_path`` must be constrained before
# interpolation into a connection setting.  Keep the accepted form narrow and
# portable: an ASCII letter/underscore followed by ASCII letters, digits, or
# underscores.  Quoted, qualified, or otherwise complex identifiers are not
# accepted by this optional isolation seam.
_POSTGRES_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_postgres_schema(schema: Optional[str]) -> Optional[str]:
    """Validate and return an optional PostgreSQL schema identifier.

    ``None`` means schema isolation is disabled and preserves the historical
    connection behavior.  Any supplied value must be a single, unquoted
    PostgreSQL identifier; invalid values fail closed instead of being
    interpolated into ``search_path`` options.
    """

    if schema is None:
        return None
    if not isinstance(schema, str) or _POSTGRES_SCHEMA_RE.fullmatch(schema) is None:
        raise ValueError(
            "postgres_schema must be a simple PostgreSQL identifier "
            "(ASCII letter/underscore followed by ASCII letters, digits, or underscores)"
        )
    return schema


def postgres_search_path(schema: Optional[str]) -> Optional[str]:
    """Return the safe ``search_path`` value for an optional schema."""

    validated = validate_postgres_schema(schema)
    if validated is None:
        return None
    # A configured schema is an isolation boundary, not merely a preferred
    # namespace. Falling back to ``public`` would make Alembic inspect the
    # user's ordinary tables and would let a QA worktree access them whenever
    # an isolated table is missing. PostgreSQL searches ``pg_catalog``
    # implicitly, so built-in functions remain available without that
    # fallback.
    return validated


_ISOLATED_SCHEMA_SAFE_REVISIONS = {
    "20260830_0001",
    "20260830_0002",
    "20260830_0003",
    "20260830_0004",
    "20260830_0005",
}


def validate_isolated_schema_heads(
    heads: tuple[str, ...],
    *,
    safe_revisions: set[str] | None = None,
) -> None:
    """Fail closed before old public-qualified migrations can run.

    Historical migrations before ``20260830_0001`` contain explicit
    ``public.*`` references and are not safe to replay in a schema-isolated
    worktree.  An isolated schema must therefore be provisioned from a clean,
    schema-only baseline and stamped at the baseline (or already be at this
    feature head) before Alembic may run there.
    """

    allowed = safe_revisions or _ISOLATED_SCHEMA_SAFE_REVISIONS
    if len(heads) != 1 or heads[0] not in allowed:
        raise RuntimeError(
            "POSTGRES_SCHEMA requires a pre-provisioned schema-only baseline "
            "at revision 20260830_0001; historical migrations are not "
            "schema-isolation safe"
        )


@dataclass
class MemoryConfig:
    """Configuration for conversation memory management"""
    
    # Message management
    max_active_messages: int = 50           # Trigger summarization at this count
    summary_overlap: int = 5                # Keep this many messages after summarization
    max_context_tokens: int = 8000         # Maximum context size in tokens
    
    # Semantic search is owned by CrossSessionMemoryService and src.rag using
    # BAAI/bge-m3 embeddings with Qdrant.  Keep search controls here, but do not
    # expose the retired SentenceTransformer model/preload settings.
    enable_search: bool = True             # Enable memory search tools; heavy work stays lazy/background
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
    # Optional per-worktree PostgreSQL schema isolation.  A default factory
    # reads the environment at instance creation so tests and callers that
    # configure the process after module import observe the same setting.
    postgres_schema: Optional[str] = field(
        default_factory=lambda: os.getenv("POSTGRES_SCHEMA")
    )
    
    # Database compatibility - needed for legacy code
    database_path: Optional[str] = None  # Not used for PostgreSQL, but needed for compatibility
    
    # Conversation logging
    conversation_logging_enabled: bool = True
    
    # Cache settings
    cache_ttl: int = 3600                  # Cache TTL in seconds
    enable_hybrid_search: bool = True      # Enable hybrid search
    
    # LLM provider metadata. Runtime memory extraction uses the active LLM client.
    llm_provider: str = "active"
    llm_model: str = ""
    
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
        # Keep this optional and fail closed when explicitly supplied.  An
        # absent value remains exactly the legacy behavior.
        self.postgres_schema = validate_postgres_schema(self.postgres_schema)

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
