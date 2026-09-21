"""MediaOps WS7 metrics, experiment and revenue evidence models.

The rows in this module are an append-only, ACL-scoped evidence ledger.  They
store normalized observations and opaque references only; adapters, provider
responses, credentials and publication side effects are deliberately outside
this boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import synonym

from .base import Base


class MetricSnapshotSource(str, Enum):
    MANUAL = "manual"
    IMPORTED = "imported"
    API = "api"


METRIC_SNAPSHOT_SOURCE_VALUES = tuple(item.value for item in MetricSnapshotSource)


class MetricIngestionRunStatus(str, Enum):
    """Immutable provider-ingestion checkpoint status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


METRIC_INGESTION_RUN_STATUS_VALUES = tuple(item.value for item in MetricIngestionRunStatus)


class MetricCompleteness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


METRIC_COMPLETENESS_VALUES = tuple(item.value for item in MetricCompleteness)


class MetricIngestionStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


METRIC_INGESTION_STATUS_VALUES = tuple(item.value for item in MetricIngestionStatus)


class ExperimentStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"
    CANCELLED = "cancelled"


EXPERIMENT_STATUS_VALUES = tuple(item.value for item in ExperimentStatus)


class ExperimentResultStatus(str, Enum):
    COMPLETE = "complete"
    INCONCLUSIVE = "inconclusive"


EXPERIMENT_RESULT_STATUS_VALUES = tuple(item.value for item in ExperimentResultStatus)


class ExperimentAnalysisDesign(str, Enum):
    """Design provenance for a server-calculated experiment result.

    ``controlled`` is reserved for results with explicit assignment and
    exposure evidence.  ``observational`` is the safe default for imported
    or manually entered metric observations and must not be presented as a
    causal experiment.
    """

    CONTROLLED = "controlled"
    OBSERVATIONAL = "observational"


EXPERIMENT_ANALYSIS_DESIGN_VALUES = tuple(
    item.value for item in ExperimentAnalysisDesign
)

# Stable defaults make old result writers forward-compatible while keeping
# the analysis implementation itself versioned and auditable.
DEFAULT_EXPERIMENT_ANALYSIS_METHOD = "normalized_metric_comparison"
DEFAULT_EXPERIMENT_ANALYSIS_VERSION = "1"


class RevenueEventType(str, Enum):
    SALE = "sale"
    REFUND = "refund"
    CHARGEBACK = "chargeback"
    ADJUSTMENT = "adjustment"
    REVERSAL = "reversal"


