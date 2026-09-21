"""Durable trigger discovery and tool-free evaluation on AgentWorkCoordinator."""

from __future__ import annotations

import unicodedata
import math
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    Agent,
    AgentRevision,
    AgentRun,
    AgentWorkItem,
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
    Project,
    User,
)
from ..memory.models.agent_automation import (
    AgentAutomationDiscoveryCursor,
    AgentAutomationEvent,
    AgentAutomationRule,
    AgentAutomationRuleDiscoveryCursor,
    AgentAutomationRuleRevision,
    AgentActionPolicy,
    AgentActionPolicyRevision,
)
from .agent_automation_service import (
    AgentAutomationService,
    AutomationError,
    assert_rule_revision_integrity,
    automation_uuid,
    normalize_condition,
)
from .agent_authority import AgentAuthorityResolver
from .agent_automation_events import chat_message_event_values, is_automation_generated
from .agent_work_runtime import ExecutionOutcome, WorkCandidate
from .integration_action_registry import ActionPolicyError


_ACTION_POLICY_BLOCK_CODES = frozenset(
    {
        "action_authority_denied",
        "action_dedupe_integrity_failed",
        "action_dedupe_suppressed",
        "action_event_origin_invalid",
        "action_origin_invalid",
        "action_origin_not_running",
        "action_origin_not_valid",
        "action_origin_work_invalid",
        "action_payload_integrity_failed",
        "action_policy_constraint_failed",
        "action_policy_inactive",
        "action_policy_integrity_failed",
        "action_policy_not_found",
        "action_policy_outside_window",
        "action_policy_retired",
        "action_policy_revision_missing",
        "action_policy_stale",
        "action_rate_limited",
        "action_registry_changed",
        "action_rule_origin_required",
        "action_source_unavailable",
        "action_type_unregistered",
        "automation_rule_inactive",
        "automation_rule_stale",
        "connection_binding_changed",
        "connection_scope_mismatch",
        "employee_features_disabled",
        "human_account_owner_required",
        "human_approval_quote_required",
        "human_approval_required",
        "idempotency_conflict",
        "integration_credential_changed",
        "integration_credential_unverified",
        "integration_provider_mismatch",
        "organization_external_actions_denied",
        "organization_runtime_disabled",
        "provider_unavailable",
        "specialized_action_required",
    }
)


def _id(value):
    return str(value) if value is not None else None


def _caused(metadata):
    return is_automation_generated(metadata)


def _normalized_text(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _accounted_usage(value):
    if not isinstance(value, dict):
        return {}
    result = {
        key: item
        for key, item in value.items()
        if key
        in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cached_tokens",
        )
        and type(item) is int
        and 0 <= item <= 2**53 - 1
    }
    if result:
        # Coordinator deliberately redacts token-shaped keys from metadata;
        # its canonical numeric budget currency is `units`.
        result["units"] = result.get("input_tokens", 0) + result.get("output_tokens", 0)
    cost = value.get("provider_reported_cost")
    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
        result["provider_reported_cost"] = cost
    return result


async def _cursor(session, model, identity, **kwargs):
    row = await session.get(model, identity, with_for_update=True)
    if row is not None:
        return row
    try:
        async with session.begin_nested():
            row = model(**kwargs)
            session.add(row)
            await session.flush()
        return row
    except IntegrityError:
        return await session.get(model, identity, with_for_update=True)


