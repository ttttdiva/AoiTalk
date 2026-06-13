"""Spotify連携のアクティビティログ・セッション集計モデル。"""

import uuid
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    Float,
    JSON,
    Boolean,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class SpotifyActivityLog(Base):
    """Spotify activity logging for analytics and history"""

    __tablename__ = "spotify_activity_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    session_id = Column(
        UUID(as_uuid=True), index=True
    )  # Link to conversation session if available

    # Activity details
    action = Column(
        String, nullable=False, index=True
    )  # play, pause, skip, queue, etc.
    track_id = Column(String, index=True)  # Spotify track ID
    track_name = Column(String)
    artist_name = Column(String)
    album_name = Column(String)
    track_uri = Column(String)

    # Context information
    playlist_id = Column(String)
    playlist_name = Column(String)
    queue_position = Column(Integer)  # Position in queue if relevant

    # Playback details
    duration_ms = Column(Integer)
    position_ms = Column(Integer)  # Current position in track
    volume_percent = Column(Integer)
    is_playing = Column(Boolean)
    shuffle_state = Column(Boolean)
    repeat_state = Column(String)  # off, track, context

    # Request details
    request_source = Column(String, default="ai_assistant")  # Source of the request
    request_text = Column(Text)  # Original user request text
    success = Column(Boolean, default=True)
    error_message = Column(Text)

    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Additional metadata
    activity_metadata = Column(JSON, default=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "character_name": self.character_name,
            "session_id": self.session_id,
            "action": self.action,
            "track_id": self.track_id,
            "track_name": self.track_name,
            "artist_name": self.artist_name,
            "album_name": self.album_name,
            "track_uri": self.track_uri,
            "playlist_id": self.playlist_id,
            "playlist_name": self.playlist_name,
            "queue_position": self.queue_position,
            "duration_ms": self.duration_ms,
            "position_ms": self.position_ms,
            "volume_percent": self.volume_percent,
            "is_playing": self.is_playing,
            "shuffle_state": self.shuffle_state,
            "repeat_state": self.repeat_state,
            "request_source": self.request_source,
            "request_text": self.request_text,
            "success": self.success,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.activity_metadata,
        }


class SpotifySessionSummary(Base):
    """Summary of Spotify usage sessions for analytics"""

    __tablename__ = "spotify_session_summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    conversation_session_id = Column(UUID(as_uuid=True), index=True)

    # Session timing
    session_start = Column(DateTime, nullable=False)
    session_end = Column(DateTime)
    duration_minutes = Column(Float)

    # Activity counts
    total_actions = Column(Integer, default=0)
    play_count = Column(Integer, default=0)
    skip_count = Column(Integer, default=0)
    queue_count = Column(Integer, default=0)
    playlist_operations = Column(Integer, default=0)

    # Music statistics
    unique_tracks_played = Column(Integer, default=0)
    unique_artists = Column(Integer, default=0)
    total_play_time_ms = Column(Integer, default=0)

    # Top tracks/artists in this session
    top_artist = Column(String)
    top_track = Column(String)
    most_used_playlist = Column(String)

    # Session characteristics
    music_genres = Column(JSON, default=list)  # List of genres if available
    session_mood = Column(String)  # Derived from music characteristics

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    session_metadata = Column(JSON, default=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "character_name": self.character_name,
            "conversation_session_id": self.conversation_session_id,
            "session_start": (
                self.session_start.isoformat() if self.session_start else None
            ),
            "session_end": self.session_end.isoformat() if self.session_end else None,
            "duration_minutes": self.duration_minutes,
            "total_actions": self.total_actions,
            "play_count": self.play_count,
            "skip_count": self.skip_count,
            "queue_count": self.queue_count,
            "playlist_operations": self.playlist_operations,
            "unique_tracks_played": self.unique_tracks_played,
            "unique_artists": self.unique_artists,
            "total_play_time_ms": self.total_play_time_ms,
            "top_artist": self.top_artist,
            "top_track": self.top_track,
            "most_used_playlist": self.most_used_playlist,
            "music_genres": self.music_genres,
            "session_mood": self.session_mood,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.session_metadata,
        }
