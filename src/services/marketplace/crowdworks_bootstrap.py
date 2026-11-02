"""Idempotent one-shot setup for local CrowdWorks marketplace automation."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from ...features import Features
from ...memory.models import Agent, AgentOrganizationProfile, AgentRevision
from ...memory.models.agent_work import AgentWorkItem
from ...memory.models.agent_automation import AgentActionPolicy, AgentActionPolicyRevision
from ...memory.models.operations import ExternalConnection
from ..agent_action_policy_service import ActionPolicyError, AgentActionPolicyService
from ..agent_identity_service import AgentIdentityService, OrganizationService, sha256_json
from ..agent_authority import AgentAuthorityResolver
from ..agent_model_routing import AgentModelRouteError, AgentRoutingPins, resolve_agent_role_route
from ..operations_service import OperationsService
from ..ephemeral_llm import (
    EPHEMERAL_PROVIDER_EXCEPTION_CLASS_CODES,
    EPHEMERAL_PROVIDER_FAILURE_STAGES,
    safe_ephemeral_exception_source_name,
)
from .crowdworks_action import CrowdWorksPolicy
from .crowdworks_source import (
    DETAIL_CANDIDATE_REASON_CODES,
    _DETAIL_INSPECTION_ERRORS,
)
from .crowdworks_application_qa import (
    APPLICATION_QA_FAILURE_CODES, DRAFT_DIAGNOSTIC_PROVIDER_CODES,
    DRAFT_DIAGNOSTIC_STAGE_CODES, is_evaluation_provider_error_code,
)
from .crowdworks_requirements import EVALUATION_REQUIREMENT_CATEGORY_CODES
from .crowdworks_scan_notifications import CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES
from .crowdworks_scan_history_schema import (
    EVALUATION_FAILURE_CODES,
    EVALUATION_FINISH_REASON_CODES,
    EVALUATION_NOT_ATTEMPTED_CODES,
    EVALUATION_PARSE_ERROR_CODES,
    EVALUATION_CIRCUIT_OPEN_REASON_CODES,
    UNIT_TIMEOUT_CODE,
    EVALUATION_MISSING_FIELD_CODES,
    EVALUATION_WINDOW_EXCLUSION_CODES,
    CROWDWORKS_DETAIL_FAILURE_CODES,
    CROWDWORKS_DETAIL_STOP_CODES,
    CROWDWORKS_NO_UNIT_REASON_CODES,
    DRAFT_REASON_CODES,
    ESTIMATE_SANITY_REWARD_UNKNOWN_CODE,
    ESTIMATE_SANITY_RATE_TOO_HIGH_CODE,
    is_evaluation_request_outcome_code,
)
from .crowdworks_specialists import (
    DEVELOPMENT_CAPABILITIES,
    SPECIALISTS,
    SPECIALIST_CAPABILITY_CEILING,
    SpecialistSpec,
    development_harness_settings,
)


BOOTSTRAP_KEY = "crowdworks-marketplace-v1"
AGENT_SLUG = "crowdworks-marketplace"
DEFAULT_CONSTRAINTS = CrowdWorksPolicy(
    search_urls=["https://crowdworks.jp/public/jobs/"],
    tick_interval_minutes=2,
    scan_interval_minutes=30,
    max_pages_per_scan=3,
    max_opportunities_per_scan=30,
    max_applications_per_scan=3,
    min_expected_profit=5000.0,
    min_profit_per_hour=2500.0,
    currency="JPY",
).model_dump()
DEFAULT_RATE_LIMIT = {"window_seconds": 86400, "max_actions": 8}
DEFAULT_DEDUPE_SECONDS = 30 * 24 * 60 * 60
ORGANIZATION_CAPABILITIES = tuple(dict.fromkeys((*SPECIALIST_CAPABILITY_CEILING, "external_action_propose")))
_SCAN_RESULT_VALUES = frozenset({"succeeded", "failed", "skipped"})
_SCAN_ERROR_CODES = frozenset({
    "crowdworks_scan_failed",
    "crowdworks_scan_capacity_deferred",
    "crowdworks_tick_no_unit",
    "crowdworks_restart_drain",
    "crowdworks_unit_failed",
    "crowdworks_unit_failure_limit_reached",
    "crowdworks_evaluation_failed",
    "unit_timeout",
    "crowdworks_evaluation_circuit_open",
    "skipped_provider_circuit_open",
    "crowdworks_browser_session_unavailable",
    "skipped_browser_unavailable",
    "skipped_browser_login_required",
    "crowdworks_detail_capture_failed",
    "crowdworks_detail_navigation_failed",
    "crowdworks_detail_url_unverified",
    "crowdworks_detail_inspection_incomplete",
    "crowdworks_detail_text_invalid",
    "crowdworks_detail_text_overflow",
    "crowdworks_detail_brief_empty",
    "crowdworks_operation_lock_required",
    "crowdworks_browser_busy",
    "edge_broker_browser_disconnected",
    "edge_broker_operation_outcome_unknown",
    "agent_browser_session_faulted",
    "agent_browser_operation_outcome_unknown",
}) | _DETAIL_INSPECTION_ERRORS
_SCAN_BROWSER_REASON_CODES = frozenset({
    "browser_backend_unavailable",
    "browser_login_required",
    "browser_owner_mismatch",
    "browser_unavailable",
    "browser_restart_limit",
    "broker_executable_missing",
    "broker_mode_legacy",
    "broker_mode_unknown",
    "broker_start_failed",
    "broker_start_timeout",
    "broker_unavailable",
    "cdp_disconnected",
    "gateway_root_missing",
    "crowdworks_browser_session_unavailable",
    "bridge_select_a_pc_first",
    "bridge_device_not_found",
    "bridge_device_offline",
    "bridge_disconnected",
    "bridge_capability_unavailable",
    "bridge_browser_command_unavailable",
    "bridge_jev_runtime_required",
    "edge_broker_browser_disconnected",
    "edge_broker_operation_outcome_unknown",
    "agent_browser_session_faulted",
    "agent_browser_operation_outcome_unknown",
})
_SCAN_DETAIL_FAILURE_CODES = _DETAIL_INSPECTION_ERRORS
_SCAN_EVALUATION_FAILURE_CODES = EVALUATION_FAILURE_CODES
_SCAN_EVALUATION_NOT_ATTEMPTED_CODES = EVALUATION_NOT_ATTEMPTED_CODES
_SCAN_EVALUATION_PARSE_ERROR_CODES = EVALUATION_PARSE_ERROR_CODES


def project_crowdworks_scan_summary(value: Any) -> dict[str, Any]:
    """Expose only bounded scan counters, timestamps, and server-owned codes."""

    raw = value if isinstance(value, dict) else {}

    def timestamp(key: str) -> str | None:
        return bounded_timestamp(raw.get(key))

    def bounded_timestamp(candidate: Any) -> str | None:
        if not isinstance(candidate, str) or len(candidate) > 40:
            return None
        try:
            from datetime import datetime

            datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
        return candidate

    def opportunity_id(key: str) -> str | None:
        candidate = raw.get(key)
        if not isinstance(candidate, str) or len(candidate) > 36:
            return None
        try:
            return str(UUID(candidate))
        except (TypeError, ValueError, AttributeError):
            return None

    def fixed_code(key: str, allowed: frozenset[str]) -> str | None:
        candidate = raw.get(key)
        return candidate if isinstance(candidate, str) and candidate in allowed else None

    def notification_records() -> dict[str, dict[str, Any]]:
        candidate = raw.get("notifications_by_reason")
        if not isinstance(candidate, dict):
            return {}
        output: dict[str, dict[str, Any]] = {}
        for reason_code in sorted(CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES):
            record = candidate.get(reason_code)
            if not isinstance(record, dict) or record.get("reason_code") != reason_code:
                continue
            attempted_at = bounded_timestamp(record.get("last_attempt_at"))
            sent_at = bounded_timestamp(record.get("last_sent_at"))
            next_sendable_at = bounded_timestamp(record.get("next_sendable_at"))
            delivery_code = record.get("delivery_code")
            if delivery_code != "crowdworks_notification_delivery_failed":
                delivery_code = None
            if attempted_at is None and sent_at is None and next_sendable_at is None:
                continue
            output[reason_code] = {
                "reason_code": reason_code,
                "last_attempt_at": attempted_at,
                "last_sent_at": sent_at,
                "next_sendable_at": next_sendable_at,
                "delivery_code": delivery_code,
            }
        return output

    def count(key: str) -> int:
        return bounded_count(key, 1_000_000)

    def bounded_count(key: str, maximum: int) -> int:
        candidate = raw.get(key)
        return max(0, min(candidate, maximum)) if type(candidate) is int else 0

    def code_counts(key: str, allowed: frozenset[str]) -> dict[str, int]:
        candidate = raw.get(key)
        if not isinstance(candidate, dict):
            return {}
        return {
            code: max(0, min(int(amount), 1_000_000))
            for code, amount in candidate.items()
            if code in allowed and type(amount) is int and amount > 0
        }

    def draft_provider_diagnostic() -> dict[str, Any] | None:
        candidate = raw.get("draft_provider_diagnostic")
        if not isinstance(candidate, dict):
            return None
        exception_class = candidate.get("exception_class")
        exception_source = candidate.get("exception_source")
        route_provider = candidate.get("route_provider")
        route_model = candidate.get("route_model")
        route_fallback = candidate.get("route_fallback")
        provider_subcode = candidate.get("provider_subcode")
        max_output_tokens = candidate.get("max_output_tokens")
        retry_count = candidate.get("consecutive_connection_reset_count")
        return {
            "stage": candidate.get("stage")
            if candidate.get("stage") in DRAFT_DIAGNOSTIC_STAGE_CODES
            else "send",
            "exception_class": exception_class
            if isinstance(exception_class, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", exception_class)
            else "other",
            "exception_source": exception_source
            if isinstance(exception_source, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.py:\d{1,6}", exception_source)
            else "other:0",
            "route_provider": route_provider
            if isinstance(route_provider, str)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", route_provider)
            else None,
            "route_model": route_model
            if isinstance(route_model, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", route_model)
            else None,
            "route_fallback": route_fallback
            if route_fallback in {"none", "luna"}
            else "none",
            "provider_subcode": provider_subcode
            if isinstance(provider_subcode, str)
            and (
                provider_subcode in DRAFT_DIAGNOSTIC_PROVIDER_CODES
                or re.fullmatch(r"free_team_[a-z][a-z0-9_]{0,47}", provider_subcode)
            )
            else None,
            "request_char_count": max(0, min(candidate.get("request_char_count"), 1_000_000))
            if type(candidate.get("request_char_count")) is int
            else 0,
            "request_token_estimate": max(0, min(candidate.get("request_token_estimate"), 333_334))
            if type(candidate.get("request_token_estimate")) is int
            else 0,
            "max_output_tokens": max(1, min(max_output_tokens, 128_000))
            if type(max_output_tokens) is int and max_output_tokens > 0
            else None,
            "duration_ms": max(0, min(candidate.get("duration_ms"), 1_800_000))
            if type(candidate.get("duration_ms")) is int
            else 0,
            "consecutive_connection_reset_count": max(0, min(retry_count, 3))
            if type(retry_count) is int
            else 0,
            "next_retry_at": bounded_timestamp(candidate.get("next_retry_at")),
        }

    def provider_exception_subcode_counts() -> dict[str, int]:
        candidate = raw.get("evaluation_provider_exception_subcode_counts")
        if not isinstance(candidate, dict):
            return {}
        output: dict[str, int] = {}
        for key, amount in candidate.items():
            if not isinstance(key, str) or type(amount) is not int or amount <= 0:
                continue
            exception_class, separator, stage = key.partition(":")
            if (
                separator
                and exception_class in EPHEMERAL_PROVIDER_EXCEPTION_CLASS_CODES
                and stage in EPHEMERAL_PROVIDER_FAILURE_STAGES
            ):
                output[f"{exception_class}:{stage}"] = max(0, min(amount, 1_000_000))
        return output

    def provider_exception_source_counts() -> dict[str, int]:
        candidate = raw.get("evaluation_provider_exception_source_counts")
        if not isinstance(candidate, dict):
            return {}
        output: dict[str, int] = {}
        for key, amount in candidate.items():
            if not isinstance(key, str) or type(amount) is not int or amount <= 0:
                continue
            parts = key.split(":", 3)
            if len(parts) != 4:
                continue
            exception_class, stage, filename, line = parts
            source = f"{filename}:{line}"
            if (
                exception_class in EPHEMERAL_PROVIDER_EXCEPTION_CLASS_CODES
                and stage in EPHEMERAL_PROVIDER_FAILURE_STAGES
                and safe_ephemeral_exception_source_name(source) == source
            ):
                output[f"{exception_class}:{stage}:{source}"] = max(0, min(amount, 1_000_000))
        return output

    def provider_error_code_counts() -> dict[str, int]:
        candidate = raw.get("evaluation_provider_error_code_counts")
        if not isinstance(candidate, dict):
            return {}
        return {
            code: max(0, min(amount, 1_000_000))
            for code, amount in candidate.items()
            if isinstance(code, str)
            and is_evaluation_provider_error_code(code)
            and type(amount) is int
            and amount > 0
        }

    def evaluation_request_diagnostics() -> list[dict[str, Any]]:
        candidate = raw.get("evaluation_request_diagnostics")
        if not isinstance(candidate, list):
            return []
        output: list[dict[str, Any]] = []
        allowed_stages = frozenset({"before_request", "request", "response_parse"})
        for item in candidate[:96]:
            if not isinstance(item, dict):
                continue
            input_chars = item.get("input_char_count")
            output_cap = item.get("output_cap")
            budget_max_requests = item.get("budget_max_requests")
            actual_request_count = item.get("actual_request_count")
            usage_present = item.get("usage_present")
            usage_input_units = item.get("usage_input_units")
            usage_output_units = item.get("usage_output_units")
            usage_total_units = item.get("usage_total_units")
            usage_total_source = item.get("usage_total_source")
            usage_telemetry_present = any(
                key in item
                for key in (
                    "usage_present",
                    "usage_input_units",
                    "usage_output_units",
                    "usage_total_units",
                    "usage_total_source",
                )
            )
            stage = item.get("stage")
            exception_class = item.get("exception_class")
            exception_source = item.get("exception_source")
            outcome_code = item.get("outcome_code")
            route_model = item.get("route_model")
            route_fallback = item.get("route_fallback", "none")
            route_model_valid = (
                route_model is None
                or (
                    isinstance(route_model, str)
                    and len(route_model) <= 128
                    and "://" not in route_model
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", route_model)
                    is not None
                )
            )
            exception_diagnostic_valid = (
                exception_class is None
                and exception_source is None
            ) or (
                isinstance(exception_class, str)
                and exception_class in EPHEMERAL_PROVIDER_EXCEPTION_CLASS_CODES
                and isinstance(exception_source, str)
                and safe_ephemeral_exception_source_name(exception_source)
                == exception_source
            )
            if (
                type(input_chars) is not int
                or not 0 <= input_chars <= 1_000_000
                or type(output_cap) is not int
                or not 1 <= output_cap <= 128_000
                or (budget_max_requests is not None and (
                    type(budget_max_requests) is not int
                    or not 0 <= budget_max_requests <= 1_000
                ))
                or type(actual_request_count) is not int
                or not 0 <= actual_request_count <= 1_000
                or (
                    usage_telemetry_present
                    and (
                        type(usage_present) is not bool
                        or (
                            usage_total_source is not None
                            and (
                                not isinstance(usage_total_source, str)
                                or usage_total_source not in {"reported", "derived"}
                            )
                        )
                        or any(
                            value is not None
                            and (
                                type(value) is not int
                                or not 0 <= value <= 1_000_000_000
                            )
                            for value in (
                                usage_input_units,
                                usage_output_units,
                                usage_total_units,
                            )
                        )
                        or (not usage_present and any(
                            value is not None
                            for value in (
                                usage_input_units,
                                usage_output_units,
                                usage_total_units,
                                usage_total_source,
                            )
                        ))
                        or (usage_present and not any(
                            value is not None
                            for value in (
                                usage_input_units,
                                usage_output_units,
                                usage_total_units,
                            )
                        ))
                        or ((usage_total_source is None) != (usage_total_units is None))
                    )
                )
                or not isinstance(stage, str)
                or stage not in allowed_stages
                or not exception_diagnostic_valid
                or not isinstance(outcome_code, str)
                or not is_evaluation_request_outcome_code(outcome_code)
                or not route_model_valid
                or not isinstance(route_fallback, str)
                or route_fallback not in {"none", "luna"}
            ):
                continue
            projected = {
                "input_char_count": input_chars,
                "output_cap": output_cap,
                "budget_max_requests": budget_max_requests,
                "actual_request_count": actual_request_count,
                "stage": stage,
                "exception_class": exception_class,
                "exception_source": exception_source,
                "outcome_code": outcome_code,
                "route_model": route_model,
                "route_fallback": route_fallback,
            }
            if usage_telemetry_present:
                projected.update({
                    "usage_present": usage_present,
                    "usage_input_units": usage_input_units,
                    "usage_output_units": usage_output_units,
                    "usage_total_units": usage_total_units,
                    "usage_total_source": usage_total_source,
                })
            output.append(projected)
        return output

    severity_categories = raw.get("requirement_severity_category_counts")
    severity_categories = severity_categories if isinstance(severity_categories, dict) else {}
    safe_severity_categories: dict[str, dict[str, int]] = {}
    for severity in ("blocking", "clarifiable"):
        categories = severity_categories.get(severity)
        safe_severity_categories[severity] = {
            category: max(0, min(amount, 1_000_000))
            for category, amount in (categories.items() if isinstance(categories, dict) else [])
            if category in EVALUATION_REQUIREMENT_CATEGORY_CODES and type(amount) is int and amount > 0
        }

    gates = raw.get("screening_gate_counts")
    gates = gates if isinstance(gates, dict) else {}

    route_counts = raw.get("evaluation_route_counts")
    route_counts = route_counts if isinstance(route_counts, dict) else {}
    safe_route_counts = {
        model: max(0, min(route_counts.get(model), 1_000_000))
        if type(route_counts.get(model)) is int
        else 0
        for model in ("sol", "luna", "other")
    }
    route_severity_counts = raw.get("evaluation_route_severity_counts")
    route_severity_counts = route_severity_counts if isinstance(route_severity_counts, dict) else {}
    route_severity_fields = (
        "evaluation_count",
        "blocking_opportunity_count",
        "clarifiable_only_opportunity_count",
        "blocking_item_count",
        "clarifiable_item_count",
    )
    safe_route_severity_counts = {
        model: {
            field: max(0, min(values.get(field), 1_000_000))
            if isinstance(values, dict) and type(values.get(field)) is int
            else 0
            for field in route_severity_fields
        }
        for model in ("sol", "luna", "other")
        for values in (route_severity_counts.get(model),)
    }
    return {
        "last_scan_started_at": timestamp("scan_started_at"),
        "last_scan_finished_at": timestamp("scan_finished_at"),
        "result": raw.get("scan_result") if raw.get("scan_result") in _SCAN_RESULT_VALUES else "unknown",
        "error_code": raw.get("scan_error_code") if raw.get("scan_error_code") in _SCAN_ERROR_CODES else None,
        "unit_kind": raw.get("unit_kind") if raw.get("unit_kind") in {
            "listing", "detail", "evaluation", "draft", "contract_monitor", "none"
        } else "none",
        "unit_status": raw.get("unit_status") if raw.get("unit_status") in {
            "idle", "succeeded", "failed", "skipped"
        } else "idle",
        "browser_backend": raw.get("browser_backend")
        if isinstance(raw.get("browser_backend"), str)
        and raw.get("browser_backend") in {"bridge", "local"}
        else None,
        "precondition_skip_code": raw.get("precondition_skip_code")
        if isinstance(raw.get("precondition_skip_code"), str)
        and raw.get("precondition_skip_code") in {
            "skipped_browser_unavailable", "skipped_browser_login_required"
        }
        else None,
        "precondition_skip_reason_code": raw.get("precondition_skip_reason_code")
        if isinstance(raw.get("precondition_skip_reason_code"), str)
        and raw.get("precondition_skip_reason_code") in _SCAN_BROWSER_REASON_CODES
        else None,
        "precondition_skip_count": count("precondition_skip_count"),
        "precondition_skip_since": timestamp("precondition_skip_since"),
        "precondition_skip_last_at": timestamp("precondition_skip_last_at"),
        "notification_sent_at": timestamp("notification_sent_at"),
        "last_precondition_skip_code": raw.get("last_precondition_skip_code")
        if isinstance(raw.get("last_precondition_skip_code"), str)
        and raw.get("last_precondition_skip_code") in {
            "skipped_browser_unavailable", "skipped_browser_login_required"
        }
        else None,
        "last_precondition_skip_reason_code": raw.get("last_precondition_skip_reason_code")
        if isinstance(raw.get("last_precondition_skip_reason_code"), str)
        and raw.get("last_precondition_skip_reason_code") in _SCAN_BROWSER_REASON_CODES
        else None,
        "last_precondition_skip_at": timestamp("last_precondition_skip_at"),
        "last_precondition_skip_count": count("last_precondition_skip_count"),
        "last_unit_failure_kind": raw.get("last_unit_failure_kind")
        if raw.get("last_unit_failure_kind") in {
            "listing", "detail", "evaluation", "draft", "contract_monitor", "tick"
        }
        else None,
        "last_unit_failure_code": raw.get("last_unit_failure_code")
        if raw.get("last_unit_failure_code") in {
            "crowdworks_unit_failed",
            "crowdworks_unit_failure_limit_reached",
            UNIT_TIMEOUT_CODE,
        }
        else None,
        "last_unit_failure_at": timestamp("last_unit_failure_at"),
        "last_unit_failure_count": count("last_unit_failure_count"),
        "last_unit_failure_stopped": raw.get("last_unit_failure_stopped") is True,
        "last_unit_failure_subcode": fixed_code(
            "last_unit_failure_subcode",
            CROWDWORKS_DETAIL_FAILURE_CODES | {UNIT_TIMEOUT_CODE},
        ),
        "last_unit_failure_exception_class": (
            raw.get("last_unit_failure_exception_class")
            if isinstance(raw.get("last_unit_failure_exception_class"), str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", raw["last_unit_failure_exception_class"])
            else None
        ),
        "last_unit_failure_exception_source": (
            raw.get("last_unit_failure_exception_source")
            if isinstance(raw.get("last_unit_failure_exception_source"), str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.py:\d{1,6}", raw["last_unit_failure_exception_source"])
            else None
        ),
        "last_unit_failure_sqlstate": (
            raw.get("last_unit_failure_sqlstate")
            if isinstance(raw.get("last_unit_failure_sqlstate"), str)
            and re.fullmatch(r"[0-9A-Z]{5}", raw["last_unit_failure_sqlstate"])
            else None
        ),
        "detail_retry_stopped": raw.get("detail_retry_stopped") is True,
        "detail_retry_resume_count": bounded_count("detail_retry_resume_count", 3),
        "detail_retry_stop_code": fixed_code(
            "detail_retry_stop_code", CROWDWORKS_DETAIL_STOP_CODES
        ),
        "detail_retry_last_resumed_at": timestamp("detail_retry_last_resumed_at"),
        "last_notification_code": fixed_code(
            "last_notification_code", CROWDWORKS_SCAN_NOTIFICATION_REASON_CODES
        ),
        "last_notification_attempted_at": timestamp("last_notification_attempted_at"),
        "last_notification_sent_at": timestamp("last_notification_sent_at"),
        "next_notification_sendable_at": timestamp("next_notification_sendable_at"),
        "last_notification_delivery_code": raw.get("last_notification_delivery_code")
        if raw.get("last_notification_delivery_code")
        == "crowdworks_notification_delivery_failed"
        else None,
        "notifications_by_reason": notification_records(),
        "draft_candidate_id": opportunity_id("draft_candidate_id"),
        "draft_reason_code": fixed_code("draft_reason_code", DRAFT_REASON_CODES),
        "draft_provider_subcode": (
            raw.get("draft_provider_subcode")
            if isinstance(raw.get("draft_provider_subcode"), str)
            and raw.get("draft_provider_subcode")
            in {"http_402", "http_429", "quota_exhausted", "rate_limited", "free_team_budget_insufficient", "connection_reset"}
            else None
        ),
        "draft_provider_diagnostic": draft_provider_diagnostic(),
        "draft_route_model": (
            raw.get("draft_route_model")
            if isinstance(raw.get("draft_route_model"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", raw["draft_route_model"])
            else None
        ),
        "draft_route_fallback": (
            raw.get("draft_route_fallback")
            if raw.get("draft_route_fallback") in {"none", "luna"}
            else None
        ),
        "qa_route_model": (
            raw.get("qa_route_model")
            if isinstance(raw.get("qa_route_model"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", raw["qa_route_model"])
            else None
        ),
        "qa_route_fallback": (
            raw.get("qa_route_fallback")
            if raw.get("qa_route_fallback") in {"none", "luna"}
            else None
        ),
        "application_qa_failure_code_counts": code_counts(
            "application_qa_failure_code_counts", APPLICATION_QA_FAILURE_CODES
        ),
        "application_qa_duration_ms": bounded_count(
            "application_qa_duration_ms", 1_800_000
        ),
        "application_qa_attempt_count": count("application_qa_attempt_count"),
        "draft_generation_attempt_count": count("draft_generation_attempt_count"),
        "application_qa_retry_after": timestamp("application_qa_retry_after"),
        "provider_circuit_skip_code": (
            "skipped_provider_circuit_open"
            if raw.get("provider_circuit_skip_code") == "skipped_provider_circuit_open"
            else None
        ),
        "provider_circuit_reason_code": (
            raw.get("provider_circuit_reason_code")
            if isinstance(raw.get("provider_circuit_reason_code"), str)
            and raw.get("provider_circuit_reason_code") in EVALUATION_CIRCUIT_OPEN_REASON_CODES
            else None
        ),
        "draft_gate_opportunity_id": opportunity_id("draft_gate_opportunity_id"),
        "draft_gate_reason_code": fixed_code("draft_gate_reason_code", DRAFT_REASON_CODES),
        "estimate_revenue_capped_count": count("estimate_revenue_capped_count"),
        "estimate_exclusion_code_counts": code_counts(
            "estimate_exclusion_code_counts",
            frozenset({
                ESTIMATE_SANITY_REWARD_UNKNOWN_CODE,
                ESTIMATE_SANITY_RATE_TOO_HIGH_CODE,
            }),
        ),
        "unit_failure_count": count("unit_failure_count"),
        "unit_stopped": raw.get("unit_stopped") is True,
        "last_listing_started_at": timestamp("last_listing_started_at"),
        "last_listing_fetched_at": timestamp("last_listing_fetched_at"),
        "last_detail_started_at": timestamp("last_detail_started_at"),
        "last_detail_at": timestamp("last_detail_at"),
        "last_evaluation_at": timestamp("last_evaluation_at"),
        "last_draft_at": timestamp("last_draft_at"),
        "cumulative_detail_attempted": count("cumulative_detail_attempted"),
        "cumulative_detail_completed": count("cumulative_detail_completed"),
        "cumulative_detail_failed": count("cumulative_detail_failed"),
        "cumulative_evaluation_attempted": count("cumulative_evaluation_attempted"),
        "cumulative_evaluation_succeeded": count("cumulative_evaluation_succeeded"),
        "cumulative_evaluation_failed": count("cumulative_evaluation_failed"),
        "cumulative_drafted": count("cumulative_drafted"),
        "cumulative_proposed": count("cumulative_proposed"),
        "window_detail_attempted": count("window_detail_attempted"),
        "window_evaluation_attempted": count("window_evaluation_attempted"),
        "window_draft_attempted": count("window_draft_attempted"),
        "scan_exception_class": (
            raw.get("scan_exception_class")
            if isinstance(raw.get("scan_exception_class"), str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", raw["scan_exception_class"])
            else None
        ),
        "scan_exception_source": (
            raw.get("scan_exception_source")
            if isinstance(raw.get("scan_exception_source"), str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.py:\d{1,6}", raw["scan_exception_source"])
            else None
        ),
        "scan_sqlstate": (
            raw.get("scan_sqlstate")
            if isinstance(raw.get("scan_sqlstate"), str)
            and re.fullmatch(r"[0-9A-Z]{5}", raw["scan_sqlstate"])
            else None
        ),
        "unit_timeout_code": fixed_code(
            "unit_timeout_code", frozenset({UNIT_TIMEOUT_CODE})
        ),
        "unit_timeout_retry_count": bounded_count("unit_timeout_retry_count", 1),
        "unit_timeout_exception_class": (
            raw.get("unit_timeout_exception_class")
            if isinstance(raw.get("unit_timeout_exception_class"), str)
            and re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]{0,47}",
                raw["unit_timeout_exception_class"],
            )
            else None
        ),
        "unit_timeout_exception_source": (
            raw.get("unit_timeout_exception_source")
            if isinstance(raw.get("unit_timeout_exception_source"), str)
            and re.fullmatch(
                r"[A-Za-z0-9_.-]{1,80}\.py:\d{1,6}",
                raw["unit_timeout_exception_source"],
            )
            else None
        ),
        "listing_count": count("listing_count"),
        "new_created_count": count("new_created_count"),
        "detail_fetch": {
            "attempted": count("detail_fetch_attempted"),
            "succeeded": count("detail_fetch_completed"),
            "failed": count("detail_fetch_failed"),
            "deferred": count("detail_fetch_deferred"),
            "failure_code_counts": code_counts("detail_failure_code_counts", _SCAN_DETAIL_FAILURE_CODES),
        },
        "detail_candidate_reason_code_counts": code_counts(
            "detail_candidate_reason_code_counts", DETAIL_CANDIDATE_REASON_CODES
        ),
        "no_unit_reason_code_counts": code_counts(
            "no_unit_reason_code_counts", CROWDWORKS_NO_UNIT_REASON_CODES
        ),
        "screening_gate_counts": {
            key: max(0, min(int(gates.get(key)), 1_000_000)) if type(gates.get(key)) is int else 0
            for key in ("deadline_unverified", "expired", "requirement_short")
        },
        "requirement_short_category_counts": code_counts(
            "requirement_short_category_counts", EVALUATION_REQUIREMENT_CATEGORY_CODES
        ),
        "requirement_blocked_count": count("requirement_blocked_count"),
        "requirement_clarifiable_allowed_count": count("requirement_clarifiable_allowed_count"),
        "requirement_clarifiable_item_count": count("requirement_clarifiable_item_count"),
        "requirement_severity_counts": code_counts(
            "requirement_severity_counts", frozenset({"blocking", "clarifiable"})
        ),
        "requirement_severity_category_counts": safe_severity_categories,
        "requirement_invalid_count": count("requirement_invalid_count"),
        "evaluated_count": count("evaluated"),
        "drafted_count": count("drafted"),
        "proposed_count": count("proposed"),
        "evaluation_candidates": count("evaluation_candidates"),
        "evaluation_loop_entered": raw.get("evaluation_loop_entered") is True,
        "evaluation_rows_loaded": count("evaluation_rows_loaded"),
        "evaluation_selection_limit_reached": raw.get("evaluation_selection_limit_reached") is True,
        "evaluation_candidate_window_exclusion_code_counts": code_counts(
            "evaluation_candidate_window_exclusion_code_counts",
            EVALUATION_WINDOW_EXCLUSION_CODES,
        ),
        "evaluation_candidate_window_prioritized_count": count(
            "evaluation_candidate_window_prioritized_count"
        ),
        "evaluation_draft_revisit_count": count("evaluation_draft_revisit_count"),
        "evaluation_circuit_open": raw.get("evaluation_circuit_open") is True,
        "evaluation_circuit_open_reason_code": (
            raw.get("evaluation_circuit_open_reason_code")
            if isinstance(raw.get("evaluation_circuit_open_reason_code"), str)
            and raw.get("evaluation_circuit_open_reason_code")
            in EVALUATION_CIRCUIT_OPEN_REASON_CODES
            else None
        ),
        "provider_retry_budget_rearmed_count": count("evaluation_provider_retry_budget_rearmed_count"),
        "evaluation_fallback_invocation_count": count("evaluation_fallback_invocation_count"),
        "evaluation_route_counts": safe_route_counts,
        "evaluation_route_severity_counts": safe_route_severity_counts,
        "evaluation_request_diagnostics": evaluation_request_diagnostics(),
        "evaluation_request_diagnostics_truncated_count": count(
            "evaluation_request_diagnostics_truncated_count"
        ),
        "evaluation_attempted": count("evaluation_attempted"),
        "evaluation_succeeded": count("evaluation_succeeded"),
        "evaluation_failed": count("evaluation_failed"),
        "evaluation_failure_code_counts": code_counts(
            "evaluation_failure_code_counts", _SCAN_EVALUATION_FAILURE_CODES
        ),
        "evaluation_missing_field_counts": code_counts(
            "evaluation_missing_field_counts", EVALUATION_MISSING_FIELD_CODES
        ),
        "evaluation_missing_field_repair_attempted_count": count(
            "evaluation_missing_field_repair_attempted_count"
        ),
        "evaluation_missing_field_repair_succeeded_count": count(
            "evaluation_missing_field_repair_succeeded_count"
        ),
        "evaluation_not_attempted_code_counts": code_counts(
            "evaluation_not_attempted_code_counts", _SCAN_EVALUATION_NOT_ATTEMPTED_CODES
        ),
        "evaluation_response_metrics": {
            "generation_calls": count("evaluation_generation_calls"),
            "response_count": count("evaluation_response_count"),
            "response_char_count": count("evaluation_response_char_count"),
            "response_item_count": count("evaluation_response_item_count"),
            "parse_error_code_counts": code_counts(
                "evaluation_parse_error_code_counts", _SCAN_EVALUATION_PARSE_ERROR_CODES
            ),
            "provider_error_code_counts": provider_error_code_counts(),
            "provider_exception_subcode_counts": provider_exception_subcode_counts(),
            "provider_exception_source_counts": provider_exception_source_counts(),
            "finish_reason_counts": code_counts(
                "evaluation_finish_reason_counts", EVALUATION_FINISH_REASON_CODES
            ),
        },
    }


class CrowdWorksBootstrapService:
    """Materialize the standard Agent/Connection/Policy without a manual wizard."""

    def __init__(self, db_manager: Any, *, config: Any = None, policy_service: AgentActionPolicyService | None = None):
        self.db_manager = db_manager
        self.config = config
        self.identity = AgentIdentityService(db_manager, config=config)
        self.organizations = OrganizationService(db_manager)
        self.operations = OperationsService()
        self.policies = policy_service or AgentActionPolicyService(db_manager, config=config)

    @staticmethod
    def _actor(actor_user_id: Any) -> dict[str, str]:
        return {"id": str(actor_user_id), "actor_type": "human"}

    def _default_constraints(self) -> dict[str, Any]:
        # The Policy no longer chooses a model.  The Agent's AgentRevision
        # Revision role routes are the canonical model source; the deprecated
        # ``llm_*`` fields stay empty for new setups.
        return CrowdWorksPolicy.model_validate(dict(DEFAULT_CONSTRAINTS)).model_dump()

    async def _ensure_organization(self, actor_user_id: Any) -> dict[str, Any]:
        current = await self.organizations.get(bootstrap=False)
        if current is None:
            return await self.organizations.bootstrap(
                {
                    "display_name": "AoiTalk",
                    "locale": "ja-JP",
                    "timezone": "Asia/Tokyo",
                    "autonomy_level": "supervised",
                    "policy": {
                        "allow_agent_runtime": True,
                        "allow_external_actions": True,
                        "require_human_approval": True,
                        "allowed_capabilities": list(ORGANIZATION_CAPABILITIES),
                    },
                    "budget_policy": {},
                }
            )

        # Existing organization ceilings and autonomy are user-owned. Setup is
        # not permission to undo a revocation or broaden the installation.
        return current

    async def _ensure_employee(
        self,
        actor_user_id: Any,
        *,
        slug: str,
        bootstrap_key: str,
        display_name: str,
        revision: dict[str, Any],
        profile: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create or reuse one Agent through the normal identity service."""

        session = await self.db_manager.get_session()
        try:
            row = await session.scalar(select(Agent).where(Agent.slug == slug).limit(1))
            agent = row.to_safe_dict() if row is not None else None
        finally:
            await session.close()

        created = agent is None
        if created:
            agent = await self.identity.create_agent(
                display_name=display_name,
                slug=slug,
                idempotency_key=bootstrap_key,
                actor_user_id=actor_user_id,
            )
        if agent.get("state") == "retired":
            raise RuntimeError(f"crowdworks_bootstrap_agent_retired:{slug}")

        session = await self.db_manager.get_session()
        try:
            rows = (
                await session.scalars(
                    select(AgentRevision)
                    .where(AgentRevision.agent_id == UUID(str(agent["id"])))
                    .order_by(AgentRevision.version.desc())
                )
            ).all()
            revisions = []
            for row in rows:
                record = row.to_safe_dict()
                record["_bootstrap_idempotency_key"] = row.idempotency_key
                revisions.append(record)
        finally:
            await session.close()
        # Latest means the user's current identity contract, even when it is
        # intentionally less capable. Never resurrect a bootstrap revision or
        # publish an automatic upgrade above a newer user-authored revision.
        current_revision = revisions[0] if revisions else None
        if current_revision is None:
            current_revision = await self.identity.create_revision(
                agent_id=agent["id"],
                display_name=display_name,
                idempotency_key=bootstrap_key,
                actor_user_id=actor_user_id,
                **revision,
            )
        current_revision.pop("_bootstrap_idempotency_key", None)
        session = await self.db_manager.get_session()
        try:
            existing_profile = await session.get(AgentOrganizationProfile, UUID(str(agent["id"])))
        finally:
            await session.close()
        if existing_profile is None:
            await self.identity.upsert_organization_profile(
                agent_id=agent["id"], actor_user_id=actor_user_id, payload=profile,
            )
        current = await self.identity.get_agent(agent["id"])
        if created and current and current.get("state") == "draft":
            agent = await self.identity.transition_agent(
                agent["id"], "active", actor_user_id=actor_user_id,
                expected_state="draft",
            )
        else:
            agent = current or agent
        return agent, current_revision

    async def _ensure_agent(self, actor_user_id: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._ensure_employee(
            actor_user_id,
            slug=AGENT_SLUG,
            bootstrap_key=BOOTSTRAP_KEY,
            display_name="CrowdWorks担当",
            revision={
                "mission": "利益期待値の高いCrowdWorks案件を継続的に発見し、採算評価・応募・受注後作業を進める。",
                "responsibility_summary": "案件取得、採算評価、応募、契約監視、レビュー前成果物の作成。Web制作・アプリ開発案件は専門担当へ引き継ぐ。",
                "operational_instructions": (
                    "CrowdWorksの外部操作は登録済みengagement.submit_application Policyの範囲だけで行う。"
                    "応募結果が不明な場合は再送せずuncertainとして停止する。最終納品は行わない。"
                ),
                "model_overrides": {},
                "tool_ids": ["external_action_propose"],
                "delegate_policy": {"agents": [], "workers": []},
                "capability_ceiling": ["external_action_propose"],
                "wake_policy": {"mode": "interval", "interval_seconds": 1800, "max_wakes_per_day": 48},
                "concurrency_policy": {"max_parallel_runs": 1, "max_parallel_tasks": 1, "queue_class": "marketplace"},
                "budget_policy": {},
            },
            profile={
                "job_title": "CrowdWorks担当",
                "responsibility_summary": "案件探索から最終レビュー待ちまでを自動化する。",
                "autonomy_level": "bounded",
                "employment_state": "active",
                "company_permission_ceiling": {
                    "capabilities": ["external_action_propose"],
                },
            },
        )

    async def _ensure_specialist(
        self, actor_user_id: Any, spec: SpecialistSpec
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the specialist Agent with an explicit v2 tool ceiling.

        The model/runner is chosen by the revision's role routes, so a human
        can change it from the Agent screen by creating a revision.
        """

        return await self._ensure_employee(
            actor_user_id,
            slug=spec.slug,
            bootstrap_key=spec.bootstrap_key,
            display_name=spec.display_name,
            revision={
                "mission": spec.mission,
                "responsibility_summary": spec.responsibility_summary,
                "operational_instructions": spec.operational_instructions,
                "model_overrides": {},
                "tool_ids": list(DEVELOPMENT_CAPABILITIES),
                "delegate_policy": {"agents": [], "workers": []},
                "capability_ceiling": list(SPECIALIST_CAPABILITY_CEILING),
                "wake_policy": {"mode": "event"},
                "concurrency_policy": {"max_parallel_runs": 1, "max_parallel_tasks": 1, "queue_class": "client_development"},
                "budget_policy": {},
            },
            profile={
                "job_title": spec.display_name,
                "responsibility_summary": spec.responsibility_summary,
                "autonomy_level": "bounded",
                "employment_state": "active",
                "company_permission_ceiling": {
                    "capabilities": list(SPECIALIST_CAPABILITY_CEILING),
                },
            },
        )

    async def _ensure_specialists(self, actor_user_id: Any) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for kind, spec in SPECIALISTS.items():
            agent, revision = await self._ensure_specialist(actor_user_id, spec)
            result[kind] = {"agent": agent, "agent_revision_id": revision["id"]}
        return result

    async def _ensure_connection(self, session: Any, actor_user_id: Any) -> ExternalConnection:
        owner = UUID(str(actor_user_id))
        # A user may have replaced the setup connection. Its current policy
        # binding wins over the historical bootstrap marker.
        agent = await session.scalar(select(Agent).where(Agent.slug == AGENT_SLUG).limit(1))
        if agent is not None:
            policy = await session.scalar(select(AgentActionPolicy).where(
                AgentActionPolicy.agent_id == agent.id,
                AgentActionPolicy.idempotency_key == BOOTSTRAP_KEY,
            ).limit(1))
            current = await self.policies.current(session, policy.id) if policy else None
            bound = await session.get(ExternalConnection, current.connection_id) if current and current.connection_id else None
            if bound is not None and bound.owner_user_id == owner and bound.provider_key == "crowdworks":
                return bound
        rows = (
            await session.scalars(
                select(ExternalConnection)
                .where(
                    ExternalConnection.owner_user_id == owner,
                    ExternalConnection.provider_key == "crowdworks",
                    ExternalConnection.project_id.is_(None),
                )
                .order_by(ExternalConnection.created_at.asc())
                .limit(50)
            )
        ).all()
        for row in rows:
            metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            if metadata.get("bootstrap_key") == BOOTSTRAP_KEY:
                return row

        created = await self.operations.create_connection(
            session,
            self._actor(actor_user_id),
            provider_key="crowdworks",
            display_name="CrowdWorks",
            remote_account_ref="browser-session",
            auth_status="browser_session",
            project_id=None,
            metadata={"bootstrap_key": BOOTSTRAP_KEY},
        )
        return await session.get(ExternalConnection, UUID(created["id"]))

    async def _ensure_policy(
        self,
        session: Any,
        *,
        actor_user_id: Any,
        agent_id: str,
        connection: ExternalConnection,
    ) -> tuple[AgentActionPolicy, AgentActionPolicyRevision, bool, str | None]:
        policy = await session.scalar(
            select(AgentActionPolicy)
            .where(
                AgentActionPolicy.agent_id == UUID(str(agent_id)),
                AgentActionPolicy.idempotency_key == BOOTSTRAP_KEY,
            )
            .limit(1)
        )
        created = policy is None
        if created:
            projected = await self.policies.create_policy(
                session,
                actor_user_id=actor_user_id,
                agent_id=agent_id,
                display_name="CrowdWorks案件進行",
                idempotency_key=BOOTSTRAP_KEY,
            )
            await session.commit()
            policy = await session.get(AgentActionPolicy, UUID(projected["id"]))

        current = await self.policies.current(session, policy.id)
        constraints = self._default_constraints()
        desired_content = {
            "constraints": constraints,
            "authorization_mode": "human_approval",
            "rate_limit": DEFAULT_RATE_LIMIT,
            "dedupe_window_seconds": DEFAULT_DEDUPE_SECONDS,
            "fallback_behavior": "block",
            "connection_id": str(connection.id),
        }
        needs_revision = current is None
        if needs_revision:
            revision_key = "crowdworks-policy-" + sha256_json(desired_content)[:24]
            projected = await self.policies.create_revision(
                session,
                policy.id,
                actor_user_id=actor_user_id,
                expected_version=int(policy.version or 1),
                idempotency_key=revision_key,
                action_type="engagement.submit_application",
                connection_id=connection.id,
                authorization_mode="human_approval",
                constraints=constraints,
                rate_limit=DEFAULT_RATE_LIMIT,
                dedupe_window_seconds=DEFAULT_DEDUPE_SECONDS,
                fallback_behavior="block",
            )
            await session.commit()
            policy = await session.get(AgentActionPolicy, policy.id, populate_existing=True)
            current = await self.policies.current(session, policy.id)

        ready = False
        blocked_reason = None
        try:
            await self.policies._ready(session, current)
            ready = True
        except ActionPolicyError as exc:
            blocked_reason = exc.code

        if created and ready and policy.state == "draft":
            try:
                projected = await self.policies.update_policy(
                    session, policy.id, actor_user_id=actor_user_id,
                    expected_version=int(policy.version or 1), state="active",
                )
                await session.commit()
                policy = await session.get(AgentActionPolicy, UUID(projected["id"]), populate_existing=True)
            except ActionPolicyError as exc:
                # A connected browser is not authority to override an existing
                # organization restriction. Keep the new policy in draft.
                ready = False
                blocked_reason = exc.code

        return policy, current, ready, blocked_reason

    async def _agent_readiness(self, session, agent_id: Any, *, specialist: bool = False, project_id: Any = None) -> dict[str, Any]:
        """Inspect the same latest revision discovery will pin; never repair grants."""
        agent = await session.get(Agent, UUID(str(agent_id))) if agent_id else None
        revision = await session.scalar(
            select(AgentRevision).where(AgentRevision.agent_id == agent.id)
            .order_by(AgentRevision.version.desc()).limit(1)
        ) if agent else None
        result = {"ready": False, "agent_revision_id": str(revision.id) if revision else None,
                  "blocked_reason": None, "model_routes": {}}
        if agent is None or agent.state != "active" or revision is None:
            result["blocked_reason"] = "agent_inactive" if agent else "agent_missing"
            if agent and not revision:
                result["blocked_reason"] = "agent_revision_missing"
            return result
        resolver = AgentAuthorityResolver(self.db_manager, config=self.config)
        capabilities = DEVELOPMENT_CAPABILITIES if specialist else ("external_action_propose",)
        for capability in capabilities:
            decision = await resolver.resolve(agent_id=agent.id, revision_id=revision.id,
                                              required_capability=capability, project_id=project_id)
            if not decision.allowed:
                result["blocked_reason"] = decision.reason or "capability_denied"
                return result
        roles = ("planner", "code_worker", "reviewer") if specialist else ("planner", "writer", "reviewer")
        try:
            pins = AgentRoutingPins.from_revision(revision)
            result["model_routes"] = {}
            for role in roles:
                route = resolve_agent_role_route(self.config, pins, role)
                provenance = route.provenance()
                # The route adapter is v2-backed; expose only the neutral
                # provider/model projection. Legacy pins remain audit-only.
                result["model_routes"][role] = {
                    key: provenance[key]
                    for key in (
                        "agent_id", "agent_revision_id", "role", "provider",
                        "model", "effort", "route_source", "backend",
                    )
                    if key in provenance
                }
            if specialist:
                # Resolution alone does not prove that the selected provider
                # has a supported development backend. Never call it "ready"
                # when execution would silently need a different CLI.
                from .client_development import _client_role_provider_error, _native_worker_boundary_error

                execution_boundary = "file_tools_only; isolated_verification_required"
                # Report an unsupported role route before evaluating the
                # code-worker isolation probe. Read-only planner/reviewer
                # routes must retain their precise provider error even when
                # the mutating worker's backend is unavailable.
                role_order = ("code_worker", *(role for role in result["model_routes"] if role != "code_worker"))
                for role in role_order:
                    route = result["model_routes"].get(role)
                    if not isinstance(route, dict):
                        continue
                    mode = "mutate" if role == "code_worker" else "read_only"
                    provider_error = _client_role_provider_error(str(route.get("provider") or ""), role, mode)
                    if provider_error:
                        if role == "code_worker" and provider_error == "agent_route_provider_unsupported":
                            provider_error = "agent_route_development_provider_unsupported"
                        result["blocked_reason"] = provider_error
                        return result
                for role, route in result["model_routes"].items():
                    mode = "mutate" if role == "code_worker" else "read_only"
                    settings, plan = development_harness_settings(
                        self.config, route=route, workspace_root=Path(__file__).resolve().parent)
                    if role == "code_worker":
                        boundary_error = await asyncio.to_thread(
                            _native_worker_boundary_error, plan.runner, mode, settings=settings,
                        )
                        if boundary_error:
                            result["blocked_reason"] = boundary_error
                            result["execution_boundary"] = "isolated_execution_required"
                            return result
                        if plan.runner != "local_agent_task":
                            # Status does not issue execution authority. The
                            # coordinator must supply a fresh claim scope.
                            execution_boundary = "claim_scoped_execution; isolated_verification_required"
                result["execution_boundary"] = execution_boundary
        except AgentModelRouteError as exc:
            result["blocked_reason"] = exc.code
            return result
        result["ready"] = True
        return result

    async def bootstrap(self, actor_user_id: Any) -> dict[str, Any]:
        if not (Features.virtual_company() and Features.autonomous_agent_runtime()):
            raise RuntimeError("crowdworks_bootstrap_runtime_disabled")

        admin_session = await self.db_manager.get_session()
        try:
            await self.policies.admin(admin_session, actor_user_id)
        finally:
            await admin_session.close()
        await self._ensure_organization(actor_user_id)
        agent, revision = await self._ensure_agent(actor_user_id)
        specialists = await self._ensure_specialists(actor_user_id)

        session = await self.db_manager.get_session()
        try:
            await self.policies.admin(session, actor_user_id)
            connection = await self._ensure_connection(session, actor_user_id)
            policy, policy_revision, browser_ready, blocked_reason = await self._ensure_policy(
                session,
                actor_user_id=actor_user_id,
                agent_id=agent["id"],
                connection=connection,
            )
            readiness = await self._agent_readiness(session, agent["id"], project_id=connection.project_id)
            specialist_readiness = {kind: await self._agent_readiness(session, item["agent"]["id"], specialist=True)
                                    for kind, item in specialists.items()}
            return {
                "configured": True,
                "started": bool(browser_ready and policy.state == "active" and readiness["ready"]),
                "runtime_readiness": readiness,
                "specialist_readiness": specialist_readiness,
                "browser_ready": browser_ready,
                "blocked_reason": blocked_reason or readiness["blocked_reason"],
                "agent": agent,
                "agent_revision_id": revision["id"],
                "specialists": specialists,
                "connection": connection.to_safe_dict(),
                "policy": await self.policies.project(session, policy),
                "defaults": {
                    "constraints": self._default_constraints(),
                    "rate_limit": DEFAULT_RATE_LIMIT,
                    "dedupe_window_seconds": DEFAULT_DEDUPE_SECONDS,
                },
            }
        finally:
            await session.close()

    async def status(self, actor_user_id: Any) -> dict[str, Any]:
        session = await self.db_manager.get_session()
        try:
            await self.policies.admin(session, actor_user_id)
            agent = await session.scalar(select(Agent).where(Agent.slug == AGENT_SLUG).limit(1))
            connections = (
                await session.scalars(
                    select(ExternalConnection)
                    .where(
                        ExternalConnection.owner_user_id == UUID(str(actor_user_id)),
                        ExternalConnection.provider_key == "crowdworks",
                        ExternalConnection.project_id.is_(None),
                    )
                    .order_by(ExternalConnection.created_at.asc())
                    .limit(50)
                )
            ).all()
            connection = next(
                (
                    row
                    for row in connections
                    if isinstance(row.metadata_json, dict)
                    and row.metadata_json.get("bootstrap_key") == BOOTSTRAP_KEY
                ),
                None,
            )
            policy = (
                await session.scalar(
                    select(AgentActionPolicy)
                    .where(
                        AgentActionPolicy.agent_id == agent.id,
                        AgentActionPolicy.idempotency_key == BOOTSTRAP_KEY,
                    )
                    .limit(1)
                )
                if agent is not None
                else None
            )
            specialist_rows = {
                kind: await session.scalar(select(Agent).where(Agent.slug == spec.slug).limit(1))
                for kind, spec in SPECIALISTS.items()
            }
            revision = await self.policies.current(session, policy.id) if policy is not None else None
            if revision is not None:
                bound = await session.get(ExternalConnection, revision.connection_id) if revision.connection_id else None
                connection = bound if bound is not None and bound.owner_user_id == UUID(str(actor_user_id)) and bound.provider_key == "crowdworks" else None
                if connection is None:
                    revision = None
            browser_ready = False
            blocked_reason = None
            if revision is not None:
                try:
                    await self.policies._ready(session, revision)
                    browser_ready = True
                except ActionPolicyError as exc:
                    blocked_reason = exc.code
            readiness = await self._agent_readiness(session, agent.id if agent else None,
                                                    project_id=connection.project_id if connection else None)
            specialist_readiness = {kind: await self._agent_readiness(session, row.id if row else None, specialist=True)
                                    for kind, row in specialist_rows.items()}
            scan_summary = project_crowdworks_scan_summary({})
            if agent is not None and policy is not None and connection is not None:
                connection_metadata = (
                    connection.metadata_json if isinstance(connection.metadata_json, dict) else {}
                )
                persisted_scan_state = connection_metadata.get("crowdworks_scan_state")
                persisted_last_scan = (
                    persisted_scan_state.get("last_scan")
                    if isinstance(persisted_scan_state, dict)
                    else None
                )
                if isinstance(persisted_last_scan, dict):
                    scan_summary = project_crowdworks_scan_summary(persisted_last_scan)
                # Historical fallback only: legacy work is never scheduled by bootstrap.
                # The bound connection was checked against this authenticated owner above.
                # Keep this read pinned to that single connection; never use a global work projection.
                latest_work = await session.scalar(
                    select(AgentWorkItem)
                    .where(
                        AgentWorkItem.source_type == "marketplace_crowdworks",
                        AgentWorkItem.source_id == str(policy.id),
                        AgentWorkItem.intent_key == "marketplace.crowdworks.scan",
                        AgentWorkItem.domain == "marketplace",
                        AgentWorkItem.assigned_agent_id == agent.id,
                        AgentWorkItem.execution_adapter == "crowdworks_workflow",
                        AgentWorkItem.metadata_json["connection_id"].as_string() == str(connection.id),
                    )
                    .order_by(AgentWorkItem.updated_at.desc(), AgentWorkItem.id.desc())
                    .limit(1)
                )
                if latest_work is not None and scan_summary["result"] == "unknown":
                    scan_summary = project_crowdworks_scan_summary(latest_work.result_summary_json)
            return {
                "configured": bool(agent and connection and policy and revision),
                "runtime_readiness": readiness,
                "specialist_readiness": specialist_readiness,
                "started": bool(
                    agent
                    and agent.state == "active"
                    and policy
                    and policy.state == "active"
                    and browser_ready
                    and readiness["ready"]
                ),
                "browser_ready": browser_ready,
                "blocked_reason": blocked_reason or readiness["blocked_reason"],
                "agent": agent.to_safe_dict() if agent else None,
                "specialists": {
                    kind: ({"agent": row.to_safe_dict()} if row is not None else None)
                    for kind, row in specialist_rows.items()
                },
                "connection": connection.to_safe_dict() if connection else None,
                "policy": await self.policies.project(session, policy) if policy else None,
                "last_scan": scan_summary,
            }
        finally:
            await session.close()


__all__ = [
    "BOOTSTRAP_KEY",
    "DEFAULT_CONSTRAINTS",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_DEDUPE_SECONDS",
    "CrowdWorksBootstrapService",
]
