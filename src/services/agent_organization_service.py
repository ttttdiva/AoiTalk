"""Shared employee reporting graph, independent of each employee's Team."""

from sqlalchemy import select

from ..memory.models import Agent, AgentOrganizationProfile, AgentRevision, Organization
from .agent_identity_service import AgentIdentityService, AgentIdentityValidationError


async def lock_reporting_graph(session):
    # Serialize reporting edits even when the employee has no profile yet.
    await session.execute(select(Organization).where(Organization.singleton_key == "installation").with_for_update())


async def validate_manager_graph(session, agent_id, manager_agent_id):
    cursor = manager_agent_id
    seen = {agent_id}
    for _ in range(128):
        if cursor is None:
            return
        if cursor in seen:
            raise AgentIdentityValidationError("manager hierarchy contains a cycle")
        seen.add(cursor)
        profile = await session.get(AgentOrganizationProfile, cursor, populate_existing=True)
        cursor = profile.manager_agent_id if profile else None
    raise AgentIdentityValidationError("manager hierarchy exceeds its limit")


class AgentOrganizationService:
    def __init__(self, db_manager, *, config=None):
        self.identity = AgentIdentityService(db_manager, config=config)

    async def graph(self, *, actor_user_id):
        session = await self.identity._session()
        try:
            await self.identity._assert_admin(session, actor_user_id)
            agents = (await session.scalars(select(Agent).order_by(Agent.created_at, Agent.id))).all()
            nodes = []
            for agent in agents:
                profile = await session.get(AgentOrganizationProfile, agent.id)
                revision = await session.scalar(select(AgentRevision).where(AgentRevision.agent_id == agent.id).order_by(AgentRevision.version.desc()).limit(1))
                nodes.append({"agent": agent.to_safe_dict(), "organization_profile": profile.to_safe_dict() if profile else None,
                              "revision": self.identity._revision_projection(revision) if revision else None})
            return {"nodes": nodes, "edges": [{"manager_agent_id": str(node["organization_profile"]["manager_agent_id"]), "agent_id": node["agent"]["id"]}
                    for node in nodes if node["organization_profile"] and node["organization_profile"]["manager_agent_id"]]}
        finally:
            await session.close()

    async def setup(self, *, actor_user_id, employees):
        """Rerunnable manifest: reuse explicit IDs/slugs; create drafts only.

        This entry point sets reporting labels and applies only explicitly
        supplied revision/activation commands. It never edits Team settings,
        grants, credentials, or organization policy.
        References are manifest keys, so caller may build CW or brand trees.
        """
        from ..features import Features
        from .agent_identity_service import AgentIdentityFeatureDisabledError
        if not Features.virtual_company():
            raise AgentIdentityFeatureDisabledError("virtual company features are disabled")
        if not employees or len(employees) > 64:
            raise AgentIdentityValidationError("employees must contain 1 to 64 entries")
        keys = [entry["key"] for entry in employees]
        if len(set(keys)) != len(keys):
            raise AgentIdentityValidationError("duplicate employee key")
        entries = {entry["key"]: entry for entry in employees}
        for key in keys:
            seen = {key}
            cursor = entries[key].get("manager_key")
            while cursor:
                if cursor not in entries or cursor in seen:
                    raise AgentIdentityValidationError("invalid manifest reporting graph")
                seen.add(cursor)
                cursor = entries[cursor].get("manager_key")
        session = await self.identity._session()
        try:
            await self.identity._assert_admin(session, actor_user_id)
            resolved = {}
            for entry in employees:
                if entry.get("agent_id"):
                    from uuid import UUID
                    agent = await session.get(Agent, UUID(str(entry["agent_id"])))
                else:
                    agent = await session.scalar(select(Agent).where(Agent.slug == entry["slug"]))
                if agent is not None:
                    resolved[entry["key"]] = agent.to_safe_dict()
        finally:
            await session.close()
        # All explicit references must resolve before creating anything.
        if any(entry.get("agent_id") and entry["key"] not in resolved for entry in employees):
            raise AgentIdentityValidationError("existing employee reference is missing")
        for entry in employees:
            if entry["key"] not in resolved:
                resolved[entry["key"]] = await self.identity.create_agent(
                    display_name=entry["display_name"], slug=entry["slug"],
                    idempotency_key=f"employee-organization:{entry['slug']}", actor_user_id=actor_user_id)
        for entry in employees:
            revision = entry.get("revision")
            if revision:
                from .agent_identity_service import sha256_json
                revision = {key: value for key, value in revision.items() if value is not None}
                revision.setdefault("display_name", entry["display_name"])
                revision.setdefault("idempotency_key", f"employee-organization:{sha256_json(revision)}")
                await self.identity.create_revision(agent_id=resolved[entry["key"]]["id"], actor_user_id=actor_user_id, **revision)
            if entry.get("activate") and resolved[entry["key"]]["state"] == "draft":
                resolved[entry["key"]] = await self.identity.transition_agent(resolved[entry["key"]]["id"], "active", actor_user_id=actor_user_id, expected_state="draft")
        # Existing active supervisors are required by the identity service.
        # A draft supervisor is returned as a concrete blocker for activation.
        blockers = []
        for entry in employees:
            manager = resolved.get(entry.get("manager_key"))
            if manager and manager["state"] != "active":
                blockers.append({"key": entry["key"], "code": "manager_activation_required", "manager_agent_id": manager["id"]})
                continue
            existing_profile = await self.identity.get_organization_profile(resolved[entry["key"]]["id"])
            patch = {}
            for name in ("job_title", "responsibility_summary"):
                if name in entry:
                    patch[name] = entry[name]
            if existing_profile is None:
                patch.setdefault("job_title", entry["display_name"])
            if "manager_key" in entry:
                patch.update(manager_agent_id=manager["id"] if manager else None, manager_user_id=None)
            await self.identity.upsert_organization_profile(
                agent_id=resolved[entry["key"]]["id"], actor_user_id=actor_user_id,
                payload=patch)
        for key, agent in resolved.items():
            revisions = await self.identity.list_revisions(agent["id"], limit=1)
            if agent["state"] != "active" or not revisions:
                blockers.append({"key": key, "code": "employee_revision_or_activation_required", "agent_id": agent["id"]})
        return {"employees": resolved, "blockers": blockers, "ready": not blockers}
