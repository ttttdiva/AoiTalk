"""Typed, server-owned proof for recovered MediaOps generation receipts.

The provider response is deliberately absent from this module.  The
generation service hands this module a small normalized evidence object after
its authenticated GET and local persistence checks.  ``AgentWorkService``
then binds that evidence to fresh ORM rows before returning a proof to the
parent-owned lifecycle runtime.

This is an internal authority seam, not a public receipt format.  In
particular, a mapping that happens to contain ``status=succeeded`` is never a
proof and cannot cross the factory boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


_EVIDENCE_TOKEN = object()
_PROOF_TOKEN = object()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{3,163}$")
_RUN_REF_RE = re.compile(r"^run_[A-Za-z0-9_-]{4,160}$")
_WORKSPACE_REF_RE = re.compile(r"^wsp_[A-Za-z0-9_-]{4,160}$")
_PROJECT_REF_RE = re.compile(r"^prj_[A-Za-z0-9_-]{4,160}$")
_RECEIPT_ERROR_CODES = frozenset(
    {
        "generation_receipt_pending",
        "generation_receipt_persistence_failed",
        "generation_recovery_lookup_unknown",
        "generation_recovery_receipt_pending",
        "generation_recovery_run_id_missing",
        "generation_submit_outcome_unknown",
    }
)
_RECEIPT_DOMAIN_STATUSES = frozenset({"receipt_pending", "pending"})


class GenerationReceiptProofError(ValueError):
    """Base error for an invalid or untrusted generation proof."""


class GenerationReceiptEvidenceError(GenerationReceiptProofError):
    """The MediaOps evidence object is malformed or not server-issued."""


class GenerationReceiptBindingError(GenerationReceiptProofError):
    """The evidence cannot be bound to the immutable local ledger."""


def _text(value: Any, field_name: str, *, limit: int = 255) -> str:
    if not isinstance(value, str):
        raise GenerationReceiptEvidenceError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise GenerationReceiptEvidenceError(f"{field_name} is invalid")
    return normalized


def _uuid(value: Any, field_name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise GenerationReceiptBindingError(f"{field_name} is not a UUID") from exc


def _hash(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name, limit=64).lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise GenerationReceiptEvidenceError(f"{field_name} must be a SHA-256 hash")
    return normalized


def _opaque(value: Any, field_name: str, pattern: re.Pattern[str] = _OPAQUE_REF_RE) -> str:
    normalized = _text(value, field_name, limit=164)
    if pattern.fullmatch(normalized) is None:
        raise GenerationReceiptEvidenceError(f"{field_name} is not an opaque reference")
    return normalized


def _row_field(row: Any, field_name: str, *, required: bool = True) -> Any:
    """Read an ORM-like row, rejecting caller-shaped mappings."""

    if row is None or isinstance(row, Mapping):
        if required:
            raise GenerationReceiptBindingError(f"{field_name} row is not authoritative")
        return None
    value = getattr(row, field_name, None)
    if required and value is None:
        raise GenerationReceiptBindingError(f"{field_name} is missing")
    return value


def _safe_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _canonical(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(item) for item in value]
    return value


def _proof_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonical(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _marker_values(value: Any, *, depth: int = 0) -> set[str]:
    """Collect bounded status/error markers from old Work/Run projections."""

    if depth > 3:
        return set()
    if isinstance(value, str):
        return {value.strip().casefold()[:128]}
    if isinstance(value, Mapping):
        result: set[str] = set()
        for key, item in list(value.items())[:64]:
            name = str(key).strip().casefold()
            if name in {
                "status",
                "state",
                "classification",
                "domain_status",
                "reason_code",
                "error_code",
                "safe_error_code",
            }:
                result.update(_marker_values(item, depth=depth + 1))
            elif name in {"receipt_recovery", "metadata", "result", "run_metadata"}:
                result.update(_marker_values(item, depth=depth + 1))
        return result
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for item in list(value)[:32]:
            result.update(_marker_values(item, depth=depth + 1))
        return result
    return set()


@dataclass(frozen=True, slots=True)
class GenerationReceiptOutputEvidence:
    """One normalized immutable output reference returned by the provider GET."""

    external_asset_id: str
    external_output_version: str
    sha256: str

    def __post_init__(self) -> None:
        asset = _opaque(self.external_asset_id, "output.external_asset_id")
        version = _opaque(self.external_output_version, "output.external_output_version")
        digest = _hash(self.sha256, "output.sha256")
        if not asset.startswith("ast_"):
            raise GenerationReceiptEvidenceError("output.external_asset_id is invalid")
        if not version.startswith("outv_"):
            raise GenerationReceiptEvidenceError("output.external_output_version is invalid")
        object.__setattr__(self, "external_asset_id", asset)
        object.__setattr__(self, "external_output_version", version)
        object.__setattr__(self, "sha256", digest)


@dataclass(frozen=True, slots=True, init=False)
class GenerationReceiptEvidence:
    """Sealed normalized evidence issued by the generation service."""

    remote_status: str
    external_run_id: str
    workspace_id: str
    project_id: str
    external_idempotency_key: str
    correlation_id: str
    plan_hash: str
    request_hash: str
    response_digest: str
    selector_id: str | None
    preset_id: str | None
    preset_revision_id: str | None
    outputs: tuple[GenerationReceiptOutputEvidence, ...]
    _token: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        remote_status: str,
        external_run_id: str,
        workspace_id: str,
        project_id: str,
        external_idempotency_key: str,
        correlation_id: str,
        plan_hash: str,
        request_hash: str,
        response_digest: str,
        selector_id: str | None = None,
        preset_id: str | None = None,
        preset_revision_id: str | None = None,
        outputs: Sequence[GenerationReceiptOutputEvidence],
        _token: object | None = None,
    ) -> None:
        if _token is not _EVIDENCE_TOKEN:
            raise GenerationReceiptEvidenceError(
                "generation receipt evidence is server-issued"
            )
        status = _text(remote_status, "remote_status", limit=32).casefold()
        if status != "succeeded":
            raise GenerationReceiptEvidenceError("remote receipt is not succeeded")
        run_id = _opaque(external_run_id, "external_run_id", _RUN_REF_RE)
        workspace = _opaque(workspace_id, "workspace_id", _WORKSPACE_REF_RE)
        project = _opaque(project_id, "project_id", _PROJECT_REF_RE)
        key = _hash(external_idempotency_key, "external_idempotency_key")
        correlation = _text(correlation_id, "correlation_id", limit=64)
        if correlation != key:
            raise GenerationReceiptEvidenceError("correlation_id does not match intent key")
        plan = _hash(plan_hash, "plan_hash")
        request = _hash(request_hash, "request_hash")
        digest = _hash(response_digest, "response_digest")
        selector = None if selector_id in (None, "") else _text(selector_id, "selector_id", limit=164)
        preset = None if preset_id in (None, "") else _text(preset_id, "preset_id", limit=164)
        preset_revision = (
            None
            if preset_revision_id in (None, "")
            else _text(preset_revision_id, "preset_revision_id", limit=164)
        )
        if selector is None and (preset is None or preset_revision is None):
            raise GenerationReceiptEvidenceError("selector or complete preset pin is required")
        if (preset is None) != (preset_revision is None):
            raise GenerationReceiptEvidenceError("preset pin is incomplete")
        if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
            raise GenerationReceiptEvidenceError("outputs must be a sequence")
        projected = tuple(outputs)
        if not projected or len(projected) > 20 or any(
            not isinstance(item, GenerationReceiptOutputEvidence) for item in projected
        ):
            raise GenerationReceiptEvidenceError("outputs are not typed evidence")
        keys = {
            (item.external_asset_id, item.external_output_version)
            for item in projected
        }
        if len(keys) != len(projected):
            raise GenerationReceiptEvidenceError("duplicate output lineage")
        object.__setattr__(self, "remote_status", status)
        object.__setattr__(self, "external_run_id", run_id)
        object.__setattr__(self, "workspace_id", workspace)
        object.__setattr__(self, "project_id", project)
        object.__setattr__(self, "external_idempotency_key", key)
        object.__setattr__(self, "correlation_id", correlation)
        object.__setattr__(self, "plan_hash", plan)
        object.__setattr__(self, "request_hash", request)
        object.__setattr__(self, "response_digest", digest)
        object.__setattr__(self, "selector_id", selector)
        object.__setattr__(self, "preset_id", preset)
        object.__setattr__(self, "preset_revision_id", preset_revision)
        object.__setattr__(self, "outputs", projected)
        object.__setattr__(self, "_token", _EVIDENCE_TOKEN)

    @property
    def sealed(self) -> bool:
        return self._token is _EVIDENCE_TOKEN

    def to_dict(self) -> dict[str, Any]:
        """Return only normalized evidence fields; never raw provider data."""

        return {
            "remote_status": self.remote_status,
            "external_run_id": self.external_run_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "external_idempotency_key": self.external_idempotency_key,
            "correlation_id": self.correlation_id,
            "plan_hash": self.plan_hash,
            "request_hash": self.request_hash,
            "response_digest": self.response_digest,
            "selector_id": self.selector_id,
            "preset_id": self.preset_id,
            "preset_revision_id": self.preset_revision_id,
            "outputs": [
                {
                    "external_asset_id": item.external_asset_id,
                    "external_output_version": item.external_output_version,
                    "sha256": item.sha256,
                }
                for item in self.outputs
            ],
        }


@dataclass(frozen=True, slots=True)
class GenerationOutputBindingProof:
    """Local immutable output plus its managed byte binding."""

    output_id: UUID
    asset_revision_id: UUID
    external_asset_id: str
    external_output_version: str
    output_sha256: str
    asset_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "output_id": str(self.output_id),
            "asset_revision_id": str(self.asset_revision_id),
            "external_asset_id": self.external_asset_id,
            "external_output_version": self.external_output_version,
            "output_sha256": self.output_sha256,
            "asset_sha256": self.asset_sha256,
        }


@dataclass(frozen=True, slots=True, init=False)
class GenerationReceiptProof:
    """Immutable proof consumed by the parent-owned settlement boundary."""

    work_item_id: UUID
    agent_run_id: UUID
    plan_id: UUID
    plan_hash: str
    project_id: UUID | None
    space_id: UUID | None
    persona_id: UUID
    agent_id: UUID
    agent_revision_id: UUID
    intent_id: UUID
    request_hash: str
    external_idempotency_key: str
    generation_run_id: UUID
    observation_id: UUID
    external_run_id: str
    outputs: tuple[GenerationOutputBindingProof, ...]
    response_digest: str
    proof_hash: str
    _token: object = field(repr=False, compare=False)

    def __init__(self, **kwargs: Any) -> None:
        token = kwargs.pop("_token", None)
        if token is not _PROOF_TOKEN:
            raise GenerationReceiptProofError("generation proof is factory-sealed")
        raw_project_id = kwargs.pop("project_id", None)
        raw_space_id = kwargs.pop("space_id", None)
        fields = {
            "work_item_id": _uuid(kwargs.pop("work_item_id"), "work_item_id"),
            "agent_run_id": _uuid(kwargs.pop("agent_run_id"), "agent_run_id"),
            "plan_id": _uuid(kwargs.pop("plan_id"), "plan_id"),
            "plan_hash": _hash(kwargs.pop("plan_hash"), "plan_hash"),
            "project_id": (
                None
                if raw_project_id in (None, "")
                else _uuid(raw_project_id, "project_id")
            ),
            "space_id": (
                None
                if raw_space_id in (None, "")
                else _uuid(raw_space_id, "space_id")
            ),
            "persona_id": _uuid(kwargs.pop("persona_id"), "persona_id"),
            "agent_id": _uuid(kwargs.pop("agent_id"), "agent_id"),
            "agent_revision_id": _uuid(kwargs.pop("agent_revision_id"), "agent_revision_id"),
            "intent_id": _uuid(kwargs.pop("intent_id"), "intent_id"),
            "request_hash": _hash(kwargs.pop("request_hash"), "request_hash"),
            "external_idempotency_key": _hash(
                kwargs.pop("external_idempotency_key"), "external_idempotency_key"
            ),
            "generation_run_id": _uuid(kwargs.pop("generation_run_id"), "generation_run_id"),
            "observation_id": _uuid(kwargs.pop("observation_id"), "observation_id"),
            "external_run_id": _opaque(
                kwargs.pop("external_run_id"), "external_run_id", _RUN_REF_RE
            ),
            "response_digest": _hash(kwargs.pop("response_digest"), "response_digest"),
        }
        outputs = kwargs.pop("outputs", None)
        proof_hash = kwargs.pop("proof_hash", None)
        if kwargs:
            raise GenerationReceiptProofError("unsupported proof fields")
        if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
            raise GenerationReceiptBindingError("proof outputs are invalid")
        outputs_tuple = tuple(outputs)
        if not outputs_tuple or len(outputs_tuple) > 20 or any(
            not isinstance(item, GenerationOutputBindingProof) for item in outputs_tuple
        ):
            raise GenerationReceiptBindingError("proof outputs are not typed")
        fields["outputs"] = outputs_tuple
        calculated = _proof_hash(
            {
                **{key: str(value) if isinstance(value, UUID) else value for key, value in fields.items() if key != "outputs"},
                "outputs": [item.to_dict() for item in outputs_tuple],
            }
        )
        if proof_hash not in (None, "") and str(proof_hash) != calculated:
            raise GenerationReceiptBindingError("proof hash mismatch")
        fields["proof_hash"] = calculated
        fields["_token"] = _PROOF_TOKEN
        for key, value in fields.items():
            object.__setattr__(self, key, value)

    @property
    def sealed(self) -> bool:
        return self._token is _PROOF_TOKEN

    def integrity_valid(self) -> bool:
        payload = {
            "work_item_id": self.work_item_id,
            "agent_run_id": self.agent_run_id,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "project_id": self.project_id,
            "space_id": self.space_id,
            "persona_id": self.persona_id,
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "intent_id": self.intent_id,
            "request_hash": self.request_hash,
            "external_idempotency_key": self.external_idempotency_key,
            "generation_run_id": self.generation_run_id,
            "observation_id": self.observation_id,
            "external_run_id": self.external_run_id,
            "response_digest": self.response_digest,
            "outputs": [item.to_dict() for item in self.outputs],
        }
        return self.proof_hash == _proof_hash(payload)

    @property
    def output_ids(self) -> tuple[UUID, ...]:
        return tuple(item.output_id for item in self.outputs)

    @property
    def asset_revision_ids(self) -> tuple[UUID, ...]:
        return tuple(item.asset_revision_id for item in self.outputs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": str(self.work_item_id),
            "agent_run_id": str(self.agent_run_id),
            "plan_id": str(self.plan_id),
            "plan_hash": self.plan_hash,
            "project_id": _safe_id(self.project_id),
            "space_id": _safe_id(self.space_id),
            "persona_id": str(self.persona_id),
            "agent_id": str(self.agent_id),
            "agent_revision_id": str(self.agent_revision_id),
            "intent_id": str(self.intent_id),
            "request_hash": self.request_hash,
            "external_idempotency_key": self.external_idempotency_key,
            "generation_run_id": str(self.generation_run_id),
            "observation_id": str(self.observation_id),
            "external_run_id": self.external_run_id,
            "outputs": [item.to_dict() for item in self.outputs],
            "output_ids": [str(item) for item in self.output_ids],
            "asset_revision_ids": [
                str(item) for item in self.asset_revision_ids
            ],
            "response_digest": self.response_digest,
            "proof_hash": self.proof_hash,
            "receipt_recovered": True,
        }


def _issue_generation_receipt_evidence(**kwargs: Any) -> GenerationReceiptEvidence:
    """Issue normalized evidence from the MediaOps generation service only.

    This private factory is the intended hook for the recovery service.  It
    accepts scalar normalized fields and typed output refs, never a provider
    mapping, so arbitrary API JSON cannot become authoritative by wrapping it.
    """

    return GenerationReceiptEvidence(_token=_EVIDENCE_TOKEN, **kwargs)


# Named import for the MediaOps recovery hook. The function still accepts
# normalized scalar facts and typed output refs only; it has no mapping/raw
# response overload and therefore is not a public success-JSON promotion API.
issue_generation_receipt_evidence = _issue_generation_receipt_evidence


def _bind_generation_receipt_proof(
    evidence: GenerationReceiptEvidence,
    *,
    work: Any,
    agent_run: Any,
    plan: Any,
    intent: Any,
    generation_run: Any,
    observation: Any,
    outputs: Iterable[Any],
    asset_revisions: Iterable[Any],
    allow_settled_replay: bool = False,
    allow_run_success_replay: bool = False,
) -> GenerationReceiptProof:
    """Bind server evidence to immutable local ORM rows.

    ACL, ExternalActionApproval and current relationship checks intentionally
    stay in ``AgentWorkService`` where they require a live session.  This
    function performs the pure row-to-evidence binding and hash construction.
    """

    if not isinstance(evidence, GenerationReceiptEvidence) or not evidence.sealed:
        raise GenerationReceiptEvidenceError("generation receipt evidence is not sealed")

    work_id = _uuid(_row_field(work, "id"), "work.id")
    run_id = _uuid(_row_field(agent_run, "id"), "agent_run.id")
    plan_id = _uuid(_row_field(plan, "id"), "plan.id")
    if _row_field(work, "source_type") != "media.generation.plan":
        raise GenerationReceiptBindingError("work source is not generation")
    if str(_row_field(work, "source_id")) != str(plan_id):
        raise GenerationReceiptBindingError("work source id does not match plan")
    if str(_row_field(work, "source_revision")) != str(_row_field(plan, "plan_hash")):
        raise GenerationReceiptBindingError("work source revision does not match plan")
    work_state = str(_row_field(work, "state"))
    work_classification = str(_row_field(work, "outcome_classification"))
    settled_replay = bool(allow_settled_replay)
    if settled_replay:
        if work_state != "succeeded" or work_classification != "succeeded":
            raise GenerationReceiptBindingError("settled replay is not terminal success")
    else:
        if work_state != "awaiting_approval":
            raise GenerationReceiptBindingError("work is not the original awaiting-approval row")
        if work_classification != "awaiting_approval":
            raise GenerationReceiptBindingError("work classification is not awaiting approval")
    if any(
        _row_field(work, name, required=False) not in (None, "")
        for name in ("lease_owner", "lease_token", "lease_expires_at")
    ):
        raise GenerationReceiptBindingError("generation work still has a lease")
    old_error_code = str(_row_field(work, "safe_error_code", required=False) or "").strip().casefold()
    if not settled_replay and old_error_code and old_error_code not in _RECEIPT_ERROR_CODES:
        raise GenerationReceiptBindingError("old Work error is not generation receipt recovery")
    if _uuid(_row_field(agent_run, "work_item_id"), "agent_run.work_item_id") != work_id:
        raise GenerationReceiptBindingError("AgentRun is not the original Work run")
    run_status = str(_row_field(agent_run, "status"))
    if settled_replay:
        if run_status not in {"awaiting_approval", "succeeded"}:
            raise GenerationReceiptBindingError("settled replay AgentRun is not recoverable")
    elif allow_run_success_replay:
        if run_status != "succeeded":
            raise GenerationReceiptBindingError("run-success replay AgentRun is not terminal success")
    elif run_status != "awaiting_approval":
        raise GenerationReceiptBindingError("AgentRun is not awaiting approval")
    attempt = _row_field(work, "attempt_count", required=False)
    run_attempt = _row_field(agent_run, "work_item_attempt", required=False)
    if attempt is not None and run_attempt is not None and int(attempt) != int(run_attempt):
        raise GenerationReceiptBindingError("AgentRun attempt does not match Work")
    markers = set()
    for row in (work, agent_run):
        markers.update(_marker_values(_row_field(row, "safe_error_code", required=False)))
        markers.update(_marker_values(_row_field(row, "result_summary_json", required=False)))
        markers.update(_marker_values(_row_field(row, "run_metadata", required=False)))
        markers.update(_marker_values(_row_field(row, "metadata_json", required=False)))
        markers.update(_marker_values(_row_field(row, "result", required=False)))
    if (
        not settled_replay
        and not allow_run_success_replay
        and not (markers & _RECEIPT_ERROR_CODES)
        and not (markers & _RECEIPT_DOMAIN_STATUSES)
    ):
        raise GenerationReceiptBindingError("old generation receipt-pending marker is missing")

    expected_plan_hash = _hash(_row_field(plan, "plan_hash"), "plan.plan_hash")
    expected_request_hash = _hash(_row_field(intent, "request_hash"), "intent.request_hash")
    if expected_plan_hash != evidence.plan_hash:
        raise GenerationReceiptBindingError("plan hash does not match evidence")
    if evidence.request_hash != expected_request_hash:
        raise GenerationReceiptBindingError("request hash does not match intent")
    external_key = _hash(
        _row_field(intent, "external_idempotency_key"),
        "intent.external_idempotency_key",
    )
    if external_key != evidence.external_idempotency_key:
        raise GenerationReceiptBindingError("external idempotency key does not match intent")
    intent_status = str(_row_field(intent, "status", required=False) or "").strip().casefold()
    if intent_status and intent_status not in {"pending", "submitted", "uncertain"}:
        raise GenerationReceiptBindingError("generation intent is not recoverable")
    generation_run_id = _uuid(_row_field(generation_run, "id"), "generation_run.id")
    if _uuid(_row_field(generation_run, "plan_id"), "generation_run.plan_id") != plan_id:
        raise GenerationReceiptBindingError("generation run does not match plan")
    if _uuid(_row_field(generation_run, "intent_id"), "generation_run.intent_id") != _uuid(
        _row_field(intent, "id"), "intent.id"
    ):
        raise GenerationReceiptBindingError("generation run does not match intent")
    if str(_row_field(generation_run, "status")) != "succeeded":
        raise GenerationReceiptBindingError("local generation run is not succeeded")
    external_run_id = _opaque(
        _row_field(generation_run, "external_run_id"),
        "generation_run.external_run_id",
        _RUN_REF_RE,
    )
    if external_run_id != evidence.external_run_id:
        raise GenerationReceiptBindingError("external run id does not match evidence")
    intent_run_id = _row_field(intent, "external_run_id", required=False)
    if intent_run_id not in (None, "") and str(intent_run_id) != external_run_id:
        raise GenerationReceiptBindingError("intent external run id changed")
    if _row_field(generation_run, "external_workspace_id") != evidence.workspace_id:
        raise GenerationReceiptBindingError("generation workspace scope mismatch")
    if _row_field(generation_run, "external_project_id") != evidence.project_id:
        raise GenerationReceiptBindingError("generation project scope mismatch")
    if str(_row_field(observation, "status")) != "succeeded":
        raise GenerationReceiptBindingError("generation observation is not succeeded")
    if _uuid(_row_field(observation, "generation_run_id"), "observation.generation_run_id") != generation_run_id:
        raise GenerationReceiptBindingError("observation does not match generation run")
    observation_run_id = _row_field(observation, "external_run_id", required=False)
    if observation_run_id not in (None, "") and str(observation_run_id) != external_run_id:
        raise GenerationReceiptBindingError("observation external run id changed")
    observation_id = _uuid(_row_field(observation, "id"), "observation.id")
    observation_hash = _hash(_row_field(observation, "observation_hash"), "observation.observation_hash")
    if evidence.response_digest != observation_hash:
        raise GenerationReceiptBindingError("authoritative GET digest does not match observation")

    output_rows = tuple(outputs)
    asset_rows = tuple(asset_revisions)
    if not output_rows or len(output_rows) != len(evidence.outputs):
        raise GenerationReceiptBindingError("local output count does not match receipt")
    output_by_lineage = {}
    for output in output_rows:
        if isinstance(output, Mapping):
            raise GenerationReceiptBindingError("output row is not authoritative")
        output_id = _uuid(_row_field(output, "id"), "output.id")
        if _uuid(_row_field(output, "generation_run_id"), "output.generation_run_id") != generation_run_id:
            raise GenerationReceiptBindingError("output does not match generation run")
        if str(_row_field(output, "byte_binding_state")) != "managed":
            raise GenerationReceiptBindingError("output bytes are not managed")
        lineage = (
            _row_field(output, "external_asset_id"),
            _row_field(output, "external_output_version"),
        )
        if lineage in output_by_lineage:
            raise GenerationReceiptBindingError("duplicate local output lineage")
        output_sha = _hash(_row_field(output, "sha256"), "output.sha256")
        output_by_lineage[lineage] = (output, output_id, output_sha)
    evidence_by_lineage = {
        (item.external_asset_id, item.external_output_version): item
        for item in evidence.outputs
    }
    if set(output_by_lineage) != set(evidence_by_lineage):
        raise GenerationReceiptBindingError("output lineage does not match receipt")
    asset_by_output: dict[UUID, Any] = {}
    for asset in asset_rows:
        if isinstance(asset, Mapping):
            raise GenerationReceiptBindingError("asset revision row is not authoritative")
        asset_id = _uuid(_row_field(asset, "id"), "asset_revision.id")
        output_ref = _uuid(_row_field(asset, "generation_output_id"), "asset_revision.generation_output_id")
        if output_ref in asset_by_output:
            raise GenerationReceiptBindingError("duplicate managed asset revision")
        if str(_row_field(asset, "source_kind")) != "generation_output":
            raise GenerationReceiptBindingError("asset source is not generation output")
        asset_sha = _hash(_row_field(asset, "sha256"), "asset_revision.sha256")
        asset_by_output[output_ref] = (asset_id, asset_sha)
    if set(asset_by_output) != {item[1] for item in output_by_lineage.values()}:
        raise GenerationReceiptBindingError("managed asset bindings are incomplete")
    bindings: list[GenerationOutputBindingProof] = []
    for lineage, item in sorted(evidence_by_lineage.items()):
        output, output_id, output_sha = output_by_lineage[lineage]
        asset_id, asset_sha = asset_by_output[output_id]
        if output_sha != item.sha256 or asset_sha != output_sha:
            raise GenerationReceiptBindingError("managed asset SHA does not match output")
        bindings.append(
            GenerationOutputBindingProof(
                output_id=output_id,
                asset_revision_id=asset_id,
                external_asset_id=lineage[0],
                external_output_version=lineage[1],
                output_sha256=output_sha,
                asset_sha256=asset_sha,
            )
        )

    return GenerationReceiptProof(
        _token=_PROOF_TOKEN,
        work_item_id=work_id,
        agent_run_id=run_id,
        plan_id=plan_id,
        plan_hash=expected_plan_hash,
        project_id=(
            None
            if _row_field(work, "project_id", required=False) in (None, "")
            else _uuid(_row_field(work, "project_id"), "work.project_id")
        ),
        space_id=(
            None
            if _row_field(work, "space_id", required=False) in (None, "")
            else _uuid(_row_field(work, "space_id"), "work.space_id")
        ),
        persona_id=_uuid(_row_field(work, "persona_id"), "work.persona_id"),
        agent_id=_uuid(_row_field(work, "assigned_agent_id"), "work.assigned_agent_id"),
        agent_revision_id=_uuid(_row_field(work, "agent_revision_id"), "work.agent_revision_id"),
        intent_id=_uuid(_row_field(intent, "id"), "intent.id"),
        request_hash=expected_request_hash,
        external_idempotency_key=external_key,
        generation_run_id=generation_run_id,
        observation_id=observation_id,
        external_run_id=external_run_id,
        outputs=tuple(bindings),
        response_digest=observation_hash,
    )


__all__ = [
    "GenerationOutputBindingProof",
    "GenerationReceiptBindingError",
    "GenerationReceiptEvidence",
    "GenerationReceiptEvidenceError",
    "GenerationReceiptProof",
    "GenerationReceiptProofError",
    "_bind_generation_receipt_proof",
    "_issue_generation_receipt_evidence",
    "issue_generation_receipt_evidence",
]
