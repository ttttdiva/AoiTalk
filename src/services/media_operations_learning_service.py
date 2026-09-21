"""Evidence-backed MediaOps Learning proposal workflow.

Learning is intentionally a proposal-first boundary. Agents and imported
evidence may create a proposal, but only a server-authenticated human (or an
administrator) may edit, reject, approve, or apply one. Applying a Persona
proposal is an optimistic, append-only revision operation; the expected
revision triple is checked while the stable Persona row is locked so a retry
cannot create a second revision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.models import Persona, PersonaRevision
from ..memory.models.media_operations_learning import LearningProposal
from .media_operations_metrics_service import (
    MediaOperationsMetricsService,
    _datetime,
    _normalize_evidence,
    _ref,
    _required_text,
)
from .media_operations_service import (
    MediaOperationsConflictError,
    MediaOperationsNotFoundError,
    MediaOperationsValidationError,
    _REVISION_CONTENT_FIELDS,
    _actor_field,
    _actor_id,
    _as_uuid,
    _normalize_revision_input,
    _revision_content_from_safe_dict,
    _validated_sha256,
    sha256_json,
)


_TARGET_FIELD_SET = frozenset(_REVISION_CONTENT_FIELDS)
_MAX_TARGET_FIELDS = len(_REVISION_CONTENT_FIELDS)
_MAX_HISTORY = 100
_REVIEW_ACTIONS = frozenset({"edit", "approve", "reject", "apply", "stale"})


def _mapping(value: Any, label: str, *, required: bool = False) -> dict[str, Any]:
    if value is None:
        if required:
            raise MediaOperationsValidationError(f"{label} is required")
        return {}
    if not isinstance(value, Mapping):
        raise MediaOperationsValidationError(f"{label} must be an object")
    if len(value) > _MAX_TARGET_FIELDS:
        raise MediaOperationsValidationError(f"{label} has too many fields")
    return dict(value)


def _target_fields(value: Any, *, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    supplied = list(after.keys()) + [key for key in before if key not in after]
    if value is None:
        values = supplied
    else:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise MediaOperationsValidationError("target_fields must be a list")
        if len(value) > _MAX_TARGET_FIELDS:
            raise MediaOperationsValidationError("target_fields has too many fields")
        values = list(value)
    result: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise MediaOperationsValidationError("target_fields must contain field names")
        field = raw.strip()
        if field not in _TARGET_FIELD_SET:
            raise MediaOperationsValidationError(f"unsupported target Character field: {field}")
        if field not in result:
            result.append(field)
    if not result and (before or after):
        raise MediaOperationsValidationError("target_fields must not be empty")
    if set(before) - set(result) or set(after) - set(result):
        raise MediaOperationsValidationError("proposed diff contains a field outside target_fields")
    if set(result) - set(after):
        raise MediaOperationsValidationError("proposed_after must include every target field")
    return result


def _normalise_diff(target_fields: Any, proposed_before: Any, proposed_after: Any) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    before = _mapping(proposed_before, "proposed_before")
    after = _mapping(proposed_after, "proposed_after")
    fields = _target_fields(target_fields, before=before, after=after)
    # Validate values before persisting them.  Applying the full revision
    # normalizer later is not sufficient because an untrusted proposal must
    # never store a raw provider/path/credential-shaped object in its diff.
    baseline: dict[str, Any] = {
        "display_name": "Learning target",
        "summary": None,
        "voice": None,
        "audience": None,
        "platforms": [],
        "content_pillars": [],
        "public_aliases": [],
        "niche": None,
        "positioning": None,
        "visual_identity": {},
        "creative_direction": None,
        "allowed_subjects": [],
        "prohibited_subjects": [],
        "adult_policy": None,
        "sensitive_policy": None,
        "ip_policy": None,
        "disclosure_policy": None,
        "monetization_policy": {},
        "kpi_objectives": [],
        "default_language": None,
        "locale": None,
        "timezone": None,
        "research_policy": {},
        "image_production_policy": {},
        "video_production_policy": {},
    }

    def safe_values(source: Mapping[str, Any], label: str) -> dict[str, Any]:
        candidate = dict(baseline)
        candidate.update(source)
        try:
            normalized = _normalize_revision_input(**candidate)
        except (TypeError, ValueError, MediaOperationsValidationError) as exc:
            raise MediaOperationsValidationError(f"{label} contains an invalid Character field value") from exc
        return {field: normalized[field] for field in source}

    return fields, safe_values({field: before[field] for field in fields if field in before}, "proposed_before"), safe_values({field: after[field] for field in fields}, "proposed_after")


def _revision_triple(revision_id: Any, revision_version: Any, revision_hash: Any, *, required: bool) -> tuple[UUID | None, int | None, str | None]:
    values = (revision_id, revision_version, revision_hash)
    if all(value in (None, "") for value in values):
        if required:
            raise MediaOperationsValidationError("expected_persona_revision_id, expected_persona_revision_version and expected_persona_revision_hash are required")
        return None, None, None
    if any(value in (None, "") for value in values):
        raise MediaOperationsValidationError("expected Persona revision triple is incomplete")
    parsed_id = _as_uuid(revision_id, "expected_persona_revision_id")
    assert parsed_id is not None
    if isinstance(revision_version, bool):
        raise MediaOperationsValidationError("expected_persona_revision_version must be an integer")
    try:
        parsed_version = int(revision_version)
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError("expected_persona_revision_version must be an integer") from exc
    if parsed_version < 1:
        raise MediaOperationsValidationError("expected_persona_revision_version must be at least 1")
    return parsed_id, parsed_version, _validated_sha256(revision_hash, "expected_persona_revision_hash")


def _parse_persona_ref(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return _as_uuid(value, "subject_ref", required=False)
    except MediaOperationsValidationError:
        return None


def _is_human_actor(actor: Any) -> bool:
    role = str(_actor_field(actor, "role", "") or "").strip().lower()
    if role == "admin":
        return True
    if bool(_actor_field(actor, "is_agent", False)):
        return False
    actor_type = _actor_field(actor, "actor_type", None)
    # Direct service callers from older integrations do not carry an
    # authority marker; preserve compatibility while rejecting explicit
    # agent/system/unknown markers from the HTTP principal projection.
    if actor_type is None:
        return True
    return str(actor_type).strip().lower() in {"human", "user", "admin"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MediaOperationsLearningService(MediaOperationsMetricsService):
    """ACL-scoped proposal, review and Persona-application facade."""

    async def _get_proposal(self, session: Any, proposal_id: Any, *, for_update: bool = False) -> LearningProposal:
        parsed = _as_uuid(proposal_id, "proposal_id")
        assert parsed is not None
        statement = select(LearningProposal).where(LearningProposal.id == parsed).limit(1)
        if for_update:
            statement = statement.with_for_update()
        row = await self._scalar(session, statement)
        if row is None:
            raise MediaOperationsNotFoundError("proposal_id not found")
        return row

    def _assert_human(self, actor: Any) -> None:
        if not _is_human_actor(actor):
            raise MediaOperationsValidationError("human approval is required for learning proposal review")

    async def _assert_proposal_access(self, session: Any, actor: Any, row: LearningProposal, *, permission: str) -> UUID:
        return await self._assert_entity_access(session, actor, row, permission=permission)

    @staticmethod
    def _history(row: LearningProposal) -> list[dict[str, Any]]:
        raw = getattr(row, "review_history_json", None)
        return [dict(item) for item in raw if isinstance(item, Mapping)][-_MAX_HISTORY:]

    def _history_replay(self, row: LearningProposal, *, action: str, idempotency_key: str, action_hash: str | None = None) -> dict[str, Any] | None:
        for entry in reversed(self._history(row)):
            if entry.get("action") != action or entry.get("idempotency_key") != idempotency_key:
                continue
            if action_hash is not None and entry.get("action_hash") != action_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different learning action")
            result = entry.get("result")
            return dict(result) if isinstance(result, Mapping) else row.to_safe_dict()
        return None

    def _append_history(self, row: LearningProposal, *, action: str, actor: Any, idempotency_key: str, reason: str | None = None, action_hash: str | None = None, result: Mapping[str, Any] | None = None, from_status: str | None = None) -> None:
        if action not in _REVIEW_ACTIONS:
            raise MediaOperationsValidationError("unsupported learning review action")
        entry: dict[str, Any] = {"action": action, "actor_id": str(_actor_id(actor)), "at": _iso_now(), "idempotency_key": idempotency_key}
        if reason is not None:
            entry["reason"] = reason
        if action_hash is not None:
            entry["action_hash"] = action_hash
        if from_status is not None:
            entry["from_status"] = from_status
        if result is not None:
            entry["result"] = dict(result)
        history = self._history(row)
        history.append(entry)
        row.review_history_json = history[-_MAX_HISTORY:]

    async def _persona_revision_for_proposal(self, session: Any, row: LearningProposal, *, lock: bool) -> tuple[Persona, PersonaRevision] | None:
        if str(getattr(row, "subject_type", "")).lower() != "persona":
            return None
        persona_id = _parse_persona_ref(row.subject_ref)
        if persona_id is None:
            return None
        statement = select(Persona).where(Persona.id == persona_id).limit(1)
        if lock:
            statement = statement.with_for_update()
        persona = await self._scalar(session, statement)
        if persona is None:
            raise MediaOperationsNotFoundError("target Character not found")
        revision = await self._latest_revision(session, persona.id)
        if revision is None:
            raise MediaOperationsConflictError("target Character revision history is incomplete")
        return persona, revision

    async def _hydrate_expected_revision(self, session: Any, row: LearningProposal, *, expected_id: UUID | None, expected_version: int | None, expected_hash: str | None, current: PersonaRevision | None) -> tuple[UUID | None, int | None, str | None]:
        if expected_id is not None:
            return expected_id, expected_version, expected_hash
        stored = _revision_triple(getattr(row, "expected_persona_revision_id", None), getattr(row, "expected_persona_revision_version", None), getattr(row, "expected_persona_revision_hash", None), required=False)
        if stored[0] is not None:
            return stored
        if current is None:
            return None, None, None
        return current.id, int(current.version), str(current.content_hash).lower()

    async def create_learning_proposal(self, session: Any | None = None, actor: Any | None = None, *, subject_type: Any, subject_ref: Any, title: Any, summary: Any, recommendation: Any, evidence_refs: Any = None, evidence: Any = None, human_decision_refs: Any = None, decision_refs: Any = None, target_fields: Any = None, target_character_fields: Any = None, proposed_before: Any = None, before: Any = None, proposed_after: Any = None, after: Any = None, expected_persona_revision_id: Any = None, expected_persona_revision_version: Any = None, expected_persona_revision_hash: Any = None, window_start: Any, window_end: Any, confidence: Any, uncertainty: Any, project_id: Any = None, proposal_type: Any = "learning", idempotency_key: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        actor_id, project_uuid = await self._scope_for_create(session, actor, project_id)
        clean_subject_type = _required_text(subject_type, "subject_type", 32).lower()
        if clean_subject_type not in {"persona", "account", "content", "publication", "experiment", "general"}:
            raise MediaOperationsValidationError("subject_type is invalid")
        clean_subject_ref = _ref(subject_ref, "subject_ref")
        clean_proposal_type = _required_text(proposal_type, "proposal_type", 32).lower()
        if clean_proposal_type not in {"learning", "content", "timing", "audience", "pricing"}:
            raise MediaOperationsValidationError("proposal_type is invalid")
        clean_title = _required_text(title, "title", 255)
        clean_summary = _required_text(summary, "summary", 4000)
        clean_recommendation = _required_text(recommendation, "recommendation", 8000)
        start = _datetime(window_start, "window_start")
        end = _datetime(window_end, "window_end")
        assert start is not None and end is not None
        if start >= end:
            raise MediaOperationsValidationError("window_start must be before window_end")
        try:
            confidence_value = float(confidence)
            uncertainty_value = float(uncertainty)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError("confidence and uncertainty must be numbers") from exc
        if not 0 <= confidence_value <= 1:
            raise MediaOperationsValidationError("confidence must be between 0 and 1")
        if not 0 <= uncertainty_value <= 1:
            raise MediaOperationsValidationError("uncertainty must be between 0 and 1")
        refs = _normalize_evidence(evidence_refs if evidence_refs is not None else evidence, "evidence_refs", required=True)
        decision_values = human_decision_refs if human_decision_refs is not None else decision_refs
        decisions: list[str] = []
        if decision_values is not None:
            if isinstance(decision_values, (str, bytes)) or not isinstance(decision_values, Sequence):
                raise MediaOperationsValidationError("human_decision_refs must be a list")
            if len(decision_values) > 64:
                raise MediaOperationsValidationError("human_decision_refs has too many items")
            decisions = [_ref(item, "human_decision_ref") for item in decision_values]
        fields_value = target_fields if target_fields is not None else target_character_fields
        before_value = proposed_before if proposed_before is not None else before
        after_value = proposed_after if proposed_after is not None else after
        fields, diff_before, diff_after = _normalise_diff(fields_value, before_value, after_value)
        expected_id, expected_version, expected_hash = _revision_triple(expected_persona_revision_id, expected_persona_revision_version, expected_persona_revision_hash, required=False)
        persona_id = _parse_persona_ref(clean_subject_ref) if clean_subject_type == "persona" else None
        if persona_id is not None:
            if not fields:
                raise MediaOperationsValidationError("Persona learning proposal requires target Character fields")
            current = await self._latest_revision(session, persona_id)
            if current is None:
                raise MediaOperationsNotFoundError("target Character not found")
            await self._assert_entity_access(session, actor, current, permission="read")
            if project_uuid != current.project_id:
                raise MediaOperationsValidationError("proposal target Character is outside the proposal scope")
            if expected_id is None:
                expected_id, expected_version, expected_hash = current.id, int(current.version), str(current.content_hash).lower()
            if not diff_before and fields:
                current_content = _revision_content_from_safe_dict(current.to_safe_dict())
                diff_before = {field: current_content.get(field) for field in fields}
        key = _required_text(idempotency_key, "idempotency_key", 255)
        payload = {"project_id": str(project_uuid) if project_uuid else None, "subject_type": clean_subject_type, "subject_ref": clean_subject_ref, "proposal_type": clean_proposal_type, "title": clean_title, "summary": clean_summary, "recommendation": clean_recommendation, "evidence_refs": refs, "human_decision_refs": decisions, "target_fields": fields, "proposed_before": diff_before, "proposed_after": diff_after, "expected_persona_revision_id": str(expected_id) if expected_id else None, "expected_persona_revision_version": expected_version, "expected_persona_revision_hash": expected_hash, "window_start": start.isoformat(), "window_end": end.isoformat(), "confidence": confidence_value, "uncertainty": uncertainty_value, "human_review_required": True, "review_policy": "human_review_before_apply", "status": "pending_review"}
        proposal_hash = sha256_json(payload)
        existing = await self._find_scoped_idempotency(session, LearningProposal, owner_user_id=actor_id, project_id=project_uuid, key=key)
        if existing is not None:
            if existing.proposal_hash != proposal_hash:
                raise MediaOperationsConflictError("idempotency key was already used with different learning proposal")
            return existing.to_safe_dict()
        row = LearningProposal(id=uuid4(), owner_user_id=actor_id, project_id=project_uuid, subject_type=clean_subject_type, subject_ref=clean_subject_ref, proposal_type=clean_proposal_type, title=clean_title, summary=clean_summary, recommendation=clean_recommendation, evidence_refs=refs, human_decision_refs_json=decisions, target_fields_json=fields, proposed_before_json=diff_before, proposed_after_json=diff_after, expected_persona_revision_id=expected_id, expected_persona_revision_version=expected_version, expected_persona_revision_hash=expected_hash, review_history_json=[], window_start=start, window_end=end, confidence=confidence_value, uncertainty=uncertainty_value, human_review_required=True, review_policy="human_review_before_apply", status="pending_review", proposal_hash=proposal_hash, idempotency_key=key, created_by=actor_id)
        session.add(row)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(session, LearningProposal, owner_user_id=actor_id, project_id=project_uuid, key=key)
            if recovered is not None and recovered.proposal_hash == proposal_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError("learning proposal conflicts with an existing record") from exc
        return row.to_safe_dict()

    propose_learning = create_learning_proposal
    propose_learning_from_evidence = create_learning_proposal

    async def list_learning_proposals(self, session: Any | None = None, actor: Any | None = None, *, project_id: Any = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        rows = await self._list(session, actor, LearningProposal, project_id=project_id, limit=limit, offset=offset)
        return [row.to_safe_dict() for row in rows]

    async def get_learning_proposal(self, session: Any | None = None, actor: Any | None = None, proposal_id: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or proposal_id is None:
            raise MediaOperationsValidationError("actor and proposal_id are required")
        row = await self._get_proposal(session, proposal_id)
        await self._assert_proposal_access(session, actor, row, permission="read")
        return row.to_safe_dict()

    def _editable_payload(self, row: LearningProposal) -> dict[str, Any]:
        return {"title": row.title, "summary": row.summary, "recommendation": row.recommendation, "evidence_refs": list(row.evidence_refs or []), "human_decision_refs": list(getattr(row, "human_decision_refs_json", None) or []), "target_fields": list(getattr(row, "target_fields_json", None) or []), "proposed_before": dict(getattr(row, "proposed_before_json", None) or {}), "proposed_after": dict(getattr(row, "proposed_after_json", None) or {}), "window_start": row.window_start, "window_end": row.window_end, "confidence": row.confidence, "uncertainty": row.uncertainty}

    async def edit_learning_proposal(self, session: Any | None = None, actor: Any | None = None, proposal_id: Any = None, *, changes: Mapping[str, Any] | None = None, reason: Any = None, idempotency_key: Any = None, **direct_changes: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or proposal_id is None:
            raise MediaOperationsValidationError("actor and proposal_id are required")
        self._assert_human(actor)
        row = await self._get_proposal(session, proposal_id, for_update=True)
        await self._assert_proposal_access(session, actor, row, permission="write")
        key = _required_text(idempotency_key, "idempotency_key", 255)
        raw_changes = {**dict(changes or {}), **direct_changes}
        allowed = {"title", "summary", "recommendation", "evidence_refs", "human_decision_refs", "decision_refs", "target_fields", "target_character_fields", "proposed_before", "before", "proposed_after", "after", "window_start", "window_end", "confidence", "uncertainty"}
        unknown = set(raw_changes) - allowed
        if unknown:
            raise MediaOperationsValidationError(f"unsupported learning proposal edit field: {sorted(unknown)[0]}")
        if not raw_changes:
            raise MediaOperationsValidationError("at least one learning proposal field is required")
        clean_reason = _required_text(reason, "reason", 4000)
        action_hash = sha256_json({"proposal_id": str(row.id), "changes": raw_changes, "reason": clean_reason})
        replay = self._history_replay(row, action="edit", idempotency_key=key, action_hash=action_hash)
        if replay is not None:
            return replay
        if row.status != "pending_review":
            raise MediaOperationsConflictError("only pending learning proposals can be edited")
        current = self._editable_payload(row)
        for key_name, value in raw_changes.items():
            target_name = {"decision_refs": "human_decision_refs", "target_character_fields": "target_fields", "before": "proposed_before", "after": "proposed_after"}.get(key_name, key_name)
            current[target_name] = value
        current["evidence_refs"] = _normalize_evidence(current["evidence_refs"], "evidence_refs", required=True)
        decisions = [_ref(item, "human_decision_ref") for item in (current.get("human_decision_refs") or [])]
        fields, diff_before, diff_after = _normalise_diff(current.get("target_fields"), current.get("proposed_before"), current.get("proposed_after"))
        start = _datetime(current["window_start"], "window_start")
        end = _datetime(current["window_end"], "window_end")
        assert start is not None and end is not None
        if start >= end:
            raise MediaOperationsValidationError("window_start must be before window_end")
        confidence = float(current["confidence"])
        uncertainty = float(current["uncertainty"])
        if not 0 <= confidence <= 1 or not 0 <= uncertainty <= 1:
            raise MediaOperationsValidationError("confidence and uncertainty must be between 0 and 1")
        payload = {"project_id": str(row.project_id) if row.project_id else None, "subject_type": row.subject_type, "subject_ref": row.subject_ref, "proposal_type": row.proposal_type, "title": _required_text(current["title"], "title", 255), "summary": _required_text(current["summary"], "summary", 4000), "recommendation": _required_text(current["recommendation"], "recommendation", 8000), "evidence_refs": current["evidence_refs"], "human_decision_refs": decisions, "target_fields": fields, "proposed_before": diff_before, "proposed_after": diff_after, "expected_persona_revision_id": str(row.expected_persona_revision_id) if getattr(row, "expected_persona_revision_id", None) else None, "expected_persona_revision_version": getattr(row, "expected_persona_revision_version", None), "expected_persona_revision_hash": getattr(row, "expected_persona_revision_hash", None), "window_start": start.isoformat(), "window_end": end.isoformat(), "confidence": confidence, "uncertainty": uncertainty, "human_review_required": True, "review_policy": row.review_policy, "status": "pending_review"}
        row.title, row.summary, row.recommendation = payload["title"], payload["summary"], payload["recommendation"]
        row.evidence_refs = current["evidence_refs"]
        row.human_decision_refs_json, row.target_fields_json = decisions, fields
        row.proposed_before_json, row.proposed_after_json = diff_before, diff_after
        row.window_start, row.window_end = start, end
        row.confidence, row.uncertainty = confidence, uncertainty
        row.proposal_hash = sha256_json(payload)
        result = row.to_safe_dict()
        self._append_history(row, action="edit", actor=actor, idempotency_key=key, reason=clean_reason, action_hash=action_hash, result=result, from_status="pending_review")
        await self._flush_commit(session)
        return row.to_safe_dict()

    async def reject_learning_proposal(self, session: Any | None = None, actor: Any | None = None, proposal_id: Any = None, *, reason: Any, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or proposal_id is None:
            raise MediaOperationsValidationError("actor and proposal_id are required")
        self._assert_human(actor)
        row = await self._get_proposal(session, proposal_id, for_update=True)
        await self._assert_proposal_access(session, actor, row, permission="write")
        clean_reason = _required_text(reason, "reason", 4000)
        key = _required_text(idempotency_key, "idempotency_key", 255)
        action_hash = sha256_json({"proposal_id": str(row.id), "reason": clean_reason})
        replay = self._history_replay(row, action="reject", idempotency_key=key, action_hash=action_hash)
        if replay is not None:
            return replay
        if row.status in {"accepted", "rejected", "stale"}:
            raise MediaOperationsConflictError("learning proposal is already terminal")
        from_status = row.status
        row.status = "rejected"
        result = row.to_safe_dict()
        self._append_history(row, action="reject", actor=actor, idempotency_key=key, reason=clean_reason, action_hash=action_hash, result=result, from_status=from_status)
        await self._flush_commit(session)
        return row.to_safe_dict()

    async def _apply_persona_revision(self, session: Any, actor: Any, row: LearningProposal, *, expected_id: UUID | None, expected_version: int | None, expected_hash: str | None, idempotency_key: str, reason: str | None, commit: bool) -> dict[str, Any]:
        target = await self._persona_revision_for_proposal(session, row, lock=True)
        if target is None:
            raise MediaOperationsValidationError("learning proposal has no UUID-backed Character target")
        persona, latest = target
        await self._assert_entity_access(session, actor, persona, permission="write")
        if row.project_id != persona.project_id or row.owner_user_id != persona.owner_user_id:
            raise MediaOperationsValidationError("proposal target Character is outside the proposal scope")
        expected_id, expected_version, expected_hash = await self._hydrate_expected_revision(session, row, expected_id=expected_id, expected_version=expected_version, expected_hash=expected_hash, current=latest)
        expected_id, expected_version, expected_hash = _revision_triple(expected_id, expected_version, expected_hash, required=True)
        assert expected_id is not None and expected_version is not None and expected_hash is not None
        action_hash = sha256_json({"proposal_id": str(row.id), "proposal_hash": row.proposal_hash, "expected_revision_id": str(expected_id), "expected_revision_version": expected_version, "expected_revision_hash": expected_hash, "proposed_after": row.proposed_after_json})
        replay = self._history_replay(row, action="apply", idempotency_key=idempotency_key, action_hash=action_hash)
        if replay is not None:
            return replay
        if row.applied_persona_revision_id is not None:
            result = row.to_safe_dict()
            self._append_history(row, action="apply", actor=actor, idempotency_key=idempotency_key, reason=reason, action_hash=action_hash, result=result, from_status=row.status)
            if commit:
                await self._flush_commit(session)
            return row.to_safe_dict()
        current_content = _revision_content_from_safe_dict(latest.to_safe_dict())
        if latest.id != expected_id or int(latest.version or 0) != expected_version or str(latest.content_hash or "").lower() != expected_hash:
            await self._mark_stale(session, row, actor=actor, idempotency_key=idempotency_key, reason="target Character revision is stale")
            raise MediaOperationsConflictError("stale learning proposal")
        before = dict(getattr(row, "proposed_before_json", None) or {})
        for field in getattr(row, "target_fields_json", None) or []:
            if field in before and current_content.get(field) != before[field]:
                await self._mark_stale(session, row, actor=actor, idempotency_key=idempotency_key, reason="proposal before snapshot is stale")
                raise MediaOperationsConflictError("stale learning proposal")
        merged = dict(current_content)
        merged.update(dict(getattr(row, "proposed_after_json", None) or {}))
        normalized = _normalize_revision_input(**merged)
        revision_key = f"learning:{row.id}:{idempotency_key}"[:255]
        existing_revision = await self._find_revision_by_idempotency(session, persona_id=persona.id, idempotency_key=revision_key)
        if existing_revision is not None:
            revision = existing_revision
        else:
            revision = self._build_revision(persona=persona, version=expected_version + 1, content=normalized, content_hash=sha256_json(normalized), created_by=_actor_id(actor), idempotency_key=revision_key)
            session.add(revision)
            try:
                await self._flush_only(session)
            except IntegrityError as exc:
                await self._rollback(session)
                recovered = await self._find_revision_by_idempotency(session, persona_id=persona.id, idempotency_key=revision_key)
                if recovered is None:
                    raise MediaOperationsConflictError("Character revision changed concurrently") from exc
                revision = recovered
        row.status = "accepted"
        row.expected_persona_revision_id, row.expected_persona_revision_version, row.expected_persona_revision_hash = expected_id, expected_version, expected_hash
        row.applied_persona_revision_id, row.applied_persona_revision_version, row.applied_persona_revision_hash = revision.id, int(revision.version), str(revision.content_hash).lower()
        result = row.to_safe_dict()
        self._append_history(row, action="apply", actor=actor, idempotency_key=idempotency_key, reason=reason, action_hash=action_hash, result=result, from_status="accepted")
        if commit:
            await self._flush_commit(session)
        return row.to_safe_dict()

    async def _mark_stale(self, session: Any, row: LearningProposal, *, actor: Any, idempotency_key: str, reason: str) -> None:
        if row.status != "stale":
            previous = row.status
            row.status = "stale"
            result = row.to_safe_dict()
            self._append_history(row, action="stale", actor=actor, idempotency_key=idempotency_key, reason=reason, result=result, from_status=previous)
            await self._flush_commit(session)

    async def approve_learning_proposal(self, session: Any | None = None, actor: Any | None = None, proposal_id: Any = None, *, expected_persona_revision_id: Any = None, expected_persona_revision_version: Any = None, expected_persona_revision_hash: Any = None, reason: Any = None, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or proposal_id is None:
            raise MediaOperationsValidationError("actor and proposal_id are required")
        self._assert_human(actor)
        row = await self._get_proposal(session, proposal_id, for_update=True)
        await self._assert_proposal_access(session, actor, row, permission="write")
        key = _required_text(idempotency_key, "idempotency_key", 255)
        clean_reason = _required_text(reason, "reason", 4000)
        expected = _revision_triple(expected_persona_revision_id, expected_persona_revision_version, expected_persona_revision_hash, required=False)
        action_hash = sha256_json({"proposal_id": str(row.id), "expected": [str(expected[0]) if expected[0] else None, expected[1], expected[2]], "reason": clean_reason})
        replay = self._history_replay(row, action="approve", idempotency_key=key, action_hash=action_hash)
        if replay is not None:
            return replay
        if row.status != "pending_review":
            if row.status == "accepted" and row.applied_persona_revision_id is not None:
                return row.to_safe_dict()
            raise MediaOperationsConflictError("only pending learning proposals can be approved")
        from_status = row.status
        target = await self._persona_revision_for_proposal(session, row, lock=True)
        if target is not None:
            result = await self._apply_persona_revision(session, actor, row, expected_id=expected[0], expected_version=expected[1], expected_hash=expected[2], idempotency_key=key, reason=clean_reason, commit=False)
        else:
            row.status = "accepted"
            result = row.to_safe_dict()
        self._append_history(row, action="approve", actor=actor, idempotency_key=key, reason=clean_reason, action_hash=action_hash, result=result, from_status=from_status)
        await self._flush_commit(session)
        return row.to_safe_dict()

    async def apply_learning_proposal(self, session: Any | None = None, actor: Any | None = None, proposal_id: Any = None, *, expected_persona_revision_id: Any = None, expected_persona_revision_version: Any = None, expected_persona_revision_hash: Any = None, reason: Any = None, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or proposal_id is None:
            raise MediaOperationsValidationError("actor and proposal_id are required")
        self._assert_human(actor)
        row = await self._get_proposal(session, proposal_id, for_update=True)
        await self._assert_proposal_access(session, actor, row, permission="write")
        if row.status != "accepted":
            raise MediaOperationsConflictError("learning proposal must be approved before apply")
        key = _required_text(idempotency_key, "idempotency_key", 255)
        clean_reason = _required_text(reason, "reason", 4000) if reason is not None else None
        expected = _revision_triple(expected_persona_revision_id, expected_persona_revision_version, expected_persona_revision_hash, required=False)
        return await self._apply_persona_revision(session, actor, row, expected_id=expected[0], expected_version=expected[1], expected_hash=expected[2], idempotency_key=key, reason=clean_reason, commit=True)

    approve_and_apply_learning_proposal = approve_learning_proposal


MediaOperationsInsightsService = MediaOperationsLearningService
MediaOperationsAgentService = MediaOperationsLearningService
MediaOperationsDirectAgentFacade = MediaOperationsLearningService


__all__ = [
    "MediaOperationsLearningService",
    "MediaOperationsInsightsService",
    "MediaOperationsAgentService",
    "MediaOperationsDirectAgentFacade",
]
