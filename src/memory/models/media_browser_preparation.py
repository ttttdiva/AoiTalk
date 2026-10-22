"""Durable draft preparation evidence; no external publication success receipt."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class MediaBrowserPreparation(Base):
    __tablename__ = "media_browser_preparations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("external_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    action_version = Column(Integer, nullable=False)
    action_state_version = Column(Integer, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    platform_account_id = Column(UUID(as_uuid=True), nullable=False)
    platform_account_revision_id = Column(UUID(as_uuid=True), nullable=False)
    platform = Column(String(16), nullable=False)
    device_id = Column(String(128), nullable=False)
    tab_id = Column(Integer, nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    command_hash = Column(String(64), nullable=False)
    text_sha256 = Column(String(64), nullable=False)
    status = Column(String(40), nullable=False, default="preparing")
    phase = Column(String(40), nullable=False, default="claimed")
    version = Column(Integer, nullable=False, default=1)
    checkpoint_ciphertext = Column(Text, nullable=False)
    lease_expires_at = Column(DateTime, nullable=True)
    error_code = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("action_id", "action_version", name="uq_media_browser_preparation_action_revision"),
        CheckConstraint("status IN ('preparing','awaiting_final_confirmation','uncertain','blocked')", name="ck_media_browser_preparation_status"),
        CheckConstraint("action_version > 0 AND action_state_version > 0 AND version > 0", name="ck_media_browser_preparation_version"),
        CheckConstraint("length(payload_hash)=64 AND length(command_hash)=64 AND length(text_sha256)=64", name="ck_media_browser_preparation_hash"),
        CheckConstraint("platform IN ('x','youtube')", name="ck_media_browser_preparation_platform"),
    )

    def to_safe_dict(self):
        expired = self.status == "preparing" and self.lease_expires_at is not None and self.lease_expires_at <= datetime.utcnow()
        return {
            "id": str(self.id), "action_id": str(self.action_id), "owner_user_id": str(self.owner_user_id),
            "project_id": str(self.project_id) if self.project_id else None,
            "action_version": self.action_version, "action_state_version": self.action_state_version,
            "payload_hash": self.payload_hash, "platform": self.platform,
            "platform_account_id": str(self.platform_account_id), "platform_account_revision_id": str(self.platform_account_revision_id),
            "device_id": self.device_id, "tab_id": self.tab_id, "content_sha256": self.text_sha256,
            "text_sha256": self.text_sha256 if self.platform == "x" else None,
            "metadata_sha256": self.text_sha256 if self.platform == "youtube" else None,
            "status": "uncertain" if expired else self.status, "phase": self.phase, "version": self.version,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "error_code": "browser_preparation_lease_expired" if expired else self.error_code,
            "publication_confirmed": False, "automatic_retry_allowed": False,
            "created_at": self.created_at.isoformat(), "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