class AutomationRuleWorkSource:
    source_type = "automation_event"

    def __init__(
        self,
        db_manager=None,
        *,
        config=None,
        rule_limit=32,
        event_limit=128,
        authority_resolver=None,
    ):
        self.service = AgentAutomationService(db_manager, config=config)
        self.authority = authority_resolver or AgentAuthorityResolver(
            db_manager, config=config
        )
        self.rule_limit = max(1, min(int(rule_limit), 64))
        self.event_limit = max(1, min(int(event_limit), 256))

    async def _authority(self, rule, revision, project_id, space_id):
        decision = await self.authority.resolve(
            agent_id=rule.agent_id,
            revision_id=revision.agent_revision_id,
            project_id=project_id,
            space_id=space_id,
            required_capability="external_action_propose",
        )
        if not decision.allowed:
            raise AutomationError("automation_source_authority_denied", 403)

    async def load_source(
        self, session, rule, revision, *, event=None, message_id=None, now=None
    ):
        """Re-read canonical ACL and identity before ever decrypting content."""
        self.service.require_features()
        now = now or datetime.utcnow()
        if (
            revision.active_from
            and revision.active_from > now
            or revision.active_until
            and revision.active_until < now
        ):
            raise AutomationError("automation_rule_outside_window", 409)
        agent_revision = await session.get(AgentRevision, revision.agent_revision_id)
        agent = await session.get(Agent, rule.agent_id)
        if (
            not agent
            or agent.state != "active"
            or not agent_revision
            or agent_revision.agent_id != rule.agent_id
        ):
            raise AutomationError("automation_agent_unavailable", 409)
        source_id = message_id if message_id is not None else event.source_id
        message = await session.get(ConversationMessage, automation_uuid(source_id))
        conversation = (
            await session.get(ConversationSession, message.session_id)
            if message
            else None
        )
        if (
            not message
            or message.deleted_at
            or not message.is_active_branch
            or not conversation
            or conversation.deleted_at
            or conversation.app_id
        ):
            raise AutomationError("trigger_source_unavailable", 404)
        if (
            message.role != "user"
            or message.sender_type not in ("user", "human")
            or _caused(message.message_metadata)
        ):
            raise AutomationError("automation_human_source_required", 403)
        actor_id = automation_uuid(message.sender_id)
        human = await session.get(User, actor_id)
        if (
            not human
            or not human.is_active
            or await session.get(Agent, actor_id) is not None
        ):
            raise AutomationError("automation_human_source_required", 403)
        from ..memory.conversation_repository import ConversationRepository

        repository = ConversationRepository(session)
        if not await repository.user_has_session_access(
            str(conversation.id), str(actor_id)
        ):
            raise AutomationError("trigger_source_unavailable", 403)
        project = (
            await session.get(Project, conversation.project_id)
            if conversation.project_id
            else None
        )
        if conversation.project_id and (not project or project.deleted_at):
            raise AutomationError("trigger_source_unavailable", 404)
        project_id, space_id = (
            conversation.project_id,
            project.space_id if project else None,
        )
        trigger = revision.trigger_config_json
        if not isinstance(trigger, dict) or trigger.get("human_only") is not True:
            raise AutomationError("automation_trigger_invalid")
        for field, actual in (
            ("project_id", project_id),
            ("space_id", space_id),
            ("conversation_session_id", conversation.id),
        ):
            if trigger.get(field) is not None and str(trigger[field]) != _id(actual):
                raise AutomationError("automation_scope_mismatch", 403)
        if project_id is None:
            participant = await session.scalar(
                select(ConversationParticipant.id).where(
                    ConversationParticipant.session_id == conversation.id,
                    ConversationParticipant.participant_type == "agent",
                    ConversationParticipant.participant_id == str(rule.agent_id),
                    ConversationParticipant.status == "joined",
                )
            )
            if participant is None:
                raise AutomationError("trigger_source_unavailable", 403)
        if event is not None:
            if (
                event.event_type != "chat.message.created"
                or event.source_type != "conversation_message"
                or event.actor_kind != "human"
                or event.actor_id != actor_id
                or event.conversation_session_id != conversation.id
                or event.project_id != project_id
                or event.space_id != space_id
                or _caused(event.safe_metadata_json or {})
            ):
                raise AutomationError("automation_event_source_mismatch", 409)
            expected = chat_message_event_values(
                message, conversation, space_id=space_id
            )
            if (
                expected["event_hash"] != event.event_hash
                or expected["source_revision"] != event.source_revision
                or expected["id"] != event.id
            ):
                raise AutomationError("automation_event_source_mismatch", 409)
            if event.occurred_at < revision.created_at or (
                revision.active_from and event.occurred_at < revision.active_from
            ):
                raise AutomationError("automation_event_before_revision", 409)
        await self._authority(rule, revision, project_id, space_id)
        return message, conversation, agent_revision, project_id, space_id

    async def discover(self, session, *, now=None):
        """Cyclic keysets + durable anti-join; cursors commit with materialization.

        No moving time cutoff can lose a backlog. Both pagination dimensions
        wrap, so late transactions and previously denied sources are revisited.
        Existing WorkItems are excluded at SQL level, including terminal ones.
        A source never claims, settles, or owns execution state.
        """
        try:
            self.service.require_features()
        except AutomationError:
            return []
        now = now or datetime.utcnow()
        cursor = await _cursor(
            session, AgentAutomationDiscoveryCursor, "rules", key="rules"
        )
        if cursor.through_rule_id is None:
            cursor.after_rule_id = None
            cursor.through_rule_id = await session.scalar(
                select(AgentAutomationRule.id)
                .where(AgentAutomationRule.state == "active")
                .order_by(AgentAutomationRule.id.desc())
                .limit(1)
            )
        if cursor.through_rule_id is None:
            return []
        query = (
            select(AgentAutomationRule)
            .where(
                AgentAutomationRule.state == "active",
                AgentAutomationRule.id <= cursor.through_rule_id,
            )
            .order_by(AgentAutomationRule.id)
            .limit(self.rule_limit)
        )
        if cursor.after_rule_id:
            query = query.where(AgentAutomationRule.id > cursor.after_rule_id)
        rules = list((await session.scalars(query)).all())
        if not rules and cursor.after_rule_id:
            cursor.after_rule_id = None
            cursor.through_rule_id = None
            await session.flush()
            return []
        candidates = []
        for rule in rules:
            cursor.after_rule_id = rule.id
            revision = await session.scalar(
                select(AgentAutomationRuleRevision)
                .where(AgentAutomationRuleRevision.rule_id == rule.id)
                .order_by(AgentAutomationRuleRevision.version.desc())
                .limit(1)
            )
            if not revision:
                continue
            try:
                await assert_rule_revision_integrity(session, revision)
            except AutomationError:
                continue
            event_cursor = await _cursor(
                session,
                AgentAutomationRuleDiscoveryCursor,
                revision.id,
                rule_revision_id=revision.id,
            )
            if event_cursor.sweep_until is None:
                event_cursor.sweep_until = now
                event_cursor.after_occurred_at = event_cursor.after_event_id = None
            intent = f"automation-rule:{revision.id}"
            # SQLAlchemy UUID renders SQLite UUID as hex, while source_id is
            # the portable hyphenated UUID. Use the event's deterministic UUID
            # text normalization in the anti-join on both database dialects.
            from sqlalchemy import func

            query = select(AgentAutomationEvent).where(
                AgentAutomationEvent.event_type == revision.event_type,
                AgentAutomationEvent.actor_kind == "human",
                AgentAutomationEvent.occurred_at <= min(now, event_cursor.sweep_until),
                AgentAutomationEvent.occurred_at
                >= max(
                    revision.created_at, revision.active_from or revision.created_at
                ),
                ~exists(
                    select(AgentWorkItem.id).where(
                        AgentWorkItem.source_type == self.source_type,
                        func.replace(AgentWorkItem.source_id, "-", "")
                        == func.replace(
                            AgentAutomationEvent.id.cast(AgentWorkItem.source_id.type),
                            "-",
                            "",
                        ),
                        AgentWorkItem.source_revision
                        == AgentAutomationEvent.event_hash,
                        AgentWorkItem.intent_key == intent,
                    )
                ),
            )
            if revision.active_until:
                query = query.where(
                    AgentAutomationEvent.occurred_at <= revision.active_until
                )
            for name in ("project_id", "space_id", "conversation_session_id"):
                if revision.trigger_config_json.get(name):
                    query = query.where(
                        getattr(AgentAutomationEvent, name)
                        == automation_uuid(revision.trigger_config_json[name])
                    )
            if event_cursor.after_occurred_at:
                query = query.where(
                    or_(
                        AgentAutomationEvent.occurred_at
                        > event_cursor.after_occurred_at,
                        and_(
                            AgentAutomationEvent.occurred_at
                            == event_cursor.after_occurred_at,
                            AgentAutomationEvent.id > event_cursor.after_event_id,
                        ),
                    )
                )
            events = list(
                (
                    await session.scalars(
                        query.order_by(
                            AgentAutomationEvent.occurred_at, AgentAutomationEvent.id
                        ).limit(self.event_limit)
                    )
                ).all()
            )
            for event in events:
                event_cursor.after_occurred_at, event_cursor.after_event_id = (
                    event.occurred_at,
                    event.id,
                )
                try:
                    _, conversation, _, project_id, space_id = await self.load_source(
                        session, rule, revision, event=event, now=now
                    )
                except AutomationError:
                    continue
                candidates.append(
                    WorkCandidate(
                        source_type=self.source_type,
                        source_id=str(event.id),
                        source_revision=event.event_hash,
                        intent_key=intent,
                        domain="internal",
                        project_id=_id(project_id),
                        space_id=_id(space_id),
                        assigned_agent_id=str(rule.agent_id),
                        agent_revision_id=str(revision.agent_revision_id),
                        required_capabilities=("external_action_propose",),
                        execution_adapter="agent_automation",
                        priority=revision.priority,
                        budget_reservation={"used": 0},
                        max_attempts=revision.max_attempts,
                        concurrency_key=revision.concurrency_key
                        or f"agent-automation:{rule.id}",
                        metadata={
                            "rule_id": str(rule.id),
                            "rule_revision_id": str(revision.id),
                            "event_type": event.event_type,
                            "conversation_session_id": str(conversation.id),
                        },
                    )
                )
            if len(events) < self.event_limit:
                event_cursor.after_occurred_at = event_cursor.after_event_id = None
                event_cursor.sweep_until = None
        if len(rules) < self.rule_limit or (
            rules and rules[-1].id >= cursor.through_rule_id
        ):
            cursor.after_rule_id = None
            cursor.through_rule_id = None
        await session.flush()
        return candidates

    async def load_claim(self, session, claim):
        self.service.require_features()
        if claim.source_type != self.source_type or not claim.intent_key.startswith(
            "automation-rule:"
        ):
            raise AutomationError("automation_work_identity_invalid", 409)
        revision = await session.get(
            AgentAutomationRuleRevision,
            automation_uuid(claim.intent_key.split(":", 1)[1]),
            populate_existing=True,
        )
        rule = (
            await session.get(
                AgentAutomationRule, revision.rule_id, populate_existing=True
            )
            if revision
            else None
        )
        event = await session.get(
            AgentAutomationEvent,
            automation_uuid(claim.source_id),
            populate_existing=True,
        )
        if (
            not rule
            or rule.state != "active"
            or not event
            or event.event_hash != claim.source_revision
        ):
            raise AutomationError("automation_rule_inactive", 409)
        latest = await session.scalar(
            select(AgentAutomationRuleRevision.id)
            .where(AgentAutomationRuleRevision.rule_id == rule.id)
            .order_by(AgentAutomationRuleRevision.version.desc())
            .limit(1)
        )
        if (
            latest != revision.id
            or str(rule.agent_id) != claim.assigned_agent_id
            or str(revision.agent_revision_id) != claim.agent_revision_id
        ):
            raise AutomationError("automation_rule_stale", 409)
        actions = await assert_rule_revision_integrity(session, revision)
        source = await self.load_source(session, rule, revision, event=event)
        if _id(source[3]) != claim.project_id or _id(source[4]) != claim.space_id:
            raise AutomationError("automation_scope_mismatch", 403)
        return rule, revision, event, actions, source

    async def refresh(self, session, claim):
        try:
            await self.load_claim(session, claim)
            return True
        except AutomationError:
            return False


