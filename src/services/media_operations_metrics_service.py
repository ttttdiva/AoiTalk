"""MediaOps WS7 metrics, experiments and revenue evidence service.

This service is intentionally provider-neutral.  Public commands accept only
manual/imported observations; provider-authoritative API observations enter via
the server-owned adapter boundary below.  Every path validates a closed
semantic shape, applies the existing MediaOperationsService ACL boundary, and
persists immutable rows.  It never calls a provider, publication endpoint, or
Memory service.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..memory.models.media_operations_metrics import (
    DEFAULT_EXPERIMENT_ANALYSIS_METHOD,
    EXPERIMENT_RESULT_STATUS_VALUES,
    EXPERIMENT_STATUS_VALUES,
    METRIC_COMPLETENESS_VALUES,
    METRIC_INGESTION_RUN_STATUS_VALUES,
    METRIC_SNAPSHOT_SOURCE_VALUES,
    REVENUE_EVENT_TYPE_VALUES,
    Experiment,
    ExperimentResult,
    ExperimentResultStatus,
    ExperimentStatus,
    MediaMetricIngestionRun,
    MetricSnapshot,
    RevenueEvent,
)
from ..memory.models.media_operations import (
    MEDIA_PLATFORM_VALUES,
    Persona,
    PersonaRevision,
)
from ..memory.models.media_operations_content import ContentVariant, ContentVariantRevision
from ..memory.models.media_operations_research import ContentItem
from ..memory.models.media_operations_setup import PlatformAccount
from ..memory.models.media_operations_setup import PlatformAccountRevision
from ..memory.models.media_operations_credentials import MediaPlatformCredential
from ..memory.models.operations import ExternalAction, ExternalActionReceipt

try:
    from ..memory.models.media_provider_capability import MediaProviderCapabilitySnapshot
except ImportError:  # pragma: no cover - transitional import while migrations land
    MediaProviderCapabilitySnapshot = None  # type: ignore[assignment,misc]

try:
    from ..memory.models.media_operations_metrics import ExperimentResultMetricInput
except ImportError:  # pragma: no cover - transitional import while migrations land
    ExperimentResultMetricInput = None  # type: ignore[assignment,misc]
from .media_operations_service import (
    MediaOperationsAuthorizationError,
    MediaOperationsConflictError,
    MediaOperationsNotFoundError,
    MediaOperationsService,
    MediaOperationsValidationError,
    _actor_id,
    _actor_field,
    _as_uuid,
    _bounded_page,
    _optional_text,
    _required_text,
    _validated_resource_url,
    _validated_sha256,
    sha256_json,
)


_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,163}$")
_METRIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_MAX_METRIC_KEYS = 32
_MAX_PLATFORMS = 16
_MAX_EVIDENCE = 32
_MAX_REFS = 32
_MAX_GROUPS = 12
_MAX_GROUP_METRICS = 32

# A closed but intentionally provider-neutral vocabulary.  Unknown metrics are
# rejected instead of becoming an unbounded JSON escape hatch.
ALLOWED_METRIC_KEYS = frozenset(
    {
        "impressions",
        "reach",
        "views",
        "likes",
        "comments",
        "shares",
        "saves",
        "clicks",
        "conversions",
        "followers",
        "watch_time_seconds",
        "engagement_rate",
        "click_through_rate",
        "conversion_rate",
        "sample_size",
        "gross_revenue",
        "net_revenue",
        "refunds",
        "cost",
    }
)

# A caller may submit a hand-authored or file-imported observation, but it may
# not elevate that observation to a provider-authoritative API record by
# setting ``source="api"``.  The token is intentionally private and identity
# compared; a boolean/header/client marker is not an authority boundary.
_SERVER_INGESTION_TOKEN = object()
_LOWER_IS_BETTER_METRICS = frozenset({"cost", "refunds"})
_CHECKPOINT_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CHECKPOINT_SENSITIVE_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
        "key",
        "path",
        "payload",
        "response",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderMetricsAdapterIdentity:
    """Server-owned identity stamped by a provider metrics adapter.

    This tiny value is useful for adapter implementations and tests without
    exposing credentials or raw provider responses.  ``server_owned`` is
    deliberately immutable and the ingestion methods still require the
    private module token, so a REST/agent caller cannot self-authorise.
    """

    provider: str
    adapter_ref: str
    status: str = "unverified"
    server_owned: bool = True


def _actor_is_non_human(actor: Any) -> bool:
    actor_type = str(_actor_field(actor, "actor_type", "") or "").strip().lower()
    return actor_type in {"agent", "system"}


def _uuid_ref(value: Any) -> UUID | None:
    """Parse UUID-shaped refs without rejecting legitimate opaque refs."""

    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _safe_revenue_dict(row: RevenueEvent) -> dict[str, Any]:
    """Project revenue with an explicit settled-vs-estimated distinction."""

    payload = row.to_safe_dict()
    settled = row.settlement_at is not None
    payload["settlement_status"] = "settled" if settled else "estimated"
    payload["revenue_authority"] = (
        "provider_settled" if row.source == "api" and settled else
        "imported_settled" if row.source == "imported" and settled else
        "manual_settled" if row.source == "manual" and settled else
        "manual_estimate" if row.source == "manual" else
        "provider_estimate" if row.source == "api" else
        "imported_estimate"
    )
    payload["is_estimated"] = not settled
    return payload


def _static_provider_operation_status(provider: str, operation: str) -> str | None:
    """Read the optional WS05 registry without making the subsystem required.

    A missing registry is treated as unavailable for this authority path.
    When present, an unverified/manual provider is never promoted by an
    adapter that merely self-reports ``verified``.
    """

    try:
        from .media_provider_capability_registry import effective_operation_status
    except ImportError:
        # The registry is optional for this authority path, but there is no
        # legacy compatibility module to fall back to.  Keep the import
        # failure fail-closed rather than retaining a reference to a module
        # that is not part of the repository (and therefore cannot be shipped
        # in an Enterprise handoff).
        return None
    try:
        value = effective_operation_status(provider, operation)
    except Exception:
        return "unavailable"
    if isinstance(value, Mapping):
        value = value.get("status") or value.get("state")
    if value in (None, ""):
        return "unavailable"
    return str(getattr(value, "value", value)).strip().lower()


def _assert_server_owned_adapter(adapter: Any, operation: str) -> ProviderMetricsAdapterIdentity:
    """Validate a server-stamped adapter identity before API/revenue ingest."""

    # Do not accept a caller-shaped object that merely carries a
    # ``server_owned=True`` flag.  Provider adapters must be stamped with this
    # module's immutable identity type before they can cross the authority
    # boundary; REST/agent payloads have no way to construct one.
    if not isinstance(adapter, ProviderMetricsAdapterIdentity) or not adapter.server_owned:
        raise MediaOperationsAuthorizationError("provider ingestion requires a server-owned adapter")
    provider = _required_text(getattr(adapter, "provider", None), "adapter.provider", 128).lower()
    adapter_ref = _required_text(getattr(adapter, "adapter_ref", None), "adapter.adapter_ref", 164)
    if not _REF_RE.fullmatch(adapter_ref):
        raise MediaOperationsValidationError("adapter_ref must be an opaque reference")
    if provider not in MEDIA_PLATFORM_VALUES:
        raise MediaOperationsValidationError("adapter.provider is unsupported")
    status = str(getattr(adapter, "status", "unverified") or "unverified").strip().lower()
    registry_status = _static_provider_operation_status(provider, operation)
    if status != "verified" or registry_status is None or registry_status in {
        "unknown", "unverified", "manual", "unsupported", "unavailable", "configured",
    }:
        raise MediaOperationsValidationError("provider ingestion is unavailable or unverified")
    if registry_status not in ("available", "verified", "implemented", "enabled", "automatable"):
        raise MediaOperationsValidationError("provider ingestion is unavailable or unverified")
    return ProviderMetricsAdapterIdentity(
        provider=provider,
        adapter_ref=adapter_ref,
        status="verified",
    )


def _assert_human_write(actor: Any) -> None:
    """Reject agent/system principals at metrics authority boundaries."""

    if _actor_is_non_human(actor):
        raise MediaOperationsAuthorizationError(
            "metrics authority mutations require a human or administrator"
        )


def _assert_ingestion_actor(actor: Any) -> None:
    """Permit a server/system principal but never an agent principal.

    Provider adapters run on behalf of a server-owned boundary.  A product
    agent may propose work, but it cannot call this authority path to stamp a
    provider API observation or settled payout.
    """

    actor_type = str(_actor_field(actor, "actor_type", "") or "").strip().lower()
    if actor_type == "agent":
        raise MediaOperationsAuthorizationError(
            "agent principals cannot ingest provider-authoritative metrics"
        )


def _safe_checkpoint(value: Any) -> dict[str, Any]:
    """Normalize a small cursor/checkpoint object without retaining raw data.

    Provider responses and credentials must never be copied into the durable
    ingestion ledger.  Checkpoints therefore accept only scalar progress
    values under non-sensitive keys; nested objects, bytes and path/secret
    shaped fields are rejected rather than redacted heuristically.
    """

    if value in (None, ""):
        return {}
    payload = _mapping(value, "checkpoint")
    if len(payload) > 16:
        raise MediaOperationsValidationError("checkpoint has too many fields")
    result: dict[str, Any] = {}
    for raw_key, raw_value in payload.items():
        key = _required_text(raw_key, "checkpoint key", 64).lower()
        if not _CHECKPOINT_KEY_RE.fullmatch(key) or any(
            token in key for token in _CHECKPOINT_SENSITIVE_TOKENS
        ):
            raise MediaOperationsValidationError("checkpoint contains an unsafe field")
        if isinstance(raw_value, bool) or raw_value is None:
            result[key] = raw_value
            continue
        if isinstance(raw_value, (int, float)):
            if not math.isfinite(float(raw_value)) or abs(float(raw_value)) > 1_000_000_000_000:
                raise MediaOperationsValidationError("checkpoint number is outside the supported range")
            result[key] = raw_value
            continue
        if isinstance(raw_value, str):
            result[key] = _required_text(raw_value, f"checkpoint.{key}", 512)
            continue
        raise MediaOperationsValidationError("checkpoint values must be scalar")
    return result


def _optional_sha256(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    return _validated_sha256(value, label)


def _calculated_experiment_result(
    experiment: Experiment,
    *,
    sample_size: int,
    sample_sizes: Mapping[str, int],
    group_metrics: Mapping[str, Mapping[str, int | float]],
) -> tuple[str, str | None, float, float]:
    """Derive result status/winner/confidence from normalized observations.

    ``winner_variant_ref``, ``confidence`` and ``uncertainty`` supplied by a
    caller are hints only.  The persisted values come from this deterministic
    calculation.  A winner requires minimum total sample, a positive sample
    in every configured group and the primary metric in every group.
    """

    groups = list(experiment.variant_groups or [])
    minimum = int(experiment.minimum_sample_size or 1)
    coverage = max(0.0, min(1.0, sample_size / minimum))
    group_names = [str(item.get("name")) for item in groups]
    complete_samples = (
        sample_size >= minimum
        and sum(int(sample_sizes.get(name, 0) or 0) for name in group_names)
        == sample_size
        and all(int(sample_sizes.get(name, 0) or 0) > 0 for name in group_names)
    )
    primary = str(experiment.primary_metric or "").strip().lower()
    values: dict[str, float] = {}
    if complete_samples:
        for name in group_names:
            metric_values = group_metrics.get(name)
            if not isinstance(metric_values, Mapping) or primary not in metric_values:
                complete_samples = False
                break
            raw = metric_values[primary]
            number = float(raw)
            if not math.isfinite(number):
                complete_samples = False
                break
            values[name] = number

    if not complete_samples or len(values) < 2:
        confidence = round(coverage, 6) if coverage < 1 else 0.0
        return ExperimentResultStatus.INCONCLUSIVE.value, None, confidence, round(1.0 - confidence, 6)

    reverse = primary not in _LOWER_IS_BETTER_METRICS
    ranked = sorted(values.items(), key=lambda pair: pair[1], reverse=reverse)
    best_name, best_value = ranked[0]
    second_value = ranked[1][1]
    if math.isclose(best_value, second_value, rel_tol=1e-12, abs_tol=1e-12):
        return ExperimentResultStatus.INCONCLUSIVE.value, None, 0.0, 1.0

    # Margin is a bounded, reproducible confidence proxy.  It is deliberately
    # conservative and carries no statistical claim beyond normalized input.
    denominator = max(abs(best_value), abs(second_value), 1.0)
    margin = max(0.0, min(1.0, abs(best_value - second_value) / denominator))
    confidence = round(margin * coverage, 6)
    winner_refs = [str(item) for item in groups if str(item.get("name")) == best_name]
    winner_ref = None
    if winner_refs:
        refs = groups[group_names.index(best_name)].get("variant_refs") or []
        if len(refs) == 1:
            winner_ref = str(refs[0])
    if winner_ref is None:
        return ExperimentResultStatus.INCONCLUSIVE.value, None, confidence, round(1.0 - confidence, 6)
    return ExperimentResultStatus.COMPLETE.value, winner_ref, confidence, round(1.0 - confidence, 6)


async def get_effective_metric_snapshot(
    session: Any,
    snapshot: MetricSnapshot | UUID | str,
    *,
    accepted_only: bool = True,
) -> MetricSnapshot:
    """Resolve the accepted terminal head of a metric correction chain.

    Metric snapshots are append-only.  A correction points at the observation
    it supersedes, so consumers must not pick a row by ``created_at`` alone or
    accidentally aggregate both the original and its correction.  This helper
    follows accepted corrections, choosing the newest deterministic child at
    each step.  Cycles are rejected rather than silently truncating the chain.

    The helper deliberately performs no ACL decision; callers must authorize
    the returned row (and the starting row) in their own scope before exposing
    it.  ``accepted_only`` is retained for read-side compatibility, but an
    effective head is always required to be accepted when it is enabled.
    """

    if isinstance(snapshot, MetricSnapshot):
        current = snapshot
    else:
        parsed = _as_uuid(snapshot, "snapshot_id")
        assert parsed is not None
        current = await _scalar_compat(
            session,
            select(MetricSnapshot).where(MetricSnapshot.id == parsed).limit(1),
        )
        if current is None:
            raise MediaOperationsNotFoundError("snapshot_id not found")

    seen: set[UUID] = set()
    while True:
        current_id = getattr(current, "id", None)
        if current_id is None:
            raise MediaOperationsValidationError("metric snapshot has no id")
        if current_id in seen:
            raise MediaOperationsValidationError("metric correction chain contains a cycle")
        seen.add(current_id)
        if accepted_only and getattr(current, "ingestion_status", None) != "accepted":
            # A superseded starting row may still have an accepted correction;
            # resolve it below.  A rejected row must never become authority.
            if getattr(current, "ingestion_status", None) == "rejected":
                raise MediaOperationsValidationError("metric snapshot is not accepted")
        children = await _scalars_compat(
            session,
            select(MetricSnapshot)
            .where(MetricSnapshot.correction_of_id == current.id)
            .order_by(MetricSnapshot.created_at.desc(), MetricSnapshot.id.desc()),
        )
        accepted_children = [
            child
            for child in children
            if not accepted_only or getattr(child, "ingestion_status", None) == "accepted"
        ]
        if len(accepted_children) > 1:
            # A correction chain is a linear authority history.  Multiple
            # accepted children are malformed/ambiguous and must never be
            # resolved by an arbitrary timestamp winner.
            raise MediaOperationsValidationError(
                "metric correction chain has multiple accepted heads"
            )
        if not accepted_children:
            if accepted_only and getattr(current, "ingestion_status", None) != "accepted":
                raise MediaOperationsValidationError("metric snapshot has no accepted effective head")
            return current
        parent_scope = {
            "owner_user_id": getattr(current, "owner_user_id", None),
            "project_id": getattr(current, "project_id", None),
            "persona_ref": getattr(current, "persona_ref", None),
            "platform_account_ref": getattr(current, "platform_account_ref", None),
            "content_variant_ref": getattr(current, "content_variant_ref", None),
            "publication_ref": getattr(current, "publication_ref", None),
            "provider": getattr(current, "provider", None),
            "period_start": getattr(current, "period_start", None),
            "period_end": getattr(current, "period_end", None),
        }
        child = accepted_children[0]
        for field, target in parent_scope.items():
            if getattr(child, field, None) != target:
                raise MediaOperationsValidationError(
                    f"metric correction chain scope mismatch: {field}"
                )
        current = child


# Stable aliases used by dashboard/read-side callers and early WS06 notes.
effective_metric_snapshot_head = get_effective_metric_snapshot
resolve_effective_metric_snapshot = get_effective_metric_snapshot


async def _scalar_compat(session: Any, statement: Any) -> Any:
    """Small module-level SQLAlchemy compatibility helper for read utilities."""

    result = session.execute(statement)
    if isawaitable(result):
        result = await result
    scalar = getattr(result, "scalar", None)
    if callable(scalar):
        return scalar()
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        return scalar_one_or_none()
    return result


async def _scalars_compat(session: Any, statement: Any) -> list[Any]:
    result = session.execute(statement)
    if isawaitable(result):
        result = await result
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        return list(scalars().all())
    return list(result or [])


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MediaOperationsValidationError(f"{label} must be an object")
    return value


def _ref(value: Any, label: str, *, allow_uuid: bool = True) -> str:
    rendered = _required_text(value, label, 164)
    if not _REF_RE.fullmatch(rendered):
        raise MediaOperationsValidationError(f"{label} must be an opaque reference")
    if not allow_uuid and _as_uuid(rendered, label, required=False) is not None:
        raise MediaOperationsValidationError(f"{label} must not be a database identifier")
    return rendered


def _optional_ref(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    return _ref(value, label)


def _finite_number(value: Any, label: str, *, allow_negative: bool = True) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MediaOperationsValidationError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1_000_000_000_000:
        raise MediaOperationsValidationError(f"{label} is outside the supported range")
    if not allow_negative and number < 0:
        raise MediaOperationsValidationError(f"{label} must not be negative")
    if isinstance(value, int):
        return value
    return number


def _validate_revenue_signs(
    event_type: str,
    gross: int | float,
    net: int | float,
) -> None:
    """Apply an unambiguous signed-ledger convention to revenue events."""

    if event_type == "sale":
        if gross < 0 or net < 0:
            raise MediaOperationsValidationError("sale amounts must be non-negative")
        if net > gross:
            raise MediaOperationsValidationError("net_amount must not exceed gross_amount")
        return
    if event_type in {"refund", "chargeback", "reversal"}:
        # Reversals are non-positive deltas in the append-only ledger. At
        # least one side must be strictly negative so a no-op correction
        # cannot masquerade as a settled refund. Gross/net are deliberately
        # independent here: provider ledgers may report a zero gross with a
        # negative fee adjustment (or vice versa).
        if gross > 0 or net > 0 or (gross == 0 and net == 0):
            raise MediaOperationsValidationError(
                f"{event_type} amounts must be non-positive with a negative delta"
            )
        return
    # Adjustments may be either a debit or credit, but their values must share
    # a sign so gross/net cannot describe contradictory directions.
    if gross != 0 and net != 0 and (gross < 0) != (net < 0):
        raise MediaOperationsValidationError("adjustment amounts must share a sign")


def _metric_map(value: Any, label: str, *, allow_negative: bool = False) -> dict[str, int | float]:
    payload = _mapping(value, label)
    if len(payload) > _MAX_METRIC_KEYS:
        raise MediaOperationsValidationError(f"{label} has too many metrics")
    result: dict[str, int | float] = {}
    for raw_key, raw_value in payload.items():
        key = _required_text(raw_key, f"{label} key", 64).lower()
        if not _METRIC_KEY_RE.fullmatch(key) or key not in ALLOWED_METRIC_KEYS:
            raise MediaOperationsValidationError(f"unsupported metric key: {key}")
        result[key] = _finite_number(raw_value, f"{label}.{key}", allow_negative=allow_negative)
    return result


def _platform_metric_map(value: Any) -> dict[str, dict[str, int | float]]:
    payload = _mapping(value or {}, "platform_metrics")
    if len(payload) > _MAX_PLATFORMS:
        raise MediaOperationsValidationError("platform_metrics has too many platforms")
    result: dict[str, dict[str, int | float]] = {}
    for raw_platform, metrics in payload.items():
        platform = _required_text(raw_platform, "platform_metrics platform", 32).lower()
        if platform not in MEDIA_PLATFORM_VALUES:
            raise MediaOperationsValidationError(
                "platform_metrics contains an unsupported MediaOps platform"
            )
        result[platform] = _metric_map(metrics, f"platform_metrics.{platform}")
    return result


def _datetime(value: Any, label: str, *, required: bool = True) -> datetime | None:
    if value in (None, ""):
        if required:
            raise MediaOperationsValidationError(f"{label} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            if required:
                raise MediaOperationsValidationError(f"{label} is required")
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MediaOperationsValidationError(f"{label} must be an ISO datetime") from exc
    else:
        raise MediaOperationsValidationError(f"{label} must be a datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _enum(value: Any, values: Sequence[str], label: str, default: str | None = None) -> str:
    candidate = default if value in (None, "") and default is not None else value
    rendered = str(getattr(candidate, "value", candidate) or "").strip().lower()
    if rendered not in values:
        raise MediaOperationsValidationError(f"{label} is invalid")
    return rendered


def _provider(value: Any, *, source: str) -> str:
    if value in (None, ""):
        return "manual" if source == "manual" else "unspecified"
    rendered = _required_text(value, "provider", 128)
    # Providers are labels, not URLs, credentials or arbitrary payloads.
    if any(character.isspace() for character in rendered) or not _PLATFORM_RE.fullmatch(
        rendered.lower().replace("-", "_")
    ):
        raise MediaOperationsValidationError("provider must be a bounded identifier")
    return rendered


def _normalize_evidence(value: Any, label: str = "evidence", *, required: bool = True) -> list[dict[str, Any]]:
    if value is None:
        if required:
            raise MediaOperationsValidationError(f"{label} is required")
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if required and not value:
        raise MediaOperationsValidationError(f"{label} must contain at least one item")
    if len(value) > _MAX_EVIDENCE:
        raise MediaOperationsValidationError(f"{label} has too many items")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        payload = {
            key: item
            for key, item in _mapping(raw, f"{label} item").items()
            if item is not None
        }
        kind = str(payload.get("kind", payload.get("type", "")) or "").strip().lower()
        if kind in {"url", "source_url"}:
            if set(payload) - {"kind", "type", "url", "label", "note", "sha256"}:
                raise MediaOperationsValidationError(f"{label} url item has unknown fields")
            ref = _validated_resource_url(payload.get("url"))
            item: dict[str, Any] = {"kind": "url", "ref": ref}
        elif kind == "file":
            if set(payload) - {
                "kind",
                "type",
                "sha256",
                "role",
                "label",
                "note",
                "format_version",
                "row_index",
            }:
                raise MediaOperationsValidationError(
                    f"{label} file item has unknown fields"
                )
            item = {
                "kind": "file",
                "sha256": _validated_sha256(
                    payload.get("sha256"),
                    f"{label}.sha256",
                ),
            }
            if payload.get("role") is not None:
                item["role"] = _required_text(
                    payload.get("role"),
                    f"{label}.role",
                    64,
                )
            if payload.get("format_version") is not None:
                item["format_version"] = _required_text(
                    payload.get("format_version"),
                    f"{label}.format_version",
                    64,
                )
            if payload.get("row_index") is not None:
                row_index = payload.get("row_index")
                if isinstance(row_index, bool) or not isinstance(row_index, int):
                    raise MediaOperationsValidationError(
                        f"{label}.row_index must be an integer"
                    )
                if row_index < 0 or row_index > 1_000_000:
                    raise MediaOperationsValidationError(
                        f"{label}.row_index is out of range"
                    )
                item["row_index"] = row_index
        elif kind == "artifact":
            if set(payload) - {"kind", "type", "sha256", "mime_type", "label", "note"}:
                raise MediaOperationsValidationError(f"{label} artifact item has unknown fields")
            digest = _validated_sha256(payload.get("sha256"), f"{label}.sha256")
            mime = _required_text(payload.get("mime_type"), f"{label}.mime_type", 255)
            item = {"kind": "artifact", "sha256": digest, "mime_type": mime}
        elif kind in {"record", "snapshot", "experiment", "result", "revenue_event", "publication"}:
            if set(payload) - {"kind", "type", "ref", "sha256", "role", "label", "note"}:
                raise MediaOperationsValidationError(f"{label} record item has unknown fields")
            item = {"kind": kind, "ref": _ref(payload.get("ref"), f"{label}.ref")}
            if payload.get("sha256") is not None:
                item["sha256"] = _validated_sha256(payload.get("sha256"), f"{label}.sha256")
            if payload.get("role") is not None:
                item["role"] = _required_text(payload.get("role"), f"{label}.role", 64)
        else:
            raise MediaOperationsValidationError(
                f"{label}.kind must be url, artifact, file, or record"
            )

        # Labels/notes are presentation metadata but remain bounded.  Never
        # copy arbitrary fields into a durable evidence row.
        if payload.get("label") is not None:
            item["label"] = _required_text(payload.get("label"), f"{label}.label", 255)
        if payload.get("note") is not None:
            item["note"] = _required_text(payload.get("note"), f"{label}.note", 2000)
        digest = sha256_json(item)
        if digest in seen:
            raise MediaOperationsValidationError(f"duplicate {label} item")
        seen.add(digest)
        item["evidence_hash"] = digest
        result.append(item)
    return result


def _normalize_refs(value: Any, label: str, *, maximum: int = _MAX_REFS) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(value) > maximum:
        raise MediaOperationsValidationError(f"{label} has too many items")
    result: list[str] = []
    for raw in value:
        parsed = _ref(raw, label)
        if parsed not in result:
            result.append(parsed)
    return result


def _variant_groups(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MediaOperationsValidationError("variant_groups must be a list")
    if len(value) < 2 or len(value) > _MAX_GROUPS:
        raise MediaOperationsValidationError("variant_groups must contain between 2 and 12 groups")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in value:
        payload = _mapping(raw, "variant_group")
        if set(payload) - {"name", "variant_refs", "refs"}:
            raise MediaOperationsValidationError("variant_group has unknown fields")
        name = _required_text(payload.get("name"), "variant_group.name", 64)
        key = name.casefold()
        if key in names:
            raise MediaOperationsValidationError("variant group names must be unique")
        names.add(key)
        refs = _normalize_refs(payload.get("variant_refs", payload.get("refs")), "variant_group.variant_refs", maximum=64)
        if not refs:
            raise MediaOperationsValidationError("variant groups require at least one variant reference")
        result.append({"name": name, "variant_refs": refs})
    return result


def _sample_sizes(value: Any, groups: Sequence[Mapping[str, Any]], sample_size: int) -> dict[str, int]:
    if value is None:
        return {str(group["name"]): 0 for group in groups}
    payload = _mapping(value, "sample_sizes")
    if len(payload) > _MAX_GROUPS:
        raise MediaOperationsValidationError("sample_sizes has too many groups")
    group_names = {str(group["name"]) for group in groups}
    if set(payload) - group_names:
        raise MediaOperationsValidationError("sample_sizes contains an unknown variant group")
    result: dict[str, int] = {}
    for group in groups:
        name = str(group["name"])
        raw = payload.get(name, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 10_000_000:
            raise MediaOperationsValidationError("sample_sizes values must be non-negative integers")
        result[name] = raw
    if sum(result.values()) > sample_size:
        raise MediaOperationsValidationError("sample_sizes cannot exceed sample_size")
    return result


class MediaOperationsMetricsService(MediaOperationsService):
    """ACL-scoped immutable metrics/experiment/revenue service."""

    async def _get_row(self, session: Any, model: Any, entity_id: UUID | str, label: str, *, for_update: bool = False) -> Any:
        parsed = _as_uuid(entity_id, label)
        assert parsed is not None
        statement = select(model).where(model.id == parsed).limit(1)
        if for_update:
            statement = statement.with_for_update()
        row = await self._scalar(session, statement)
        if row is None:
            raise MediaOperationsNotFoundError(f"{label} not found")
        return row

    async def _find_scoped_idempotency(self, session: Any, model: Any, *, owner_user_id: UUID, project_id: UUID | None, key: str) -> Any:
        conditions = [model.idempotency_key == key]
        if project_id is None:
            conditions.extend([model.project_id.is_(None), model.owner_user_id == owner_user_id])
        else:
            conditions.append(model.project_id == project_id)
        return await self._scalar(session, select(model).where(*conditions).limit(1))

    async def _scope_for_create(self, session: Any, actor: Any, project_id: Any) -> tuple[UUID, UUID | None]:
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        actor_id = await self._assert_create_scope(session, actor, project_uuid)
        return actor_id, project_uuid

    async def _scope_for_list(self, session: Any, actor: Any, project_id: Any) -> tuple[UUID, UUID | None]:
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        actor_id = _actor_id(actor)
        if project_uuid is not None:
            await self._assert_access(session, actor, project_id=project_uuid, permission="read")
        return actor_id, project_uuid

    async def _list(self, session: Any, actor: Any, model: Any, *, project_id: Any, limit: Any, offset: Any) -> list[Any]:
        actor_id, project_uuid = await self._scope_for_list(session, actor, project_id)
        page_limit, page_offset = _bounded_page(limit, offset)
        if project_uuid is None:
            condition = (model.project_id.is_(None)) & (model.owner_user_id == actor_id)
        else:
            condition = model.project_id == project_uuid
        return await self._scalars(
            session,
            select(model).where(condition).order_by(model.created_at.desc(), model.id.desc()).limit(page_limit).offset(page_offset),
        )

    async def _validate_reference_graph(
        self,
        session: Any,
        actor: Any,
        *,
        project_uuid: UUID | None,
        persona_ref: Any = None,
        platform_account_ref: Any = None,
        content_variant_ref: Any = None,
    ) -> None:
        """Validate UUID-shaped refs against the authorized MediaOps graph.

        Opaque provider refs remain valid for imported/manual evidence, but a
        value that is also a local UUID must resolve to the exact project and
        ACL-visible entity.  This prevents accidental cross-character joins
        based on a string that merely happens to match another row.
        """

        checks = (
            (persona_ref, Persona, "persona_ref"),
            (platform_account_ref, PlatformAccount, "platform_account_ref"),
            (content_variant_ref, ContentVariant, "content_variant_ref"),
        )
        resolved: dict[str, Any] = {}
        for raw_ref, model, label in checks:
            if raw_ref in (None, ""):
                continue
            parsed = _uuid_ref(raw_ref)
            if parsed is None and model is not PlatformAccount:
                continue
            if parsed is not None:
                row = await self._scalar(session, select(model).where(model.id == parsed).limit(1))
            else:
                # Platform account references are commonly provider-facing
                # ``account_ref`` strings rather than local UUIDs.  Resolve
                # them when present so an opaque value cannot silently cross
                # Character/project boundaries.
                row = await self._scalar(
                    session,
                    select(model).where(model.account_ref == str(raw_ref)).limit(1),
                )
                if row is None:
                    continue
            if row is None:
                raise MediaOperationsNotFoundError(f"{label} not found")
            row_project = getattr(row, "project_id", None)
            if row_project != project_uuid:
                raise MediaOperationsValidationError(
                    f"{label} is outside the requested project"
                )
            await self._assert_entity_access(session, actor, row, permission="read")
            resolved[label] = row

        persona = resolved.get("persona_ref")
        account = resolved.get("platform_account_ref")
        variant = resolved.get("content_variant_ref")
        if persona is not None and account is not None:
            account_persona_id = getattr(account, "persona_id", None)
            if account_persona_id is not None and account_persona_id != getattr(persona, "id", None):
                raise MediaOperationsValidationError(
                    "platform_account_ref does not belong to persona_ref"
                )
        if persona is not None and variant is not None:
            # ContentVariant → ContentItem → PersonaRevision is the canonical
            # Character graph.  A missing optional link remains valid for
            # legacy rows, but an explicit link must match exactly.
            content_item_id = getattr(variant, "content_item_id", None)
            if content_item_id is not None:
                content_item = await self._scalar(
                    session,
                    select(ContentItem).where(ContentItem.id == content_item_id).limit(1),
                )
                revision_id = getattr(content_item, "persona_revision_id", None) if content_item is not None else None
                if revision_id is not None:
                    revision = await self._scalar(
                        session,
                        select(PersonaRevision).where(PersonaRevision.id == revision_id).limit(1),
                    )
                    if revision is not None and getattr(revision, "persona_id", None) != getattr(persona, "id", None):
                        raise MediaOperationsValidationError(
                            "content_variant_ref does not belong to persona_ref"
                        )

    async def _prepare_scope_refs(
        self,
        session: Any,
        actor: Any,
        *,
        project_uuid: UUID | None,
        persona_ref: Any = None,
        platform_account_ref: Any = None,
        content_variant_ref: Any = None,
    ) -> tuple[str | None, str | None, str | None]:
        normalized_persona = _optional_ref(persona_ref, "persona_ref")
        normalized_account = _optional_ref(platform_account_ref, "platform_account_ref")
        normalized_variant = _optional_ref(content_variant_ref, "content_variant_ref")
        await self._validate_reference_graph(
            session,
            actor,
            project_uuid=project_uuid,
            persona_ref=normalized_persona,
            platform_account_ref=normalized_account,
            content_variant_ref=normalized_variant,
        )
        return normalized_persona, normalized_account, normalized_variant

    async def _result_safe_dict(self, session: Any, row: ExperimentResult) -> dict[str, Any]:
        """Return a safe result projection enriched from the child input ledger."""

        payload = row.to_safe_dict()
        # Lane A adds the child table in the 0027 migration.  Keep old
        # deployments/read paths usable while that migration is rolling out.
        if ExperimentResultMetricInput is None:
            return payload
        try:
            inputs = await self._scalars(
                session,
                select(ExperimentResultMetricInput)
                .where(ExperimentResultMetricInput.experiment_result_id == row.id)
                .order_by(
                    ExperimentResultMetricInput.ordinal.asc(),
                    ExperimentResultMetricInput.id.asc(),
                ),
            )
        except Exception:
            # A read against a pre-0027 database should not turn the entire
            # dashboard unavailable.  The immutable result row remains safe.
            return payload
        ids = [str(getattr(item, "metric_snapshot_id")) for item in inputs]
        hashes = [str(getattr(item, "metric_snapshot_hash")) for item in inputs]
        if ids:
            payload.setdefault("input_metric_snapshot_ids", ids)
            payload.setdefault("input_metric_snapshot_hashes", hashes)
            payload.setdefault(
                "metric_inputs",
                [
                    {
                        "metric_snapshot_id": str(getattr(item, "metric_snapshot_id")),
                        "metric_snapshot_hash": getattr(item, "metric_snapshot_hash", None),
                        "group_name": getattr(item, "group_name", None),
                        "variant_ref": getattr(item, "variant_ref", None),
                        "ordinal": int(getattr(item, "ordinal", index)),
                    }
                    for index, item in enumerate(inputs)
                ],
            )
        return payload

    async def _resolve_experiment_metric_inputs(
        self,
        session: Any,
        actor: Any,
        experiment: Experiment,
        inputs: Any,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int], dict[str, dict[str, int | float]]]:
        """Authorize and aggregate MetricSnapshot inputs for an experiment.

        Every input is checked against the ACL-scoped graph, character/account
        refs, experiment window, accepted effective correction head and the
        caller-supplied expected hash.  No client-provided metric values are
        consumed; aggregation is derived solely from the immutable snapshot
        rows.
        """

        if isinstance(inputs, (str, bytes)) or not isinstance(inputs, Sequence):
            raise MediaOperationsValidationError("inputs must be a list")
        if not inputs:
            raise MediaOperationsValidationError("inputs must contain at least one metric snapshot")
        if len(inputs) > 1000:
            raise MediaOperationsValidationError("inputs has too many metric snapshots")
        group_defs = {
            str(group.get("name")): group
            for group in (experiment.variant_groups or [])
            if isinstance(group, Mapping)
        }
        if len(group_defs) < 2:
            raise MediaOperationsValidationError("experiment must define at least two variant groups")
        persona_refs = {str(item) for item in (experiment.persona_refs or [])}
        account_refs = {str(item) for item in (experiment.account_refs or [])}
        seen_ids: set[str] = set()
        normalized: list[dict[str, Any]] = []
        sample_sizes: dict[str, int] = {name: 0 for name in group_defs}
        group_metrics: dict[str, dict[str, int | float]] = {
            name: {} for name in group_defs
        }
        for ordinal, raw_input in enumerate(inputs):
            item = _mapping(raw_input, f"inputs[{ordinal}]")
            snapshot_id = item.get(
                "metric_snapshot_id",
                item.get("snapshot_id", item.get("id")),
            )
            expected_hash = item.get(
                "metric_snapshot_hash",
                item.get(
                    "expected_snapshot_hash",
                    item.get("expected_hash", item.get("snapshot_hash")),
                ),
            )
            if snapshot_id in (None, "") or expected_hash in (None, ""):
                raise MediaOperationsValidationError(
                    "each metric input requires metric_snapshot_id and expected hash"
                )
            parsed_id = _as_uuid(snapshot_id, f"inputs[{ordinal}].metric_snapshot_id")
            assert parsed_id is not None
            id_key = str(parsed_id)
            if id_key in seen_ids:
                raise MediaOperationsValidationError("inputs contains a duplicate metric snapshot")
            seen_ids.add(id_key)
            expected_hash_value = _validated_sha256(
                expected_hash,
                f"inputs[{ordinal}].metric_snapshot_hash",
            )
            snapshot = await self._get_row(
                session,
                MetricSnapshot,
                parsed_id,
                f"inputs[{ordinal}].metric_snapshot_id",
            )
            await self._assert_entity_access(session, actor, snapshot, permission="read")
            if snapshot.project_id != experiment.project_id:
                raise MediaOperationsValidationError("metric input is outside the experiment project")
            # Result lineage must pin the exact accepted head supplied by the
            # caller. An ancestor/superseded or rejected snapshot may still
            # be resolvable for read-side projections, but it is not a valid
            # experiment input: accepting it would let the same observation
            # enter a result under two different immutable identities.
            if getattr(snapshot, "ingestion_status", None) != "accepted":
                raise MediaOperationsValidationError(
                    "metric input must be an accepted effective snapshot"
                )
            effective = await get_effective_metric_snapshot(session, snapshot)
            if getattr(effective, "id", None) != getattr(snapshot, "id", None):
                raise MediaOperationsValidationError(
                    "metric input must be the effective accepted snapshot"
                )
            await self._assert_entity_access(session, actor, effective, permission="read")
            if effective.project_id != experiment.project_id:
                raise MediaOperationsValidationError("effective metric input is outside the experiment project")
            if effective.snapshot_hash != expected_hash_value:
                raise MediaOperationsConflictError(
                    "metric input hash does not match the effective accepted snapshot"
                )
            if effective.ingestion_status != "accepted":
                raise MediaOperationsValidationError("metric input must be accepted")
            observed = effective.observed_at
            if observed is None or observed < experiment.window_start or observed > experiment.window_end:
                raise MediaOperationsValidationError("metric input is outside the experiment window")

            persona_ref = getattr(effective, "persona_ref", None)
            account_ref = getattr(effective, "platform_account_ref", None)
            if persona_refs and str(persona_ref) not in persona_refs:
                raise MediaOperationsValidationError("metric input is outside the experiment character set")
            if account_refs and str(account_ref) not in account_refs:
                raise MediaOperationsValidationError("metric input is outside the experiment account set")
            # If the experiment is explicitly scoped to a Character/account,
            # an unbound observation cannot be smuggled into the analysis.
            if persona_refs and persona_ref in (None, ""):
                raise MediaOperationsValidationError("metric input is missing its experiment character")
            if account_refs and account_ref in (None, ""):
                raise MediaOperationsValidationError("metric input is missing its experiment account")

            group_name = _required_text(
                item.get("group_name", item.get("variant_group")),
                f"inputs[{ordinal}].group_name",
                64,
            )
            if group_name not in group_defs:
                raise MediaOperationsValidationError("metric input group_name is not in the experiment")
            variant_ref = _optional_ref(item.get("variant_ref"), f"inputs[{ordinal}].variant_ref")
            variant_refs = {
                str(value)
                for value in (group_defs[group_name].get("variant_refs") or [])
            }
            if variant_ref is not None and variant_ref not in variant_refs:
                raise MediaOperationsValidationError("metric input variant_ref is not in its group")

            normalized_metrics = _metric_map(
                getattr(effective, "normalized_metrics", None) or {},
                f"inputs[{ordinal}].normalized_metrics",
            )
            if not normalized_metrics:
                # Platform-only imports are valid; derive a metric map by
                # summing each platform's normalized values deterministically.
                platform_values = _platform_metric_map(
                    getattr(effective, "platform_metrics", None) or {}
                )
                for platform in sorted(platform_values):
                    for metric, value in platform_values[platform].items():
                        normalized_metrics[metric] = normalized_metrics.get(metric, 0) + value
            raw_count = normalized_metrics.get("sample_size", 1)
            if isinstance(raw_count, bool) or not isinstance(raw_count, (int, float)):
                raise MediaOperationsValidationError("metric input sample_size must be numeric")
            count = int(raw_count)
            if count < 0 or count > 10_000_000:
                raise MediaOperationsValidationError("metric input sample_size is invalid")
            # A snapshot with an explicit zero sample is still an observation;
            # use one row for coverage so a zero-valued metric cannot disappear.
            contribution = count if count > 0 else 1
            sample_sizes[group_name] += contribution
            for metric, value in normalized_metrics.items():
                if metric == "sample_size":
                    continue
                group_metrics[group_name][metric] = (
                    group_metrics[group_name].get(metric, 0) + value
                )
            normalized.append(
                {
                    "metric_snapshot_id": str(effective.id),
                    "metric_snapshot_hash": effective.snapshot_hash,
                    "group_name": group_name,
                    "variant_ref": variant_ref,
                    "source_snapshot_id": str(snapshot.id),
                }
            )

        # Hashing and child ordinals are canonicalized below; input request
        # order is intentionally not authority.
        normalized.sort(
            key=lambda value: (
                str(value["group_name"]),
                str(value.get("variant_ref") or ""),
                str(value["metric_snapshot_id"]),
                str(value["metric_snapshot_hash"]),
            )
        )
        total_sample = sum(sample_sizes.values())
        return normalized, total_sample, sample_sizes, group_metrics

    async def _validate_external_action_receipt(
        self,
        session: Any,
        actor: Any,
        receipt_ref: Any,
        *,
        project_uuid: UUID | None,
        provider: str,
    ) -> str:
        """Resolve an explicitly claimed Trusted-Operations receipt.

        A caller may also provide a provider's own settlement reference; that
        opaque value is evidence but is not an AoiTalk action receipt.  This
        helper is only used for the explicit ``external_action_receipt_ref``
        field and therefore refuses claims that do not resolve to an immutable
        receipt row bound to the same project/provider action.
        """

        normalized = _ref(receipt_ref, "external_action_receipt_ref")
        parsed = _uuid_ref(normalized)
        predicates = [
            ExternalActionReceipt.id == parsed,
        ] if parsed is not None else [
            ExternalActionReceipt.provider_receipt_ref == normalized,
            ExternalActionReceipt.remote_resource_id == normalized,
        ]
        statement = (
            select(ExternalActionReceipt, ExternalAction)
            .join(ExternalAction, ExternalAction.id == ExternalActionReceipt.action_id)
            .where(or_(*predicates))
            .limit(1)
        )
        result = session.execute(statement)
        if isawaitable(result):
            result = await result
        row = result.first() if hasattr(result, "first") else None
        if row is None:
            raise MediaOperationsConflictError(
                "external_action_receipt_ref does not resolve to a stored receipt"
            )
        receipt, action = row
        await self._assert_entity_access(session, actor, action, permission="read")
        if action.project_id != project_uuid or str(getattr(action, "platform", "") or "").lower() != provider:
            raise MediaOperationsConflictError(
                "external action receipt is outside the provider/project scope"
            )
        if getattr(action, "status", None) != "succeeded":
            raise MediaOperationsConflictError(
                "external action receipt is not a successful provider receipt"
            )
        if getattr(receipt, "confirmation_level", None) not in {"provider_confirmed", "reconciled"}:
            raise MediaOperationsConflictError(
                "external action receipt lacks provider confirmation"
            )
        return normalized

    async def _validate_provider_ingestion_pins(
        self,
        session: Any,
        actor: Any,
        *,
        identity: ProviderMetricsAdapterIdentity,
        operation: str = "analytics",
        project_uuid: UUID | None,
        platform_account_id: Any,
        platform_account_ref: Any = None,
        platform_account_revision_id: Any,
        platform_account_revision_hash: Any,
        credential_state_hash: Any,
        capability_snapshot_id: Any,
        capability_snapshot_hash: Any,
        external_action_receipt_ref: Any,
        settlement_receipt_ref: Any = None,
    ) -> tuple[UUID | None, str | None, str | None, UUID | None, str | None, str | None, str | None]:
        """Validate account/revision/credential/capability pins as one graph."""

        account_uuid = _as_uuid(platform_account_id, "platform_account_id", required=False)
        receipt_candidate = (
            external_action_receipt_ref
            if external_action_receipt_ref not in (None, "")
            else settlement_receipt_ref
        )
        if account_uuid is None and platform_account_ref not in (None, ""):
            account_by_ref = await self._scalar(
                session,
                select(PlatformAccount)
                .where(PlatformAccount.account_ref == str(platform_account_ref))
                .limit(1),
            )
            if account_by_ref is not None:
                account_uuid = account_by_ref.id
        if account_uuid is None:
            if any(
                value not in (None, "")
                for value in (
                    platform_account_revision_id,
                    platform_account_revision_hash,
                    credential_state_hash,
                    capability_snapshot_id,
                    capability_snapshot_hash,
                )
            ):
                raise MediaOperationsValidationError("provider pins require platform_account_id")
            receipt = (
                None
                if receipt_candidate in (None, "")
                else _ref(receipt_candidate, "receipt_ref")
            )
            if external_action_receipt_ref not in (None, ""):
                await self._validate_external_action_receipt(
                    session,
                    actor,
                    external_action_receipt_ref,
                    project_uuid=project_uuid,
                    provider=identity.provider,
                )
            return None, None, None, None, None, None, receipt
        required = {
            "platform_account_revision_id": platform_account_revision_id,
            "platform_account_revision_hash": platform_account_revision_hash,
            "credential_state_hash": credential_state_hash,
            "capability_snapshot_id": capability_snapshot_id,
            "capability_snapshot_hash": capability_snapshot_hash,
            "receipt_ref": receipt_candidate,
        }
        # A provider observation bound to an account must be replayable against
        # the exact capability/credential graph and an external receipt.
        if any(value in (None, "") for value in required.values()):
            missing = next(name for name, value in required.items() if value in (None, ""))
            raise MediaOperationsValidationError(f"provider ingestion requires {missing}")
        account = await self._get_row(session, PlatformAccount, account_uuid, "platform_account_id")
        await self._assert_entity_access(session, actor, account, permission="read")
        if account.project_id != project_uuid or account.platform != identity.provider:
            raise MediaOperationsValidationError("provider account pin is outside the requested graph")
        if platform_account_ref not in (None, "") and str(platform_account_ref) not in {
            str(getattr(account, "account_ref", "")),
            str(account.id),
        }:
            raise MediaOperationsValidationError("platform_account_ref does not match platform_account_id")
        revision_uuid = _as_uuid(platform_account_revision_id, "platform_account_revision_id")
        assert revision_uuid is not None
        revision = await self._get_row(session, PlatformAccountRevision, revision_uuid, "platform_account_revision_id")
        await self._assert_entity_access(session, actor, revision, permission="read")
        revision_hash = _validated_sha256(platform_account_revision_hash, "platform_account_revision_hash")
        stored_revision_hash = getattr(
            revision,
            "revision_hash",
            getattr(revision, "content_hash", None),
        )
        if revision.platform_account_id != account.id or revision.project_id != project_uuid or stored_revision_hash != revision_hash:
            raise MediaOperationsConflictError("platform account revision pin does not match")
        credential_hash = _validated_sha256(credential_state_hash, "credential_state_hash")
        capability_uuid = _as_uuid(capability_snapshot_id, "capability_snapshot_id")
        assert capability_uuid is not None
        if MediaProviderCapabilitySnapshot is None:
            raise MediaOperationsValidationError("provider capability snapshot model is unavailable")
        capability = await self._get_row(session, MediaProviderCapabilitySnapshot, capability_uuid, "capability_snapshot_id")
        await self._assert_entity_access(session, actor, capability, permission="read")
        capability_hash = _validated_sha256(capability_snapshot_hash, "capability_snapshot_hash")
        if (
            capability.platform_account_id != account.id
            or capability.account_revision_id != revision.id
            or capability.account_revision != getattr(revision, "version", None)
            or capability.project_id != project_uuid
            or capability.provider != identity.provider
            or capability.operation != operation
            or capability.status not in {"automatable", "verified", "available", "implemented", "enabled"}
            or capability.snapshot_hash != capability_hash
            or capability.credential_state_hash != credential_hash
        ):
            raise MediaOperationsConflictError("provider capability pin does not match")
        credential = await self._get_row(
            session,
            MediaPlatformCredential,
            capability.credential_id,
            "capability credential_id",
        )
        if (
            credential.platform_account_id != account.id
            or credential.project_id != project_uuid
            or credential.revision != capability.credential_revision
            or credential.state_hash != credential_hash
            or credential.status != "verified"
        ):
            raise MediaOperationsConflictError("provider credential pin does not match")
        if external_action_receipt_ref not in (None, ""):
            await self._validate_external_action_receipt(
                session,
                actor,
                external_action_receipt_ref,
                project_uuid=project_uuid,
                provider=identity.provider,
            )
        receipt = _ref(receipt_candidate, "receipt_ref")
        return account_uuid, str(revision.id), revision_hash, capability_uuid, capability_hash, credential_hash, receipt

    async def _record_metric_ingestion_run(
        self,
        session: Any,
        actor: Any,
        *,
        identity: ProviderMetricsAdapterIdentity,
        project_uuid: UUID | None,
        persona_ref: Any = None,
        platform_account_ref: Any = None,
        platform_account_id: Any = None,
        window_start: Any = None,
        window_end: Any = None,
        cursor: Any = None,
        checkpoint: Any = None,
        status: Any = "succeeded",
        idempotency_key: Any,
        request_payload: Mapping[str, Any],
        observation_payload: Mapping[str, Any],
        evidence: Any,
        platform_account_revision_id: Any = None,
        platform_account_revision_hash: Any = None,
        credential_state_hash: Any = None,
        capability_snapshot_id: Any = None,
        capability_snapshot_hash: Any = None,
        external_action_receipt_ref: Any = None,
        remote_ref: Any = None,
    ) -> dict[str, Any]:
        """Append one immutable provider-ingestion checkpoint.

        The metric snapshot is written before this method is called.  That
        ordering keeps the two existing append-only ledgers independently
        replayable while this row provides a durable, secret-free checkpoint.
        A duplicate key is returned only when its request hash matches; a
        caller cannot mutate a previously observed cursor or evidence.
        """

        actor_id = _actor_id(actor)
        provider = identity.provider
        if provider not in MEDIA_PLATFORM_VALUES:
            raise MediaOperationsValidationError("adapter.provider is unsupported")
        key = _required_text(idempotency_key, "idempotency_key", 255)
        status_value = _enum(
            status,
            METRIC_INGESTION_RUN_STATUS_VALUES,
            "status",
            "succeeded",
        )
        account_ref = _optional_ref(platform_account_ref, "platform_account_ref")
        account_uuid, revision_id_value, account_revision_hash, capability_uuid, capability_hash, credential_hash, receipt_ref = await self._validate_provider_ingestion_pins(
            session,
            actor,
            identity=identity,
            operation="analytics",
            project_uuid=project_uuid,
            platform_account_id=platform_account_id or _uuid_ref(account_ref),
            platform_account_ref=account_ref,
            platform_account_revision_id=platform_account_revision_id,
            platform_account_revision_hash=platform_account_revision_hash,
            credential_state_hash=credential_state_hash,
            capability_snapshot_id=capability_snapshot_id,
            capability_snapshot_hash=capability_snapshot_hash,
            external_action_receipt_ref=external_action_receipt_ref,
        )
        if account_uuid is not None:
            account_row = await self._get_row(session, PlatformAccount, account_uuid, "platform_account_id")
            stored_ref = getattr(account_row, "account_ref", None)
            if account_ref is not None and account_ref not in {stored_ref, str(account_uuid)}:
                raise MediaOperationsValidationError("platform_account_ref does not match platform_account_id")
            persona_uuid = _uuid_ref(persona_ref)
            account_persona_id = getattr(account_row, "persona_id", None)
            if persona_uuid is not None and account_persona_id is not None and account_persona_id != persona_uuid:
                raise MediaOperationsValidationError("platform_account_id does not belong to persona_ref")
        start = _datetime(window_start, "window_start", required=False)
        end = _datetime(window_end, "window_end", required=False)
        if start and end and start > end:
            raise MediaOperationsValidationError("window_start must not be after window_end")
        cursor_value = _optional_text(cursor, "cursor", 512)
        checkpoint_value = _safe_checkpoint(checkpoint)
        evidence_value = _normalize_evidence(evidence, "ingestion evidence", required=True)
        if not any(item.get("role") == "provider_adapter" for item in evidence_value):
            raise MediaOperationsAuthorizationError(
                "ingestion evidence must include the server-owned adapter"
            )
        account_revision_uuid = _as_uuid(
            revision_id_value or platform_account_revision_id,
            "platform_account_revision_id",
            required=False,
        )
        # Values above are already cross-row validated when an account is
        # present.  Keep optional compatibility for account-less fixtures.
        account_revision_hash = account_revision_hash or _optional_sha256(
            platform_account_revision_hash,
            "platform_account_revision_hash",
        )
        credential_hash = credential_hash or _optional_sha256(credential_state_hash, "credential_state_hash")
        capability_snapshot_uuid = capability_uuid or _as_uuid(
            capability_snapshot_id,
            "capability_snapshot_id",
            required=False,
        )
        capability_hash = capability_hash or _optional_sha256(
            capability_snapshot_hash,
            "capability_snapshot_hash",
        )
        receipt_ref = receipt_ref or (None if external_action_receipt_ref in (None, "") else _ref(
            external_action_receipt_ref,
            "external_action_receipt_ref",
        ))
        remote_reference = None if remote_ref in (None, "") else _ref(remote_ref, "remote_ref")
        request_document = {
            **dict(request_payload),
            "provider": provider,
            "adapter_ref": identity.adapter_ref,
            "platform_account_ref": account_ref,
            "platform_account_id": str(account_uuid) if account_uuid else None,
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat() if end else None,
            "cursor": cursor_value,
            "checkpoint": checkpoint_value,
            "platform_account_revision_id": str(account_revision_uuid) if account_revision_uuid else None,
            "platform_account_revision_hash": account_revision_hash,
            "credential_state_hash": credential_hash,
            "capability_snapshot_id": str(capability_snapshot_uuid) if capability_snapshot_uuid else None,
            "capability_snapshot_hash": capability_hash,
            "external_action_receipt_ref": receipt_ref,
            "remote_ref": remote_reference,
        }
        request_hash = sha256_json(request_document)
        observation_hash = sha256_json(
            {
                **dict(observation_payload),
                "provider": provider,
                "evidence": evidence_value,
            }
        )
        existing = await self._find_scoped_idempotency(
            session,
            MediaMetricIngestionRun,
            owner_user_id=actor_id,
            project_id=project_uuid,
            key=key,
        )
        if existing is not None:
            if existing.request_hash != request_hash or existing.observation_hash != observation_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used with a different ingestion run"
                )
            return existing.to_safe_dict()
        row = MediaMetricIngestionRun(
            id=uuid4(),
            owner_user_id=actor_id,
            project_id=project_uuid,
            provider=provider,
            platform_account_id=account_uuid,
            platform_account_ref=account_ref,
            window_start=start,
            window_end=end,
            cursor=cursor_value,
            checkpoint=checkpoint_value,
            status=status_value,
            idempotency_key=key,
            request_hash=request_hash,
            observation_hash=observation_hash,
            evidence=evidence_value,
            platform_account_revision_id=account_revision_uuid,
            platform_account_revision_hash=account_revision_hash,
            credential_state_hash=credential_hash,
            capability_snapshot_id=capability_snapshot_uuid,
            capability_snapshot_hash=capability_hash,
            external_action_receipt_ref=receipt_ref,
            remote_ref=remote_reference,
            created_by=actor_id,
        )
        session.add(row)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(
                session,
                MediaMetricIngestionRun,
                owner_user_id=actor_id,
                project_id=project_uuid,
                key=key,
            )
            if recovered is not None and recovered.request_hash == request_hash and recovered.observation_hash == observation_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError(
                "provider ingestion run conflicts with an existing record"
            ) from exc
        return row.to_safe_dict()

    async def create_metric_snapshot(self, session: Any | None = None, actor: Any | None = None, *, project_id: Any = None, persona_ref: Any = None, persona_id: Any = None, platform_account_ref: Any = None, account_id: Any = None, content_variant_ref: Any = None, content_variant_id: Any = None, publication_ref: Any = None, publication_id: Any = None, period_start: Any = None, period_end: Any = None, observed_at: Any = None, source: Any = "manual", provider: Any = None, normalized_metrics: Any = None, metrics: Any = None, platform_metrics: Any = None, provenance: Any = None, evidence: Any = None, completeness: Any = "unknown", ingestion_status: Any = "accepted", import_status: Any = None, correction_of_id: Any = None, idempotency_key: Any = None, _ingestion_token: Any = None, _defer_commit: bool = False) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        trusted_ingestion = _ingestion_token is _SERVER_INGESTION_TOKEN
        if not trusted_ingestion:
            _assert_human_write(actor)
        actor_id, project_uuid = await self._scope_for_create(session, actor, project_id)
        persona_ref = persona_ref if persona_ref is not None else persona_id
        platform_account_ref = platform_account_ref if platform_account_ref is not None else account_id
        content_variant_ref = content_variant_ref if content_variant_ref is not None else content_variant_id
        publication_ref = publication_ref if publication_ref is not None else publication_id
        if import_status is not None:
            ingestion_status = import_status
        source_value = _enum(source, METRIC_SNAPSHOT_SOURCE_VALUES, "source", "manual")
        if source_value == "api" and not trusted_ingestion:
            raise MediaOperationsAuthorizationError(
                "source=api is reserved for server-owned provider ingestion"
            )
        if not trusted_ingestion and provider not in (None, "", "manual", "imported"):
            raise MediaOperationsValidationError(
                "provider is server-authoritative only for provider ingestion"
            )
        provider_value = (
            _provider(provider, source=source_value)
            if trusted_ingestion
            else source_value
        )
        completeness_value = _enum(completeness, METRIC_COMPLETENESS_VALUES, "completeness", "unknown")
        ingestion_value = _enum(ingestion_status, ("accepted", "rejected", "superseded"), "ingestion_status", "accepted")
        metrics_value = _metric_map(normalized_metrics if normalized_metrics is not None else (metrics or {}), "normalized_metrics")
        platforms_value = _platform_metric_map(platform_metrics)
        evidence_value = _normalize_evidence(provenance if provenance is not None else evidence, "provenance", required=True)
        persona_ref, platform_account_ref, content_variant_ref = await self._prepare_scope_refs(
            session,
            actor,
            project_uuid=project_uuid,
            persona_ref=persona_ref,
            platform_account_ref=platform_account_ref,
            content_variant_ref=content_variant_ref,
        )
        start = _datetime(period_start, "period_start", required=False)
        end = _datetime(period_end, "period_end", required=False)
        observed = _datetime(observed_at, "observed_at") or datetime.utcnow()
        if start and end and start > end:
            raise MediaOperationsValidationError("period_start must not be after period_end")
        correction = None
        if correction_of_id not in (None, ""):
            correction = await self._get_row(session, MetricSnapshot, correction_of_id, "correction_of_id")
            await self._assert_entity_access(session, actor, correction, permission="write")
            if correction.project_id != project_uuid or correction.owner_user_id != actor_id:
                raise MediaOperationsValidationError(
                    "correction must remain in the same owner/project scope"
                )
            if correction.ingestion_status == "rejected":
                raise MediaOperationsValidationError(
                    "a rejected metric snapshot cannot be corrected"
                )
            correction_context = (
                ("persona_ref", persona_ref, correction.persona_ref),
                ("platform_account_ref", platform_account_ref, correction.platform_account_ref),
                ("content_variant_ref", content_variant_ref, correction.content_variant_ref),
                ("publication_ref", publication_ref, correction.publication_ref),
                ("provider", provider_value, correction.provider),
                ("period_start", start, correction.period_start),
                ("period_end", end, correction.period_end),
            )
            for label, incoming, target in correction_context:
                if incoming != target:
                    raise MediaOperationsValidationError(
                        f"correction {label} must match its target snapshot"
                    )
        key = _required_text(idempotency_key, "idempotency_key", 255)
        payload = {
            "project_id": str(project_uuid) if project_uuid else None,
            "persona_ref": _optional_ref(persona_ref, "persona_ref"),
            "platform_account_ref": _optional_ref(platform_account_ref, "platform_account_ref"),
            "content_variant_ref": _optional_ref(content_variant_ref, "content_variant_ref"),
            "publication_ref": _optional_ref(publication_ref, "publication_ref"),
            "period_start": start.isoformat() if start else None,
            "period_end": end.isoformat() if end else None,
            "observed_at": observed.isoformat(),
            "source": source_value,
            "provider": provider_value,
            "normalized_metrics": metrics_value,
            "platform_metrics": platforms_value,
            "provenance": evidence_value,
            "completeness": completeness_value,
            "ingestion_status": ingestion_value,
            "correction_of_id": str(correction.id) if correction else None,
        }
        snapshot_hash = sha256_json(payload)
        existing = await self._find_scoped_idempotency(session, MetricSnapshot, owner_user_id=actor_id, project_id=project_uuid, key=key)
        if existing is not None:
            if existing.snapshot_hash != snapshot_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different metrics")
            return existing.to_safe_dict()
        row_payload = dict(payload)
        row_payload.pop("project_id", None)
        row_payload.update(
            {
                "period_start": start,
                "period_end": end,
                "observed_at": observed,
                "correction_of_id": correction.id if correction else None,
            }
        )
        row = MetricSnapshot(id=uuid4(), owner_user_id=actor_id, project_id=project_uuid, **row_payload, snapshot_hash=snapshot_hash, idempotency_key=key, created_by=actor_id)
        session.add(row)
        try:
            if _defer_commit:
                await self._flush_only(session)
            else:
                await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(session, MetricSnapshot, owner_user_id=actor_id, project_id=project_uuid, key=key)
            if recovered is not None and recovered.snapshot_hash == snapshot_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError("metrics snapshot conflicts with an existing record") from exc
        return row.to_safe_dict()

    async def ingest_provider_metric_snapshot(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        adapter: Any,
        observation: Mapping[str, Any],
        project_id: Any = None,
        persona_ref: Any = None,
        platform_account_ref: Any = None,
        content_variant_ref: Any = None,
        publication_ref: Any = None,
        platform_account_id: Any = None,
        cursor: Any = None,
        checkpoint: Any = None,
        status: Any = "succeeded",
        platform_account_revision_id: Any = None,
        platform_account_revision_hash: Any = None,
        credential_state_hash: Any = None,
        capability_snapshot_id: Any = None,
        capability_snapshot_hash: Any = None,
        external_action_receipt_ref: Any = None,
        remote_ref: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        """Ingest one provider observation through a server-owned adapter.

        ``observation`` is already normalized by the adapter.  Raw provider
        responses, credentials, paths and caller-selected ``source``/provider
        values are rejected before the append-only ledger is touched.
        """

        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        _assert_ingestion_actor(actor)
        identity = _assert_server_owned_adapter(adapter, "analytics")
        payload = _mapping(observation, "observation")
        allowed = {
            "period_start", "period_end", "observed_at", "normalized_metrics",
            "metrics", "platform_metrics", "provenance", "evidence",
            "completeness", "ingestion_status", "import_status", "correction_of_id",
            "cursor", "checkpoint", "status", "platform_account_id",
            "window_start", "window_end",
            "platform_account_revision_id", "platform_account_revision_hash",
            "credential_state_hash", "capability_snapshot_id",
            "capability_snapshot_hash", "external_action_receipt_ref", "remote_ref",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise MediaOperationsValidationError(
                "provider observation contains unsupported fields"
            )
        evidence = payload.get("provenance", payload.get("evidence"))
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            raise MediaOperationsValidationError("provider observation requires evidence")
        # The adapter identity is itself durable evidence, while no raw
        # provider response is copied into the row.
        evidence_items = list(evidence)
        evidence_items.append({
            "kind": "record",
            "ref": identity.adapter_ref,
            "role": "provider_adapter",
        })
        effective_account_ref = platform_account_ref
        observation_account_id = payload.get("platform_account_id", platform_account_id)
        if observation_account_id not in (None, "") and effective_account_ref in (None, ""):
            account_uuid = _as_uuid(observation_account_id, "platform_account_id")
            assert account_uuid is not None
            # The existing snapshot graph validator resolves UUID-shaped
            # account refs against the authorized PlatformAccount row before
            # appending the provider observation.
            effective_account_ref = str(account_uuid)
        result = await self.create_metric_snapshot(
            session,
            actor,
            project_id=project_id,
            persona_ref=persona_ref,
            platform_account_ref=effective_account_ref,
            content_variant_ref=content_variant_ref,
            publication_ref=publication_ref,
            period_start=payload.get("period_start"),
            period_end=payload.get("period_end"),
            observed_at=payload.get("observed_at"),
            source="api",
            provider=identity.provider,
            normalized_metrics=payload.get("normalized_metrics"),
            metrics=payload.get("metrics"),
            platform_metrics=payload.get("platform_metrics"),
            provenance=evidence_items,
            completeness=payload.get("completeness", "unknown"),
            ingestion_status=payload.get("ingestion_status", payload.get("import_status", "accepted")),
            correction_of_id=payload.get("correction_of_id"),
            idempotency_key=idempotency_key,
            _ingestion_token=_SERVER_INGESTION_TOKEN,
            _defer_commit=True,
        )
        project_uuid = _as_uuid(result.get("project_id"), "project_id", required=False)
        observation_document = {
            "snapshot_id": result.get("id"),
            "period_start": result.get("period_start"),
            "period_end": result.get("period_end"),
            "observed_at": result.get("observed_at"),
            "normalized_metrics": result.get("normalized_metrics") or {},
            "platform_metrics": result.get("platform_metrics") or {},
            "completeness": result.get("completeness"),
            "ingestion_status": result.get("ingestion_status"),
            "correction_of_id": result.get("correction_of_id"),
        }
        try:
            await self._record_metric_ingestion_run(
                session,
                actor,
                identity=identity,
                project_uuid=project_uuid,
                persona_ref=persona_ref,
                platform_account_ref=effective_account_ref,
                platform_account_id=observation_account_id,
                window_start=payload.get("window_start", payload.get("period_start")),
                window_end=payload.get("window_end", payload.get("period_end")),
                cursor=payload.get("cursor", cursor),
                checkpoint=payload.get("checkpoint", checkpoint),
                status=payload.get("status", status),
                idempotency_key=idempotency_key,
                request_payload={
                    "project_id": str(project_uuid) if project_uuid else None,
                    "persona_ref": result.get("persona_ref"),
                    "content_variant_ref": result.get("content_variant_ref"),
                    "publication_ref": result.get("publication_ref"),
                },
                observation_payload=observation_document,
                # Use the pre-normalized evidence list here.  The snapshot DTO
                # includes derived ``evidence_hash`` fields that are intentionally
                # not accepted as caller input by the ingestion ledger validator.
                evidence=evidence_items,
                platform_account_revision_id=payload.get(
                    "platform_account_revision_id",
                    platform_account_revision_id
                    if platform_account_revision_id is not None
                    else getattr(adapter, "platform_account_revision_id", None),
                ),
                platform_account_revision_hash=payload.get(
                    "platform_account_revision_hash",
                    platform_account_revision_hash
                    if platform_account_revision_hash is not None
                    else getattr(adapter, "platform_account_revision_hash", None),
                ),
                credential_state_hash=payload.get(
                    "credential_state_hash",
                    credential_state_hash
                    if credential_state_hash is not None
                    else getattr(adapter, "credential_state_hash", None),
                ),
                capability_snapshot_id=payload.get(
                    "capability_snapshot_id",
                    capability_snapshot_id
                    if capability_snapshot_id is not None
                    else getattr(adapter, "capability_snapshot_id", None),
                ),
                capability_snapshot_hash=payload.get(
                    "capability_snapshot_hash",
                    capability_snapshot_hash
                    if capability_snapshot_hash is not None
                    else getattr(adapter, "capability_snapshot_hash", None),
                ),
                external_action_receipt_ref=payload.get(
                    "external_action_receipt_ref",
                    external_action_receipt_ref
                    if external_action_receipt_ref is not None
                    else getattr(adapter, "external_action_receipt_ref", None),
                ),
                remote_ref=payload.get(
                    "remote_ref",
                    remote_ref if remote_ref is not None else getattr(adapter, "remote_ref", None),
                ),
            )
            # Snapshot and ingestion checkpoint are one logical operation.  The
            # snapshot writer above only flushed; commit both ledgers together so a
            # pin/receipt/ledger failure cannot leave an orphan API observation.
            await self._commit(session)
        except Exception:
            await self._rollback(session)
            raise
        return result

    # Explicit aliases make the internal boundary discoverable to provider
    # adapters without creating additional public HTTP or model-facing tools.
    ingest_metric_snapshot_from_provider = ingest_provider_metric_snapshot
    ingest_provider_metrics = ingest_provider_metric_snapshot

    async def list_metric_snapshots(self, session: Any | None = None, actor: Any | None = None, *, project_id: Any = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        rows = await self._list(session, actor, MetricSnapshot, project_id=project_id, limit=limit, offset=offset)
        return [row.to_safe_dict() for row in rows]

    async def list_metric_ingestion_runs(self, session: Any | None = None, actor: Any | None = None, *, project_id: Any = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List redacted, immutable provider ingestion checkpoints."""

        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        rows = await self._list(
            session,
            actor,
            MediaMetricIngestionRun,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
        return [row.to_safe_dict() for row in rows]

    async def get_metric_ingestion_run(self, session: Any | None = None, actor: Any | None = None, run_id: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or run_id is None:
            raise MediaOperationsValidationError("actor and run_id are required")
        row = await self._get_row(session, MediaMetricIngestionRun, run_id, "run_id")
        await self._assert_entity_access(session, actor, row, permission="read")
        return row.to_safe_dict()

    async def get_metric_snapshot(self, session: Any | None = None, actor: Any | None = None, snapshot_id: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or snapshot_id is None:
            raise MediaOperationsValidationError("actor and snapshot_id are required")
        row = await self._get_row(session, MetricSnapshot, snapshot_id, "snapshot_id")
        await self._assert_entity_access(session, actor, row, permission="read")
        return row.to_safe_dict()

    async def get_effective_metric_snapshot(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        snapshot_id: Any = None,
    ) -> dict[str, Any]:
        """Return the ACL-scoped accepted terminal correction head."""

        session = self._resolve_session(session)
        if actor is None or snapshot_id is None:
            raise MediaOperationsValidationError("actor and snapshot_id are required")
        original = await self._get_row(session, MetricSnapshot, snapshot_id, "snapshot_id")
        await self._assert_entity_access(session, actor, original, permission="read")
        effective = await get_effective_metric_snapshot(session, original)
        await self._assert_entity_access(session, actor, effective, permission="read")
        return effective.to_safe_dict()

    effective_metric_snapshot_head = get_effective_metric_snapshot

    async def _validate_experiment_variant_refs(
        self,
        session: Any,
        actor: Any,
        *,
        project_uuid: UUID | None,
        groups: Sequence[Mapping[str, Any]],
        persona_refs: Sequence[str],
        account_refs: Sequence[str],
    ) -> None:
        """Validate local UUID variant refs against the authorized graph.

        Provider/external variant identifiers remain opaque strings, but a
        UUID-shaped value is unambiguously a local identity and must resolve to
        the same project, Character revision and account graph.  This closes
        the cross-pair dashboard/experiment attribution hole without forcing
        external providers to adopt database UUIDs.
        """

        persona_set = {str(value) for value in persona_refs}
        account_set = {str(value) for value in account_refs}
        for group in groups:
            for raw_ref in group.get("variant_refs", ()):
                parsed = _uuid_ref(raw_ref)
                if parsed is None:
                    continue
                stable = await self._scalar(
                    session,
                    select(ContentVariant).where(ContentVariant.id == parsed).limit(1),
                )
                variant_revision = None
                if stable is None:
                    variant_revision = await self._scalar(
                        session,
                        select(ContentVariantRevision)
                        .where(ContentVariantRevision.id == parsed)
                        .limit(1),
                    )
                row = stable or variant_revision
                if row is None:
                    raise MediaOperationsNotFoundError(
                        "experiment variant_ref does not resolve to a local variant"
                    )
                await self._assert_entity_access(session, actor, row, permission="read")
                if getattr(row, "project_id", None) != project_uuid:
                    raise MediaOperationsValidationError(
                        "experiment variant_ref is outside the requested project"
                    )
                content_item_id = getattr(row, "content_item_id", None)
                content_item = None
                if content_item_id is not None:
                    content_item = await self._scalar(
                        session,
                        select(ContentItem).where(ContentItem.id == content_item_id).limit(1),
                    )
                    if content_item is None:
                        raise MediaOperationsValidationError(
                            "experiment variant graph is incomplete"
                        )
                    await self._assert_entity_access(
                        session, actor, content_item, permission="read"
                    )
                    if getattr(content_item, "project_id", None) != project_uuid:
                        raise MediaOperationsValidationError(
                            "experiment content item is outside the requested project"
                        )
                row_persona_revision = getattr(row, "persona_revision_id", None)
                if row_persona_revision is None and content_item is not None:
                    row_persona_revision = getattr(content_item, "persona_revision_id", None)
                if persona_set and row_persona_revision is not None:
                    if str(row_persona_revision) not in persona_set:
                        raise MediaOperationsValidationError(
                            "experiment variant_ref does not belong to persona_refs"
                        )
                row_account = getattr(row, "platform_account_id", None)
                if account_set and row_account is not None:
                    account = await self._get_row(
                        session, PlatformAccount, row_account, "experiment variant account"
                    )
                    await self._assert_entity_access(session, actor, account, permission="read")
                    if getattr(account, "project_id", None) != project_uuid:
                        raise MediaOperationsValidationError(
                            "experiment variant account is outside the requested project"
                        )
                    if str(row_account) not in account_set and str(getattr(account, "account_ref", "")) not in account_set:
                        raise MediaOperationsValidationError(
                            "experiment variant_ref does not belong to account_refs"
                        )

    async def create_experiment(self, session: Any | None = None, actor: Any | None = None, *, name: Any, hypothesis: Any, variant_groups: Any, primary_metric: Any, window_start: Any, window_end: Any, project_id: Any = None, persona_refs: Any = None, persona_ids: Any = None, account_refs: Any = None, account_ids: Any = None, secondary_metrics: Any = None, minimum_sample_size: Any = 1, status: Any = "draft", idempotency_key: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        _assert_human_write(actor)
        actor_id, project_uuid = await self._scope_for_create(session, actor, project_id)
        if persona_ids and not persona_refs:
            persona_refs = persona_ids
        if account_ids and not account_refs:
            account_refs = account_ids
        clean_name = _required_text(name, "name", 255)
        clean_hypothesis = _required_text(hypothesis, "hypothesis", 8000)
        groups = _variant_groups(variant_groups)
        primary = _required_text(primary_metric, "primary_metric", 64).lower()
        if primary not in ALLOWED_METRIC_KEYS:
            raise MediaOperationsValidationError("primary_metric is unsupported")
        # Secondary metrics are metric names (not opaque entity references),
        # so validate them against the same closed vocabulary as the primary
        # metric instead of accepting arbitrary JSON strings.
        secondary = []
        if secondary_metrics is not None:
            if isinstance(secondary_metrics, (str, bytes)) or not isinstance(secondary_metrics, Sequence):
                raise MediaOperationsValidationError("secondary_metrics must be a list")
            for raw in secondary_metrics:
                metric = _required_text(raw, "secondary_metric", 64).lower()
                if metric not in ALLOWED_METRIC_KEYS or metric == primary:
                    raise MediaOperationsValidationError("secondary_metric is unsupported")
                if metric not in secondary:
                    secondary.append(metric)
        start = _datetime(window_start, "window_start")
        end = _datetime(window_end, "window_end")
        assert start is not None and end is not None
        if start >= end:
            raise MediaOperationsValidationError("window_start must be before window_end")
        if isinstance(minimum_sample_size, bool) or not isinstance(minimum_sample_size, int) or not 1 <= minimum_sample_size <= 10_000_000:
            raise MediaOperationsValidationError("minimum_sample_size must be between 1 and 10000000")
        status_value = _enum(status, EXPERIMENT_STATUS_VALUES, "status", "draft")
        key = _required_text(idempotency_key, "idempotency_key", 255)
        normalized_personas = _normalize_refs(persona_refs, "persona_refs")
        normalized_accounts = _normalize_refs(account_refs, "account_refs")
        for reference in normalized_personas:
            await self._validate_reference_graph(
                session,
                actor,
                project_uuid=project_uuid,
                persona_ref=reference,
            )
        for reference in normalized_accounts:
            await self._validate_reference_graph(
                session,
                actor,
                project_uuid=project_uuid,
                platform_account_ref=reference,
            )
        await self._validate_experiment_variant_refs(
            session,
            actor,
            project_uuid=project_uuid,
            groups=groups,
            persona_refs=normalized_personas,
            account_refs=normalized_accounts,
        )
        payload = {
            "project_id": str(project_uuid) if project_uuid else None,
            "name": clean_name,
            "hypothesis": clean_hypothesis,
            "persona_refs": normalized_personas,
            "account_refs": normalized_accounts,
            "variant_groups": groups,
            "primary_metric": primary,
            "secondary_metrics": secondary,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "minimum_sample_size": minimum_sample_size,
            "status": status_value,
        }
        create_hash = sha256_json(payload)
        existing = await self._find_scoped_idempotency(session, Experiment, owner_user_id=actor_id, project_id=project_uuid, key=key)
        if existing is not None:
            if existing.create_hash != create_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different experiment")
            return existing.to_safe_dict()
        row_payload = dict(payload)
        row_payload.pop("project_id", None)
        row_payload.update({"window_start": start, "window_end": end})
        row = Experiment(id=uuid4(), owner_user_id=actor_id, project_id=project_uuid, **row_payload, create_hash=create_hash, idempotency_key=key, created_by=actor_id)
        session.add(row)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(session, Experiment, owner_user_id=actor_id, project_id=project_uuid, key=key)
            if recovered is not None and recovered.create_hash == create_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError("experiment conflicts with an existing record") from exc
        return row.to_safe_dict()

    async def list_experiments(self, session: Any | None = None, actor: Any | None = None, *, project_id: Any = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        return [row.to_safe_dict() for row in await self._list(session, actor, Experiment, project_id=project_id, limit=limit, offset=offset)]

    async def get_experiment(self, session: Any | None = None, actor: Any | None = None, experiment_id: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or experiment_id is None:
            raise MediaOperationsValidationError("actor and experiment_id are required")
        row = await self._get_row(session, Experiment, experiment_id, "experiment_id")
        await self._assert_entity_access(session, actor, row, permission="read")
        result_rows = await self._scalars(session, select(ExperimentResult).where(ExperimentResult.experiment_id == row.id).order_by(ExperimentResult.created_at.desc(), ExperimentResult.id.desc()).limit(100))
        return {
            **row.to_safe_dict(),
            "results": [await self._result_safe_dict(session, item) for item in result_rows],
        }

    async def record_experiment_result(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        experiment_id: Any = None,
        *,
        sample_size: Any = None,
        group_metrics: Any = None,
        metrics: Any = None,
        evidence_refs: Any = None,
        evidence: Any = None,
        sample_sizes: Any = None,
        winner_variant_ref: Any = None,
        confidence: Any = 0.0,
        uncertainty: Any = 1.0,
        conclusion: Any = "",
        inputs: Any = None,
        metric_inputs: Any = None,
        analysis_method: Any = None,
        analysis_version: Any = None,
        analysis_design: Any = None,
        assignment_evidence: Any = None,
        exposure_evidence: Any = None,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        """Persist one server-derived experiment result.

        New callers must supply ``inputs`` (MetricSnapshot ids + expected
        hashes).  The snapshots are ACL/character/account/window checked and
        aggregated server-side; caller metrics are ignored when inputs are
        present.  Legacy ``group_metrics`` remains available as an explicitly
        observational compatibility path for existing imports.
        """

        session = self._resolve_session(session)
        if actor is None or experiment_id is None:
            raise MediaOperationsValidationError("actor and experiment_id are required")
        _assert_human_write(actor)
        experiment = await self._get_row(session, Experiment, experiment_id, "experiment_id", for_update=True)
        actor_id = await self._assert_entity_access(session, actor, experiment, permission="write")
        # Keep scalar identity values alive across an IntegrityError rollback;
        # SQLAlchemy expires ORM attributes during rollback and reloading them
        # from an AsyncSession outside greenlet context raises MissingGreenlet.
        experiment_uuid = experiment.id
        experiment_project_id = experiment.project_id
        if inputs is None and metric_inputs is not None:
            inputs = metric_inputs

        if inputs is not None:
            # The design is intentionally explicit for the authority path.
            if analysis_design in (None, ""):
                raise MediaOperationsValidationError(
                    "analysis_design is required when metric snapshot inputs are supplied"
                )
            design = _enum(analysis_design, ("controlled", "observational"), "analysis_design")
            normalized_inputs, derived_sample_size, derived_sample_sizes, derived_group_metrics = await self._resolve_experiment_metric_inputs(
                session, actor, experiment, inputs
            )
            if sample_size not in (None, "") and sample_size != derived_sample_size:
                raise MediaOperationsValidationError("sample_size must be derived from metric inputs")
            sample_size_value = derived_sample_size
            normalized_sample_sizes = derived_sample_sizes
            normalized_groups = derived_group_metrics
        else:
            # Existing hand-authored/imported observations are explicitly
            # observational for backwards compatibility; controlled analyses
            # cannot bypass the input ledger.
            design = _enum(analysis_design, ("controlled", "observational"), "analysis_design", "observational")
            if design == "controlled":
                raise MediaOperationsValidationError(
                    "controlled analysis requires metric snapshot inputs"
                )
            if sample_size is None:
                raise MediaOperationsValidationError("sample_size is required")
            if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 0 or sample_size > 10_000_000:
                raise MediaOperationsValidationError("sample_size must be a non-negative integer")
            metrics_payload = _mapping(group_metrics if group_metrics is not None else metrics, "group_metrics")
            group_names = {str(group["name"]) for group in (experiment.variant_groups or [])}
            if set(metrics_payload) - group_names:
                raise MediaOperationsValidationError("group_metrics contains an unknown variant group")
            if len(metrics_payload) > _MAX_GROUPS:
                raise MediaOperationsValidationError("group_metrics has too many groups")
            normalized_groups = {}
            for raw_name, metric_values in metrics_payload.items():
                name = _required_text(raw_name, "group_metrics group", 64)
                normalized_groups[name] = _metric_map(metric_values, f"group_metrics.{name}")
            normalized_sample_sizes = _sample_sizes(sample_sizes, experiment.variant_groups or [], sample_size)
            sample_size_value = sample_size
            normalized_inputs = []

        # A controlled result must carry both assignment and exposure proof;
        # observational results deliberately carry no implied assignment.
        assignment = _normalize_evidence(
            assignment_evidence,
            "assignment_evidence",
            required=design == "controlled",
        )
        exposure = _normalize_evidence(
            exposure_evidence,
            "exposure_evidence",
            required=design == "controlled",
        )
        if inputs is not None and design == "observational" and not (assignment or exposure):
            raise MediaOperationsValidationError(
                "observational analysis requires assignment or exposure evidence"
            )
        refs = _normalize_evidence(
            evidence_refs if evidence_refs is not None else evidence,
            "evidence_refs",
            required=True,
        )
        try:
            confidence_value = float(confidence)
            uncertainty_value = float(uncertainty)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError("confidence and uncertainty must be numbers") from exc
        if not math.isfinite(confidence_value) or not 0 <= confidence_value <= 1:
            raise MediaOperationsValidationError("confidence must be between 0 and 1")
        if not math.isfinite(uncertainty_value) or not 0 <= uncertainty_value <= 1:
            raise MediaOperationsValidationError("uncertainty must be between 0 and 1")
        requested_winner = _optional_ref(winner_variant_ref, "winner_variant_ref")
        known_refs = {ref for group in (experiment.variant_groups or []) for ref in group.get("variant_refs", [])}
        if requested_winner is not None and requested_winner not in known_refs:
            raise MediaOperationsValidationError("winner_variant_ref is not in the experiment")
        result_status, winner, calculated_confidence, calculated_uncertainty = _calculated_experiment_result(
            experiment,
            sample_size=sample_size_value,
            sample_sizes=normalized_sample_sizes,
            group_metrics=normalized_groups,
        )
        if requested_winner is not None and result_status == ExperimentResultStatus.COMPLETE.value and requested_winner != winner:
            raise MediaOperationsValidationError("winner_variant_ref does not match the server calculation")
        confidence_value = calculated_confidence
        uncertainty_value = calculated_uncertainty
        if result_status != ExperimentResultStatus.COMPLETE.value:
            conclusion_value = (
                "inconclusive: minimum sample size was not met"
                if sample_size_value < int(experiment.minimum_sample_size or 1)
                else "inconclusive: normalized observations did not establish a unique winner"
            )
        else:
            conclusion_value = _required_text(
                conclusion or "server-calculated winner from normalized observations",
                "conclusion",
                8000,
            )
        method = _required_text(analysis_method or DEFAULT_EXPERIMENT_ANALYSIS_METHOD, "analysis_method", 64)
        version = _required_text(analysis_version or "1", "analysis_version", 32)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", method):
            raise MediaOperationsValidationError("analysis_method must be a bounded identifier")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}", version):
            raise MediaOperationsValidationError("analysis_version must be a bounded identifier")

        canonical_inputs = [
            {
                "metric_snapshot_id": item["metric_snapshot_id"],
                "metric_snapshot_hash": item["metric_snapshot_hash"],
                "group_name": item["group_name"],
                "variant_ref": item.get("variant_ref"),
            }
            for item in normalized_inputs
        ]
        key = _required_text(idempotency_key, "idempotency_key", 255)
        payload = {
            "experiment_id": str(experiment_uuid),
            "sample_size": sample_size_value,
            "sample_sizes": normalized_sample_sizes,
            "group_metrics": normalized_groups,
            "winner_variant_ref": winner,
            "confidence": confidence_value,
            "uncertainty": uncertainty_value,
            "evidence_refs": refs,
            "analysis_method": method,
            "analysis_version": version,
            "analysis_design": design,
            "assignment_evidence": assignment,
            "exposure_evidence": exposure,
            "inputs": canonical_inputs,
            "status": result_status,
            "conclusion": conclusion_value,
        }
        result_hash = sha256_json(payload)
        existing = await self._scalar(
            session,
            select(ExperimentResult)
            .where(ExperimentResult.experiment_id == experiment_uuid, ExperimentResult.idempotency_key == key)
            .limit(1),
        )
        if existing is not None:
            if existing.result_hash != result_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different experiment result")
            return await self._result_safe_dict(session, existing)

        row_kwargs: dict[str, Any] = {
            "id": uuid4(),
            "owner_user_id": actor_id,
            "project_id": experiment_project_id,
            "experiment_id": experiment_uuid,
            "status": result_status,
            "sample_size": sample_size_value,
            "sample_sizes": normalized_sample_sizes,
            "group_metrics": normalized_groups,
            "winner_variant_ref": winner,
            "confidence": confidence_value,
            "uncertainty": uncertainty_value,
            "evidence_refs": refs,
            "conclusion": conclusion_value,
            "result_hash": result_hash,
            "idempotency_key": key,
            "created_by": actor_id,
        }
        optional_columns = {
            "analysis_method": method,
            "analysis_version": version,
            "analysis_design": design,
            "assignment_evidence": assignment,
            "exposure_evidence": exposure,
            "input_metric_snapshot_ids": [item["metric_snapshot_id"] for item in canonical_inputs],
            "input_metric_snapshot_hashes": [item["metric_snapshot_hash"] for item in canonical_inputs],
        }
        for name, value in optional_columns.items():
            if hasattr(ExperimentResult, name):
                row_kwargs[name] = value
        row = ExperimentResult(**row_kwargs)
        session.add(row)
        if canonical_inputs:
            if ExperimentResultMetricInput is None:
                raise MediaOperationsValidationError("metric input ledger model is unavailable")
            for ordinal, item in enumerate(canonical_inputs, start=1):
                session.add(
                    ExperimentResultMetricInput(
                        id=uuid4(),
                        experiment_result_id=row.id,
                        metric_snapshot_id=_as_uuid(item["metric_snapshot_id"], "metric_snapshot_id"),
                        owner_user_id=actor_id,
                        project_id=experiment_project_id,
                        metric_snapshot_hash=item["metric_snapshot_hash"],
                        group_name=item["group_name"],
                        variant_ref=item.get("variant_ref"),
                        ordinal=ordinal,
                        created_by=actor_id,
                    )
                )
        experiment.status = (
            ExperimentStatus.COMPLETED.value
            if result_status == ExperimentResultStatus.COMPLETE.value
            else ExperimentStatus.INCONCLUSIVE.value
        )
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(
                session,
                select(ExperimentResult)
                .where(ExperimentResult.experiment_id == experiment_uuid, ExperimentResult.idempotency_key == key)
                .limit(1),
            )
            if recovered is not None and recovered.result_hash == result_hash:
                return await self._result_safe_dict(session, recovered)
            raise MediaOperationsConflictError("experiment result conflicts with an existing record") from exc
        return await self._result_safe_dict(session, row)

    async def list_experiment_results(self, session: Any | None = None, actor: Any | None = None, experiment_id: Any = None, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None or experiment_id is None:
            raise MediaOperationsValidationError("actor and experiment_id are required")
        experiment = await self._get_row(session, Experiment, experiment_id, "experiment_id")
        await self._assert_entity_access(session, actor, experiment, permission="read")
        page_limit, page_offset = _bounded_page(limit, offset)
        rows = await self._scalars(session, select(ExperimentResult).where(ExperimentResult.experiment_id == experiment.id).order_by(ExperimentResult.created_at.desc(), ExperimentResult.id.desc()).limit(page_limit).offset(page_offset))
        return [await self._result_safe_dict(session, row) for row in rows]

    async def analyze_experiment(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        experiment_id: Any = None,
        *,
        sample_size: Any = None,
        sample_sizes: Any = None,
        group_metrics: Any = None,
        metrics: Any = None,
        inputs: Any = None,
        metric_inputs: Any = None,
        analysis_method: Any = None,
        analysis_version: Any = None,
        analysis_design: Any = None,
        assignment_evidence: Any = None,
        exposure_evidence: Any = None,
    ) -> dict[str, Any]:
        """Return a deterministic, non-mutating experiment analysis.

        With no measurement arguments this reads the latest immutable result.
        New analyses can provide MetricSnapshot ``inputs`` and are calculated
        from accepted effective heads only; no client metric values are
        trusted.
        """

        session = self._resolve_session(session)
        if actor is None or experiment_id is None:
            raise MediaOperationsValidationError("actor and experiment_id are required")
        experiment = await self._get_row(session, Experiment, experiment_id, "experiment_id")
        await self._assert_entity_access(session, actor, experiment, permission="read")
        if inputs is None and metric_inputs is not None:
            inputs = metric_inputs
        has_measurements = any(
            value is not None
            for value in (sample_size, sample_sizes, group_metrics, metrics, inputs)
        )
        if not has_measurements:
            latest = await self._scalar(
                session,
                select(ExperimentResult)
                .where(ExperimentResult.experiment_id == experiment.id)
                .order_by(ExperimentResult.created_at.desc(), ExperimentResult.id.desc())
                .limit(1),
            )
            if latest is None:
                return {
                    "experiment_id": str(experiment.id),
                    "status": "inconclusive",
                    "winner_variant_ref": None,
                    "confidence": 0.0,
                    "uncertainty": 1.0,
                    "sample_size": 0,
                    "sample_sizes": {},
                    "group_metrics": {},
                    "calculation_basis": "no immutable result recorded",
                }
            result = await self._result_safe_dict(session, latest)
            result["calculation_basis"] = "latest immutable normalized result"
            return result

        if inputs is not None:
            if analysis_design in (None, ""):
                raise MediaOperationsValidationError(
                    "analysis_design is required when metric snapshot inputs are supplied"
                )
            design = _enum(analysis_design, ("controlled", "observational"), "analysis_design")
            normalized_inputs, derived_sample_size, normalized_sample_sizes, normalized_groups = await self._resolve_experiment_metric_inputs(
                session, actor, experiment, inputs
            )
            sample_size_value = derived_sample_size
            canonical_inputs = [
                {
                    "metric_snapshot_id": item["metric_snapshot_id"],
                    "metric_snapshot_hash": item["metric_snapshot_hash"],
                    "group_name": item["group_name"],
                    "variant_ref": item.get("variant_ref"),
                }
                for item in normalized_inputs
            ]
        else:
            design = _enum(analysis_design, ("controlled", "observational"), "analysis_design", "observational")
            if design == "controlled":
                raise MediaOperationsValidationError("controlled analysis requires metric snapshot inputs")
            if sample_size is None:
                raise MediaOperationsValidationError("sample_size is required")
            if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 0 or sample_size > 10_000_000:
                raise MediaOperationsValidationError("sample_size must be a non-negative integer")
            metrics_payload = _mapping(group_metrics if group_metrics is not None else metrics, "group_metrics")
            group_names = {str(group["name"]) for group in (experiment.variant_groups or [])}
            if set(metrics_payload) - group_names:
                raise MediaOperationsValidationError("group_metrics contains an unknown variant group")
            normalized_groups = {
                _required_text(name, "group_metrics group", 64): _metric_map(values, f"group_metrics.{name}")
                for name, values in metrics_payload.items()
            }
            normalized_sample_sizes = _sample_sizes(sample_sizes, experiment.variant_groups or [], sample_size)
            sample_size_value = sample_size
            canonical_inputs = []

        assignment = _normalize_evidence(assignment_evidence, "assignment_evidence", required=design == "controlled")
        exposure = _normalize_evidence(exposure_evidence, "exposure_evidence", required=design == "controlled")
        if inputs is not None and design == "observational" and not (assignment or exposure):
            raise MediaOperationsValidationError(
                "observational analysis requires assignment or exposure evidence"
            )
        status, winner, confidence, uncertainty = _calculated_experiment_result(
            experiment,
            sample_size=sample_size_value,
            sample_sizes=normalized_sample_sizes,
            group_metrics=normalized_groups,
        )
        analysis_conclusion = (
            "inconclusive: minimum sample size was not met"
            if sample_size_value < int(experiment.minimum_sample_size or 1)
            else "inconclusive: normalized observations did not establish a unique winner"
            if status != ExperimentResultStatus.COMPLETE.value
            else "server-calculated winner from normalized observations"
        )
        method = _required_text(analysis_method or DEFAULT_EXPERIMENT_ANALYSIS_METHOD, "analysis_method", 64)
        version = _required_text(analysis_version or "1", "analysis_version", 32)
        return {
            "experiment_id": str(experiment.id),
            "status": status,
            "winner_variant_ref": winner,
            "confidence": confidence,
            "uncertainty": uncertainty,
            "sample_size": sample_size_value,
            "sample_sizes": normalized_sample_sizes,
            "group_metrics": normalized_groups,
            "analysis_method": method,
            "analysis_version": version,
            "analysis_design": design,
            "assignment_evidence": assignment,
            "exposure_evidence": exposure,
            "conclusion": analysis_conclusion,
            "input_metric_snapshot_ids": [item["metric_snapshot_id"] for item in canonical_inputs],
            "input_metric_snapshot_hashes": [item["metric_snapshot_hash"] for item in canonical_inputs],
            "result_hash": sha256_json({
                "experiment_id": str(experiment.id),
                "sample_size": sample_size_value,
                "sample_sizes": normalized_sample_sizes,
                "group_metrics": normalized_groups,
                "winner_variant_ref": winner,
                "confidence": confidence,
                "uncertainty": uncertainty,
                "analysis_method": method,
                "analysis_version": version,
                "analysis_design": design,
                "assignment_evidence": assignment,
                "exposure_evidence": exposure,
                "inputs": canonical_inputs,
                "status": status,
                "conclusion": analysis_conclusion,
            }),
            "calculation_basis": {
                "primary_metric": experiment.primary_metric,
                "minimum_sample_size": int(experiment.minimum_sample_size or 1),
                "server_calculated": True,
                "effective_metric_snapshot_heads": True if canonical_inputs else False,
            },
        }

    async def create_revenue_event(self, session: Any | None = None, actor: Any | None = None, *, event_type: Any = "sale", gross_amount: Any, net_amount: Any, currency: Any, event_at: Any, project_id: Any = None, persona_ref: Any = None, platform_account_ref: Any = None, account_ref: Any = None, platform: Any = None, content_ref: Any = None, content_variant_ref: Any = None, publication_ref: Any = None, product_ref: Any = None, source: Any = "manual", provider: Any = None, settlement_at: Any = None, evidence: Any = None, provenance: Any = None, correction_of_id: Any = None, idempotency_key: Any = None, _defer_commit: bool = False) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        _assert_human_write(actor)
        actor_id, project_uuid = await self._scope_for_create(session, actor, project_id)
        platform_account_ref = platform_account_ref if platform_account_ref is not None else account_ref
        content_ref = content_ref if content_ref is not None else content_variant_ref
        event_type_value = _enum(event_type, REVENUE_EVENT_TYPE_VALUES, "event_type", "sale")
        gross = _finite_number(
            gross_amount,
            "gross_amount",
            allow_negative=event_type_value != "sale",
        )
        net = _finite_number(
            net_amount,
            "net_amount",
            allow_negative=event_type_value != "sale",
        )
        _validate_revenue_signs(event_type_value, gross, net)
        if event_type_value in {"refund", "chargeback", "reversal"} and correction_of_id in (None, ""):
            raise MediaOperationsValidationError(
                f"{event_type_value} requires correction_of_id"
            )
        currency_value = _required_text(currency, "currency", 3).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency_value):
            raise MediaOperationsValidationError("currency must be a three-letter ISO code")
        event_time = _datetime(event_at, "event_at")
        settled = _datetime(settlement_at, "settlement_at", required=False)
        assert event_time is not None
        if settled and settled < event_time:
            raise MediaOperationsValidationError("settlement_at must not be before event_at")
        source_value = _enum(source, METRIC_SNAPSHOT_SOURCE_VALUES, "source", "manual")
        if source_value == "api":
            raise MediaOperationsAuthorizationError(
                "source=api is reserved for server-owned settled revenue ingestion"
            )
        if provider in (None, ""):
            provider_value = source_value
        else:
            # Manual/imported provider labels are provenance metadata, not
            # authority.  They are still bounded and cannot be URLs/secrets.
            provider_value = _provider(provider, source=source_value)
        evidence_value = _normalize_evidence(provenance if provenance is not None else evidence, "evidence", required=True)
        platform_value = _optional_ref(platform, "platform")
        if platform_value is not None and platform_value not in MEDIA_PLATFORM_VALUES:
            raise MediaOperationsValidationError("platform is unsupported")
        await self._validate_reference_graph(
            session,
            actor,
            project_uuid=project_uuid,
            persona_ref=persona_ref,
            platform_account_ref=platform_account_ref,
            content_variant_ref=content_ref,
        )
        persona_ref = _optional_ref(persona_ref, "persona_ref")
        platform_account_ref = _optional_ref(platform_account_ref, "platform_account_ref")
        content_ref = _optional_ref(content_ref, "content_ref")
        publication_ref = _optional_ref(publication_ref, "publication_ref")
        product_ref = _optional_ref(product_ref, "product_ref")
        correction = None
        if correction_of_id not in (None, ""):
            correction = await self._get_row(session, RevenueEvent, correction_of_id, "correction_of_id")
            await self._assert_entity_access(session, actor, correction, permission="write")
            if correction.project_id != project_uuid or correction.owner_user_id != actor_id:
                raise MediaOperationsValidationError(
                    "correction must remain in the same owner/project scope"
                )
            if provider in (None, ""):
                raise MediaOperationsValidationError("correction requires an explicit provider")
            # A correction is a delta against one exact ledger context.  Do not
            # allow a caller to reuse an id while silently changing Character,
            # account, platform, content or product dimensions.
            context_pairs = (
                ("persona_ref", persona_ref, correction.persona_ref),
                ("platform_account_ref", platform_account_ref, correction.platform_account_ref),
                ("platform", platform_value, correction.platform),
                ("content_ref", content_ref, correction.content_ref),
                ("publication_ref", publication_ref, correction.publication_ref),
                ("product_ref", product_ref, correction.product_ref),
                ("currency", currency_value, correction.currency),
                ("provider", provider_value, correction.provider),
            )
            for label, incoming, target in context_pairs:
                if incoming != target:
                    raise MediaOperationsValidationError(
                        f"correction {label} must match its target event"
                    )
            if not any(value not in (None, "") for _, value, _ in context_pairs[:6]):
                raise MediaOperationsValidationError(
                    "correction requires a target Character/account/content context"
                )
        key = _required_text(idempotency_key, "idempotency_key", 255)
        payload = {
            "project_id": str(project_uuid) if project_uuid else None,
            "persona_ref": _optional_ref(persona_ref, "persona_ref"),
            "platform_account_ref": _optional_ref(platform_account_ref, "platform_account_ref"),
            "platform": platform_value,
            "content_ref": _optional_ref(content_ref, "content_ref"),
            "publication_ref": _optional_ref(publication_ref, "publication_ref"),
            "product_ref": _optional_ref(product_ref, "product_ref"),
            "source": source_value,
            "provider": provider_value,
            "event_type": event_type_value,
            "gross_amount": gross,
            "net_amount": net,
            "currency": currency_value,
            "event_at": event_time.isoformat(),
            "settlement_at": settled.isoformat() if settled else None,
            "evidence": evidence_value,
            "correction_of_id": str(correction.id) if correction else None,
        }
        event_hash = sha256_json(payload)
        existing = await self._find_scoped_idempotency(session, RevenueEvent, owner_user_id=actor_id, project_id=project_uuid, key=key)
        if existing is not None:
            if existing.event_hash != event_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different revenue event")
            return existing.to_safe_dict()
        row_payload = dict(payload)
        row_payload.pop("project_id", None)
        row_payload.update(
            {
                "event_at": event_time,
                "settlement_at": settled,
                "correction_of_id": correction.id if correction else None,
            }
        )
        row = RevenueEvent(id=uuid4(), owner_user_id=actor_id, project_id=project_uuid, **row_payload, event_hash=event_hash, idempotency_key=key, created_by=actor_id)
        session.add(row)
        try:
            if _defer_commit:
                await self._flush_only(session)
            else:
                await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(session, RevenueEvent, owner_user_id=actor_id, project_id=project_uuid, key=key)
            if recovered is not None and recovered.event_hash == event_hash:
                return _safe_revenue_dict(recovered)
            raise MediaOperationsConflictError("revenue event conflicts with an existing record") from exc
        return _safe_revenue_dict(row)

    async def ingest_provider_revenue_event(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        adapter: Any,
        event: Mapping[str, Any],
        project_id: Any = None,
        persona_ref: Any = None,
        platform_account_ref: Any = None,
        content_ref: Any = None,
        publication_ref: Any = None,
        product_ref: Any = None,
        platform_account_id: Any = None,
        platform_account_revision_id: Any = None,
        platform_account_revision_hash: Any = None,
        credential_state_hash: Any = None,
        capability_snapshot_id: Any = None,
        capability_snapshot_hash: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        """Ingest settled provider revenue through a server-owned adapter.

        Analytics/estimated revenue must use MetricSnapshot or an imported
        event.  This boundary accepts only a settled event with a settlement
        timestamp and evidence; there is no public route for ``source=api``.
        """

        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        _assert_ingestion_actor(actor)
        identity = _assert_server_owned_adapter(adapter, "revenue")
        payload = _mapping(event, "revenue event")
        allowed = {
            "event_type", "gross_amount", "net_amount", "currency", "event_at",
            "settlement_at", "evidence", "provenance", "correction_of_id", "platform",
            "receipt_ref", "external_action_receipt_ref", "platform_account_id",
            "platform_account_revision_id", "platform_account_revision_hash",
            "credential_state_hash", "capability_snapshot_id", "capability_snapshot_hash",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise MediaOperationsValidationError(
                "provider revenue event contains unsupported fields"
            )
        settled_at = payload.get("settlement_at")
        if settled_at in (None, ""):
            raise MediaOperationsValidationError(
                "provider revenue event requires settled authority"
            )
        direct_receipt = payload.get("receipt_ref")
        external_receipt = payload.get("external_action_receipt_ref")
        if direct_receipt not in (None, "") and external_receipt not in (None, "") and direct_receipt != external_receipt:
            raise MediaOperationsValidationError("receipt_ref aliases must match")
        if direct_receipt in (None, "") and external_receipt in (None, ""):
            raise MediaOperationsValidationError("provider revenue event requires a receipt reference")
        receipt_ref = _ref(
            direct_receipt if direct_receipt not in (None, "") else external_receipt,
            "receipt_ref",
        )
        external_receipt_ref = (
            _ref(external_receipt, "external_action_receipt_ref")
            if external_receipt not in (None, "")
            else None
        )
        evidence = payload.get("evidence", payload.get("provenance"))
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            raise MediaOperationsValidationError("provider revenue event requires evidence")
        evidence_items = list(evidence)
        evidence_items.append({
            "kind": "record",
            "ref": identity.adapter_ref,
            "role": "provider_settlement_adapter",
        })
        evidence_items.append({
            "kind": "record",
            "ref": receipt_ref,
            "role": "provider_settlement_receipt",
        })
        return await self._create_provider_revenue_event(
            session,
            actor,
            adapter_identity=identity,
            project_id=project_id,
            persona_ref=persona_ref,
            platform_account_ref=platform_account_ref,
            content_ref=content_ref,
            publication_ref=publication_ref,
            product_ref=product_ref,
            platform_account_id=payload.get("platform_account_id", platform_account_id),
            platform_account_revision_id=payload.get("platform_account_revision_id", platform_account_revision_id),
            platform_account_revision_hash=payload.get("platform_account_revision_hash", platform_account_revision_hash),
            credential_state_hash=payload.get("credential_state_hash", credential_state_hash),
            capability_snapshot_id=payload.get("capability_snapshot_id", capability_snapshot_id),
            capability_snapshot_hash=payload.get("capability_snapshot_hash", capability_snapshot_hash),
            external_action_receipt_ref=external_receipt_ref,
            provider_receipt_ref=receipt_ref,
            platform=payload.get("platform"),
            event_type=payload.get("event_type", "sale"),
            gross_amount=payload.get("gross_amount"),
            net_amount=payload.get("net_amount"),
            currency=payload.get("currency"),
            event_at=payload.get("event_at"),
            settlement_at=settled_at,
            evidence=evidence_items,
            correction_of_id=payload.get("correction_of_id"),
            idempotency_key=idempotency_key,
        )

    async def _create_provider_revenue_event(
        self,
        session: Any,
        actor: Any,
        *,
        adapter_identity: ProviderMetricsAdapterIdentity,
        project_id: Any,
        persona_ref: Any,
        platform_account_ref: Any,
        content_ref: Any,
        publication_ref: Any,
        product_ref: Any,
        platform_account_id: Any,
        platform_account_revision_id: Any,
        platform_account_revision_hash: Any,
        credential_state_hash: Any,
        capability_snapshot_id: Any,
        capability_snapshot_hash: Any,
        external_action_receipt_ref: Any,
        provider_receipt_ref: Any,
        platform: Any,
        event_type: Any,
        gross_amount: Any,
        net_amount: Any,
        currency: Any,
        event_at: Any,
        settlement_at: Any,
        evidence: Any,
        correction_of_id: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        """Internal settled-event writer; only called after adapter validation."""

        # Keep the public method's source/provider restrictions intact while
        # using the same normalization, hashing, ACL and idempotency code.
        actor_id, project_uuid = await self._scope_for_create(session, actor, project_id)
        persona_ref = _optional_ref(persona_ref, "persona_ref")
        platform_account_ref = _optional_ref(platform_account_ref, "platform_account_ref")
        content_ref = _optional_ref(content_ref, "content_ref")
        publication_ref = _optional_ref(publication_ref, "publication_ref")
        product_ref = _optional_ref(product_ref, "product_ref")
        platform_value = _optional_ref(platform, "platform")
        if platform_value is not None and platform_value not in MEDIA_PLATFORM_VALUES:
            raise MediaOperationsValidationError("platform is unsupported")
        (
            _validated_account_id,
            _validated_revision_id,
            _validated_revision_hash,
            _validated_capability_id,
            _validated_capability_hash,
            _validated_credential_hash,
            _validated_receipt,
        ) = await self._validate_provider_ingestion_pins(
            session,
            actor,
            identity=adapter_identity,
            operation="revenue",
            project_uuid=project_uuid,
            platform_account_id=platform_account_id or _uuid_ref(platform_account_ref),
            platform_account_ref=platform_account_ref,
            platform_account_revision_id=platform_account_revision_id,
            platform_account_revision_hash=platform_account_revision_hash,
            credential_state_hash=credential_state_hash,
            capability_snapshot_id=capability_snapshot_id,
            capability_snapshot_hash=capability_snapshot_hash,
            external_action_receipt_ref=external_action_receipt_ref,
            settlement_receipt_ref=provider_receipt_ref,
        )
        await self._validate_reference_graph(
            session,
            actor,
            project_uuid=project_uuid,
            persona_ref=persona_ref,
            platform_account_ref=platform_account_ref,
            content_variant_ref=content_ref,
        )
        event_type_value = _enum(event_type, REVENUE_EVENT_TYPE_VALUES, "event_type", "sale")
        gross = _finite_number(
            gross_amount,
            "gross_amount",
            allow_negative=event_type_value != "sale",
        )
        net = _finite_number(
            net_amount,
            "net_amount",
            allow_negative=event_type_value != "sale",
        )
        _validate_revenue_signs(event_type_value, gross, net)
        if event_type_value in {"refund", "chargeback", "reversal"} and correction_of_id in (None, ""):
            raise MediaOperationsValidationError(
                f"{event_type_value} requires correction_of_id"
            )
        currency_value = _required_text(currency, "currency", 3).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency_value):
            raise MediaOperationsValidationError("currency must be a three-letter ISO code")
        event_time = _datetime(event_at, "event_at")
        settled = _datetime(settlement_at, "settlement_at")
        assert event_time is not None and settled is not None
        if settled < event_time:
            raise MediaOperationsValidationError("settlement_at must not be before event_at")
        evidence_value = _normalize_evidence(evidence, "evidence", required=True)
        correction = None
        if correction_of_id not in (None, ""):
            correction = await self._get_row(session, RevenueEvent, correction_of_id, "correction_of_id")
            await self._assert_entity_access(session, actor, correction, permission="write")
            if correction.project_id != project_uuid or correction.owner_user_id != actor_id:
                raise MediaOperationsValidationError(
                    "correction must remain in the same owner/project scope"
                )
        if correction is not None:
            context_pairs = (
                ("persona_ref", persona_ref, correction.persona_ref),
                ("platform_account_ref", platform_account_ref, correction.platform_account_ref),
                ("platform", platform_value, correction.platform),
                ("content_ref", content_ref, correction.content_ref),
                ("publication_ref", publication_ref, correction.publication_ref),
                ("product_ref", product_ref, correction.product_ref),
                ("currency", currency_value, correction.currency),
                ("provider", adapter_identity.provider, correction.provider),
            )
            for label, incoming, target in context_pairs:
                if incoming != target:
                    raise MediaOperationsValidationError(
                        f"correction {label} must match its target event"
                    )
            if not any(value not in (None, "") for _, value, _ in context_pairs[:6]):
                raise MediaOperationsValidationError(
                    "correction requires a target Character/account/content context"
                )
        key = _required_text(idempotency_key, "idempotency_key", 255)
        payload = {
            "project_id": str(project_uuid) if project_uuid else None,
            "persona_ref": persona_ref,
            "platform_account_ref": platform_account_ref,
            "platform": platform_value,
            "content_ref": content_ref,
            "publication_ref": publication_ref,
            "product_ref": product_ref,
            "source": "api",
            "provider": adapter_identity.provider,
            "event_type": event_type_value,
            "gross_amount": gross,
            "net_amount": net,
            "currency": currency_value,
            "event_at": event_time.isoformat(),
            "settlement_at": settled.isoformat(),
            "evidence": evidence_value,
            "correction_of_id": str(correction.id) if correction else None,
        }
        event_hash = sha256_json(payload)
        existing = await self._find_scoped_idempotency(
            session,
            RevenueEvent,
            owner_user_id=actor_id,
            project_id=project_uuid,
            key=key,
        )
        if existing is not None:
            if existing.event_hash != event_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different revenue event")
            return _safe_revenue_dict(existing)
        row = RevenueEvent(
            id=uuid4(),
            owner_user_id=actor_id,
            project_id=project_uuid,
            persona_ref=persona_ref,
            platform_account_ref=platform_account_ref,
            platform=platform_value,
            content_ref=content_ref,
            publication_ref=publication_ref,
            product_ref=product_ref,
            source="api",
            provider=adapter_identity.provider,
            event_type=event_type_value,
            gross_amount=gross,
            net_amount=net,
            currency=currency_value,
            event_at=event_time,
            settlement_at=settled,
            evidence=evidence_value,
            correction_of_id=correction.id if correction else None,
            event_hash=event_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        session.add(row)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(
                session,
                RevenueEvent,
                owner_user_id=actor_id,
                project_id=project_uuid,
                key=key,
            )
            if recovered is not None and recovered.event_hash == event_hash:
                return _safe_revenue_dict(recovered)
            raise MediaOperationsConflictError("revenue event conflicts with an existing record") from exc
        return _safe_revenue_dict(row)

    ingest_revenue_event_from_provider = ingest_provider_revenue_event
    ingest_provider_revenue = ingest_provider_revenue_event

    async def import_patreon_csv_revenue(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        rows: Any = None,
        csv_text: Any = None,
        csv_bytes: Any = None,
        project_id: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        """Import a bounded Patreon batch as one atomic evidence transaction.

        ``csv_bytes`` (or the exact UTF-8 bytes of ``csv_text``) is hashed
        before parsing.  The digest is copied only as evidence metadata; raw
        CSV, file paths and payloads never enter the revenue ledger.  Reusing a
        key with the same digest is a read-only replay, while a different digest
        conflicts.  ``rows`` remains a JSON compatibility path.
        """

        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        _assert_human_write(actor)
        key = _required_text(idempotency_key, "idempotency_key", 255)
        provided = sum(value is not None for value in (rows, csv_text, csv_bytes))
        if provided != 1:
            raise MediaOperationsValidationError("provide exactly one of rows, csv_text or csv_bytes")

        file_sha256: str | None = None
        if csv_bytes is not None or csv_text is not None:
            if csv_bytes is not None:
                if not isinstance(csv_bytes, (bytes, bytearray, memoryview)):
                    raise MediaOperationsValidationError("csv_bytes must be bytes")
                raw_bytes = bytes(csv_bytes)
            else:
                if not isinstance(csv_text, str):
                    raise MediaOperationsValidationError("csv_text must be text")
                raw_bytes = csv_text.encode("utf-8")
            if len(raw_bytes) > 2 * 1024 * 1024:
                raise MediaOperationsValidationError("CSV input is too large")
            file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            try:
                decoded = raw_bytes.decode("utf-8")
                reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
                records = list(reader)
            except (UnicodeDecodeError, csv.Error, TypeError, ValueError) as exc:
                raise MediaOperationsValidationError("CSV input is invalid") from exc
            if not records or not any(cell.strip() for cell in records[0]):
                raise MediaOperationsValidationError("CSV input must contain a header")
            headers = [str(item).strip() for item in records[0]]
            if any(not item for item in headers) or len(set(headers)) != len(headers):
                raise MediaOperationsValidationError("CSV header is invalid")
            parsed_rows: list[dict[str, Any]] = []
            for record in records[1:]:
                if not record or all(not str(item).strip() for item in record):
                    continue
                if len(record) != len(headers):
                    raise MediaOperationsValidationError("CSV row has an invalid column count")
                parsed_rows.append(dict(zip(headers, record)))
        else:
            if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
                raise MediaOperationsValidationError("rows must be a list")
            parsed_rows = [dict(_mapping(item, "revenue row")) for item in rows]

        if not parsed_rows or len(parsed_rows) > 1000:
            raise MediaOperationsValidationError("rows must contain between 1 and 1000 items")
        allowed = {
            "event_type", "gross_amount", "net_amount", "currency", "event_at",
            "settlement_at", "persona_ref", "platform_account_ref", "account_ref",
            "content_ref", "content_variant_ref", "publication_ref", "product_ref",
            "platform", "evidence_ref", "correction_of_id", "provider",
        }
        normalized_rows: list[dict[str, Any]] = []
        for row_index, source_row in enumerate(parsed_rows):
            unknown = set(source_row).difference(allowed)
            if unknown or any(key_name is None for key_name in source_row):
                raise MediaOperationsValidationError("CSV row contains unsupported fields")
            clean = {
                str(key_name): value
                for key_name, value in source_row.items()
                if value not in (None, "")
            }
            for amount_key in ("gross_amount", "net_amount"):
                if amount_key in clean and isinstance(clean[amount_key], str):
                    try:
                        clean[amount_key] = float(clean[amount_key].replace(",", "").strip())
                    except (TypeError, ValueError) as exc:
                        raise MediaOperationsValidationError("CSV amount is invalid") from exc
            normalized_rows.append(clean)
        actor_id, project_uuid = await self._scope_for_create(session, actor, project_id)
        # Bind replay identity to the exact uploaded bytes (when available),
        # parser contract, scope and normalized rows.  This prevents the same
        # idempotency key from silently changing meaning across format/parser
        # revisions or projects.
        batch_document: dict[str, Any] = {
            "source_file_sha256": file_sha256,
            "format_version": "patreon-csv-v1",
            "project_id": str(project_uuid) if project_uuid else None,
            "options": {},
        }
        # JSON rows/csv_text remains a compatibility import path.  It has no
        # uploaded-file identity, so include its normalized rows in the
        # idempotency document; exact file uploads deliberately hash only the
        # source bytes, parser version, scope and import options.
        if file_sha256 is None:
            batch_document["normalized_rows"] = normalized_rows
        batch_hash = sha256_json(batch_document)
        comparison_hash = file_sha256 or batch_hash

        existing_scope_rows = await self._scalars(
            session,
            select(RevenueEvent).where(
                RevenueEvent.owner_user_id == actor_id,
                (
                    RevenueEvent.project_id == project_uuid
                    if project_uuid is not None
                    else RevenueEvent.project_id.is_(None)
                ),
            ),
        )
        existing_batch_rows = [
            item
            for item in existing_scope_rows
            if item.idempotency_key.startswith(f"{key}:")
        ]
        if existing_batch_rows:
            stored_hashes: set[str] = set()
            for event in existing_batch_rows:
                for evidence_item in event.evidence or []:
                    if not isinstance(evidence_item, Mapping):
                        continue
                    if evidence_item.get("role") in {
                        "patreon_csv_source",
                        "patreon_csv_file",  # legacy compatibility rows
                    }:
                        digest = evidence_item.get("sha256")
                        if digest:
                            stored_hashes.add(str(digest))
                # Legacy imports encoded the canonical row hash in the ref.
                if not stored_hashes:
                    for evidence_item in event.evidence or []:
                        ref = evidence_item.get("ref") if isinstance(evidence_item, Mapping) else None
                        if isinstance(ref, str) and ref.startswith("patreon-csv:"):
                            parts = ref.split(":")
                            if len(parts) >= 2:
                                stored_hashes.add(parts[1])
            if len(existing_batch_rows) != len(normalized_rows) or stored_hashes != {comparison_hash}:
                raise MediaOperationsConflictError(
                    "idempotency key was already used with a different Patreon batch"
                )
            ordered_existing = sorted(
                existing_batch_rows,
                key=lambda event: event.idempotency_key.rsplit(":", 1)[-1],
            )
            return {
                "status": "imported",
                "source": "imported",
                "provider": "imported",
                "count": len(ordered_existing),
                "event_ids": [str(event.id) for event in ordered_existing],
                "batch_hash": batch_hash,
                "file_sha256": file_sha256,
                "replayed": True,
            }

        imported_ids: list[str] = []
        try:
            for index, source_row in enumerate(normalized_rows):
                row = dict(source_row)
                evidence_ref = row.pop("evidence_ref", None) or f"patreon-csv-row:{comparison_hash}:{index}"
                if file_sha256 is not None:
                    evidence = [
                        {
                            "kind": "file",
                            "sha256": file_sha256,
                            "role": "patreon_csv_source",
                            "format_version": "patreon-csv-v1",
                            "row_index": index,
                        },
                        {
                            "kind": "record",
                            "ref": _ref(evidence_ref, "evidence_ref"),
                            "role": "patreon_csv_import",
                        },
                    ]
                else:
                    # Compatibility/manual imports must not be represented as
                    # exact uploaded-file provenance.  Keep only a bounded
                    # opaque row reference and the deterministic batch hash.
                    evidence = [
                        {
                            "kind": "record",
                            "ref": _ref(evidence_ref, "evidence_ref"),
                            "role": "patreon_csv_import",
                        },
                        {
                            "kind": "record",
                            "ref": _ref(
                                f"patreon-csv:{batch_hash}:{index}",
                                "batch_ref",
                            ),
                            "role": "patreon_import_batch",
                        },
                    ]
                result = await self.create_revenue_event(
                    session,
                    actor,
                    project_id=project_id,
                    persona_ref=row.pop("persona_ref", None),
                    platform_account_ref=row.pop("platform_account_ref", row.pop("account_ref", None)),
                    content_ref=row.pop("content_ref", row.pop("content_variant_ref", None)),
                    publication_ref=row.pop("publication_ref", None),
                    product_ref=row.pop("product_ref", None),
                    platform=row.pop("platform", None),
                    source="imported",
                    provider=row.pop("provider", None),
                    event_type=row.pop("event_type", "sale"),
                    gross_amount=row.pop("gross_amount", None),
                    net_amount=row.pop("net_amount", None),
                    currency=row.pop("currency", None),
                    event_at=row.pop("event_at", None),
                    settlement_at=row.pop("settlement_at", None),
                    evidence=evidence,
                    correction_of_id=row.pop("correction_of_id", None),
                    idempotency_key=f"{key}:{index}",
                    _defer_commit=True,
                )
                if row:
                    raise MediaOperationsValidationError("CSV row is missing supported typed fields")
                imported_ids.append(str(result.get("id")))
            await self._flush_only(session)
            await self._commit(session)
        except Exception:
            # Validation errors, malformed rows and FK conflicts all roll back
            # the pending batch, guaranteeing no partial import is visible.
            await self._rollback(session)
            raise
        return {
            "status": "imported",
            "source": "imported",
            "provider": "imported",
            "count": len(imported_ids),
            "event_ids": imported_ids,
            "batch_hash": batch_hash,
            "file_sha256": file_sha256,
            "replayed": False,
        }

    import_patron_csv_revenue = import_patreon_csv_revenue

    async def list_revenue_events(self, session: Any | None = None, actor: Any | None = None, *, project_id: Any = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        return [_safe_revenue_dict(row) for row in await self._list(session, actor, RevenueEvent, project_id=project_id, limit=limit, offset=offset)]

    async def get_revenue_event(self, session: Any | None = None, actor: Any | None = None, event_id: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or event_id is None:
            raise MediaOperationsValidationError("actor and event_id are required")
        row = await self._get_row(session, RevenueEvent, event_id, "event_id")
        await self._assert_entity_access(session, actor, row, permission="read")
        return _safe_revenue_dict(row)


# Compatibility names used by early WS7 planning notes.
MediaOperationsInsightsService = MediaOperationsMetricsService


__all__ = [
    "ALLOWED_METRIC_KEYS",
    "ProviderMetricsAdapterIdentity",
    "get_effective_metric_snapshot",
    "effective_metric_snapshot_head",
    "resolve_effective_metric_snapshot",
    "MediaOperationsMetricsService",
    "MediaOperationsInsightsService",
    "MediaOperationsAuthorizationError",
    "MediaOperationsConflictError",
    "MediaOperationsNotFoundError",
    "MediaOperationsValidationError",
]
