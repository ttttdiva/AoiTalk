"""
Repository for Feedback management
"""

import json
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import select, delete, update, and_
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
        conditions = []
        
        if not include_resolved:
            conditions.append(Feedback.resolved == False)
        
        if session_id:
            conditions.append(Feedback.session_id == session_id)
        
        if category:
            conditions.append(Feedback.category == category)
        
        # Get total count
        count_query = select(Feedback)
        if conditions:
            count_query = count_query.where(and_(*conditions))
        count_result = await session.execute(count_query)
        total_count = len(count_result.scalars().all())
        
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
        
        migrated = 0
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    try:
                        data = json.loads(line)
                        
                        # Check if already exists
                        existing = await FeedbackRepository.get_by_id(session, data.get("id", ""))
                        if existing:
                            logger.debug(f"Skipping existing feedback: {data.get('id')}")
                            continue
                        
                        # Create feedback entry
                        feedback = Feedback(
                            id=data.get("id") or FeedbackRepository.generate_id(),
                            session_id=data.get("session_id"),
                            message=data.get("message", ""),
                            character=data.get("character"),
                            user_input=data.get("user_input"),
                            category=data.get("category", "other"),
                            comment=data.get("comment"),
                            resolved=data.get("resolved", False),
                            created_at=datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else datetime.utcnow(),
                            feedback_metadata={}
                        )
                        
                        session.add(feedback)
                        migrated += 1
                        
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"Failed to parse feedback entry: {e}")
                        continue
            
            if migrated > 0:
                await session.commit()
                logger.info(f"Migrated {migrated} feedback entries from JSONL to database")
            
            return migrated
            
        except Exception as e:
            logger.error(f"Failed to migrate feedback: {e}")
            await session.rollback()
            return 0
