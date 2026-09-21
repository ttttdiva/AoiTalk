"""Actual PostgreSQL races using canonical messages, claims, runs and policies.

Only a deterministic semantic provider rendezvous is injected. Two independent
coordinators execute the actual policy code in independent transactions; no
authorization, hashes, leases, locks or business outcomes are patched.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from scripts.verification.ai_employee_qa import (
    private_state, restrict_backend_network, source_provenance, validate_runtime_db, write_json,
)
from scripts.verification.ai_employee_qa_support import fixture_dependencies


async def run_case(manager, config, manifest, *, rate_edge):
    from sqlalchemy import select
    from src.memory.models import AgentWorkItem, ExternalAction, ExternalActionAttempt, ExternalActionReceipt, ExternalActionApproval, Project, ProjectMember, Space
    from src.memory.conversation_repository import ConversationRepository
    from src.services.agent_identity_service import AgentIdentityService
    from src.services.agent_automation_invoker import AutomationDecision
    from src.services.agent_work_runtime import AgentWorkCoordinator
    from src.services.actor_principal import ActorPrincipal
    from src.services.ai_employee_platform import build_ai_employee_services, register_ai_employee_work

    root = Path(manifest["output_dir"])
    case_id = uuid4().hex
    case_name = "rate-edge" if rate_edge else "dedupe"
    user_id = UUID(manifest["fixtures"]["user_id"])
    actor = {"id": str(user_id), "actor_type": "human", "role": "admin"}
    space_id, project_id, connection_id = uuid4(), uuid4(), uuid4()
    evidence_name = f"provider-probe-{case_id}.json"
    registry, vault, _ = fixture_dependencies({**manifest, "provider_mode": "succeeded"}, evidence_name=evidence_name)

    class RendezvousProvider:
        def __init__(self):
            self.barrier = asyncio.Barrier(2)
            self.run_ids = []

        async def evaluate(self, *, source_text, situation_description, run_id, **kwargs):
            matched = source_text == situation_description
            if matched:
                self.run_ids.append(str(UUID(run_id)))
                await asyncio.wait_for(self.barrier.wait(), timeout=20)
            return AutomationDecision(matched=matched, reason_code="matched" if matched else "not_matched", extracted={}, usage={})

    invoker = RendezvousProvider()
    services = build_ai_employee_services(manager, config=config, registry=registry, credential_vault=vault, invoker=invoker)
    identity = AgentIdentityService(manager, config=config)
    from src.memory.models import ExternalConnection
    async with manager.SessionLocal() as session:
        session.add(Space(id=space_id, owner_id=user_id, name=f"QA {case_name} Space", slug="probe-" + case_id))
        await session.flush()
        session.add(Project(id=project_id, owner_id=user_id, space_id=space_id, name=f"QA {case_name} Project", slug="probe-" + case_id))
        await session.flush()
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role="owner"))
        session.add(ExternalConnection(id=connection_id, owner_user_id=user_id, project_id=project_id,
            provider_key="procurement", display_name=f"QA concurrency {case_name} TEST fixture", remote_account_ref="probe-" + case_id))
        await session.flush()
        await vault.upload_credential(session, actor, connection_id, credential_kind="api_key",
            payload={"api_key": private_state(manifest)["fixture_api_key"]}, expected_revision=0)
        await vault.verify_credential(session, actor, connection_id, expected_revision=1)
        await session.commit()
    agent = await identity.create_agent(display_name=f"QA concurrent {case_name}",
        idempotency_key=case_id, actor_user_id=user_id)
    agent_id = agent["id"]
    revision = await identity.create_revision(agent_id=agent_id, display_name=agent["display_name"],
        mission="Deterministic PostgreSQL verification only", agent_team_id="employee", execution_profile_id="manual",
        allowed_subagent_ids=["employee_operator"], capability_ceiling=["external_action_propose"],
        concurrency_policy={"max_parallel_runs": 2, "max_parallel_tasks": 2},
        idempotency_key=case_id, actor_user_id=user_id)
    await identity.upsert_organization_profile(agent_id=agent_id, actor_user_id=user_id,
        payload={"primary_space_id": str(space_id), "autonomy_level": "bounded", "employment_state": "active"})
    await identity.create_space_assignment(agent_id=agent_id, space_id=space_id, assignment_kind="primary", actor_user_id=user_id)
    await identity.create_project_grant(agent_id=agent_id, project_id=project_id, role="member",
        permissions={"read": True, "write": True}, actor_user_id=user_id)
    await identity.transition_agent(agent_id, "active", actor_user_id=user_id, expected_state="draft")
    constraints = {"fixed_ship_to_ref": "test-office", "min_quantity": 1, "max_quantity": 2,
        "default_quantity": 1, "currency": "JPY", "max_order_total_minor": 10000, "require_quote_before_execute": True}
    constraints.update({"allowed_item_refs": ["test-water-a", "test-water-b"]} if rate_edge else {"fixed_item_ref": "test-water"})
    async with manager.SessionLocal() as session:
        policy = await services.action_policy.create_policy(session, actor_user_id=user_id, agent_id=agent_id,
            display_name=f"QA {case_name} policy", idempotency_key=case_id)
        policy = await services.action_policy.create_revision(session, policy["id"], actor_user_id=user_id,
            expected_version=1, idempotency_key=case_id, action_type="procurement.place_order", connection_id=str(connection_id),
            authorization_mode="bounded_auto", constraints=constraints, rate_limit={"window_seconds": 3600, "max_actions": 1 if rate_edge else 4},
            dedupe_window_seconds=86400, fallback_behavior="block")
        policy_revision_id = policy["current_revision"]["id"]
        await services.action_policy.update_policy(session, policy["id"], actor_user_id=user_id, expected_version=2, state="active")
        await session.commit()
    conversations, rules, texts = [], [], []
    repository = ConversationRepository()
    for index in range(2):
        text = f"QA report {case_id} {index}"
        conversation = await repository.create_session(user_id=str(user_id), character_name="project_manager",
            title=f"QA concurrency {case_name} {index}", project_id=str(project_id))
        await repository.ensure_participant(str(conversation.id), "user", str(user_id), role="owner")
        rule = await services.automation.create_rule(actor_user_id=user_id, agent_id=agent_id,
            display_name=f"QA {case_name} independent rule {index}", idempotency_key=case_id + str(index))
        rule = await services.automation.create_revision(rule["id"], actor_user_id=user_id,
            agent_revision_id=revision["id"], expected_version=1, idempotency_key=case_id + str(index),
            trigger_config={"human_only": True, "conversation_session_id": str(conversation.id)},
            condition_mode="semantic", condition_config={"situation_description": text},
            concurrency_key=f"qa-{case_id}-{index}", max_attempts=1,
            actions=[{"action_policy_revision_id": policy_revision_id,
                "input_mapping": {"item_ref": {"constant": "test-water-a" if index == 0 else "test-water-b"}} if rate_edge else {}}])
        await services.automation.transition_state(rule["id"], actor_user_id=user_id, state="active", expected_version=2)
        conversations.append(str(conversation.id))
        rules.append(rule["id"])
        texts.append(text)
    messages = await asyncio.gather(*(repository.add_message(conversation, "user", text,
        sender_type="user", sender_id=str(user_id), actor_user_id=str(user_id), actor_role="admin")
        for conversation, text in zip(conversations, texts)))
    coordinators = [AgentWorkCoordinator(manager, config=config, enabled=True, max_concurrency=2,
        execution_actor=ActorPrincipal.service("aoitalk.system")) for _ in range(2)]
    for coordinator in coordinators:
        register_ai_employee_work(coordinator, services)
    # Cursor sweep may first finish existing rules. Materialization uses its
    # real durable cursor, uniqueness and WorkCandidate hashes throughout.
    for _ in range(20):
        await coordinators[0].discover_and_materialize()
        async with manager.SessionLocal() as session:
            work = (await session.scalars(select(AgentWorkItem).where(AgentWorkItem.assigned_agent_id == UUID(agent_id),
                AgentWorkItem.source_type == "automation_event"))).all()
        if len(work) == 2:
            break
    if len(work) != 2:
        raise RuntimeError("Probe did not discover exactly two independent canonical work items")
    await asyncio.wait_for(asyncio.gather(*(coordinator.execute_once(limit=1) for coordinator in coordinators)), timeout=60)
    if len(set(invoker.run_ids)) != 2:
        raise RuntimeError("Two real claimed AgentRuns did not rendezvous before policy proposal")
    # Execute the single eligible Action through the same canonical coordinator.
    for _ in range(10):
        await coordinators[0].execute_once(limit=1)
        async with manager.SessionLocal() as session:
            actions = (await session.scalars(select(ExternalAction).where(ExternalAction.connection_id == connection_id))).all()
        if actions and actions[0].status == "succeeded":
            break
    if len(actions) != 1 or actions[0].status != "succeeded":
        raise RuntimeError("PostgreSQL concurrency did not produce exactly one successful action")
    action_id = actions[0].id
    async with manager.SessionLocal() as session:
        work = (await session.scalars(select(AgentWorkItem).where(AgentWorkItem.assigned_agent_id == UUID(agent_id),
            AgentWorkItem.source_type == "automation_event"))).all()
        attempts = (await session.scalars(select(ExternalActionAttempt).where(ExternalActionAttempt.action_id == action_id))).all()
        receipts = (await session.scalars(select(ExternalActionReceipt).where(ExternalActionReceipt.action_id == action_id))).all()
        approvals = (await session.scalars(select(ExternalActionApproval).where(ExternalActionApproval.action_id == action_id))).all()
        if (len(attempts) != 1 or len(receipts) != 1 or attempts[0].status != "succeeded"
                or receipts[0].attempt_id != attempts[0].id or approvals):
            raise RuntimeError("Concurrency probe Attempt/Receipt count exceeded one")
        if rate_edge:
            if (sum(item.state == "succeeded" for item in work) != 1
                    or not any(item.state == "blocked" and item.safe_error_code == "action_rate_limited" for item in work)):
                raise RuntimeError("Concurrent distinct targets did not hit the one-action rate ceiling")
        elif (any(item.state != "succeeded" for item in work)
                or not any((item.result_summary_json or {}).get("suppressed") for item in work)):
            raise RuntimeError("Duplicate concurrent proposal did not record suppression")
        work_evidence = [{"id": str(item.id), "state": item.state, "safe_error_code": item.safe_error_code,
            "blocker_code": item.blocker_code, "result": item.result_summary_json} for item in work]
    observed = json.loads((root / evidence_name).read_text(encoding="utf-8"))
    if observed["submission_count"] != 1 or observed.get("duplicate_attempts", 0) != 0:
        raise RuntimeError("Independent provider counter exceeded one")
    return {"case": case_name, "agent_id": agent_id, "connection_id": str(connection_id), "policy_id": policy["id"],
        "rule_ids": rules, "session_ids": conversations, "message_ids": [str(message.id) for message in messages],
        "concurrent_agent_run_ids": invoker.run_ids, "action_id": str(action_id), "provider_submission_count": 1,
        "attempt_count": 1, "receipt_count": 1, "human_approval_count": 0, "work": work_evidence, "provider_evidence": evidence_name}


async def run(manifest):
    source_before = source_provenance()
    restrict_backend_network(manifest)
    manager = validate_runtime_db(manifest)
    if not await manager.initialize(max_retries=1):
        raise RuntimeError("Concurrency probe requires prepared isolated PostgreSQL")
    from src.config import Config
    from src.rag.config import get_rag_config
    config = Config(config_path=str(Path(manifest["output_dir"]) / "absent-seed.yaml"))
    get_rag_config().docs_enabled = False
    cases = []
    for rate in (False, True):
        cases.append(await run_case(manager, config, manifest, rate_edge=rate))
    write_json(Path(manifest["output_dir"]) / "postgres-concurrency-evidence.json", {
        "database": manifest["database"], "cases": cases, "source_before": source_before, "source": source_provenance(),
        "transport": "canonical service/repository fixtures; two real independent coordinators",
        "synchronization": "deterministic semantic provider rendezvous only; policy/locks/authority unchanged"})
    await manager.engine.dispose()
    manager.sync_engine.dispose()
