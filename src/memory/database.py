"""
Database configuration and setup for memory management
"""

import os
import asyncio
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from .models import Base
from .config import MemoryConfig
from ..utils.windows_optimization import get_windows_optimizer


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
            f"postgresql+asyncpg://{self.config.postgres_user}:"
            f"{self.config.postgres_password}@{postgres_host}:"
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
            f"postgresql://{self.config.postgres_user}:"
            f"{self.config.postgres_password}@{postgres_host}:"
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
    
    async def initialize(self, force: bool = False, max_retries: int = 10, retry_delay: float = 2.0) -> bool:
        """Initialize database tables
        
        Args:
            force: If True, run create_all even if already initialized
            max_retries: Maximum number of connection retry attempts (for Docker)
            retry_delay: Delay between retries in seconds
        
        Returns:
            bool: True if initialization succeeded
        """
        import platform
        
        # Docker environment detection
        is_docker = os.path.exists('/.dockerenv') or os.environ.get('AOITALK_DOCKER', '').lower() == 'true'
        
        # Use retry logic for Docker environment
        if is_docker:
            print("[DatabaseManager] Docker environment detected, using connection retry logic")
            for attempt in range(max_retries):
                try:
                    async with self.engine.begin() as conn:
                        await conn.run_sync(Base.metadata.create_all)
                    
                    self._initialized = True
                    print(f"[DatabaseManager] PostgreSQL database initialized (attempt {attempt + 1}/{max_retries})")
                    return True
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    is_retryable = (
                        "connection refused" in error_msg or
                        "could not connect" in error_msg or
                        "timeout" in error_msg or
                        "host" in error_msg
                    )
                    
                    if is_retryable and attempt < max_retries - 1:
                        print(f"[DatabaseManager] Connection attempt {attempt + 1}/{max_retries} failed: {e}")
                        print(f"[DatabaseManager] Retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                    else:
                        print(f"[DatabaseManager] PostgreSQL initialization failed after {attempt + 1} attempts: {e}")
                        import traceback
                        print(f"[DatabaseManager] Traceback: {traceback.format_exc()}")
                        return False
            
            return False
        
        # Non-Docker environment - original logic
        try:
            # PostgreSQL initialization - no Windows-specific timeout wrapper
            # asyncpg already has its own timeout settings from connect_args
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            
            self._initialized = True
            if not self._initialized or force:
                print("[DatabaseManager] PostgreSQL database initialized")
            return True
            
        except Exception as e:
            import traceback
            error_msg = str(e)
            
            # PostgreSQL-specific error handling
            if platform.system() == "Windows":
                if "TimeoutError" in error_msg or "timeout" in error_msg.lower():
                    print("[DatabaseManager] Database connection failed - PostgreSQL connection timeout")
                    print("[DatabaseManager] This is a known issue on Windows. Possible solutions:")
                    print("[DatabaseManager]   1. Ensure PostgreSQL service is running")
                    print("[DatabaseManager]   2. Try connecting with: psql -h 127.0.0.1 -p 5432 -U postgres")
                    print("[DatabaseManager]   3. Check Windows Firewall settings")
                    print("[DatabaseManager]   4. Verify PostgreSQL is listening on 127.0.0.1:5432")
                elif "Connection refused" in error_msg:
                    print("[DatabaseManager] Database connection failed - Connection refused")
                    print("[DatabaseManager] PostgreSQL is not accepting connections on port 5432")
                else:
                    print(f"[DatabaseManager] PostgreSQL initialization failed: {e}")
                    print(f"[DatabaseManager] Traceback: {traceback.format_exc()}")
            else:
                print(f"[DatabaseManager] PostgreSQL initialization failed: {e}")
                print(f"[DatabaseManager] Traceback: {traceback.format_exc()}")
            
            return False
    
    async def get_session(self) -> AsyncSession:
        """Get async database session
        
        Returns:
            AsyncSession: Database session
        """
        if not self._initialized:
            await self.initialize()
        
        return self.SessionLocal()
    
    def get_sync_session(self) -> Session:
        """Get synchronous database session
        
        Returns:
            Session: Synchronous database session
        """
        return self.SyncSessionLocal()
    
    async def close(self):
        """Close database connections"""
        try:
            if hasattr(self, 'engine') and self.engine:
                # Close all connections in the pool immediately
                await asyncio.wait_for(self.engine.dispose(), timeout=2.0)
                print("[DatabaseManager] Database connections closed")
        except asyncio.TimeoutError:
            print("[DatabaseManager] Database close timeout, forcing shutdown")
        except Exception as e:
            print(f"[DatabaseManager] Error closing database: {e}")
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
        print(f"[DatabaseManager] Error getting session: {e}")
        raise