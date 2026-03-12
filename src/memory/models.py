"""
Database models for conversation memory management
"""

import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from sqlalchemy import Column, String, Text, Integer, DateTime, Float, JSON, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
# pgvector removed - using Qdrant for vector search instead

Base = declarative_base()


class ConversationSession(Base):
    """Active conversation session"""
    __tablename__ = 'conversation_sessions'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    title = Column(String(200), default='')  # UI表示用タイトル（LLM自動生成）
    session_start = Column(DateTime, default=datetime.utcnow)
    last_activity = Column(DateTime, default=datetime.utcnow)
    message_count = Column(Integer, default=0)
    context = Column(JSON, default=dict)
    current_summary = Column(Text, default='')
    is_active = Column(Boolean, default=True)
    deleted_at = Column(DateTime, nullable=True, index=True)  # ソフトデリート用（3ヶ月後に実削除）
    
    # Project association
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id'), nullable=True, index=True)
    
    # Relationships
    messages = relationship("ConversationMessage", back_populates="session", cascade="all, delete-orphan")
    project = relationship("Project", backref="conversation_sessions")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'user_id': self.user_id,
            'character_name': self.character_name,
            'title': self.title,
            'session_start': self.session_start.isoformat() if self.session_start else None,
            'last_activity': self.last_activity.isoformat() if self.last_activity else None,
            'message_count': self.message_count,
            'context': self.context,
            'current_summary': self.current_summary,
            'is_active': self.is_active,
            'deleted_at': self.deleted_at.isoformat() if self.deleted_at else None,
            'project_id': str(self.project_id) if self.project_id else None
        }


class ConversationMessage(Base):
    """Individual conversation message"""
    __tablename__ = 'conversation_messages'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey('conversation_sessions.id'), nullable=False)
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    
    # embedding removed - using Qdrant for vector search instead
    
    message_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    token_count = Column(Integer)
    
    # Branching support (like ChatGPT's edit/branch feature)
    parent_message_id = Column(UUID(as_uuid=True), ForeignKey('conversation_messages.id'), nullable=True)
    branch_index = Column(Integer, default=0)  # Index among sibling branches
    is_active_branch = Column(Boolean, default=True)  # Currently displayed branch
    
    # Relationship to session
    session = relationship("ConversationSession", back_populates="messages")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'session_id': str(self.session_id),
            'role': self.role,
            'content': self.content,
            'metadata': self.message_metadata,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'token_count': self.token_count,
            'parent_message_id': str(self.parent_message_id) if self.parent_message_id else None,
            'branch_index': self.branch_index,
            'is_active_branch': self.is_active_branch
        }


class ConversationArchive(Base):
    """Archived conversation summaries for long-term memory"""
    __tablename__ = 'conversation_archives'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    original_session_id = Column(String)
    summary = Column(Text, nullable=False)
    
    # summary_embedding removed - using Qdrant for vector search instead
    
    message_count = Column(Integer)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    message_metadata = Column(JSON, default=dict)
    archived_at = Column(DateTime, default=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'character_name': self.character_name,
            'original_session_id': self.original_session_id,
            'summary': self.summary,
            'message_count': self.message_count,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'metadata': self.message_metadata,
            'archived_at': self.archived_at.isoformat() if self.archived_at else None
        }


class ConversationHistory(Base):
    """Complete conversation history for audit and analysis"""
    __tablename__ = 'conversation_history'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    session_id = Column(UUID(as_uuid=True))
    character_name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    message_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    token_count = Column(Integer)
    function_call_data = Column(JSON)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'session_id': self.session_id,
            'character_name': self.character_name,
            'role': self.role,
            'content': self.content,
            'metadata': self.message_metadata,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'token_count': self.token_count,
            'function_call_data': self.function_call_data
        }


class SpotifyActivityLog(Base):
    """Spotify activity logging for analytics and history"""
    __tablename__ = 'spotify_activity_logs'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    session_id = Column(UUID(as_uuid=True), index=True)  # Link to conversation session if available
    
    # Activity details
    action = Column(String, nullable=False, index=True)  # play, pause, skip, queue, etc.
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
    request_source = Column(String, default='ai_assistant')  # Source of the request
    request_text = Column(Text)  # Original user request text
    success = Column(Boolean, default=True)
    error_message = Column(Text)
    
    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Additional metadata
    activity_metadata = Column(JSON, default=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'character_name': self.character_name,
            'session_id': self.session_id,
            'action': self.action,
            'track_id': self.track_id,
            'track_name': self.track_name,
            'artist_name': self.artist_name,
            'album_name': self.album_name,
            'track_uri': self.track_uri,
            'playlist_id': self.playlist_id,
            'playlist_name': self.playlist_name,
            'queue_position': self.queue_position,
            'duration_ms': self.duration_ms,
            'position_ms': self.position_ms,
            'volume_percent': self.volume_percent,
            'is_playing': self.is_playing,
            'shuffle_state': self.shuffle_state,
            'repeat_state': self.repeat_state,
            'request_source': self.request_source,
            'request_text': self.request_text,
            'success': self.success,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'metadata': self.activity_metadata
        }


