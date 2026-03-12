"""
Background index task manager for RAG collections.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


@dataclass
class IndexTask:
    """Represents a running or completed indexing task."""
    collection_id: str
    collection_name: str
    source_directory: str
    status: str = "pending"  # pending, running, completed, error, cancelled
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    files_processed: int = 0
    total_chunks: int = 0
    _task: Optional[asyncio.Task] = field(default=None, repr=False)

    def to_dict(self) -> Dict:
        return {
            "collection_id": self.collection_id,
            "collection_name": self.collection_name,
            "source_directory": self.source_directory,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
            "files_processed": self.files_processed,
            "total_chunks": self.total_chunks,
        }


class IndexTaskManager:
    """Manages background indexing tasks. Only one task runs at a time."""

    def __init__(self):
        self._tasks: Dict[str, IndexTask] = {}
        self._semaphore = asyncio.Semaphore(1)
        self._db_manager = None

    def set_db_manager(self, db_manager):
        """Set database manager for status updates."""
        self._db_manager = db_manager

    async def start_indexing(
        self,
        collection_id: str,
        collection_name: str,
        source_directory: str,
        clear_existing: bool = False,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ) -> Optional[IndexTask]:
        """Start a background indexing task.

        Returns IndexTask if started, None if already running for this collection.
        """
        if collection_id in self._tasks:
            existing = self._tasks[collection_id]
            if existing.status == "running":
                logger.warning(f"Indexing already running for collection {collection_id}")
                return None

        task_info = IndexTask(
            collection_id=collection_id,
            collection_name=collection_name,
            source_directory=source_directory,
        )
        self._tasks[collection_id] = task_info

        asyncio_task = asyncio.create_task(
            self._run_indexing(
                task_info, clear_existing, include_patterns, exclude_patterns
            )
        )
        task_info._task = asyncio_task
        return task_info

    async def _run_indexing(
        self,
        task_info: IndexTask,
        clear_existing: bool,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
    ):
        """Execute indexing in the background with semaphore control."""
        async with self._semaphore:
            task_info.status = "running"
            task_info.started_at = datetime.utcnow()
            await self._update_db_status(task_info.collection_id, "indexing")

            try:
                from .manager import get_rag_manager_for_collection
                from .config import SourceConfig

                manager = get_rag_manager_for_collection(task_info.collection_name)

                # Override source config if patterns provided
                if include_patterns or exclude_patterns:
                    config = manager.config
                    if include_patterns:
                        config.source.include_patterns = include_patterns
                    if exclude_patterns:
                        config.source.exclude_patterns = exclude_patterns

                if not await manager.initialize():
                    raise RuntimeError("Failed to initialize RAG manager")

                if clear_existing:
                    await manager.clear_index()
                    manager._initialized = False
                    await manager.initialize()

                results = await manager.index_directory(
                    task_info.source_directory, recursive=True
                )

                task_info.files_processed = len(results)
                task_info.total_chunks = sum(results.values())
                task_info.status = "completed"
                task_info.completed_at = datetime.utcnow()

                # Get actual point count from Qdrant
                info = await manager.get_collection_info()
                points_count = info.get("points_count", 0) if info else task_info.total_chunks

                await self._update_db_status(
                    task_info.collection_id, "ready", points_count=points_count
                )
                logger.info(
                    f"Indexing completed for {task_info.collection_name}: "
                    f"{task_info.files_processed} files, {task_info.total_chunks} chunks"
                )

            except asyncio.CancelledError:
                task_info.status = "cancelled"
                task_info.completed_at = datetime.utcnow()
                await self._update_db_status(task_info.collection_id, "error",
                                             error_message="Cancelled")
            except Exception as e:
                task_info.status = "error"
                task_info.error_message = str(e)
                task_info.completed_at = datetime.utcnow()
                await self._update_db_status(
                    task_info.collection_id, "error", error_message=str(e)
                )
                logger.error(f"Indexing failed for {task_info.collection_name}: {e}")

    async def _update_db_status(
        self, collection_id: str, status: str,
        points_count: Optional[int] = None,
        error_message: Optional[str] = None,
    ):
        """Update collection status in the database."""
        if self._db_manager is None:
            return
        try:
            from ..memory.rag_collection_repository import RagCollectionRepository
            from uuid import UUID

            async with self._db_manager.get_session() as session:
                await RagCollectionRepository.update_status(
                    session, UUID(collection_id), status,
                    points_count=points_count, error_message=error_message
                )
        except Exception as e:
            logger.error(f"Failed to update DB status: {e}")

    def get_task_status(self, collection_id: str) -> Optional[Dict]:
        """Get current status of an indexing task."""
        task = self._tasks.get(collection_id)
        if task:
            return task.to_dict()
        return None

    def cancel_task(self, collection_id: str) -> bool:
        """Cancel a running task."""
        task = self._tasks.get(collection_id)
        if task and task._task and not task._task.done():
            task._task.cancel()
            return True
        return False


# Global instance
_index_task_manager: Optional[IndexTaskManager] = None


def get_index_task_manager() -> IndexTaskManager:
    """Get or create the global IndexTaskManager."""
    global _index_task_manager
    if _index_task_manager is None:
        _index_task_manager = IndexTaskManager()
    return _index_task_manager
