"""Chat の依頼から CrowdWorks の一覧・詳細と応募案を扱う道具。"""

from __future__ import annotations

from collections import OrderedDict
import math
import re
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ..memory.database import get_database_manager
from ..memory.models.operations import (
    ApplicationDraft,
    EngagementOpportunity,
    ExternalAction,
    ExternalConnection,
    OpportunityEvaluation,
)
from ..services.marketplace.crowdworks_action import CrowdWorksPolicy
from ..services.marketplace.crowdworks_application_qa import (
    application_copy_qa,
    _crowdworks_applicant_capability_profile,
)
from ..services.marketplace.crowdworks_brief import crowdworks_brief_status
from ..services.marketplace.crowdworks_deadline import _deadline_state, crowdworks_deadline
from ..services.marketplace.crowdworks_source import CrowdWorksSource, _DETAIL_FRESH_WINDOW
from ..services.marketplace.crowdworks_execution import CrowdWorksExecutionService
from ..services.operations_service import (
    OperationsConflictError,
    create_operations_service,
    sha256_json,
)
from ..services.project_context import get_runtime_project_context
from ..services.turn_context import get_turn_context
from .core import tool
from .operations_direct import _effective_project_id, _turn_actor


_CROWDWORKS_ACTION_TYPE = "engagement.submit_application"
_CROWDWORKS_LIST_LIMIT = 100
_CROWDWORKS_CANDIDATE_WINDOW = _DETAIL_FRESH_WINDOW
_QUESTION_NUMBER_PREFIX = re.compile(
    r"(?m)^\s*(?:\d+\s*[.)、:：]\s*|[・●•◦▪‣\-*]+\s*)"
)
_QUESTION_BOILERPLATE = (
    "教えていただけると助かります",
    "教えていただけますでしょうか",
    "教えていただけますか",
    "教えて頂けますか",
    "ご教示いただけますでしょうか",
    "ご教示いただけますか",
    "ご教示ください",
    "お知らせいただけますか",
    "お知らせください",
    "ご確認いただけますか",
    "ご確認ください",
    "確認していただけますか",
    "確認できますか",
    "教えてください",
    "教えて下さい",
    "について",
    "お願いいたします",
    "お願いします",
    "でしょうか",
    "ですか",
    "ますか",
)
_BROWSER_FIXED_CODES = frozenset({
    "browser_backend_unavailable",
    "browser_login_required",
    "browser_unavailable",
    "browser_restart_limit",
    "bridge_browser_command_unavailable",
    "bridge_device_not_found",
    "bridge_device_offline",
    "bridge_disconnected",
    "crowdworks_browser_session_unavailable",
    "crowdworks_browser_busy",
    "crowdworks_operation_lock_required",
    "agent_browser_session_faulted",
})


@dataclass(frozen=True)
class _CrowdWorksScope:
    operations: Any
    connection: Any | None
    policy_service: Any
    policy_revision: Any | None
    policy: CrowdWorksPolicy | None


@asynccontextmanager
async def _session_scope():
    session = await get_database_manager().get_session()
    try:
        yield session
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result


def _turn_config() -> Any:
    turn = get_turn_context()
    config = getattr(turn, "agent_execution_config", None)
    if config is not None:
        return getattr(config, "config", config)
    runtime_context = get_runtime_project_context()
    if isinstance(runtime_context, dict):
        return runtime_context.get("agent_execution_config", runtime_context.get("config"))
    return None


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    if type(value) is not int:
        return default
    return max(1, min(maximum, value))


_CANDIDATE_EXCLUDED_TITLE_WORDS = (
    "アンケート", "モニター", "インタビュー", "テレアポ", "架電", "営業", "クロージング", "商談",
    "限定", "監修", "出演", "モデル募集", "顔出し", "秘書", "ライブ", "ヒアリング",
)
_CANDIDATE_MIN_FIXED_YEN = 5_000.0
# runtime.agent_run_max_total_tokens is global, so one run must stay small.
_READ_JOB_BRIEF_CHARS = 6_000
_RUN_MAX_JOB_READS = 6
_RUN_USAGE_MAX_ENTRIES = 256
_run_usage: "OrderedDict[tuple[str | None, str | None], dict[str, Any]]" = OrderedDict()
_CANDIDATE_MIN_HOURLY_YEN = 2_500.0