class SpotifySessionSummary(Base):
    """Summary of Spotify usage sessions for analytics"""
    __tablename__ = 'spotify_session_summaries'
    
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
            'id': self.id,
            'user_id': self.user_id,
            'character_name': self.character_name,
            'conversation_session_id': self.conversation_session_id,
            'session_start': self.session_start.isoformat() if self.session_start else None,
            'session_end': self.session_end.isoformat() if self.session_end else None,
            'duration_minutes': self.duration_minutes,
            'total_actions': self.total_actions,
            'play_count': self.play_count,
            'skip_count': self.skip_count,
            'queue_count': self.queue_count,
            'playlist_operations': self.playlist_operations,
            'unique_tracks_played': self.unique_tracks_played,
            'unique_artists': self.unique_artists,
            'total_play_time_ms': self.total_play_time_ms,
            'top_artist': self.top_artist,
            'top_track': self.top_track,
            'most_used_playlist': self.most_used_playlist,
            'music_genres': self.music_genres,
            'session_mood': self.session_mood,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'metadata': self.session_metadata
        }


class ClickUpTask(Base):
    """ClickUp task information for context awareness"""
    __tablename__ = 'clickup_tasks'
    
    id = Column(String, primary_key=True)  # ClickUp task ID
    name = Column(String, nullable=False)
    description = Column(Text)
    status = Column(String, nullable=False)
    priority = Column(String)  # urgent, high, normal, low
    
    # Date information
    start_date = Column(DateTime)
    due_date = Column(DateTime)
    date_created = Column(DateTime)
    date_updated = Column(DateTime)
    date_closed = Column(DateTime)
    
    # User and assignment
    creator_id = Column(String)
    creator_name = Column(String)
    assignee_ids = Column(JSON, default=list)  # List of assignee IDs
    assignee_names = Column(JSON, default=list)  # List of assignee names
    
    # Organization
    list_id = Column(String, index=True)
    list_name = Column(String)
    folder_id = Column(String)
    folder_name = Column(String)
    space_id = Column(String, index=True)
    space_name = Column(String)
    
    # Task details
    tags = Column(JSON, default=list)
    time_estimate = Column(Integer)  # in milliseconds
    time_spent = Column(Integer)  # in milliseconds
    
    # Custom fields and metadata
    custom_fields = Column(JSON, default=dict)
    task_metadata = Column(JSON, default=dict)
    
    # Sync information
    last_synced = Column(DateTime, default=datetime.utcnow)
    sync_version = Column(Integer, default=1)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'status': self.status,
            'priority': self.priority,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'due_date': self.due_date.isoformat() if self.due_date else None,
            'date_created': self.date_created.isoformat() if self.date_created else None,
            'date_updated': self.date_updated.isoformat() if self.date_updated else None,
            'date_closed': self.date_closed.isoformat() if self.date_closed else None,
            'creator_id': self.creator_id,
            'creator_name': self.creator_name,
            'assignee_ids': self.assignee_ids,
            'assignee_names': self.assignee_names,
            'list_id': self.list_id,
            'list_name': self.list_name,
            'folder_id': self.folder_id,
            'folder_name': self.folder_name,
            'space_id': self.space_id,
            'space_name': self.space_name,
            'tags': self.tags,
            'time_estimate': self.time_estimate,
            'time_spent': self.time_spent,
            'custom_fields': self.custom_fields,
            'metadata': self.task_metadata,
            'last_synced': self.last_synced.isoformat() if self.last_synced else None,
            'sync_version': self.sync_version
        }


class WebUILoginLog(Base):
    """WebUI login/logout activity log for security and audit"""
    __tablename__ = 'webui_login_logs'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)  # 'login' or 'logout'
    ip_address = Column(String)
    user_agent = Column(Text)
    
    # Success/failure tracking
    success = Column(Boolean, default=True, index=True)
    failure_reason = Column(String)  # e.g., 'invalid_credentials', 'session_expired'
    
    # Session tracking
    session_duration_seconds = Column(Integer)  # Duration for logout events
    
    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Additional metadata
    login_metadata = Column(JSON, default=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'username': self.username,
            'action': self.action,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'success': self.success,
            'failure_reason': self.failure_reason,
            'session_duration_seconds': self.session_duration_seconds,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'metadata': self.login_metadata
        }


