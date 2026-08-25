"""
Repository for Feedback management
"""

import json
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import select, delete, update, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Feedback

logger = logging.getLogger(__name__)


class FeedbackRepository:
    """Repository for managing user feedback in database"""
    
    @staticmethod
    def generate_id() -> str:
        """Generate a unique feedback ID.
        
        Returns:
            str: Feedback ID in format fb_<timestamp>_<uuid>
        """
        timestamp = int(datetime.now().timestamp())
        unique_suffix = uuid.uuid4().hex[:8]
        return f"fb_{timestamp}_{unique_suffix}"
    
    @staticmethod
    async def create(
        session: AsyncSession,
        message: str,
        category: str,
        session_id: Optional[str] = None,
        character: Optional[str] = None,
        user_input: Optional[str] = None,
        comment: Optional[str] = None,
        metadata: Optional[dict] = None
    ) -> Feedback:
        """Create a new feedback entry.
        
        Args:
            session: Database session
            message: The AI response that received feedback
            category: Feedback category (incorrect, incomplete, slow, other)
            session_id: App session ID (corresponds to log filename)
            character: Character name
            user_input: Original user input
            comment: User's detailed comment
            metadata: Additional metadata
            
        Returns:
            Feedback: Created feedback entry
        """
        feedback = Feedback(
            id=FeedbackRepository.generate_id(),
            session_id=session_id,
            message=message,
            character=character,
            user_input=user_input,
            category=category,
            comment=comment,
            feedback_metadata=metadata or {}
        )
        
        session.add(feedback)
        await session.commit()
        await session.refresh(feedback)
        
        logger.info(f"Created feedback: {feedback.id}")
        return feedback
    
    @staticmethod
    async def get_by_id(session: AsyncSession, feedback_id: str) -> Optional[Feedback]:
        """Get feedback by ID.
        
        Args:
            session: Database session
            feedback_id: Feedback ID
            
        Returns:
            Feedback or None
        """
        query = select(Feedback).where(Feedback.id == feedback_id)
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def list_feedback(
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        include_resolved: bool = False,
        session_id: Optional[str] = None,
        category: Optional[str] = None
    ) -> Tuple[List[Feedback], int]:
        """List feedback entries with filtering and pagination.
        
        Args:
            session: Database session
            limit: Maximum entries to return
            offset: Number of entries to skip
            include_resolved: Include resolved feedback
            session_id: Filter by session ID
            category: Filter by category
            
        Returns:
            Tuple: (list of feedback, total count)
        """
        if not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        if int(offset) < 0:
            raise ValueError("offset must be non-negative")
        limit = int(limit)
        offset = int(offset)
        conditions = []
        
        if not include_resolved:
            conditions.append(Feedback.resolved == False)
        
        if session_id:
            conditions.append(Feedback.session_id == session_id)
        
        if category:
            conditions.append(Feedback.category == category)
        
        # Get total count
        count_query = select(func.count()).select_from(Feedback)
        if conditions:
            count_query = count_query.where(and_(*conditions))
        count_result = await session.execute(count_query)
        total_count = int(count_result.scalar_one())
        
        # Get paginated results
        query = select(Feedback)
        if conditions:
            query = query.where(and_(*conditions))
        query = query.order_by(Feedback.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await session.execute(query)
        feedback_list = result.scalars().all()
        
        return feedback_list, total_count
    
    @staticmethod
    async def mark_resolved(
        session: AsyncSession,
        feedback_id: str,
        resolved_by: Optional[str] = None
    ) -> bool:
        """Mark feedback as resolved.
        
        Args:
            session: Database session
            feedback_id: Feedback ID
            resolved_by: Username of resolver
            
        Returns:
            bool: True if successful
        """
        feedback = await FeedbackRepository.get_by_id(session, feedback_id)
        if not feedback:
            return False
        
        feedback.resolved = True
        feedback.resolved_at = datetime.utcnow()
        feedback.resolved_by = resolved_by
        
        await session.commit()
        logger.info(f"Marked feedback as resolved: {feedback_id}")
        return True
    
    @staticmethod
    async def delete_feedback(session: AsyncSession, feedback_id: str) -> bool:
        """Delete a feedback entry.
        
        Args:
            session: Database session
            feedback_id: Feedback ID
            
        Returns:
            bool: True if deleted
        """
        feedback = await FeedbackRepository.get_by_id(session, feedback_id)
        if not feedback:
            return False
        
        await session.delete(feedback)
        await session.commit()
        logger.info(f"Deleted feedback: {feedback_id}")
        return True
    
    @staticmethod
    async def migrate_from_jsonl(
        session: AsyncSession,
        jsonl_path: Optional[Path] = None
    ) -> int:
        """Migrate existing feedback from JSONL file to database.
        
        Args:
            session: Database session
            jsonl_path: Path to JSONL file (defaults to logs/feedback_logs.jsonl)
            
        Returns:
            int: Number of entries migrated
        """
        if jsonl_path is None:
            jsonl_path = Path(__file__).parent.parent.parent / "logs" / "feedback_logs.jsonl"
        
        if not jsonl_path.exists():
            logger.info("No JSONL file to migrate")
            return 0
        
        # Move the source to a unique staging name first.  This leaves a stable
        # snapshot for parsing and prevents an archive collision or overwrite.
        staged_path = jsonl_path.with_name(
            f"{jsonl_path.name}.migrating-{uuid.uuid4().hex}"
        )
        jsonl_path.rename(staged_path)
        migrated = 0
        try:
            records = []
            with open(staged_path, "r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError(
                            f"feedback JSONL line {line_number} must be an object"
                        )
                    created_at = (
                        datetime.fromisoformat(str(data["timestamp"]))
                        if data.get("timestamp")
                        else datetime.utcnow()
                    )
                    resolved_at = (
                        datetime.fromisoformat(str(data["resolved_at"]))
                        if data.get("resolved_at")
                        else None
                    )
                    records.append(
                        {
                            "id": data.get("id") or FeedbackRepository.generate_id(),
                            "session_id": data.get("session_id"),
                            "message": data.get("message", ""),
                            "character": data.get("character"),
                            "user_input": data.get("user_input"),
                            "category": data.get("category", "other"),
                            "comment": data.get("comment"),
                            "resolved": bool(data.get("resolved", False)),
                            "resolved_at": resolved_at,
                            "resolved_by": data.get("resolved_by"),
                            "created_at": created_at,
                        }
                    )

            # Parse and validate every line before touching the DB.  A malformed
            # line therefore cannot result in a partially migrated dataset.
            for data in records:
                existing = await FeedbackRepository.get_by_id(session, data["id"])
                if existing:
                    logger.debug("Skipping existing feedback: %s", data["id"])
                    continue
                session.add(
                    Feedback(
                        id=data["id"],
                        session_id=data["session_id"],
                        message=data["message"],
                        character=data["character"],
                        user_input=data["user_input"],
                        category=data["category"],
                        comment=data["comment"],
                        resolved=data["resolved"],
                        resolved_at=data["resolved_at"],
                        resolved_by=data["resolved_by"],
                        created_at=data["created_at"],
                        feedback_metadata={},
                    )
                )
                migrated += 1

            # Secure the archive before committing.  If chmod fails, the
            # transaction is still rollbackable and the original JSONL can be
            # restored by the exception path below.
            staged_path.chmod(0o600)
            if migrated > 0:
                await session.commit()
                logger.info(
                    "Migrated %s feedback entries from JSONL to database", migrated
                )

            # Keep a recoverable, permission-restricted archive.  The UUID
            # makes the rename non-overwriting even under same-second retries.
            archive_path = jsonl_path.with_name(
                f"{jsonl_path.name}.migrated-{uuid.uuid4().hex}"
            )
            staged_path.rename(archive_path)
            logger.info("Archived migrated feedback JSONL at %s", archive_path)
            return migrated

        except Exception as e:
            logger.error("Failed to migrate feedback: %s", e)
            await session.rollback()
            # Restore the active source when possible.  Never overwrite a new
            # source created by a concurrent writer.
            if staged_path.exists():
                if not jsonl_path.exists():
                    staged_path.rename(jsonl_path)
                else:
                    logger.error(
                        "Keeping failed feedback migration staging file at %s", staged_path
                    )
            raise