async def _resolve_scope(
    session: Any,
    actor: dict[str, Any],
    project_id: str | None,
    config: Any,
) -> _CrowdWorksScope:
    """Resolve only a CrowdWorks connection visible to this authenticated turn."""

    from ..memory.models import Agent
    from ..memory.models.agent_automation import AgentActionPolicy
    from ..services.agent_action_policy_service import AgentActionPolicyService
    from ..services.marketplace.crowdworks_bootstrap import AGENT_SLUG, BOOTSTRAP_KEY

    operations = create_operations_service()
    policy_service = AgentActionPolicyService(
        db_manager=get_database_manager(),
        config=config,
    )
    try:
        visible_connections = await operations.list_connections(
            session,
            actor,
            project_id=project_id,
            limit=100,
        )
    except Exception:
        return _CrowdWorksScope(operations, None, policy_service, None, None)

    owner_id = UUID(str(actor["id"]))
    project_value = UUID(str(project_id)) if project_id else None
    visible_ids = {
        str(row.get("id"))
        for row in visible_connections
        if isinstance(row, dict)
        and row.get("provider_key") == "crowdworks"
        and (
            str(row.get("project_id")) if row.get("project_id") else None
        ) == (str(project_value) if project_value else None)
    }
    if not visible_ids:
        return _CrowdWorksScope(operations, None, policy_service, None, None)

    connection_rows: dict[str, Any] = {}
    for connection_id in visible_ids:
        try:
            connection = await session.get(ExternalConnection, UUID(connection_id))
        except (TypeError, ValueError):
            continue
        if (
            connection is not None
            and connection.owner_user_id == owner_id
            and connection.provider_key == "crowdworks"
            and connection.project_id == project_value
        ):
            connection_rows[str(connection.id)] = connection

    turn = get_turn_context()
    agent_ids: list[UUID] = []
    turn_agent_id = getattr(turn, "agent_id", None)
    if turn_agent_id:
        try:
            turn_agent = await session.get(Agent, UUID(str(turn_agent_id)))
        except (TypeError, ValueError):
            turn_agent = None
        if turn_agent is not None:
            agent_ids.append(turn_agent.id)
    bootstrap_agent = await session.scalar(
        select(Agent).where(Agent.slug == AGENT_SLUG).limit(1)
    )
    if bootstrap_agent is not None and bootstrap_agent.id not in agent_ids:
        agent_ids.append(bootstrap_agent.id)

    for agent_id in agent_ids:
        policies = (
            await session.scalars(
                select(AgentActionPolicy)
                .where(
                    AgentActionPolicy.agent_id == agent_id,
                    AgentActionPolicy.state == "active",
                )
                .order_by(AgentActionPolicy.created_at.desc())
                .limit(50)
            )
        ).all()
        for policy_row in policies:
            revision = await policy_service.current(session, policy_row.id)
            if (
                revision is None
                or revision.action_type != _CROWDWORKS_ACTION_TYPE
                or str(revision.connection_id or "") not in connection_rows
            ):
                continue
            connection = connection_rows[str(revision.connection_id)]
            try:
                policy = CrowdWorksPolicy.model_validate(revision.constraints_json)
            except Exception:
                policy = None
            return _CrowdWorksScope(
                operations,
                connection,
                policy_service,
                revision,
                policy,
            )

    # Match the bootstrap connection when a Policy has not yet been bound.
    for connection in connection_rows.values():
        metadata = connection.metadata_json if isinstance(connection.metadata_json, dict) else {}
        if metadata.get("bootstrap_key") == BOOTSTRAP_KEY:
            return _CrowdWorksScope(operations, connection, policy_service, None, None)
    if len(connection_rows) == 1:
        connection = next(iter(connection_rows.values()))
        return _CrowdWorksScope(operations, connection, policy_service, None, None)
    return _CrowdWorksScope(operations, None, policy_service, None, None)


