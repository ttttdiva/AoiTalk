"""
Database configuration and setup for memory management
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from .migrations import run_migrations
from .config import MemoryConfig, postgres_search_path
from ..utils.windows_optimization import get_windows_optimizer
from ..utils.logging_config import FILE_ONLY_LOG_EXTRA
from ..utils.startup_console import safe_startup_reason


logger = logging.getLogger(__name__)


class DatabaseManager:
    """Database manager for conversation memory"""
    
    def __init__(self, database_path: Optional[str] = None, config: Optional[MemoryConfig] = None):
        """Initialize database manager
        
        Args:
            database_path: Not used (kept for backward compatibility)
            config: Memory configuration. If None, uses default config.
        """
        self.config = config or MemoryConfig()
        
        # PostgreSQL configuration only
        # Windows環境では明示的に127.0.0.1を使用（IPv6回避）
        import platform
        postgres_host = self.config.postgres_host
        if platform.system() == "Windows" and postgres_host == "localhost":
            postgres_host = "127.0.0.1"
            
        self.database_url = (
            f"postgresql+asyncpg://{quote_plus(str(self.config.postgres_user))}:"
            f"{quote_plus(str(self.config.postgres_password))}@{postgres_host}:"
            f"{self.config.postgres_port}/{self.config.postgres_db}"
        )
        
        # Create async engine with PostgreSQL-specific settings
        # Windows-specific optimizations
        connect_args = {
            "command_timeout": 30,  # Reduced from 60 for faster startup
            "server_settings": {"jit": "off"}
        }
        
        # Additional Windows-specific optimizations
        import platform
        if platform.system() == "Windows":
            # Use Windows optimizer for database config
            optimizer = get_windows_optimizer()
            db_overrides = optimizer.get_database_config_overrides()
            # Remove 'timeout' from db_overrides as it conflicts with asyncpg connection parameter
            if 'timeout' in db_overrides:
                del db_overrides['timeout']
            connect_args.update(db_overrides)
        
        # Windows環境ではタイムアウトを延長
        pool_pre_ping = True if platform.system() == "Windows" else False
        
        # connect_timeoutはconnect_args内で指定する必要がある
        if platform.system() == "Windows":
            connect_args["server_settings"] = connect_args.get("server_settings", {})
            # asyncpgの場合、timeoutパラメータを使用
            if "timeout" not in connect_args:
                connect_args["timeout"] = 30

        # A configured worktree schema is a strict isolation boundary. Merge
        # it into platform-specific settings without adding ``public`` as a
        # fallback.
        search_path = postgres_search_path(self.config.postgres_schema)
        if search_path is not None:
            server_settings = connect_args.setdefault("server_settings", {})
            server_settings["search_path"] = search_path
        
        self.engine = create_async_engine(
            self.database_url,
            echo=False,  # Set to True for SQL debugging
            poolclass=NullPool,  # Use NullPool for async operations
            connect_args=connect_args,
            pool_recycle=3600,  # Recycle connections after 1 hour
            pool_pre_ping=pool_pre_ping  # Windows環境では接続確認を有効化
        )
        
        # Create async session factory
        self.SessionLocal = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,  # Avoid automatic flush
            autocommit=False  # Manual commit control
        )
        
        # Create sync engine and session factory for synchronous operations
        self.sync_database_url = (
            f"postgresql://{quote_plus(str(self.config.postgres_user))}:"
            f"{quote_plus(str(self.config.postgres_password))}@{postgres_host}:"
            f"{self.config.postgres_port}/{self.config.postgres_db}"
        )
        
        # Sync engine with Windows optimizations
        sync_connect_args = {}
        if platform.system() == "Windows":
            # Use Windows optimizer for sync database config
            # Note: psycopg2 uses different parameter names than asyncpg
            sync_connect_args = {
                'connect_timeout': 30,  # Windows環境では30秒に延長
                'options': '-c tcp_keepalives_idle=600 -c tcp_keepalives_interval=30 -c tcp_keepalives_count=3'
            }

        # psycopg2 receives PostgreSQL session settings through the ``options``
        # connection argument.  Append rather than replace the Windows
        # keepalive options above, and leave the legacy empty mapping untouched
        # when schema isolation is disabled.
        if search_path is not None:
            search_option = f"-c search_path={search_path}"
            existing_options = sync_connect_args.get("options")
            sync_connect_args["options"] = (
                f"{existing_options} {search_option}" if existing_options else search_option
            )
        
        self.sync_engine = create_engine(
            self.sync_database_url,
            echo=False,
            pool_pre_ping=True,
            connect_args=sync_connect_args,
            pool_recycle=3600
        )
        
        self.SyncSessionLocal = sessionmaker(
            bind=self.sync_engine,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False
        )
        
        self._initialized = False
        self.last_error: str | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self, force: bool = False, max_retries: int = 10, retry_delay: float = 2.0) -> bool:
        """Initialize the database once, serializing concurrent callers."""
        if self._initialized and not force:
            return True

        self.last_error = None
        async with self._initialize_lock:
            if self._initialized and not force:
                return True
            return await self._initialize_unlocked(
                force=force,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )

    async def _initialize_unlocked(
        self,
        force: bool = False,
        max_retries: int = 10,
        retry_delay: float = 2.0,
    ) -> bool:
        """Initialize database tables

        Args:
            force: If True, run Alembic verification even if already initialized
            max_retries: Maximum number of connection retry attempts (for Docker)
            retry_delay: Delay between retries in seconds
        
        Returns:
            bool: True if initialization succeeded
        """
        import platform
        
        if self._initialized and not force:
            return True

        # Docker environment detection
        is_docker = os.path.exists('/.dockerenv') or os.environ.get('AOITALK_DOCKER', '').lower() == 'true'
        
        # Use retry logic for Docker environment
        if is_docker:
            logger.info(
                "Docker environment detected; using database connection retries",
            )
            for attempt in range(max_retries):
                try:
                    migrated = await asyncio.to_thread(
                        run_migrations,
                        self.sync_database_url,
                        self.config.postgres_schema,
                    )
                    if not migrated:
                        raise RuntimeError("Alembic migration runner returned false")

                    self._initialized = True
                    logger.info(
                        "PostgreSQL database initialized (attempt %s/%s)",
                        attempt + 1,
                        max_retries,
                    )
                    return True
                    
                except Exception as e:
                    self.last_error = safe_startup_reason(e, limit=180)
                    error_msg = str(e).lower()
                    is_retryable = (
                        "connection refused" in error_msg or
                        "could not connect" in error_msg or
                        "timeout" in error_msg or
                        "host" in error_msg
                    )

                    if is_retryable and attempt < max_retries - 1:
                        logger.warning(
                            "PostgreSQL connection attempt %s/%s failed; retrying in %ss",
                            attempt + 1,
                            max_retries,
                            retry_delay,
                            exc_info=True,
                            extra=FILE_ONLY_LOG_EXTRA,
                        )
                        await asyncio.sleep(retry_delay)
                    else:
                        logger.error(
                            "PostgreSQL initialization failed after %s attempt(s)",
                            attempt + 1,
                            exc_info=True,
                            extra=FILE_ONLY_LOG_EXTRA,
                        )
                        return False
            
            return False
        
        # Non-Docker environment - original logic
        try:
            migrated = await asyncio.to_thread(
                run_migrations,
                self.sync_database_url,
                self.config.postgres_schema,
            )
            if not migrated:
                raise RuntimeError("Alembic migration runner returned false")

            self._initialized = True
            logger.info(
                "PostgreSQL database initialized",
            )
            return True

        except Exception as e:
            error_msg = str(e)
            self.last_error = safe_startup_reason(e, limit=180)

            # PostgreSQL-specific error handling
            if platform.system() == "Windows":
                if "TimeoutError" in error_msg or "timeout" in error_msg.lower():
                    logger.warning(
                        "PostgreSQL connection timed out during initialization",
                        exc_info=True,
                        extra=FILE_ONLY_LOG_EXTRA,
                    )
                elif "Connection refused" in error_msg:
                    logger.warning(
                        "PostgreSQL refused the connection during initialization",
                        exc_info=True,
                        extra=FILE_ONLY_LOG_EXTRA,
                    )
                else:
                    logger.error(
                        "PostgreSQL initialization failed",
                        exc_info=True,
                        extra=FILE_ONLY_LOG_EXTRA,
                    )
            else:
                logger.error(
                    "PostgreSQL initialization failed",
                    exc_info=True,
                    extra=FILE_ONLY_LOG_EXTRA,
                )
            
            return False
    
    async def get_session(self) -> AsyncSession:
        """Get async database session
        
        Returns:
            AsyncSession: Database session
        """
        if not self._initialized:
            initialized = await self.initialize()
            if not initialized:
                raise RuntimeError("Database is not initialized; refusing to open a session")
        
        return self.SessionLocal()
    
    def get_sync_session(self) -> Session:
        """Get synchronous database session
        
        Returns:
            Session: Synchronous database session
        """
        if not self._initialized:
            raise RuntimeError("Database is not initialized; refusing to open a sync session")
        return self.SyncSessionLocal()
    
    async def close(self):
        """Close database connections"""
        try:
            if hasattr(self, 'engine') and self.engine:
                # Close all connections in the pool immediately
                await asyncio.wait_for(self.engine.dispose(), timeout=2.0)
                logger.info(
                    "Database connections closed",
                )
        except asyncio.TimeoutError:
            logger.warning(
                "Database close timed out; forcing shutdown",
                extra=FILE_ONLY_LOG_EXTRA,
            )
        except Exception as e:
            logger.warning(
                "Database close failed; forcing shutdown",
                exc_info=True,
                extra=FILE_ONLY_LOG_EXTRA,
            )
        finally:
            # Force cleanup
            try:
                if hasattr(self, 'engine'):
                    del self.engine
            except:
                pass
    
    def is_initialized(self) -> bool:
        """Check if database is initialized
        
        Returns:
            bool: True if database is initialized
        """
        return self._initialized


# Global database manager instance
_db_manager: Optional[DatabaseManager] = None


def get_database_manager(database_path: Optional[str] = None, config: Optional[MemoryConfig] = None) -> DatabaseManager:
    """Get global database manager instance
    
    Args:
        database_path: Path to database file (only used on first call)
        config: Memory configuration (only used on first call)
        
    Returns:
        DatabaseManager: Global database manager instance
    """
    global _db_manager
    
    if _db_manager is None:
        _db_manager = DatabaseManager(database_path, config)
    
    return _db_manager


async def init_database(database_path: Optional[str] = None) -> bool:
    """Initialize database with tables
    
    Args:
        database_path: Path to database file
        
    Returns:
        bool: True if initialization succeeded
    """
    db_manager = get_database_manager(database_path)
    return await db_manager.initialize()


async def get_db_session() -> AsyncSession:
    """Get database session
    
    Returns:
        AsyncSession: Database session
    """
    try:
        db_manager = get_database_manager()
        return await db_manager.get_session()
    except Exception as e:
        logger.error(
            "Unable to open a database session",
            exc_info=True,
            extra=FILE_ONLY_LOG_EXTRA,
        )
        raise
