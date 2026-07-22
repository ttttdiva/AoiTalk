"""無料Teamの認証情報、候補、クォータ、予約台帳モデル。"""

from __future__ import annotations

import uuid
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base, _encrypted_text_property


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _number(value: Any) -> float:
    return float(value or 0)


def _secret_like_key(value: Any) -> bool:
    key = str(value).lower().replace("-", "_")
    if key in {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "cookie",
        "access_token",
        "refresh_token",
        "bearer_token",
    }:
        return True
    return key.endswith(("_api_key", "_password", "_secret", "_token"))


def _safe_public_mapping(value: Any) -> Any:
    """既存DBに秘密らしい任意keyがあってもAPIへ返さない。"""

    if isinstance(value, dict):
        return {
            str(key): _safe_public_mapping(item)
            for key, item in value.items()
            if not _secret_like_key(key)
        }
    if isinstance(value, list):
        return [_safe_public_mapping(item) for item in value]
    return value


class FreeTeamCredentialProfile(Base):
    """課金状態と用途を分離した認証プロファイル。"""

    __tablename__ = "free_team_credential_profiles"

    id = Column(String(100), primary_key=True)
    display_name = Column(String(160), nullable=False)
    provider = Column(String(80), nullable=False, index=True)
    authentication_type = Column(String(40), nullable=False, default="api_key")
    _api_key = Column("api_key", Text, nullable=True)
    api_key = _encrypted_text_property(
        "_api_key", "free_team_credential_profiles.api_key"
    )
    cli_auth_reference = Column(String(255), nullable=True)
    environment_variable = Column(String(120), nullable=True)
    base_url = Column(String(500), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    billing_mode = Column(String(40), nullable=False, index=True)
    privacy_class = Column(String(40), nullable=False, default="standard")
    allow_paid_overage = Column(Boolean, nullable=False, default=False)
    status = Column(String(40), nullable=False, default="ready")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    quota_pools = relationship(
        "FreeTeamQuotaPool",
        back_populates="credential_profile",
        cascade="all, delete-orphan",
    )
    candidates = relationship(
        "FreeTeamCandidateModel",
        back_populates="credential_profile",
        cascade="all, delete-orphan",
    )

    @property
    def configured(self) -> bool:
        if self.authentication_type == "cli":
            return bool(self.cli_auth_reference)
        return bool(self.api_key or (self.environment_variable and os.getenv(self.environment_variable)))

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "authentication_type": self.authentication_type,
            "environment_variable": self.environment_variable or "",
            "base_url": self.base_url or "",
            "enabled": bool(self.enabled),
            "billing_mode": self.billing_mode,
            "privacy_class": self.privacy_class,
            "allow_paid_overage": bool(self.allow_paid_overage),
            "configured": self.configured,
            "status": self.status,
            "updated_at": _iso(self.updated_at),
        }