def _reward_summary(metadata: Any, snapshot: Any = None) -> tuple[float | None, str | None]:
    metadata = metadata if isinstance(metadata, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    source = metadata if metadata.get("compensation_parse_code") is not None else snapshot
    code = source.get("compensation_parse_code")
    amount = source.get("compensation_yen")
    reward_type = source.get("compensation_type")
    if (
        code != "reward_parsed"
        or isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(float(amount))
    ):
        return None, None
    return float(amount), reward_type if isinstance(reward_type, str) else None


def _run_usage_entry() -> dict[str, Any] | None:
    """Per-turn usage; None outside a turn (nothing to bound)."""

    turn = get_turn_context()
    key = (getattr(turn, "session_id", None), getattr(turn, "message_id", None))
    if turn is None or key == (None, None):
        return None
    entry = _run_usage.setdefault(key, {"candidates": None, "job_reads": set()})
    _run_usage.move_to_end(key)
    while len(_run_usage) > _RUN_USAGE_MAX_ENTRIES:
        _run_usage.popitem(last=False)
    return entry


def _clearly_unsuitable_candidate(row: Any) -> bool:
    """Drop rows no applicant-side AI work can profit from, to keep runs small."""

    title = str(getattr(row, "title", "") or "")
    if any(word in title for word in _CANDIDATE_EXCLUDED_TITLE_WORDS):
        return True
    reward, reward_type = _reward_summary(row.metadata_json, row.source_snapshot_json)
    if reward is None:
        return False
    if reward_type == "hourly":
        return reward < _CANDIDATE_MIN_HOURLY_YEN
    return reward < _CANDIDATE_MIN_FIXED_YEN


def _job_summary(row: Any, *, detail_captured: bool | None = None, deadline_at: str | None = None) -> dict[str, Any]:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    snapshot = row.source_snapshot_json if isinstance(row.source_snapshot_json, dict) else {}
    reward_yen, reward_type = _reward_summary(metadata, snapshot)
    result: dict[str, Any] = {
        "opportunity_id": str(row.id),
        "title": str(getattr(row, "title", "") or "")[:60],
        "reward_yen": reward_yen,
        "reward_type": reward_type,
    }
    if detail_captured is not None:
        result["detail_captured"] = bool(detail_captured)
        result["deadline_at"] = deadline_at
    return result


def _browser_skip_code(resolution: Any = None, error: BaseException | None = None) -> str | None:
    code = (
        getattr(resolution, "skip_code", None)
        or getattr(resolution, "reason_code", None)
        if resolution is not None
        else None
    )
    if code in {"skipped_browser_login_required", "skipped_browser_unavailable"}:
        return code
    raw = (
        getattr(error, "crowdworks_error_code", None)
        or getattr(error, "code", None)
        or (str(error) if error is not None else None)
    )
    if raw in {"browser_login_required", "skipped_browser_login_required"}:
        return "skipped_browser_login_required"
    if raw in _BROWSER_FIXED_CODES or raw == "skipped_browser_unavailable":
        return "skipped_browser_unavailable"
    return None


async def _load_opportunity(
    session: Any,
    actor: dict[str, Any],
    scope: _CrowdWorksScope,
    opportunity_id: UUID,
) -> Any | None:
    if scope.connection is None:
        return None
    return await session.scalar(
        select(EngagementOpportunity).where(
            EngagementOpportunity.id == opportunity_id,
            EngagementOpportunity.owner_user_id == UUID(str(actor["id"])),
            EngagementOpportunity.connection_id == scope.connection.id,
            EngagementOpportunity.project_id == scope.connection.project_id,
        )
    )


async def _has_draft_or_action(session: Any, opportunity_id: UUID) -> bool:
    draft_id = await session.scalar(
        select(ApplicationDraft.id)
        .where(ApplicationDraft.opportunity_id == opportunity_id)
        .limit(1)
    )
    action_id = await session.scalar(
        select(ExternalAction.id)
        .where(ExternalAction.opportunity_id == opportunity_id)
        .limit(1)
    )
    return draft_id is not None or action_id is not None


def _normalize_question_line(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _QUESTION_NUMBER_PREFIX.sub("", normalized)
    for phrase in _QUESTION_BOILERPLATE:
        normalized = normalized.replace(unicodedata.normalize("NFKC", phrase).casefold(), "")
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character)[0] not in {"P", "S", "Z"}
    )


def _question_reason(message: str, questions: list[str]) -> str | None:
    if not questions:
        return None
    normalized_questions = [_normalize_question_line(question) for question in questions]
    if any(not question for question in normalized_questions):
        return "question_missing"
    if len(set(normalized_questions)) != len(normalized_questions):
        return "duplicate_question"
    normalized_message = _normalize_question_line(message)
    if any(normalized_message.count(question) >= 2 for question in normalized_questions):
        return "duplicate_question"
    # The persisted Action gate checks each clarification string verbatim.
    # Reject before creating an Evaluation/Draft when that exact binding would fail.
    if any(
        question not in normalized_message or original not in message
        for question, original in zip(normalized_questions, questions, strict=True)
    ):
        return "question_missing"
    return None


