"""
Feedback management module for AoiTalk.
Handles saving and loading user feedback on agent responses.

Uses PostgreSQL database for storage (migrated from JSONL).
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Check if database is available
try:
    from ..memory.database import get_database_manager
    from ..memory.feedback_repository import FeedbackRepository
    from ..memory.models import Feedback
    DATABASE_AVAILABLE = True
except ImportError:
    DATABASE_AVAILABLE = False
    FeedbackRepository = None
    Feedback = None


class FeedbackRequest(BaseModel):
    """API request model for feedback submission."""
    message: str
    character: Optional[str] = None
    user_input: Optional[str] = None
    category: str = Field(
        default="other",
        description="Feedback category: incorrect, incomplete, slow, other"
    )
    comment: Optional[str] = None
    session_id: Optional[str] = None  # クライアントから送信（バックアップ用）


class FeedbackEntry(BaseModel):
    """Complete feedback entry for API responses."""
    id: str
    timestamp: str
    session_id: Optional[str] = None
    message: str
    character: Optional[str] = None
    user_input: Optional[str] = None
    category: str
    comment: Optional[str] = None
    resolved: bool = False


async def save_feedback_async(request: FeedbackRequest) -> FeedbackEntry:
    """
    Save a feedback entry to the database (async version).
    
    Args:
        request: The feedback request from the API.
        
    Returns:
        The complete feedback entry that was saved.
    """
    if not DATABASE_AVAILABLE:
        logger.warning("Database not available, using fallback JSONL storage")
        return _save_feedback_jsonl_fallback(request)
    
    # Get session ID
    from ..utils.app_session import get_session_id
    session_id = request.session_id or get_session_id()
    
    try:
        db_manager = get_database_manager()
        # Always ensure database tables exist (create_all is idempotent)
        await db_manager.initialize()
        session = await db_manager.get_session()
        
        try:
            feedback = await FeedbackRepository.create(
                session=session,
                message=request.message,
                category=request.category,
                session_id=session_id,
                character=request.character,
                user_input=request.user_input,
                comment=request.comment
            )
            
            return FeedbackEntry(
                id=feedback.id,
                timestamp=feedback.created_at.isoformat() if feedback.created_at else datetime.now().isoformat(),
                session_id=feedback.session_id,
                message=feedback.message,
                character=feedback.character,
                user_input=feedback.user_input,
                category=feedback.category,
                comment=feedback.comment,
                resolved=feedback.resolved
            )
        finally:
            await session.close()
            
    except Exception as e:
        logger.error(f"Failed to save feedback to database: {e}")
        # Fallback to JSONL
        return _save_feedback_jsonl_fallback(request)


def save_feedback(request: FeedbackRequest) -> FeedbackEntry:
    """
    Save a feedback entry (sync wrapper for backward compatibility).
    
    For async contexts, use save_feedback_async() instead.
    """
    import asyncio
    
    try:
        loop = asyncio.get_running_loop()
        # If already in async context, schedule coroutine
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                save_feedback_async(request)
            )
            return future.result()
    except RuntimeError:
        # No running loop, run synchronously
        return asyncio.run(save_feedback_async(request))


async def load_feedback_async(
    include_resolved: bool = False,
    limit: int = 100
) -> List[FeedbackEntry]:
    """
    Load feedback entries from the database (async version).
    
    Args:
        include_resolved: Whether to include resolved feedback.
        limit: Maximum number of entries to return.
        
    Returns:
        List of feedback entries, newest first.
    """
    if not DATABASE_AVAILABLE:
        logger.warning("Database not available, using fallback JSONL storage")
        return _load_feedback_jsonl_fallback(include_resolved, limit)
    
    try:
        db_manager = get_database_manager()
        # Always ensure database tables exist (create_all is idempotent)
        await db_manager.initialize()
        session = await db_manager.get_session()
        
        try:
            feedback_list, _ = await FeedbackRepository.list_feedback(
                session=session,
                limit=limit,
                include_resolved=include_resolved
            )
            
            return [
                FeedbackEntry(
                    id=f.id,
                    timestamp=f.created_at.isoformat() if f.created_at else "",
                    session_id=f.session_id,
                    message=f.message,
                    character=f.character,
                    user_input=f.user_input,
                    category=f.category,
                    comment=f.comment,
                    resolved=f.resolved
                )
                for f in feedback_list
            ]
        finally:
            await session.close()
            
    except Exception as e:
        logger.error(f"Failed to load feedback from database: {e}")
        return _load_feedback_jsonl_fallback(include_resolved, limit)


def load_feedback(
    include_resolved: bool = False,
    limit: int = 100
) -> List[FeedbackEntry]:
    """
    Load feedback entries (sync wrapper for backward compatibility).
    """
    import asyncio
    
    try:
        loop = asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                load_feedback_async(include_resolved, limit)
            )
            return future.result()
    except RuntimeError:
        return asyncio.run(load_feedback_async(include_resolved, limit))


async def mark_feedback_resolved_async(feedback_id: str) -> bool:
    """
    Mark a feedback entry as resolved (async version).
    
    Args:
        feedback_id: The ID of the feedback to resolve.
        
    Returns:
        True if successful, False otherwise.
    """
    if not DATABASE_AVAILABLE:
        logger.warning("Database not available, using fallback JSONL storage")
        return _mark_feedback_resolved_jsonl_fallback(feedback_id)
    
    try:
        db_manager = get_database_manager()
        # Always ensure database tables exist (create_all is idempotent)
        await db_manager.initialize()
        session = await db_manager.get_session()
        
        try:
            return await FeedbackRepository.mark_resolved(session, feedback_id)
        finally:
            await session.close()
            
    except Exception as e:
        logger.error(f"Failed to mark feedback as resolved: {e}")
        return False


def mark_feedback_resolved(feedback_id: str) -> bool:
    """
    Mark a feedback entry as resolved (sync wrapper).
    """
    import asyncio
    
    try:
        loop = asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                mark_feedback_resolved_async(feedback_id)
            )
            return future.result()
    except RuntimeError:
        return asyncio.run(mark_feedback_resolved_async(feedback_id))


async def migrate_jsonl_to_database() -> int:
    """
    Migrate existing JSONL feedback data to database.
    
    Returns:
        Number of entries migrated.
    """
    if not DATABASE_AVAILABLE:
        logger.warning("Database not available, cannot migrate")
        return 0
    
    try:
        db_manager = get_database_manager()
        # Always ensure database tables exist (create_all is idempotent)
        await db_manager.initialize()
        session = await db_manager.get_session()
        
        try:
            return await FeedbackRepository.migrate_from_jsonl(session)
        finally:
            await session.close()
            
    except Exception as e:
        logger.error(f"Failed to migrate feedback: {e}")
        return 0


# ============================================================================
# Fallback JSONL functions (for when database is unavailable)
# ============================================================================

def _get_feedback_file_path() -> Path:
    """Get the path to the feedback logs file."""
    logs_dir = Path(__file__).parent.parent.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "feedback_logs.jsonl"


def _generate_feedback_id() -> str:
    """Generate a unique feedback ID."""
    import uuid
    timestamp = int(datetime.now().timestamp())
    unique_suffix = uuid.uuid4().hex[:8]
    return f"fb_{timestamp}_{unique_suffix}"


def _save_feedback_jsonl_fallback(request: FeedbackRequest) -> FeedbackEntry:
    """Save feedback to JSONL file (fallback when DB unavailable)."""
    import json
    from ..utils.app_session import get_session_id
    
    session_id = request.session_id or get_session_id()
    
    entry = FeedbackEntry(
        id=_generate_feedback_id(),
        timestamp=datetime.now().isoformat(),
        session_id=session_id,
        message=request.message,
        character=request.character,
        user_input=request.user_input,
        category=request.category,
        comment=request.comment,
        resolved=False
    )
    
    file_path = _get_feedback_file_path()
    
    try:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        logger.info(f"Saved feedback (JSONL fallback): {entry.id}")
    except Exception as e:
        logger.error(f"Failed to save feedback: {e}")
        raise
    
    return entry


def _load_feedback_jsonl_fallback(include_resolved: bool, limit: int) -> List[FeedbackEntry]:
    """Load feedback from JSONL file (fallback)."""
    import json
    
    file_path = _get_feedback_file_path()
    
    if not file_path.exists():
        return []
    
    entries = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = FeedbackEntry(**data)
                    if include_resolved or not entry.resolved:
                        entries.append(entry)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Failed to parse feedback entry: {e}")
                    continue
    except Exception as e:
        logger.error(f"Failed to load feedback: {e}")
        return []
    
    entries.reverse()
    return entries[:limit]


def _mark_feedback_resolved_jsonl_fallback(feedback_id: str) -> bool:
    """Mark feedback as resolved in JSONL file (fallback)."""
    import json
    
    file_path = _get_feedback_file_path()
    
    if not file_path.exists():
        return False
    
    entries = []
    found = False
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("id") == feedback_id:
                        data["resolved"] = True
                        found = True
                    entries.append(data)
                except (json.JSONDecodeError, ValueError):
                    continue
        
        if found:
            with open(file_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info(f"Marked feedback as resolved (JSONL fallback): {feedback_id}")
        
        return found
    except Exception as e:
        logger.error(f"Failed to mark feedback as resolved: {e}")
        return False
