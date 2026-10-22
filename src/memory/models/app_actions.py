"""Durable App action activation and execution records.

``AppJob`` is the historical server-side build/test/package runner.  External
App actions have a different delivery contract: a release-qualified action is
sent to one explicitly selected execution target, and a transport restart must
never turn a status check into a second start.  Keep those records separate so
the two state machines cannot accidentally share retry or success semantics.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


APP_ACTION_MODES = {"finite", "service", "desktop"}
APP_ACTION_RUN_STATUSES = {
    "queued",
    "dispatching",
    "accepted",
    "starting",
    "running",
    "stopping",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
    "unknown",
}
APP_ACTION_DISPATCH_STATES = {"not_sent", "sent", "accepted", "unknown"}
APP_ACTION_READINESS_STATES = {
    "not_required",
    "pending",
    "ready",
    "failed",
    "unknown",
}
APP_ACTION_PROCESS_STATES = {"not_started", "running", "exited", "unknown"}


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


class AppRuntimeActivation(Base):
    """The one release whose action/UI catalog is active for an App."""

    __tablename__ = "app_runtime_activations"

    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    release_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    manifest_hash = Column(String(64), nullable=False)
    activated_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    activated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["app_id", "release_id"],
            ["app_releases.app_id", "app_releases.id"],
            name="fk_app_runtime_activations_release_app",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(manifest_hash) = 64",
            name="ck_app_runtime_activations_manifest_hash",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": _uuid(self.app_id),
            "release_id": _uuid(self.release_id),
            "manifest_hash": self.manifest_hash,
            "activated_by": _uuid(self.activated_by),
            "activated_at": _dt(self.activated_at),
        }


class AppActionRun(Base):
    """Durable one-shot delivery and execution fence for an App action.

    ``id`` is the run_id sent to the remote worker.  ``dispatch_state`` is
    deliberately separate from ``status``: a row with ``sent`` or ``unknown``
    may have reached the worker and must never be started again automatically.
    """

    __tablename__ = "app_action_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    release_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    action_key = Column(String(128), nullable=False)
    action_ref = Column(String(220), nullable=False)
    action_spec_hash = Column(String(64), nullable=False)
    target_key = Column(String(80), nullable=False)
    execution_target_id = Column(String(128), nullable=False)
    execution_target_kind = Column(String(32), nullable=False)
    execution_mode = Column(String(16), nullable=False)
    machine_id = Column(String(128), nullable=False)
    device_id = Column(String(128), nullable=False)
    saved_action_id = Column(String(128), nullable=True)
    target_sha256 = Column(String(64), nullable=True)
    target_contract_hash = Column(String(64), nullable=False)
    probe_json = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    output_schema_json = Column(JSON, nullable=True)

    # Input is retained for the durable worker/reconciliation boundary.  API
    # projections should use ``to_safe_dict`` and expose only input_hash.
    input_json = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    input_hash = Column(String(64), nullable=False)
    idempotency_key = Column(String(160), nullable=False)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    started_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    agent_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    status = Column(String(20), nullable=False, default="queued", index=True)
    dispatch_state = Column(
        String(16), nullable=False, default="not_sent", index=True
    )
    readiness_status = Column(
        String(16), nullable=False, default="pending", index=True
    )
    process_status = Column(
        String(16), nullable=False, default="not_started", index=True
    )
    exit_code = Column(Integer, nullable=True)
    error_code = Column(String(128), nullable=True)
    result_json = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    log_path = Column(Text, nullable=True)
    log_cursor = Column(Integer, nullable=False, default=0, server_default=text("0"))

    connection_epoch = Column(String(128), nullable=True)
    remote_journal_epoch = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    dispatch_started_at = Column(DateTime, nullable=True)
    dispatch_sent_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["app_id", "release_id"],
            ["app_releases.app_id", "app_releases.id"],
            name="fk_app_action_runs_release_app",
            ondelete="RESTRICT",
        ),
        # This is the server-side duplicate tap/retry fence.  The service
        # compares action_ref, project_id and input_hash before replaying the
        # existing row, so key reuse across different work is a conflict.
        UniqueConstraint(
            "app_id",
            "started_by",
            "idempotency_key",
            name="uq_app_action_runs_app_starter_idempotency",
        ),
        CheckConstraint(
            "action_key <> '' AND length(action_key) <= 128",
            name="ck_app_action_runs_action_key",
        ),
        CheckConstraint(
            "length(action_ref) BETWEEN 1 AND 220",
            name="ck_app_action_runs_action_ref",
        ),
        CheckConstraint(
            "length(action_spec_hash) = 64",
            name="ck_app_action_runs_action_spec_hash",
        ),
        CheckConstraint(
            "execution_target_kind IN ('server','pc_bridge')",
            name="ck_app_action_runs_execution_target_kind",
        ),
        CheckConstraint(
            "execution_mode IN ('finite','service','desktop')",
            name="ck_app_action_runs_execution_mode",
        ),
        CheckConstraint(
            "length(machine_id) BETWEEN 1 AND 128",
            name="ck_app_action_runs_machine_id",
        ),
        CheckConstraint(
            "length(device_id) BETWEEN 1 AND 128",
            name="ck_app_action_runs_device_id",
        ),
        CheckConstraint(
            "target_sha256 IS NULL OR length(target_sha256) = 64",
            name="ck_app_action_runs_target_sha256",
        ),
        CheckConstraint(
            "length(target_contract_hash) = 64",
            name="ck_app_action_runs_target_contract_hash",
        ),
        CheckConstraint(
            "length(input_hash) = 64",
            name="ck_app_action_runs_input_hash",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="ck_app_action_runs_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('queued','dispatching','accepted','starting','running','stopping','succeeded','failed','cancelled','blocked','unknown')",
            name="ck_app_action_runs_status",
        ),
        CheckConstraint(
            "dispatch_state IN ('not_sent','sent','accepted','unknown')",
            name="ck_app_action_runs_dispatch_state",
        ),
        CheckConstraint(
            "readiness_status IN ('not_required','pending','ready','failed','unknown')",
            name="ck_app_action_runs_readiness_status",
        ),
        CheckConstraint(
            "process_status IN ('not_started','running','exited','unknown')",
            name="ck_app_action_runs_process_status",
        ),
        CheckConstraint(
            "log_cursor >= 0",
            name="ck_app_action_runs_log_cursor",
        ),
        Index(
            "ix_app_action_runs_app_status_created",
            "app_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_app_action_runs_device_status_created",
            "device_id",
            "status",
            "created_at",
        ),
    )

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled", "blocked"}

    def to_safe_dict(self) -> Dict[str, Any]:
        """Return API-safe metadata without input, local paths or raw output."""

        return {
            "id": _uuid(self.id),
            "app_id": _uuid(self.app_id),
            "project_id": _uuid(self.project_id),
            "release_id": _uuid(self.release_id),
            "action_key": self.action_key,
            "action_ref": self.action_ref,
            "target_key": self.target_key,
            "execution_target_id": self.execution_target_id,
            "execution_target_kind": self.execution_target_kind,
            "execution_mode": self.execution_mode,
            "machine_id": self.machine_id,
            "device_id": self.device_id,
            "saved_action_id": self.saved_action_id,
            "status": self.status,
            "dispatch_state": self.dispatch_state,
            "readiness_status": self.readiness_status,
            "process_status": self.process_status,
            "exit_code": self.exit_code,
            "error_code": self.error_code,
            "input_hash": self.input_hash,
            "idempotency_key": self.idempotency_key,
            "owner_user_id": _uuid(self.owner_user_id),
            "started_by": _uuid(self.started_by),
            "agent_run_id": _uuid(self.agent_run_id),
            "log_cursor": int(self.log_cursor or 0),
            "connection_epoch": self.connection_epoch,
            "remote_journal_epoch": self.remote_journal_epoch,
            "created_at": _dt(self.created_at),
            "dispatch_started_at": _dt(self.dispatch_started_at),
            "dispatch_sent_at": _dt(self.dispatch_sent_at),
            "accepted_at": _dt(self.accepted_at),
            "started_at": _dt(self.started_at),
            "heartbeat_at": _dt(self.heartbeat_at),
            "ended_at": _dt(self.ended_at),
        }

    def to_dict(self, *, include_input: bool = False) -> Dict[str, Any]:
        result = self.to_safe_dict()
        if include_input:
            result["input_json"] = self.input_json or {}
        return result


__all__ = [
    "APP_ACTION_DISPATCH_STATES",
    "APP_ACTION_MODES",
    "APP_ACTION_PROCESS_STATES",
    "APP_ACTION_READINESS_STATES",
    "APP_ACTION_RUN_STATUSES",
    "AppActionRun",
    "AppRuntimeActivation",
]