class FreeTeamQuotaPool(Base):
    """複数候補から共有される原子的なクォータ台帳。"""

    __tablename__ = "free_team_quota_pools"

    id = Column(String(120), primary_key=True)
    credential_profile_id = Column(
        String(100),
        ForeignKey("free_team_credential_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_type = Column(String(40), nullable=False, index=True)
    limit_value = Column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    consumed = Column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    reserved = Column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    safety_margin_ratio = Column(
        Numeric(9, 6), nullable=False, default=Decimal("0")
    )
    safety_margin_units = Column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    window_start = Column(DateTime, nullable=True)
    window_end = Column(DateTime, nullable=True, index=True)
    reset_policy = Column(JSON, nullable=False, default=dict)
    last_provider_sync_at = Column(DateTime, nullable=True)
    provider_observed_usage = Column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    status = Column(String(40), nullable=False, default="active", index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    credential_profile = relationship(
        "FreeTeamCredentialProfile", back_populates="quota_pools"
    )

    @property
    def provider_sync_delta(self) -> Decimal:
        observed = Decimal(self.provider_observed_usage or 0)
        consumed = Decimal(self.consumed or 0)
        return max(Decimal("0"), observed - consumed)

    @property
    def safety_margin(self) -> Decimal:
        ratio = Decimal(self.safety_margin_ratio or 0)
        units = Decimal(self.safety_margin_units or 0)
        return max(units, Decimal(self.limit_value or 0) * ratio)

    @property
    def available(self) -> Decimal:
        return max(
            Decimal("0"),
            Decimal(self.limit_value or 0)
            - Decimal(self.consumed or 0)
            - Decimal(self.reserved or 0)
            - self.provider_sync_delta
            - self.safety_margin,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "credential_profile_id": self.credential_profile_id,
            "metric_type": self.metric_type,
            "limit": _number(self.limit_value),
            "consumed": _number(self.consumed),
            "reserved": _number(self.reserved),
            "available": _number(self.available),
            "safety_margin_ratio": _number(self.safety_margin_ratio),
            "safety_margin_units": _number(self.safety_margin_units),
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "reset_policy": _safe_public_mapping(self.reset_policy or {}),
            "last_provider_sync_at": _iso(self.last_provider_sync_at),
            "provider_observed_usage": _number(self.provider_observed_usage),
            "status": self.status,
        }


class FreeTeamCandidateModel(Base):
    """実行時に選択される具体的なprovider/model候補。"""

    __tablename__ = "free_team_candidate_models"

    id = Column(String(140), primary_key=True)
    credential_profile_id = Column(
        String(100),
        ForeignKey("free_team_credential_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(80), nullable=False, index=True)
    model = Column(String(240), nullable=False)
    effort = Column(String(40), nullable=True)
    priority = Column(Integer, nullable=False, default=100, index=True)
    weight = Column(Integer, nullable=False, default=1)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    quota_pool_ids = Column(JSON, nullable=False, default=list)
    capabilities = Column(JSON, nullable=False, default=list)
    quality_class = Column(String(40), nullable=False, default="standard")
    max_input_tokens = Column(Integer, nullable=False, default=32768)
    max_output_tokens = Column(Integer, nullable=False, default=2048)
    timeout_seconds = Column(Integer, nullable=False, default=120)
    max_retries = Column(Integer, nullable=False, default=0)
    cooldown_policy = Column(JSON, nullable=False, default=dict)
    tool_call_policy = Column(JSON, nullable=False, default=dict)
    privacy_class = Column(String(40), nullable=False, default="standard")
    provider_options = Column(JSON, nullable=False, default=dict)
    cooldown_until = Column(DateTime, nullable=True, index=True)
    status = Column(String(40), nullable=False, default="ready", index=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    success_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    selection_count = Column(Integer, nullable=False, default=0)
    average_latency_ms = Column(Numeric(16, 3), nullable=True)
    last_selected_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_failure_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    credential_profile = relationship(
        "FreeTeamCredentialProfile", back_populates="candidates"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "credential_profile_id": self.credential_profile_id,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort or "",
            "priority": self.priority,
            "weight": self.weight,
            "enabled": bool(self.enabled),
            "quota_pool_ids": list(self.quota_pool_ids or []),
            "capabilities": list(self.capabilities or []),
            "quality_class": self.quality_class,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "timeout": self.timeout_seconds,
            "max_retries": self.max_retries,
            "cooldown_policy": dict(self.cooldown_policy or {}),
            "tool_call_policy": _safe_public_mapping(self.tool_call_policy or {}),
            "privacy_class": self.privacy_class,
            "provider_options": _safe_public_mapping(self.provider_options or {}),
            "cooldown_until": _iso(self.cooldown_until),
            "status": self.status,
            "consecutive_failures": self.consecutive_failures,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "selection_count": self.selection_count,
            "average_latency_ms": _number(self.average_latency_ms),
        }


class FreeTeamReservation(Base):
    """実行前の最大量予約と実使用量の監査台帳。"""

    __tablename__ = "free_team_reservations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id = Column(
        String(140),
        ForeignKey("free_team_candidate_models.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    routing_profile_id = Column(String(100), nullable=False, default="free-team")
    pool_id = Column(String(100), nullable=False, index=True)
    member_key = Column(String(100), nullable=True)
    status = Column(String(32), nullable=False, default="reserved", index=True)
    estimated_usage = Column(JSON, nullable=False, default=dict)
    actual_usage = Column(JSON, nullable=False, default=dict)
    quota_pool_ids = Column(JSON, nullable=False, default=list)
    error_class = Column(String(40), nullable=True)
    fallback_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    finalized_at = Column(DateTime, nullable=True)

    candidate = relationship("FreeTeamCandidateModel")

    __table_args__ = (
        Index("ix_free_team_reservation_pool_status", "pool_id", "status"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "candidate_id": self.candidate_id,
            "routing_profile_id": self.routing_profile_id,
            "pool_id": self.pool_id,
            "member_key": self.member_key,
            "status": self.status,
            "estimated_usage": dict(self.estimated_usage or {}),
            "actual_usage": dict(self.actual_usage or {}),
            "quota_pool_ids": list(self.quota_pool_ids or []),
            "error_class": self.error_class,
            "fallback_count": self.fallback_count,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "finalized_at": _iso(self.finalized_at),
        }
