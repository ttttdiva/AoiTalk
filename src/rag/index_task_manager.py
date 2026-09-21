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
        # IndexTaskManager is a process-owned background-task owner.  Once
        # shutdown starts no new work may be published, while existing tasks
        # are cancelled and awaited before shutdown returns.
        self._shutdown_started = False
        self._shutdown_complete = False
        self._shutdown_lock = asyncio.Lock()

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
        if self._shutdown_started:
            logger.warning("Indexing manager is shutting down; refusing new task")
            return None

        if collection_id in self._tasks:
            existing = self._tasks[collection_id]
            if (
                existing.status in {"pending", "running"}
                or (
                    existing._task is not None
                    and not existing._task.done()
                )
            ):
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
            ),
            name=f"rag-index:{collection_id}",
        )
        task_info._task = asyncio_task
        # Always retrieve an unexpected task exception.  _run_indexing handles
        # normal errors itself, but this callback protects the manager if a
        # future change or a cancellation-path hook raises unexpectedly.
        asyncio_task.add_done_callback(self._consume_task_exception)
        return task_info

    def _consume_task_exception(self, task: asyncio.Task) -> None:
        """Retrieve a finished task's exception without re-raising it."""
        task_info = next(
            (info for info in self._tasks.values() if info._task is task),
            None,
        )
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            # A cancelled task has no unhandled exception to report.
            if task_info is not None and task_info.completed_at is None:
                task_info.status = "cancelled"
                task_info.completed_at = datetime.utcnow()
            return
        except Exception:
            # Calling exception() is solely for retrieval; _run_indexing logs
            # and records normal failures before this callback runs.
            return

        if exception is not None:
            if task_info is not None and task_info.completed_at is None:
                task_info.status = "error"
                task_info.error_message = str(exception)
                task_info.completed_at = datetime.utcnow()
            task_name = (
                task.get_name()
                if hasattr(task, "get_name")
                else repr(task)
            )
            logger.error(
                "Unhandled exception in indexing task %s: %s",
                task_name,
                exception,
            )
        elif task_info is not None and task_info.completed_at is None:
            # A task that returns before entering _run_indexing's normal
            # completion path should not remain permanently pending.
            task_info.status = "completed"
            task_info.completed_at = datetime.utcnow()

    async def _run_indexing(
        self,
        task_info: IndexTask,
        clear_existing: bool,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
    ):
        """Execute indexing in the background with semaphore control."""
        try:
            # Keep the semaphore wait inside the cancellation/error boundary.
            # A task cancelled while queued must still transition out of its
            # initial ``pending`` state.
            async with self._semaphore:
                task_info.status = "running"
                task_info.started_at = datetime.utcnow()
                await self._update_db_status(task_info.collection_id, "indexing")

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
            # Cancellation is an expected lifecycle outcome.  Set status even
            # when cancellation happened before semaphore acquisition and
            # consume any legacy status-hook failure without masking it.
            task_info.status = "cancelled"
            task_info.completed_at = datetime.utcnow()
            try:
                await self._update_db_status(
                    task_info.collection_id,
                    "error",
                    error_message="Cancelled",
                )
            except Exception as status_error:
                logger.debug(
                    "Unable to update cancelled indexing status for %s: %s",
                    task_info.collection_id,
                    status_error,
                )
        except Exception as e:
            task_info.status = "error"
            task_info.error_message = str(e)
            task_info.completed_at = datetime.utcnow()
            try:
                await self._update_db_status(
                    task_info.collection_id, "error", error_message=str(e)
                )
            except Exception as status_error:
                logger.debug(
                    "Unable to update failed indexing status for %s: %s",
                    task_info.collection_id,
                    status_error,
                )
            logger.error(f"Indexing failed for {task_info.collection_name}: {e}")

    async def _update_db_status(
        self, collection_id: str, status: str,
        points_count: Optional[int] = None,
        error_message: Optional[str] = None,
    ):
        """Legacy hook retained for internal index tasks.

        Knowledge Source status is now updated by src.knowledge.service.
        """
        return

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
            # Mark synchronously as well as in _run_indexing's cancellation
            # handler.  A task can be cancelled before its coroutine receives
            # its first timeslice, in which case the handler never runs.
            task.status = "cancelled"
            task.completed_at = datetime.utcnow()
            task._task.cancel()
            return True
        return False

    async def shutdown(self) -> None:
        """Cancel and await every task owned by this manager.

        Completed task records are retained for status introspection, but no
        further indexing can be started after shutdown begins.
        """
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return

            self._shutdown_started = True
            owned_entries = [
                (task_info, task_info._task)
                for task_info in self._tasks.values()
                if task_info._task is not None
            ]
            owned_tasks = [task for _, task in owned_entries]

            # Request cancellation before awaiting so all work is stopped as
            # one shutdown operation.  Do not cancel external tasks: every
            # task here was created and published by start_indexing().
            for task_info, task in owned_entries:
                if task is not None and not task.done():
                    task_info.status = "cancelled"
                    task_info.completed_at = datetime.utcnow()
                    task.cancel()

            if owned_tasks:
                # return_exceptions keeps one misbehaving task from skipping
                # the await/retrieval of the remaining tasks.
                await asyncio.gather(*owned_tasks, return_exceptions=True)

            self._shutdown_complete = True


# Global instance
_index_task_manager: Optional[IndexTaskManager] = None


def get_index_task_manager() -> IndexTaskManager:
    """Get or create the global IndexTaskManager."""
    global _index_task_manager
    if _index_task_manager is None:
        _index_task_manager = IndexTaskManager()
    return _index_task_manager