async def _find_existing_action(
    session: Any,
    *,
    actor: dict[str, Any],
    project_id: str | None,
    idempotency_key: str,
) -> Any | None:
    query = select(ExternalAction).where(
        ExternalAction.owner_user_id == UUID(str(actor["id"])),
        ExternalAction.idempotency_key == idempotency_key,
    )
    if project_id is None:
        query = query.where(ExternalAction.project_id.is_(None))
    else:
        query = query.where(ExternalAction.project_id == UUID(str(project_id)))
    return await session.scalar(query.order_by(ExternalAction.created_at.desc()).limit(1))


async def _idempotent_action_result(
    session: Any,
    action: Any,
    *,
    scope: _CrowdWorksScope,
    opportunity_id: UUID,
    message: str,
    effort: float,
    revenue: float,
    cost: float,
    fit: str,
    offered_price: float,
    delivery_estimate: str,
    questions: list[str],
) -> dict[str, Any]:
    if (
        scope.connection is None
        or action.action_type != _CROWDWORKS_ACTION_TYPE
        or action.connection_id != scope.connection.id
        or action.opportunity_id != opportunity_id
        or action.status != "proposed"
    ):
        return {"accepted": False, "reason_code": "idempotency_key_conflict"}
    draft = await session.get(ApplicationDraft, action.application_draft_id)
    evaluation = await session.scalar(
        select(OpportunityEvaluation)
        .where(OpportunityEvaluation.opportunity_id == opportunity_id)
        .order_by(OpportunityEvaluation.version.desc(), OpportunityEvaluation.id.desc())
        .limit(1)
    )
    if (
        draft is None
        or evaluation is None
        or draft.message != message
        or float(draft.offered_price or 0) != offered_price
        or (draft.delivery_estimate or "") != delivery_estimate
        or float(evaluation.estimated_effort_hours or 0) != effort
        or float(evaluation.estimated_revenue or 0) != revenue
        or float(evaluation.estimated_cost or 0) != cost
        or (evaluation.fit or "") != fit
        or list(evaluation.missing_requirements or []) != questions
    ):
        return {"accepted": False, "reason_code": "idempotency_key_conflict"}
    return {
        "accepted": True,
        "evaluation_id": str(evaluation.id),
        "draft_id": str(draft.id),
        "action_id": str(action.id),
        "status": "proposed",
        "summary": "利用者の承認を待ちます。承認すると CrowdWorks へ外部送信されます。",
    }


async def _check_proposal_capacity(
    session: Any,
    scope: _CrowdWorksScope,
) -> bool:
    if scope.policy_revision is None or scope.policy_service is None:
        return False
    try:
        capacity = await scope.policy_service.proposal_capacity(
            session,
            policy_revision_id=scope.policy_revision.id,
        )
    except Exception:
        return False
    return type(capacity) is int and capacity > 0


async def _load_turn_pins(session: Any) -> Any | None:
    turn = get_turn_context()
    agent_id = getattr(turn, "agent_id", None)
    revision_id = getattr(turn, "agent_revision_id", None)
    if not agent_id or not revision_id:
        return None
    try:
        from ..services.agent_model_routing import load_pinned_agent_pins

        return await load_pinned_agent_pins(
            session,
            agent_revision_id=revision_id,
            agent_id=agent_id,
        )
    except Exception:
        return None


@tool
async def crowdworks_list_jobs(limit: int = 30) -> dict[str, Any]:
    """CrowdWorksの一覧を1回取得して保存し、件数を返します。案件はcrowdworks_candidatesで確認します。"""

    actor = _turn_actor()
    project_id = _effective_project_id(None)
    config = _turn_config()
    bounded_limit = _bounded_limit(limit, default=30, maximum=_CROWDWORKS_LIST_LIMIT)
    async with _session_scope() as session:
        scope = await _resolve_scope(session, actor, project_id, config)
        if scope.connection is None:
            return {"error_code": "crowdworks_connection_unavailable"}
        if scope.policy is None:
            return {"error_code": "crowdworks_policy_unavailable"}
        source = CrowdWorksSource(operations=scope.operations, config=config)
        try:
            resolution = await source._bridge(str(actor["id"]))
        except Exception as exc:
            code = _browser_skip_code(error=exc)
            if code:
                return {"error_code": code}
            raise
        if not resolution.ready or resolution.bridge is None:
            return {"error_code": _browser_skip_code(resolution=resolution) or "skipped_browser_unavailable"}
        try:
            listing = await source.ingest_listings(
                session,
                owner_user_id=str(actor["id"]),
                connection_id=scope.connection.id,
                project_id=scope.connection.project_id,
                search_urls=list(scope.policy.search_urls),
                max_pages=scope.policy.max_pages_per_scan,
                max_items=bounded_limit,
                device_id=resolution.device_id,
            )
        except Exception as exc:
            code = _browser_skip_code(error=exc)
            if code:
                return {"error_code": code}
            raise
        if listing.get("error_code"):
            code = _browser_skip_code(
                error=RuntimeError(str(listing.get("error_code") or ""))
            )
            return {"error_code": code or "crowdworks_listing_failed"}
        return {
            "fetched": listing.get("listing_count"),
            "created": listing.get("created_count"),
            "next_step": "crowdworks_candidates で候補を確認してください",
        }


