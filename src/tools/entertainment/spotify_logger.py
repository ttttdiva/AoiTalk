"""
Spotify activity logging service
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy import select, func, and_, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

try:
    from ...memory.database import get_database_manager
    from ...memory.models import SpotifyActivityLog, SpotifySessionSummary
except ImportError:
    # Fallback for when running in test mode or memory system is not available
    def get_database_manager():
        return None
    SpotifyActivityLog = None
    SpotifySessionSummary = None


class SpotifyLogger:
    """Service for logging and analyzing Spotify activities"""
    
    def __init__(self):
        self.db_manager = get_database_manager()
        self._session_cache = {}  # Cache for active sessions
        self._logging_enabled = self.db_manager is not None
    
    async def log_activity(
        self,
        user_id: str,
        character_name: str,
        action: str,
        track_info: Optional[Dict[str, Any]] = None,
        playback_info: Optional[Dict[str, Any]] = None,
        request_text: Optional[str] = None,
        session_id: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        **kwargs
    ) -> str:
        """Log a Spotify activity
        
        Args:
            user_id: User identifier
            character_name: Character name
            action: Action type (play, pause, skip, queue, etc.)
            track_info: Track information dict
            playback_info: Playback state information
            request_text: Original user request
            session_id: Conversation session ID
            success: Whether the action succeeded
            error_message: Error message if failed
            **kwargs: Additional metadata
            
        Returns:
            str: Log entry ID
        """
        # Skip logging if database is not available
        if not self._logging_enabled:
            return f"spotify_log_{int(time.time() * 1000)}"  # Return dummy ID
            
        try:
            await self.db_manager.initialize()
            
            # Extract track info
            track_data = {}
            if track_info:
                track_data.update({
                    'track_id': track_info.get('id'),
                    'track_name': track_info.get('name'),
                    'artist_name': ', '.join([artist.get('name', '') for artist in track_info.get('artists', [])]),
                    'album_name': track_info.get('album', {}).get('name'),
                    'track_uri': track_info.get('uri'),
                    'duration_ms': track_info.get('duration_ms')
                })
            
            # Extract playback info
            playback_data = {}
            if playback_info:
                playback_data.update({
                    'position_ms': playback_info.get('progress_ms'),
                    'volume_percent': playback_info.get('device', {}).get('volume_percent'),
                    'is_playing': playback_info.get('is_playing'),
                    'shuffle_state': playback_info.get('shuffle_state'),
                    'repeat_state': playback_info.get('repeat_state')
                })
            
            # Create log entry
            log_entry = SpotifyActivityLog(
                user_id=user_id,
                character_name=character_name,
                session_id=session_id,
                action=action,
                request_text=request_text,
                success=success,
                error_message=error_message,
                activity_metadata=kwargs,
                **track_data,
                **playback_data
            )
            
            async with self.db_manager.SessionLocal() as session:
                session.add(log_entry)
                await session.commit()
                
                # Update session summary if needed
                await self._update_session_summary(session, user_id, character_name, action, track_data)
                
                return log_entry.id
        
        except Exception as e:
            print(f"[SpotifyLogger] Error logging activity: {e}")
            return ""
    
    async def _update_session_summary(
        self,
        session: AsyncSession,
        user_id: str,
        character_name: str,
        action: str,
        track_data: Dict[str, Any]
    ):
        """Update or create session summary"""
        try:
            now = datetime.utcnow()
            session_start_threshold = now - timedelta(hours=2)  # Consider 2 hours as session boundary
            
            # Get or create current session summary
            stmt = select(SpotifySessionSummary).where(
                and_(
                    SpotifySessionSummary.user_id == user_id,
                    SpotifySessionSummary.character_name == character_name,
                    SpotifySessionSummary.session_end.is_(None),
                    SpotifySessionSummary.session_start >= session_start_threshold
                )
            ).order_by(desc(SpotifySessionSummary.session_start)).limit(1)
            
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()
            
            if not summary:
                # Create new session summary
                summary = SpotifySessionSummary(
                    user_id=user_id,
                    character_name=character_name,
                    session_start=now,
                    total_actions=0,
                    play_count=0,
                    skip_count=0,
                    queue_count=0,
                    playlist_operations=0,
                    unique_tracks_played=0,
                    unique_artists=0,
                    total_play_time_ms=0
                )
                session.add(summary)
            
            # Update summary based on action
            summary.total_actions += 1
            
            if action == 'play':
                summary.play_count += 1
            elif action == 'skip':
                summary.skip_count += 1
            elif action in ['queue', 'add_to_queue']:
                summary.queue_count += 1
            elif action in ['create_playlist', 'add_to_playlist', 'remove_from_playlist']:
                summary.playlist_operations += 1
            
            # Update track/artist info if available
            if track_data.get('track_name') and track_data.get('artist_name'):
                # This is simplified - in production you might want to track unique tracks/artists more precisely
                if not summary.top_track:
                    summary.top_track = track_data['track_name']
                if not summary.top_artist:
                    summary.top_artist = track_data['artist_name']
            
            await session.commit()
            
        except Exception as e:
            print(f"[SpotifyLogger] Error updating session summary: {e}")
    
    async def get_recent_activity(
        self,
        user_id: str,
        character_name: Optional[str] = None,
        limit: int = 50,
        hours: int = 24
    ) -> List[Dict[str, Any]]:
        """Get recent Spotify activity
        
        Args:
            user_id: User identifier
            character_name: Optional character filter
            limit: Maximum number of entries
            hours: Number of hours to look back
            
        Returns:
            List of activity log entries
        """
        try:
            await self.db_manager.initialize()
            
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)
            
            # Build query
            conditions = [
                SpotifyActivityLog.user_id == user_id,
                SpotifyActivityLog.created_at >= cutoff_time
            ]
            
            if character_name:
                conditions.append(SpotifyActivityLog.character_name == character_name)
            
            stmt = select(SpotifyActivityLog).where(
                and_(*conditions)
            ).order_by(desc(SpotifyActivityLog.created_at)).limit(limit)
            
            async with self.db_manager.SessionLocal() as session:
                result = await session.execute(stmt)
                logs = result.scalars().all()
                
                return [log.to_dict() for log in logs]
        
        except Exception as e:
            print(f"[SpotifyLogger] Error getting recent activity: {e}")
            return []
    
    async def get_activity_stats(
        self,
        user_id: str,
        character_name: Optional[str] = None,
        days: int = 7
    ) -> Dict[str, Any]:
        """Get activity statistics
        
        Args:
            user_id: User identifier
            character_name: Optional character filter
            days: Number of days to analyze
            
        Returns:
            Dictionary with statistics
        """
        try:
            await self.db_manager.initialize()
            
            cutoff_time = datetime.utcnow() - timedelta(days=days)
            
            # Build base conditions
            conditions = [
                SpotifyActivityLog.user_id == user_id,
                SpotifyActivityLog.created_at >= cutoff_time,
                SpotifyActivityLog.success == True
            ]
            
            if character_name:
                conditions.append(SpotifyActivityLog.character_name == character_name)
            
            async with self.db_manager.SessionLocal() as session:
                # Total activities
                total_stmt = select(func.count(SpotifyActivityLog.id)).where(and_(*conditions))
                total_result = await session.execute(total_stmt)
                total_activities = total_result.scalar() or 0
                
                # Activity breakdown
                action_stmt = select(
                    SpotifyActivityLog.action,
                    func.count(SpotifyActivityLog.id).label('count')
                ).where(and_(*conditions)).group_by(SpotifyActivityLog.action)
                
                action_result = await session.execute(action_stmt)
                action_counts = dict(action_result.all())
                
                # Top tracks
                track_stmt = select(
                    SpotifyActivityLog.track_name,
                    SpotifyActivityLog.artist_name,
                    func.count(SpotifyActivityLog.id).label('play_count')
                ).where(
                    and_(
                        *conditions,
                        SpotifyActivityLog.action.in_(['play', 'queue']),
                        SpotifyActivityLog.track_name.isnot(None)
                    )
                ).group_by(
                    SpotifyActivityLog.track_name,
                    SpotifyActivityLog.artist_name
                ).order_by(desc('play_count')).limit(10)
                
                track_result = await session.execute(track_stmt)
                top_tracks = [
                    {
                        'track_name': row.track_name,
                        'artist_name': row.artist_name,
                        'play_count': row.play_count
                    }
                    for row in track_result.all()
                ]
                
                # Top artists
                artist_stmt = select(
                    SpotifyActivityLog.artist_name,
                    func.count(SpotifyActivityLog.id).label('play_count')
                ).where(
                    and_(
                        *conditions,
                        SpotifyActivityLog.action.in_(['play', 'queue']),
                        SpotifyActivityLog.artist_name.isnot(None)
                    )
                ).group_by(SpotifyActivityLog.artist_name).order_by(desc('play_count')).limit(10)
                
                artist_result = await session.execute(artist_stmt)
                top_artists = [
                    {
                        'artist_name': row.artist_name,
                        'play_count': row.play_count
                    }
                    for row in artist_result.all()
                ]
                
                return {
                    'period_days': days,
                    'total_activities': total_activities,
                    'action_breakdown': action_counts,
                    'top_tracks': top_tracks,
                    'top_artists': top_artists,
                    'most_common_action': max(action_counts.items(), key=lambda x: x[1])[0] if action_counts else None
                }
        
        except Exception as e:
            print(f"[SpotifyLogger] Error getting activity stats: {e}")
            return {}
    
    async def search_activity(
        self,
        user_id: str,
        query: str,
        character_name: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search activity logs by track name, artist, or request text
        
        Args:
            user_id: User identifier
            query: Search query
            character_name: Optional character filter
            limit: Maximum number of results
            
        Returns:
            List of matching activity log entries
        """
        try:
            await self.db_manager.initialize()
            
            # Build search conditions
            search_pattern = f"%{query.lower()}%"
            search_conditions = [
                func.lower(SpotifyActivityLog.track_name).like(search_pattern),
                func.lower(SpotifyActivityLog.artist_name).like(search_pattern),
                func.lower(SpotifyActivityLog.album_name).like(search_pattern),
                func.lower(SpotifyActivityLog.request_text).like(search_pattern)
            ]
            
            conditions = [
                SpotifyActivityLog.user_id == user_id,
                or_(*search_conditions)
            ]
            
            if character_name:
                conditions.append(SpotifyActivityLog.character_name == character_name)
            
            stmt = select(SpotifyActivityLog).where(
                and_(*conditions)
            ).order_by(desc(SpotifyActivityLog.created_at)).limit(limit)
            
            async with self.db_manager.SessionLocal() as session:
                result = await session.execute(stmt)
                logs = result.scalars().all()
                
                return [log.to_dict() for log in logs]
        
        except Exception as e:
            print(f"[SpotifyLogger] Error searching activity: {e}")
            return []
    
    async def get_recent_tracks_for_exclusion(
        self,
        user_id: str,
        character_name: str,
        window_size: int = 20,
        hours: float = 2.0
    ) -> List[str]:
        """Get recently played track IDs for duplicate exclusion in auto-queue
        
        Args:
            user_id: User identifier
            character_name: Character name
            window_size: Maximum number of recent tracks to exclude
            hours: Time window to look back (default 2 hours = current session)
            
        Returns:
            List of track IDs to exclude from auto-queue
        """
        try:
            # Skip if database is not available
            if not self._logging_enabled:
                return []
                
            await self.db_manager.initialize()
            
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)
            
            # Get recent tracks that were added by auto-queue
            stmt = select(SpotifyActivityLog.track_id).where(
                and_(
                    SpotifyActivityLog.user_id == user_id,
                    SpotifyActivityLog.character_name == character_name,
                    SpotifyActivityLog.created_at >= cutoff_time,
                    SpotifyActivityLog.action.in_(['play', 'queue', 'add_to_queue']),
                    SpotifyActivityLog.track_id.isnot(None),
                    SpotifyActivityLog.success == True,
                    # Check if it was added by auto-queue (via metadata)
                    or_(
                        SpotifyActivityLog.activity_metadata.op('->>')('auto_queue').is_not(None),
                        SpotifyActivityLog.request_text.like('%自動%')
                    )
                )
            ).order_by(desc(SpotifyActivityLog.created_at)).limit(window_size)
            
            async with self.db_manager.SessionLocal() as session:
                result = await session.execute(stmt)
                track_ids = [row[0] for row in result.all() if row[0]]
                
                # Remove duplicates while preserving order
                seen = set()
                unique_track_ids = []
                for track_id in track_ids:
                    if track_id not in seen:
                        seen.add(track_id)
                        unique_track_ids.append(track_id)
                
                logger.info(f"[SpotifyLogger] Found {len(unique_track_ids)} recent tracks for exclusion")
                return unique_track_ids
        
        except Exception as e:
            logger.error(f"[SpotifyLogger] Error getting recent tracks for exclusion: {e}")
            return []

    async def close_session(self, user_id: str, character_name: str):
        """Close current Spotify session summary"""
        try:
            await self.db_manager.initialize()
            
            async with self.db_manager.SessionLocal() as session:
                # Find open session
                stmt = select(SpotifySessionSummary).where(
                    and_(
                        SpotifySessionSummary.user_id == user_id,
                        SpotifySessionSummary.character_name == character_name,
                        SpotifySessionSummary.session_end.is_(None)
                    )
                ).order_by(desc(SpotifySessionSummary.session_start)).limit(1)
                
                result = await session.execute(stmt)
                summary = result.scalar_one_or_none()
                
                if summary:
                    summary.session_end = datetime.utcnow()
                    if summary.session_start:
                        duration = summary.session_end - summary.session_start
                        summary.duration_minutes = duration.total_seconds() / 60
                    
                    await session.commit()
        
        except Exception as e:
            print(f"[SpotifyLogger] Error closing session: {e}")


# Global logger instance
_spotify_logger: Optional[SpotifyLogger] = None


def get_spotify_logger() -> SpotifyLogger:
    """Get global Spotify logger instance"""
    global _spotify_logger
    
    if _spotify_logger is None:
        _spotify_logger = SpotifyLogger()
    
    return _spotify_logger