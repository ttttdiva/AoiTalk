"""Human-managed immutable automation rules and closed action bindings."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..features import Features
from ..memory.models import (
    Agent,
    AgentRevision,
    ConversationSession,
    Project,
    Space,
    User,
)
from ..memory.models.agent_automation import (
    AgentActionPolicy,
    AgentActionPolicyRevision,
    AgentAutomationRule,
    AgentAutomationRuleAction,
    AgentAutomationRuleRevision,
)


class AutomationError(Exception):
    def __init__(self, code: str, status_code: int = 422):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class AutomationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RuleCreate(AutomationCommand):
    agent_id: str
    display_name: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=255)


class RulePatch(AutomationCommand):
    display_name: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)


class RuleState(AutomationCommand):
    state: Literal["active", "paused", "retired"]
    expected_version: int = Field(ge=1)


class RuleActionInput(AutomationCommand):
    action_policy_revision_id: str
    input_mapping: dict[str, Any] = Field(default_factory=dict)
    on_noop: Literal["continue", "stop"] = "continue"


class RuleRevisionCreate(AutomationCommand):
    agent_revision_id: str
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=1)
    event_type: Literal["chat.message.created"] = "chat.message.created"
    trigger_config: dict[str, Any]
    condition_mode: Literal["always", "keywords", "semantic"] = "always"
    condition_config: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-10000, le=10000)
    concurrency_key: str | None = Field(default=None, max_length=255)
    max_attempts: int = Field(default=3, ge=1, le=10)
    active_from: str | None = None
    active_until: str | None = None
    actions: list[RuleActionInput] = Field(default_factory=list, max_length=16)


class RuleTest(AutomationCommand):
    rule_revision_id: str | None = None
    source_message_id: str | None = None
    text: str | None = Field(default=None, max_length=16000)


def automation_uuid(value: Any) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AutomationError("invalid_automation_id") from exc


def utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        return (
            parsed.astimezone(timezone.utc).replace(tzinfo=None)
            if parsed.tzinfo
            else parsed
        )
    except (ValueError, TypeError, AttributeError) as exc:
        raise AutomationError("invalid_automation_date") from exc


def reject_unsafe_json(value: Any, depth: int = 0) -> None:
    if depth > 8:
        raise AutomationError("automation_config_too_deep")
    if isinstance(value, dict):
        if len(value) > 64:
            raise AutomationError("automation_config_too_large")
        for key, item in value.items():
            if not isinstance(key, str) or any(
                marker in key.casefold().replace("-", "_")
                for marker in (
                    "secret",
                    "token",
                    "password",
                    "credential",
                    "api_key",
                    "apikey",
                    "cookie",
                    "authorization",
                    "private_key",
                )
            ):
                raise AutomationError("automation_secret_field_forbidden")
            reject_unsafe_json(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 64:
            raise AutomationError("automation_config_too_large")
        for item in value:
            reject_unsafe_json(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 16000 or re.search(
            r"(?i)(bearer\s|(?:api[_-]?key|password|secret|token|authorization)\s*[:=])",
            value,
        ):
            raise AutomationError("automation_unsafe_value")
    elif value is not None and type(value) not in (bool, int):
        raise AutomationError("automation_scalar_required")


def _closed(value: Any, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - keys:
        raise AutomationError("automation_config_field_invalid")
    return value


def _phrases(value: Any, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 32 or (required and not value):
        raise AutomationError("automation_phrases_invalid")
    if any(not isinstance(x, str) or not x.strip() or len(x) > 400 for x in value):
        raise AutomationError("automation_phrases_invalid")
    return [x.strip() for x in value]


def normalize_condition(mode: str, value: dict) -> dict:
    reject_unsafe_json(value)
    if mode == "always":
        return _closed(value, set())
    if mode == "keywords":
        _closed(value, {"operator", "phrases"})
        if value.get("operator") not in {"any", "all"}:
            raise AutomationError("automation_keyword_operator_invalid")
        return {
            "operator": value["operator"],
            "phrases": _phrases(value.get("phrases"), required=True),
        }
    if mode != "semantic":
        raise AutomationError("automation_condition_invalid")
    _closed(
        value,
        {
            "situation_description",
            "positive_examples",
            "negative_examples",
            "extraction_schema",
        },
    )
    description = value.get("situation_description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 4000
    ):
        raise AutomationError("automation_situation_invalid")
    positive = _phrases(value.get("positive_examples", []))
    negative = _phrases(value.get("negative_examples", []))
    schema = value.get(
        "extraction_schema",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    _closed(schema, {"type", "properties", "required", "additionalProperties"})
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        raise AutomationError("automation_extraction_schema_invalid")
    properties = schema.get("properties")
    required = schema.get("required", [])
    if (
        not isinstance(properties, dict)
        or len(properties) > 16
        or not isinstance(required, list)
        or any(not isinstance(k, str) or k not in properties for k in required)
    ):
        raise AutomationError("automation_extraction_schema_invalid")
    for name, spec in properties.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise AutomationError("automation_extraction_schema_invalid")
        _closed(spec, {"type", "enum", "minimum", "maximum", "maxLength"})
        if spec.get("type") not in {"string", "integer", "boolean"}:
            raise AutomationError("automation_extraction_schema_invalid")
        if "enum" in spec and (
            not isinstance(spec["enum"], list)
            or not spec["enum"]
            or len(spec["enum"]) > 32
        ):
            raise AutomationError("automation_extraction_schema_invalid")
        for key in ("minimum", "maximum", "maxLength"):
            if key in spec and type(spec[key]) is not int:
                raise AutomationError("automation_extraction_schema_invalid")
        if spec.get("minimum", -(10**18)) > spec.get("maximum", 10**18) or spec.get(
            "maxLength", 512
        ) not in range(1, 513):
            raise AutomationError("automation_extraction_schema_invalid")
        allowed_bounds = {
            "string": {"maxLength"},
            "integer": {"minimum", "maximum"},
            "boolean": set(),
        }[spec["type"]]
        if set(spec) - ({"type", "enum"} | allowed_bounds):
            raise AutomationError("automation_extraction_schema_invalid")
        scalar_type = {"string": str, "integer": int, "boolean": bool}[spec["type"]]
        if any(type(choice) is not scalar_type for choice in spec.get("enum", [])):
            raise AutomationError("automation_extraction_schema_invalid")
    if len(required) != len(set(required)) or len(positive) > 12 or len(negative) > 12:
        raise AutomationError("automation_extraction_schema_invalid")
    return {
        "situation_description": description.strip(),
        "positive_examples": positive,
        "negative_examples": negative,
        "extraction_schema": {**schema, "required": required},
    }


def _revision_hash_payload(revision: Any, actions: list[Any]) -> dict:
    def field(row, key, fallback=None):
        return (
            row.get(key, fallback)
            if isinstance(row, dict)
            else getattr(row, key, fallback)
        )

    data = {
        key: field(revision, key)
        for key in (
            "event_type",
            "condition_mode",
            "priority",
            "concurrency_key",
            "max_attempts",
        )
    }
    data.update(
        agent_revision_id=str(field(revision, "agent_revision_id")),
        trigger_config=field(
            revision, "trigger_config_json", field(revision, "trigger_config")
        ),
        condition_config=field(
            revision, "condition_config_json", field(revision, "condition_config")
        ),
    )
    for key in ("active_from", "active_until"):
        date = utc_datetime(field(revision, key))
        data[key] = date.isoformat() if date else None
    data["actions"] = [
        {
            "position": field(a, "position"),
            "action_policy_revision_id": str(field(a, "action_policy_revision_id")),
            "input_mapping": field(
                a, "input_mapping_json", field(a, "input_mapping", {})
            ),
            "on_noop": field(a, "on_noop") or "continue",
        }
        for a in sorted(actions, key=lambda a: field(a, "position"))
    ]
    return data


def automation_rule_content_hash(revision: Any, actions: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            _revision_hash_payload(revision, actions),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


async def assert_rule_revision_integrity(
    session, revision
) -> list[AgentAutomationRuleAction]:
    actions = list(
        (
            await session.scalars(
                select(AgentAutomationRuleAction)
                .where(AgentAutomationRuleAction.rule_revision_id == revision.id)
                .order_by(AgentAutomationRuleAction.position)
            )
        ).all()
    )
    if automation_rule_content_hash(revision, actions) != revision.content_hash:
        raise AutomationError("automation_rule_integrity_failed", 409)
    return actions


class AgentAutomationService:
    def __init__(
        self,
        db_manager=None,
        *,
        config=None,
        registry=None,
        invoker=None,
        session_factory=None,
    ):
        self._db_manager = db_manager
        self._session_factory = session_factory
        self.config = config
        self._registry = registry
        if invoker is None:
            from .agent_automation_invoker import AgentAutomationInvoker

            invoker = AgentAutomationInvoker(config=config)
        self.invoker = invoker

    @property
    def registry(self):
        if self._registry is None:
            from .integration_action_registry import IntegrationActionRegistry

            self._registry = IntegrationActionRegistry()
        return self._registry

    @asynccontextmanager
    async def session(self):
        if self._session_factory:
            pending = self._session_factory()
        else:
            from ..memory.database import get_database_manager

            pending = (self._db_manager or get_database_manager()).get_session()
        session = await pending if inspect.isawaitable(pending) else pending
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @staticmethod
    def require_features():
        if not Features.virtual_company() or not Features.autonomous_agent_runtime():
            raise AutomationError("automation_feature_disabled", 404)

    async def assert_admin(self, session, actor_user_id):
        self.require_features()
        actor_id = automation_uuid(actor_user_id)
        if await session.get(Agent, actor_id) is not None:
            raise AutomationError("automation_human_admin_required", 403)
        user = await session.get(User, actor_id)
        if user is None or not user.is_active or user.role != "admin":
            raise AutomationError("automation_human_admin_required", 403)
        return actor_id

    async def _rule(self, session, rule_id):
        rule = await session.get(AgentAutomationRule, automation_uuid(rule_id))
        if rule is None:
            raise AutomationError("automation_rule_not_found", 404)
        return rule

    async def aggregate(self, session, rule, *, history=False):
        query = (
            select(AgentAutomationRuleRevision)
            .where(AgentAutomationRuleRevision.rule_id == rule.id)
            .order_by(AgentAutomationRuleRevision.version.desc())
            .limit(100 if history else 1)
        )
        revisions = list((await session.scalars(query)).all())
        summaries = []
        for revision in revisions if history else revisions[:1]:
            actions = await assert_rule_revision_integrity(session, revision)
            summary = {
                **revision.to_safe_dict(),
                "actions": [a.to_safe_dict() for a in actions],
            }
            if revision.condition_mode == "semantic":
                pinned = await session.get(AgentRevision, revision.agent_revision_id)
                summary["semantic_readiness"] = self.semantic_readiness(pinned)
            summaries.append(summary)
        result = {
            **rule.to_safe_dict(),
            "current_revision": summaries[0] if summaries else None,
        }
        if history:
            result["revisions"] = summaries
        return result

    def semantic_readiness(self, agent_revision):
        from .agent_automation_invoker import AgentAutomationInvoker, readiness

        if agent_revision is None:
            return {"supported": False, "error_code": "semantic_route_invalid"}
        if not isinstance(self.invoker, AgentAutomationInvoker):
            # An explicitly injected implementation is server-owned (QA uses
            # this seam). It never comes from API JSON or persisted rule text.
            return {"supported": True, "error_code": None, "injected": True}
        config = self.config
        if config is None:
            from ..config import Config

            config = Config()
        return readiness(
            config, agent_revision.agent_team_id, agent_revision.execution_profile_id
        )

    def require_semantic_readiness(self, agent_revision):
        state = self.semantic_readiness(agent_revision)
        if state["supported"] is not True:
            raise AutomationError(state["error_code"], 409)

    async def list_rules(self, *, actor_user_id, agent_id=None, limit=100):
        async with self.session() as session:
            await self.assert_admin(session, actor_user_id)
            query = (
                select(AgentAutomationRule)
                .order_by(AgentAutomationRule.created_at.desc(), AgentAutomationRule.id)
                .limit(max(1, min(limit, 500)))
            )
            if agent_id:
                query = query.where(
                    AgentAutomationRule.agent_id == automation_uuid(agent_id)
                )
            return [
                await self.aggregate(session, r)
                for r in (await session.scalars(query)).all()
            ]

    async def get_rule(self, rule_id, *, actor_user_id):
        async with self.session() as session:
            await self.assert_admin(session, actor_user_id)
            return await self.aggregate(
                session, await self._rule(session, rule_id), history=True
            )

    async def create_rule(self, *, actor_user_id, **payload):
        try:
            command = RuleCreate.model_validate(payload)
        except ValidationError as exc:
            raise AutomationError("automation_request_invalid") from exc
        reject_unsafe_json(payload)
        agent_id = automation_uuid(command.agent_id)
        name = command.display_name.strip()
        if not name:
            raise AutomationError("automation_name_required")
        async with self.session() as session:
            actor = await self.assert_admin(session, actor_user_id)
            agent = await session.get(Agent, agent_id)
            if not agent or agent.state == "retired":
                raise AutomationError("automation_agent_unavailable", 409)
            query = select(AgentAutomationRule).where(
                AgentAutomationRule.agent_id == agent_id,
                AgentAutomationRule.idempotency_key == command.idempotency_key,
            )
            existing = await session.scalar(query)
            if existing:
                if existing.display_name != name:
                    raise AutomationError("automation_idempotency_conflict", 409)
                return await self.aggregate(session, existing)
            rule = AgentAutomationRule(
                id=uuid4(),
                agent_id=agent_id,
                display_name=name,
                idempotency_key=command.idempotency_key,
                version=1,
                state="draft",
                created_by=actor,
            )
            session.add(rule)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(query)
                if not existing or existing.display_name != name:
                    raise AutomationError("automation_idempotency_conflict", 409)
                rule = existing
            await session.refresh(rule)
            return await self.aggregate(session, rule)

    async def _cas(self, session, rule, expected_version, **values):
        if rule.state == "retired":
            raise AutomationError("automation_rule_retired", 409)
        result = await session.execute(
            update(AgentAutomationRule)
            .where(
                AgentAutomationRule.id == rule.id,
                AgentAutomationRule.version == expected_version,
                AgentAutomationRule.state != "retired",
            )
            .values(
                version=expected_version + 1, updated_at=datetime.utcnow(), **values
            )
        )
        if result.rowcount != 1:
            raise AutomationError("automation_version_conflict", 409)

    async def update_rule(
        self, rule_id, *, actor_user_id, display_name, expected_version
    ):
        command = RulePatch(
            display_name=display_name, expected_version=expected_version
        )
        reject_unsafe_json({"display_name": display_name})
        if not command.display_name.strip():
            raise AutomationError("automation_name_required")
        async with self.session() as session:
            await self.assert_admin(session, actor_user_id)
            rule = await self._rule(session, rule_id)
            await self._cas(
                session,
                rule,
                expected_version,
                display_name=command.display_name.strip(),
            )
            await session.commit()
            await session.refresh(rule)
            return await self.aggregate(session, rule)

    async def transition_state(
        self, rule_id, *, actor_user_id, state, expected_version
    ):
        RuleState(state=state, expected_version=expected_version)
        async with self.session() as session:
            await self.assert_admin(session, actor_user_id)
            rule = await self._rule(session, rule_id)
            if state == "active":
                revision = await session.scalar(
                    select(AgentAutomationRuleRevision)
                    .where(AgentAutomationRuleRevision.rule_id == rule.id)
                    .order_by(AgentAutomationRuleRevision.version.desc())
                    .limit(1)
                )
                if not revision:
                    raise AutomationError("automation_revision_required", 409)
                await assert_rule_revision_integrity(session, revision)
                await self.validate_scope(session, revision.trigger_config_json)
                if revision.condition_mode == "semantic":
                    self.require_semantic_readiness(
                        await session.get(AgentRevision, revision.agent_revision_id)
                    )
                agent = await session.get(Agent, rule.agent_id)
                if not agent or agent.state != "active":
                    raise AutomationError("automation_agent_unavailable", 409)
            await self._cas(session, rule, expected_version, state=state)
            await session.commit()
            await session.refresh(rule)
            return await self.aggregate(session, rule)

    async def validate_scope(self, session, trigger):
        _closed(
            trigger, {"human_only", "project_id", "space_id", "conversation_session_id"}
        )
        if trigger.get("human_only", True) is not True or not any(
            trigger.get(k)
            for k in ("project_id", "space_id", "conversation_session_id")
        ):
            raise AutomationError("automation_human_scoped_trigger_required")
        normalized = {"human_only": True}
        for key, model in (
            ("project_id", Project),
            ("space_id", Space),
            ("conversation_session_id", ConversationSession),
        ):
            if trigger.get(key) is not None:
                identity = automation_uuid(trigger[key])
                row = await session.get(model, identity)
                if row is None or getattr(row, "deleted_at", None):
                    raise AutomationError("automation_scope_unavailable", 404)
                normalized[key] = str(identity)
        conversation = (
            await session.get(
                ConversationSession,
                automation_uuid(normalized["conversation_session_id"]),
            )
            if "conversation_session_id" in normalized
            else None
        )
        project_id = (
            conversation.project_id if conversation else normalized.get("project_id")
        )
        project = (
            await session.get(Project, automation_uuid(project_id))
            if project_id
            else None
        )
        if (
            conversation
            and "project_id" in normalized
            and str(conversation.project_id) != normalized["project_id"]
        ):
            raise AutomationError("automation_scope_mismatch")
        if (
            "space_id" in normalized
            and (conversation or project)
            and (not project or str(project.space_id) != normalized["space_id"])
        ):
            raise AutomationError("automation_scope_mismatch")
        return normalized

    async def validate_bindings(self, session, agent_id, inputs, condition):
        slots = condition.get("extraction_schema", {}).get("properties", {})
        declared = {}
        result = []
        for position, binding in enumerate(inputs):
            policy_revision = await session.get(
                AgentActionPolicyRevision,
                automation_uuid(binding.action_policy_revision_id),
            )
            policy = (
                await session.get(AgentActionPolicy, policy_revision.policy_id)
                if policy_revision
                else None
            )
            if not policy or policy.agent_id != agent_id or policy.state == "retired":
                raise AutomationError("automation_action_policy_invalid")
            definition = self.registry.require(policy_revision.action_type)
            declared.update(definition.extraction_schema)
            mapping = binding.input_mapping
            reject_unsafe_json(mapping)
            if set(mapping) - set(definition.payload_fields):
                raise AutomationError("automation_mapping_field_invalid")
            constants = {}
            for field, expression in mapping.items():
                if not isinstance(expression, dict) or len(expression) != 1:
                    raise AutomationError("automation_mapping_invalid")
                kind, value = next(iter(expression.items()))
                if kind == "constant":
                    if type(value) not in (str, int, bool):
                        raise AutomationError("automation_mapping_scalar_required")
                    constants[field] = value
                elif kind == "extracted":
                    if (
                        not isinstance(value, str)
                        or value not in slots
                        or value not in definition.extraction_schema
                    ):
                        raise AutomationError("automation_mapping_slot_invalid")
                    if (
                        slots[value]["type"]
                        != definition.extraction_schema[field]["type"]
                    ):
                        raise AutomationError("automation_mapping_slot_invalid")
                elif kind == "event":
                    if value not in (
                        "source_id",
                        "project_id",
                        "space_id",
                        "conversation_session_id",
                    ):
                        raise AutomationError("automation_mapping_source_invalid")
                elif kind == "source":
                    if value not in ("message_id", "session_id"):
                        raise AutomationError("automation_mapping_source_invalid")
                else:
                    raise AutomationError("automation_mapping_invalid")
            try:
                constraints = definition.normalize_policy(
                    policy_revision.constraints_json
                )
            except (ValueError, TypeError) as exc:
                raise AutomationError("action_policy_constraint_failed") from exc
            # All fixed values must pass the actual policy normalizer at save.
            # Dynamic slots use policy defaults here and are revalidated after evaluation.
            preview = dict(constants)
            if definition.action_type == "procurement.place_order":
                # Allow-list policies have no default target. A declared
                # dynamic mapping can be validated using an authorized member;
                # the actual extracted value is still normalized at execution.
                for field, allowed in (
                    ("item_ref", "allowed_item_refs"),
                    ("ship_to_ref", "allowed_ship_to_refs"),
                ):
                    if (
                        field in mapping
                        and field not in preview
                        and constraints.get(allowed)
                    ):
                        preview[field] = constraints[allowed][0]
            try:
                definition.normalize_payload(preview, constraints)
            except (ValueError, TypeError) as exc:
                raise AutomationError("action_policy_constraint_failed") from exc
            result.append(
                {
                    "position": position,
                    "action_policy_revision_id": policy_revision.id,
                    "input_mapping": mapping,
                    "on_noop": binding.on_noop,
                }
            )
        for name, spec in slots.items():
            if name not in declared or spec.get("type") != declared[name].get("type"):
                raise AutomationError("automation_extraction_slot_unregistered")
        return result

    async def create_revision(self, rule_id, *, actor_user_id, **payload):
        try:
            command = RuleRevisionCreate.model_validate(payload)
        except ValidationError as exc:
            raise AutomationError("automation_request_invalid") from exc
        reject_unsafe_json(command.model_dump())
        async with self.session() as session:
            actor = await self.assert_admin(session, actor_user_id)
            rule = await self._rule(session, rule_id)
            if rule.state == "retired":
                raise AutomationError("automation_rule_retired", 409)
            agent_revision = await session.get(
                AgentRevision, automation_uuid(command.agent_revision_id)
            )
            if not agent_revision or agent_revision.agent_id != rule.agent_id:
                raise AutomationError("automation_agent_revision_mismatch")
            trigger = await self.validate_scope(session, command.trigger_config)
            condition = normalize_condition(
                command.condition_mode, command.condition_config
            )
            if rule.state == "active" and command.condition_mode == "semantic":
                self.require_semantic_readiness(agent_revision)
            actions = await self.validate_bindings(
                session, rule.agent_id, command.actions, condition
            )
            values = command.model_dump(
                exclude={
                    "actions",
                    "expected_version",
                    "trigger_config",
                    "condition_config",
                }
            )
            values.update(
                agent_revision_id=agent_revision.id,
                trigger_config_json=trigger,
                condition_config_json=condition,
                active_from=utc_datetime(command.active_from),
                active_until=utc_datetime(command.active_until),
            )
            if (
                values["active_from"]
                and values["active_until"]
                and values["active_from"] > values["active_until"]
            ):
                raise AutomationError("automation_date_window_invalid")
            digest = automation_rule_content_hash(values, actions)
            existing = await session.scalar(
                select(AgentAutomationRuleRevision).where(
                    AgentAutomationRuleRevision.rule_id == rule.id,
                    AgentAutomationRuleRevision.idempotency_key
                    == command.idempotency_key,
                )
            )
            if existing:
                if existing.content_hash != digest:
                    raise AutomationError("automation_idempotency_conflict", 409)
                return await self.aggregate(session, rule)
            await self._cas(session, rule, command.expected_version)
            version = (
                await session.scalar(
                    select(func.max(AgentAutomationRuleRevision.version)).where(
                        AgentAutomationRuleRevision.rule_id == rule.id
                    )
                )
                or 0
            ) + 1
            revision = AgentAutomationRuleRevision(
                id=uuid4(),
                rule_id=rule.id,
                version=version,
                content_hash=digest,
                created_by=actor,
                **values,
            )
            session.add(revision)
            await session.flush()
            for action in actions:
                session.add(
                    AgentAutomationRuleAction(
                        id=uuid4(),
                        rule_revision_id=revision.id,
                        position=action["position"],
                        action_policy_revision_id=action["action_policy_revision_id"],
                        input_mapping_json=action["input_mapping"],
                        on_noop=action["on_noop"],
                    )
                )
            await session.commit()
            await session.refresh(rule)
            return await self.aggregate(session, rule)

    async def test_rule(self, rule_id, *, actor_user_id, **payload):
        command = RuleTest.model_validate(payload)
        if (command.source_message_id is None) == (command.text is None):
            raise AutomationError("automation_test_source_required")
        from .agent_automation_runtime import AutomationRuleExecutionAdapter

        async with self.session() as session:
            await self.assert_admin(session, actor_user_id)
            rule = await self._rule(session, rule_id)
            revision = (
                await session.get(
                    AgentAutomationRuleRevision,
                    automation_uuid(command.rule_revision_id),
                )
                if command.rule_revision_id
                else await session.scalar(
                    select(AgentAutomationRuleRevision)
                    .where(AgentAutomationRuleRevision.rule_id == rule.id)
                    .order_by(AgentAutomationRuleRevision.version.desc())
                    .limit(1)
                )
            )
            if not revision or revision.rule_id != rule.id:
                raise AutomationError("automation_revision_not_found", 404)
            adapter = AutomationRuleExecutionAdapter(
                self._db_manager,
                config=self.config,
                invoker=self.invoker,
                registry=self.registry,
            )
            return await adapter.evaluate_only(
                session,
                rule,
                revision,
                message_id=command.source_message_id,
                text=command.text,
            )