@tool
async def crowdworks_candidates(limit: int = 20) -> dict[str, Any]:
    """直近14日以内の詳細読取候補を本文なしで新しい順に返します。"""

    actor = _turn_actor()
    project_id = _effective_project_id(None)
    config = _turn_config()
    bounded_limit = _bounded_limit(limit, default=10, maximum=30)
    usage = _run_usage_entry()
    if usage is not None:
        # A model may lose the earlier result; repeat it instead of refusing,
        # so the run stays bounded without a dead end.
        if usage.get("candidates") is not None:
            return usage["candidates"]
    async with _session_scope() as session:
        scope = await _resolve_scope(session, actor, project_id, config)
        if scope.connection is None:
            return {"error_code": "crowdworks_connection_unavailable"}
        has_draft = select(ApplicationDraft.id).where(
            ApplicationDraft.opportunity_id == EngagementOpportunity.id
        ).exists()
        has_action = select(ExternalAction.id).where(
            ExternalAction.opportunity_id == EngagementOpportunity.id
        ).exists()
        cutoff = (datetime.now(timezone.utc) - _CROWDWORKS_CANDIDATE_WINDOW).replace(tzinfo=None)
        rows = (
            await session.scalars(
                select(EngagementOpportunity)
                .where(
                    EngagementOpportunity.owner_user_id == UUID(str(actor["id"])),
                    EngagementOpportunity.connection_id == scope.connection.id,
                    EngagementOpportunity.project_id == scope.connection.project_id,
                    EngagementOpportunity.status == "open",
                    EngagementOpportunity.created_at >= cutoff,
                    EngagementOpportunity.source_url.like("https://crowdworks.jp/public/jobs/%"),
                    ~has_draft,
                    ~has_action,
                )
                .order_by(EngagementOpportunity.created_at.desc(), EngagementOpportunity.id.desc())
                .limit(500)
            )
        ).all()
        jobs: list[dict[str, Any]] = []
        for row in rows:
            snapshot = row.source_snapshot_json if isinstance(row.source_snapshot_json, dict) else {}
            deadline_at, deadline_status = _deadline_state(row)
            if deadline_status == "expired" or _clearly_unsuitable_candidate(row):
                continue
            jobs.append(_job_summary(
                row,
                detail_captured=(
                    snapshot.get("detail_capture_status") == "complete"
                    and crowdworks_brief_status(snapshot, row.source_text) == "complete"
                ),
                deadline_at=deadline_at,
            ))
        # Known rewards first (highest first), then unknown rewards, newest
        # first within each group: unknown-reward rows are often tiny trial jobs.
        jobs.sort(key=lambda job: -float(job.get("reward_yen") or 0))
        result = {"jobs": jobs[:bounded_limit]}
        if usage is not None:
            usage["candidates"] = result
        return result


