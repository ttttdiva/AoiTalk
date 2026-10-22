"""Idempotent one-shot setup for local CrowdWorks marketplace automation."""

from __future__ import annotations

import asyncio
from typing import Any
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from ...features import Features
from ...memory.models import Agent, AgentOrganizationProfile, AgentRevision
from ...memory.models.agent_automation import AgentActionPolicy, AgentActionPolicyRevision
from ...memory.models.operations import ExternalConnection
from ..agent_action_policy_service import ActionPolicyError, AgentActionPolicyService
from ..agent_identity_service import AgentIdentityService, OrganizationService, sha256_json
from ..agent_authority import AgentAuthorityResolver
from ..agent_model_routing import AgentModelRouteError, AgentRoutingPins, resolve_agent_role_route
from ..operations_service import OperationsService
from .crowdworks_action import CrowdWorksPolicy
from .crowdworks_specialists import (
    DEVELOPMENT_CAPABILITIES,
    SPECIALISTS,
    SPECIALIST_CAPABILITY_CEILING,
    SPECIALIST_SUBAGENTS,
    SPECIALIST_TEAM_ID,
    SpecialistSpec,
    development_harness_settings,
)


BOOTSTRAP_KEY = "crowdworks-marketplace-v1"
AGENT_SLUG = "crowdworks-marketplace"
DEFAULT_CONSTRAINTS = CrowdWorksPolicy(
    search_urls=["https://crowdworks.jp/public/jobs/"],
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
        # (Agent Team + Execution Profile) is the canonical model source; the
        # deprecated ``llm_*`` fields stay empty for new setups.
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
                "agent_team_id": "employee",
                "execution_profile_id": "manual",
                "allowed_subagent_ids": ["employee_operator"],
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
        """Specialists use the existing App Development Team and its profiles.

        The model/runner is chosen by the revision's Execution Profile, so a
        human can change it from the Agent screen by creating a revision.
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
                "agent_team_id": SPECIALIST_TEAM_ID,
                "execution_profile_id": "manual",
                "allowed_subagent_ids": list(SPECIALIST_SUBAGENTS),
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
        pins = AgentRoutingPins.from_revision(revision)
        roles = ("planner", "code_worker", "reviewer") if specialist else ("planner", "writer", "reviewer")
        try:
            result["model_routes"] = {role: resolve_agent_role_route(self.config, pins, role).provenance()
                                      for role in roles}
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
