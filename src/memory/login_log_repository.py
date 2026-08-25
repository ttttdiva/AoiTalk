"""
Repository for WebUI login log management
"""

from datetime import datetime
from ipaddress import ip_address as parse_ip_address
from typing import List, Optional, Dict, Any, Sequence
from sqlalchemy import select, delete, and_, or_, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import WebUILoginLog


def resolve_login_client_ip(
    peer: Optional[str], forwarded_values: Sequence[str]
) -> str:
    """Return one canonical audit/limiter IP across trusted proxy boundaries."""
    normalized_peer = str(peer or "").strip()
    try:
        peer_address = parse_ip_address(normalized_peer)
    except ValueError:
        return normalized_peer or "unknown"
    normalized_peer = str(peer_address)
    if not peer_address.is_loopback or len(forwarded_values) != 1:
        return normalized_peer

    forwarded_parts = [
        part.strip() for part in forwarded_values[0].split(",") if part.strip()
    ]
    if len(forwarded_parts) != 1:
        return normalized_peer
    try:
        return str(parse_ip_address(forwarded_parts[0]))
    except ValueError:
        return normalized_peer


class LoginLogRepository:
    """Repository for managing login/logout logs"""

    resolve_login_client_ip = staticmethod(resolve_login_client_ip)
    
    @staticmethod
    async def create_log_entry(
        session: AsyncSession,
        username: str,
        action: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        success: bool = True,
        failure_reason: Optional[str] = None,
        session_duration_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        commit: bool = True,
    ) -> WebUILoginLog:
        """Create a new login log entry
        
        Args:
            session: Database session
            username: Username
            action: Action type ('login' or 'logout')
            ip_address: IP address of the client
            user_agent: User agent string
            success: Whether the action was successful
            failure_reason: Reason for failure (if applicable)
            session_duration_seconds: Session duration for logout events
            metadata: Additional metadata
            
        Returns:
            WebUILoginLog: Created log entry
        """
        log_entry = WebUILoginLog(
            username=username,
            action=action,
            ip_address=ip_address,
            user_agent=user_agent,
            success=success,
            failure_reason=failure_reason,
            session_duration_seconds=session_duration_seconds,
            metadata=metadata or {}
        )
        
        session.add(log_entry)
        if commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(log_entry)
        
        return log_entry
    
    @staticmethod
    async def get_login_history(
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        username: Optional[str] = None,
        action: Optional[str] = None,
        success: Optional[bool] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> tuple[List[WebUILoginLog], int]:
        """Get login history with filtering and pagination
        
        Args:
            session: Database session
            limit: Maximum number of records to return
            offset: Number of records to skip
            username: Filter by username
            action: Filter by action type ('login' or 'logout')
            success: Filter by success status
            start_date: Filter logs after this date
            end_date: Filter logs before this date
            
        Returns:
            tuple: (list of log entries, total count)
        """
        # Build filter conditions
        conditions = []
        
        if username:
            conditions.append(WebUILoginLog.username == username)
        
        if action:
            conditions.append(WebUILoginLog.action == action)
        
        if success is not None:
            conditions.append(WebUILoginLog.success == success)
        
        if start_date:
            conditions.append(WebUILoginLog.created_at >= start_date)
        
        if end_date:
            conditions.append(WebUILoginLog.created_at <= end_date)
        
        # Build query
        query = select(WebUILoginLog)
        
        if conditions:
            query = query.where(and_(*conditions))
        
        # Get total count
        count_query = select(WebUILoginLog)
        if conditions:
            count_query = count_query.where(and_(*conditions))
        
        count_result = await session.execute(count_query)
        total_count = len(count_result.scalars().all())
        
        # Get paginated results
        query = query.order_by(WebUILoginLog.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await session.execute(query)
        logs = result.scalars().all()
        
        return logs, total_count
    
    @staticmethod
    async def delete_logs_before(
        session: AsyncSession,
        before_date: datetime
    ) -> int:
        """Delete logs before a specific date
        
        Args:
            session: Database session
            before_date: Delete logs before this date
            
        Returns:
            int: Number of deleted records
        """
        stmt = delete(WebUILoginLog).where(
            WebUILoginLog.created_at < before_date
        )
        
        result = await session.execute(stmt)
        await session.commit()
        
        return result.rowcount
    
    @staticmethod
    async def clear_all_logs(session: AsyncSession) -> int:
        """Clear all login logs
        
        Args:
            session: Database session
            
        Returns:
            int: Number of deleted records
        """
        stmt = delete(WebUILoginLog)
        
        result = await session.execute(stmt)
        await session.commit()
        
        return result.rowcount
    
    @staticmethod
    async def get_failed_login_attempts(
        session: AsyncSession,
        username: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[WebUILoginLog]:
        """Get failed login attempts for security monitoring
        
        Args:
            session: Database session
            username: Filter by username
            since: Get attempts since this date
            limit: Maximum number of records
            
        Returns:
            List[WebUILoginLog]: Failed login attempts
        """
        conditions = [
            WebUILoginLog.action == 'login',
            WebUILoginLog.success == False
        ]
        
        if username:
            conditions.append(WebUILoginLog.username == username)
        
        if since:
            conditions.append(WebUILoginLog.created_at >= since)
        
        query = select(WebUILoginLog).where(and_(*conditions))
        query = query.order_by(WebUILoginLog.created_at.desc())
        query = query.limit(limit)
        
        result = await session.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_recent_login_attempts_for_throttling(
        session: AsyncSession,
        normalized_username: str,
        ip_address: str,
        since: datetime,
        limit: int = 32,
    ) -> List[WebUILoginLog]:
        """Return recent login outcomes for one normalized user/IP pair.

        Successful outcomes are intentionally included so callers can reset
        temporary backoff without maintaining process-global limiter state.
        """
        query = (
            select(WebUILoginLog)
            .where(
                and_(
                    WebUILoginLog.action == "login",
                    func.lower(func.trim(WebUILoginLog.username))
                    == normalized_username,
                    WebUILoginLog.ip_address == ip_address,
                    WebUILoginLog.created_at >= since,
                    # Throttled requests remain in the audit trail but must
                    # not consume this bounded result set; otherwise a burst
                    # of 429 retries could evict the credential failures that
                    # caused the backoff and immediately bypass it.
                    or_(
                        WebUILoginLog.success.is_(True),
                        WebUILoginLog.failure_reason.is_(None),
                        WebUILoginLog.failure_reason != "rate_limited",
                    ),
                )
            )
            .order_by(WebUILoginLog.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(query)
        return result.scalars().all()

    @staticmethod
    async def acquire_login_throttle_lock(
        session: AsyncSession,
        normalized_username: str,
        ip_address: str,
    ) -> Optional[bool]:
        """Serialize one PostgreSQL login-attempt key for this transaction.

        SQLite and lightweight test sessions deliberately fall back to the
        persistent history check without a process-global mutable lock.
        """
        try:
            bind = session.get_bind()
            dialect_name = str(bind.dialect.name).lower()
        except (AttributeError, TypeError):
            return None
        if dialect_name != "postgresql":
            return None

        lock_key = f"aoitalk-login:{ip_address}:{normalized_username}"
        result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        return bool(result.scalar_one())