class AutomationRuleExecutionAdapter:
    adapter_key = "agent_automation"

    def __init__(
        self,
        db_manager=None,
        *,
        config=None,
        action_policy_service=None,
        invoker=None,
        registry=None,
    ):
        self.service = AgentAutomationService(
            db_manager, config=config, registry=registry
        )
        self.source = AutomationRuleWorkSource(db_manager, config=config)
        self._actions = action_policy_service
        self._invoker = invoker or self.service.invoker

    def required_capabilities(self, claim):
        return ("external_action_propose",)

    async def _evaluate_in_scope(
        self,
        session,
        rule,
        revision,
        agent_revision,
        text,
        *,
        conversation=None,
        project_id=None,
        run_id=None,
    ):
        from .outbound_privacy_service import (
            get_privacy_policy_context,
            reset_privacy_policy_context,
            set_privacy_policy_context,
        )

        project = (
            await session.get(
                Project, automation_uuid(project_id), populate_existing=True
            )
            if project_id
            else None
        )
        if project_id and (project is None or project.deleted_at):
            raise AutomationError("trigger_source_unavailable", 403)
        context = dict(conversation.context or {}) if conversation else {}
        metadata = dict(project.project_metadata or {}) if project else {}
        # Preserve a stricter inherited policy as well as the freshly loaded
        # canonical source policy. None of these dictionaries enter the prompt.
        inherited = get_privacy_policy_context()
        ranks = {"direct": 0, "protected": 1, "local_only": 2}
        modes = [
            str(data.get("privacy_mode") or "").strip().lower()
            for data in (
                context,
                metadata,
                inherited.session_context or {},
                inherited.project_metadata or {},
            )
        ]
        modes = [mode for mode in modes if mode in ranks]
        if modes:
            context["privacy_mode"] = max(modes, key=ranks.__getitem__)
        token = set_privacy_policy_context(
            session_context=context, project_metadata=metadata
        )
        try:
            return await self._evaluate(
                rule, revision, agent_revision, text, run_id=run_id
            )
        finally:
            reset_privacy_policy_context(token)

    async def _evaluate(self, rule, revision, agent_revision, text, *, run_id=None):
        config = normalize_condition(
            revision.condition_mode, revision.condition_config_json
        )
        if not isinstance(text, str) or len(text) > 16000:
            raise AutomationError("automation_source_too_large")
        if revision.condition_mode == "always":
            return {"matched": True, "reason_code": "matched", "extracted": {}}, {}
        if revision.condition_mode == "keywords":
            haystack = _normalized_text(text)
            matches = [
                _normalized_text(phrase) in haystack for phrase in config["phrases"]
            ]
            matched = any(matches) if config["operator"] == "any" else all(matches)
            return {
                "matched": matched,
                "reason_code": "matched" if matched else "not_matched",
                "extracted": {},
            }, {}
        if self._invoker is None:
            from .agent_automation_invoker import AgentAutomationInvoker

            self._invoker = AgentAutomationInvoker(config=self.service.config)
        result = await self._invoker.evaluate(
            run_id=run_id,
            agent_id=str(rule.agent_id),
            agent_revision_id=str(agent_revision.id),
            rule_revision_id=str(revision.id),
            agent_team_id=agent_revision.agent_team_id,
            execution_profile_id=agent_revision.execution_profile_id,
            situation_description=config["situation_description"],
            positive_examples=config["positive_examples"],
            negative_examples=config["negative_examples"],
            source_text=text,
            bounded_context={},
            extraction_schema=config["extraction_schema"],
        )
        raw = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        usage = _accounted_usage(raw.pop("usage", {}))
        # Even injected implementations must pass the same output contract.
        if (
            set(raw) != {"matched", "reason_code", "extracted"}
            or type(raw["matched"]) is not bool
            or raw["reason_code"] != ("matched" if raw["matched"] else "not_matched")
            or not isinstance(raw["extracted"], dict)
        ):
            raise AutomationError("semantic_condition_invalid")
        slots = config["extraction_schema"]["properties"]
        if set(raw["extracted"]) - set(slots) or set(
            config["extraction_schema"]["required"]
        ) - set(raw["extracted"]):
            raise AutomationError("semantic_condition_invalid")
        for name, value in raw["extracted"].items():
            spec = slots[name]
            expected = {"string": str, "integer": int, "boolean": bool}[spec["type"]]
            if (
                type(value) is not expected
                or (isinstance(value, str) and len(value) > spec.get("maxLength", 512))
                or (
                    type(value) is int
                    and not spec.get("minimum", -(10**18))
                    <= value
                    <= spec.get("maximum", 10**18)
                )
                or ("enum" in spec and value not in spec["enum"])
            ):
                raise AutomationError("semantic_condition_invalid")
        return raw, usage

    async def _mapped_actions(
        self, session, actions, decision, *, event=None, message=None
    ):
        result = []
        for binding in actions:
            policy_revision = await session.get(
                AgentActionPolicyRevision, binding.action_policy_revision_id
            )
            policy = (
                await session.get(AgentActionPolicy, policy_revision.policy_id)
                if policy_revision
                else None
            )
            if not policy or policy.state != "active":
                raise AutomationError("action_policy_inactive", 409)
            latest = await session.scalar(
                select(AgentActionPolicyRevision.id)
                .where(AgentActionPolicyRevision.policy_id == policy.id)
                .order_by(AgentActionPolicyRevision.version.desc())
                .limit(1)
            )
            if latest != policy_revision.id:
                raise AutomationError("action_policy_stale", 409)
            definition = self.service.registry.require(policy_revision.action_type)
            payload = {}
            for key, expression in binding.input_mapping_json.items():
                if (
                    key not in definition.payload_fields
                    or not isinstance(expression, dict)
                    or len(expression) != 1
                ):
                    raise AutomationError("automation_mapping_invalid")
                kind, value = next(iter(expression.items()))
                if kind == "constant":
                    payload[key] = value
                elif kind == "extracted" and value in decision["extracted"]:
                    payload[key] = decision["extracted"][value]
                elif (
                    kind == "event"
                    and event
                    and value
                    in (
                        "source_id",
                        "project_id",
                        "space_id",
                        "conversation_session_id",
                    )
                ):
                    payload[key] = _id(getattr(event, value))
                elif (
                    kind == "source"
                    and message
                    and value in ("message_id", "session_id")
                ):
                    payload[key] = str(
                        message.id if value == "message_id" else message.session_id
                    )
                else:
                    raise AutomationError("automation_mapping_value_unavailable")
            try:
                payload = definition.normalize_payload(
                    payload,
                    definition.normalize_policy(policy_revision.constraints_json),
                )
            except (ValueError, TypeError) as exc:
                raise AutomationError("action_policy_constraint_failed", 422) from exc
            result.append(
                {
                    "position": binding.position,
                    "action_policy_revision_id": str(policy_revision.id),
                    "payload": payload,
                }
            )
        return result

    async def evaluate_only(
        self, session, rule, revision, *, message_id=None, text=None
    ):
        actions = await assert_rule_revision_integrity(session, revision)
        event = None
        if message_id:
            (
                message,
                conversation,
                agent_revision,
                _,
                space_id,
            ) = await self.source.load_source(
                session, rule, revision, message_id=message_id
            )
            event = SimpleNamespace(
                **chat_message_event_values(message, conversation, space_id=space_id)
            )
            text = message.content
        else:
            message = None
            await self.service.validate_scope(session, revision.trigger_config_json)
            conversation_id = revision.trigger_config_json.get(
                "conversation_session_id"
            )
            conversation = (
                await session.get(ConversationSession, automation_uuid(conversation_id))
                if conversation_id
                else None
            )
            agent_revision = await session.get(
                AgentRevision, revision.agent_revision_id
            )
            if not agent_revision or agent_revision.agent_id != rule.agent_id:
                raise AutomationError("automation_agent_revision_mismatch")
        project_id = (
            conversation.project_id
            if conversation
            else revision.trigger_config_json.get("project_id")
        )
        decision, _ = await self._evaluate_in_scope(
            session,
            rule,
            revision,
            agent_revision,
            text,
            conversation=conversation,
            project_id=project_id,
        )
        mapped = (
            await self._mapped_actions(
                session, actions, decision, event=event, message=message
            )
            if decision["matched"]
            else []
        )
        return {**decision, "actions": mapped, "dry_run": True}

    async def execute(self, claim, *, coordinator, session=None, run_id=None, run=None):
        if session is None:
            async with self.service.session() as owned:
                return await self.execute(
                    claim,
                    coordinator=coordinator,
                    session=owned,
                    run_id=run_id,
                    run=run,
                )
        usage = {}
        try:
            rule, revision, event, bindings, source = await self.source.load_claim(
                session, claim
            )
            message, conversation, agent_revision, project_id, _ = source
            work = await session.get(AgentWorkItem, automation_uuid(claim.work_item_id))
            run_row = await session.get(AgentRun, automation_uuid(run_id))
            if (
                not work
                or not run_row
                or work.agent_run_id != run_row.id
                or run_row.work_item_id != work.id
                or run_row.agent_id != rule.agent_id
                or run_row.agent_revision_id != revision.agent_revision_id
                or work.state != "running"
                or work.lease_token != claim.lease_token
            ):
                raise AutomationError("automation_run_provenance_invalid", 409)
            decision, usage = await self._evaluate_in_scope(
                session,
                rule,
                revision,
                agent_revision,
                message.content,
                conversation=conversation,
                project_id=project_id,
                run_id=str(run_row.id),
            )
            # End the evaluation read transaction; fresh reads below
            # must see revocations made during evaluation, even on PostgreSQL.
            await session.rollback()
            rule, revision, event, bindings, source = await self.source.load_claim(
                session, claim
            )
            work = await session.get(
                AgentWorkItem,
                automation_uuid(claim.work_item_id),
                populate_existing=True,
            )
            if (
                work.state != "running"
                or work.lease_token != claim.lease_token
                or (
                    work.lease_expires_at and work.lease_expires_at <= datetime.utcnow()
                )
            ):
                raise AutomationError("automation_lease_lost", 409)
            if not decision["matched"]:
                return ExecutionOutcome(
                    classification="succeeded",
                    result_summary="Automation condition did not match",
                    usage=usage,
                    result={"reason_code": "not_matched", "matched": False},
                )
            if self._actions is None:
                from .agent_action_policy_service import AgentActionPolicyService

                self._actions = AgentActionPolicyService(
                    self.service._db_manager,
                    config=self.service.config,
                    registry=self.service.registry,
                )
            action_ids = []
            suppressed = []
            for binding in bindings:
                action = (
                    await self._mapped_actions(
                        session, [binding], decision, event=event, message=source[0]
                    )
                )[0]
                row = await self._actions.propose_action(
                    session,
                    policy_revision_id=binding.action_policy_revision_id,
                    automation_rule_revision_id=revision.id,
                    origin_agent_id=rule.agent_id,
                    origin_agent_run_id=automation_uuid(run_id),
                    origin_work_item_id=work.id,
                    source_event_id=event.id,
                    action_position=binding.position,
                    payload=action["payload"],
                )
                action_ids.append(str(row.id))
                is_suppressed = any(
                    (
                        _id(row.origin_work_item_id) != str(work.id),
                        _id(row.source_event_id) != str(event.id),
                        _id(row.automation_rule_revision_id) != str(revision.id),
                        row.action_position != binding.position,
                    )
                )
                if is_suppressed:
                    suppressed.append(str(row.id))
                if binding.on_noop == "stop" and is_suppressed:
                    break
            await session.commit()
            summary = (
                "Equivalent action already exists"
                if suppressed
                else "Automation action proposals recorded"
                if action_ids
                else "Automation condition matched"
            )
            return ExecutionOutcome(
                classification="succeeded",
                result_summary=summary,
                domain_ref=action_ids[0] if action_ids else None,
                domain_status="suppressed"
                if suppressed
                else "proposed"
                if action_ids
                else "evaluated",
                usage=usage,
                result={
                    "action_ids": action_ids,
                    "suppressed": bool(suppressed),
                    "suppressed_action_ids": suppressed,
                    "reason_code": "action_dedupe_suppressed"
                    if suppressed
                    else "matched",
                },
            )
        except ActionPolicyError as exc:
            await session.rollback()
            # Only code-owned policy errors have this terminal configuration
            # meaning. Never echo unknown codes or exception/provider text.
            code = (
                exc.code
                if isinstance(exc.code, str) and exc.code in _ACTION_POLICY_BLOCK_CODES
                else "action_policy_blocked"
            )
            return ExecutionOutcome(
                classification="blocked", error_code=code, usage=usage
            )
        except AutomationError as exc:
            await session.rollback()
            return ExecutionOutcome(
                classification="permanent"
                if exc.code == "semantic_condition_invalid"
                else "blocked",
                error_code=exc.code,
            )
        except Exception as exc:
            await session.rollback()
            # Never expose model/provider exception messages to the work ledger.
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code.startswith("semantic_"):
                return ExecutionOutcome(
                    classification="transient"
                    if getattr(exc, "retryable", False)
                    else "permanent",
                    error_code=code,
                    usage=_accounted_usage(getattr(exc, "usage", {})),
                )
            if (
                isinstance(code, str)
                and code.startswith(("action_", "automation_", "semantic_"))
                and len(code) < 100
            ):
                return ExecutionOutcome(classification="blocked", error_code=code)
            return ExecutionOutcome(
                classification="transient", error_code="automation_evaluation_failed"
            )
