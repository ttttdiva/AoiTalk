"""Read-only vocabulary for projecting historical CrowdWorks scan summaries."""

from typing import Any

from .crowdworks_application_qa import EVALUATION_PROVIDER_ERROR_CODES, is_evaluation_provider_error_code
from .crowdworks_source import DETAIL_CANDIDATE_REASON_CODES, _DETAIL_BRIDGE_ERROR_CODES, _DETAIL_INSPECTION_ERRORS

ESTIMATE_SANITY_REWARD_UNKNOWN_CODE = "estimate_unreliable_reward_unknown"
ESTIMATE_SANITY_RATE_TOO_HIGH_CODE = "estimate_unreliable_rate_too_high"
DRAFT_CONNECTION_RESET_BACKOFF_CODE = "draft_connection_reset_backoff"
DRAFT_REASON_CODES = frozenset({
    "already_proposed",
    "gated_expired",
    "gated_deadline_unverified",
    "gated_brief_unverified",
    "gated_human_review_required",
    "evaluation_missing",
    "gated_blocking_requirements",
    "application_qa_failed_same_binding",
    "application_qa_retry_cooldown",
    "evaluation_economics_missing",
    "gated_below_profit_threshold",
    "gated_below_profit_per_hour",
    ESTIMATE_SANITY_REWARD_UNKNOWN_CODE,
    ESTIMATE_SANITY_RATE_TOO_HIGH_CODE,
    "action_rate_limited",
    "draft_candidate_selected",
    "specialist_unavailable",
    "specialist_review_pending",
    "specialist_declined",
    "evaluation_incomplete",
    "application_output_missing",
    "provider_unavailable",
    DRAFT_CONNECTION_RESET_BACKOFF_CODE,
    "draft_retry_rearmed",
    "application_text_invalid",
    "application_text_requires_human_review",
    "application_qa_binding_mismatch",
    "application_qa_failed",
    "application_qa_unavailable",
    "draft_created",
    "draft_reused",
    "action_rate_limited_after_draft",
    "proposal_created",
    "draft_no_qualified_candidate",
})
CROWDWORKS_DETAIL_FAILURE_CODES = (
    _DETAIL_INSPECTION_ERRORS | _DETAIL_BRIDGE_ERROR_CODES
    | {"crowdworks_detail_capture_failed"}
)
CROWDWORKS_DETAIL_STOP_CODES = frozenset({
    "crowdworks_unit_failure_limit_reached",
    "crowdworks_detail_retry_limit_reached",
})
EVALUATION_FAILURE_CODES = frozenset({
    "parse_error",
    "missing_target_id",
    "missing_fields",
    "provider_unavailable",
    "timeout",
    "evaluation_output_too_large",
})
EVALUATION_MISSING_FIELD_CODES = frozenset({
    "estimated_effort_hours",
    "estimated_revenue",
    "fit",
    "summary",
    "risks",
    "missing_requirements",
    "confidence",
})
EVALUATION_WINDOW_EXCLUSION_CODES = frozenset({
    "deadline_unverified",
    "expired",
    "brief_unverified",
    "terminal_below_profit",
    "has_draft",
    "has_action",
    "quota_cooldown",
    "retry_limit",
    "legacy_requirements_retry_limit",
    "terminal_output_too_large",
    "already_proposed",
    "unit_failure_limit",
    ESTIMATE_SANITY_REWARD_UNKNOWN_CODE,
    ESTIMATE_SANITY_RATE_TOO_HIGH_CODE,
    DRAFT_CONNECTION_RESET_BACKOFF_CODE,
    "application_qa_retry_cooldown",
})
EVALUATION_NOT_ATTEMPTED_CODES = frozenset({
    "gated_deadline_unverified",
    "gated_expired",
    "gated_brief_unverified",
    "evaluation_retry_limit",
    "evaluation_provider_quota_not_retried",
    "evaluation_limit",
    "existing_evaluation_reused",
    "already_proposed",
    "evaluation_loop_not_entered",
    "evaluation_selection_failed",
    "evaluation_target_set_empty",
    "evaluation_route_unavailable",
    "application_route_unavailable",
    "agent_revision_route_unavailable",
    "evaluation_output_too_large",
    "evaluation_circuit_open",
    "legacy_requirements_retry_limit",
})
CROWDWORKS_NO_UNIT_REASON_CODES = frozenset({
    "draft_candidate_not_selected",
    "draft_candidate_window_empty",
    "draft_rate_window_full",
    "evaluation_candidate_window_empty",
    "evaluation_circuit_open",
    "evaluation_rate_window_full",
    "detail_candidate_window_empty",
    "detail_rate_window_full",
    "detail_retry_candidates_not_returned",
    "detail_unit_stopped",
    "listing_capacity_reached",
    "listing_not_due",
    "listing_unit_stopped",
    "contract_monitor_no_candidate",
    "contract_monitor_unit_stopped",
} | {
    *(f"draft_gate_{code}" for code in DRAFT_REASON_CODES),
    *(f"evaluation_excluded_{code}" for code in EVALUATION_WINDOW_EXCLUSION_CODES),
    *(f"evaluation_not_attempted_{code}" for code in EVALUATION_NOT_ATTEMPTED_CODES),
    *DETAIL_CANDIDATE_REASON_CODES,
})
EVALUATION_PARSE_ERROR_CODES = frozenset({
    "empty_response",
    "not_json",
    "wrong_shape_not_array",
    "empty_array",
    "multiple_json_values",
    "truncated_length",
    "incomplete_output",
    "refusal",
    "provider_error_body",
})
EVALUATION_FINISH_REASON_CODES = frozenset({"length", "max_output_tokens", "other"})
EVALUATION_REQUEST_DIAGNOSTIC_OUTCOME_CODES = frozenset(
    EVALUATION_FAILURE_CODES
    | EVALUATION_PROVIDER_ERROR_CODES
    | EVALUATION_PARSE_ERROR_CODES
    | {"completed", "response_received", "provider_exception"}
)
EVALUATION_CIRCUIT_OPEN_REASON_CODES = frozenset({
    "provider_failure_threshold",
    "free_team_budget_insufficient",
})
UNIT_TIMEOUT_CODE = "unit_timeout"


def is_evaluation_request_outcome_code(value: Any) -> bool:
    return (
        isinstance(value, str)
        and (
            value in EVALUATION_REQUEST_DIAGNOSTIC_OUTCOME_CODES
            or is_evaluation_provider_error_code(value)
        )
    )
