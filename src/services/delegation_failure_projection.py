"""Closed projection of delegate_plan failures for model and audit output."""

import json
import re

# Literal service-owned DelegationError codes; never accept arbitrary error text.
KNOWN_DELEGATION_ERRORS = frozenset({
    'deadline_expired',
    'delegate_policy_denied',
    'delegation_artifact_corrupt',
    'delegation_artifact_not_adopted',
    'delegation_attempt_already_reserved',
    'delegation_authority_denied',
    'delegation_binding_mismatch',
    'delegation_bounded_route_required',
    'delegation_child_return_missing',
    'delegation_claim_invalid',
    'delegation_cli_privacy_blocked',
    'delegation_code_requires_native_work',
    'delegation_concurrent_create',
    'delegation_context_too_large',
    'delegation_dependency_blocked',
    'delegation_dependency_changed',
    'delegation_dependency_evidence_missing',
    'delegation_dependency_parent_changed',
    'delegation_depth_exceeded',
    'delegation_employment_profile_missing',
    'delegation_ephemeral_route_unsupported',
    'delegation_execution_context_missing',
    'delegation_graph_agent_invalid',
    'delegation_graph_authorization_mismatch',
    'delegation_graph_constraints_server_only',
    'delegation_graph_input_changed',
    'delegation_graph_input_conflict',
    'delegation_graph_invalid',
    'delegation_graph_kind_mismatch',
    'delegation_graph_leader_mismatch',
    'delegation_graph_media_tool_unavailable',
    'delegation_graph_pair_invalid',
    'delegation_graph_plan_tool_unavailable',
    'delegation_graph_planning_required',
    'delegation_graph_project_mismatch',
    'delegation_graph_ref_mismatch',
    'delegation_graph_revision_invalid',
    'delegation_graph_route_unavailable',
    'delegation_human_authority_revoked',
    'delegation_idempotency_conflict',
    'delegation_independent_employees_required',
    'delegation_input_scope_missing',
    'delegation_invalid_id',
    'delegation_invalid_input',
    'delegation_invalid_role',
    'delegation_lease_lost',
    'delegation_media_input_denied',
    'delegation_media_input_invalid',
    'delegation_media_input_unsupported',
    'delegation_media_invoker_unsupported',
    'delegation_media_reader_unavailable',
    'delegation_native_artifact_binding_invalid',
    'delegation_native_artifact_corrupt',
    'delegation_native_artifact_scope_mismatch',
    'delegation_native_artifacts_missing',
    'delegation_native_checks_incomplete',
    'delegation_native_dependencies_unsupported',
    'delegation_native_report_binding_invalid',
    'delegation_native_report_invalid',
    'delegation_native_report_too_large',
    'delegation_native_requirements_changed',
    'delegation_native_result_missing',
    'delegation_native_review_missing',
    'delegation_native_review_provenance_missing',
    'delegation_native_run_missing',
    'delegation_native_runtime_unavailable',
    'delegation_native_source_bounds',
    'delegation_native_source_invalid',
    'delegation_native_source_missing',
    'delegation_native_source_secret',
    'delegation_native_source_unsafe',
    'delegation_native_work_binding_changed',
    'delegation_native_work_binding_invalid',
    'delegation_native_work_missing',
    'delegation_not_found',
    'delegation_operations_source_changed',
    'delegation_operations_source_invalid',
    'delegation_operations_source_scope_mismatch',
    'delegation_output_too_large',
    'delegation_parent_already_in_review',
    'delegation_parent_plan_changed',
    'delegation_parent_scope_mismatch',
    'delegation_parent_terminal',
    'delegation_parent_unavailable',
    'delegation_persona_project_mismatch',
    'delegation_persona_read_ceiling_denied',
    'delegation_plan_adoption_changed',
    'delegation_plan_assignment_invalid',
    'delegation_plan_attempt_budget_exhausted',
    'delegation_plan_children_budget_exhausted',
    'delegation_plan_depth_exceeded',
    'delegation_plan_fanout_invalid',
    'delegation_plan_graph_assignment_invalid',
    'delegation_plan_graph_dependency_invalid',
    'delegation_plan_graph_nested_unsupported',
    'delegation_plan_independent_employees_required',
    'delegation_plan_input_too_large',
    'delegation_plan_nested_roster_invalid',
    'delegation_plan_output_budget_exhausted',
    'delegation_plan_rework_budget_exhausted',
    'delegation_planned_parent_requires_adopted_plan',
    'delegation_planning_brand_changed',
    'delegation_planning_brand_unknown',
    'delegation_planning_headquarters_required',
    'delegation_planning_input_changed',
    'delegation_planning_input_missing',
    'delegation_planning_input_not_adopted',
    'delegation_planning_invalid',
    'delegation_planning_owner_required',
    'delegation_planning_policy_missing',
    'delegation_planning_root_unavailable',
    'delegation_planning_roster_invalid',
    'delegation_prior_evidence_missing',
    'delegation_project_management_denied',
    'delegation_project_missing',
    'delegation_reporting_relation_revoked',
    'delegation_retry_limit',
    'delegation_retry_not_allowed',
    'delegation_return_invalid',
    'delegation_review_verdict_invalid',
    'delegation_rework_evidence_missing',
    'delegation_run_invalid',
    'delegation_runtime_policy_denied',
    'delegation_scope_changed',
    'delegation_software_graph_invalid',
    'delegation_source_changed',
    'delegation_source_inactive',
    'delegation_source_invalid',
    'delegation_source_override_denied',
    'delegation_source_scope_mismatch',
    'delegation_tool_free_route_required',
    'delegation_workspace_denied',
    'employee_runtime_disabled',
    'team_graph_group_not_ready',
    'team_graph_handoff_ambiguous',
    'team_graph_handoff_invalid',
    'team_media_outcome_invalid',
    'team_media_return_invalid',
})
_FRAME = re.compile(r"([A-Za-z0-9_/.-]+\.py):([A-Za-z_][A-Za-z_0-9]{0,79}):([1-9][0-9]{0,6})")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,79}")

def project_delegate_failure(value):
    """Rebuild a bounded, non-sensitive failure line from untrusted output."""
    if isinstance(value, str):
        if value.startswith("Error: "):
            value = value[7:]
        if value in KNOWN_DELEGATION_ERRORS:
            return "Error: " + value
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = None
    if not isinstance(value, dict):
        return "Error: tool_execution_failed"
    code = value.get("error")
    if isinstance(code, str) and code in KNOWN_DELEGATION_ERRORS:
        return "Error: " + code
    diagnostic = value.get("diagnostic")
    if code != "tool_execution_failed" or not isinstance(diagnostic, dict):
        return "Error: tool_execution_failed"
    phase, cls, frames = (diagnostic.get(k) for k in ("phase", "exception_class", "frames"))
    if phase not in ("service", "construct", "create_from_agent") or not isinstance(cls, str) or not _IDENTIFIER.fullmatch(cls) or not isinstance(frames, list) or len(frames) > 8:
        return "Error: tool_execution_failed"
    safe_frames = []
    for frame in frames:
        match = _FRAME.fullmatch(frame) if isinstance(frame, str) and len(frame) <= 240 else None
        if not match:
            return "Error: tool_execution_failed"
        if any(part in ("", ".", "..") for part in match.group(1).split("/")):
            return "Error: tool_execution_failed"
        safe_frames.append(frame)
    return "Error: " + json.dumps({"error": "tool_execution_failed", "diagnostic": {"phase": phase, "exception_class": cls, "frames": safe_frames}}, separators=(",", ":"))