REVENUE_EVENT_TYPE_VALUES = tuple(item.value for item in RevenueEventType)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class MetricSnapshot(Base):
    """One immutable, normalized metrics observation."""

    __tablename__ = "media_metric_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    persona_ref = Column(String(164), nullable=True, index=True)
    platform_account_ref = Column(String(164), nullable=True, index=True)
    content_variant_ref = Column(String(164), nullable=True, index=True)
    publication_ref = Column(String(164), nullable=True, index=True)
    period_start = Column(DateTime, nullable=True, index=True)
    period_end = Column(DateTime, nullable=True, index=True)
    observed_at = Column(DateTime, nullable=False, index=True)
    source = Column(String(16), nullable=False, index=True)
    provider = Column(String(128), nullable=False, default="manual")
    normalized_metrics = Column(JSON, nullable=False, default=dict)
    platform_metrics = Column(JSON, nullable=False, default=dict)
    # Compatibility aliases keep the wire terminology usable for callers that
    # refer to normalized metrics as simply ``metrics`` or to opaque identity
    # references as ``*_id``.  They map to the same persisted columns.
    metrics = synonym("normalized_metrics")
    persona_id = synonym("persona_ref")
    account_id = synonym("platform_account_ref")
    content_variant_id = synonym("content_variant_ref")
    publication_id = synonym("publication_ref")
    provenance = Column(JSON, nullable=False, default=list)
    completeness = Column(String(16), nullable=False, default="unknown")
    ingestion_status = Column(String(16), nullable=False, default="accepted")
    import_status = synonym("ingestion_status")
    correction_of_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_metric_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    snapshot_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "source IN ('manual', 'imported', 'api')",
            name="ck_media_metric_snapshots_source",
        ),
        CheckConstraint(
            "completeness IN ('complete', 'partial', 'unknown')",
            name="ck_media_metric_snapshots_completeness",
        ),
        CheckConstraint(
            "ingestion_status IN ('accepted', 'rejected', 'superseded')",
            name="ck_media_metric_snapshots_ingestion_status",
        ),
        CheckConstraint(
            "length(snapshot_hash) = 64",
            name="ck_media_metric_snapshots_hash",
        ),
        Index(
            "ix_media_metric_snapshots_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_metric_snapshots_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_metric_snapshots_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "persona_ref": self.persona_ref,
            "persona_id": self.persona_ref,
            "platform_account_ref": self.platform_account_ref,
            "account_id": self.platform_account_ref,
            "content_variant_ref": self.content_variant_ref,
            "content_variant_id": self.content_variant_ref,
            "publication_ref": self.publication_ref,
            "publication_id": self.publication_ref,
            "period_start": _dt(self.period_start),
            "period_end": _dt(self.period_end),
            "observed_at": _dt(self.observed_at),
            "source": self.source,
            "provider": self.provider,
            "normalized_metrics": self.normalized_metrics or {},
            "metrics": self.normalized_metrics or {},
            "platform_metrics": self.platform_metrics or {},
            "provenance": self.provenance or [],
            "completeness": self.completeness,
            "ingestion_status": self.ingestion_status,
            "import_status": self.ingestion_status,
            "correction_of_id": _uuid(self.correction_of_id),
            "snapshot_hash": self.snapshot_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class MediaMetricIngestionRun(Base):
    """Append-only, secret-free provider metrics ingestion checkpoint.

    A checkpoint is a ledger event rather than a mutable job row.  Re-fetches
    append a new event with a new idempotency key and can point at the same
    remote reference; replaying one key is always a read of the original row.
    """

    __tablename__ = "media_metric_ingestion_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    provider = Column(String(16), nullable=False, index=True)
    platform_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_accounts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    platform_account_ref = Column(String(164), nullable=True, index=True)
    window_start = Column(DateTime, nullable=True, index=True)
    window_end = Column(DateTime, nullable=True, index=True)
    cursor = Column(String(512), nullable=True)
    checkpoint = Column(JSON, nullable=False, default=dict, server_default="{}")
    status = Column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    idempotency_key = Column(String(255), nullable=False)
    request_hash = Column(String(64), nullable=False, index=True)
    observation_hash = Column(String(64), nullable=False, index=True)
    evidence = Column(JSON, nullable=False, default=list, server_default="[]")
    platform_account_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_account_revisions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    platform_account_revision_hash = Column(String(64), nullable=True)
    credential_state_hash = Column(String(64), nullable=True)
    capability_snapshot_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_provider_capability_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    capability_snapshot_hash = Column(String(64), nullable=True)
    external_action_receipt_ref = Column(String(164), nullable=True)
    remote_ref = Column(String(164), nullable=True, index=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "provider IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_metric_ingestion_runs_provider",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'uncertain')",
            name="ck_media_metric_ingestion_runs_status",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_media_metric_ingestion_runs_request_hash",
        ),
        CheckConstraint(
            "length(observation_hash) = 64",
            name="ck_media_metric_ingestion_runs_observation_hash",
        ),
        CheckConstraint(
            "platform_account_revision_hash IS NULL OR length(platform_account_revision_hash) = 64",
            name="ck_media_metric_ingestion_runs_account_revision_hash",
        ),
        CheckConstraint(
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
            name="ck_media_metric_ingestion_runs_credential_state_hash",
        ),
        CheckConstraint(
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
            name="ck_media_metric_ingestion_runs_capability_snapshot_hash",
        ),
        Index(
            "ix_media_metric_ingestion_runs_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_metric_ingestion_runs_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_metric_ingestion_runs_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "provider": self.provider,
            "platform_account_id": _uuid(self.platform_account_id),
            "platform_account_ref": self.platform_account_ref,
            "window_start": _dt(self.window_start),
            "window_end": _dt(self.window_end),
            "cursor": self.cursor,
            "checkpoint": self.checkpoint or {},
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "observation_hash": self.observation_hash,
            "evidence": self.evidence or [],
            "platform_account_revision_id": _uuid(self.platform_account_revision_id),
            "platform_account_revision_hash": self.platform_account_revision_hash,
            "capability_snapshot_id": _uuid(self.capability_snapshot_id),
            "capability_snapshot_hash": self.capability_snapshot_hash,
            "external_action_receipt_ref": self.external_action_receipt_ref,
            "remote_ref": self.remote_ref,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


# The plural spelling appeared in early planning notes.  Keep it as a stable
# Python alias while using the singular table/entity terminology internally.
MetricsSnapshot = MetricSnapshot


class Experiment(Base):
    """Experiment hypothesis and bounded variant/measurement definition."""

    __tablename__ = "media_experiments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name = Column(String(255), nullable=False)
    hypothesis = Column(Text, nullable=False)
    persona_refs = Column(JSON, nullable=False, default=list)
    account_refs = Column(JSON, nullable=False, default=list)
    persona_ids = synonym("persona_refs")
    account_ids = synonym("account_refs")
    variant_groups = Column(JSON, nullable=False, default=list)
    primary_metric = Column(String(64), nullable=False)
    secondary_metrics = Column(JSON, nullable=False, default=list)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    minimum_sample_size = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="draft", index=True)
    create_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'running', 'completed', 'inconclusive', 'cancelled')",
            name="ck_media_experiments_status",
        ),
        CheckConstraint(
            "minimum_sample_size >= 1 AND minimum_sample_size <= 10000000",
            name="ck_media_experiments_sample_size",
        ),
        CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_experiments_create_hash",
        ),
        Index("ix_media_experiments_owner_project", "owner_user_id", "project_id"),
        Index(
            "uq_media_experiments_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_experiments_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "name": self.name,
            "hypothesis": self.hypothesis,
            "persona_refs": self.persona_refs or [],
            "persona_ids": self.persona_refs or [],
            "account_refs": self.account_refs or [],
            "account_ids": self.account_refs or [],
            "variant_groups": self.variant_groups or [],
            "primary_metric": self.primary_metric,
            "secondary_metrics": self.secondary_metrics or [],
            "window_start": _dt(self.window_start),
            "window_end": _dt(self.window_end),
            "minimum_sample_size": self.minimum_sample_size,
            "status": self.status,
            "create_hash": self.create_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class ExperimentResult(Base):
    """Immutable result/evidence record for an Experiment."""

    __tablename__ = "media_experiment_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    status = Column(String(16), nullable=False, default="inconclusive")
    sample_size = Column(Integer, nullable=False)
    sample_sizes = Column(JSON, nullable=False, default=dict)
    group_metrics = Column(JSON, nullable=False, default=dict)
    metrics = synonym("group_metrics")
    winner_variant_ref = Column(String(164), nullable=True)
    confidence = Column(Float, nullable=False)
    uncertainty = Column(Float, nullable=False)
    evidence_refs = Column(JSON, nullable=False, default=list)
    # Analysis authority is explicit rather than inferred from caller input.
    # Existing writers remain observational by default; a controlled result
    # must carry both assignment and exposure evidence in its immutable row.
    analysis_method = Column(
        String(64),
        nullable=False,
        default=DEFAULT_EXPERIMENT_ANALYSIS_METHOD,
        server_default=DEFAULT_EXPERIMENT_ANALYSIS_METHOD,
    )
    analysis_version = Column(
        String(32),
        nullable=False,
        default=DEFAULT_EXPERIMENT_ANALYSIS_VERSION,
        server_default=DEFAULT_EXPERIMENT_ANALYSIS_VERSION,
    )
    analysis_design = Column(
        "analysis_design",
        String(16),
        nullable=False,
        default=ExperimentAnalysisDesign.OBSERVATIONAL.value,
        server_default=ExperimentAnalysisDesign.OBSERVATIONAL.value,
    )
    # ``design`` remains a Python compatibility alias; the persisted column
    # is explicitly named ``analysis_design`` in the evidence contract.
    design = synonym("analysis_design")
    assignment_evidence = Column(
        "assignment_evidence",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    exposure_evidence = Column(
        "exposure_evidence",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    # ``*_refs`` aliases are retained for callers that use the evidence-ref
    # naming convention used by the surrounding MediaOps tables.
    assignment_evidence_json = synonym("assignment_evidence")
    exposure_evidence_json = synonym("exposure_evidence")
    assignment_evidence_refs = synonym("assignment_evidence")
    exposure_evidence_refs = synonym("exposure_evidence")
    conclusion = Column(Text, nullable=False)
    result_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('complete', 'inconclusive')",
            name="ck_media_experiment_results_status",
        ),
        CheckConstraint(
            "sample_size >= 0 AND sample_size <= 10000000",
            name="ck_media_experiment_results_sample_size",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_media_experiment_results_confidence",
        ),
        CheckConstraint(
            "uncertainty >= 0 AND uncertainty <= 1",
            name="ck_media_experiment_results_uncertainty",
        ),
        CheckConstraint(
            "length(result_hash) = 64",
            name="ck_media_experiment_results_hash",
        ),
        CheckConstraint(
            "length(trim(analysis_method)) > 0",
            name="ck_media_experiment_results_analysis_method",
        ),
        CheckConstraint(
            "length(trim(analysis_version)) > 0",
            name="ck_media_experiment_results_analysis_version",
        ),
        CheckConstraint(
            "analysis_design IN ('controlled', 'observational')",
            name="ck_media_experiment_results_analysis_design",
        ),
        UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_media_experiment_results_idempotency",
        ),
        Index("ix_media_experiment_results_owner_project", "owner_user_id", "project_id"),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "experiment_id": _uuid(self.experiment_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "status": self.status,
            "sample_size": self.sample_size,
            "sample_sizes": self.sample_sizes or {},
            "group_metrics": self.group_metrics or {},
            "metrics": self.group_metrics or {},
            "winner_variant_ref": self.winner_variant_ref,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "evidence_refs": self.evidence_refs or [],
            "analysis_method": self.analysis_method,
            "analysis_version": self.analysis_version,
            "analysis_design": self.analysis_design,
            "design": self.analysis_design,
            "assignment_evidence": self.assignment_evidence or [],
            "assignment_evidence_refs": self.assignment_evidence or [],
            "exposure_evidence": self.exposure_evidence or [],
            "exposure_evidence_refs": self.exposure_evidence or [],
            "conclusion": self.conclusion,
            "result_hash": self.result_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ExperimentResultMetricInput(Base):
    """Immutable linkage from an experiment result to one MetricSnapshot.

    The snapshot hash is copied into this child row at write time.  Consumers
    can therefore verify that a result was calculated from the exact
    immutable observation without loading mutable provider state.  Group and
    variant references are opaque MediaOps graph identifiers only; they do
    not embed provider payloads, credentials, paths or adapter internals.
    """

    __tablename__ = "media_experiment_result_metric_inputs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_experiment_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric_snapshot_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_metric_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    metric_snapshot_hash = Column(String(64), nullable=False)
    # ``snapshot_hash`` is a convenient compatibility spelling for callers
    # that use the parent MetricSnapshot field name.
    snapshot_hash = synonym("metric_snapshot_hash")
    group_name = Column(String(64), nullable=False)
    variant_ref = Column(String(164), nullable=True)
    group_ref = synonym("group_name")
    group_id = synonym("group_name")
    variant_id = synonym("variant_ref")
    ordinal = Column(Integer, nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "length(metric_snapshot_hash) = 64",
            name="ck_media_experiment_result_metric_inputs_snapshot_hash",
        ),
        CheckConstraint(
            "length(trim(group_name)) > 0",
            name="ck_media_experiment_result_metric_inputs_group_name",
        ),
        CheckConstraint(
            "variant_ref IS NULL OR length(trim(variant_ref)) > 0",
            name="ck_media_experiment_result_metric_inputs_variant_ref",
        ),
        CheckConstraint(
            "ordinal >= 1 AND ordinal <= 1000000",
            name="ck_media_experiment_result_metric_inputs_ordinal",
        ),
        UniqueConstraint(
            "experiment_result_id",
            "ordinal",
            name="uq_media_experiment_result_metric_inputs_ordinal",
        ),
        UniqueConstraint(
            "experiment_result_id",
            "metric_snapshot_id",
            name="uq_media_experiment_result_metric_inputs_snapshot",
        ),
        Index(
            "ix_media_experiment_result_metric_inputs_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "ix_media_experiment_result_inputs_result_id",
            "experiment_result_id",
        ),
        Index(
            "ix_media_experiment_result_inputs_snapshot_id",
            "metric_snapshot_id",
        ),
        Index(
            "ix_media_experiment_result_inputs_owner_id",
            "owner_user_id",
        ),
        Index(
            "ix_media_experiment_result_inputs_project_id",
            "project_id",
        ),
        Index(
            "ix_media_experiment_result_inputs_snapshot_hash",
            "metric_snapshot_hash",
        ),
        Index(
            "ix_media_experiment_result_inputs_group_name",
            "group_name",
        ),
        Index(
            "ix_media_experiment_result_inputs_variant_ref",
            "variant_ref",
        ),
        Index(
            "ix_media_experiment_result_inputs_created_at",
            "created_at",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "experiment_result_id": _uuid(self.experiment_result_id),
            "metric_snapshot_id": _uuid(self.metric_snapshot_id),
            "snapshot_id": _uuid(self.metric_snapshot_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "metric_snapshot_hash": self.metric_snapshot_hash,
            "snapshot_hash": self.metric_snapshot_hash,
            "group_name": self.group_name,
            "group_ref": self.group_name,
            "group_id": self.group_name,
            "variant_ref": self.variant_ref,
            "variant_id": self.variant_ref,
            "ordinal": int(self.ordinal),
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


# Stable aliases for integrations/tests that refer to this linkage as an
# experiment input or a metric-snapshot input.
ExperimentResultInput = ExperimentResultMetricInput
ExperimentMetricInput = ExperimentResultMetricInput
MetricSnapshotInput = ExperimentResultMetricInput


class RevenueEvent(Base):
    """One immutable revenue or reversal event with evidence references."""

    __tablename__ = "media_revenue_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    persona_ref = Column(String(164), nullable=True, index=True)
    platform_account_ref = Column(String(164), nullable=True, index=True)
    account_ref = synonym("platform_account_ref")
    platform = Column(String(32), nullable=True, index=True)
    content_ref = Column(String(164), nullable=True, index=True)
    content_variant_ref = synonym("content_ref")
    publication_ref = Column(String(164), nullable=True, index=True)
    product_ref = Column(String(164), nullable=True, index=True)
    source = Column(String(16), nullable=False, index=True)
    provider = Column(String(128), nullable=False, default="manual")
    event_type = Column(String(16), nullable=False, index=True)
    gross_amount = Column(Float, nullable=False)
    net_amount = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False)
    event_at = Column(DateTime, nullable=False, index=True)
    settlement_at = Column(DateTime, nullable=True, index=True)
    evidence = Column(JSON, nullable=False, default=list)
    correction_of_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_revenue_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "source IN ('manual', 'imported', 'api')",
            name="ck_media_revenue_events_source",
        ),
        CheckConstraint(
            "event_type IN ('sale', 'refund', 'chargeback', 'adjustment', 'reversal')",
            name="ck_media_revenue_events_type",
        ),
        CheckConstraint(
            "length(currency) = 3",
            name="ck_media_revenue_events_currency",
        ),
        CheckConstraint(
            "length(event_hash) = 64",
            name="ck_media_revenue_events_hash",
        ),
        Index("ix_media_revenue_events_owner_project", "owner_user_id", "project_id"),
        Index(
            "uq_media_revenue_events_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_revenue_events_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "persona_ref": self.persona_ref,
            "platform_account_ref": self.platform_account_ref,
            "account_ref": self.platform_account_ref,
            "platform": self.platform,
            "content_ref": self.content_ref,
            "content_variant_ref": self.content_ref,
            "publication_ref": self.publication_ref,
            "product_ref": self.product_ref,
            "source": self.source,
            "provider": self.provider,
            "event_type": self.event_type,
            "gross_amount": self.gross_amount,
            "net_amount": self.net_amount,
            "currency": self.currency,
            "event_at": _dt(self.event_at),
            "settlement_at": _dt(self.settlement_at),
            "evidence": self.evidence or [],
            "correction_of_id": _uuid(self.correction_of_id),
            "event_hash": self.event_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


__all__ = [
    "MetricSnapshotSource",
    "METRIC_SNAPSHOT_SOURCE_VALUES",
    "MetricCompleteness",
    "METRIC_COMPLETENESS_VALUES",
    "MetricIngestionStatus",
    "METRIC_INGESTION_STATUS_VALUES",
    "ExperimentStatus",
    "EXPERIMENT_STATUS_VALUES",
    "ExperimentResultStatus",
    "EXPERIMENT_RESULT_STATUS_VALUES",
    "ExperimentAnalysisDesign",
    "EXPERIMENT_ANALYSIS_DESIGN_VALUES",
    "DEFAULT_EXPERIMENT_ANALYSIS_METHOD",
    "DEFAULT_EXPERIMENT_ANALYSIS_VERSION",
    "RevenueEventType",
    "REVENUE_EVENT_TYPE_VALUES",
    "MetricSnapshot",
    "MetricsSnapshot",
    "Experiment",
    "ExperimentResult",
    "ExperimentResultMetricInput",
    "ExperimentResultInput",
    "ExperimentMetricInput",
    "MetricSnapshotInput",
    "RevenueEvent",
]