class User(Base):
    """User account for multi-user enterprise support"""
    __tablename__ = 'users'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)
    
    # Profile
    display_name = Column(String(100))
    preferred_character = Column(String(100))
    
    # Role & Status
    role = Column(String(20), default='user', index=True)  # 'admin', 'user'
    is_active = Column(Boolean, default=True, index=True)
    is_password_reset_required = Column(Boolean, default=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime)
    
    # Settings (JSON for flexibility)
    user_settings = Column(JSON, default=dict)
    
    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """Convert to dictionary
        
        Args:
            include_sensitive: Include sensitive fields (password_hash)
        """
        result = {
            'id': str(self.id),
            'username': self.username,
            'email': self.email,
            'display_name': self.display_name,
            'preferred_character': self.preferred_character,
            'role': self.role,
            'is_active': self.is_active,
            'is_password_reset_required': self.is_password_reset_required,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'settings': self.user_settings
        }
        if include_sensitive:
            result['password_hash'] = self.password_hash
        return result


class Feedback(Base):
    """User feedback on AI responses"""
    __tablename__ = 'feedback'
    
    id = Column(String(50), primary_key=True)  # fb_<timestamp>_<uuid>
    session_id = Column(String(50), index=True)  # Corresponds to app log filename (YYYYMMDD_HHMMSS)
    
    # Feedback content
    message = Column(Text, nullable=False)  # The AI response that received feedback
    character = Column(String(100))  # Character name that gave the response
    user_input = Column(Text)  # Original user input (if applicable)
    
    # Feedback details
    category = Column(String(50), nullable=False, index=True)  # incorrect, incomplete, slow, other
    comment = Column(Text)  # User's detailed comment
    
    # Status
    resolved = Column(Boolean, default=False, index=True)
    resolved_at = Column(DateTime)
    resolved_by = Column(String(100))
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Additional metadata
    feedback_metadata = Column(JSON, default=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'session_id': self.session_id,
            'message': self.message,
            'character': self.character,
            'user_input': self.user_input,
            'category': self.category,
            'comment': self.comment,
            'resolved': self.resolved,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None,
            'resolved_by': self.resolved_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'metadata': self.feedback_metadata
        }


class Project(Base):
    """プロジェクト（共有ストレージ単位）"""
    __tablename__ = 'projects'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    slug = Column(String(100), unique=True, nullable=False, index=True)  # URL用の識別子
    
    # オーナー（作成者）
    owner_id = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=False)
    
    # 設定
    allow_join_requests = Column(Boolean, default=True)  # 参加申請を受け付けるか
    
    # ストレージ設定
    storage_quota_mb = Column(Integer, default=1000)  # 容量制限（MB）
    storage_used_mb = Column(Float, default=0)
    
    # タイムスタンプ
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # メタデータ
    project_metadata = Column(JSON, default=dict)
    
    # リレーション
    owner = relationship("User", backref="owned_projects", foreign_keys=[owner_id])
    members = relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")
    join_requests = relationship("ProjectJoinRequest", back_populates="project", cascade="all, delete-orphan")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'name': self.name,
            'description': self.description,
            'slug': self.slug,
            'owner_id': str(self.owner_id),
            'allow_join_requests': self.allow_join_requests,
            'storage_quota_mb': self.storage_quota_mb,
            'storage_used_mb': self.storage_used_mb,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'metadata': self.project_metadata
        }


class ProjectMember(Base):
    """プロジェクトメンバー"""
    __tablename__ = 'project_members'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id'), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=False)
    
    # 役割: 'owner', 'admin', 'member', 'viewer'
    role = Column(String(20), default='member')
    
    # 権限（JSONで柔軟に管理）
    permissions = Column(JSON, default=lambda: {
        'read': True,
        'write': True,
        'delete': False,
        'manage_members': False
    })
    
    # タイムスタンプ
    joined_at = Column(DateTime, default=datetime.utcnow)
    invited_by = Column(UUID(as_uuid=True), ForeignKey('users.id'))
    
    # リレーション
    project = relationship("Project", back_populates="members")
    user = relationship("User", foreign_keys=[user_id], backref="project_memberships")
    inviter = relationship("User", foreign_keys=[invited_by])
    
    __table_args__ = (
        # 同一プロジェクトに同一ユーザーは1回のみ
        UniqueConstraint('project_id', 'user_id', name='unique_project_member'),
    )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'project_id': str(self.project_id),
            'user_id': str(self.user_id),
            'role': self.role,
            'permissions': self.permissions,
            'joined_at': self.joined_at.isoformat() if self.joined_at else None,
            'invited_by': str(self.invited_by) if self.invited_by else None
        }


