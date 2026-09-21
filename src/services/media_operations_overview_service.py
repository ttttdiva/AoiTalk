"""Read-only MediaOps Calendar and Results projections.

The operational workspace needs a compact, safe view over the append-only
MediaOps ledgers.  This service deliberately does not create a second source
of truth: calendar entries and result summaries are projections assembled from
the existing Persona, research, generation, content, publication, metrics and
learning tables.  Every query is scoped through the same owner/project ACL
boundary used by the mutation services, and provider payloads are never
returned.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select

from ..memory.models.media_operations import Persona, PersonaIntakeSlot, PersonaRevision
from ..memory.models.media_operations_content import (
    ContentVariant,
    ContentVariantRevision,
    QAAssessment,
    RightsAssessment,
)
from ..memory.models.media_operations_generation import (
    CreativeRecipe,
    CreativeRecipeRevision,
    GenerationPlan,
    GenerationRun,
)
from ..memory.models.media_operations_learning import LearningProposal
from ..memory.models.media_operations_metrics import (
    Experiment,
    ExperimentResult,
    MetricSnapshot,
    RevenueEvent,
)
from ..memory.models.media_operations_research import (
    ContentItem,
    EditorialProgram,
    ResearchCandidate,
    ResearchCandidateDecision,
    ResearchRoutine,
    ResearchRoutineRevision,
    ResearchRun,
)
from ..memory.models.media_operations_setup import PlatformAccount, PlatformAccountRevision
from ..memory.models.media_provider_capability import MediaProviderCapabilitySnapshot
from ..memory.models.operations import ExternalAction, ExternalActionAttempt, ExternalActionReceipt
from .media_operations_content_service import MediaOperationsContentService
from .media_operations_service import (
    MediaOperationsValidationError,
    _as_uuid,
    _bounded_page,
)


_MEDIA_ACTION_TYPES = frozenset(
    {
        "media.publish_content",
        "media.update_content",
        "media.delete_content",
        "media.release_product",
    }
)
_PLATFORMS = ("x", "pixiv", "dlsite", "patreon", "youtube", "instagram")
_METRIC_KEYS = frozenset(
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


def _metric_is_effective(row: Any) -> bool:
    """Return whether an immutable metric row is current evidence.

    The metrics write service may expose a richer effective-row helper in a
    later workstream.  This projection intentionally keeps a local fail-safe
    so dashboards never aggregate rows explicitly marked rejected or
    superseded when the optional helper is unavailable.
    """

    status = getattr(row, "ingestion_status", None)
    if status in (None, ""):
        status = getattr(row, "import_status", "accepted")
    return str(getattr(status, "value", status)).strip().lower() == "accepted"


def _effective_metric_rows(rows: list[Any]) -> list[Any]:
    """Project only unambiguous accepted heads of correction chains.

    Dashboard projections are intentionally read-only and should not issue a
    per-row async query.  Build the same linear authority graph in memory:
    rejected/superseded rows are excluded, accepted ancestors with an
    accepted child are excluded, and branching/cyclic/scope-mismatched chains
    fail closed instead of selecting an arbitrary timestamp winner.
    """

    # Lightweight projections/tests may provide legacy row-shaped objects
    # without an identity.  They cannot participate in a correction graph,
    # but their explicit accepted status is still safe to aggregate.
    if not any(getattr(row, "id", None) is not None for row in rows):
        return [row for row in rows if _metric_is_effective(row)]
    by_id = {str(getattr(row, "id", "")): row for row in rows if getattr(row, "id", None) is not None}
    accepted = {
        key: row for key, row in by_id.items() if _metric_is_effective(row)
    }
    children: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        parent_id = getattr(row, "correction_of_id", None)
        if parent_id is not None:
            children.setdefault(str(parent_id), []).append(row)

    ambiguous: set[str] = set()
    for parent_id, values in children.items():
        accepted_children = [item for item in values if _metric_is_effective(item)]
        if len(accepted_children) > 1:
            ambiguous.add(parent_id)
            ambiguous.update(str(getattr(item, "id", "")) for item in accepted_children)

    def same_scope(left: Any, right: Any) -> bool:
        fields = (
            "owner_user_id",
            "project_id",
            "persona_ref",
            "platform_account_ref",
            "content_variant_ref",
            "publication_ref",
            "provider",
            "period_start",
            "period_end",
        )
        return all(getattr(left, field, None) == getattr(right, field, None) for field in fields)

    def parent_valid(row: Any) -> bool:
        status = str(getattr(row, "ingestion_status", "") or "").strip().lower()
        return status in {"accepted", "superseded"}

    effective: list[Any] = []
    for key, row in accepted.items():
        if key in ambiguous:
            continue
        accepted_children = [item for item in children.get(key, []) if _metric_is_effective(item)]
        if accepted_children:
            # This row is an accepted correction ancestor; its child is the
            # sole effective head (unless ambiguity was already detected).
            continue
        parent_id = getattr(row, "correction_of_id", None)
        if parent_id is None:
            effective.append(row)
            continue
        parent_key = str(parent_id)
        parent = by_id.get(parent_key)
        if parent is None or not parent_valid(parent) or not same_scope(row, parent):
            # An accepted correction without a valid accepted parent is an
            # orphaned/malformed authority row.  Do not surface it as current.
            continue
        seen: set[str] = {key}
        cursor = parent
        malformed = False
        while getattr(cursor, "correction_of_id", None) is not None:
            cursor_key = str(getattr(cursor, "correction_of_id"))
            if cursor_key in seen:
                malformed = True
                break
            seen.add(cursor_key)
            next_parent = by_id.get(cursor_key)
            if next_parent is None or not parent_valid(next_parent) or not same_scope(row, next_parent):
                malformed = True
                break
            cursor = next_parent
        if not malformed:
            effective.append(row)
    return effective


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a persisted or payload datetime as a naive UTC datetime."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    # The metrics write path already applies a much tighter bound.  Keep this
    # guard for legacy rows so an old malformed row cannot poison a summary.
    if abs(number) > 1_000_000_000_000:
        return None
    return value


def _sum_number(left: int | float, right: int | float) -> int | float:
    value = left + right
    if isinstance(left, int) and isinstance(right, int):
        return int(value)
    return float(value)


def _bounded_text(value: Any, *, limit: int = 500) -> str | None:
    """Return a bounded human-readable string for calendar summaries."""

    if not isinstance(value, str):
        return None
    rendered = value.strip()
    if not rendered:
        return None
    return rendered[:limit]


def _variant_caption(payload: Any) -> str | None:
    """Extract only editorial caption-like fields from a variant payload."""

    if not isinstance(payload, Mapping):
        return None
    for key in ("caption", "summary", "description", "text", "body"):
        value = _bounded_text(payload.get(key))
        if value:
            return value
    return None


def _media_readiness(row: Any | None) -> dict[str, Any]:
    """Project media readiness without exposing generation/provider payloads."""

    if row is None:
        return {"status": "unknown", "ready": False, "output_count": 0}
    refs = getattr(row, "generation_output_refs_json", None)
    if refs is None:
        refs = getattr(row, "generation_output_refs", None)
    if isinstance(refs, (list, tuple, set)):
        count = sum(1 for item in refs if item not in (None, ""))
    else:
        count = 0
    payload = getattr(row, "payload_json", None)
    if count == 0 and isinstance(payload, Mapping):
        # Only count explicit opaque output/reference fields.  Arbitrary
        # provider payloads are not interpreted as readiness signals.
        for key in ("generation_output_refs", "output_refs", "asset_refs", "media_refs"):
            candidate = payload.get(key)
            if isinstance(candidate, (list, tuple, set)):
                count = sum(1 for item in candidate if item not in (None, ""))
                if count:
                    break
    if count:
        return {"status": "ready", "ready": True, "output_count": count}
    return {"status": "missing", "ready": False, "output_count": 0}


def _safe_account_projection(account: Any | None, revision: Any | None = None) -> dict[str, Any] | None:
    """Project a PlatformAccount target without credential or remote payloads."""

    if account is None:
        return None
    result: dict[str, Any] = {
        "id": str(getattr(account, "id", "")),
        "platform": getattr(account, "platform", None),
        "account_ref": getattr(account, "account_ref", None),
        "status": getattr(account, "status", "unknown"),
    }
    if revision is not None:
        result.update(
            {
                "revision_id": str(getattr(revision, "id", "")),
                "revision_version": int(getattr(revision, "version", 0) or 0),
                "credential_status": getattr(revision, "credential_status", "unknown"),
                "publish_capability": getattr(revision, "publish_capability", "unknown"),
                "media_capability": getattr(revision, "media_capability", "unknown"),
                "analytics_capability": getattr(revision, "analytics_capability", "unknown"),
            }
        )
    return result


def _safe_receipt_projection(receipt: Any | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "id": str(getattr(receipt, "id", "")),
        "action_id": str(getattr(receipt, "action_id", "")),
        "action_version": int(getattr(receipt, "action_version", 1) or 1),
        "confirmation_level": getattr(receipt, "confirmation_level", None),
        "remote_status": getattr(receipt, "remote_status", None),
        "provider_observed_at": _iso(_parse_datetime(getattr(receipt, "provider_observed_at", None))),
        "created_at": _iso(_parse_datetime(getattr(receipt, "created_at", None))),
    }


class MediaOperationsOverviewService(MediaOperationsContentService):
    """ACL-scoped, read-only projections for the primary MediaOps UI."""

    async def _rows(
        self,
        session: Any,
        actor: Any,
        model: Any,
        *,
        project_id: UUID | None,
        limit: int | None,
        order_column: Any | None = None,
    ) -> list[Any]:
        condition = await self._scope_condition(
            session,
            actor,
            model,
            project_id=project_id,
        )
        statement = select(model).where(condition)
        if order_column is not None:
            statement = statement.order_by(order_column.desc(), model.id.desc())
        else:
            statement = statement.order_by(model.id.desc())
        if limit is not None:
            statement = statement.limit(limit)
        return await self._scalars(session, statement)

    @staticmethod
    def _within(value: datetime | None, start: datetime | None, end: datetime | None) -> bool:
        if value is None:
            return False
        return (start is None or value >= start) and (end is None or value <= end)

    @staticmethod
    def _scheduled_at(row: Any) -> datetime | None:
        payload = getattr(row, "payload_json", None)
        if not isinstance(payload, Mapping):
            return None
        for key in ("scheduled_at", "schedule_at", "scheduled_time"):
            parsed = _parse_datetime(payload.get(key))
            if parsed is not None:
                return parsed
        return None

    async def _persona_labels(
        self,
        session: Any,
        actor: Any,
        *,
        project_id: UUID | None,
        persona_ids: set[UUID],
    ) -> dict[UUID, str]:
        if not persona_ids:
            return {}
        condition = await self._scope_condition(
            session,
            actor,
            Persona,
            project_id=project_id,
        )
        rows = await self._scalars(
            session,
            select(Persona).where(condition, Persona.id.in_(persona_ids)),
        )
        # PersonaRevision carries its own owner/project ACL columns.  Do not
        # query it by id alone: a caller can otherwise supply a revision UUID
        # from another project and have its display label projected into an
        # otherwise authorized calendar/results response.
        revision_condition = await self._scope_condition(
            session,
            actor,
            PersonaRevision,
            project_id=project_id,
        )
        revision_targets = [
            PersonaRevision.persona_id.in_([row.id for row in rows])
            if rows
            else PersonaRevision.persona_id.is_(None),
            PersonaRevision.id.in_(persona_ids),
        ]
        revisions = await self._scalars(
            session,
            select(PersonaRevision)
            .where(revision_condition, or_(*revision_targets))
            .order_by(PersonaRevision.persona_id.asc(), PersonaRevision.version.desc()),
        )
        if not rows and revisions:
            persona_ids_from_revisions = {revision.persona_id for revision in revisions}
            rows = await self._scalars(
                session,
                select(Persona).where(condition, Persona.id.in_(persona_ids_from_revisions)),
            )
        latest: dict[UUID, PersonaRevision] = {}
        for revision in revisions:
            latest.setdefault(revision.persona_id, revision)
        labels = {
            row.id: (latest.get(row.id).display_name if latest.get(row.id) else "Persona")
            for row in rows
        }
        # Variant events carry a PersonaRevision reference while editorial
        # events carry the stable Persona reference.  Return both keys so the
        # public projection can resolve either without exposing internals.
        for revision in revisions:
            persona = labels.get(revision.persona_id)
            if persona:
                labels[revision.id] = persona
        return labels

    async def get_calendar(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        start: Any = None,
        end: Any = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        page_limit, page_offset = _bounded_page(limit, offset)
        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        if start not in (None, "") and start_dt is None:
            raise MediaOperationsValidationError("start must be an ISO datetime")
        if end not in (None, "") and end_dt is None:
            raise MediaOperationsValidationError("end must be an ISO datetime")
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            raise MediaOperationsValidationError("end must be after start")

        # A bounded default window keeps the projection useful without making
        # a full historical scan.  Explicit windows are respected exactly.
        now = datetime.utcnow()
        start_dt = start_dt if start_dt is not None else now - timedelta(days=30)
        end_dt = end_dt if end_dt is not None else now + timedelta(days=90)
        fetch_limit = min(1000, max(200, (page_limit + page_offset) * 4))
        events: list[dict[str, Any]] = []
        persona_ids: set[UUID] = set()

        variants = await self._rows(
            session,
            actor,
            ContentVariantRevision,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=ContentVariantRevision.created_at,
        )
        latest_variant_ids: set[UUID] = set()
        for row in variants:
            if row.content_variant_id in latest_variant_ids:
                continue
            latest_variant_ids.add(row.content_variant_id)
            when = self._scheduled_at(row) or _parse_datetime(row.created_at)
            if not self._within(when, start_dt, end_dt):
                continue
            persona_ids.add(row.persona_revision_id)
            has_schedule = self._scheduled_at(row) is not None
            events.append(
                {
                    "id": str(row.id),
                    "source": "content_variant",
                    "kind": "publication" if has_schedule else "editorial_draft",
                    "title": f"{row.platform} コンテンツ",
                    "starts_at": _iso(when),
                    "ends_at": None,
                    "status": "scheduled" if has_schedule else "draft",
                    "platform": row.platform,
                    "persona_ref": str(row.persona_revision_id),
                    "requires_human_action": not has_schedule,
                    "reference": {"type": "content_variant_revision", "id": str(row.id)},
                }
            )

        actions = await self._rows(
            session,
            actor,
            ExternalAction,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=ExternalAction.created_at,
        )
        for row in actions:
            if row.action_type not in _MEDIA_ACTION_TYPES:
                continue
            when = self._scheduled_at(row) or _parse_datetime(row.created_at)
            if not self._within(when, start_dt, end_dt):
                continue
            if getattr(row, "persona_revision_id", None):
                try:
                    persona_ids.add(UUID(str(row.persona_revision_id)))
                except (TypeError, ValueError):
                    pass
            events.append(
                {
                    "id": str(row.id),
                    "source": "publication_action",
                    "kind": "publication",
                    "title": row.action_type.replace("media.", "").replace("_", " "),
                    "starts_at": _iso(when),
                    "ends_at": None,
                    "status": row.status,
                    "platform": getattr(row, "platform", None),
                    "persona_ref": str(row.persona_revision_id) if getattr(row, "persona_revision_id", None) else None,
                    "requires_human_action": row.status in {"proposed", "pending", "approved", "uncertain"},
                    "reference": {"type": "external_action", "id": str(row.id)},
                }
            )

        generation_runs = await self._rows(
            session,
            actor,
            GenerationRun,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=GenerationRun.created_at,
        )
        for row in generation_runs:
            when = _parse_datetime(row.started_at) or _parse_datetime(row.created_at)
            if not self._within(when, start_dt, end_dt):
                continue
            events.append(
                {
                    "id": str(row.id),
                    "source": "generation_run",
                    "kind": "generation",
                    "title": "生成ラン",
                    "starts_at": _iso(when),
                    "ends_at": _iso(_parse_datetime(row.finished_at)),
                    "status": row.status,
                    "platform": None,
                    "persona_ref": None,
                    "requires_human_action": row.status in {"failed", "uncertain", "output_pending"},
                    "reference": {"type": "generation_run", "id": str(row.id)},
                }
            )

        for model, kind, title, source, status_getter, human_statuses in (
            (ResearchRun, "research", "調査ラン", "research_run", lambda row: "recorded", frozenset()),
            (ContentItem, "editorial_draft", "コンテンツ案", "content_item", lambda row: "draft", frozenset({"draft"})),
            (ResearchRoutine, "research", "調査ルーチン", "research_routine", lambda row: "configured", frozenset()),
            (EditorialProgram, "editorial_draft", "編集プログラム", "editorial_program", lambda row: "configured", frozenset()),
        ):
            rows = await self._rows(
                session,
                actor,
                model,
                project_id=project_uuid,
                limit=fetch_limit,
                order_column=model.created_at,
            )
            for row in rows:
                when = _parse_datetime(getattr(row, "created_at", None))
                if not self._within(when, start_dt, end_dt):
                    continue
                if model is EditorialProgram:
                    persona_ids.add(row.persona_id)
                events.append(
                    {
                        "id": str(row.id),
                        "source": source,
                        "kind": kind,
                        "title": getattr(row, "title", None) or getattr(row, "name", None) or title,
                        "starts_at": _iso(when),
                        "ends_at": None,
                        "status": status_getter(row),
                        "platform": None,
                        "persona_ref": str(row.persona_id) if getattr(row, "persona_id", None) else None,
                        "requires_human_action": status_getter(row) in human_statuses,
                        "reference": {"type": source, "id": str(row.id)},
                    }
                )

        for model, kind, title, source in (
            (QAAssessment, "review", "QAレビュー", "qa_assessment"),
            (RightsAssessment, "review", "Rightsレビュー", "rights_assessment"),
            (LearningProposal, "review", "学習提案レビュー", "learning_proposal"),
        ):
            rows = await self._rows(
                session,
                actor,
                model,
                project_id=project_uuid,
                limit=fetch_limit,
                order_column=model.created_at,
            )
            for row in rows:
                when = _parse_datetime(getattr(row, "created_at", None))
                if not self._within(when, start_dt, end_dt):
                    continue
                result = str(getattr(row, "result", getattr(row, "status", "review_required")))
                events.append(
                    {
                        "id": str(row.id),
                        "source": source,
                        "kind": kind,
                        "title": title,
                        "starts_at": _iso(when),
                        "ends_at": None,
                        "status": result,
                        "platform": None,
                        "persona_ref": None,
                        "requires_human_action": result in {"failed", "blocked", "review_required", "pending_review"},
                        "reference": {"type": source, "id": str(row.id)},
                    }
                )

        # Hydrate each calendar entry from the authorized FK graph.  The
        # source ledgers remain append-only; this is a read-only projection
        # that adds account/content/review/approval/execution context without
        # returning raw variant or provider payloads.
        variant_stable_rows = await self._rows(
            session,
            actor,
            ContentVariant,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=ContentVariant.created_at,
        )
        variant_by_id = {str(getattr(row, "id", "")): row for row in variant_stable_rows}
        variant_revision_by_id = {
            str(getattr(row, "id", "")): row for row in variants
        }
        content_ids = {
            str(getattr(row, "content_item_id", ""))
            for row in variants
            if getattr(row, "content_item_id", None) is not None
        }
        content_ids.update(
            str(getattr(row, "content_item_id", ""))
            for row in variant_stable_rows
            if getattr(row, "content_item_id", None) is not None
        )
        content_rows = await self._rows(
            session,
            actor,
            ContentItem,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=ContentItem.created_at,
        )
        content_by_id = {
            str(getattr(row, "id", "")): row
            for row in content_rows
            if str(getattr(row, "id", "")) in content_ids
        }
        account_ids = {
            str(getattr(row, "platform_account_id", ""))
            for row in variants
            if getattr(row, "platform_account_id", None) is not None
        }
        account_ids.update(
            str(getattr(row, "platform_account_id", ""))
            for row in actions
            if getattr(row, "platform_account_id", None) is not None
        )
        account_rows = await self._rows(
            session,
            actor,
            PlatformAccount,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=PlatformAccount.created_at,
        )
        account_by_id = {
            str(getattr(row, "id", "")): row
            for row in account_rows
            if str(getattr(row, "id", "")) in account_ids
        }
        account_revision_rows = await self._rows(
            session,
            actor,
            PlatformAccountRevision,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=PlatformAccountRevision.created_at,
        )
        account_revision_by_id = {
            str(getattr(row, "id", "")): row for row in account_revision_rows
        }
        latest_account_revision: dict[str, Any] = {}
        for row in sorted(
            account_revision_rows,
            key=lambda item: (
                int(getattr(item, "version", 0) or 0),
                str(getattr(item, "created_at", "") or ""),
            ),
            reverse=True,
        ):
            latest_account_revision.setdefault(
                str(getattr(row, "platform_account_id", "")), row
            )

        variant_revision_ids = set(variant_revision_by_id)
        qa_rows = await self._rows(
            session,
            actor,
            QAAssessment,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=QAAssessment.created_at,
        )
        rights_rows = await self._rows(
            session,
            actor,
            RightsAssessment,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=RightsAssessment.created_at,
        )

        def latest_by_revision(rows_: list[Any]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for item in sorted(
                rows_,
                key=lambda row: str(getattr(row, "created_at", "") or ""),
                reverse=True,
            ):
                key = str(getattr(item, "content_variant_revision_id", ""))
                if key in variant_revision_ids:
                    result.setdefault(key, item)
            return result

        qa_by_revision = latest_by_revision(qa_rows)
        rights_by_revision = latest_by_revision(rights_rows)
        action_by_id = {str(getattr(row, "id", "")): row for row in actions}
        action_candidates: list[Any] = [
            row for row in actions if row.action_type in _MEDIA_ACTION_TYPES
        ]
        attempts = await self._rows(
            session,
            actor,
            ExternalActionAttempt,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=ExternalActionAttempt.started_at,
        )
        attempts_by_action: dict[str, list[Any]] = defaultdict(list)
        for attempt in attempts:
            attempts_by_action.setdefault(
                str(getattr(attempt, "action_id", "")), []
            ).append(attempt)
        receipts = await self._rows(
            session,
            actor,
            ExternalActionReceipt,
            project_id=project_uuid,
            limit=fetch_limit,
            order_column=ExternalActionReceipt.created_at,
        )
        receipts_by_action: dict[str, list[Any]] = defaultdict(list)
        for receipt in receipts:
            receipts_by_action.setdefault(
                str(getattr(receipt, "action_id", "")), []
            ).append(receipt)

        def action_is_coherent(action: Any) -> bool:
            """Reject media actions whose references cross the FK graph."""

            content_id = getattr(action, "content_item_id", None)
            variant_id = getattr(action, "content_variant_id", None)
            variant_revision_id = getattr(action, "content_variant_revision_id", None)
            persona_revision_id = getattr(action, "persona_revision_id", None)
            account_id = getattr(action, "platform_account_id", None)
            account_revision_id = getattr(action, "platform_account_revision_id", None)
            # Media actions must carry the complete immutable content graph;
            # a product/publication ref by itself is not enough to attribute a
            # calendar entry to a Character.
            if any(
                value is None
                for value in (
                    content_id,
                    variant_id,
                    variant_revision_id,
                    persona_revision_id,
                    account_id,
                )
            ):
                return False
            content = content_by_id.get(str(content_id))
            variant = variant_by_id.get(str(variant_id))
            variant_revision = variant_revision_by_id.get(str(variant_revision_id))
            if content is None or variant is None or variant_revision is None:
                return False
            if str(getattr(variant, "content_item_id", "")) != str(content_id):
                return False
            if str(getattr(variant_revision, "content_variant_id", "")) != str(variant_id):
                return False
            if str(getattr(variant_revision, "content_item_id", "")) != str(content_id):
                return False
            content_persona = getattr(content, "persona_revision_id", None)
            if content_persona is not None and str(content_persona) != str(persona_revision_id):
                return False
            if str(getattr(variant_revision, "persona_revision_id", "")) != str(persona_revision_id):
                return False
            if str(getattr(variant_revision, "platform_account_id", "")) != str(account_id):
                return False
            account = account_by_id.get(str(account_id))
            if account is None:
                return False
            connection_id = getattr(action, "connection_id", None)
            account_connection_id = getattr(account, "connection_id", None)
            if (
                connection_id is None
                or account_connection_id is None
                or str(connection_id) != str(account_connection_id)
            ):
                return False
            if str(getattr(account, "platform", "")) != str(getattr(action, "platform", "")):
                return False
            if account_revision_id is not None:
                account_revision = account_revision_by_id.get(str(account_revision_id))
                if account_revision is None or str(getattr(account_revision, "platform_account_id", "")) != str(account_id):
                    return False
            variant_platform = getattr(variant_revision, "platform", None)
            if variant_platform is not None and str(variant_platform) != str(getattr(action, "platform", "")):
                return False
            return True

        # Remove malformed media action entries before enriching the events;
        # engagement actions are not admitted by the media action allow-list
        # above and remain outside this calendar projection.
        coherent_action_ids = {
            str(getattr(action, "id", ""))
            for action in action_candidates
            if action_is_coherent(action)
        }
        events = [
            event
            for event in events
            if event.get("source") != "publication_action"
            or str(event.get("id")) in coherent_action_ids
        ]

        def matching_action(event: dict[str, Any]) -> Any | None:
            if event.get("source") == "publication_action":
                return action_by_id.get(str(event.get("id")))
            reference = event.get("reference") or {}
            variant_revision_id = str(reference.get("id", "")) if reference.get("type") == "content_variant_revision" else ""
            for action in action_candidates:
                if str(getattr(action, "content_variant_revision_id", "")) == variant_revision_id:
                    return action
            return None

        def related_variant(event: dict[str, Any]) -> Any | None:
            reference = event.get("reference") or {}
            if reference.get("type") == "content_variant_revision":
                return variant_revision_by_id.get(str(reference.get("id")))
            action = matching_action(event)
            if action is not None:
                return variant_revision_by_id.get(str(getattr(action, "content_variant_revision_id", "")))
            return None

        # Populate safe contextual fields for every event.  Generic research
        # and review events intentionally retain null context.
        for event in events:
            variant_revision = related_variant(event)
            action = matching_action(event)
            content_id = getattr(variant_revision, "content_item_id", None) if variant_revision is not None else None
            if content_id is None and action is not None:
                content_id = getattr(action, "content_item_id", None)
            content = content_by_id.get(str(content_id)) if content_id is not None else None
            account_id = getattr(variant_revision, "platform_account_id", None) if variant_revision is not None else None
            if account_id is None and action is not None:
                account_id = getattr(action, "platform_account_id", None)
            account = account_by_id.get(str(account_id)) if account_id is not None else None
            account_revision_id = getattr(variant_revision, "platform_account_revision_id", None) if variant_revision is not None else None
            if account_revision_id is None and action is not None:
                account_revision_id = getattr(action, "platform_account_revision_id", None)
            account_revision = account_revision_by_id.get(str(account_revision_id)) if account_revision_id is not None else None
            if account_revision is None and account is not None:
                account_revision = latest_account_revision.get(str(getattr(account, "id", "")))
            caption = _variant_caption(getattr(variant_revision, "payload_json", None)) if variant_revision is not None else None
            title = _bounded_text(getattr(content, "title", None)) if content is not None else None
            brief = _bounded_text(getattr(content, "brief", None)) if content is not None else None
            summary_parts = [part for part in (title, caption or brief) if part]
            if summary_parts:
                event["content_summary"] = " — ".join(summary_parts)[:1000]
            event["target_account"] = _safe_account_projection(account, account_revision)
            event["media_readiness"] = _media_readiness(variant_revision)
            event["qa_status"] = (
                str(getattr(qa_by_revision.get(str(getattr(variant_revision, "id", ""))), "result", "review_required"))
                if variant_revision is not None
                else None
            )
            event["rights_status"] = (
                str(getattr(rights_by_revision.get(str(getattr(variant_revision, "id", ""))), "result", "review_required"))
                if variant_revision is not None
                else None
            )
            event["approval_status"] = str(getattr(action, "status", "not_requested")) if action is not None else "not_requested"
            attempts_for_action = attempts_by_action.get(str(getattr(action, "id", "")), []) if action is not None else []
            latest_attempt = attempts_for_action[0] if attempts_for_action else None
            event["execution_status"] = str(getattr(latest_attempt, "status", "not_started")) if latest_attempt is not None else "not_started"
            receipts_for_action = receipts_by_action.get(str(getattr(action, "id", "")), []) if action is not None else []
            latest_receipt = receipts_for_action[0] if receipts_for_action else None
            event["receipt_status"] = (
                str(getattr(latest_receipt, "remote_status", None) or getattr(latest_receipt, "confirmation_level", "received"))
                if latest_receipt is not None
                else "none"
            )
            event["receipt"] = _safe_receipt_projection(latest_receipt)

        labels = await self._persona_labels(
            session,
            actor,
            project_id=project_uuid,
            persona_ids=persona_ids,
        )
        for event in events:
            raw = event.get("persona_ref")
            try:
                event["persona_label"] = labels.get(UUID(str(raw))) if raw else None
            except (TypeError, ValueError):
                event["persona_label"] = None
            event["character"] = event.get("persona_label")
            event.pop("persona_ref", None)

        events.sort(key=lambda item: (item.get("starts_at") or "", item.get("id") or ""), reverse=False)
        visible = events[page_offset : page_offset + page_limit]
        return {
            "start": _iso(start_dt),
            "end": _iso(end_dt),
            "items": visible,
            "total": len(events),
            "has_more": page_offset + page_limit < len(events),
        }

    async def get_results(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        start: Any = None,
        end: Any = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        if start not in (None, "") and start_dt is None:
            raise MediaOperationsValidationError("start must be an ISO datetime")
        if end not in (None, "") and end_dt is None:
            raise MediaOperationsValidationError("end must be an ISO datetime")
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            raise MediaOperationsValidationError("end must be after start")
        now = datetime.utcnow()
        start_dt = start_dt if start_dt is not None else now - timedelta(days=30)
        end_dt = end_dt if end_dt is not None else now

        snapshots = _effective_metric_rows(await self._rows(
            session,
            actor,
            MetricSnapshot,
            project_id=project_uuid,
            limit=None,
            order_column=MetricSnapshot.observed_at,
        ))
        platform_totals: dict[str, dict[str, Any]] = {
            platform: {"platform": platform, "snapshot_count": 0, "metrics": {}, "last_observed_at": None}
            for platform in _PLATFORMS
        }
        persona_totals: dict[str, dict[str, Any]] = {}
        persona_ids: set[UUID] = set()
        account_totals: dict[str, dict[str, Any]] = {}
        content_totals: dict[str, dict[str, Any]] = {}
        account_ids: set[UUID] = set()
        content_ids: set[UUID] = set()
        accepted_snapshots = 0
        evidence_count = 0
        for snapshot in snapshots:
            if not _metric_is_effective(snapshot):
                continue
            observed = _parse_datetime(snapshot.observed_at)
            if not self._within(observed, start_dt, end_dt):
                continue
            accepted_snapshots += 1
            evidence_count += len(snapshot.provenance or []) if isinstance(snapshot.provenance, list) else 0
            raw_platforms = snapshot.platform_metrics if isinstance(snapshot.platform_metrics, Mapping) else {}
            if not raw_platforms:
                raw_platforms = {"unassigned": snapshot.normalized_metrics if isinstance(snapshot.normalized_metrics, Mapping) else {}}
            for platform, raw_metrics in raw_platforms.items():
                key = str(platform).lower()
                if key not in platform_totals:
                    continue
                bucket = platform_totals[key]
                bucket["snapshot_count"] += 1
                if observed is not None and (bucket["last_observed_at"] is None or observed.isoformat() > bucket["last_observed_at"]):
                    bucket["last_observed_at"] = observed.isoformat()
                if not isinstance(raw_metrics, Mapping):
                    continue
                for metric, raw_value in raw_metrics.items():
                    if metric not in _METRIC_KEYS:
                        continue
                    number = _number(raw_value)
                    if number is not None:
                        bucket["metrics"][metric] = _sum_number(bucket["metrics"].get(metric, 0), number)
            if snapshot.persona_ref:
                key = str(snapshot.persona_ref)
                bucket = persona_totals.setdefault(key, {"persona_ref": key, "snapshot_count": 0, "metrics": {}})
                bucket["snapshot_count"] += 1
                try:
                    persona_ids.add(UUID(key))
                except (TypeError, ValueError):
                    # Metrics may legitimately refer to an external/opaque
                    # persona key.  Keep the stable reference, but do not
                    # attempt an ACL lookup for a non-UUID value.
                    pass
                raw_metrics = snapshot.normalized_metrics if isinstance(snapshot.normalized_metrics, Mapping) else {}
                for metric, raw_value in raw_metrics.items():
                    if metric not in _METRIC_KEYS:
                        continue
                    number = _number(raw_value)
                    if number is not None:
                        bucket["metrics"][metric] = _sum_number(bucket["metrics"].get(metric, 0), number)

            if snapshot.platform_account_ref:
                key = str(snapshot.platform_account_ref)
                bucket = account_totals.setdefault(
                    key,
                    {"account_ref": key, "snapshot_count": 0, "metrics": {}},
                )
                bucket["snapshot_count"] += 1
                try:
                    account_ids.add(UUID(key))
                except (TypeError, ValueError):
                    pass
                raw_metrics = snapshot.normalized_metrics if isinstance(snapshot.normalized_metrics, Mapping) else {}
                for metric, raw_value in raw_metrics.items():
                    if metric not in _METRIC_KEYS:
                        continue
                    number = _number(raw_value)
                    if number is not None:
                        bucket["metrics"][metric] = _sum_number(bucket["metrics"].get(metric, 0), number)

            if snapshot.content_variant_ref:
                key = str(snapshot.content_variant_ref)
                bucket = content_totals.setdefault(
                    key,
                    {"content_ref": key, "snapshot_count": 0, "metrics": {}},
                )
                bucket["snapshot_count"] += 1
                try:
                    content_ids.add(UUID(key))
                except (TypeError, ValueError):
                    pass
                raw_metrics = snapshot.normalized_metrics if isinstance(snapshot.normalized_metrics, Mapping) else {}
                for metric, raw_value in raw_metrics.items():
                    if metric not in _METRIC_KEYS:
                        continue
                    number = _number(raw_value)
                    if number is not None:
                        bucket["metrics"][metric] = _sum_number(bucket["metrics"].get(metric, 0), number)

        persona_labels = await self._persona_labels(
            session,
            actor,
            project_id=project_uuid,
            persona_ids=persona_ids,
        )
        for bucket in persona_totals.values():
            try:
                bucket["persona_label"] = persona_labels.get(UUID(str(bucket["persona_ref"])))
            except (TypeError, ValueError):
                bucket["persona_label"] = None

        account_condition = await self._scope_condition(
            session,
            actor,
            PlatformAccount,
            project_id=project_uuid,
        ) if account_ids else None
        account_labels: dict[UUID, str] = {}
        if account_condition is not None:
            account_rows = await self._scalars(
                session,
                select(PlatformAccount).where(account_condition, PlatformAccount.id.in_(account_ids)),
            )
            account_labels = {
                row.id: str(row.account_ref or row.platform or "Account")
                for row in account_rows
            }
        for bucket in account_totals.values():
            try:
                bucket["account_label"] = account_labels.get(UUID(str(bucket["account_ref"])))
            except (TypeError, ValueError):
                bucket["account_label"] = None

        content_labels: dict[UUID, str] = {}
        if content_ids:
            content_condition = await self._scope_condition(
                session,
                actor,
                ContentVariantRevision,
                project_id=project_uuid,
            )
            variant_rows = await self._scalars(
                session,
                select(ContentVariantRevision).where(
                    content_condition,
                    or_(
                        ContentVariantRevision.id.in_(content_ids),
                        ContentVariantRevision.content_variant_id.in_(content_ids),
                    ),
                ),
            )
            item_ids = {row.content_item_id for row in variant_rows if row.content_item_id}
            item_labels: dict[UUID, str] = {}
            if item_ids:
                item_condition = await self._scope_condition(
                    session,
                    actor,
                    ContentItem,
                    project_id=project_uuid,
                )
                item_rows = await self._scalars(
                    session,
                    select(ContentItem).where(item_condition, ContentItem.id.in_(item_ids)),
                )
                item_labels = {row.id: str(row.title or "Content") for row in item_rows}
            for row in variant_rows:
                label = item_labels.get(row.content_item_id) or f"{row.platform} variant"
                content_labels[row.id] = label
                content_labels[row.content_variant_id] = label
        for bucket in content_totals.values():
            try:
                bucket["content_label"] = content_labels.get(UUID(str(bucket["content_ref"])))
            except (TypeError, ValueError):
                bucket["content_label"] = None

        revenues = await self._rows(
            session,
            actor,
            RevenueEvent,
            project_id=project_uuid,
            limit=None,
            order_column=RevenueEvent.event_at,
        )
        revenue_by_currency: dict[str, dict[str, Any]] = {}
        revenue_by_platform: dict[str, dict[str, Any]] = {}
        revenue_by_account: dict[str, dict[str, Any]] = {}
        revenue_by_content: dict[str, dict[str, Any]] = {}
        revenue_by_product: dict[str, dict[str, Any]] = {}
        revenue_count = 0
        evidence_count += 0

        def add_revenue_bucket(
            collection: dict[str, dict[str, Any]],
            key: Any,
            field: str,
            event: Any,
        ) -> None:
            rendered = str(key) if key not in (None, "") else "unassigned"
            bucket = collection.setdefault(
                rendered,
                {
                    field: rendered,
                    "event_count": 0,
                    "gross": 0.0,
                    "net": 0.0,
                },
            )
            bucket["event_count"] += 1
            bucket["gross"] = float(bucket["gross"] + float(event.gross_amount or 0))
            bucket["net"] = float(bucket["net"] + float(event.net_amount or 0))

        for event in revenues:
            event_at = _parse_datetime(event.event_at)
            if not self._within(event_at, start_dt, end_dt):
                continue
            revenue_count += 1
            currency = str(event.currency or "").upper()
            add_revenue_bucket(revenue_by_currency, currency, "currency", event)
            add_revenue_bucket(revenue_by_platform, getattr(event, "platform", None), "platform", event)
            add_revenue_bucket(
                revenue_by_account,
                getattr(event, "platform_account_ref", None),
                "account_ref",
                event,
            )
            add_revenue_bucket(revenue_by_content, getattr(event, "content_ref", None), "content_ref", event)
            add_revenue_bucket(revenue_by_product, getattr(event, "product_ref", None), "product_ref", event)

        experiments = await self._rows(
            session,
            actor,
            Experiment,
            project_id=project_uuid,
            limit=None,
            order_column=Experiment.created_at,
        )
        experiment_statuses: dict[str, int] = defaultdict(int)
        experiment_ids: list[UUID] = []
        for experiment in experiments:
            created = _parse_datetime(experiment.created_at)
            if self._within(created, start_dt, end_dt):
                experiment_statuses[str(experiment.status)] += 1
                experiment_ids.append(experiment.id)
        result_count = 0
        if experiment_ids:
            result_condition = await self._scope_condition(
                session,
                actor,
                ExperimentResult,
                project_id=project_uuid,
            )
            result_rows = await self._scalars(
                session,
                select(ExperimentResult).where(
                    result_condition,
                    ExperimentResult.experiment_id.in_(experiment_ids),
                ),
            )
            result_count = sum(
                1
                for row in result_rows
                if self._within(_parse_datetime(row.created_at), start_dt, end_dt)
            )

        proposals = await self._rows(
            session,
            actor,
            LearningProposal,
            project_id=project_uuid,
            limit=None,
            order_column=LearningProposal.created_at,
        )
        learning_statuses: dict[str, int] = defaultdict(int)
        for proposal in proposals:
            if self._within(_parse_datetime(proposal.created_at), start_dt, end_dt):
                learning_statuses[str(proposal.status)] += 1

        return {
            "start": _iso(start_dt),
            "end": _iso(end_dt),
            "metric_snapshots": {
                "count": accepted_snapshots,
                "by_platform": [bucket for bucket in platform_totals.values() if bucket["snapshot_count"]],
                "by_persona": list(persona_totals.values()),
                "by_account": list(account_totals.values()),
                "by_content": list(content_totals.values()),
            },
            "revenue": {
                "event_count": revenue_count,
                "by_currency": list(revenue_by_currency.values()),
                "by_platform": list(revenue_by_platform.values()),
                "by_account": list(revenue_by_account.values()),
                "by_content": list(revenue_by_content.values()),
                "by_product": list(revenue_by_product.values()),
            },
            "experiments": {
                "count": sum(experiment_statuses.values()),
                "by_status": dict(experiment_statuses),
                "result_count": result_count,
            },
            "learning": {
                "count": sum(learning_statuses.values()),
                "by_status": dict(learning_statuses),
                "pending_review_count": learning_statuses.get("pending_review", 0),
            },
            "evidence_count": evidence_count,
        }

    async def get_character_dashboard(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        character_id: UUID | str | None = None,
        *,
        limit: int = 100,
        accounts_offset: int = 0,
        research_offset: int = 0,
        content_offset: int = 0,
        variants_offset: int = 0,
        recipes_offset: int = 0,
        plans_offset: int = 0,
        runs_offset: int = 0,
        qa_offset: int = 0,
        rights_offset: int = 0,
        publications_offset: int = 0,
        metrics_offset: int = 0,
        revenue_offset: int = 0,
        experiments_offset: int = 0,
        learning_offset: int = 0,
        calendar_offset: int = 0,
    ) -> dict[str, Any]:
        """Return an ACL-scoped projection for one slot-free Character.

        Child rows are admitted only when their persisted foreign keys point to
        the authorized Persona graph.  Opaque metric/learning references are
        resolved against those sets, never against the caller's UUID.
        """
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        if character_id is None:
            raise MediaOperationsValidationError("character_id is required")
        page_limit, _ = _bounded_page(limit, 0)
        offsets = {
            "accounts_offset": accounts_offset,
            "research_offset": research_offset,
            "content_offset": content_offset,
            "variants_offset": variants_offset,
            "recipes_offset": recipes_offset,
            "plans_offset": plans_offset,
            "runs_offset": runs_offset,
            "qa_offset": qa_offset,
            "rights_offset": rights_offset,
            "publications_offset": publications_offset,
            "metrics_offset": metrics_offset,
            "revenue_offset": revenue_offset,
            "experiments_offset": experiments_offset,
            "learning_offset": learning_offset,
            "calendar_offset": calendar_offset,
        }
        for name, value in offsets.items():
            if isinstance(value, bool):
                raise MediaOperationsValidationError(f"{name} must be a non-negative integer")
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise MediaOperationsValidationError(f"{name} must be a non-negative integer") from exc
            if value < 0:
                raise MediaOperationsValidationError(f"{name} must be a non-negative integer")
            offsets[name] = value

        persona = await self._get_persona_row(session, character_id)
        await self._assert_entity_access(session, actor, persona, permission="read")
        persona_id = persona.id
        project_uuid = getattr(persona, "project_id", None)
        owner_id = getattr(persona, "owner_user_id", None)

        def same(left: Any, right: Any) -> bool:
            return (left is None and right is None) or (
                left is not None and right is not None and str(left) == str(right)
            )

        def scope_ok(row: Any, *, project: bool = True) -> bool:
            return same(getattr(row, "owner_user_id", None), owner_id) and (
                not project or same(getattr(row, "project_id", None), project_uuid)
            )

        async def rows(model: Any, *where: Any, project: bool = True) -> list[Any]:
            # _scope_condition is the common project ACL boundary.  Receipt
            # rows have no project_id; still apply their owner boundary in SQL
            # rather than scanning every receipt and filtering only in Python.
            has_scope_columns = hasattr(model, "project_id") and hasattr(model, "owner_user_id")
            if has_scope_columns:
                condition = await self._scope_condition(
                    session, actor, model, project_id=project_uuid
                )
                statement = select(model).where(condition, *where)
            elif hasattr(model, "owner_user_id"):
                statement = select(model).where(model.owner_user_id == owner_id, *where)
            else:
                statement = select(model).where(*where)
            created = getattr(model, "created_at", None)
            ident = getattr(model, "id", None)
            if created is not None and ident is not None:
                statement = statement.order_by(created.desc(), ident.desc())
            elif ident is not None:
                statement = statement.order_by(ident.desc())
            return [
                row for row in await self._scalars(session, statement)
                if scope_ok(row, project=project)
            ]

        def ref(value: Any) -> str | None:
            return str(value) if value is not None else None

        def refs(values: Any) -> set[str]:
            if values is None:
                return set()
            if isinstance(values, (str, UUID)):
                return {str(values)}
            try:
                return {str(value) for value in values if value is not None}
            except TypeError:
                return {str(values)}

        def valid(value: Any, allowed: set[str]) -> bool:
            return value is None or str(value) in allowed

        def page(items: list[dict[str, Any]], key: str) -> dict[str, Any]:
            offset = offsets[key]
            count = len(items)
            return {
                "items": items[offset : offset + page_limit],
                "count": count,
                "limit": page_limit,
                "offset": offset,
                "has_more": offset + page_limit < count,
            }

        # Reject legacy intake slots: this endpoint is Character-only.
        slots = await self._scalars(
            session, select(PersonaIntakeSlot).where(PersonaIntakeSlot.persona_id == persona_id)
        )
        if slots:
            raise MediaOperationsValidationError("character must be slot-free")

        revision_rows = await rows(PersonaRevision, PersonaRevision.persona_id == persona_id)
        revision_rows = [row for row in revision_rows if same(getattr(row, "persona_id", None), persona_id)]
        revision_rows.sort(
            key=lambda row: (int(getattr(row, "version", 0) or 0), str(getattr(row, "id", ""))),
            reverse=True,
        )
        if not revision_rows:
            raise MediaOperationsValidationError("character revision history is incomplete")
        revision_ids = refs(row.id for row in revision_rows)
        character = {
            **persona.to_safe_dict(),
            "current_revision": revision_rows[0].to_safe_dict(),
            "revisions": [row.to_safe_dict() for row in revision_rows[:100]],
            "revision_history_truncated": len(revision_rows) > 100,
        }

        account_rows = [
            row for row in await rows(PlatformAccount, PlatformAccount.persona_id == persona_id)
            if same(getattr(row, "persona_id", None), persona_id)
        ]
        account_by_id = {ref(row.id): row for row in account_rows}
        account_ids = refs(row.id for row in account_rows)
        account_refs = refs(getattr(row, "account_ref", None) for row in account_rows)
        account_revision_rows = [
            row for row in await rows(PlatformAccountRevision)
            if ref(getattr(row, "platform_account_id", None)) in account_ids
        ]
        account_revision_ids = refs(row.id for row in account_revision_rows)
        account_revision_by_id = {ref(row.id): row for row in account_revision_rows}
        try:
            capability_snapshot_rows = await rows(MediaProviderCapabilitySnapshot)
        except Exception:
            # Older optional deployments may not have the WS05 table yet.  A
            # missing capability observation must degrade to ``unknown`` and
            # never make the Character dashboard unavailable.
            capability_snapshot_rows = []
        latest_capabilities_by_account: dict[str, dict[str, str]] = {}
        for snapshot in sorted(
            capability_snapshot_rows,
            key=lambda item: (
                str(getattr(item, "observed_at", "") or ""),
                str(getattr(item, "id", "")),
            ),
            reverse=True,
        ):
            account_key = ref(getattr(snapshot, "platform_account_id", None)) or ""
            if account_key not in account_ids:
                continue
            latest_capabilities_by_account.setdefault(account_key, {})
            latest_capabilities_by_account[account_key].setdefault(
                str(getattr(snapshot, "operation", "")),
                str(getattr(snapshot, "status", "unknown")),
            )
        latest_account_revision_by_account: dict[str, Any] = {}
        for revision in sorted(
            account_revision_rows,
            key=lambda item: (
                int(getattr(item, "version", 0) or 0),
                str(getattr(item, "created_at", "") or ""),
                str(getattr(item, "id", "")),
            ),
            reverse=True,
        ):
            latest_account_revision_by_account.setdefault(
                ref(getattr(revision, "platform_account_id", None)) or "",
                revision,
            )
        account_items = []
        for row in account_rows:
            account_revision = latest_account_revision_by_account.get(ref(row.id) or "")
            observed_capabilities = latest_capabilities_by_account.get(ref(row.id) or "", {})
            credential_status = str(
                getattr(account_revision, "credential_status", "unknown")
            ) if account_revision is not None else "unknown"
            account_items.append(
                {
                    "id": ref(row.id),
                    "platform": str(getattr(row, "platform", "")),
                    "account_ref": ref(getattr(row, "account_ref", None)),
                    "status": str(getattr(row, "status", "unknown")),
                    "connection_status": credential_status,
                    "capability_status": credential_status,
                    "capabilities": (
                        {
                            "publish": str(getattr(account_revision, "publish_capability", "unknown")),
                            "media": str(getattr(account_revision, "media_capability", "unknown")),
                            "analytics": str(getattr(account_revision, "analytics_capability", "unknown")),
                            "credential": credential_status,
                        }
                        if account_revision is not None
                        else {}
                    ),
                    "observed_capabilities": observed_capabilities,
                    # The checked-in registry intentionally has no verified
                    # provider adapter.  Do not infer readiness from account
                    # or credential presence.
                    "adapter_ready": False,
                    "account_revision_id": ref(getattr(account_revision, "id", None)),
                    "account_revision_version": (
                        int(getattr(account_revision, "version", 0) or 0)
                        if account_revision is not None
                        else None
                    ),
                }
            )

        routine_rows = [
            row for row in await rows(ResearchRoutine, ResearchRoutine.persona_id == persona_id)
            if same(getattr(row, "persona_id", None), persona_id)
        ]
        routine_ids = refs(row.id for row in routine_rows)
        routine_revision_rows = [
            row for row in await rows(ResearchRoutineRevision)
            if ref(getattr(row, "research_routine_id", None)) in routine_ids
        ]
        routine_revision_ids = refs(row.id for row in routine_revision_rows)
        routine_revision_by_id = {
            ref(row.id): row for row in routine_revision_rows
        }
        routine_names: dict[str, str] = {}
        for row in sorted(
            routine_revision_rows,
            key=lambda item: (int(getattr(item, "version", 0) or 0), str(getattr(item, "id", ""))),
            reverse=True,
        ):
            routine_names.setdefault(
                ref(getattr(row, "research_routine_id", None)) or "",
                str(getattr(row, "name", "Research run")),
            )
        research_run_rows = [
            row for row in await rows(ResearchRun)
            if ref(getattr(row, "research_routine_id", None)) in routine_ids
            and ref(getattr(row, "research_routine_revision_id", None)) in routine_revision_ids
            and ref(getattr(row, "research_routine_revision_id", None))
            in {
                ref(revision.id)
                for revision in routine_revision_rows
                if ref(getattr(revision, "research_routine_id", None))
                == ref(getattr(row, "research_routine_id", None))
                and str(getattr(revision, "content_hash", ""))
                == str(getattr(row, "routine_content_hash", ""))
            }
        ]
        research_run_ids = refs(row.id for row in research_run_rows)
        research_run_by_id = {ref(row.id): row for row in research_run_rows}

        program_rows = [
            row for row in await rows(EditorialProgram, EditorialProgram.persona_id == persona_id)
            if same(getattr(row, "persona_id", None), persona_id)
        ]
        program_ids = refs(row.id for row in program_rows)
        content_rows = [
            row for row in await rows(ContentItem)
            if ref(getattr(row, "editorial_program_id", None)) in program_ids
            and valid(getattr(row, "persona_revision_id", None), revision_ids)
        ]
        content_ids = refs(row.id for row in content_rows)
        content_by_id = {ref(row.id): row for row in content_rows}
        content_items = [
            {
                "id": ref(row.id),
                "title": str(getattr(row, "title", "")),
                "status": str(getattr(row, "status", "draft")),
                "scheduled_at": _iso(_parse_datetime(getattr(row, "scheduled_at", None))),
                "content_type": str(getattr(row, "content_type", "article")),
            }
            for row in content_rows
        ]
        candidate_rows = [
            row for row in await rows(ResearchCandidate)
            if ref(getattr(row, "research_routine_id", None)) in routine_ids
            and ref(getattr(row, "research_run_id", None)) in research_run_ids
            and ref(getattr(row, "routine_revision_id", None)) in routine_revision_ids
            and (
                (run := research_run_by_id.get(ref(getattr(row, "research_run_id", None))))
                is not None
            )
            and (
                revision := routine_revision_by_id.get(
                    ref(getattr(row, "routine_revision_id", None))
                )
            ) is not None
            and ref(getattr(run, "research_routine_id", None))
            == ref(getattr(row, "research_routine_id", None))
            and ref(getattr(run, "research_routine_revision_id", None))
            == ref(getattr(row, "routine_revision_id", None))
            and str(getattr(run, "routine_content_hash", ""))
            == str(getattr(revision, "content_hash", ""))
            and (
                getattr(row, "content_item_id", None) is None
                or valid(getattr(row, "content_item_id", None), content_ids)
            )
            and (
                str(getattr(row, "status", "")) != "promoted"
                or valid(getattr(row, "content_item_id", None), content_ids)
            )
        ]
        candidate_ids = refs(row.id for row in candidate_rows)
        candidate_decision_rows = [
            row
            for row in await rows(ResearchCandidateDecision)
            if ref(getattr(row, "candidate_id", None)) in candidate_ids
            and same(getattr(row, "owner_user_id", None), owner_id)
            and same(getattr(row, "project_id", None), project_uuid)
        ]
        latest_candidate_decision: dict[str, Any] = {}
        for row in sorted(
            candidate_decision_rows,
            key=lambda item: (
                int(getattr(item, "sequence", 0) or 0),
                str(getattr(item, "id", "")),
            ),
            reverse=True,
        ):
            latest_candidate_decision.setdefault(
                ref(getattr(row, "candidate_id", None)) or "",
                row,
            )

        def safe_decision(row: Any | None) -> dict[str, Any] | None:
            if row is None:
                return None
            projector = getattr(row, "to_safe_dict", None)
            if callable(projector):
                value = projector()
                if isinstance(value, Mapping):
                    # Explicit allow-list: candidate snapshots are audit-only
                    # and must not leak through dashboard projections.
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
                        "decided_at",
                        "created_at",
                    }
                    return {key: value[key] for key in allowed if key in value}
            return None

        candidate_items = [
            {
                "id": ref(row.id),
                "title": str(getattr(row, "title", "")),
                "summary": str(getattr(row, "summary", "") or ""),
                "status": str(getattr(row, "status", "discovered")),
                "review_state": (
                    "pending"
                    if str(getattr(row, "status", "")) in {"discovered", "triaged"}
                    else "accepted"
                    if str(getattr(row, "status", "")) in {"accepted", "promoted"}
                    else "rejected"
                    if str(getattr(row, "status", "")) == "rejected"
                    else "expired"
                ),
                "reason": getattr(row, "reason", None),
                "candidate_hash": str(getattr(row, "candidate_hash", "")),
                "decision_version": int(getattr(row, "decision_version", 0) or 0),
                "content_item_id": ref(getattr(row, "content_item_id", None)),
                "latest_decision": safe_decision(
                    latest_candidate_decision.get(ref(getattr(row, "id", None)) or "")
                ),
                "discovered_at": _iso(_parse_datetime(getattr(row, "discovered_at", None))),
                "expires_at": _iso(_parse_datetime(getattr(row, "expires_at", None))),
            }
            for row in candidate_rows
        ]

        variant_rows = [
            row for row in await rows(ContentVariant)
            if ref(getattr(row, "content_item_id", None)) in content_ids
        ]
        variant_ids = refs(row.id for row in variant_rows)
        variant_by_id = {ref(row.id): row for row in variant_rows}
        variant_revision_rows: list[Any] = []
        for row in await rows(ContentVariantRevision):
            variant_id = ref(getattr(row, "content_variant_id", None))
            stable = variant_by_id.get(variant_id)
            if stable is None or not same(
                getattr(row, "content_item_id", None), getattr(stable, "content_item_id", None)
            ):
                continue
            if not valid(getattr(row, "persona_revision_id", None), revision_ids):
                continue
            if not valid(getattr(row, "platform_account_id", None), account_ids):
                continue
            if not valid(getattr(row, "platform_account_revision_id", None), account_revision_ids):
                continue
            account_revision = account_revision_by_id.get(ref(getattr(row, "platform_account_revision_id", None)))
            if account_revision is not None and not same(
                getattr(account_revision, "platform_account_id", None),
                getattr(row, "platform_account_id", None),
            ):
                continue
            if getattr(stable, "platform", None) is not None and not same(
                getattr(row, "platform", None), getattr(stable, "platform", None)
            ):
                continue
            content = content_by_id.get(ref(getattr(row, "content_item_id", None)))
            if content is not None and getattr(row, "content_item_hash", None) and getattr(content, "content_hash", None):
                if not same(getattr(row, "content_item_hash", None), getattr(content, "content_hash", None)):
                    continue
            variant_revision_rows.append(row)
        variant_revision_ids = refs(row.id for row in variant_revision_rows)
        variant_revision_by_id = {ref(row.id): row for row in variant_revision_rows}
        variant_revisions_by_variant: dict[str, list[Any]] = defaultdict(list)
        for row in variant_revision_rows:
            variant_revisions_by_variant.setdefault(
                ref(getattr(row, "content_variant_id", None)) or "", []
            ).append(row)
        variant_items = [
            {
                "id": ref(row.id),
                "content_item_id": ref(getattr(row, "content_item_id", None)),
                "platform": str(getattr(row, "platform", "")),
                "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
            }
            for row in variant_rows
        ]
        variant_items += [
            {
                "id": ref(row.id),
                "content_variant_id": ref(getattr(row, "content_variant_id", None)),
                "content_item_id": ref(getattr(row, "content_item_id", None)),
                "platform": str(getattr(row, "platform", "")),
                "version": int(getattr(row, "version", 1) or 1),
                "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
            }
            for row in variant_revision_rows
        ]
        variant_ref_allowed = lambda value: str(value) in variant_ids or str(value) in variant_revision_ids

        qa_rows = [
            row for row in await rows(QAAssessment)
            if ref(getattr(row, "content_variant_id", None)) in variant_ids
            and ref(getattr(row, "content_variant_revision_id", None)) in variant_revision_ids
            and same(
                getattr(variant_revision_by_id.get(ref(getattr(row, "content_variant_revision_id", None))), "content_variant_id", None),
                getattr(row, "content_variant_id", None),
            )
        ]
        rights_rows = [
            row for row in await rows(RightsAssessment)
            if ref(getattr(row, "content_variant_id", None)) in variant_ids
            and ref(getattr(row, "content_variant_revision_id", None)) in variant_revision_ids
            and same(
                getattr(variant_revision_by_id.get(ref(getattr(row, "content_variant_revision_id", None))), "content_variant_id", None),
                getattr(row, "content_variant_id", None),
            )
        ]
        def assessment_payload(rows_: list[Any]) -> list[dict[str, Any]]:
            return [
                {
                    "id": ref(row.id),
                    "content_variant_id": ref(getattr(row, "content_variant_id", None)),
                    "content_variant_revision_id": ref(getattr(row, "content_variant_revision_id", None)),
                    "result": str(getattr(row, "result", "review_required")),
                    "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
                }
                for row in rows_
            ]
        qa_items, rights_items = assessment_payload(qa_rows), assessment_payload(rights_rows)

        recipe_rows = [
            row for row in await rows(CreativeRecipe, CreativeRecipe.persona_id == persona_id)
            if same(getattr(row, "persona_id", None), persona_id)
        ]
        recipe_ids = refs(row.id for row in recipe_rows)
        recipe_revision_rows = [
            row for row in await rows(CreativeRecipeRevision)
            if ref(getattr(row, "creative_recipe_id", None)) in recipe_ids
            and valid(getattr(row, "persona_revision_id", None), revision_ids)
        ]
        recipe_revision_ids = refs(row.id for row in recipe_revision_rows)
        recipe_items = [
            {
                "id": ref(row.id),
                "name": str(getattr(row, "name", "Untitled recipe")),
                "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
            }
            for row in recipe_rows
        ]
        plan_rows: list[Any] = []
        for row in await rows(GenerationPlan):
            persona_revision_id = ref(getattr(row, "persona_revision_id", None))
            content_item_id = ref(getattr(row, "content_item_id", None))
            content_variant_id = ref(getattr(row, "content_variant_id", None))
            recipe_revision_id = ref(getattr(row, "creative_recipe_revision_id", None))
            if persona_revision_id not in revision_ids:
                continue
            content = content_by_id.get(content_item_id or "")
            variant = variant_by_id.get(content_variant_id or "")
            if content is None or variant is None or recipe_revision_id not in recipe_revision_ids:
                continue
            # ContentItem -> ContentVariant must be an exact stable identity;
            # otherwise an opaque UUID coincidence can attribute another
            # Character's generation plan here.
            if ref(getattr(variant, "content_item_id", None)) != content_item_id:
                continue
            content_persona = ref(getattr(content, "persona_revision_id", None))
            if content_persona is not None and content_persona != persona_revision_id:
                continue
            matching_variant_revisions = [
                revision
                for revision in variant_revisions_by_variant.get(content_variant_id or "", [])
                if ref(getattr(revision, "content_item_id", None)) == content_item_id
                and ref(getattr(revision, "persona_revision_id", None)) == persona_revision_id
                and (
                    getattr(revision, "platform_account_id", None) is None
                    or ref(getattr(revision, "platform_account_id", None)) in account_ids
                )
                and (
                    getattr(revision, "platform_account_revision_id", None) is None
                    or ref(getattr(revision, "platform_account_revision_id", None)) in account_revision_ids
                )
            ]
            if not matching_variant_revisions:
                continue
            recipe_revision = next(
                (
                    revision
                    for revision in recipe_revision_rows
                    if ref(getattr(revision, "id", None)) == recipe_revision_id
                    and ref(getattr(revision, "creative_recipe_id", None)) in recipe_ids
                    and ref(getattr(revision, "persona_revision_id", None)) == persona_revision_id
                ),
                None,
            )
            if recipe_revision is None:
                continue
            plan_rows.append(row)
        plan_ids = refs(row.id for row in plan_rows)
        plan_items = [
            {
                "id": ref(row.id),
                "persona_revision_id": ref(getattr(row, "persona_revision_id", None)),
                "content_item_id": ref(getattr(row, "content_item_id", None)),
                "content_variant_id": ref(getattr(row, "content_variant_id", None)),
                "status": str(getattr(row, "status", "planned")),
                "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
            }
            for row in plan_rows
        ]
        run_rows = [
            row for row in await rows(GenerationRun)
            if ref(getattr(row, "plan_id", None)) in plan_ids
        ]
        run_items = [
            {
                "id": ref(row.id),
                "status": str(getattr(row, "status", "unknown")),
                "started_at": _iso(_parse_datetime(getattr(row, "started_at", None))),
                "finished_at": _iso(_parse_datetime(getattr(row, "finished_at", None))),
            }
            for row in run_rows
        ]

        action_rows = await rows(ExternalAction)
        allowed_actions: list[Any] = []
        for row in action_rows:
            if str(getattr(row, "action_type", "")) not in _MEDIA_ACTION_TYPES:
                continue
            # Engagement-only FK references cannot be attributed to a
            # Character media graph.
            if getattr(row, "opportunity_id", None) is not None or getattr(
                row, "application_draft_id", None
            ) is not None:
                continue
            checks = (
                (getattr(row, "content_item_id", None), content_ids),
                (getattr(row, "content_variant_id", None), variant_ids),
                (getattr(row, "content_variant_revision_id", None), variant_revision_ids),
                (getattr(row, "persona_revision_id", None), revision_ids),
                (getattr(row, "platform_account_id", None), account_ids),
                (getattr(row, "platform_account_revision_id", None), account_revision_ids),
            )
            # A media publication must carry every hop of the immutable
            # ContentItem -> Variant -> VariantRevision -> Persona/account
            # graph.  Accepting a partial set here makes a cross-pair UUID
            # look valid merely because each individual UUID belongs to this
            # Character.
            if any(value is None for value, _ in checks):
                continue
            if not all(valid(value, allowed) for value, allowed in checks):
                continue
            content = content_by_id.get(ref(getattr(row, "content_item_id", None)) or "")
            variant = variant_by_id.get(ref(getattr(row, "content_variant_id", None)) or "")
            variant_revision = variant_revision_by_id.get(
                ref(getattr(row, "content_variant_revision_id", None)) or ""
            )
            if content is None or variant is None or variant_revision is None:
                continue
            if ref(getattr(variant, "content_item_id", None)) != ref(getattr(row, "content_item_id", None)):
                continue
            if ref(getattr(variant_revision, "content_variant_id", None)) != ref(getattr(row, "content_variant_id", None)):
                continue
            if ref(getattr(variant_revision, "content_item_id", None)) != ref(getattr(row, "content_item_id", None)):
                continue
            content_persona = getattr(content, "persona_revision_id", None)
            if content_persona is not None and ref(content_persona) != ref(getattr(row, "persona_revision_id", None)):
                continue
            if ref(getattr(variant_revision, "persona_revision_id", None)) != ref(getattr(row, "persona_revision_id", None)):
                continue
            if ref(getattr(variant_revision, "platform_account_id", None)) != ref(getattr(row, "platform_account_id", None)):
                continue
            account = account_by_id.get(ref(getattr(row, "platform_account_id", None)) or "")
            if account is None:
                continue
            if str(getattr(account, "platform", "")) != str(getattr(row, "platform", "")):
                continue
            if str(getattr(variant_revision, "platform", "")) != str(getattr(row, "platform", "")):
                continue
            if getattr(row, "platform_account_revision_id", None):
                account_revision = account_revision_by_id.get(ref(getattr(row, "platform_account_revision_id", None)))
                if account_revision is None or ref(getattr(account_revision, "platform_account_id", None)) != ref(getattr(row, "platform_account_id", None)):
                    continue
                variant_account_revision = getattr(variant_revision, "platform_account_revision_id", None)
                if variant_account_revision is not None and ref(variant_account_revision) != ref(getattr(row, "platform_account_revision_id", None)):
                    continue
            connection_id = getattr(row, "connection_id", None)
            account_connection_id = getattr(account, "connection_id", None)
            if (
                connection_id is None
                or account_connection_id is None
                or str(connection_id) != str(account_connection_id)
            ):
                continue
            if getattr(row, "platform_account_revision_id", None):
                account_revision = account_revision_by_id.get(ref(getattr(row, "platform_account_revision_id", None)))
                if account_revision is None or not same(
                    getattr(account_revision, "platform_account_id", None),
                    getattr(row, "platform_account_id", None),
                ):
                    continue
            if getattr(row, "content_variant_revision_id", None) and getattr(row, "content_variant_id", None):
                variant_revision = variant_revision_by_id.get(ref(getattr(row, "content_variant_revision_id", None)))
                if variant_revision is None or not same(
                    getattr(variant_revision, "content_variant_id", None),
                    getattr(row, "content_variant_id", None),
                ):
                    continue
            allowed_actions.append(row)
        action_ids = refs(row.id for row in allowed_actions)
        receipt_rows = [
            row for row in await rows(ExternalActionReceipt, project=False)
            if ref(getattr(row, "action_id", None)) in action_ids
        ]
        receipts_by_action: dict[str, list[Any]] = defaultdict(list)
        for row in receipt_rows:
            receipts_by_action.setdefault(ref(getattr(row, "action_id", None)) or "", []).append(row)
        attempt_rows = [
            row
            for row in await rows(ExternalActionAttempt)
            if ref(getattr(row, "action_id", None)) in action_ids
        ]
        attempts_by_action: dict[str, list[Any]] = defaultdict(list)
        for row in attempt_rows:
            attempts_by_action.setdefault(ref(getattr(row, "action_id", None)) or "", []).append(row)
        for values in attempts_by_action.values():
            values.sort(
                key=lambda item: (
                    str(getattr(item, "started_at", "") or ""),
                    str(getattr(item, "id", "") or ""),
                ),
                reverse=True,
            )
        for values in receipts_by_action.values():
            values.sort(
                key=lambda item: (
                    str(getattr(item, "created_at", "") or ""),
                    str(getattr(item, "id", "") or ""),
                ),
                reverse=True,
            )
        publication_items = [
            {
                "id": ref(row.id),
                "action_type": str(getattr(row, "action_type", "")),
                "status": str(getattr(row, "status", "proposed")),
                "content_item_id": ref(getattr(row, "content_item_id", None)),
                "content_variant_id": ref(getattr(row, "content_variant_id", None)),
                "content_variant_revision_id": ref(getattr(row, "content_variant_revision_id", None)),
                "persona_revision_id": ref(getattr(row, "persona_revision_id", None)),
                "platform_account_id": ref(getattr(row, "platform_account_id", None)),
                "platform": getattr(row, "platform", None),
                "receipts": [
                    {
                        "id": ref(receipt.id),
                        "action_id": ref(receipt.action_id),
                        "action_version": int(getattr(receipt, "action_version", 1) or 1),
                        "confirmation_level": str(getattr(receipt, "confirmation_level", "human_confirmed")),
                        "remote_status": getattr(receipt, "remote_status", None),
                        "created_at": _iso(_parse_datetime(getattr(receipt, "created_at", None))),
                    }
                    for receipt in receipts_by_action.get(ref(row.id) or "", [])
                ],
                "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
            }
            for row in allowed_actions
        ]

        metric_rows = _effective_metric_rows(await rows(MetricSnapshot))
        allowed_metric_rows: list[Any] = []
        metric_items: list[dict[str, Any]] = []
        character_refs = {str(persona_id)} | revision_ids
        account_ref_set = account_ids | account_refs
        for row in metric_rows:
            if not _metric_is_effective(row):
                continue
            persona_ref, account_ref = getattr(row, "persona_ref", None), getattr(row, "platform_account_ref", None)
            variant_ref, publication_ref = getattr(row, "content_variant_ref", None), getattr(row, "publication_ref", None)
            if persona_ref is not None and str(persona_ref) not in character_refs:
                continue
            if account_ref is not None and str(account_ref) not in account_ref_set:
                continue
            if variant_ref is not None and not variant_ref_allowed(variant_ref):
                continue
            if publication_ref is not None and (
                str(publication_ref) not in action_ids or not any(value is not None for value in (persona_ref, account_ref, variant_ref))
            ):
                continue
            if not any(value is not None for value in (persona_ref, account_ref, variant_ref)):
                continue
            allowed_metric_rows.append(row)
            raw = getattr(row, "normalized_metrics", None)
            safe_metrics = {
                str(key): number
                for key, value in (raw.items() if isinstance(raw, Mapping) else [])
                if str(key) in _METRIC_KEYS and (number := _number(value)) is not None
            }
            metric_items.append(
                {
                    "id": ref(row.id),
                    "persona_ref": ref(persona_ref),
                    "platform_account_ref": ref(account_ref),
                    "content_variant_ref": ref(variant_ref),
                    "publication_ref": ref(publication_ref),
                    "observed_at": _iso(_parse_datetime(getattr(row, "observed_at", None))),
                    "metrics": safe_metrics,
                }
            )

        revenue_rows = await rows(RevenueEvent)
        revenue_items: list[dict[str, Any]] = []
        for row in revenue_rows:
            persona_ref, account_ref = getattr(row, "persona_ref", None), getattr(row, "platform_account_ref", None)
            content_ref, publication_ref = getattr(row, "content_ref", None), getattr(row, "publication_ref", None)
            if persona_ref is not None and str(persona_ref) not in character_refs:
                continue
            if account_ref is not None and str(account_ref) not in account_ref_set:
                continue
            if content_ref is not None and str(content_ref) not in (content_ids | variant_ids | variant_revision_ids):
                continue
            if publication_ref is not None and (
                str(publication_ref) not in action_ids or not any(value is not None for value in (persona_ref, account_ref, content_ref))
            ):
                continue
            product_ref = getattr(row, "product_ref", None)
            # A product-only event is not attributable to this Character.  A
            # product reference attached to an authorized persona/account/
            # content/publication event is safe and must remain visible.
            if not any(value is not None for value in (persona_ref, account_ref, content_ref, publication_ref)):
                continue
            revenue_items.append(
                {
                    "id": ref(row.id),
                    "persona_ref": ref(persona_ref),
                    "platform_account_ref": ref(account_ref),
                    "content_ref": ref(content_ref),
                    "publication_ref": ref(publication_ref),
                    "product_ref": ref(product_ref),
                    "platform": getattr(row, "platform", None),
                    "currency": str(getattr(row, "currency", "")),
                    "gross_amount": float(getattr(row, "gross_amount", 0) or 0),
                    "net_amount": float(getattr(row, "net_amount", 0) or 0),
                    "event_at": _iso(_parse_datetime(getattr(row, "event_at", None))),
                }
            )

        def json_refs(value: Any) -> list[str]:
            if isinstance(value, Mapping):
                result: list[str] = []
                for key, item in value.items():
                    if str(key).lower() in {
                        "ref",
                        "id",
                        "variant_ref",
                        "variant_id",
                        "variant_refs",
                        "variant_ids",
                        "content_variant_ref",
                        "content_variant_refs",
                    }:
                        result.extend(json_refs(item))
                    elif isinstance(item, (Mapping, list, tuple, set)):
                        result.extend(json_refs(item))
                return result
            if isinstance(value, (list, tuple, set)):
                result: list[str] = []
                for item in value:
                    result.extend(json_refs(item))
                return result
            return [str(value)] if isinstance(value, (str, UUID)) else []

        experiment_rows = await rows(Experiment)
        experiment_result_rows = await rows(ExperimentResult)
        experiment_result_map: dict[str, list[Any]] = defaultdict(list)
        for result in experiment_result_rows:
            experiment_result_map.setdefault(ref(getattr(result, "experiment_id", None)) or "", []).append(result)
        experiment_items: list[dict[str, Any]] = []
        experiment_ids: set[str] = set()
        experiment_result_ids: set[str] = set()
        for row in experiment_rows:
            persona_refs, account_values = refs(getattr(row, "persona_refs", None)), refs(getattr(row, "account_refs", None))
            variant_values = set(json_refs(getattr(row, "variant_groups", None)))
            # Experiments can be variant-linked while their optional persona
            # and account arrays are intentionally empty.  The variant graph
            # is still sufficient to attribute the experiment to this
            # Character; requiring a persona/account value silently dropped
            # those rows from the dashboard.
            if not persona_refs and not account_values and not variant_values:
                continue
            if not persona_refs.issubset(character_refs) or not account_values.issubset(account_ref_set):
                continue
            if not variant_values.issubset(variant_ids | variant_revision_ids):
                continue
            if not (persona_refs or account_values or variant_values):
                continue
            result_items: list[dict[str, Any]] = []
            for result in experiment_result_map.get(ref(row.id) or "", []):
                winner = getattr(result, "winner_variant_ref", None)
                if winner is not None and not variant_ref_allowed(winner):
                    continue
                experiment_result_ids.add(ref(result.id) or "")
                result_items.append(
                    {
                        "id": ref(result.id),
                        "status": str(getattr(result, "status", "recorded")),
                        "sample_size": int(getattr(result, "sample_size", 0) or 0),
                        "winner_variant_ref": ref(winner),
                        "created_at": _iso(_parse_datetime(getattr(result, "created_at", None))),
                }
            )
            experiment_ids.add(ref(row.id) or "")
            experiment_items.append(
                {
                    "id": ref(row.id),
                    "name": str(getattr(row, "name", "")),
                    "status": str(getattr(row, "status", "draft")),
                    "persona_refs": sorted(persona_refs),
                    "account_refs": sorted(account_values),
                    "results": result_items,
                    "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
                }
            )

        learning_items: list[dict[str, Any]] = []
        learning_rows = await rows(LearningProposal)
        learning_sets = {
            "persona": {str(persona_id)},
            "character": {str(persona_id)},
            "account": account_ref_set,
            "platform_account": account_ref_set,
            "content": content_ids,
            "content_item": content_ids,
            # A learning proposal that targets a variant must use the stable
            # ContentVariant identity.  Revisions are immutable snapshots and
            # are deliberately not accepted as a ContentVariant subject ref.
            "content_variant": variant_ids,
            # Publication/action and experiment evidence may be referenced by
            # a proposal even when no content/persona field is repeated on
            # the proposal itself.  Permit only rows already admitted to this
            # Character's authorized graph.
            "publication": action_ids,
            "action": action_ids,
            "external_action": action_ids,
            "experiment": experiment_ids,
            "experiment_result": experiment_result_ids,
        }
        allowed_learning_rows: list[Any] = []
        for row in learning_rows:
            subject_type = str(getattr(row, "subject_type", "")).strip().lower()
            subject_ref = ref(getattr(row, "subject_ref", None))
            if not subject_ref or subject_ref not in learning_sets.get(subject_type, set()):
                continue
            allowed_learning_rows.append(row)
            learning_items.append(
                {
                    "id": ref(row.id),
                    "title": str(getattr(row, "title", "")),
                    "proposal_type": str(getattr(row, "proposal_type", "")),
                    "status": str(getattr(row, "status", "pending_review")),
                    "created_at": _iso(_parse_datetime(getattr(row, "created_at", None))),
                }
            )

        calendar_items: list[dict[str, Any]] = []
        character_label = _bounded_text(
            getattr(revision_rows[0], "display_name", None)
        ) or "Character"
        latest_variant_revision_by_variant: dict[str, Any] = {}
        for variant_revision in sorted(
            variant_revision_rows,
            key=lambda item: (
                int(getattr(item, "version", 0) or 0),
                str(getattr(item, "created_at", "") or ""),
            ),
            reverse=True,
        ):
            latest_variant_revision_by_variant.setdefault(
                ref(getattr(variant_revision, "content_variant_id", None)) or "",
                variant_revision,
            )
        plan_by_id = {ref(row.id): row for row in plan_rows}

        def dashboard_calendar_context(
            *,
            content: Any | None = None,
            variant_revision: Any | None = None,
            action: Any | None = None,
        ) -> dict[str, Any]:
            if content is None and variant_revision is not None:
                content = content_by_id.get(ref(getattr(variant_revision, "content_item_id", None)))
            if variant_revision is None and action is not None:
                variant_revision = variant_revision_by_id.get(
                    ref(getattr(action, "content_variant_revision_id", None)) or ""
                )
            if content is None and action is not None:
                content = content_by_id.get(ref(getattr(action, "content_item_id", None)))
            account_id = (
                getattr(variant_revision, "platform_account_id", None)
                if variant_revision is not None
                else None
            )
            if account_id is None and action is not None:
                account_id = getattr(action, "platform_account_id", None)
            account = account_by_id.get(ref(account_id) or "")
            account_revision = None
            revision_id = (
                getattr(variant_revision, "platform_account_revision_id", None)
                if variant_revision is not None
                else None
            )
            if revision_id is None and action is not None:
                revision_id = getattr(action, "platform_account_revision_id", None)
            if revision_id is not None:
                account_revision = account_revision_by_id.get(ref(revision_id) or "")
            if account_revision is None and account is not None:
                account_revision = latest_account_revision_by_account.get(ref(getattr(account, "id", None)) or "")
            related_action = action
            if related_action is None and variant_revision is not None:
                for candidate in allowed_actions:
                    if ref(getattr(candidate, "content_variant_revision_id", None)) == ref(getattr(variant_revision, "id", None)):
                        related_action = candidate
                        break
            action_receipts = receipts_by_action.get(ref(getattr(related_action, "id", None)) or "", []) if related_action is not None else []
            latest_receipt = action_receipts[0] if action_receipts else None
            platform = (
                getattr(variant_revision, "platform", None)
                if variant_revision is not None
                else getattr(related_action, "platform", None) if related_action is not None else None
            )
            summary_parts = [
                _bounded_text(getattr(content, "title", None)),
                _variant_caption(getattr(variant_revision, "payload_json", None)) if variant_revision is not None else _bounded_text(getattr(content, "brief", None)),
            ]
            return {
                "platform": platform,
                "character": character_label,
                "target_account": _safe_account_projection(account, account_revision),
                "content_summary": " — ".join(part for part in summary_parts if part)[:1000] or None,
                "media_readiness": _media_readiness(variant_revision),
                "qa_status": (
                    str(getattr(qa_by_variant_revision.get(ref(getattr(variant_revision, "id", None)) or ""), "result", "review_required"))
                    if variant_revision is not None
                    else None
                ),
                "rights_status": (
                    str(getattr(rights_by_variant_revision.get(ref(getattr(variant_revision, "id", None)) or ""), "result", "review_required"))
                    if variant_revision is not None
                    else None
                ),
                "approval_status": str(getattr(related_action, "status", "not_requested")) if related_action is not None else "not_requested",
                "execution_status": (
                    str(
                        getattr(
                            attempts_by_action.get(ref(getattr(related_action, "id", None)) or "", [None])[0],
                            "status",
                            "not_started",
                        )
                    )
                    if related_action is not None
                    else "not_started"
                ),
                "receipt_status": (
                    str(getattr(latest_receipt, "remote_status", None) or getattr(latest_receipt, "confirmation_level", "received"))
                    if latest_receipt is not None
                    else "none"
                ),
                "receipt": _safe_receipt_projection(latest_receipt),
            }

        # Normalise assessment rows to their latest immutable result for the
        # dashboard calendar context.
        def latest_assessment(rows_: list[Any]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for item in sorted(
                rows_,
                key=lambda row: str(getattr(row, "created_at", "") or ""),
                reverse=True,
            ):
                key = ref(getattr(item, "content_variant_revision_id", None)) or ""
                result.setdefault(key, item)
            return result

        qa_by_variant_revision = latest_assessment(qa_rows)
        rights_by_variant_revision = latest_assessment(rights_rows)
        for row in research_run_rows:
            calendar_items.append(
                {
                    "id": ref(row.id), "kind": "research",
                    "title": routine_names.get(ref(getattr(row, "research_routine_id", None)) or "", "Research run"),
                    "starts_at": _iso(_parse_datetime(getattr(row, "started_at", None)) or _parse_datetime(getattr(row, "created_at", None))),
                    "status": str(getattr(row, "status", "recorded")),
                    "character": character_label,
                }
            )
        for row in content_rows:
            variant_revision = latest_variant_revision_by_variant.get(
                next(
                    (
                        ref(variant.id)
                        for variant in variant_rows
                        if ref(getattr(variant, "content_item_id", None)) == ref(row.id)
                    ),
                    "",
                )
            )
            calendar_items.append(
                {
                    "id": ref(row.id), "kind": "content", "title": str(getattr(row, "title", "Content")),
                    "starts_at": _iso(_parse_datetime(getattr(row, "scheduled_at", None)) or _parse_datetime(getattr(row, "created_at", None))),
                    "status": str(getattr(row, "status", "draft")),
                    **dashboard_calendar_context(content=row, variant_revision=variant_revision),
                }
            )
        for row in run_rows:
            plan = plan_by_id.get(ref(getattr(row, "plan_id", None)))
            variant_revision = None
            if plan is not None:
                variant_revision = latest_variant_revision_by_variant.get(ref(getattr(plan, "content_variant_id", None)) or "")
            content = content_by_id.get(ref(getattr(plan, "content_item_id", None))) if plan is not None else None
            calendar_items.append(
                {
                    "id": ref(row.id), "kind": "generation", "title": "Generation run",
                    "starts_at": _iso(_parse_datetime(getattr(row, "started_at", None)) or _parse_datetime(getattr(row, "created_at", None))),
                    "status": str(getattr(row, "status", "unknown")),
                    **dashboard_calendar_context(content=content, variant_revision=variant_revision),
                }
            )
        for row in allowed_actions:
            calendar_items.append(
                {
                    "id": ref(row.id), "kind": "publication",
                    "title": str(getattr(row, "action_type", "publication")).replace("media.", ""),
                    "starts_at": _iso(self._scheduled_at(row) or _parse_datetime(getattr(row, "created_at", None))),
                    "status": str(getattr(row, "status", "proposed")),
                    **dashboard_calendar_context(action=row),
                }
            )
        calendar_items.sort(key=lambda item: (item.get("starts_at") or "", item.get("id") or ""), reverse=True)

        accounts_page = page(account_items, "accounts_offset")
        research_page = page(candidate_items, "research_offset")
        content_page = page(content_items, "content_offset")
        variants_page = page(variant_items, "variants_offset")
        qa_page, rights_page = page(qa_items, "qa_offset"), page(rights_items, "rights_offset")
        recipes_page, plans_page = page(recipe_items, "recipes_offset"), page(plan_items, "plans_offset")
        runs_page = page(run_items, "runs_offset")
        publications_page, metrics_page = page(publication_items, "publications_offset"), page(metric_items, "metrics_offset")
        revenue_page, experiments_page = page(revenue_items, "revenue_offset"), page(experiment_items, "experiments_offset")
        learning_page, calendar_page = page(learning_items, "learning_offset"), page(calendar_items, "calendar_offset")

        aggregate_metrics: dict[str, int | float] = {}
        last_observed_at: str | None = None
        for row in allowed_metric_rows:
            observed = _parse_datetime(getattr(row, "observed_at", None))
            rendered = _iso(observed)
            if rendered is not None and (last_observed_at is None or rendered > last_observed_at):
                last_observed_at = rendered
            raw = getattr(row, "normalized_metrics", None)
            for key, value in (raw.items() if isinstance(raw, Mapping) else []):
                if str(key) not in _METRIC_KEYS:
                    continue
                number = _number(value)
                if number is not None:
                    aggregate_metrics[str(key)] = _sum_number(aggregate_metrics.get(str(key), 0), number)

        revenue_dimensions: dict[str, dict[str, dict[str, Any]]] = {
            "currency": {},
            "platform": {},
            "account_ref": {},
            "content_ref": {},
            "product_ref": {},
        }

        def aggregate_revenue_dimension(
            dimension: str,
            value: Any,
            event: Mapping[str, Any],
        ) -> None:
            if value in (None, ""):
                key = "unassigned"
            else:
                key = str(value)
            bucket = revenue_dimensions[dimension].setdefault(
                key,
                {
                    dimension: key,
                    "event_count": 0,
                    "gross": 0.0,
                    "net": 0.0,
                },
            )
            bucket["event_count"] += 1
            bucket["gross"] = float(bucket["gross"] + float(event.get("gross_amount") or 0))
            bucket["net"] = float(bucket["net"] + float(event.get("net_amount") or 0))

        for event in revenue_items:
            aggregate_revenue_dimension("currency", event.get("currency"), event)
            aggregate_revenue_dimension("platform", event.get("platform"), event)
            aggregate_revenue_dimension("account_ref", event.get("platform_account_ref"), event)
            aggregate_revenue_dimension("content_ref", event.get("content_ref"), event)
            aggregate_revenue_dimension("product_ref", event.get("product_ref"), event)
        revenue_summary = {
            "event_count": len(revenue_items),
            "by_currency": list(revenue_dimensions["currency"].values()),
            "by_platform": list(revenue_dimensions["platform"].values()),
            "by_account": list(revenue_dimensions["account_ref"].values()),
            "by_content": list(revenue_dimensions["content_ref"].values()),
            "by_product": list(revenue_dimensions["product_ref"].values()),
        }

        pagination = {
            "accounts": accounts_page, "research": research_page, "content": content_page,
            "variants": variants_page, "qa": qa_page, "rights": rights_page,
            "recipes": recipes_page, "plans": plans_page, "runs": runs_page,
            "publications": publications_page, "metrics": metrics_page, "revenue": revenue_page,
            "experiments": experiments_page, "learning": learning_page, "calendar": calendar_page,
        }
        return {
            "character": character,
            "connected_accounts": accounts_page["items"],
            "research_candidates": research_page["items"],
            "content": content_page, "variants": variants_page, "qa": qa_page, "rights": rights_page,
            "publications": publications_page, "metrics": metrics_page, "revenue": revenue_page,
            "experiments": experiments_page,
            "generation": {
                "recipes": recipes_page["items"], "runs": runs_page["items"], "plans": plans_page["items"],
                "recipes_page": recipes_page, "plans_page": plans_page, "runs_page": runs_page,
            },
            "calendar": calendar_page["items"],
            "results": {
                "snapshot_count": len(allowed_metric_rows),
                "metrics": aggregate_metrics,
                "last_observed_at": last_observed_at,
            },
            "revenue_summary": revenue_summary,
            "learning": {
                "count": learning_page["count"],
                "pending_review_count": sum(
                    1 for row in allowed_learning_rows if str(getattr(row, "status", "")) == "pending_review"
                ),
                "items": learning_page["items"], "page": learning_page,
            },
            "accounts_page": accounts_page, "research_page": research_page, "content_page": content_page,
            "variants_page": variants_page, "qa_page": qa_page, "rights_page": rights_page,
            "recipes_page": recipes_page, "plans_page": plans_page, "runs_page": runs_page,
            "publications_page": publications_page, "metrics_page": metrics_page, "revenue_page": revenue_page,
            "experiments_page": experiments_page, "learning_page": learning_page, "calendar_page": calendar_page,
            "pagination": pagination,
        }


__all__ = ["MediaOperationsOverviewService"]
