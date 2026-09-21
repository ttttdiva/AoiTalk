"""
Services package for AoiTalk.

Contains service modules for various functionalities.
"""

from .project_context import (
    ProjectContextResolver,
    build_project_context,
    format_minimal_project_context_for_chat_prompt,
    format_project_context_for_chat_prompt,
    format_project_context_for_prompt,
    get_runtime_project_context,
    merge_project_metadata,
    normalize_project_metadata,
    reset_runtime_project_context,
    sanitize_project_context_for_chat,
    set_runtime_project_context,
)
from .mention_resolver import (
    CanonicalMention,
    MentionResolution,
    MentionResolver,
    normalize_mentions,
    resolve_mentions,
)

__all__ = [
    "ProjectContextResolver",
    "build_project_context",
    "format_minimal_project_context_for_chat_prompt",
    "format_project_context_for_chat_prompt",
    "format_project_context_for_prompt",
    "get_runtime_project_context",
    "merge_project_metadata",
    "normalize_project_metadata",
    "reset_runtime_project_context",
    "sanitize_project_context_for_chat",
    "set_runtime_project_context",
    "CanonicalMention",
    "MentionResolution",
    "MentionResolver",
    "normalize_mentions",
    "resolve_mentions",
    "ActorPrincipal",
    "ActorPrincipalError",
    "AuthorityDecision",
    "AgentAuthorityResolver",
    "AgentIdentityService",
    "AgentService",
    "OrganizationService",
    "OrganizationSettingsService",
    "OrganizationBootstrapService",
    "AgentWorkCoordinator",
    "WorkSource",
    "ExecutionAdapter",
    "WorkCandidate",
    "WorkClaim",
    "ExecutionOutcome",
    "TaskWorkSource",
    "TaskExecutionAdapter",
    "OperationsCommandCenterService",
]


def __getattr__(name):
    """Lazily expose WS01 services without creating import cycles."""

    if name in {"ActorPrincipal", "ActorPrincipalError"}:
        from .actor_principal import ActorPrincipal, ActorPrincipalError

        value = {
            "ActorPrincipal": ActorPrincipal,
            "ActorPrincipalError": ActorPrincipalError,
        }[name]
    elif name in {"AuthorityDecision", "AgentAuthorityResolver"}:
        from .agent_authority import AuthorityDecision, AgentAuthorityResolver

        value = {
            "AuthorityDecision": AuthorityDecision,
            "AgentAuthorityResolver": AgentAuthorityResolver,
        }[name]
    elif name in {
        "AgentIdentityService",
        "AgentService",
        "OrganizationService",
        "OrganizationSettingsService",
        "OrganizationBootstrapService",
    }:
        from .agent_identity_service import (
            AgentIdentityService,
            AgentService,
            OrganizationService,
        )

        value = {
            "AgentIdentityService": AgentIdentityService,
            "AgentService": AgentService,
            "OrganizationService": OrganizationService,
            "OrganizationSettingsService": OrganizationService,
            "OrganizationBootstrapService": OrganizationService,
        }[name]
    elif name in {
        "AgentWorkCoordinator",
        "WorkSource",
        "ExecutionAdapter",
        "WorkCandidate",
        "WorkClaim",
        "ExecutionOutcome",
    }:
        from .agent_work_runtime import (
            AgentWorkCoordinator,
            ExecutionAdapter,
            ExecutionOutcome,
            WorkCandidate,
            WorkClaim,
            WorkSource,
        )

        value = {
            "AgentWorkCoordinator": AgentWorkCoordinator,
            "WorkSource": WorkSource,
            "ExecutionAdapter": ExecutionAdapter,
            "WorkCandidate": WorkCandidate,
            "WorkClaim": WorkClaim,
            "ExecutionOutcome": ExecutionOutcome,
        }[name]
    elif name == "TaskWorkSource":
        from .task_work_source import TaskWorkSource

        value = TaskWorkSource
    elif name == "TaskExecutionAdapter":
        from .task_execution_adapter import TaskExecutionAdapter

        value = TaskExecutionAdapter
    elif name == "OperationsCommandCenterService":
        from .operations_command_center import OperationsCommandCenterService

        value = OperationsCommandCenterService
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