class ProjectJoinRequest(Base):
    """プロジェクト参加申請"""
    __tablename__ = 'project_join_requests'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id'), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=False)
    
    # 申請内容
    message = Column(Text)  # 申請メッセージ
    status = Column(String(20), default='pending', index=True)  # 'pending', 'approved', 'rejected'
    
    # 処理情報
    processed_by = Column(UUID(as_uuid=True), ForeignKey('users.id'))
    processed_at = Column(DateTime)
    rejection_reason = Column(Text)
    
    # タイムスタンプ
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # リレーション
    project = relationship("Project", back_populates="join_requests")
    user = relationship("User", foreign_keys=[user_id], backref="join_requests")
    processor = relationship("User", foreign_keys=[processed_by])
    
    __table_args__ = (
        # 同一プロジェクトに同一ユーザーは申請中は1件のみ
        UniqueConstraint('project_id', 'user_id', name='unique_pending_request'),
    )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'project_id': str(self.project_id),
            'user_id': str(self.user_id),
            'message': self.message,
            'status': self.status,
            'processed_by': str(self.processed_by) if self.processed_by else None,
            'processed_at': self.processed_at.isoformat() if self.processed_at else None,
            'rejection_reason': self.rejection_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class RagCollection(Base):
    """RAGコレクション（ベクトルDB）のメタデータ管理"""
    __tablename__ = 'rag_collections'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    collection_name = Column(String(200), unique=True, nullable=False, index=True)

    # インデックスソース情報
    source_directory = Column(Text)
    include_patterns = Column(JSON, default=lambda: ["*.md", "*.txt", "*.pdf"])
    exclude_patterns = Column(JSON, default=lambda: [".*", "__pycache__"])

    # ステータス
    status = Column(String(20), default='empty', index=True)  # 'empty', 'indexing', 'ready', 'error'
    points_count = Column(Integer, default=0)
    last_indexed_at = Column(DateTime)
    error_message = Column(Text)

    # 作成者
    created_by = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=False)

    # タイムスタンプ
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # リレーション
    creator = relationship("User", backref="rag_collections", foreign_keys=[created_by])
    project_links = relationship("ProjectRagCollection", back_populates="collection",
                                 cascade="all, delete-orphan")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'name': self.name,
            'description': self.description,
            'collection_name': self.collection_name,
            'source_directory': self.source_directory,
            'include_patterns': self.include_patterns,
            'exclude_patterns': self.exclude_patterns,
            'status': self.status,
            'points_count': self.points_count,
            'last_indexed_at': self.last_indexed_at.isoformat() if self.last_indexed_at else None,
            'error_message': self.error_message,
            'created_by': str(self.created_by),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ProjectRagCollection(Base):
    """プロジェクトとRAGコレクションの多対多紐付け"""
    __tablename__ = 'project_rag_collections'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id'), nullable=False)
    collection_id = Column(UUID(as_uuid=True), ForeignKey('rag_collections.id'), nullable=False)

    is_active = Column(Boolean, default=True)

    # タイムスタンプ
    linked_at = Column(DateTime, default=datetime.utcnow)
    linked_by = Column(UUID(as_uuid=True), ForeignKey('users.id'))

    # リレーション
    project = relationship("Project", backref="rag_collection_links")
    collection = relationship("RagCollection", back_populates="project_links")
    linker = relationship("User", foreign_keys=[linked_by])

    __table_args__ = (
        UniqueConstraint('project_id', 'collection_id', name='unique_project_collection'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'project_id': str(self.project_id),
            'collection_id': str(self.collection_id),
            'is_active': self.is_active,
            'linked_at': self.linked_at.isoformat() if self.linked_at else None,
            'linked_by': str(self.linked_by) if self.linked_by else None,
        }


class UserRagCollection(Base):
    """ユーザーとRAGコレクションの直接紐付け"""
    __tablename__ = 'user_rag_collections'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=False)
    collection_id = Column(UUID(as_uuid=True), ForeignKey('rag_collections.id'), nullable=False)

    # 権限: 'read'（閲覧のみ）/ 'write'（更新・削除・再インデックス可）
    permission = Column(String(20), default='read')

    # タイムスタンプ
    linked_at = Column(DateTime, default=datetime.utcnow)
    linked_by = Column(UUID(as_uuid=True), ForeignKey('users.id'))

    # リレーション
    user = relationship("User", foreign_keys=[user_id], backref="rag_collection_links")
    collection = relationship("RagCollection", backref="user_links")
    linker = relationship("User", foreign_keys=[linked_by])

    __table_args__ = (
        UniqueConstraint('user_id', 'collection_id', name='unique_user_collection'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': str(self.id),
            'user_id': str(self.user_id),
            'collection_id': str(self.collection_id),
            'permission': self.permission,
            'linked_at': self.linked_at.isoformat() if self.linked_at else None,
            'linked_by': str(self.linked_by) if self.linked_by else None,
        }