@tool
async def crowdworks_read_job(opportunity_id: str) -> dict[str, Any]:
    """指定したCrowdWorks案件の詳細を取得し、期限と本文を返します。"""

    try:
        identifier = UUID(str(opportunity_id).strip())
    except (TypeError, ValueError):
        return {"error_code": "opportunity_id_invalid"}
    usage = _run_usage_entry()
    if usage is not None:
        reads = usage["job_reads"]
        if str(identifier) not in reads and len(reads) >= _RUN_MAX_JOB_READS:
            return {
                "error_code": "job_read_limit_reached",
                "next_step": "これ以上読まず、読んだ案件から提案するか、条件に合う案件が無いと理由を返して終えてください",
            }
        reads.add(str(identifier))
    actor = _turn_actor()
    project_id = _effective_project_id(None)
    config = _turn_config()
    async with _session_scope() as session:
        scope = await _resolve_scope(session, actor, project_id, config)
        if scope.connection is None:
            return {"error_code": "crowdworks_connection_unavailable"}
        row = await _load_opportunity(session, actor, scope, identifier)
        if row is None:
            return {"error_code": "opportunity_not_found"}
        if await _has_draft_or_action(session, identifier):
            return {"error_code": "already_has_draft_or_action"}
        if not isinstance(row.source_url, str) or not row.source_url:
            return {"error_code": "detail_source_url_missing"}
        snapshot = row.source_snapshot_json if isinstance(row.source_snapshot_json, dict) else {}
        if (
            snapshot.get("detail_capture_status") != "complete"
            or crowdworks_brief_status(snapshot, row.source_text) != "complete"
        ):
            source = CrowdWorksSource(operations=scope.operations, config=config)
            try:
                result = await source.fetch_one_pending_detail(
                    session,
                    owner_user_id=str(actor["id"]),
                    connection_id=scope.connection.id,
                    opportunity_id=str(identifier),
                    min_expected_profit=(scope.policy.min_expected_profit if scope.policy else 5000.0),
                    min_profit_per_hour=(scope.policy.min_profit_per_hour if scope.policy else 2500.0),
                )
            except Exception as exc:
                code = _browser_skip_code(error=exc)
                if code:
                    return {"error_code": code}
                return {"error_code": "crowdworks_detail_capture_failed"}
            if result.get("completed") != 1:
                return {"error_code": result.get("error_code") or "crowdworks_detail_not_completed"}
            row = await session.get(EngagementOpportunity, identifier, populate_existing=True)
            if row is None:
                return {"error_code": "opportunity_not_found"}
            snapshot = row.source_snapshot_json if isinstance(row.source_snapshot_json, dict) else {}
        deadline_at, deadline_status = _deadline_state(row)
        reward_yen, reward_type = _reward_summary(row.metadata_json, snapshot)
        return {
            "opportunity_id": str(row.id),
            "title": str(row.title or "")[:1000],
            "url": row.source_url,
            "reward_yen": reward_yen,
            "reward_type": reward_type,
            "deadline_at": deadline_at,
            "deadline_status": deadline_status,
            "brief_text": str(row.source_text or "")[:_READ_JOB_BRIEF_CHARS],
        }


@tool
async def crowdworks_profile() -> dict[str, Any]:
    """応募者について申告済みの能力と対応条件を返します。"""

    return {"profile": _crowdworks_applicant_capability_profile(_turn_config())}


