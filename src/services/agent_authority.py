"""Fail-closed effective authority resolver for generic Agents.

The resolver deliberately computes an intersection of independent ceilings. It
does not read Character prompts/tools, Persona text, Team IDs, execution
profiles, model output, or client payloads as grants.  Durable relationship
rows are queried fresh for every decision so revoked assignments cannot remain
cached in a worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import inspect
from typing import Any, Iterable, Mapping
from uuid import UUID

from sqlalchemy import select

from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentOrganizationProfile,
    AgentProjectGrant,
    AgentRevision,
    AgentSpaceAssignment,
    Organization,
    Persona,
    PersonaOperatorAssignment,
    Project,
)
from .agent_team_v3 import (
    AGENT_TEAM_CAPABILITY_CATALOG,
    AGENT_TEAM_DEFAULT_TEAMS,
AGENT_TEAM_SUBAGENT_CATALOG,
    agent_team_v3_subagents,
    agent_team_v3_teams,
)
from .project_permissions import normalize_project_member_permissions
from ..features import Features


_AUTHORITY_CAPABILITY_ALIASES = {
    "external_action": "media",
    "external_publish": "media",
    "media_publish": "media",
}


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """Safe, audit-friendly authority result."""

    allowed: bool
    agent_id: str | None = None
    agent_revision_id: str | None = None
    organization_id: str | None = None
    project_id: str | None = None
    space_id: str | None = None
    persona_id: str | None = None
    requested_capability: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    deny_reasons: tuple[str, ...] = ()

    @property
    def denied(self) -> bool:
        return not self.allowed

    @property
    def is_allowed(self) -> bool:
        return self.allowed

    @property
    def reason(self) -> str | None:
        return self.deny_reasons[0] if self.deny_reasons else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "space_id": self.space_id,
            "persona_id": self.persona_id,
            "requested_capability": self.requested_capability,
            "capabilities": sorted(self.capabilities),
            "deny_reasons": list(self.deny_reasons),
            "reason": self.reason,
        }


def _parse_uuid(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _active_window(row: Any, *, now: datetime) -> bool:
    if str(getattr(row, "state", "")) != "active":
        return False
    start = getattr(row, "active_from", None)
    end = getattr(row, "active_until", None)
    return not (start and start > now) and not (end and end < now)


def _required_project_permission(capability: str | None) -> str:
    """Map a catalog capability to the least project ACL it requires.

    The old resolver special-cased three write names and treated every other
    capability as read.  That made newly added write-family capabilities such
    as ``story_import`` accidentally executable with a read-only grant.  The
    canonical Team catalog is the authority for access class; unknown values
    remain read-only here and are rejected by the capability validator.
    """

    normalized = str(capability or "").strip()
    normalized = _AUTHORITY_CAPABILITY_ALIASES.get(normalized, normalized)
    item = AGENT_TEAM_CAPABILITY_CATALOG.get(normalized) or {}
    return "write" if item.get("access") == "write" else "read"


def _scope_capabilities(scope: Any) -> tuple[set[str] | None, bool]:
    """Extract an explicit capability ceiling from a trusted scope object.

    ``(None, False)`` means no scope was supplied.  ``(set(), True)`` means a
    scope was supplied but did not expose a parseable ceiling and therefore
    must fail closed for capability-bearing decisions.
    """

    if scope is None:
        return None, False
    raw: Any = None
    if isinstance(scope, Mapping):
        raw = scope.get("capabilities", scope.get("capability_ids", scope.get("allowed_capabilities")))
        if raw is None and isinstance(scope.get("scope"), Mapping):
            nested = scope["scope"]
            raw = nested.get("capabilities", nested.get("capability_ids", nested.get("allowed_capabilities")))
    else:
        for name in ("capabilities", "capability_ids", "allowed_capabilities"):
            raw = getattr(scope, name, None)
            if raw is not None:
                break
        if raw is None and callable(getattr(scope, "to_dict", None)):
            try:
                return _scope_capabilities(scope.to_dict())
            except Exception:
                raw = None
    if raw is None:
        return set(), True
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set(), True
    values = {str(item).strip() for item in raw if str(item).strip()}
    if any(item not in AGENT_TEAM_CAPABILITY_CATALOG for item in values):
        return set(), True
    return values, True


class AgentAuthorityResolver:
    """Resolve the current effective Agent capability intersection."""

    def __init__(self, db_manager: Any | None = None, *, config: Any | None = None) -> None:
        self._db_manager = db_manager
        self.config = config

    async def _session(self):
        manager = self._db_manager or get_database_manager()
        value = manager.get_session()
        return await value if inspect.isawaitable(value) else value

    def _teams(self) -> dict[str, dict[str, Any]]:
        result = {str(key): dict(value) for key, value in AGENT_TEAM_DEFAULT_TEAMS.items()}
        try:
            for item in agent_team_v3_teams(self.config):
                if isinstance(item, Mapping) and item.get("team_id"):
                    result[str(item["team_id"])] = dict(item)
        except Exception:
            pass
        return result

    def _team_capabilities(self, revision: AgentRevision) -> set[str]:
        teams = self._teams()
        team = teams.get(str(revision.agent_team_id or ""))
        if not team or not team.get("enabled", True):
            return set()
        subagents = {str(item.get("subagent_id")): item for item in agent_team_v3_subagents(self.config) if isinstance(item, Mapping)}
        if not subagents:
            subagents = dict(AGENT_TEAM_SUBAGENT_CATALOG)
        # The employee Team is a code-owned opt-in definition available to
        # identity revisions even on installations with older saved topology.
        # An explicit configured override (including disabled) still wins.
        subagents.setdefault("employee_operator", AGENT_TEAM_SUBAGENT_CATALOG["employee_operator"])
        result: set[str] = set()
        allowed_subagents = {
            str(item).strip()
            for item in (revision.allowed_subagent_ids or [])
            if str(item).strip()
        }
        for subagent_id in team.get("subagent_ids") or []:
            if str(subagent_id) not in allowed_subagents:
                continue
            item = subagents.get(str(subagent_id)) or {}
            if item.get("enabled") is False:
                continue
            result.update(
                str(capability)
                for capability in item.get("capability_ids") or []
                if str(capability) in AGENT_TEAM_CAPABILITY_CATALOG
            )
        return result

    @staticmethod
    def _policy_capabilities(policy: Any, key: str) -> set[str] | None:
        if not isinstance(policy, Mapping) or key not in policy:
            return None
        value = policy.get(key)
        if not isinstance(value, (list, tuple, set)):
            return set()
        if any(str(item).strip() not in AGENT_TEAM_CAPABILITY_CATALOG for item in value):
            return set()
        return {
            str(item).strip()
            for item in value
            if str(item).strip() in AGENT_TEAM_CAPABILITY_CATALOG
        }

    @staticmethod
    def _policy_scope_allows(policy: Any, key: str, value: UUID | None) -> bool | None:
        """Evaluate optional bounded project/space/persona scope lists.

        ``None`` means the policy did not declare a scope.  A malformed or
        empty declared list is a deny, never an implicit unrestricted grant.
        """

        if not isinstance(policy, Mapping) or key not in policy:
            return None
        raw = policy.get(key)
        if value is None:
            return None
        if not isinstance(raw, (list, tuple, set, frozenset)):
            return False
        allowed = {str(item).strip() for item in raw if str(item).strip()}
        return str(value) in allowed

    @staticmethod
    def _policy_permission_allows(policy: Any, capability: str | None, *, scope: str | None = None) -> bool | None:
        if not isinstance(policy, Mapping) or not capability:
            return None
        key = "permissions"
        if scope and f"{scope}_permissions" in policy:
            key = f"{scope}_permissions"
        if key not in policy:
            return None
        raw = policy.get(key)
        required = _required_project_permission(capability)
        if isinstance(raw, Mapping):
            return raw.get(required) is True
        if isinstance(raw, (list, tuple, set, frozenset)):
            # A list is a compact ceiling (e.g. ["read"]); write requires an
            # explicit write token and is not implied by read.
            return required in {str(item).strip() for item in raw}
        return False

    async def resolve(
        self,
        *,
        agent_id: Any,
        revision_id: Any | None = None,
        project_id: Any | None = None,
        space_id: Any | None = None,
        persona_id: Any | None = None,
        required_capability: str | None = None,
        tool_capabilities: Iterable[str] | None = None,
        harness_capabilities: Iterable[str] | None = None,
        tool_policy: Any | None = None,
        harness_execution_scope: Any | None = None,
        external_action_approved: bool | None = None,
        external_action_policy: Any | None = None,
    ) -> AuthorityDecision:
        aid = _parse_uuid(agent_id)
        rid = _parse_uuid(revision_id)
        pid = _parse_uuid(project_id)
        sid = _parse_uuid(space_id)
        personaid = _parse_uuid(persona_id)
        requested = str(required_capability or "").strip() or None
        requested_catalog_capability = _AUTHORITY_CAPABILITY_ALIASES.get(requested, requested)
        reasons: list[str] = []
        if aid is None:
            return AuthorityDecision(False, requested_capability=requested, deny_reasons=("invalid_agent",))
        for raw, parsed, reason in (
            (revision_id, rid, "invalid_revision"),
            (project_id, pid, "invalid_project"),
            (space_id, sid, "invalid_space"),
            (persona_id, personaid, "invalid_persona"),
        ):
            if raw not in (None, "") and parsed is None:
                return AuthorityDecision(False, agent_id=str(aid), requested_capability=requested, deny_reasons=(reason,))
        if revision_id not in (None, "") and rid is None:
            return AuthorityDecision(False, agent_id=str(aid), requested_capability=requested, deny_reasons=("invalid_revision",))
        if requested and requested_catalog_capability not in AGENT_TEAM_CAPABILITY_CATALOG:
            return AuthorityDecision(False, agent_id=str(aid), agent_revision_id=str(rid) if rid else None, requested_capability=requested, deny_reasons=("unknown_capability",))
        session = await self._session()
        try:
            agent = await session.get(Agent, aid)
            if agent is None:
                reasons.append("agent_not_found")
                return AuthorityDecision(False, agent_id=str(aid), requested_capability=requested, deny_reasons=tuple(reasons))
            if str(agent.state or "") != "active":
                reasons.append("agent_inactive")
            revision: AgentRevision | None
            if rid is not None:
                revision = await session.get(AgentRevision, rid)
                if revision is None or revision.agent_id != aid:
                    reasons.append("revision_not_bound")
                    revision = None
            else:
                revision = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == aid).order_by(AgentRevision.version.desc()))).scalars().first()
                if revision is None:
                    reasons.append("revision_missing")
            organization = (await session.execute(select(Organization).where(Organization.singleton_key == "installation"))).scalar_one_or_none()
            if organization is None:
                reasons.append("organization_missing")
            elif str(organization.autonomy_level or "disabled") == "disabled":
                reasons.append("organization_runtime_disabled")
            if organization is not None and isinstance(getattr(organization, "policy_json", None), Mapping):
                if requested and organization.policy_json.get("allow_agent_runtime") is False:
                    reasons.append("organization_policy_denied")
                if requested in {"external_action", "external_publish", "media_publish"} and organization.policy_json.get("allow_external_actions") is False:
                    reasons.append("organization_external_actions_denied")
            if requested in {"external_action_propose", "telephony_control"}:
                if not (Features.virtual_company() and Features.autonomous_agent_runtime()):
                    reasons.append("employee_runtime_disabled")
                employee_policy = getattr(organization, "policy_json", None)
                if not isinstance(employee_policy, Mapping) or employee_policy.get("allow_agent_runtime") is not True:
                    reasons.append("organization_policy_denied")
                if not isinstance(employee_policy, Mapping) or employee_policy.get("allow_external_actions") is not True:
                    reasons.append("organization_external_actions_denied")
                if requested == "telephony_control" and not (Features.voice_input() and Features.tts_output()):
                    reasons.append("telephony_disabled")
            if revision is not None:
                revision_caps = {
                    str(item).strip()
                    for item in (revision.capability_ceiling_json or [])
                    if str(item).strip() in AGENT_TEAM_CAPABILITY_CATALOG
                }
                team_caps = self._team_capabilities(revision)
                effective = revision_caps & team_caps if revision_caps else set()
                org_policy = getattr(organization, "policy_json", None) if organization else None
                org_caps = self._policy_capabilities(org_policy, "allowed_capabilities")
                if isinstance(org_policy, Mapping) and "allowed_capabilities" in org_policy:
                    raw_org_caps = org_policy.get("allowed_capabilities")
                    if not isinstance(raw_org_caps, (list, tuple, set)) or any(
                        str(item).strip() not in AGENT_TEAM_CAPABILITY_CATALOG
                        for item in raw_org_caps
                    ):
                        reasons.append("organization_policy_invalid")
                if org_caps is not None:
                    effective &= org_caps
                for scope_key, selected in (("spaces", sid), ("projects", pid), ("personas", personaid)):
                    allowed_scope = self._policy_scope_allows(org_policy, scope_key, selected)
                    if allowed_scope is False:
                        reasons.append(f"organization_{scope_key}_denied")
                policy_permission = self._policy_permission_allows(org_policy, requested)
                if policy_permission is False:
                    reasons.append("organization_permission_denied")
                for permission_scope in ("project", "space", "persona"):
                    scoped_permission = self._policy_permission_allows(
                        org_policy,
                        requested,
                        scope=permission_scope,
                    )
                    if scoped_permission is False:
                        reasons.append(f"organization_{permission_scope}_permission_denied")
            else:
                effective = set()

            if pid is not None:
                project = await session.get(Project, pid)
                if project is None or getattr(project, "deleted_at", None) is not None:
                    reasons.append("project_not_found")
                if sid is None and project is not None:
                    sid = _parse_uuid(getattr(project, "space_id", None))
                elif project is not None and sid != _parse_uuid(getattr(project, "space_id", None)):
                    reasons.append("space_project_mismatch")
                grant = (await session.execute(select(AgentProjectGrant).where(AgentProjectGrant.agent_id == aid, AgentProjectGrant.project_id == pid, AgentProjectGrant.state == "active"))).scalars().first()
                if grant is None or not _active_window(grant, now=datetime.utcnow()):
                    reasons.append("project_grant_missing")
                else:
                    permission = _required_project_permission(requested)
                    permissions = normalize_project_member_permissions(grant.permissions)
                    if permissions.get(permission) is not True:
                        reasons.append("project_permission_denied")
                    if requested_catalog_capability and requested_catalog_capability not in effective:
                        reasons.append("capability_ceiling_denied")
            if sid is not None:
                assignments = (
                    await session.execute(
                        select(AgentSpaceAssignment).where(
                            AgentSpaceAssignment.agent_id == aid,
                            AgentSpaceAssignment.space_id == sid,
                            AgentSpaceAssignment.state == "active",
                        )
                    )
                ).scalars().all()
                active_assignments = [
                    assignment
                    for assignment in assignments
                    if _active_window(assignment, now=datetime.utcnow())
                ]
                if not active_assignments:
                    reasons.append("space_assignment_missing")
                else:
                    for assignment in active_assignments:
                        ceiling = self._policy_capabilities(
                            getattr(assignment, "policy_ceiling_json", None),
                            "allowed_capabilities",
                        )
                        if ceiling is not None:
                            effective &= ceiling
                        space_policy = getattr(
                            assignment, "policy_ceiling_json", None
                        )
                        space_permission = self._policy_permission_allows(
                            space_policy,
                            requested,
                            scope="space",
                        )
                        if space_permission is False:
                            reasons.append("space_permission_denied")
            try:
                profile = await session.get(AgentOrganizationProfile, aid)
            except Exception as exc:
                # A missing table during a rolling pre-WS01 deployment is the
                # only compatibility exception.  Any other lookup failure is
                # an unavailable authority dependency and therefore denies
                # rather than silently dropping the profile ceiling.
                detail = str(exc).casefold()
                if "no such table" in detail and "agent_organization_profile" in detail:
                    profile = None
                elif "undefinedtable" in detail and "agent_organization_profile" in detail:
                    profile = None
                else:
                    profile = None
                    reasons.append("organization_profile_dependency_unavailable")
            if profile is not None:
                if str(profile.employment_state or "") not in {"active", "contractor"}:
                    reasons.append("employment_inactive")
                if str(profile.autonomy_level or "disabled") == "disabled":
                    reasons.append("organization_profile_runtime_disabled")
                profile_caps = self._policy_capabilities(getattr(profile, "company_permission_ceiling_json", None), "capabilities")
                if profile_caps is not None:
                    effective &= profile_caps
                profile_policy = getattr(profile, "company_permission_ceiling_json", None)
                if isinstance(profile_policy, Mapping) and isinstance(profile_policy.get("permissions"), Mapping):
                    permission_map = profile_policy["permissions"]
                    required_permission = _required_project_permission(requested)
                    if requested and required_permission in permission_map and permission_map.get(required_permission) is not True:
                        reasons.append("company_permission_ceiling_denied")
                if isinstance(profile_policy, Mapping):
                    for scope_key, selected in (
                        ("spaces", sid),
                        ("projects", pid),
                        ("personas", personaid),
                    ):
                        if scope_key not in profile_policy:
                            continue
                        raw_scope = profile_policy.get(scope_key)
                        allowed_scope = {
                            str(item).strip()
                            for item in raw_scope
                            if str(item).strip()
                        } if isinstance(raw_scope, (list, tuple, set, frozenset)) else set()
                        if selected is None or str(selected) not in allowed_scope:
                            reasons.append(f"company_{scope_key}_ceiling_denied")
                profile_permission = self._policy_permission_allows(profile_policy, requested)
                if profile_permission is False:
                    reasons.append("company_permission_ceiling_denied")
            if personaid is not None:
                persona = await session.get(Persona, personaid)
                if persona is None:
                    reasons.append("persona_not_found")
                elif str(getattr(persona, "state", "")) in {"archived", "retired"}:
                    reasons.append("persona_inactive")
                elif getattr(persona, "project_id", None) is not None:
                    # A PersonaOperatorAssignment is not a project ACL.  If
                    # the Persona is project-bound, resolve that canonical
                    # project and require a separate AgentProjectGrant even
                    # when the caller omitted project_id from the query.
                    persona_project_id = _parse_uuid(persona.project_id)
                    if pid is not None and persona_project_id != pid:
                        reasons.append("persona_project_mismatch")
                    elif pid is None:
                        pid = persona_project_id
                        project = await session.get(Project, persona_project_id)
                        if sid is not None and project is not None and sid != _parse_uuid(getattr(project, "space_id", None)):
                            reasons.append("space_project_mismatch")
                        grant = (
                            (await session.execute(
                                select(AgentProjectGrant).where(
                                    AgentProjectGrant.agent_id == aid,
                                    AgentProjectGrant.project_id == persona_project_id,
                                    AgentProjectGrant.state == "active",
                                )
                            )).scalars().first()
                            if project is not None and persona_project_id is not None
                            else None
                        )
                        if grant is None or not _active_window(grant, now=datetime.utcnow()):
                            reasons.append("project_grant_missing")
                        else:
                            permission = _required_project_permission(requested)
                            if normalize_project_member_permissions(grant.permissions).get(permission) is not True:
                                reasons.append("project_permission_denied")
                            if requested_catalog_capability and requested_catalog_capability not in effective:
                                reasons.append("capability_ceiling_denied")
                        if project is not None and sid is None:
                            sid = _parse_uuid(getattr(project, "space_id", None))
                            if sid is not None:
                                inferred_space_assignments = (
                                    await session.execute(
                                        select(AgentSpaceAssignment).where(
                                            AgentSpaceAssignment.agent_id == aid,
                                            AgentSpaceAssignment.space_id == sid,
                                            AgentSpaceAssignment.state == "active",
                                        )
                                    )
                                ).scalars().all()
                                inferred_space_assignments = [
                                    item
                                    for item in inferred_space_assignments
                                    if _active_window(item, now=datetime.utcnow())
                                ]
                                if not inferred_space_assignments:
                                    reasons.append("space_assignment_missing")
                                else:
                                    for inferred_space_assignment in inferred_space_assignments:
                                        inferred_ceiling = self._policy_capabilities(
                                            getattr(
                                                inferred_space_assignment,
                                                "policy_ceiling_json",
                                                None,
                                            ),
                                            "allowed_capabilities",
                                        )
                                        if inferred_ceiling is not None:
                                            effective &= inferred_ceiling
                                        inferred_permission = self._policy_permission_allows(
                                            getattr(
                                                inferred_space_assignment,
                                                "policy_ceiling_json",
                                                None,
                                            ),
                                            requested,
                                            scope="space",
                                        )
                                        if inferred_permission is False:
                                            reasons.append("space_permission_denied")
                assignments = (
                    await session.execute(
                        select(PersonaOperatorAssignment).where(
                            PersonaOperatorAssignment.agent_id == aid,
                            PersonaOperatorAssignment.persona_id == personaid,
                            PersonaOperatorAssignment.state == "active",
                        )
                    )
                ).scalars().all()
                active_assignments = [
                    item
                    for item in assignments
                    if _active_window(item, now=datetime.utcnow())
                ]
                persona_roles = org_policy.get("persona_roles") if isinstance(org_policy, Mapping) else None
                if persona_roles is not None:
                    allowed_roles = {
                        str(item).strip().lower()
                        for item in persona_roles
                        if str(item).strip()
                    } if isinstance(persona_roles, (list, tuple, set, frozenset)) else set()
                    active_assignments = [
                        item
                        for item in active_assignments
                        if str(getattr(item, "role", "") or "").strip().lower()
                        in allowed_roles
                    ]
                if not active_assignments:
                    reasons.append("persona_assignment_missing")
                else:
                    # A personal Persona has no project grant to carry owner
                    # authorization.  Require the explicit operator assignment
                    # to have been issued by that Persona's owner; a global
                    # admin may manage the row, but cannot silently turn that
                    # management action into an Agent operating grant.
                    authorized_assignments = active_assignments
                    if (
                        getattr(persona, "project_id", None) is None
                        and getattr(persona, "owner_user_id", None) is not None
                    ):
                        authorized_assignments = [
                            item
                            for item in active_assignments
                            if str(getattr(item, "assigned_by", ""))
                            == str(persona.owner_user_id)
                        ]
                        if not authorized_assignments:
                            reasons.append("persona_owner_authorization_missing")
                    for assignment in authorized_assignments:
                        persona_caps = {
                            str(item).strip()
                            for item in (assignment.capability_ceiling_json or [])
                            if str(item).strip()
                            in AGENT_TEAM_CAPABILITY_CATALOG
                        }
                        if persona_caps:
                            effective &= persona_caps
            if tool_capabilities is None and tool_policy is not None:
                if isinstance(tool_policy, Mapping):
                    tool_capabilities = tool_policy.get(
                        "capabilities",
                        tool_policy.get(
                            "capability_ids", tool_policy.get("allowed_capabilities")
                        ),
                    )
                else:
                    tool_capabilities = getattr(
                        tool_policy,
                        "capabilities",
                        getattr(tool_policy, "allowed_capabilities", None),
                    )
            if harness_capabilities is None and harness_execution_scope is not None:
                harness_capabilities, scope_present = _scope_capabilities(
                    harness_execution_scope
                )
                if scope_present and harness_capabilities == set():
                    reasons.append("invalid_harness_scope")
            if external_action_approved is None and external_action_policy is not None:
                if isinstance(external_action_policy, Mapping):
                    external_action_approved = external_action_policy.get("approved")
                else:
                    external_action_approved = getattr(
                        external_action_policy, "approved", None
                    )
            if tool_capabilities is not None:
                raw_tool = {str(item).strip() for item in tool_capabilities if str(item).strip()}
                if any(item not in AGENT_TEAM_CAPABILITY_CATALOG for item in raw_tool):
                    reasons.append("invalid_tool_capabilities")
                effective &= raw_tool & set(AGENT_TEAM_CAPABILITY_CATALOG)
            if harness_capabilities is not None:
                raw_harness = {str(item).strip() for item in harness_capabilities if str(item).strip()}
                if any(item not in AGENT_TEAM_CAPABILITY_CATALOG for item in raw_harness):
                    reasons.append("invalid_harness_capabilities")
                effective &= raw_harness & set(AGENT_TEAM_CAPABILITY_CATALOG)
            if external_action_approved is False or (
                requested in {"external_action", "external_publish", "media_publish"}
                and external_action_approved is not True
            ):
                reasons.append("external_action_approval_required")
            if requested_catalog_capability in AGENT_TEAM_CAPABILITY_CATALOG and AGENT_TEAM_CAPABILITY_CATALOG[requested_catalog_capability].get("access") == "write" and not Features.autonomous_agent_runtime():
                reasons.append("autonomous_runtime_disabled")
            # Re-evaluate organization scope lists after Persona inference has
            # filled project/space IDs.  The first pass intentionally skips
            # unresolved optional dimensions rather than denying a valid
            # project-bound Persona call prematurely.
            org_policy = getattr(organization, "policy_json", None) if organization else None
            for scope_key, selected in (("spaces", sid), ("projects", pid), ("personas", personaid)):
                allowed_scope = self._policy_scope_allows(org_policy, scope_key, selected)
                if allowed_scope is False:
                    reasons.append(f"organization_{scope_key}_denied")
            for permission_scope in ("project", "space", "persona"):
                scoped_permission = self._policy_permission_allows(
                    org_policy,
                    requested,
                    scope=permission_scope,
                )
                if scoped_permission is False:
                    reasons.append(f"organization_{permission_scope}_permission_denied")
            if requested_catalog_capability and requested_catalog_capability not in effective:
                reasons.append("capability_denied")
            if requested_catalog_capability in AGENT_TEAM_CAPABILITY_CATALOG:
                catalog_entry = AGENT_TEAM_CAPABILITY_CATALOG[requested_catalog_capability]
                if catalog_entry.get("access") == "write" and not Features.autonomous_agent_runtime():
                    reasons.append("autonomous_runtime_disabled")
                family = str(catalog_entry.get("family") or "")
                if family in {"project", "docs", "story"} and pid is None:
                    reasons.append("project_scope_required")
                if family in {"media", "spotify"} and personaid is None:
                    reasons.append("persona_scope_required")
            if requested in {"external_action", "external_publish", "media_publish"} and pid is None and personaid is None:
                reasons.append("external_scope_required")
            return AuthorityDecision(
                allowed=not reasons,
                agent_id=str(aid),
                agent_revision_id=str(revision.id) if revision is not None else None,
                organization_id=str(organization.id) if organization is not None else None,
                project_id=str(pid) if pid else None,
                space_id=str(sid) if sid else None,
                persona_id=str(personaid) if personaid else None,
                requested_capability=requested,
                capabilities=frozenset(effective),
                deny_reasons=tuple(dict.fromkeys(reasons)),
            )
        finally:
            await session.close()


async def resolve_agent_authority(*args: Any, **kwargs: Any) -> AuthorityDecision:
    """Convenience facade for one-shot authority inspection."""

    resolver = kwargs.pop("resolver", None)
    return await (resolver or AgentAuthorityResolver()).resolve(*args, **kwargs)


resolve_effective_agent_authority = resolve_agent_authority
resolve_effective_authority = resolve_agent_authority
AuthorityResult = AuthorityDecision
EffectiveAuthority = AuthorityDecision
AgentAuthority = AgentAuthorityResolver
AgentAuthorityResolver.resolve_effective = AgentAuthorityResolver.resolve  # type: ignore[attr-defined]


__all__ = [
    "AuthorityDecision",
    "AgentAuthorityResolver",
    "resolve_agent_authority",
    "resolve_effective_agent_authority",
    "resolve_effective_authority",
    "AuthorityResult",
    "EffectiveAuthority",
    "AgentAuthority",
]
