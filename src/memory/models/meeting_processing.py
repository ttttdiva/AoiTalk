from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base, _encrypted_json_property


class MeetingProcessingJob(Base):
    """Durable server-owned ledger for one meeting-processing request."""

    __tablename__ = "meeting_processing_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    actor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    idempotency_key = Column(String(128), nullable=False)
    request_sha256 = Column(String(64), nullable=False, index=True)

    _request_json = Column(
        "request_json",
        JSON,
        nullable=False,
        default=dict,
    )
    request_json = _encrypted_json_property(
        "_request_json",
        "meeting_processing_jobs.request_json",
    )

    audio_upload_id = Column(UUID(as_uuid=True), nullable=False)
    audio_file_name = Column(String(255), nullable=False)
    audio_mime_type = Column(String(120), nullable=False)
    audio_size_bytes = Column(BigInteger, nullable=False)
    audio_sha256 = Column(String(64), nullable=False, index=True)

    status = Column(
        String(16),
        nullable=False,
        default="queued",
        server_default="queued",
        index=True,
    )
    stage = Column(
        String(32),
        nullable=False,
        default="queued",
        server_default="queued",
    )

    retry_generation = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    retryable = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    lease_owner = Column(String(160), nullable=True)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)

    _result_json = Column(
        "result_json",
        JSON,
        nullable=False,
        default=dict,
    )
    result_json = _encrypted_json_property(
        "_result_json",
        "meeting_processing_jobs.result_json",
    )

    _error_json = Column(
        "error_json",
        JSON,
        nullable=False,
        default=dict,
    )
    error_json = _encrypted_json_property(
        "_error_json",
        "meeting_processing_jobs.error_json",
    )

    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    actor = relationship("User", foreign_keys=[actor_user_id])

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_meeting_processing_jobs_status",
        ),
        CheckConstraint(
            "stage IN ("
            "'queued',"
            "'transcribing',"
            "'generating_minutes',"
            "'generating_memo',"
            "'persisting_minutes',"
            "'persisting_memo',"
            "'complete'"
            ")",
            name="ck_meeting_processing_jobs_stage",
        ),
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_meeting_processing_jobs_actor_idempotency",
        ),
        Index(
            "ix_meeting_processing_jobs_claim",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        result = (
            dict(self.result_json)
            if isinstance(self.result_json, dict)
            else {}
        )
        error = (
            dict(self.error_json)
            if isinstance(self.error_json, dict)
            else {}
        )
        return {
            "job_id": str(self.id),
            "actor_user_id": str(self.actor_user_id),
            "idempotency_key": self.idempotency_key,
            "request_sha256": self.request_sha256,
            "audio_upload_id": str(self.audio_upload_id),
            "audio_file_name": self.audio_file_name,
            "audio_mime_type": self.audio_mime_type,
            "audio_size_bytes": int(self.audio_size_bytes or 0),
            "audio_sha256": self.audio_sha256,
            "status": self.status,
            "stage": self.stage,
            "retry_generation": int(self.retry_generation or 0),
            "attempt_count": int(self.attempt_count or 0),
            "retryable": bool(self.retryable),
            "result": result,
            "error": error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
        }