@tool
async def crowdworks_submit_proposal(
    opportunity_id: str,
    estimated_effort_hours: float,
    estimated_revenue_yen: float,
    estimated_cost_yen: float,
    fit: str,
    ai_completable: bool,
    profile_conditions_met: bool,
    message: str,
    offered_price_yen: float,
    delivery_estimate: str,
    clarification_questions: list[str],
    idempotency_key: str,
) -> dict[str, Any]:
    """ゲートと独立QAを通った応募案を承認待ちのproposed Actionとして保存します。"""

    if ai_completable is not True:
        return {"accepted": False, "reason_code": "not_ai_completable"}
    if profile_conditions_met is not True:
        return {"accepted": False, "reason_code": "profile_conditions_unmet"}
    try:
        identifier = UUID(str(opportunity_id).strip())
    except (TypeError, ValueError):
        return {"accepted": False, "reason_code": "opportunity_id_invalid"}
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message.encode("utf-8")) > 20_000
        or not isinstance(delivery_estimate, str)
        or not delivery_estimate.strip()
        or len(delivery_estimate.encode("utf-8")) > 2_000
        or not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key.encode("utf-8")) > 255
        or not isinstance(fit, str)
        or not fit.strip()
        or not isinstance(clarification_questions, list)
        or any(not isinstance(question, str) or not question.strip() for question in clarification_questions)
    ):
        return {"accepted": False, "reason_code": "invalid_proposal_input"}
    message = message.strip()
    delivery_estimate = delivery_estimate.strip()
    fit = fit.strip()
    questions = [question.strip() for question in clarification_questions]
    if questions:
        # The tool writes the questions itself: the QA and Action gates need
        # each one verbatim, and a body that repeats one is a duplicate.
        message = "\n".join([message, "", "確認したい点", *(f"・{question}" for question in questions)])
    question_code = _question_reason(message, questions)
    if question_code is not None:
        return {"accepted": False, "reason_code": question_code}
    numeric_values = (
        estimated_effort_hours,
        estimated_revenue_yen,
        estimated_cost_yen,
        offered_price_yen,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in numeric_values
    ):
        return {"accepted": False, "reason_code": "below_profit_threshold"}
    effort = float(estimated_effort_hours)
    revenue = float(estimated_revenue_yen)
    cost = float(estimated_cost_yen)
    offered_price = float(offered_price_yen)
    if effort <= 0 or revenue < 0 or cost < 0 or offered_price < 0:
        return {"accepted": False, "reason_code": "below_profit_threshold"}

    actor = _turn_actor()
    project_id = _effective_project_id(None)
    config = _turn_config()
    async with _session_scope() as session:
        scope = await _resolve_scope(session, actor, project_id, config)
        if scope.connection is None:
            return {"accepted": False, "reason_code": "crowdworks_connection_unavailable"}
        row = await _load_opportunity(session, actor, scope, identifier)
        if row is None:
            return {"accepted": False, "reason_code": "opportunity_not_found"}
        existing_action = await _find_existing_action(
            session,
            actor=actor,
            project_id=project_id,
            idempotency_key=idempotency_key.strip(),
        )
        if existing_action is not None:
            return await _idempotent_action_result(
                session,
                existing_action,
                scope=scope,
                opportunity_id=identifier,
                message=message,
                effort=effort,
                revenue=revenue,
                cost=cost,
                fit=fit,
                offered_price=offered_price,
                delivery_estimate=delivery_estimate,
                questions=questions,
            )
        if await _has_draft_or_action(session, identifier):
            return {"accepted": False, "reason_code": "already_has_draft_or_action"}

        snapshot = row.source_snapshot_json if isinstance(row.source_snapshot_json, dict) else {}
        saved_deadline = snapshot.get("deadline_at")
        if not isinstance(saved_deadline, str) or not saved_deadline.strip():
            return {"accepted": False, "reason_code": "deadline_not_open"}
        deadline_at, deadline_status = crowdworks_deadline(
            source_snapshot={**snapshot, "deadline_at": saved_deadline},
            source_text=row.source_text,
            captured_at=row.created_at,
        )
        if deadline_status != "open" or deadline_at is None:
            return {"accepted": False, "reason_code": "deadline_not_open"}
        if (
            snapshot.get("detail_capture_status") != "complete"
            or crowdworks_brief_status(snapshot, row.source_text) != "complete"
        ):
            return {"accepted": False, "reason_code": "detail_not_captured"}

        policy = scope.policy
        if policy is None:
            return {"accepted": False, "reason_code": "daily_limit_reached"}
        profit = revenue - cost
        if (
            profit < float(policy.min_expected_profit)
            or profit / effort < float(policy.min_profit_per_hour)
        ):
            return {"accepted": False, "reason_code": "below_profit_threshold"}
        # The Agent already judged feasibility (ai_completable,
        # profile_conditions_met); its questions are client clarifications.
        # The legacy string classifier would mark scope questions as blocking.
        requirements = {"clarifiable": list(questions)}
        if not await _check_proposal_capacity(session, scope):
            return {"accepted": False, "reason_code": "daily_limit_reached"}
        pins = await _load_turn_pins(session)
        if pins is None:
            return {"accepted": False, "reason_code": "application_qa_unavailable"}

        evaluation_result = await scope.operations.create_evaluation(
            session,
            actor,
            identifier,
            estimated_effort_hours=effort,
            estimated_cost=cost,
            estimated_revenue=revenue,
            # Evaluation.fit is a 32-byte label; the Agent's reasoning is the summary.
            fit="AI完結",
            risks=[],
            missing_requirements=questions,
            summary=fit,
        )
        evaluation_id = UUID(str(evaluation_result["id"]))
        evaluation = await session.get(OpportunityEvaluation, evaluation_id)
        if evaluation is None:
            return {"accepted": False, "reason_code": "evaluation_unavailable"}
        workflow = CrowdWorksExecutionService(config=config, operations=scope.operations)
        qa = await application_copy_qa(
            workflow,
            row=row,
            evaluation=evaluation,
            clarification_questions=requirements["clarifiable"],
            message=message,
            offered_price=offered_price,
            currency=policy.currency,
            delivery_estimate=delivery_estimate,
            artifact_version_ids=[],
            owner_user_id=str(actor["id"]),
            project_id=project_id,
            policy=policy,
            pins=pins,
        )
        if qa.get("status") != "passed":
            metadata = dict(row.metadata_json or {})
            metadata.update({
                "latest_evaluation_id": str(evaluation.id),
                "workflow_stage": "EVALUATED",
                "evaluation_requirements_schema_version": 1,
                "expected_profit": profit,
                "expected_profit_per_hour": profit / effort,
                "application_qa_status": qa.get("status") or "unavailable",
                "application_qa": qa,
                "screening_status": (
                    "application_qa_failed"
                    if qa.get("status") == "failed"
                    else "application_qa_unavailable"
                ),
                "crowdworks_draft_reason_code": "application_qa_failed",
            })
            row.metadata_json = metadata
            await session.commit()
            return {
                "accepted": False,
                "reason_code": "application_qa_failed" if qa.get("status") == "failed" else "application_qa_unavailable",
            }

        draft_result = await scope.operations.create_draft(
            session,
            actor,
            identifier,
            message=message,
            offered_price=offered_price,
            currency=policy.currency,
            delivery_estimate=delivery_estimate,
            artifact_version_ids=[],
        )
        draft_id = UUID(str(draft_result["id"]))
        draft = await session.get(ApplicationDraft, draft_id)
        if draft is None or str(draft.draft_hash) != str(qa.get("draft_hash") or ""):
            return {"accepted": False, "reason_code": "application_qa_binding_mismatch"}
        qa = {
            **qa,
            "status": "passed",
            "draft_persisted": True,
            "draft_id": str(draft.id),
            "draft_version": int(draft.version),
            "draft_hash": str(draft.draft_hash),
            "evaluation_id": str(evaluation.id),
            "clarification_questions_hash": sha256_json(requirements["clarifiable"]),
        }
        metadata = dict(row.metadata_json or {})
        metadata.update({
            "latest_evaluation_id": str(evaluation.id),
            "workflow_stage": "EVALUATED",
            "evaluation_requirements_schema_version": 1,
            "expected_profit": profit,
            "expected_profit_per_hour": profit / effort,
            "estimated_effort_hours": effort,
            "estimated_revenue_yen": revenue,
            "estimated_cost_yen": cost,
            "screening_status": "application_preparing",
            "application_clarification_questions": requirements["clarifiable"],
            "application_draft_id": str(draft.id),
            "application_draft_version": int(draft.version),
            "application_draft_hash": str(draft.draft_hash),
            "application_qa_status": "passed",
            "application_qa": qa,
            "crowdworks_application_qa_failure_code_counts": {},
            "crowdworks_application_qa_retry_after": None,
        })
        row.metadata_json = metadata
        await session.commit()
        try:
            action = await scope.operations.create_action(
                session,
                actor,
                connection_id=scope.connection.id,
                application_draft_id=draft.id,
                idempotency_key=idempotency_key.strip(),
            )
        except OperationsConflictError as exc:
            return {"accepted": False, "reason_code": "proposal_blocked"}
        if action.get("status") != "proposed":
            return {"accepted": False, "reason_code": "proposal_not_pending"}
        action_id = str(action.get("id") or "")
        if not action_id:
            return {"accepted": False, "reason_code": "proposal_not_pending"}
        metadata = dict(row.metadata_json or {})
        metadata.update({
            "workflow_stage": "APPLICATION_PROPOSED",
            "external_action_id": action_id,
            "external_action_status": "proposed",
            "screening_status": "decision_ready",
            "screening_reason": "AI QA通過。人間の応募判断待ち",
            "crowdworks_draft_reason_code": "proposal_created",
        })
        row.metadata_json = metadata
        await session.commit()
        return {
            "accepted": True,
            "evaluation_id": str(evaluation.id),
            "draft_id": str(draft.id),
            "action_id": action_id,
            "status": "proposed",
            "summary": "利用者の承認を待ちます。承認すると CrowdWorks へ外部送信されます。",
        }


CROWDWORKS_AGENT_TOOL_DEFINITIONS = (
    crowdworks_list_jobs,
    crowdworks_candidates,
    crowdworks_read_job,
    crowdworks_profile,
    crowdworks_submit_proposal,
)
CROWDWORKS_AGENT_TOOL_NAMES = frozenset(
    definition.name for definition in CROWDWORKS_AGENT_TOOL_DEFINITIONS
)
