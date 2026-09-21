"""Signed SIP ingress, durable one-shot call fences and employee routing.

The server directory owns physical destinations. Route JSON only selects keys.
Live PSTN connectivity is not inferred from a locally ready route.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import inspect
import json
import os
import re
from typing import Any, Mapping
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from ..features import Features
from ..memory.models import Agent, AgentRevision, ConversationSession, ExternalConnection, User
from ..memory.models.telephony import TelephonyCall, TelephonyRoute
from ..memory.models.agent_automation import _safe_text
from .agent_authority import AgentAuthorityResolver
from .agent_run_service import AgentRunService
from .telephony_provider import OpenAITelephonyProvider
from .outbound_privacy_service import OutboundPrivacyGateway, set_privacy_policy_context, reset_privacy_policy_context

PROVIDER = "openai_realtime_sip"
KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
# Absolute, not idle, lifetime. Recovery waits another full lifetime before
# classifying rows left by another process; it never sweeps recent calls.
MAX_CALL_LIFETIME_SECONDS = 3600
RECOVERY_MIN_AGE_SECONDS = 7200


class TelephonyError(ValueError):
    def __init__(self, code: str, status_code: int = 422):
        super().__init__(code)
        self.code, self.status_code = code, status_code


class _PinnedCredential:
    __slots__ = ("_handle", "pins")

    def __init__(self, handle, pins):
        self._handle, self.pins = handle, pins

    def reveal(self):
        return self._handle.reveal()

    def __repr__(self):
        return "TelephonyCredential([REDACTED])"

    def __reduce_ex__(self, protocol):
        raise TypeError("telephony_secret_serialization_forbidden")


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise TelephonyError("telephony_invalid_identifier") from None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _mask(value):
    digits = "".join(c for c in str(value) if c.isdigit())
    return "****" + digits[-4:] if digits else "anonymous"


def _identity(value):
    # Header display names and parameters are not routing authority.
    value = str(value or "").strip()
    if "<" in value and ">" in value:
        value = value.split("<", 1)[1].split(">", 1)[0]
    return value.split(";", 1)[0].strip().casefold()


def _features_ready():
    return bool(Features.virtual_company() and Features.autonomous_agent_runtime() and Features.voice_input())


def _hours(config, zone, now):
    if not config:
        return True
    local = now.astimezone(ZoneInfo(zone))
    clock = local.strftime("%H:%M")
    return local.weekday() in config["days"] and config["start"] <= clock < config["end"]


class TelephonyService:
    def __init__(self, db_manager, *, config=None, vault=None, provider=None,
                 directory=None, authority=None, runtime_factory=None,
                 action_policy_service=None, action_execution_service=None):
        self.db = db_manager
        self.config = config
        if vault is None:
            from .integration_credential_vault_service import IntegrationCredentialVaultService
            vault = IntegrationCredentialVaultService()
        self.vault = vault
        self.provider = provider or OpenAITelephonyProvider(config=config)
        self.authority = authority or AgentAuthorityResolver(db_manager, config=config)
        self.agent_runs = AgentRunService(db_manager)
        self.runtime_factory = runtime_factory
        self.action_policy_service = action_policy_service
        self.action_execution_service = action_execution_service
        self._runtimes = {}
        # Only trusted deployment configuration reaches this constructor.
        if directory is None:
            try:
                directory = json.loads(os.getenv("AOITALK_TELEPHONY_DIRECTORY_JSON", "{}"))
            except ValueError:
                directory = {}
        self.directory = self._validate_directory(directory)

    @staticmethod
    def _validate_directory(directory):
        if not isinstance(directory, dict) or len(directory) > 128:
            raise TelephonyError("telephony_directory_invalid")
        output = {}
        identities = set()
        for key, raw in directory.items():
            if not KEY.fullmatch(key) or not isinstance(raw, dict):
                raise TelephonyError("telephony_directory_invalid")
            incoming = _identity(raw.get("incoming_uri"))
            if not re.fullmatch(r"(?:sips?:[^\s<>?]+@[^\s<>?]+|tel:\+[0-9]{5,15})", incoming):
                raise TelephonyError("telephony_directory_invalid")
            binding = (str(_uuid(raw.get("connection_id"))), incoming)
            if binding in identities:
                raise TelephonyError("telephony_directory_ambiguous")
            identities.add(binding)
            destinations = raw.get("destinations", {})
            if not isinstance(destinations, dict) or len(destinations) > 32:
                raise TelephonyError("telephony_directory_invalid")
            for name, destination in destinations.items():
                if not KEY.fullmatch(name) or not isinstance(destination, dict):
                    raise TelephonyError("telephony_directory_invalid")
                uri = destination.get("uri", "")
                if not isinstance(uri, str) or not re.fullmatch(r"(?:sips?:[a-zA-Z0-9+_.-]+@[a-zA-Z0-9.-]+|tel:\+[0-9]{5,15})", uri):
                    raise TelephonyError("telephony_directory_invalid")
            output[key] = {**raw, "incoming_uri": incoming, "connection_id": binding[0], "destinations": destinations}
        return output

    @asynccontextmanager
    async def session(self):
        value = self.db.get_session()
        session = await value if inspect.isawaitable(value) else value
        try:
            yield session
        finally:
            await session.close()

    async def _admin(self, session, actor_user_id):
        if not Features.virtual_company():
            raise TelephonyError("telephony_feature_disabled", 404)
        uid = _uuid(actor_user_id)
        if await session.get(Agent, uid) is not None:
            raise TelephonyError("telephony_admin_required", 403)
        user = await session.get(User, uid)
        if user is None or not user.is_active or user.role != "admin":
            raise TelephonyError("telephony_admin_required", 403)
        return uid

    async def catalog(self, *, actor_user_id):
        async with self.session() as session:
            await self._admin(session, actor_user_id)
        return {"provider": PROVIDER, "provider_status": "unverified", "route_keys": [
            {"key": key, "display_name": _safe_text(item.get("display_name") or key)[:160],
             "called_number_masked": _mask(item["incoming_uri"]), "connection_id": item["connection_id"],
             "destination_keys": [{"key": name, "display_name": _safe_text(dest.get("display_name") or name)[:160]}
                                  for name, dest in item["destinations"].items()]}
            for key, item in self.directory.items()]}

    def _route_values(self, body):
        allowed = {"display_name", "connection_id", "provider_route_ref", "agent_id", "agent_revision_id", "timezone",
                   "business_hours_json", "greeting_override", "transfer_policy_json", "fallback_mode", "fallback_destination_key"}
        if set(body) - allowed:
            raise TelephonyError("telephony_route_fields_invalid")
        values = dict(body)
        for name in ("connection_id", "agent_id", "agent_revision_id"):
            values[name] = _uuid(values.get(name))
        name = str(values.get("display_name") or "").strip()
        if not name or len(name) > 160:
            raise TelephonyError("telephony_route_name_invalid")
        values["display_name"] = name
        directory = self.directory.get(values.get("provider_route_ref"))
        if directory is None or directory["connection_id"] != str(values["connection_id"]):
            raise TelephonyError("telephony_route_key_unavailable")
        zone = values.get("timezone", "Asia/Tokyo")
        try:
            ZoneInfo(zone)
        except (ValueError, KeyError, TypeError):
            raise TelephonyError("telephony_timezone_invalid") from None
        values["timezone"] = zone
        hours = values.get("business_hours_json") or {}
        if hours:
            if set(hours) != {"days", "start", "end"} or not isinstance(hours["days"], list) or not hours["days"]:
                raise TelephonyError("telephony_hours_invalid")
            if any(type(day) is not int or day not in range(7) for day in hours["days"]):
                raise TelephonyError("telephony_hours_invalid")
            if any(not isinstance(hours[key], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", hours[key]) for key in ("start", "end")) or hours["start"] >= hours["end"]:
                raise TelephonyError("telephony_hours_invalid")
        values["business_hours_json"] = hours
        transfer = values.get("transfer_policy_json") or {}
        if transfer:
            if set(transfer) != {"action_policy_revision_id", "destination_keys"}:
                raise TelephonyError("telephony_transfer_policy_invalid")
            transfer = {"action_policy_revision_id": str(_uuid(transfer["action_policy_revision_id"])), "destination_keys": transfer["destination_keys"]}
            keys = transfer["destination_keys"]
            if not isinstance(keys, list) or not keys or len(keys) > 32 or any(not isinstance(key, str) or key not in directory["destinations"] for key in keys):
                raise TelephonyError("telephony_transfer_destination_invalid")
        values["transfer_policy_json"] = transfer
        if values.get("fallback_mode", "reject") != "reject" or values.get("fallback_destination_key"):
            raise TelephonyError("telephony_fallback_unsupported")
        values["fallback_mode"] = "reject"
        values["fallback_destination_key"] = None
        greeting = str(values.get("greeting_override") or "")
        if len(greeting) > 1200 or re.search(r"(?i)(?:secret|password|api_key|bearer |sk-[a-z0-9])", greeting):
            raise TelephonyError("telephony_greeting_invalid")
        values["greeting_override"] = greeting
        values["provider"] = PROVIDER
        values["called_number_hash"] = _hash(directory["incoming_uri"])
        values["called_number_masked"] = _mask(directory["incoming_uri"])
        # Directory changes revoke live calls even when the DB route did not change.
        values["route_hash"] = _hash({**values, "directory_hash": _hash(directory)})
        return values

    async def _validate_binding(self, session, values):
        agent = await session.get(Agent, values["agent_id"])
        revision = await session.get(AgentRevision, values["agent_revision_id"])
        connection = await session.get(ExternalConnection, values["connection_id"])
        if agent is None or revision is None or revision.agent_id != agent.id or agent.state == "retired":
            raise TelephonyError("telephony_agent_revision_invalid")
        if connection is None or connection.provider_key != PROVIDER:
            raise TelephonyError("telephony_connection_invalid")

    @staticmethod
    def _route_dict(route):
        fields = ("id", "display_name", "state", "provider", "connection_id", "provider_route_ref", "called_number_masked",
                  "agent_id", "agent_revision_id", "timezone", "business_hours_json", "greeting_override", "transfer_policy_json",
                  "fallback_mode", "fallback_destination_key", "version", "route_hash", "created_at", "updated_at")
        safe = route.to_safe_dict()
        safe["business_hours_json"] = safe.get("business_hours", {})
        safe["transfer_policy_json"] = safe.get("transfer_policy", {})
        ref = route.provider_route_ref
        safe["provider_route_ref"] = ref if isinstance(ref, str) and KEY.fullmatch(ref) else None
        return {name: safe.get(name) for name in fields}

    @staticmethod
    def _call_dict(call):
        fields = ("id", "route_id", "route_version", "route_hash", "agent_id", "agent_revision_id", "conversation_session_id", "agent_run_id",
                  "state", "caller_masked", "called_masked", "transfer_destination_key", "safe_error_code", "received_at", "accepted_at", "ended_at")
        safe = call.to_safe_dict()
        for name in ("caller_masked", "called_masked"):
            safe[name] = _mask(getattr(call, name)) if getattr(call, name) else None
        return {name: safe.get(name) for name in fields}

    async def create_route(self, body, *, actor_user_id):
        body = dict(body)
        key = body.pop("idempotency_key", "")
        if not isinstance(key, str) or not 1 <= len(key) <= 255:
            raise TelephonyError("telephony_idempotency_required")
        values = self._route_values(body)
        async with self.session() as session:
            uid = await self._admin(session, actor_user_id)
            existing = (await session.execute(select(TelephonyRoute).where(TelephonyRoute.agent_id == values["agent_id"], TelephonyRoute.idempotency_key == key))).scalars().first()
            if existing:
                if existing.route_hash != values["route_hash"]:
                    raise TelephonyError("telephony_idempotency_conflict", 409)
                return self._route_dict(existing)
            await self._validate_binding(session, values)
            route = TelephonyRoute(id=uuid4(), **values, created_by=uid, idempotency_key=key, state="draft", version=1)
            session.add(route)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = (await session.execute(select(TelephonyRoute).where(TelephonyRoute.agent_id == values["agent_id"], TelephonyRoute.idempotency_key == key))).scalars().first()
                if replay and replay.route_hash == values["route_hash"]:
                    return self._route_dict(replay)
                raise TelephonyError("telephony_route_conflict", 409) from None
            await session.refresh(route)
            return self._route_dict(route)

    async def list_routes(self, *, actor_user_id, agent_id=None):
        async with self.session() as session:
            await self._admin(session, actor_user_id)
            query = select(TelephonyRoute).order_by(TelephonyRoute.created_at.desc()).limit(200)
            if agent_id:
                query = query.where(TelephonyRoute.agent_id == _uuid(agent_id))
            return [self._route_dict(row) for row in (await session.execute(query)).scalars()]

    async def get_route(self, route_id, *, actor_user_id):
        async with self.session() as session:
            await self._admin(session, actor_user_id)
            row = await session.get(TelephonyRoute, _uuid(route_id))
            if row is None:
                raise TelephonyError("telephony_route_not_found", 404)
            return self._route_dict(row)

    async def update_route(self, route_id, body, *, actor_user_id):
        body = dict(body)
        expected = body.pop("expected_version", None)
        state = body.pop("state", None)
        async with self.session() as session:
            await self._admin(session, actor_user_id)
            route = await session.get(TelephonyRoute, _uuid(route_id))
            if route is None:
                raise TelephonyError("telephony_route_not_found", 404)
            if type(expected) is not int or expected != route.version or route.state == "retired":
                raise TelephonyError("telephony_route_stale", 409)
            current = {key: getattr(route, key) for key in ("display_name", "connection_id", "provider_route_ref", "agent_id", "agent_revision_id", "timezone", "business_hours_json", "greeting_override", "transfer_policy_json", "fallback_mode", "fallback_destination_key")}
            values = self._route_values({**current, **body})
            await self._validate_binding(session, values)
            if state is not None:
                if state not in {"draft", "active", "paused", "retired"}:
                    raise TelephonyError("telephony_route_state_invalid")
                values["state"] = state
            candidate = TelephonyRoute(id=route.id, **{**values, "state": state or route.state})
            if candidate.state == "active":
                reasons = await self._readiness(session, candidate, check_state=False)
                if reasons:
                    raise TelephonyError(reasons[0], 409)
            changed = await session.execute(update(TelephonyRoute).where(TelephonyRoute.id == route.id, TelephonyRoute.version == expected).values(**values, version=expected + 1, updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise TelephonyError("telephony_route_stale", 409)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise TelephonyError("telephony_route_conflict", 409) from None
            await session.refresh(route)
            return self._route_dict(route)

    async def _connection(self, session, connection_id):
        connection = await session.get(ExternalConnection, _uuid(connection_id))
        if connection is None or connection.provider_key != PROVIDER:
            raise TelephonyError("telephony_connection_unavailable", 503)
        return connection

    async def _credential(self, session, connection, *, pins=None):
        kwargs = {"owner_user_id": connection.owner_user_id, "project_id": connection.project_id,
                  "required_capabilities": ("telephony.control",)}
        ready = await self.vault.readiness_for_connection(session, connection.id, **kwargs)
        if ready.get("ready") is not True:
            raise TelephonyError("telephony_credential_unready", 503)
        current = {"revision": ready["revision"], "state_hash": ready["state_hash"], "connection_version": connection.version}
        if pins is not None and current != pins:
            raise TelephonyError("telephony_credential_stale", 409)
        handle = await self.vault.resolve_for_execution(session, connection.id,
            expected_revision=ready["revision"], expected_state_hash=ready["state_hash"], **kwargs)
        return _PinnedCredential(handle, current)

    async def _readiness(self, session, route, *, check_state=True):
        reasons = []
        if not _features_ready():
            reasons.append("telephony_feature_disabled")
        if check_state and route.state != "active":
            reasons.append("telephony_route_inactive")
        directory = self.directory.get(route.provider_route_ref)
        if directory is None or directory["connection_id"] != str(route.connection_id):
            reasons.append("telephony_route_key_unavailable")
        try:
            revision = await session.get(AgentRevision, route.agent_revision_id)
            if revision is None or revision.agent_id != route.agent_id:
                raise TelephonyError("telephony_agent_revision_invalid")
            self.resolve_realtime_model(revision)
            connection = await self._connection(session, route.connection_id)
            from ..memory.models import Project
            project = await session.get(Project, connection.project_id) if connection.project_id else None
            if connection.project_id and (project is None or project.deleted_at is not None):
                raise TelephonyError("telephony_project_unavailable")
            OutboundPrivacyGateway(self.config, project_metadata=dict(project.project_metadata or {}) if project else {}).ensure_provider_allowed(
                "openai_realtime", base_url="https://api.openai.com")
            secret = await self._credential(session, connection)
            if not secret.reveal().get("webhook_secret"):
                reasons.append("telephony_webhook_unconfigured")
            decision = await self.authority.resolve(agent_id=route.agent_id, revision_id=route.agent_revision_id,
                project_id=connection.project_id, required_capability="telephony_control")
            if decision.allowed is not True:
                reasons.append("telephony_agent_denied")
            transfer = route.transfer_policy_json or {}
            if transfer:
                from ..memory.models.agent_automation import AgentActionPolicy, AgentActionPolicyRevision
                if self.action_policy_service is None or self.action_execution_service is None:
                    raise TelephonyError("telephony_action_kernel_unavailable")
                policy_revision = await session.get(AgentActionPolicyRevision, _uuid(transfer.get("action_policy_revision_id")))
                policy = await session.get(AgentActionPolicy, policy_revision.policy_id) if policy_revision else None
                current = await self.action_policy_service.current(session, policy.id) if policy else None
                if (policy is None or policy.state != "active" or policy.agent_id != route.agent_id or current.id != policy_revision.id
                    or policy_revision.action_type != "telephony.transfer_call" or policy_revision.connection_id != route.connection_id
                    or str(policy_revision.constraints_json.get("route_id")) != str(route.id)
                    or not set(transfer.get("destination_keys", [])) <= set(policy_revision.constraints_json.get("allowed_destination_keys", []))):
                    raise TelephonyError("telephony_transfer_policy_unavailable")
                await self.action_policy_service._revision_integrity(session, policy_revision,
                    bounded=policy_revision.authorization_mode == "bounded_auto")
        except TelephonyError as exc:
            reasons.append(exc.code)
        except Exception:
            reasons.append("telephony_readiness_unavailable")
        return reasons

    def resolve_realtime_model(self, revision):
        from .agent_team_v3 import AGENT_TEAM_DEFAULT_TEAMS, agent_team_v3_teams, _apply_execution_route
        from .execution_profile_service import list_team_execution_profiles, resolve_execution_main_route
        from .session_llm_runtime_context import bind_session_main_route_override, reset_session_main_route_override
        from .live_voice_service import DEFAULT_REALTIME_MODELS
        teams = {key: dict(value) for key, value in AGENT_TEAM_DEFAULT_TEAMS.items()}
        teams.update({item["team_id"]: item for item in agent_team_v3_teams(self.config)})
        team = teams.get(revision.agent_team_id)
        if not team or not team.get("enabled", True) or "employee_operator" not in team.get("subagent_ids", []) or "employee_operator" not in (revision.allowed_subagent_ids or []):
            raise TelephonyError("telephony_team_unsupported")
        profiles = list_team_execution_profiles(self.config, revision.agent_team_id)
        profile = next((item for item in profiles if item["profile_id"] == revision.execution_profile_id), None)
        # manual is the existing system profile: inherit the configured Main
        # route. A custom profile must exist; absence never means manual.
        if profile is None and revision.execution_profile_id != "manual":
            raise TelephonyError("telephony_execution_profile_unavailable")
        if profile and not profile.get("enabled", True):
            raise TelephonyError("telephony_execution_profile_unavailable")
        route_spec = ((profile.get("overrides") or {}).get("employee_operator") or profile.get("default_route")) if profile else None
        token = bind_session_main_route_override(None)
        try:
            selected = _apply_execution_route(route_spec, resolve_execution_main_route(self.config))
        finally:
            reset_session_main_route_override(token)
        if selected.get("provider") != "openai" or selected.get("model") not in DEFAULT_REALTIME_MODELS:
            raise TelephonyError("telephony_realtime_model_unsupported")
        return selected["model"]

    async def readiness(self, route_id, *, actor_user_id):
        async with self.session() as session:
            await self._admin(session, actor_user_id)
            row = await session.get(TelephonyRoute, _uuid(route_id))
            if row is None:
                raise TelephonyError("telephony_route_not_found", 404)
            reasons = await self._readiness(session, row)
        return {"ready": not reasons, "reason_codes": reasons, "provider_status": "unverified", "external_setup": "unverified"}

    async def list_calls(self, *, actor_user_id, route_id=None, call_id=None):
        async with self.session() as session:
            await self._admin(session, actor_user_id)
            query = select(TelephonyCall).order_by(TelephonyCall.received_at.desc()).limit(200)
            if route_id:
                query = query.where(TelephonyCall.route_id == _uuid(route_id))
            if call_id:
                query = query.where(TelephonyCall.id == _uuid(call_id))
            return [self._call_dict(row) for row in (await session.execute(query)).scalars()]

    async def handle_webhook(self, connection_id, raw: bytes, headers):
        if len(raw) > 65536:
            raise TelephonyError("telephony_webhook_too_large", 413)
        async with self.session() as session:
            connection = await self._connection(session, connection_id)
            handle = await self._credential(session, connection)
            secret = handle.reveal()
            webhook_secret = secret.get("webhook_secret")
            if not webhook_secret:
                raise TelephonyError("telephony_webhook_unconfigured", 503)
        try:
            event = self.provider.verify_webhook(raw, headers, webhook_secret=webhook_secret)
        except Exception:
            raise TelephonyError("telephony_webhook_invalid", 401) from None
        if event.get("type") != "realtime.call.incoming":
            raise TelephonyError("telephony_webhook_event_unsupported", 400)
        data = event.get("data")
        if not isinstance(data, dict):
            raise TelephonyError("telephony_webhook_invalid", 400)
        call_id, event_id = data.get("call_id"), event.get("id")
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", v) for v in (call_id, event_id)):
            raise TelephonyError("telephony_webhook_invalid", 400)
        sip = data.get("sip_headers", [])
        if not isinstance(sip, list) or len(sip) > 128:
            raise TelephonyError("telephony_webhook_invalid", 400)
        selected = {}
        for item in sip:
            if not isinstance(item, dict):
                raise TelephonyError("telephony_webhook_invalid", 400)
            name = str(item.get("name", "")).casefold()
            if name in {"to", "from"}:
                if name in selected:
                    raise TelephonyError("telephony_webhook_ambiguous", 400)
                selected[name] = _identity(item.get("value"))
        called, caller = selected.get("to", ""), selected.get("from", "")
        route_key = next((key for key, item in self.directory.items() if item["connection_id"] == str(connection.id) and item["incoming_uri"] == called), None)
        async with self.session() as session:
            existing = (await session.execute(select(TelephonyCall).where(or_(TelephonyCall.provider_call_id == call_id, TelephonyCall.provider_event_id == event_id)))).scalars().first()
            if existing:
                return {"ok": True, "duplicate": True}
            routes = list((await session.execute(select(TelephonyRoute).where(TelephonyRoute.connection_id == connection.id, TelephonyRoute.provider_route_ref == route_key, TelephonyRoute.state != "retired"))).scalars()) if route_key else []
            active_routes = [item for item in routes if item.state == "active"]
            route = active_routes[0] if len(active_routes) == 1 else routes[0] if len(routes) == 1 else None
            call = TelephonyCall(id=uuid4(), provider=PROVIDER, provider_call_id=call_id, provider_event_id=event_id,
                state="incoming", route_id=route.id if route else None, route_version=route.version if route else None,
                route_hash=route.route_hash if route else None, agent_id=route.agent_id if route else None,
                agent_revision_id=route.agent_revision_id if route else None,
                caller_identity_hash=hmac.new(webhook_secret.encode(), caller.encode(), hashlib.sha256).hexdigest(),
                caller_masked=_mask(caller), called_identity_hash=_hash(called), called_masked=_mask(called))
            session.add(call)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return {"ok": True, "duplicate": True}
            await session.refresh(call)
            reasons = ["telephony_route_unavailable"] if route is None else await self._readiness(session, route)
            if route and not _hours(route.business_hours_json, route.timezone, datetime.now(timezone.utc)):
                reasons.append("telephony_outside_hours")
            if reasons:
                await self._change(call.id, "incoming", "uncertain", safe_error_code="reject_pending")
                result = await self._provider_command("reject", connection_id=connection.id, call_id=call_id, credential_pins=handle.pins, api_key=secret["api_key"], status_code=603)
                await self._settle(call.id, "uncertain", result, success="rejected", error=reasons[0])
                return {"ok": True}
        if not await self._change(call.id, "incoming", "accepting", route_fence=call, safe_error_code="accept_pending"):
            return {"ok": True, "duplicate": True}
        runtime = None
        try:
            runtime = await self._prepare_runtime(call.id, secret["api_key"])
            result = await self._provider_command("accept", connection_id=connection.id, call_id=call_id, credential_pins=handle.pins, accept_runtime=runtime, api_key=secret["api_key"], session_config=runtime.session_config, conversation_id=runtime.snapshot["conversation_session_id"])
            if result.status != "succeeded":
                await self._settle(call.id, "accepting", result, success="active")
                await runtime.close_local()
                return {"ok": True}
            # Persist positive accept evidence before attaching the socket.
            # This permits one narrowly scoped safety hangup on attach failure;
            # ambiguous accept results never enter this path.
            await self._change(call.id, "accepting", "accepting", accepted_at=datetime.utcnow(), safe_error_code="accept_confirmed")
            await runtime.attach(call_id)
            async with self.session() as session:
                current = await self._validate_accept_runtime(session, runtime)
            if await self._change(call.id, "accepting", "active", route_fence=current, safe_error_code=None, accepted_at=datetime.utcnow()):
                self._runtimes[str(call.id)] = runtime
            else:
                await self.terminate_runtime(runtime)
                await runtime.close_local()
        except BaseException as exc:
            if runtime:
                try:
                    await asyncio.shield(self.terminate_runtime(runtime))
                except Exception:
                    pass
            await asyncio.shield(self._change(call.id, "accepting", "uncertain", safe_error_code="telephony_accept_uncertain"))
            if runtime:
                await runtime.close_local()
            if isinstance(exc, asyncio.CancelledError):
                raise
        return {"ok": True}

    async def _change(self, call_id, expected, state, *, route_fence=None, transfer_fence=False, **values):
        async with self.session() as session:
            conditions = [TelephonyCall.id == _uuid(call_id), TelephonyCall.state == expected]
            if transfer_fence:
                conditions.append(TelephonyCall.transfer_destination_key.is_(None))
            if route_fence is not None:
                conditions.extend([
                    select(TelephonyRoute.id).where(TelephonyRoute.id == route_fence.route_id,
                        TelephonyRoute.version == route_fence.route_version, TelephonyRoute.route_hash == route_fence.route_hash,
                        TelephonyRoute.state == "active").exists(),
                    select(Agent.id).where(Agent.id == route_fence.agent_id, Agent.state == "active").exists(),
                ])
            changed = await session.execute(update(TelephonyCall).where(*conditions).values(state=state, version=TelephonyCall.version + 1, updated_at=datetime.utcnow(), **values))
            await session.commit()
            return changed.rowcount == 1

    async def _settle(self, call_id, expected, result, *, success, error=None):
        state = success if result.status == "succeeded" else "failed" if result.status == "failed" else "uncertain"
        if success == "transferred" and result.status == "failed":
            # A rejected REFER did not terminate the accepted SIP call. Keep
            # its termination control; transfer_destination_key remains the
            # durable no-repeat fence independently of the live call state.
            await self._change(call_id, expected, "active", safe_error_code="telephony_transfer_failed")
            return
        values = {"safe_error_code": error if result.status == "succeeded" else "telephony_provider_" + state}
        if state in {"rejected", "completed", "failed", "transferred"}:
            values["ended_at"] = datetime.utcnow()
        await self._change(call_id, expected, state, **values)

    async def _current_call(self, session, call_id, *, expected="active"):
        call = await session.get(TelephonyCall, _uuid(call_id))
        if call is None or call.state != expected:
            raise TelephonyError("telephony_call_not_active", 409)
        route = await session.get(TelephonyRoute, call.route_id)
        if route is None or route.version != call.route_version or route.route_hash != call.route_hash or route.agent_id != call.agent_id or route.agent_revision_id != call.agent_revision_id:
            raise TelephonyError("telephony_route_stale", 409)
        current = {key: getattr(route, key) for key in ("display_name", "connection_id", "provider_route_ref", "agent_id", "agent_revision_id", "timezone", "business_hours_json", "greeting_override", "transfer_policy_json", "fallback_mode", "fallback_destination_key")}
        if self._route_values(current)["route_hash"] != route.route_hash:
            raise TelephonyError("telephony_directory_changed", 409)
        reasons = await self._readiness(session, route)
        if reasons:
            raise TelephonyError(reasons[0], 409)
        return call, route, await self._connection(session, route.connection_id)

    async def _prepare_runtime(self, call_id, api_key):
        from .telephony_runtime import TelephonyLiveRuntime
        async with self.session() as session:
            call, route, connection = await self._current_call(session, call_id, expected="accepting")
            revision = await session.get(AgentRevision, route.agent_revision_id)
            conversation = ConversationSession(id=uuid4(), user_id=str(connection.owner_user_id), character_name="AI employee",
                title="Telephone reception", project_id=connection.project_id,
                context={"source": "telephony", "telephony_call_id": str(call.id), "agent_id": str(call.agent_id), "agent_revision_id": str(call.agent_revision_id)})
            session.add(conversation)
            call.conversation_session_id = conversation.id
            await session.commit()
            snapshot = {"call_id": str(call.id), "conversation_session_id": str(conversation.id), "agent_id": str(call.agent_id),
                "agent_revision_id": str(call.agent_revision_id), "project_id": str(connection.project_id) if connection.project_id else None,
                "instructions": "\n".join([revision.mission or "", revision.responsibility_summary or "", revision.operational_instructions or "",
                    "Speak concisely. Verify caller requests before sensitive actions. Never reveal internal secrets. Transfer only using destination keys.", route.greeting_override or ""]),
                "team_id": revision.agent_team_id, "execution_profile_id": revision.execution_profile_id,
                "connection_id": str(connection.id),
                "received_at": call.received_at.replace(tzinfo=timezone.utc).isoformat(),
                "model": self.resolve_realtime_model(revision),
                "destination_keys": (route.transfer_policy_json or {}).get("destination_keys", [])}
        run = await self.agent_runs.create_run(session_id=snapshot["conversation_session_id"], user_id=None,
            agent_id=snapshot["agent_id"], agent_revision_id=snapshot["agent_revision_id"], project_id=snapshot["project_id"],
            client_message_id=f"telephony:{call_id}", objective="Telephone reception", run_type="live_voice_session",
            provider="openai", model=snapshot["model"],
            metadata={"source": "telephony", "telephony_call_id": str(call_id), "team_id": snapshot["team_id"], "execution_profile_id": snapshot["execution_profile_id"]})
        snapshot["agent_run_id"] = run["id"]
        async with self.session() as session:
            await session.execute(update(TelephonyCall).where(TelephonyCall.id == _uuid(call_id), TelephonyCall.state == "accepting").values(agent_run_id=_uuid(run["id"])))
            await session.commit()
        await self.agent_runs.mark_running(run["id"], message="Telephone reception started")
        factory = self.runtime_factory or TelephonyLiveRuntime
        runtime = factory(self, snapshot=snapshot, api_key=api_key)
        try:
            await runtime.prepare()
        except BaseException:
            try:
                await asyncio.shield(self.agent_runs.fail_run(run["id"], error="telephony_runtime_prepare_failed"))
            finally:
                await runtime.close_local()
            raise
        return runtime

    async def transfer(self, call_id, destination_key):
        if not isinstance(destination_key, str) or not KEY.fullmatch(destination_key):
            raise TelephonyError("telephony_transfer_destination_invalid")
        if self.action_policy_service is None or self.action_execution_service is None:
            raise TelephonyError("telephony_action_kernel_unavailable", 503)
        async with self.session() as session:
            call, route, connection = await self._current_call(session, call_id)
            if call.transfer_destination_key is not None:
                raise TelephonyError("telephony_transfer_already_attempted", 409)
            policy = route.transfer_policy_json or {}
            if destination_key not in policy.get("destination_keys", []):
                raise TelephonyError("telephony_transfer_destination_denied", 403)
            action = await self.action_policy_service.propose_action(session,
                policy_revision_id=policy["action_policy_revision_id"], origin_agent_id=call.agent_id,
                origin_agent_run_id=call.agent_run_id, origin_work_item_id=None, automation_rule_revision_id=None,
                source_event_id=None, action_position=0, payload={"telephony_call_id": str(call.id), "destination_key": destination_key})
            action_id = action.id
            await session.commit()
            result = await self.action_execution_service.execute_action(session, action_id, agent_run_id=call.agent_run_id)
            await session.commit()
            return result

    async def hangup(self, call_id):
        async with self.session() as session:
            call = await session.get(TelephonyCall, _uuid(call_id))
            if call is not None and call.state in {"completed", "uncertain", "transferred", "rejected", "failed"}:
                return {"state": call.state}
            call, route, connection = await self._current_call(session, call_id)
            credential = await self._credential(session, connection)
        if not await self._change(call.id, "active", "uncertain", route_fence=call, safe_error_code="hangup_pending"):
            return {"state": "uncertain"}
        result = await self._provider_command("hangup", connection_id=connection.id, call_id=call.provider_call_id,
            conversation_id=call.conversation_session_id, credential_pins=credential.pins, api_key=credential.reveal()["api_key"])
        await self._settle(call.id, "uncertain", result, success="completed")
        return {"state": "completed" if result.status == "succeeded" else result.status}

    async def _validate_accept_runtime(self, session, runtime):
        snapshot = runtime.snapshot
        call, route, connection = await self._current_call(session, snapshot["call_id"], expected="accepting")
        revision = await session.get(AgentRevision, call.agent_revision_id)
        if (self.resolve_realtime_model(revision) != snapshot["model"]
            or str(connection.id) != snapshot["connection_id"]
            or str(call.agent_run_id) != str(snapshot["agent_run_id"])
            or str(call.conversation_session_id) != snapshot["conversation_session_id"]):
            raise TelephonyError("telephony_accept_binding_changed", 409)
        return call

    async def _provider_command(self, operation, *, connection_id, call_id, conversation_id=None, credential_pins=None, accept_runtime=None, **kwargs):
        from ..memory.models import Project
        # Resolve scope freshly for every outbound command, including reject.
        async with self.session() as session:
            connection = await self._connection(session, connection_id)
            credential = await self._credential(session, connection, pins=credential_pins)
            if not hmac.compare_digest(credential.reveal()["api_key"], kwargs["api_key"]):
                raise TelephonyError("telephony_credential_stale", 409)
            project = await session.get(Project, connection.project_id) if connection.project_id else None
            if connection.project_id and (project is None or project.deleted_at is not None):
                raise TelephonyError("telephony_project_unavailable", 403)
            conversation = await session.get(ConversationSession, _uuid(conversation_id)) if conversation_id else None
            context = dict(conversation.context or {}) if conversation else {}
            metadata = dict(project.project_metadata or {}) if project else {}
            if operation == "accept":
                if accept_runtime is None:
                    raise TelephonyError("telephony_accept_runtime_required", 403)
                await self._validate_accept_runtime(session, accept_runtime)
        token = set_privacy_policy_context(session_context=context, project_metadata=metadata)
        try:
            return await getattr(self.provider, operation)(call_id, **kwargs)
        finally:
            reset_privacy_policy_context(token)

    async def terminate_runtime(self, runtime):
        """Trusted safety teardown of one positively accepted local session.

        This is not a model/admin control endpoint. The caller is the owning
        SIP runtime. Current employee/route grants cannot prevent revocation
        cleanup; connection credential and egress restrictions still apply.
        """
        snapshot = runtime.snapshot
        async with self.session() as session:
            call = await session.get(TelephonyCall, _uuid(snapshot["call_id"]))
            if call is None or call.state not in {"active", "accepting"} or call.accepted_at is None:
                return
            if str(call.conversation_session_id) != snapshot["conversation_session_id"] or str(call.agent_run_id) != str(snapshot["agent_run_id"]):
                raise TelephonyError("telephony_runtime_binding_invalid", 403)
            connection = await self._connection(session, snapshot["connection_id"])
            credential = await self._credential(session, connection)
        if not await self._change(call.id, call.state, "uncertain", safe_error_code="hangup_pending"):
            return
        result = await self._provider_command("hangup", connection_id=connection.id, call_id=call.provider_call_id,
            conversation_id=call.conversation_session_id, credential_pins=credential.pins, api_key=credential.reveal()["api_key"])
        await self._settle(call.id, "uncertain", result, success="completed")

    async def recover_interrupted_calls(self, *, stale_before=None):
        """Classify expired orphan calls, without touching another live worker.

        The default cutoff is two hours old, beyond the runtime's absolute
        one-hour lifetime plus setup/shutdown allowance. A supplied cutoff may
        be older, never newer. This only records uncertainty: no remote retry.
        """
        latest = datetime.now(timezone.utc) - timedelta(seconds=RECOVERY_MIN_AGE_SECONDS)
        cutoff = stale_before or latest
        if not isinstance(cutoff, datetime):
            raise TelephonyError("telephony_recovery_cutoff_invalid")
        cutoff = cutoff.replace(tzinfo=timezone.utc) if cutoff.tzinfo is None else cutoff.astimezone(timezone.utc)
        if cutoff > latest:
            raise TelephonyError("telephony_recovery_cutoff_too_recent")
        async with self.session() as session:
            result = await session.execute(update(TelephonyCall).where(
                TelephonyCall.state.in_(["incoming", "accepting", "active"]),
                TelephonyCall.received_at < cutoff.replace(tzinfo=None),
                TelephonyCall.updated_at < cutoff.replace(tzinfo=None),
                TelephonyCall.id.not_in([_uuid(key) for key in self._runtimes]),
            ).values(
                state="uncertain", safe_error_code="telephony_runtime_interrupted", version=TelephonyCall.version + 1))
            await session.commit()
            return result.rowcount

    async def close(self):
        runtimes, self._runtimes = self._runtimes, {}
        failures = []
        for call_id, runtime in runtimes.items():
            try:
                await self.terminate_runtime(runtime)
                await self._change(call_id, "active", "uncertain", safe_error_code="telephony_runtime_shutdown")
            except Exception:
                failures.append("telephony_shutdown_audit_unavailable")
            finally:
                try:
                    await runtime.close_local()
                except Exception:
                    failures.append("telephony_shutdown_runtime_unavailable")
        if failures:
            raise TelephonyError(failures[0], 503)


class TelephonyTransferAdapter:
    adapter_key = "openai_realtime_sip"
    adapter_version = "1"

    def __init__(self, service):
        self.service = service

    async def execute(self, *, action_id, attempt_id, execution_key, payload, connection, credential, quote=None):
        from .integration_action_registry import IntegrationActionResult
        service = self.service
        async with service.session() as session:
            call, route, binding = await service._current_call(session, payload["telephony_call_id"])
            from ..memory.models import ExternalAction, ExternalActionAttempt
            from ..memory.models.agent_automation import AgentActionPolicyRevision
            action = await session.get(ExternalAction, _uuid(action_id))
            policy = await session.get(AgentActionPolicyRevision, action.action_policy_revision_id) if action else None
            attempt = await session.get(ExternalActionAttempt, _uuid(attempt_id))
            if (policy is None or str(policy.constraints_json.get("route_id")) != str(route.id)
                or action.origin_agent_run_id != call.agent_run_id or action.origin_agent_id != call.agent_id
                or action.connection_id != binding.id or str(connection.get("id")) != str(binding.id)
                or str((route.transfer_policy_json or {}).get("action_policy_revision_id")) != str(policy.id)
                or action.status != "attempting" or action.execution_key != execution_key or action.payload_json != payload
                or attempt is None or attempt.action_id != action.id or attempt.status != "running" or attempt.execution_key != execution_key):
                raise TelephonyError("telephony_transfer_binding_invalid", 403)
            key = payload["destination_key"]
            if key not in (route.transfer_policy_json or {}).get("destination_keys", []):
                raise TelephonyError("telephony_transfer_destination_denied", 403)
            target = service.directory[route.provider_route_ref]["destinations"][key]["uri"]
            credential_pins = {"revision": action.credential_revision, "state_hash": action.credential_state_hash,
                               "connection_version": binding.version}
        if not await service._change(call.id, "active", "uncertain", route_fence=call, transfer_fence=True, safe_error_code="transfer_pending", transfer_destination_key=key):
            return IntegrationActionResult(status="uncertain", safe_error_code="telephony_transfer_fenced")
        try:
            result = await service._provider_command("refer", connection_id=binding.id, call_id=call.provider_call_id,
                conversation_id=call.conversation_session_id, credential_pins=credential_pins, api_key=credential.reveal()["api_key"], target_uri=target)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            return IntegrationActionResult(status="uncertain", safe_error_code="telephony_transfer_uncertain")
        await service._settle(call.id, "uncertain", result, success="transferred")
        # REFER acknowledgement proves the provider accepted the referral, not
        # that a destination answered. No receipt claims a connected call.
        return IntegrationActionResult(status=result.status,
            provider_attempt_ref=result.request_id, provider_receipt_ref=result.request_id if result.status == "succeeded" else None,
            remote_resource_id=call.provider_call_id, remote_status="refer_accepted" if result.status == "succeeded" else result.status,
            observed_at=result.observed_at, safe_error_code=result.safe_error_code)
