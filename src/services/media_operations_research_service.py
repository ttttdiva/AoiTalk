"""MediaOps WS3 service: research evidence and editorial trace.

This service performs no searching, provider calls, publication, scheduling,
content generation, credential access, or filesystem access.

ResearchRun is an immutable snapshot of one ResearchRoutine revision.
ResearchFinding and its evidence are inserted atomically and never updated.
Editorial ContentItem is an immutable planning record whose source Finding
links preserve research provenance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    RESEARCH_FINDING_KIND_VALUES,
    ContentItem,
    ContentItemFinding,
    EditorialProgram,
    EditorialProgramRevision,
    Persona,
    PersonaRevision,
    PlatformAccount,
    ResearchFinding,
    ResearchFindingEvidence,
    ResearchRoutine,
    ResearchRoutineRevision,
    ResearchRun,
    ResearchCandidate,
)
from .media_operations_service import (
    MediaOperationsAuthorizationError,
    MediaOperationsConflictError,
    MediaOperationsNotFoundError,
    MediaOperationsValidationError,
    _actor_id,
    _as_uuid,
    _bounded_page,
    _idempotency_key,
    _normalize_platforms,
    _optional_text,
    _required_text,
    _validated_resource_url,
    _validated_sha256,
    sha256_json,
)
from .media_operations_setup_service import MediaOperationsSetupService


_PLATFORM_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("x", "platform_x"),
    ("pixiv", "platform_pixiv"),
    ("dlsite", "platform_dlsite"),
    ("patreon", "platform_patreon"),
    ("youtube", "platform_youtube"),
    ("instagram", "platform_instagram"),
)


def _normalize_questions(values: Any) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError("questions must be a list")
    if len(values) < 1 or len(values) > 20:
        raise MediaOperationsValidationError(
            "questions must contain between 1 and 20 items"
        )

    result: list[str] = []
    for raw in values:
        value = _required_text(raw, "question", 500)
        if value not in result:
            result.append(value)

    if not result:
        raise MediaOperationsValidationError(
            "questions must contain at least one non-empty item"
        )
    return result


def _normalize_routine_revision(
    *,
    name: Any,
    objective: Any,
    questions: Any,
    target_platforms: Any = None,
    cadence: Any = "manual",
    timezone: Any = "UTC",
    schedule: Any = None,
    source_types: Any = None,
    search_queries: Any = None,
    domains: Any = None,
    follow_accounts: Any = None,
    follow_tags: Any = None,
    exclusions: Any = None,
    freshness_hours: Any = 168,
    max_candidates: Any = 20,
    review_policy: Any = "human_review",
) -> dict[str, Any]:
    cadence_value = _normalize_cadence(cadence)
    timezone_value = _normalize_timezone(timezone)
    return {
        "name": _required_text(name, "name", 255),
        "objective": _required_text(objective, "objective", 4000),
        "questions": _normalize_questions(questions),
        "target_platforms": _normalize_platforms(
            target_platforms or []
        ),
        "cadence": cadence_value,
        "timezone": timezone_value,
        "schedule": _normalize_schedule(schedule),
        "source_types": _normalize_text_list(source_types, "source_types", 12, 64),
        "search_queries": _normalize_text_list(search_queries, "search_queries", 20, 500),
        "domains": _normalize_text_list(domains, "domains", 50, 255),
        "follow_accounts": _normalize_text_list(follow_accounts, "follow_accounts", 50, 164),
        "follow_tags": _normalize_text_list(follow_tags, "follow_tags", 50, 120),
        "exclusions": _normalize_text_list(exclusions, "exclusions", 50, 255),
        "freshness_hours": _bounded_int(
            168 if freshness_hours is None else freshness_hours,
            "freshness_hours",
            1,
            8760,
        ),
        "max_candidates": _bounded_int(
            20 if max_candidates is None else max_candidates,
            "max_candidates",
            1,
            500,
        ),
        "review_policy": _required_text(review_policy or "human_review", "review_policy", 64),
    }


_CADENCE_VALUES = frozenset({"manual", "hourly", "daily", "weekly", "monthly"})


def _normalize_cadence(value: Any) -> str:
    rendered = str(getattr(value, "value", value) or "manual").strip().lower()
    if rendered not in _CADENCE_VALUES:
        raise MediaOperationsValidationError(
            "cadence must be manual, hourly, daily, weekly, or monthly"
        )
    return rendered


def _normalize_timezone(value: Any) -> str:
    rendered = _required_text(value or "UTC", "timezone", 64)
    if any(character.isspace() for character in rendered):
        raise MediaOperationsValidationError("timezone must be an IANA timezone name")
    try:
        ZoneInfo(rendered)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise MediaOperationsValidationError(
            "timezone must be an IANA timezone name"
        ) from exc
    return rendered


def _normalize_text_list(value: Any, label: str, maximum: int, item_length: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(value) > maximum:
        raise MediaOperationsValidationError(f"{label} exceeds {maximum} items")
    result: list[str] = []
    for raw in value:
        rendered = _required_text(raw, label[:-1] if label.endswith("s") else label, item_length)
        if rendered not in result:
            result.append(rendered)
    return result


def _normalize_run_refs(value: Any, label: str) -> list[str]:
    refs = _normalize_text_list(value, label, 100, 4000)
    normalized: list[str] = []
    for ref in refs:
        if "\\" in ref or any(ord(character) < 0x20 for character in ref):
            raise MediaOperationsValidationError(f"{label} contains an unsafe reference")
        if ref.lower().startswith(("http://", "https://")):
            ref = _validated_resource_url(ref)
        normalized.append(ref)
    return normalized


def _normalize_run_status(value: Any) -> str:
    rendered = str(getattr(value, "value", value) or "recorded").strip().lower()
    allowed = {"queued", "running", "recorded", "partial", "succeeded", "failed"}
    if rendered not in allowed:
        raise MediaOperationsValidationError("research run status is invalid")
    return rendered


def _normalize_schedule(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise MediaOperationsValidationError("schedule must be an object")
    allowed = {"hour", "minute", "weekdays", "day_of_month"}
    if set(value) - allowed:
        raise MediaOperationsValidationError("schedule contains unknown fields")
    result: dict[str, Any] = {}
    for key in ("hour", "minute", "day_of_month"):
        if key in value:
            result[key] = _bounded_int(value[key], f"schedule.{key}", 0 if key != "day_of_month" else 1, 23 if key == "hour" else 59 if key == "minute" else 31)
    if "weekdays" in value:
        weekdays = _normalize_text_list(value["weekdays"], "weekdays", 7, 9)
        allowed_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        if any(day.lower() not in allowed_days for day in weekdays):
            raise MediaOperationsValidationError("schedule.weekdays contains an invalid day")
        result["weekdays"] = [day.lower() for day in weekdays]
    return result


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MediaOperationsValidationError(f"{label} must be an integer")
    try:
        rendered = int(value)
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError(f"{label} must be an integer") from exc
    if rendered < minimum or rendered > maximum:
        raise MediaOperationsValidationError(f"{label} must be between {minimum} and {maximum}")
    return rendered


def _normalize_state(value: Any, label: str = "state") -> str:
    rendered = str(getattr(value, "value", value) or "draft").strip().lower()
    if rendered not in {"draft", "active", "paused", "archived"}:
        raise MediaOperationsValidationError(f"{label} is invalid")
    return rendered


def _next_due(cadence: str, *, now: datetime | None = None) -> datetime | None:
    if cadence == "manual":
        return None
    current = now or datetime.utcnow()
    if cadence == "hourly":
        return current + timedelta(hours=1)
    if cadence == "daily":
        return current + timedelta(days=1)
    if cadence == "weekly":
        return current + timedelta(days=7)
    return current + timedelta(days=30)


def _finding_kind(value: Any) -> str:
    rendered = str(getattr(value, "value", value)).strip().lower()
    if rendered not in RESEARCH_FINDING_KIND_VALUES:
        raise MediaOperationsValidationError(
            "kind must be fact, signal, or hypothesis"
        )
    return rendered


def _normalize_evidence(values: Any) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError("evidence must be a list")
    if len(values) < 1 or len(values) > 20:
        raise MediaOperationsValidationError(
            "evidence must contain between 1 and 20 items"
        )

    result: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for index, raw in enumerate(values, start=1):
        if not isinstance(raw, Mapping):
            raise MediaOperationsValidationError(
                "each evidence item must be a typed object"
            )

        payload = {str(key): value for key, value in raw.items()}
        evidence_type = str(payload.get("type") or "").strip().lower()
        label = _optional_text(payload.get("label"), "evidence.label", 255)
        note = _optional_text(payload.get("note"), "evidence.note", 2000)

        if evidence_type == "url":
            allowed = {"type", "url", "label", "note"}
            if set(payload) != allowed:
                raise MediaOperationsValidationError(
                    "url evidence accepts only type, url, label, and note"
                )
            source_url = _validated_resource_url(payload.get("url"))
            normalized = {
                "ordinal": index,
                "evidence_type": "url",
                "label": label,
                "note": note,
                "source_url": source_url,
                "artifact_sha256": None,
                "artifact_mime_type": None,
                "provenance": {
                    "type": "url",
                    "url": source_url,
                },
            }
        elif evidence_type == "artifact":
            allowed = {
                "type",
                "sha256",
                "mime_type",
                "label",
                "note",
            }
            if set(payload) != allowed:
                raise MediaOperationsValidationError(
                    "artifact evidence accepts only "
                    "type, sha256, mime_type, label, and note"
                )
            artifact_sha256 = _validated_sha256(
                payload.get("sha256"),
                "evidence.sha256",
            )
            mime_type = _required_text(
                payload.get("mime_type"),
                "evidence.mime_type",
                255,
            )
            normalized = {
                "ordinal": index,
                "evidence_type": "artifact",
                "label": label,
                "note": note,
                "source_url": None,
                "artifact_sha256": artifact_sha256,
                "artifact_mime_type": mime_type,
                "provenance": {
                    "type": "artifact",
                    "sha256": artifact_sha256,
                    "mime_type": mime_type,
                },
            }
        else:
            raise MediaOperationsValidationError(
                "evidence.type must be url or artifact"
            )

        evidence_hash = sha256_json(
            {
                "label": normalized["label"],
                "note": normalized["note"],
                "provenance": normalized["provenance"],
            }
        )
        if evidence_hash in seen_hashes:
            raise MediaOperationsValidationError(
                "duplicate evidence is not allowed"
            )
        seen_hashes.add(evidence_hash)
        normalized["evidence_hash"] = evidence_hash
        result.append(normalized)

    return result


def _normalize_program_revision(
    *,
    name: Any,
    objective: Any,
    content_type: Any = "article",
    cadence: Any = "manual",
    target_platforms: Any = None,
    target_account_refs: Any = None,
    content_pillar: Any = None,
    required_resources: Any = None,
    default_creative_recipe_ref: Any = None,
    default_qa_policy_ref: Any = None,
    experiment_ref: Any = None,
    draft_generation_policy: Any = "human_review",
) -> dict[str, Any]:
    return {
        "name": _required_text(name, "name", 255),
        "objective": _required_text(objective, "objective", 4000),
        "content_type": _required_text(content_type or "article", "content_type", 64),
        "cadence": _normalize_cadence(cadence),
        "target_platforms": _normalize_platforms(target_platforms or []),
        "target_account_refs": _normalize_text_list(target_account_refs, "target_account_refs", 32, 164),
        "content_pillar": _optional_text(content_pillar, "content_pillar", 200),
        "required_resources": _normalize_text_list(required_resources, "required_resources", 32, 164),
        "default_creative_recipe_ref": _optional_text(default_creative_recipe_ref, "default_creative_recipe_ref", 164),
        "default_qa_policy_ref": _optional_text(default_qa_policy_ref, "default_qa_policy_ref", 164),
        "experiment_ref": _optional_text(experiment_ref, "experiment_ref", 164),
        "draft_generation_policy": _required_text(draft_generation_policy or "human_review", "draft_generation_policy", 64),
    }


def _normalize_finding_ids(values: Any) -> list[UUID]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError("finding_ids must be a list")
    if len(values) < 1 or len(values) > 20:
        raise MediaOperationsValidationError(
            "finding_ids must contain between 1 and 20 items"
        )

    result: list[UUID] = []
    for raw in values:
        parsed = _as_uuid(raw, "finding_id")
        assert parsed is not None
        if parsed not in result:
            result.append(parsed)

    if not result:
        raise MediaOperationsValidationError(
            "finding_ids must contain at least one unique item"
        )
    return result


def _normalize_optional_ids(values: Any, label: str, *, maximum: int = 20) -> list[UUID]:
    """Normalize bounded UUID references while allowing an empty collection."""

    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(values) > maximum:
        raise MediaOperationsValidationError(f"{label} exceeds {maximum} items")
    result: list[UUID] = []
    for raw in values:
        parsed = _as_uuid(raw, label[:-1] if label.endswith("s") else label)
        assert parsed is not None
        if parsed not in result:
            result.append(parsed)
    return result


def _normalize_candidate_key(value: Any) -> str:
    rendered = _required_text(value, "candidate_key", 64)
    if any(character.isspace() for character in rendered):
        raise MediaOperationsValidationError("candidate_key must not contain whitespace")
    return rendered


def _candidate_status(value: Any) -> str:
    rendered = str(getattr(value, "value", value) or "discovered").strip().lower()
    if rendered not in {"discovered", "triaged", "accepted", "rejected", "expired", "promoted"}:
        raise MediaOperationsValidationError("candidate status is invalid")
    return rendered


def _bounded_score(value: Any, label: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise MediaOperationsValidationError(f"{label} must be a number")
    try:
        rendered = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError(f"{label} must be a number") from exc
    if rendered != rendered or rendered in (float("inf"), float("-inf")) or not 0 <= rendered <= 1:
        raise MediaOperationsValidationError(f"{label} must be between 0 and 1")
    return rendered


def _parse_datetime(value: Any, label: str, *, required: bool = False) -> datetime | None:
    if value in (None, ""):
        if required:
            raise MediaOperationsValidationError(f"{label} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise MediaOperationsValidationError(f"{label} must be an ISO datetime") from exc
    else:
        raise MediaOperationsValidationError(f"{label} must be a datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _is_human_actor(actor: Any) -> bool:
    if isinstance(actor, Mapping):
        actor_type = actor.get("actor_type")
    else:
        actor_type = getattr(actor, "actor_type", None)
    # Fail closed: only an explicit human marker may make a review decision.
    # ``admin`` is an explicit human-operated privileged session in the
    # decision model; automated/system/agent actors remain ineligible.
    return str(actor_type or "").strip().lower() in {"human", "admin"}


def _actor_type(actor: Any, *, default: str = "system") -> str:
    """Return a bounded audit actor type, failing closed for unknown values."""

    if isinstance(actor, Mapping):
        value = actor.get("actor_type")
    else:
        value = getattr(actor, "actor_type", None)
    rendered = str(value or default).strip().lower()
    if rendered not in {"human", "agent", "system", "admin", "unknown"}:
        rendered = "unknown"
    return rendered or default


class MediaOperationsResearchService(MediaOperationsSetupService):
    """WS3 Research/Editorial service using WS1/WS2 ACL/session primitives."""

    async def _get_row(
        self,
        session: Any,
        model: Any,
        entity_id: UUID | str,
        label: str,
        *,
        for_update: bool = False,
    ) -> Any:
        parsed = _as_uuid(entity_id, label)
        assert parsed is not None

        statement = select(model).where(model.id == parsed).limit(1)
        if for_update:
            statement = statement.with_for_update()

        row = await self._scalar(session, statement)
        if row is None:
            raise MediaOperationsNotFoundError(f"{label} not found")
        return row

    async def _find_scoped_idempotency(
        self,
        session: Any,
        model: Any,
        *,
        owner_user_id: UUID,
        project_id: UUID | None,
        idempotency_key: str,
    ) -> Any:
        conditions = [model.idempotency_key == idempotency_key]

        if project_id is None:
            conditions.extend(
                [
                    model.project_id.is_(None),
                    model.owner_user_id == owner_user_id,
                ]
            )
        else:
            conditions.append(model.project_id == project_id)

        return await self._scalar(
            session,
            select(model).where(*conditions).limit(1),
        )

    def _build_routine_revision(
        self,
        *,
        routine: ResearchRoutine,
        version: int,
        content: Mapping[str, Any],
        actor_id: UUID,
        idempotency_key: str | None,
    ) -> ResearchRoutineRevision:
        selected = set(content["target_platforms"])

        return ResearchRoutineRevision(
            id=uuid4(),
            research_routine_id=routine.id,
            owner_user_id=routine.owner_user_id,
            project_id=routine.project_id,
            version=version,
            name=content["name"],
            objective=content["objective"],
            questions_json=list(content["questions"]),
            platform_x="x" in selected,
            platform_pixiv="pixiv" in selected,
            platform_dlsite="dlsite" in selected,
            platform_patreon="patreon" in selected,
            platform_youtube="youtube" in selected,
            platform_instagram="instagram" in selected,
            cadence=content["cadence"],
            timezone=content["timezone"],
            schedule_json=dict(content["schedule"]),
            source_types_json=list(content["source_types"]),
            search_queries_json=list(content["search_queries"]),
            domains_json=list(content["domains"]),
            follow_accounts_json=list(content["follow_accounts"]),
            follow_tags_json=list(content["follow_tags"]),
            exclusions_json=list(content["exclusions"]),
            freshness_hours=content["freshness_hours"],
            max_candidates=content["max_candidates"],
            review_policy=content["review_policy"],
            content_hash=sha256_json(content),
            idempotency_key=idempotency_key,
            created_by=actor_id,
        )

    async def _routine_detail(
        self,
        session: Any,
        routine: ResearchRoutine,
    ) -> dict[str, Any]:
        revisions = await self._scalars(
            session,
            select(ResearchRoutineRevision)
            .where(
                ResearchRoutineRevision.research_routine_id
                == routine.id
            )
            .order_by(
                ResearchRoutineRevision.version.desc(),
                ResearchRoutineRevision.id.desc(),
            )
            .limit(101),
        )

        if not revisions:
            raise MediaOperationsConflictError(
                "ResearchRoutine revision history is incomplete"
            )

        visible = revisions[:100]
        return {
            **routine.to_safe_dict(),
            "current_revision": visible[0].to_safe_dict(),
            "revisions": [
                revision.to_safe_dict()
                for revision in visible
            ],
            "revision_history_truncated": len(revisions) > 100,
        }

    async def _routine_summary_map(
        self,
        session: Any,
        routines: Sequence[ResearchRoutine],
    ) -> dict[UUID, dict[str, Any]]:
        if not routines:
            return {}

        ids = [routine.id for routine in routines]
        revisions = await self._scalars(
            session,
            select(ResearchRoutineRevision)
            .where(
                ResearchRoutineRevision.research_routine_id.in_(ids)
            )
            .order_by(
                ResearchRoutineRevision.research_routine_id.asc(),
                ResearchRoutineRevision.version.desc(),
            ),
        )

        latest: dict[UUID, ResearchRoutineRevision] = {}
        for revision in revisions:
            latest.setdefault(revision.research_routine_id, revision)

        result: dict[UUID, dict[str, Any]] = {}
        for routine in routines:
            revision = latest.get(routine.id)
            if revision is None:
                raise MediaOperationsConflictError(
                    "ResearchRoutine revision history is incomplete"
                )
            result[routine.id] = {
                **routine.to_safe_dict(),
                "current_revision": revision.to_safe_dict(),
            }
        return result

    async def create_research_routine(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        name: Any,
        objective: Any,
        questions: Any,
        target_platforms: Any = None,
        cadence: Any = "manual",
        timezone: Any = "UTC",
        schedule: Any = None,
        source_types: Any = None,
        search_queries: Any = None,
        domains: Any = None,
        follow_accounts: Any = None,
        follow_tags: Any = None,
        exclusions: Any = None,
        freshness_hours: Any = 168,
        max_candidates: Any = 20,
        review_policy: Any = "human_review",
        state: Any = "draft",
        enabled: Any = False,
        persona_id: UUID | str | None = None,
        platform_account_id: UUID | str | None = None,
        project_id: UUID | str | None = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        persona_uuid = _as_uuid(persona_id, "persona_id", required=False)
        persona = None
        if persona_uuid is not None:
            persona = await self._get_row(session, Persona, persona_uuid, "persona_id")
            await self._assert_entity_access(session, actor, persona, permission="write")
            if project_uuid is not None and persona.project_id != project_uuid:
                raise MediaOperationsValidationError(
                    "persona and ResearchRoutine must share project scope"
                )
            project_uuid = persona.project_id
        actor_id = await self._assert_create_scope(
            session,
            actor,
            project_uuid,
        )
        key = _idempotency_key(idempotency_key)
        content = _normalize_routine_revision(
            name=name,
            objective=objective,
            questions=questions,
            target_platforms=target_platforms,
            cadence=cadence,
            timezone=timezone,
            schedule=schedule,
            source_types=source_types,
            search_queries=search_queries,
            domains=domains,
            follow_accounts=follow_accounts,
            follow_tags=follow_tags,
            exclusions=exclusions,
            freshness_hours=freshness_hours,
            max_candidates=max_candidates,
            review_policy=review_policy,
        )
        account_uuid = _as_uuid(platform_account_id, "platform_account_id", required=False)
        if account_uuid is not None:
            account = await self._get_row(session, PlatformAccount, account_uuid, "platform_account_id")
            await self._assert_entity_access(session, actor, account, permission="read")
            if project_uuid is None and account.project_id is not None:
                project_uuid = account.project_id
                actor_id = await self._assert_create_scope(session, actor, project_uuid)
            if project_uuid is not None and account.project_id != project_uuid:
                raise MediaOperationsValidationError(
                    "platform account and ResearchRoutine must share project scope"
                )
            if persona_uuid is None and getattr(account, "persona_id", None) is not None:
                persona_uuid = account.persona_id
            if persona_uuid is not None and getattr(account, "persona_id", None) not in (None, persona_uuid):
                raise MediaOperationsValidationError(
                    "platform account is linked to a different Persona"
                )
        if persona_uuid is not None and persona is None:
            persona = await self._get_row(session, Persona, persona_uuid, "persona_id")
            await self._assert_entity_access(session, actor, persona, permission="write")
            if project_uuid is not None and persona.project_id != project_uuid:
                raise MediaOperationsValidationError(
                    "persona and ResearchRoutine must share project scope"
                )
            project_uuid = persona.project_id
            actor_id = await self._assert_create_scope(session, actor, project_uuid)
        create_hash = sha256_json(
            {
                "project_id": (
                    str(project_uuid)
                    if project_uuid is not None
                    else None
                ),
                "persona_id": str(persona_uuid) if persona_uuid is not None else None,
                "platform_account_id": str(account_uuid) if account_uuid is not None else None,
                "revision": content,
            }
        )

        existing = await self._find_scoped_idempotency(
            session,
            ResearchRoutine,
            owner_user_id=actor_id,
            project_id=project_uuid,
            idempotency_key=key,
        )
        if existing is not None:
            if existing.create_hash != create_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different ResearchRoutine payload"
                )
            return await self._routine_detail(session, existing)

        routine_state = _normalize_state(state)
        if enabled is None:
            enabled = False
        if not isinstance(enabled, bool):
            raise MediaOperationsValidationError("enabled must be a boolean")
        routine = ResearchRoutine(
            id=uuid4(),
            owner_user_id=actor_id,
            project_id=project_uuid,
            persona_id=persona_uuid,
            state=routine_state,
            enabled=enabled,
            platform_account_id=account_uuid,
            next_due_at=_next_due(content["cadence"]) if enabled and routine_state == "active" else None,
            create_hash=create_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        revision = self._build_routine_revision(
            routine=routine,
            version=1,
            content=content,
            actor_id=actor_id,
            idempotency_key=None,
        )

        session.add(routine)
        session.add(revision)

        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(
                session,
                ResearchRoutine,
                owner_user_id=actor_id,
                project_id=project_uuid,
                idempotency_key=key,
            )
            if recovered is not None and recovered.create_hash == create_hash:
                return await self._routine_detail(session, recovered)
            raise MediaOperationsConflictError(
                "ResearchRoutine conflicts with an existing record"
            ) from exc

        return await self._routine_detail(session, routine)

    async def list_research_routines(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        page_limit, page_offset = _bounded_page(limit, offset)

        condition = await self._scope_condition(
            session,
            actor,
            ResearchRoutine,
            project_id=project_uuid,
        )

        routines = await self._scalars(
            session,
            select(ResearchRoutine)
            .where(condition)
            .order_by(
                ResearchRoutine.created_at.desc(),
                ResearchRoutine.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset),
        )

        summaries = await self._routine_summary_map(session, routines)
        return [summaries[routine.id] for routine in routines]

    async def list_due_research_routines(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        as_of: Any = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return enabled active routines whose durable due marker has elapsed.

        This is an inspection/claim boundary only.  Starting a run still
        requires an idempotency key derived by the scheduler from the routine
        revision and due window, so a restart cannot duplicate a run.
        """

        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        cutoff = _parse_datetime(as_of, "as_of") or datetime.utcnow()
        page_limit, _ = _bounded_page(limit, 0)
        condition = await self._scope_condition(
            session,
            actor,
            ResearchRoutine,
            project_id=project_uuid,
        )
        rows = await self._scalars(
            session,
            select(ResearchRoutine)
            .where(
                condition,
                ResearchRoutine.enabled.is_(True),
                ResearchRoutine.state == "active",
                ResearchRoutine.next_due_at.is_not(None),
                ResearchRoutine.next_due_at <= cutoff,
            )
            .order_by(ResearchRoutine.next_due_at.asc(), ResearchRoutine.id.asc())
            .limit(page_limit),
        )
        summaries = await self._routine_summary_map(session, rows)
        return [summaries[row.id] for row in rows]

    async def get_research_routine(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        routine_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or routine_id is None:
            raise MediaOperationsValidationError(
                "actor and research_routine_id are required"
            )

        routine = await self._get_row(
            session,
            ResearchRoutine,
            routine_id,
            "research_routine_id",
        )
        await self._assert_entity_access(
            session,
            actor,
            routine,
            permission="read",
        )
        return await self._routine_detail(session, routine)

    async def append_research_routine_revision(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        routine_id: UUID | str | None = None,
        *,
        expected_version: Any,
        name: Any,
        objective: Any,
        questions: Any,
        target_platforms: Any = None,
        cadence: Any = None,
        timezone: Any = None,
        schedule: Any = None,
        source_types: Any = None,
        search_queries: Any = None,
        domains: Any = None,
        follow_accounts: Any = None,
        follow_tags: Any = None,
        exclusions: Any = None,
        freshness_hours: Any = None,
        max_candidates: Any = None,
        review_policy: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or routine_id is None:
            raise MediaOperationsValidationError(
                "actor and research_routine_id are required"
            )

        routine = await self._get_row(
            session,
            ResearchRoutine,
            routine_id,
            "research_routine_id",
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            routine,
            permission="write",
        )
        key = _idempotency_key(idempotency_key)
        latest = await self._scalar(
            session,
            select(ResearchRoutineRevision)
            .where(ResearchRoutineRevision.research_routine_id == routine.id)
            .order_by(
                ResearchRoutineRevision.version.desc(),
                ResearchRoutineRevision.id.desc(),
            )
            .limit(1),
        )
        if latest is None:
            raise MediaOperationsConflictError(
                "ResearchRoutine revision history is incomplete"
            )
        content = _normalize_routine_revision(
            name=name,
            objective=objective,
            questions=questions,
            target_platforms=(target_platforms if target_platforms is not None else getattr(latest, "target_platforms_json", [])),
            cadence=(cadence if cadence is not None else getattr(latest, "cadence", "manual")),
            timezone=(timezone if timezone is not None else getattr(latest, "timezone", "UTC")),
            schedule=(schedule if schedule is not None else getattr(latest, "schedule_json", {})),
            source_types=(source_types if source_types is not None else getattr(latest, "source_types_json", [])),
            search_queries=(search_queries if search_queries is not None else getattr(latest, "search_queries_json", [])),
            domains=(domains if domains is not None else getattr(latest, "domains_json", [])),
            follow_accounts=(follow_accounts if follow_accounts is not None else getattr(latest, "follow_accounts_json", [])),
            follow_tags=(follow_tags if follow_tags is not None else getattr(latest, "follow_tags_json", [])),
            exclusions=(exclusions if exclusions is not None else getattr(latest, "exclusions_json", [])),
            freshness_hours=(freshness_hours if freshness_hours is not None else getattr(latest, "freshness_hours", 168)),
            max_candidates=(max_candidates if max_candidates is not None else getattr(latest, "max_candidates", 20)),
            review_policy=(review_policy if review_policy is not None else getattr(latest, "review_policy", "human_review")),
        )
        content_hash = sha256_json(content)

        existing = await self._scalar(
            session,
            select(ResearchRoutineRevision)
            .where(
                ResearchRoutineRevision.research_routine_id == routine.id,
                ResearchRoutineRevision.idempotency_key == key,
            )
            .limit(1),
        )
        if existing is not None:
            if existing.content_hash != content_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with different ResearchRoutine revision content"
                )
            return existing.to_safe_dict()

        current = int(
            await self._scalar(
                session,
                select(func.max(ResearchRoutineRevision.version)).where(
                    ResearchRoutineRevision.research_routine_id
                    == routine.id
                ),
            )
            or 0
        )
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "expected_version must be an integer"
            ) from exc

        if expected != current:
            raise MediaOperationsConflictError(
                "stale ResearchRoutine version"
            )

        revision = self._build_routine_revision(
            routine=routine,
            version=current + 1,
            content=content,
            actor_id=actor_id,
            idempotency_key=key,
        )
        if bool(getattr(routine, "enabled", False)) and getattr(routine, "state", "draft") == "active":
            routine.next_due_at = _next_due(content["cadence"])
        session.add(revision)

        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(
                session,
                select(ResearchRoutineRevision)
                .where(
                    ResearchRoutineRevision.research_routine_id
                    == routine.id,
                    ResearchRoutineRevision.idempotency_key == key,
                )
                .limit(1),
            )
            if recovered is not None and recovered.content_hash == content_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError(
                "ResearchRoutine revision changed concurrently"
            ) from exc

        return revision.to_safe_dict()

    async def _exact_routine_revision(
        self,
        session: Any,
        routine: ResearchRoutine,
        version: Any,
    ) -> ResearchRoutineRevision:
        try:
            parsed_version = int(version)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "routine_version must be an integer"
            ) from exc
        if parsed_version < 1:
            raise MediaOperationsValidationError(
                "routine_version must be positive"
            )

        revision = await self._scalar(
            session,
            select(ResearchRoutineRevision)
            .where(
                ResearchRoutineRevision.research_routine_id == routine.id,
                ResearchRoutineRevision.version == parsed_version,
            )
            .limit(1),
        )
        if revision is None:
            raise MediaOperationsNotFoundError(
                "ResearchRoutine revision not found"
            )
        return revision

    async def _run_detail(
        self,
        session: Any,
        actor: Any,
        run: ResearchRun,
    ) -> dict[str, Any]:
        routine_revision = await self._scalar(
            session,
            select(ResearchRoutineRevision)
            .where(
                ResearchRoutineRevision.id
                == run.research_routine_revision_id
            )
            .limit(1),
        )
        if routine_revision is None:
            raise MediaOperationsConflictError(
                "ResearchRun routine snapshot is incomplete"
            )

        findings = await self.list_research_findings(
            session,
            actor,
            run.id,
        )

        return {
            **run.to_safe_dict(),
            "routine_revision": routine_revision.to_safe_dict(),
            "findings": findings,
        }

    async def start_research_run(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        research_routine_id: UUID | str,
        routine_version: Any,
        focus_note: Any = None,
        source_refs: Any = None,
        omissions: Any = None,
        status: Any = "recorded",
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        routine = await self._get_row(
            session,
            ResearchRoutine,
            research_routine_id,
            "research_routine_id",
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            routine,
            permission="write",
        )
        revision = await self._exact_routine_revision(
            session,
            routine,
            routine_version,
        )
        key = _idempotency_key(idempotency_key)
        focus = _optional_text(focus_note, "focus_note", 4000)
        status_value = _normalize_run_status(status)
        source_refs_value = _normalize_run_refs(source_refs, "source_refs")
        omissions_value = _normalize_run_refs(omissions, "omissions")

        run_hash = sha256_json(
            {
                "research_routine_id": str(routine.id),
                "research_routine_revision_id": str(revision.id),
                "routine_content_hash": revision.content_hash,
                "focus_note": focus,
                "source_refs": source_refs_value,
                "omissions": omissions_value,
                "status": status_value,
            }
        )
        now = datetime.utcnow()

        existing = await self._scalar(
            session,
            select(ResearchRun)
            .where(
                ResearchRun.research_routine_id == routine.id,
                ResearchRun.idempotency_key == key,
            )
            .limit(1),
        )
        if existing is not None:
            if existing.run_hash != run_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different ResearchRun payload"
                )
            await self._assert_entity_access(
                session,
                actor,
                existing,
                permission="read",
            )
            return await self._run_detail(session, actor, existing)

        run = ResearchRun(
            id=uuid4(),
            owner_user_id=actor_id,
            project_id=routine.project_id,
            research_routine_id=routine.id,
            research_routine_revision_id=revision.id,
            routine_content_hash=revision.content_hash,
            focus_note=focus,
            status=status_value,
            started_at=now,
            finished_at=now if status_value not in {"queued", "running"} else None,
            source_refs_json=source_refs_value,
            omissions_json=omissions_value,
            run_hash=run_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        routine.last_due_at = now
        if bool(getattr(routine, "enabled", False)) and getattr(routine, "state", "draft") == "active":
            routine.next_due_at = _next_due(getattr(revision, "cadence", "manual"), now=now)
        session.add(run)

        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(
                session,
                select(ResearchRun)
                .where(
                    ResearchRun.research_routine_id == routine.id,
                    ResearchRun.idempotency_key == key,
                )
                .limit(1),
            )
            if recovered is not None and recovered.run_hash == run_hash:
                return await self._run_detail(session, actor, recovered)
            raise MediaOperationsConflictError(
                "ResearchRun conflicts with an existing record"
            ) from exc

        return await self._run_detail(session, actor, run)

    async def list_research_runs(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        research_routine_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        page_limit, page_offset = _bounded_page(limit, offset)
        conditions = [
            await self._scope_condition(
                session,
                actor,
                ResearchRun,
                project_id=project_uuid,
            )
        ]

        if research_routine_id is not None:
            routine = await self._get_row(
                session,
                ResearchRoutine,
                research_routine_id,
                "research_routine_id",
            )
            await self._assert_entity_access(
                session,
                actor,
                routine,
                permission="read",
            )
            conditions.append(
                ResearchRun.research_routine_id == routine.id
            )

        rows = await self._scalars(
            session,
            select(ResearchRun)
            .where(*conditions)
            .order_by(
                ResearchRun.created_at.desc(),
                ResearchRun.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset),
        )
        return [row.to_safe_dict() for row in rows]

    async def get_research_run(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        run_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or run_id is None:
            raise MediaOperationsValidationError(
                "actor and research_run_id are required"
            )

        run = await self._get_row(
            session,
            ResearchRun,
            run_id,
            "research_run_id",
        )
        await self._assert_entity_access(
            session,
            actor,
            run,
            permission="read",
        )
        return await self._run_detail(session, actor, run)

    async def _finding_detail(
        self,
        session: Any,
        finding: ResearchFinding,
    ) -> dict[str, Any]:
        evidence = await self._scalars(
            session,
            select(ResearchFindingEvidence)
            .where(
                ResearchFindingEvidence.finding_id == finding.id
            )
            .order_by(
                ResearchFindingEvidence.ordinal.asc(),
            ),
        )
        if not evidence:
            raise MediaOperationsConflictError(
                "ResearchFinding evidence is incomplete"
            )

        return {
            **finding.to_safe_dict(),
            "evidence": [
                item.to_safe_dict()
                for item in evidence
            ],
        }

    async def list_research_findings(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        run_id: UUID | str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None or run_id is None:
            raise MediaOperationsValidationError(
                "actor and research_run_id are required"
            )

        run = await self._get_row(
            session,
            ResearchRun,
            run_id,
            "research_run_id",
        )
        await self._assert_entity_access(
            session,
            actor,
            run,
            permission="read",
        )
        page_limit, page_offset = _bounded_page(limit, offset)

        findings = await self._scalars(
            session,
            select(ResearchFinding)
            .where(
                ResearchFinding.research_run_id == run.id
            )
            .order_by(
                ResearchFinding.created_at.desc(),
                ResearchFinding.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset),
        )

        return [
            await self._finding_detail(session, finding)
            for finding in findings
        ]

    async def append_research_finding(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        run_id: UUID | str | None = None,
        *,
        kind: Any,
        statement: Any,
        evidence: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or run_id is None:
            raise MediaOperationsValidationError(
                "actor and research_run_id are required"
            )

        run = await self._get_row(
            session,
            ResearchRun,
            run_id,
            "research_run_id",
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            run,
            permission="write",
        )
        key = _idempotency_key(idempotency_key)
        kind_value = _finding_kind(kind)
        statement_value = _required_text(
            statement,
            "statement",
            8000,
        )
        normalized_evidence = _normalize_evidence(evidence)

        finding_hash = sha256_json(
            {
                "kind": kind_value,
                "statement": statement_value,
                "evidence": [
                    {
                        "label": item["label"],
                        "note": item["note"],
                        "provenance": item["provenance"],
                    }
                    for item in normalized_evidence
                ],
            }
        )

        existing = await self._scalar(
            session,
            select(ResearchFinding)
            .where(
                ResearchFinding.research_run_id == run.id,
                ResearchFinding.idempotency_key == key,
            )
            .limit(1),
        )
        if existing is not None:
            if existing.finding_hash != finding_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different ResearchFinding payload"
                )
            return await self._finding_detail(session, existing)

        duplicate = await self._scalar(
            session,
            select(ResearchFinding)
            .where(
                ResearchFinding.research_run_id == run.id,
                ResearchFinding.finding_hash == finding_hash,
            )
            .limit(1),
        )
        if duplicate is not None:
            raise MediaOperationsConflictError(
                "an identical ResearchFinding already exists in this run"
            )

        finding = ResearchFinding(
            id=uuid4(),
            research_run_id=run.id,
            owner_user_id=actor_id,
            project_id=run.project_id,
            kind=kind_value,
            statement=statement_value,
            finding_hash=finding_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        session.add(finding)

        for item in normalized_evidence:
            session.add(
                ResearchFindingEvidence(
                    id=uuid4(),
                    finding_id=finding.id,
                    owner_user_id=actor_id,
                    project_id=run.project_id,
                    ordinal=item["ordinal"],
                    evidence_type=item["evidence_type"],
                    label=item["label"],
                    source_url=item["source_url"],
                    artifact_sha256=item["artifact_sha256"],
                    artifact_mime_type=item[
                        "artifact_mime_type"
                    ],
                    note=item["note"],
                    evidence_hash=item["evidence_hash"],
                )
            )

        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)

            recovered_run = await self._get_row(
                session,
                ResearchRun,
                run_id,
                "research_run_id",
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_run,
                permission="read",
            )
            recovered = await self._scalar(
                session,
                select(ResearchFinding)
                .where(
                    ResearchFinding.research_run_id == recovered_run.id,
                    ResearchFinding.idempotency_key == key,
                )
                .limit(1),
            )
            if (
                recovered is not None
                and recovered.finding_hash == finding_hash
            ):
                return await self._finding_detail(session, recovered)

            raise MediaOperationsConflictError(
                "ResearchFinding conflicts with an existing record"
            ) from exc

        return await self._finding_detail(session, finding)

    async def _candidate_detail(
        self,
        session: Any,
        candidate: ResearchCandidate,
    ) -> dict[str, Any]:
        # Candidate model projections retain the write-only idempotency key
        # for internal replay checks.  It is not part of the browser/tool
        # contract (and exposing it would make the strict response model
        # reject otherwise safe candidate payloads), so strip it at this
        # service boundary just like ContentItem projections do.
        detail = dict(candidate.to_safe_dict())
        detail.pop("idempotency_key", None)
        # Reuse the same provenance sanitizer used for immutable decision
        # snapshots.  This keeps legacy rows with malformed URLs or arbitrary
        # nested evidence from leaking secrets through the browser DTO.
        safe_snapshot = self._candidate_decision_snapshot(candidate)
        detail["source_url"] = safe_snapshot["source_url"]
        detail["evidence"] = safe_snapshot["evidence"]
        return {
            **detail,
            **await self._candidate_ranking(session, candidate),
        }

    async def _candidate_graph_is_consistent(
        self,
        session: Any,
        candidate: ResearchCandidate,
    ) -> bool:
        """Validate the complete Character → candidate FK graph.

        Candidate/decision rows are untrusted projections.  A matching owner
        or opaque UUID alone is not enough to expose them: the run and pinned
        routine revision must belong to the same routine/persona, and a
        promoted candidate must point to a ContentItem in that same graph.
        """

        routine = await self._scalar(
            session,
            select(ResearchRoutine).where(ResearchRoutine.id == candidate.research_routine_id).limit(1),
        )
        run = await self._scalar(
            session,
            select(ResearchRun).where(ResearchRun.id == candidate.research_run_id).limit(1),
        )
        revision = await self._scalar(
            session,
            select(ResearchRoutineRevision)
            .where(ResearchRoutineRevision.id == candidate.routine_revision_id)
            .limit(1),
        )
        if routine is None or run is None or revision is None:
            return False
        owner_id = getattr(candidate, "owner_user_id", None)
        project_id = getattr(candidate, "project_id", None)
        if any(
            getattr(row, "owner_user_id", None) != owner_id
            or getattr(row, "project_id", None) != project_id
            for row in (routine, run, revision)
        ):
            return False
        if (
            run.research_routine_id != routine.id
            or run.research_routine_revision_id != revision.id
            or run.routine_content_hash != revision.content_hash
            or revision.research_routine_id != routine.id
        ):
            return False
        # Standalone research routines predate the Character binding.  Keep
        # their internally consistent candidate history readable; once a
        # routine is bound to a Persona, the full Character graph below is
        # mandatory.
        if routine.persona_id is None:
            return getattr(candidate, "content_item_id", None) is None
        persona = await self._scalar(
            session,
            select(Persona).where(Persona.id == routine.persona_id).limit(1),
        )
        if persona is None or any(
            getattr(persona, field, None) != value
            for field, value in (
                ("owner_user_id", owner_id),
                ("project_id", project_id),
            )
        ):
            return False

        content_item_id = getattr(candidate, "content_item_id", None)
        if content_item_id is None:
            return True
        content_item = await self._scalar(
            session,
            select(ContentItem).where(ContentItem.id == content_item_id).limit(1),
        )
        if content_item is None or any(
            getattr(content_item, field, None) != value
            for field, value in (
                ("owner_user_id", owner_id),
                ("project_id", project_id),
            )
        ):
            return False
        program = await self._scalar(
            session,
            select(EditorialProgram)
            .where(EditorialProgram.id == content_item.editorial_program_id)
            .limit(1),
        )
        if program is None or any(
            getattr(program, field, None) != value
            for field, value in (
                ("owner_user_id", owner_id),
                ("project_id", project_id),
            )
        ) or program.persona_id != persona.id:
            return False
        if str(getattr(candidate, "status", "")) == "promoted":
            persona_revision_id = getattr(content_item, "persona_revision_id", None)
            if persona_revision_id is None:
                return False
            linked_revision = await self._scalar(
                session,
                select(PersonaRevision)
                .where(PersonaRevision.id == persona_revision_id)
                .limit(1),
            )
            if linked_revision is None or linked_revision.persona_id != persona.id:
                return False
        return True

    async def _candidate_detail_with_decision(
        self,
        session: Any,
        candidate: ResearchCandidate,
        decision: Any | None,
    ) -> dict[str, Any]:
        detail = await self._candidate_detail(session, candidate)
        detail["decision"] = (
            self._safe_candidate_decision(decision)
            if decision is not None
            else None
        )
        return detail

    async def _candidate_ranking(
        self,
        session: Any,
        candidate: ResearchCandidate,
    ) -> dict[str, Any]:
        """Compute a deterministic, policy-aware score for an untrusted idea.

        Ranking is intentionally ephemeral: it is derived from the latest
        immutable PersonaRevision and candidate evidence, never persisted as
        an authority-bearing field.  A changed Persona policy therefore
        changes the next list/detail response without rewriting history.
        """

        routine = await self._scalar(
            session,
            select(ResearchRoutine)
            .where(ResearchRoutine.id == candidate.research_routine_id)
            .limit(1),
        )
        persona_revision = None
        if routine is not None and getattr(routine, "persona_id", None) is not None:
            persona_revision = await self._scalar(
                session,
                select(PersonaRevision)
                .where(PersonaRevision.persona_id == routine.persona_id)
                .order_by(PersonaRevision.version.desc(), PersonaRevision.id.desc())
                .limit(1),
            )

        relevance = float(candidate.relevance_score or 0.0)
        freshness = float(candidate.freshness_score or 0.0)
        base_score = (0.6 * relevance) + (0.3 * freshness)
        text = f"{candidate.title} {candidate.summary}".casefold()
        factors: dict[str, Any] = {
            "relevance": round(relevance, 6),
            "freshness": round(freshness, 6),
            "content_pillar_match": 0,
            "creative_direction_match": 0,
            "research_policy_match": 0,
        }

        if persona_revision is not None:
            pillars = [
                str(value).strip()
                for value in (getattr(persona_revision, "content_pillars_json", None) or [])
                if isinstance(value, str) and value.strip()
            ]
            pillar_matches = sum(1 for value in pillars if value.casefold() in text)
            factors["content_pillar_match"] = pillar_matches

            direction = str(getattr(persona_revision, "creative_direction", None) or "")
            direction_tokens = {
                token
                for token in direction.casefold().split()
                if len(token) >= 4 and token.isalnum()
            }
            candidate_tokens = {
                token
                for token in text.split()
                if len(token) >= 4 and token.isalnum()
            }
            direction_match = len(direction_tokens & candidate_tokens)
            factors["creative_direction_match"] = direction_match

            policy = getattr(persona_revision, "research_policy_json", None)
            policy_terms: set[str] = set()

            def _collect_policy_terms(value: Any, *, depth: int = 0) -> None:
                if depth > 3 or len(policy_terms) >= 40:
                    return
                if isinstance(value, str):
                    rendered = value.strip().casefold()
                    if len(rendered) >= 3:
                        policy_terms.add(rendered)
                elif isinstance(value, Mapping):
                    for child in value.values():
                        _collect_policy_terms(child, depth=depth + 1)
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    for child in value:
                        _collect_policy_terms(child, depth=depth + 1)

            _collect_policy_terms(policy)
            policy_match = sum(1 for term in policy_terms if term in text)
            factors["research_policy_match"] = policy_match

        # Policy signals are capped so opaque candidate text can never dominate
        # the explicit run scores.  The exact arithmetic is stable across
        # Python/database backends and therefore suitable for tie-breaking.
        policy_bonus = min(
            0.1,
            0.02 * float(factors["content_pillar_match"])
            + 0.01 * float(factors["creative_direction_match"])
            + 0.02 * float(factors["research_policy_match"]),
        )
        score = max(0.0, min(1.0, base_score + policy_bonus))
        rationale = (
            f"relevance={relevance:.3f}; freshness={freshness:.3f}; "
            f"pillar_matches={factors['content_pillar_match']}; "
            f"creative_matches={factors['creative_direction_match']}; "
            f"policy_matches={factors['research_policy_match']}"
        )
        return {
            "ranking_score": round(score, 6),
            "ranking_factors": factors,
            "ranking_rationale": rationale,
            "ranking_policy_revision_id": (
                str(persona_revision.id) if persona_revision is not None else None
            ),
            "ranking_policy_content_hash": (
                str(persona_revision.content_hash)
                if persona_revision is not None
                else None
            ),
        }

    @staticmethod
    def _candidate_decision_model() -> Any:
        """Resolve the WS03 append-only ledger model lazily.

        The lazy lookup keeps older import-only callers usable while the
        migration/model pair is being rolled out.  A request that actually
        writes or reads decisions still fails closed if the model is absent.
        """

        try:
            from ..memory.models import ResearchCandidateDecision
        except ImportError as exc:  # pragma: no cover - pre-WS03 deployments
            try:
                # During a rolling deploy the model may already be present in
                # its domain module while the package re-export is pending.
                from ..memory.models.media_operations_research import (
                    ResearchCandidateDecision,
                )
            except ImportError:
                raise MediaOperationsValidationError(
                    "ResearchCandidate decision ledger is unavailable"
                ) from exc
        return ResearchCandidateDecision

    @staticmethod
    def _candidate_decision_snapshot(candidate: ResearchCandidate) -> dict[str, Any]:
        """Build a bounded, source-only candidate snapshot for the ledger."""

        evidence = list(getattr(candidate, "evidence_json", None) or [])[:20]
        # Evidence is normally normalized by candidate intake.  Legacy rows
        # may still contain arbitrary JSON, so reconstruct a bounded
        # provenance envelope instead of copying nested ``provenance`` values
        # (which could carry credential/query secrets).
        safe_evidence: list[dict[str, Any]] = []
        for raw in evidence:
            if not isinstance(raw, Mapping):
                continue
            item: dict[str, Any] = {}
            evidence_type = str(
                raw.get("evidence_type") or raw.get("type") or ""
            ).strip().lower()
            if evidence_type not in {"url", "artifact"}:
                continue
            item["evidence_type"] = evidence_type
            # Keep both the normalized wire key and the legacy alias where
            # available, but only after URL validation strips sensitive query
            # material and rejects non-http(s) targets.
            raw_url = raw.get("source_url") or raw.get("url")
            if isinstance(raw_url, str) and raw_url.strip():
                try:
                    normalized_url = _validated_resource_url(raw_url)
                except MediaOperationsValidationError:
                    normalized_url = None
                if normalized_url:
                    item["source_url"] = normalized_url
                    if "url" in raw:
                        item["url"] = normalized_url

            raw_sha = raw.get("artifact_sha256") or raw.get("sha256")
            if isinstance(raw_sha, str) and raw_sha.strip():
                try:
                    normalized_sha = _validated_sha256(raw_sha, "evidence.sha256")
                except MediaOperationsValidationError:
                    normalized_sha = None
                if normalized_sha:
                    item["artifact_sha256"] = normalized_sha
                    if "sha256" in raw:
                        item["sha256"] = normalized_sha

            raw_mime = raw.get("artifact_mime_type") or raw.get("mime_type")
            if isinstance(raw_mime, str) and raw_mime.strip():
                normalized_mime = raw_mime.strip()[:255]
                item["artifact_mime_type"] = normalized_mime
                if "mime_type" in raw:
                    item["mime_type"] = normalized_mime

            raw_ordinal = raw.get("ordinal")
            if isinstance(raw_ordinal, int) and not isinstance(raw_ordinal, bool):
                if 1 <= raw_ordinal <= 20:
                    item["ordinal"] = raw_ordinal
            raw_evidence_hash = raw.get("evidence_hash")
            if isinstance(raw_evidence_hash, str) and len(raw_evidence_hash) == 64:
                try:
                    item["evidence_hash"] = _validated_sha256(
                        raw_evidence_hash,
                        "evidence.evidence_hash",
                    )
                except MediaOperationsValidationError:
                    pass

            for key, maximum in (("label", 255), ("note", 2000)):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    item[key] = value.strip()[:maximum]

            # Rebuild provenance from already-normalized references; never
            # persist the source row's arbitrary nested object.
            provenance: dict[str, Any] = {"type": evidence_type}
            if item.get("source_url"):
                provenance["url"] = item["source_url"]
            if item.get("artifact_sha256"):
                provenance["sha256"] = item["artifact_sha256"]
            if item.get("artifact_mime_type"):
                provenance["mime_type"] = item["artifact_mime_type"]
            item["provenance"] = provenance
            safe_evidence.append(item)
        source_url = None
        if candidate.source_url:
            try:
                source_url = _validated_resource_url(candidate.source_url)
            except MediaOperationsValidationError:
                source_url = None
        return {
            "candidate_id": str(candidate.id),
            "research_routine_id": str(candidate.research_routine_id),
            "research_run_id": str(candidate.research_run_id),
            "routine_revision_id": str(candidate.routine_revision_id),
            "candidate_key": str(candidate.candidate_key)[:64],
            "title": str(candidate.title)[:500],
            "summary": str(candidate.summary)[:8000],
            "discovered_at": (
                candidate.discovered_at.isoformat()
                if candidate.discovered_at
                else None
            ),
            "expires_at": (
                candidate.expires_at.isoformat() if candidate.expires_at else None
            ),
            "source_url": source_url,
            "source_published_at": (
                candidate.source_published_at.isoformat()
                if candidate.source_published_at
                else None
            ),
            "relevance_score": candidate.relevance_score,
            "freshness_score": candidate.freshness_score,
            "evidence": safe_evidence,
            "reason": str(candidate.reason)[:4000] if candidate.reason else None,
            "status": candidate.status,
            "candidate_hash": candidate.candidate_hash,
        }

    @staticmethod
    def _safe_candidate_decision(decision: Any) -> dict[str, Any]:
        """Project a decision row without exposing ORM internals/secrets."""

        to_safe_dict = getattr(decision, "to_safe_dict", None)
        if callable(to_safe_dict):
            raw = to_safe_dict()
            if isinstance(raw, Mapping):
                projected = dict(raw)
                # Keep the public projection stable even if a model helper
                # grows internal request/hash fields in a later migration.
                allowed = {
                    "id",
                    "candidate_id",
                    "sequence",
                    "event_type",
                    "from_status",
                    "to_status",
                    "reason",
                    "candidate_hash",
                    "candidate_snapshot_hash",
                    "request_hash",
                    "actor_id",
                    "actor_type",
                    "content_item_id",
                    "decision_hash",
                    "prev_decision_hash",
                    "prev_event_hash",
                    "event_hash",
                    "decided_at",
                    "created_at",
                }
                return {key: value for key, value in projected.items() if key in allowed}

        def _text(name: str) -> str | None:
            value = getattr(decision, name, None)
            return str(value) if value is not None else None

        return {
            "id": _text("id"),
            "candidate_id": _text("candidate_id"),
            "sequence": int(getattr(decision, "sequence", 0) or 0),
            "event_type": _text("event_type"),
            "from_status": _text("from_status"),
            "to_status": _text("to_status"),
            "reason": _text("reason"),
            "candidate_hash": _text("candidate_hash"),
            "candidate_snapshot_hash": _text("candidate_hash"),
            "request_hash": _text("request_hash"),
            "actor_id": _text("actor_id"),
            "actor_type": _text("actor_type"),
            "content_item_id": _text("content_item_id"),
            "decision_hash": _text("decision_hash"),
            "prev_decision_hash": _text("prev_event_hash"),
            "prev_event_hash": _text("prev_event_hash"),
            "event_hash": _text("event_hash"),
            "decided_at": (
                decision.created_at.isoformat()
                if getattr(decision, "created_at", None)
                else None
            ),
            "created_at": (
                decision.created_at.isoformat()
                if getattr(decision, "created_at", None)
                else None
            ),
        }

    async def _append_candidate_decision(
        self,
        session: Any,
        candidate: ResearchCandidate,
        actor: Any,
        *,
        event_type: str,
        to_status: str,
        reason: str | None,
        idempotency_key: str,
        request_hash: str,
        content_item_id: UUID | None = None,
    ) -> Any:
        """Append one immutable candidate decision under a candidate row lock."""

        Decision = self._candidate_decision_model()
        key = _idempotency_key(idempotency_key)
        existing = await self._scalar(
            session,
            select(Decision)
            .where(
                Decision.candidate_id == candidate.id,
                Decision.idempotency_key == key,
                Decision.owner_user_id == candidate.owner_user_id,
                (
                    Decision.project_id.is_(None)
                    if candidate.project_id is None
                    else Decision.project_id == candidate.project_id
                ),
            )
            .limit(1),
        )
        if existing is not None:
            if getattr(existing, "request_hash", None) != request_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used with a different candidate decision"
                )
            return existing

        previous = await self._scalar(
            session,
            select(Decision)
            .where(Decision.candidate_id == candidate.id)
            .order_by(Decision.sequence.desc(), Decision.id.desc())
            .limit(1)
            .with_for_update(),
        )
        previous_hash = getattr(previous, "event_hash", None) if previous else None
        sequence = int(getattr(previous, "sequence", 0) or 0) + 1
        actor_id = _actor_id(actor)
        actor_type = _actor_type(actor)
        snapshot = self._candidate_decision_snapshot(candidate)
        decision_hash = sha256_json(
            {
                "candidate_id": str(candidate.id),
                "candidate_hash": candidate.candidate_hash,
                "sequence": sequence,
                "event_type": event_type,
                "from_status": candidate.status,
                "to_status": to_status,
                "reason": reason,
                "content_item_id": str(content_item_id) if content_item_id else None,
                "request_hash": request_hash,
                "actor_id": str(actor_id),
                "actor_type": actor_type,
            }
        )
        event_hash = sha256_json(
            {
                "decision_hash": decision_hash,
                "prev_event_hash": previous_hash,
            }
        )
        decision = Decision(
            id=uuid4(),
            owner_user_id=getattr(candidate, "owner_user_id", None),
            project_id=getattr(candidate, "project_id", None),
            candidate_id=candidate.id,
            sequence=sequence,
            event_type=event_type,
            from_status=candidate.status,
            to_status=to_status,
            reason=reason,
            candidate_snapshot_json=snapshot,
            candidate_hash=candidate.candidate_hash,
            actor_id=actor_id,
            actor_type=actor_type,
            idempotency_key=key,
            request_hash=request_hash,
            decision_hash=decision_hash,
            prev_event_hash=previous_hash,
            event_hash=event_hash,
            content_item_id=content_item_id,
            created_at=datetime.utcnow(),
        )
        session.add(decision)
        # Keep the mutable candidate projection monotonic with the immutable
        # ledger.  This is a denormalized read optimisation; the source of
        # truth remains the decision sequence itself.
        candidate.decision_version = sequence
        return decision

    async def _recover_candidate_decision(
        self,
        session: Any,
        candidate: ResearchCandidate,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> Any | None:
        """Read a concurrently committed decision after a uniqueness error."""

        Decision = self._candidate_decision_model()
        recovered = await self._scalar(
            session,
            select(Decision)
            .where(
                Decision.candidate_id == candidate.id,
                Decision.idempotency_key == _idempotency_key(idempotency_key),
                Decision.owner_user_id == candidate.owner_user_id,
                (
                    Decision.project_id.is_(None)
                    if candidate.project_id is None
                    else Decision.project_id == candidate.project_id
                ),
            )
            .limit(1),
        )
        if recovered is not None and getattr(recovered, "request_hash", None) != request_hash:
            raise MediaOperationsConflictError(
                "idempotency key was already used with a different candidate decision"
            )
        return recovered

    async def list_research_candidate_decisions(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        candidate_id: UUID | str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or candidate_id is None:
            raise MediaOperationsValidationError("actor and candidate_id are required")
        candidate = await self._get_row(session, ResearchCandidate, candidate_id, "candidate_id")
        await self._assert_entity_access(session, actor, candidate, permission="read")
        if not await self._candidate_graph_is_consistent(session, candidate):
            raise MediaOperationsNotFoundError("candidate_id not found")
        Decision = self._candidate_decision_model()
        page_limit, page_offset = _bounded_page(limit, offset)
        condition = await self._scope_condition(
            session,
            actor,
            Decision,
            project_id=getattr(candidate, "project_id", None),
        )
        base_condition = (
            condition,
            Decision.candidate_id == candidate.id,
            Decision.owner_user_id == candidate.owner_user_id,
            (
                Decision.project_id.is_(None)
                if candidate.project_id is None
                else Decision.project_id == candidate.project_id
            ),
        )
        count_result = await self._execute(
            session,
            select(func.count(Decision.id)).where(*base_condition),
        )
        total = int(count_result.scalar_one() or 0)
        rows = await self._scalars(
            session,
            select(Decision)
            .where(*base_condition)
            .order_by(Decision.sequence.asc(), Decision.id.asc())
            .limit(page_limit)
            .offset(page_offset),
        )
        return {
            "candidate_id": str(candidate.id),
            "current_status": str(candidate.status),
            "current_decision_version": int(getattr(candidate, "decision_version", 0) or 0),
            "candidate_hash": str(candidate.candidate_hash),
            "items": [self._safe_candidate_decision(row) for row in rows],
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + page_limit < total,
        }

    async def create_research_candidate(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        research_run_id: UUID | str,
        candidate_key: Any,
        title: Any,
        summary: Any,
        source_url: Any = None,
        source_published_at: Any = None,
        relevance_score: Any = None,
        freshness_score: Any = None,
        evidence: Any,
        reason: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        """Record one bounded, untrusted candidate from an exact run.

        This is deliberately an intake operation: it can only create a
        ``discovered`` (or already-expired) candidate.  Review transitions and
        promotion are separate, human-gated methods so external text can never
        grant authority or trigger publication.
        """

        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        run = await self._get_row(
            session,
            ResearchRun,
            research_run_id,
            "research_run_id",
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            run,
            permission="write",
        )
        routine = await self._get_row(
            session,
            ResearchRoutine,
            run.research_routine_id,
            "research_routine_id",
        )
        revision = await self._get_row(
            session,
            ResearchRoutineRevision,
            run.research_routine_revision_id,
            "routine_revision_id",
        )
        if revision.research_routine_id != routine.id or run.routine_content_hash != revision.content_hash:
            raise MediaOperationsConflictError("ResearchRun routine snapshot is inconsistent")

        key = _idempotency_key(idempotency_key)
        candidate_key_value = _normalize_candidate_key(candidate_key)
        title_value = _required_text(title, "title", 500)
        summary_value = _required_text(summary, "summary", 8000)
        source_url_value = (
            _validated_resource_url(source_url)
            if source_url not in (None, "")
            else None
        )
        source_date = _parse_datetime(source_published_at, "source_published_at")
        now = datetime.utcnow()
        if source_date is not None and source_date > now:
            raise MediaOperationsValidationError("source_published_at must not be in the future")
        relevance = _bounded_score(relevance_score, "relevance_score")
        freshness = _bounded_score(freshness_score, "freshness_score")
        normalized_evidence = _normalize_evidence(evidence)
        reason_value = _optional_text(reason, "reason", 4000)
        expires_at = (
            source_date + timedelta(hours=int(revision.freshness_hours or 168))
            if source_date is not None
            else now + timedelta(hours=int(revision.freshness_hours or 168))
        )
        status = "expired" if expires_at <= now else "discovered"
        candidate_hash = sha256_json(
            {
                "research_routine_id": str(routine.id),
                "research_run_id": str(run.id),
                "routine_revision_id": str(revision.id),
                "candidate_key": candidate_key_value,
                "title": title_value,
                "summary": summary_value,
                "source_url": source_url_value,
                "source_published_at": source_date.isoformat() if source_date else None,
                "relevance_score": relevance,
                "freshness_score": freshness,
                "evidence": normalized_evidence,
                "reason": reason_value,
            }
        )

        existing = await self._scalar(
            session,
            select(ResearchCandidate)
            .where(
                ResearchCandidate.research_run_id == run.id,
                ResearchCandidate.idempotency_key == key,
            )
            .limit(1),
        )
        if existing is not None:
            if existing.candidate_hash != candidate_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used with a different ResearchCandidate payload"
                )
            if not await self._candidate_graph_is_consistent(session, existing):
                raise MediaOperationsNotFoundError("candidate_id not found")
            return await self._candidate_detail(session, existing)

        duplicate = await self._scalar(
            session,
            select(ResearchCandidate)
            .where(
                ResearchCandidate.research_routine_id == routine.id,
                ResearchCandidate.candidate_key == candidate_key_value,
            )
            .limit(1),
        )
        if duplicate is not None:
            if duplicate.candidate_hash == candidate_hash:
                if not await self._candidate_graph_is_consistent(session, duplicate):
                    raise MediaOperationsNotFoundError("candidate_id not found")
                return await self._candidate_detail(session, duplicate)
            raise MediaOperationsConflictError(
                "an identical candidate key already exists for this ResearchRoutine"
            )

        candidate = ResearchCandidate(
            id=uuid4(),
            owner_user_id=actor_id,
            project_id=run.project_id,
            research_routine_id=routine.id,
            research_run_id=run.id,
            routine_revision_id=revision.id,
            candidate_key=candidate_key_value,
            title=title_value,
            summary=summary_value,
            source_url=source_url_value,
            source_published_at=source_date,
            discovered_at=now,
            expires_at=expires_at,
            relevance_score=relevance,
            freshness_score=freshness,
            evidence_json=normalized_evidence,
            reason=reason_value,
            status=status,
            candidate_hash=candidate_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        # Keep the run projection bounded and source-only.  Candidate body and
        # evidence remain in the candidate ledger, never in a tool authority
        # field or a provider credential payload.
        if source_url_value:
            refs = list(run.source_refs_json or [])
            if source_url_value not in refs and len(refs) < 100:
                refs.append(source_url_value)
            run.source_refs_json = refs
        session.add(candidate)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(
                session,
                select(ResearchCandidate)
                .where(
                    ResearchCandidate.research_run_id == run.id,
                    ResearchCandidate.idempotency_key == key,
                )
                .limit(1),
            )
            if recovered is not None and recovered.candidate_hash == candidate_hash:
                if not await self._candidate_graph_is_consistent(session, recovered):
                    raise MediaOperationsNotFoundError("candidate_id not found")
                return await self._candidate_detail(session, recovered)
            raise MediaOperationsConflictError(
                "ResearchCandidate conflicts with an existing record"
            ) from exc
        return await self._candidate_detail(session, candidate)

    async def list_research_candidates(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        research_run_id: UUID | str | None = None,
        research_routine_id: UUID | str | None = None,
        status: Any = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        page_limit, page_offset = _bounded_page(limit, offset)
        conditions = [
            await self._scope_condition(
                session,
                actor,
                ResearchCandidate,
                project_id=project_uuid,
            )
        ]
        if research_run_id is not None:
            run = await self._get_row(session, ResearchRun, research_run_id, "research_run_id")
            await self._assert_entity_access(session, actor, run, permission="read")
            conditions.append(ResearchCandidate.research_run_id == run.id)
        if research_routine_id is not None:
            routine = await self._get_row(session, ResearchRoutine, research_routine_id, "research_routine_id")
            await self._assert_entity_access(session, actor, routine, permission="read")
            conditions.append(ResearchCandidate.research_routine_id == routine.id)
        if status not in (None, ""):
            rendered_status = str(getattr(status, "value", status)).strip().lower()
            if rendered_status == "pending":
                conditions.append(ResearchCandidate.status.in_(["discovered", "triaged"]))
            else:
                conditions.append(ResearchCandidate.status == _candidate_status(rendered_status))
        rows = await self._scalars(
            session,
            select(ResearchCandidate)
            .where(*conditions)
            # Ranking is derived from PersonaRevision JSON and cannot be
            # expressed portably in both PostgreSQL and SQLite.  Load a
            # bounded candidate window, score in Python, then page the stable
            # result.  The hard cap prevents an untrusted project from causing
            # an unbounded read while preserving deterministic ordering for
            # normal dashboard-sized queues.
            .order_by(ResearchCandidate.discovered_at.desc(), ResearchCandidate.id.desc())
            .limit(5000),
        )
        # Treat malformed/legacy cross-scope candidates as invisible rather
        # than allowing an opaque owner/project match to leak source material
        # into a list response.  Single-candidate reads fail closed with a
        # not-found error; list reads simply omit the invalid row.
        details: list[dict[str, Any]] = []
        for row in rows:
            if not await self._candidate_graph_is_consistent(session, row):
                continue
            details.append(await self._candidate_detail(session, row))
        # Stable multi-pass sort gives score-descending priority with
        # discovered_at/id descending tie-breaks without backend-specific
        # datetime expressions.
        details.sort(key=lambda item: str(item.get("id") or ""), reverse=True)
        details.sort(
            key=lambda item: str(item.get("discovered_at") or ""),
            reverse=True,
        )
        details.sort(
            key=lambda item: float(item.get("ranking_score") or 0.0),
            reverse=True,
        )
        return details[page_offset : page_offset + page_limit]

    async def get_research_candidate(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        candidate_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or candidate_id is None:
            raise MediaOperationsValidationError("actor and candidate_id are required")
        candidate = await self._get_row(session, ResearchCandidate, candidate_id, "candidate_id")
        await self._assert_entity_access(session, actor, candidate, permission="read")
        if not await self._candidate_graph_is_consistent(session, candidate):
            raise MediaOperationsNotFoundError("candidate_id not found")
        return await self._candidate_detail(session, candidate)

    async def triage_research_candidate(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        candidate_id: UUID | str | None = None,
        *,
        status: Any,
        reason: Any = None,
        idempotency_key: Any = None,
        expected_status: Any = None,
        expected_decision_version: Any = None,
        expected_candidate_hash: Any = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or candidate_id is None:
            raise MediaOperationsValidationError("actor and candidate_id are required")
        candidate = await self._get_row(
            session, ResearchCandidate, candidate_id, "candidate_id", for_update=True
        )
        await self._assert_entity_access(session, actor, candidate, permission="write")
        if not await self._candidate_graph_is_consistent(session, candidate):
            raise MediaOperationsNotFoundError("candidate_id not found")
        next_status = _candidate_status(status)
        if next_status not in {"triaged", "accepted", "rejected"}:
            raise MediaOperationsValidationError("candidate status transition is invalid")
        key = _idempotency_key(idempotency_key)
        reason_value = _optional_text(reason, "reason", 4000)
        if expected_status is None or expected_decision_version is None or expected_candidate_hash is None:
            raise MediaOperationsValidationError(
                "expected_status, expected_decision_version, and expected_candidate_hash are required"
            )
        expected_status_value = str(
            getattr(expected_status, "value", expected_status)
        ).strip().lower()
        if expected_status_value not in {"discovered", "triaged", "accepted"}:
            raise MediaOperationsValidationError(
                "expected_status must be discovered, triaged, or accepted"
            )
        if isinstance(expected_decision_version, bool):
            raise MediaOperationsValidationError(
                "expected_decision_version must be an integer"
            )
        try:
            expected_version_value = int(expected_decision_version)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "expected_decision_version must be an integer"
            ) from exc
        if expected_version_value < 0:
            raise MediaOperationsValidationError(
                "expected_decision_version must be non-negative"
            )
        expected_hash_value = _validated_sha256(
            expected_candidate_hash,
            "expected_candidate_hash",
        )
        request_hash = sha256_json(
            {
                "candidate_id": str(candidate.id),
                "status": next_status,
                "reason": reason_value,
                "expected_status": expected_status_value,
                "expected_decision_version": expected_version_value,
                "expected_candidate_hash": expected_hash_value,
                "actor_id": str(_actor_id(actor)),
                "actor_type": _actor_type(actor),
            }
        )
        Decision = self._candidate_decision_model()
        replay = await self._scalar(
            session,
            select(Decision)
            .where(
                Decision.candidate_id == candidate.id,
                Decision.idempotency_key == key,
                Decision.owner_user_id == candidate.owner_user_id,
                (
                    Decision.project_id.is_(None)
                    if candidate.project_id is None
                    else Decision.project_id == candidate.project_id
                ),
            )
            .limit(1),
        )
        if replay is not None:
            if getattr(replay, "request_hash", None) != request_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used with a different candidate decision"
                )
            return await self._candidate_detail_with_decision(session, candidate, replay)
        # Reject automated final decisions before mutating the projection, but
        # after the idempotency replay path so a harmless retry cannot create a
        # second row or leak a different authorization outcome.
        if next_status in {"accepted", "rejected"} and not _is_human_actor(actor):
            raise MediaOperationsAuthorizationError(
                "only an explicitly human actor may accept or reject a candidate"
            )
        if (
            str(candidate.status or "").strip().lower() != expected_status_value
            or int(getattr(candidate, "decision_version", 0) or 0) != expected_version_value
            or str(getattr(candidate, "candidate_hash", "")).lower() != expected_hash_value
        ):
            raise MediaOperationsConflictError(
                "candidate changed; expected status, decision version, and candidate hash do not match"
            )
        if candidate.status in {"expired", "rejected", "promoted"}:
            # A retry of an already-recorded event is safe and handled by the
            # ledger idempotency check below; a new transition is not.
            raise MediaOperationsConflictError("candidate is no longer triageable")
        if candidate.status == "triaged" and next_status == "triaged":
            raise MediaOperationsConflictError("candidate is already triaged")
        if candidate.status == "accepted" and next_status == "triaged":
            # Acceptance is a terminal review decision unless the reviewer
            # explicitly revokes it.  A transition back to the triage queue
            # would make the candidate look pending again without recording a
            # rejection/revocation event, so fail closed instead.
            raise MediaOperationsConflictError(
                "accepted candidate cannot return to triage"
            )

        if next_status in {"accepted", "rejected"}:
            if reason_value is None:
                raise MediaOperationsValidationError(
                    "a non-empty reason is required to accept or reject a candidate"
                )
        event_type = {
            "triaged": "triage",
            "accepted": "accept",
            "rejected": "reject",
        }[next_status]
        await self._append_candidate_decision(
            session,
            candidate,
            actor,
            event_type=event_type,
            to_status=next_status,
            reason=reason_value,
            idempotency_key=key,
            request_hash=request_hash,
        )
        candidate.status = next_status
        candidate.decision_version = expected_version_value + 1
        if reason_value is not None:
            candidate.reason = reason_value
        candidate.updated_at = datetime.utcnow()
        decision = await self._scalar(
            session,
            select(Decision)
            .where(
                Decision.candidate_id == candidate.id,
                Decision.idempotency_key == key,
            )
            .limit(1),
        )
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered_candidate = await self._get_row(
                session,
                ResearchCandidate,
                candidate.id,
                "candidate_id",
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_candidate,
                permission="read",
            )
            recovered = await self._recover_candidate_decision(
                session,
                recovered_candidate,
                idempotency_key=key,
                request_hash=request_hash,
            )
            if recovered is not None:
                return await self._candidate_detail_with_decision(session, recovered_candidate, recovered)
            raise MediaOperationsConflictError(
                "candidate decision conflicts with an existing record"
            ) from exc
        return await self._candidate_detail_with_decision(session, candidate, decision)

    async def expire_research_candidates(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        now: Any = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        page_limit, _ = _bounded_page(limit, 0)
        condition = await self._scope_condition(
            session, actor, ResearchCandidate, project_id=project_uuid
        )
        cutoff = _parse_datetime(now, "now") or datetime.utcnow()
        rows = await self._scalars(
            session,
            select(ResearchCandidate)
            .where(
                condition,
                ResearchCandidate.expires_at.is_not(None),
                ResearchCandidate.expires_at <= cutoff,
                ResearchCandidate.status.in_(["discovered", "triaged"]),
            )
            .order_by(ResearchCandidate.expires_at.asc(), ResearchCandidate.id.asc())
            .limit(page_limit)
            .with_for_update(),
        )
        expired_candidates: list[ResearchCandidate] = []
        for candidate in rows:
            # Rows can change between the bounded discovery query and this
            # loop.  Re-check status under the lock and append an audit event
            # before mutating the projection.
            if candidate.status not in {"discovered", "triaged"}:
                continue
            key = f"system-expire:{cutoff.isoformat()}"
            request_hash = sha256_json(
                {
                    "candidate_id": str(candidate.id),
                    "to_status": "expired",
                    "as_of": cutoff.isoformat(),
                }
            )
            await self._append_candidate_decision(
                session,
                candidate,
                actor,
                event_type="expire",
                to_status="expired",
                reason="candidate expired",
                idempotency_key=key,
                request_hash=request_hash,
            )
            candidate.status = "expired"
            candidate.updated_at = cutoff
            expired_candidates.append(candidate)
        if expired_candidates:
            await self._flush_commit(session)
        return {
            "expired_count": len(expired_candidates),
            "candidate_ids": [str(candidate.id) for candidate in expired_candidates],
            "as_of": cutoff.isoformat(),
        }

    async def promote_research_candidate(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        candidate_id: UUID | str | None = None,
        *,
        title: Any = None,
        brief: Any = None,
        accepted_decision_id: Any,
        expected_decision_version: Any = None,
        expected_decision_hash: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or candidate_id is None:
            raise MediaOperationsValidationError("actor and candidate_id are required")
        # Promotion creates a draft ContentItem only after a candidate has
        # already reached the human-controlled ``accepted`` state.  It remains
        # a human-only initiation boundary; the Agent facade may propose the
        # operation but cannot execute it.  Publication and all external
        # actions remain in the human-only Trusted Operations Kernel.
        candidate = await self._get_row(
            session, ResearchCandidate, candidate_id, "candidate_id", for_update=True
        )
        await self._assert_entity_access(session, actor, candidate, permission="write")
        if not await self._candidate_graph_is_consistent(session, candidate):
            raise MediaOperationsNotFoundError("candidate_id not found")
        if not _is_human_actor(actor):
            raise MediaOperationsAuthorizationError(
                "only an explicitly human actor may promote an accepted candidate"
            )
        key = _idempotency_key(idempotency_key)
        accepted_decision_uuid = _as_uuid(
            accepted_decision_id,
            "accepted_decision_id",
        )
        assert accepted_decision_uuid is not None
        title_value = _required_text(
            title if title not in (None, "") else candidate.title,
            "title",
            500,
        )
        brief_value = _required_text(
            brief if brief not in (None, "") else candidate.summary,
            "brief",
            8000,
        )
        request_hash = sha256_json(
            {
                "candidate_id": str(candidate.id),
                "title": title_value,
                "brief": brief_value,
                "accepted_decision_id": str(accepted_decision_uuid),
                "expected_decision_version": (
                    int(expected_decision_version)
                    if expected_decision_version is not None
                    else None
                ),
                "expected_decision_hash": (
                    _validated_sha256(expected_decision_hash, "expected_decision_hash")
                    if expected_decision_hash is not None
                    else None
                ),
                "actor_id": str(_actor_id(actor)),
                "actor_type": _actor_type(actor),
            }
        )
        Decision = self._candidate_decision_model()
        replay = await self._scalar(
            session,
            select(Decision)
            .where(
                Decision.candidate_id == candidate.id,
                Decision.idempotency_key == key,
                Decision.owner_user_id == candidate.owner_user_id,
                (
                    Decision.project_id.is_(None)
                    if candidate.project_id is None
                    else Decision.project_id == candidate.project_id
                ),
            )
            .limit(1),
        )
        if replay is not None:
            if getattr(replay, "request_hash", None) != request_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used with a different promotion payload"
                )
            linked_id = getattr(replay, "content_item_id", None) or getattr(
                candidate, "content_item_id", None
            )
            if linked_id is None:
                raise MediaOperationsConflictError("promotion decision has no ContentItem link")
            return await self.get_content_item(session, actor, linked_id)
        if candidate.status == "promoted" and candidate.content_item_id is not None:
            raise MediaOperationsConflictError(
                "candidate was already promoted with a different idempotency key"
            )
        if candidate.status != "accepted":
            raise MediaOperationsConflictError("only an accepted candidate can be promoted")
        accepted_decision = await self._scalar(
            session,
            select(Decision)
            .where(
                Decision.id == accepted_decision_uuid,
                Decision.candidate_id == candidate.id,
            )
            .limit(1)
            .with_for_update(),
        )
        latest_decision = await self._scalar(
            session,
            select(Decision)
            .where(Decision.candidate_id == candidate.id)
            .order_by(Decision.sequence.desc(), Decision.id.desc())
            .limit(1),
        )
        if (
            accepted_decision is None
            or accepted_decision.event_type != "accept"
            or accepted_decision.to_status != "accepted"
            or int(getattr(accepted_decision, "sequence", 0) or 0)
            != int(getattr(candidate, "decision_version", 0) or 0)
            or latest_decision is None
            or latest_decision.id != accepted_decision.id
            or candidate.content_item_id is not None
        ):
            raise MediaOperationsConflictError(
                "promotion must bind to the latest accepted candidate decision"
            )
        if expected_decision_version is not None:
            if isinstance(expected_decision_version, bool):
                raise MediaOperationsValidationError(
                    "expected_decision_version must be an integer"
                )
            try:
                expected_promotion_version = int(expected_decision_version)
            except (TypeError, ValueError) as exc:
                raise MediaOperationsValidationError(
                    "expected_decision_version must be an integer"
                ) from exc
            if expected_promotion_version != int(accepted_decision.sequence):
                raise MediaOperationsConflictError(
                    "promotion decision version is stale"
                )
        if expected_decision_hash is not None:
            expected_promotion_hash = _validated_sha256(
                expected_decision_hash,
                "expected_decision_hash",
            )
            if str(accepted_decision.decision_hash).lower() != expected_promotion_hash:
                raise MediaOperationsConflictError(
                    "promotion decision hash is stale"
                )
        latest_decision = await self._scalar(
            session,
            select(Decision)
            .where(Decision.candidate_id == candidate.id)
            .order_by(Decision.sequence.desc(), Decision.id.desc())
            .limit(1),
        )
        if (
            int(getattr(candidate, "decision_version", 0) or 0) < 1
            or latest_decision is None
            or getattr(latest_decision, "event_type", None) != "accept"
            or getattr(latest_decision, "to_status", None) != "accepted"
            or str(getattr(latest_decision, "actor_type", "")).lower()
            not in {"human", "admin"}
        ):
            raise MediaOperationsConflictError(
                "candidate requires an explicit human acceptance decision before promotion"
            )
        run = await self._get_row(session, ResearchRun, candidate.research_run_id, "research_run_id")
        routine = await self._get_row(session, ResearchRoutine, candidate.research_routine_id, "research_routine_id")
        program = await self._scalar(
            session,
            select(EditorialProgram)
            .where(
                EditorialProgram.persona_id == routine.persona_id,
                EditorialProgram.owner_user_id == candidate.owner_user_id,
                EditorialProgram.project_id == candidate.project_id,
            )
            .order_by(EditorialProgram.created_at.asc(), EditorialProgram.id.asc())
            .limit(1),
        )
        if program is None:
            raise MediaOperationsValidationError(
                "an EditorialProgram for the candidate Persona is required before promotion"
            )
        await self._assert_entity_access(session, actor, program, permission="write")
        source_refs: list[str] = []
        if candidate.source_url:
            try:
                normalized_candidate_url = _validated_resource_url(candidate.source_url)
            except MediaOperationsValidationError:
                normalized_candidate_url = None
            if normalized_candidate_url:
                source_refs.append(normalized_candidate_url)
        for evidence in list(candidate.evidence_json or []):
            if not isinstance(evidence, Mapping):
                continue
            url = evidence.get("url") or evidence.get("source_url")
            if isinstance(url, str) and url not in source_refs:
                # Intake already validated this URL, but revalidate legacy
                # rows before carrying provenance into a ContentItem.
                try:
                    normalized_url = _validated_resource_url(url)
                except MediaOperationsValidationError:
                    normalized_url = None
                if (
                    normalized_url
                    and normalized_url not in source_refs
                    and len(source_refs) < 20
                ):
                    source_refs.append(normalized_url)
            if len(source_refs) >= 20:
                break
            artifact_sha = evidence.get("artifact_sha256") or evidence.get("sha256")
            if (
                isinstance(artifact_sha, str)
                and len(artifact_sha) == 64
                and len(source_refs) < 20
            ):
                try:
                    normalized_sha = _validated_sha256(artifact_sha, "evidence.sha256")
                except MediaOperationsValidationError:
                    normalized_sha = None
                if normalized_sha:
                    artifact_ref = f"artifact:{normalized_sha}"
                    if artifact_ref not in source_refs:
                        source_refs.append(artifact_ref)
            if len(source_refs) >= 20:
                break
        content_item = await self.create_content_item(
            session,
            actor,
            editorial_program_id=program.id,
            title=title_value,
            brief=brief_value,
            finding_ids=[],
            candidate_ids=[candidate.id],
            source_refs=source_refs,
            idempotency_key=key,
            _commit_transaction=False,
        )
        content_item_id = _as_uuid(content_item.get("id"), "content_item_id")
        await self._append_candidate_decision(
            session,
            candidate,
            actor,
            event_type="promote",
            to_status="promoted",
            reason="candidate promoted to draft ContentItem",
            idempotency_key=key,
            request_hash=request_hash,
            content_item_id=content_item_id,
        )
        candidate.status = "promoted"
        candidate.content_item_id = content_item_id
        candidate.updated_at = datetime.utcnow()
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered_candidate = await self._get_row(
                session,
                ResearchCandidate,
                candidate.id,
                "candidate_id",
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_candidate,
                permission="read",
            )
            recovered = await self._recover_candidate_decision(
                session,
                recovered_candidate,
                idempotency_key=key,
                request_hash=request_hash,
            )
            if recovered is not None:
                linked_id = getattr(recovered, "content_item_id", None) or getattr(
                    recovered_candidate,
                    "content_item_id",
                    None,
                )
                if linked_id is not None:
                    return await self.get_content_item(session, actor, linked_id)
            raise MediaOperationsConflictError(
                "candidate promotion conflicts with an existing record"
            ) from exc
        # Re-read after the atomic candidate projection update so nested
        # source-candidate data reflects the final ``promoted`` status rather
        # than the pre-transition accepted snapshot used to build the draft.
        return await self.get_content_item(session, actor, content_item_id)

    def _build_program_revision(
        self,
        *,
        program: EditorialProgram,
        version: int,
        content: Mapping[str, Any],
        actor_id: UUID,
        idempotency_key: str | None,
    ) -> EditorialProgramRevision:
        return EditorialProgramRevision(
            id=uuid4(),
            editorial_program_id=program.id,
            owner_user_id=program.owner_user_id,
            project_id=program.project_id,
            version=version,
            name=content["name"],
            objective=content["objective"],
            content_type=content["content_type"],
            cadence=content["cadence"],
            target_platforms_json=list(content["target_platforms"]),
            target_account_refs_json=list(content["target_account_refs"]),
            content_pillar=content["content_pillar"],
            required_resources_json=list(content["required_resources"]),
            default_creative_recipe_ref=content["default_creative_recipe_ref"],
            default_qa_policy_ref=content["default_qa_policy_ref"],
            experiment_ref=content["experiment_ref"],
            draft_generation_policy=content["draft_generation_policy"],
            content_hash=sha256_json(content),
            idempotency_key=idempotency_key,
            created_by=actor_id,
        )

    async def _program_detail(
        self,
        session: Any,
        program: EditorialProgram,
    ) -> dict[str, Any]:
        revisions = await self._scalars(
            session,
            select(EditorialProgramRevision)
            .where(
                EditorialProgramRevision.editorial_program_id
                == program.id
            )
            .order_by(
                EditorialProgramRevision.version.desc(),
                EditorialProgramRevision.id.desc(),
            )
            .limit(101),
        )
        if not revisions:
            raise MediaOperationsConflictError(
                "EditorialProgram revision history is incomplete"
            )

        visible = revisions[:100]
        return {
            **program.to_safe_dict(),
            "current_revision": visible[0].to_safe_dict(),
            "revisions": [
                revision.to_safe_dict()
                for revision in visible
            ],
            "revision_history_truncated": len(revisions) > 100,
        }

    async def _program_summary_map(
        self,
        session: Any,
        programs: Sequence[EditorialProgram],
    ) -> dict[UUID, dict[str, Any]]:
        if not programs:
            return {}

        ids = [program.id for program in programs]
        revisions = await self._scalars(
            session,
            select(EditorialProgramRevision)
            .where(
                EditorialProgramRevision.editorial_program_id.in_(ids)
            )
            .order_by(
                EditorialProgramRevision.editorial_program_id.asc(),
                EditorialProgramRevision.version.desc(),
            ),
        )
        latest: dict[UUID, EditorialProgramRevision] = {}
        for revision in revisions:
            latest.setdefault(revision.editorial_program_id, revision)

        result: dict[UUID, dict[str, Any]] = {}
        for program in programs:
            revision = latest.get(program.id)
            if revision is None:
                raise MediaOperationsConflictError(
                    "EditorialProgram revision history is incomplete"
                )
            result[program.id] = {
                **program.to_safe_dict(),
                "current_revision": revision.to_safe_dict(),
            }
        return result

    async def create_editorial_program(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        persona_id: UUID | str,
        name: Any,
        objective: Any,
        content_type: Any = "article",
        cadence: Any = "manual",
        target_platforms: Any = None,
        target_account_refs: Any = None,
        content_pillar: Any = None,
        required_resources: Any = None,
        default_creative_recipe_ref: Any = None,
        default_qa_policy_ref: Any = None,
        experiment_ref: Any = None,
        draft_generation_policy: Any = "human_review",
        state: Any = "draft",
        enabled: Any = False,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        persona = await self._get_row(
            session,
            Persona,
            persona_id,
            "persona_id",
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            persona,
            permission="write",
        )
        key = _idempotency_key(idempotency_key)
        content = _normalize_program_revision(
            name=name,
            objective=objective,
            content_type=content_type,
            cadence=cadence,
            target_platforms=target_platforms,
            target_account_refs=target_account_refs,
            content_pillar=content_pillar,
            required_resources=required_resources,
            default_creative_recipe_ref=default_creative_recipe_ref,
            default_qa_policy_ref=default_qa_policy_ref,
            experiment_ref=experiment_ref,
            draft_generation_policy=draft_generation_policy,
        )
        create_hash = sha256_json(
            {
                "persona_id": str(persona.id),
                "project_id": (
                    str(persona.project_id)
                    if persona.project_id is not None
                    else None
                ),
                "revision": content,
            }
        )

        existing = await self._find_scoped_idempotency(
            session,
            EditorialProgram,
            owner_user_id=actor_id,
            project_id=persona.project_id,
            idempotency_key=key,
        )
        if existing is not None:
            if existing.create_hash != create_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different EditorialProgram payload"
                )
            return await self._program_detail(session, existing)

        program_state = _normalize_state(state, "state")
        if not isinstance(enabled, bool):
            raise MediaOperationsValidationError("enabled must be a boolean")
        program = EditorialProgram(
            id=uuid4(),
            owner_user_id=actor_id,
            project_id=persona.project_id,
            persona_id=persona.id,
            state=program_state,
            enabled=enabled,
            next_due_at=_next_due(content["cadence"]) if enabled and program_state == "active" else None,
            create_hash=create_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        revision = self._build_program_revision(
            program=program,
            version=1,
            content=content,
            actor_id=actor_id,
            idempotency_key=None,
        )

        session.add(program)
        session.add(revision)

        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._find_scoped_idempotency(
                session,
                EditorialProgram,
                owner_user_id=actor_id,
                project_id=persona.project_id,
                idempotency_key=key,
            )
            if recovered is not None and recovered.create_hash == create_hash:
                return await self._program_detail(session, recovered)
            raise MediaOperationsConflictError(
                "EditorialProgram conflicts with an existing record"
            ) from exc

        return await self._program_detail(session, program)

    async def list_editorial_programs(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        page_limit, page_offset = _bounded_page(limit, offset)

        condition = await self._scope_condition(
            session,
            actor,
            EditorialProgram,
            project_id=project_uuid,
        )

        programs = await self._scalars(
            session,
            select(EditorialProgram)
            .where(condition)
            .order_by(
                EditorialProgram.created_at.desc(),
                EditorialProgram.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset),
        )

        summaries = await self._program_summary_map(session, programs)
        return [summaries[program.id] for program in programs]

    async def list_due_editorial_programs(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        as_of: Any = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Inspect active editorial programs that are due for a draft."""

        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        cutoff = _parse_datetime(as_of, "as_of") or datetime.utcnow()
        page_limit, _ = _bounded_page(limit, 0)
        condition = await self._scope_condition(
            session,
            actor,
            EditorialProgram,
            project_id=project_uuid,
        )
        rows = await self._scalars(
            session,
            select(EditorialProgram)
            .where(
                condition,
                EditorialProgram.enabled.is_(True),
                EditorialProgram.state == "active",
                EditorialProgram.next_due_at.is_not(None),
                EditorialProgram.next_due_at <= cutoff,
            )
            .order_by(EditorialProgram.next_due_at.asc(), EditorialProgram.id.asc())
            .limit(page_limit),
        )
        summaries = await self._program_summary_map(session, rows)
        return [summaries[row.id] for row in rows]

    async def get_editorial_program(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        program_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or program_id is None:
            raise MediaOperationsValidationError(
                "actor and editorial_program_id are required"
            )

        program = await self._get_row(
            session,
            EditorialProgram,
            program_id,
            "editorial_program_id",
        )
        await self._assert_entity_access(
            session,
            actor,
            program,
            permission="read",
        )
        return await self._program_detail(session, program)

    async def append_editorial_program_revision(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        program_id: UUID | str | None = None,
        *,
        expected_version: Any,
        name: Any,
        objective: Any,
        content_type: Any = None,
        cadence: Any = None,
        target_platforms: Any = None,
        target_account_refs: Any = None,
        content_pillar: Any = None,
        required_resources: Any = None,
        default_creative_recipe_ref: Any = None,
        default_qa_policy_ref: Any = None,
        experiment_ref: Any = None,
        draft_generation_policy: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or program_id is None:
            raise MediaOperationsValidationError(
                "actor and editorial_program_id are required"
            )

        program = await self._get_row(
            session,
            EditorialProgram,
            program_id,
            "editorial_program_id",
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            program,
            permission="write",
        )
        key = _idempotency_key(idempotency_key)
        latest = await self._scalar(
            session,
            select(EditorialProgramRevision)
            .where(EditorialProgramRevision.editorial_program_id == program.id)
            .order_by(EditorialProgramRevision.version.desc(), EditorialProgramRevision.id.desc())
            .limit(1),
        )
        content = _normalize_program_revision(
            name=name,
            objective=objective,
            content_type=content_type if content_type is not None else getattr(latest, "content_type", "article"),
            cadence=cadence if cadence is not None else getattr(latest, "cadence", "manual"),
            target_platforms=target_platforms if target_platforms is not None else getattr(latest, "target_platforms_json", []),
            target_account_refs=target_account_refs if target_account_refs is not None else getattr(latest, "target_account_refs_json", []),
            content_pillar=content_pillar if content_pillar is not None else getattr(latest, "content_pillar", None),
            required_resources=required_resources if required_resources is not None else getattr(latest, "required_resources_json", []),
            default_creative_recipe_ref=default_creative_recipe_ref if default_creative_recipe_ref is not None else getattr(latest, "default_creative_recipe_ref", None),
            default_qa_policy_ref=default_qa_policy_ref if default_qa_policy_ref is not None else getattr(latest, "default_qa_policy_ref", None),
            experiment_ref=experiment_ref if experiment_ref is not None else getattr(latest, "experiment_ref", None),
            draft_generation_policy=draft_generation_policy if draft_generation_policy is not None else getattr(latest, "draft_generation_policy", "human_review"),
        )
        content_hash = sha256_json(content)

        existing = await self._scalar(
            session,
            select(EditorialProgramRevision)
            .where(
                EditorialProgramRevision.editorial_program_id
                == program.id,
                EditorialProgramRevision.idempotency_key == key,
            )
            .limit(1),
        )
        if existing is not None:
            if existing.content_hash != content_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with different EditorialProgram revision content"
                )
            return existing.to_safe_dict()

        current = int(
            await self._scalar(
                session,
                select(func.max(EditorialProgramRevision.version)).where(
                    EditorialProgramRevision.editorial_program_id
                    == program.id
                ),
            )
            or 0
        )
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "expected_version must be an integer"
            ) from exc
        if expected != current:
            raise MediaOperationsConflictError(
                "stale EditorialProgram version"
            )

        revision = self._build_program_revision(
            program=program,
            version=current + 1,
            content=content,
            actor_id=actor_id,
            idempotency_key=key,
        )
        session.add(revision)

        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(
                session,
                select(EditorialProgramRevision)
                .where(
                    EditorialProgramRevision.editorial_program_id
                    == program.id,
                    EditorialProgramRevision.idempotency_key == key,
                )
                .limit(1),
            )
            if recovered is not None and recovered.content_hash == content_hash:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError(
                "EditorialProgram revision changed concurrently"
            ) from exc

        return revision.to_safe_dict()

    async def _content_item_summary(
        self,
        session: Any,
        item: ContentItem,
    ) -> dict[str, Any]:
        links = await self._scalars(
            session,
            select(ContentItemFinding)
            .where(ContentItemFinding.content_item_id == item.id)
            .order_by(ContentItemFinding.ordinal.asc()),
        )
        return {
            **item.to_safe_dict(),
            "source_finding_ids": [
                str(link.finding_id)
                for link in links
            ],
            "source_candidate_ids": list(item.candidate_refs_json or []),
        }

    async def _content_item_detail(
        self,
        session: Any,
        actor: Any,
        item: ContentItem,
    ) -> dict[str, Any]:
        summary = await self._content_item_summary(session, item)
        findings: list[dict[str, Any]] = []

        for finding_id in summary["source_finding_ids"]:
            finding = await self._get_row(
                session,
                ResearchFinding,
                finding_id,
                "finding_id",
            )
            findings.append(
                await self._finding_detail(session, finding)
            )

        return {
            **summary,
            "source_findings": findings,
            "source_candidates": [
                await self.get_research_candidate(session, actor, candidate_id)
                for candidate_id in summary.get("source_candidate_ids", [])
            ],
        }

    async def create_content_item(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        editorial_program_id: UUID | str,
        title: Any,
        brief: Any,
        finding_ids: Any = None,
        candidate_ids: Any = None,
        persona_revision_id: UUID | str | None = None,
        objective: Any = None,
        content_type: Any = "article",
        content_pillar: Any = None,
        intended_audience: Any = None,
        source_refs: Any = None,
        desired_assets: Any = None,
        monetization_ref: Any = None,
        experiment_ref: Any = None,
        scheduled_at: Any = None,
        idempotency_key: Any,
        _commit_transaction: bool = True,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        program = await self._get_row(
            session,
            EditorialProgram,
            editorial_program_id,
            "editorial_program_id",
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            program,
            permission="write",
        )
        key = _idempotency_key(idempotency_key)
        title_value = _required_text(title, "title", 500)
        brief_value = _required_text(brief, "brief", 8000)
        parsed_finding_ids = _normalize_optional_ids(finding_ids, "finding_ids")
        parsed_candidate_ids = _normalize_optional_ids(candidate_ids, "candidate_ids")
        if not parsed_finding_ids and not parsed_candidate_ids:
            raise MediaOperationsValidationError(
                "at least one finding_id or candidate_id is required"
            )

        objective_value = _optional_text(objective, "objective", 4000)
        content_type_value = _required_text(content_type or "article", "content_type", 64)
        content_pillar_value = _optional_text(content_pillar, "content_pillar", 200)
        intended_audience_value = _optional_text(intended_audience, "intended_audience", 4000)
        source_refs_value = _normalize_text_list(source_refs, "source_refs", 20, 164)
        desired_assets_value = _normalize_text_list(desired_assets, "desired_assets", 20, 164)
        monetization_ref_value = _optional_text(monetization_ref, "monetization_ref", 164)
        experiment_ref_value = _optional_text(experiment_ref, "experiment_ref", 164)
        scheduled_value = _parse_datetime(scheduled_at, "scheduled_at")

        persona_revision = None
        if persona_revision_id not in (None, ""):
            persona_revision = await self._get_row(
                session, PersonaRevision, persona_revision_id, "persona_revision_id"
            )
            await self._assert_entity_access(session, actor, persona_revision, permission="read")
            if persona_revision.persona_id != program.persona_id:
                raise MediaOperationsValidationError(
                    "ContentItem Persona revision must belong to the EditorialProgram Persona"
                )
        else:
            persona_revision = await self._scalar(
                session,
                select(PersonaRevision)
                .where(PersonaRevision.persona_id == program.persona_id)
                .order_by(PersonaRevision.version.desc(), PersonaRevision.id.desc())
                .limit(1),
            )
        if persona_revision is None:
            raise MediaOperationsConflictError(
                "EditorialProgram Persona has no immutable Persona revision"
            )

        findings: list[ResearchFinding] = []
        for finding_id in parsed_finding_ids:
            finding = await self._get_row(
                session,
                ResearchFinding,
                finding_id,
                "finding_id",
            )
            await self._assert_entity_access(
                session,
                actor,
                finding,
                permission="read",
            )
            if finding.project_id != program.project_id:
                raise MediaOperationsValidationError(
                    "ContentItem and source Findings must share project scope"
                )
            finding_run = await self._scalar(
                session,
                select(ResearchRun).where(ResearchRun.id == finding.research_run_id).limit(1),
            )
            if finding_run is None:
                raise MediaOperationsConflictError("source Finding run is missing")
            finding_routine = await self._scalar(
                session,
                select(ResearchRoutine).where(ResearchRoutine.id == finding_run.research_routine_id).limit(1),
            )
            if finding_routine is None or (
                finding_routine.persona_id is not None
                and finding_routine.persona_id != program.persona_id
            ):
                raise MediaOperationsValidationError(
                    "ContentItem and source Findings must share Persona scope"
                )
            findings.append(finding)

        candidates: list[ResearchCandidate] = []
        for candidate_id in parsed_candidate_ids:
            candidate = await self._get_row(
                session, ResearchCandidate, candidate_id, "candidate_id"
            )
            await self._assert_entity_access(session, actor, candidate, permission="read")
            if candidate.project_id != program.project_id:
                raise MediaOperationsValidationError(
                    "ContentItem and source Candidates must share project scope"
                )
            candidate_routine = await self._scalar(
                session,
                select(ResearchRoutine).where(ResearchRoutine.id == candidate.research_routine_id).limit(1),
            )
            if candidate_routine is None or (
                candidate_routine.persona_id is not None
                and candidate_routine.persona_id != program.persona_id
            ):
                raise MediaOperationsValidationError(
                    "ContentItem and source Candidates must share Persona scope"
                )
            if candidate.status not in {"accepted", "promoted"}:
                raise MediaOperationsConflictError(
                    "only accepted candidates can be linked to a ContentItem"
                )
            candidates.append(candidate)

        content_hash = sha256_json(
            {
                "editorial_program_id": str(program.id),
                "title": title_value,
                "brief": brief_value,
                "finding_ids": [
                    str(value)
                    for value in parsed_finding_ids
                ],
                "candidate_ids": [str(value) for value in parsed_candidate_ids],
                "persona_revision_id": str(persona_revision.id) if persona_revision is not None else None,
                "objective": objective_value,
                "content_type": content_type_value,
                "content_pillar": content_pillar_value,
                "intended_audience": intended_audience_value,
                "source_refs": source_refs_value,
                "desired_assets": desired_assets_value,
                "monetization_ref": monetization_ref_value,
                "experiment_ref": experiment_ref_value,
                "scheduled_at": scheduled_value.isoformat() if scheduled_value else None,
            }
        )
        if bool(getattr(program, "enabled", False)) and getattr(program, "state", "draft") == "active":
            current_program_revision = await self._scalar(
                session,
                select(EditorialProgramRevision)
                .where(EditorialProgramRevision.editorial_program_id == program.id)
                .order_by(
                    EditorialProgramRevision.version.desc(),
                    EditorialProgramRevision.id.desc(),
                )
                .limit(1),
            )
            program.next_due_at = _next_due(
                getattr(current_program_revision, "cadence", "manual")
            )

        existing = await self._scalar(
            session,
            select(ContentItem)
            .where(
                ContentItem.editorial_program_id == program.id,
                ContentItem.idempotency_key == key,
            )
            .limit(1),
        )
        if existing is not None:
            if existing.content_hash != content_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different ContentItem payload"
                )
            return await self._content_item_detail(session, actor, existing)

        item = ContentItem(
            id=uuid4(),
            editorial_program_id=program.id,
            owner_user_id=actor_id,
            project_id=program.project_id,
            title=title_value,
            brief=brief_value,
            version=1,
            status="draft",
            persona_revision_id=persona_revision.id if persona_revision is not None else None,
            objective=objective_value,
            content_type=content_type_value,
            content_pillar=content_pillar_value,
            intended_audience=intended_audience_value,
            source_refs_json=source_refs_value,
            candidate_refs_json=[str(value) for value in parsed_candidate_ids],
            desired_assets_json=desired_assets_value,
            monetization_ref=monetization_ref_value,
            experiment_ref=experiment_ref_value,
            scheduled_at=scheduled_value,
            content_hash=content_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        session.add(item)

        for ordinal, finding in enumerate(findings, start=1):
            session.add(
                ContentItemFinding(
                    id=uuid4(),
                    content_item_id=item.id,
                    finding_id=finding.id,
                    owner_user_id=actor_id,
                    project_id=program.project_id,
                    ordinal=ordinal,
                )
            )

        try:
            if _commit_transaction:
                await self._flush_commit(session)
            else:
                await self._flush_only(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered_program = await self._get_row(
                session,
                EditorialProgram,
                editorial_program_id,
                "editorial_program_id",
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_program,
                permission="read",
            )
            recovered = await self._scalar(
                session,
                select(ContentItem)
                .where(
                    ContentItem.editorial_program_id
                    == recovered_program.id,
                    ContentItem.idempotency_key == key,
                )
                .limit(1),
            )
            if recovered is not None and recovered.content_hash == content_hash:
                return await self._content_item_detail(session, actor, recovered)
            raise MediaOperationsConflictError(
                "ContentItem conflicts with an existing record"
            ) from exc

        return await self._content_item_detail(session, actor, item)

    async def list_content_items(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        editorial_program_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        page_limit, page_offset = _bounded_page(limit, offset)

        conditions = [
            await self._scope_condition(
                session,
                actor,
                ContentItem,
                project_id=project_uuid,
            )
        ]

        if editorial_program_id is not None:
            program = await self._get_row(
                session,
                EditorialProgram,
                editorial_program_id,
                "editorial_program_id",
            )
            await self._assert_entity_access(
                session,
                actor,
                program,
                permission="read",
            )
            conditions.append(
                ContentItem.editorial_program_id == program.id
            )

        items = await self._scalars(
            session,
            select(ContentItem)
            .where(*conditions)
            .order_by(
                ContentItem.created_at.desc(),
                ContentItem.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset),
        )

        return [
            await self._content_item_summary(session, item)
            for item in items
        ]

    async def get_content_item(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        content_item_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or content_item_id is None:
            raise MediaOperationsValidationError(
                "actor and content_item_id are required"
            )

        item = await self._get_row(
            session,
            ContentItem,
            content_item_id,
            "content_item_id",
        )
        await self._assert_entity_access(
            session,
            actor,
            item,
            permission="read",
        )
        return await self._content_item_detail(session, actor, item)
