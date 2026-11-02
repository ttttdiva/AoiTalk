"""Fail-closed ledger for the optional external DOTS dialogue route.

The service never loads AoiTalk credentials or infers an owner from request
data. Callers pass either a verified MCP OAuth principal or the separately
typed server-owned dedicated connection principal, plus the Aoi user
authenticated by the normal application session. Importing this module
performs no DB work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import secrets
from typing import Any
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models.conversations import ConversationSession
from ..memory.models.conversations import ConversationMessage
from ..memory.models.dots_dialogue import (
    DotsDialogueBinding,
    DotsDialogueOperationReceipt,
    DotsDialogueOutbox,
    DotsDialogueRequest,
    DotsDialogueSubscription,
)


DOTS_DIALOGUE_EVENT = "aoitalk.owner_inbox.v1.created"
DEFAULT_REQUEST_TTL = timedelta(hours=1)
MAX_REQUEST_TTL = timedelta(hours=24)
MAX_SUBSCRIPTION_TTL = timedelta(hours=24)
MAX_PENDING_DELIVERIES = 5
DELIVERY_LEASE = timedelta(seconds=15)
MAX_MESSAGE_CHARS = 32_768
MAX_LABEL_CHARS = 120


class DotsDialogueError(Exception):
    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class VerifiedMcpPrincipal:
    """Identity assembled only from a trusted MCP authentication verifier."""

    client_id: str
    issuer: str
    subject: str

    @property
    def fingerprint(self) -> str:
        values = (self.client_id, self.issuer, self.subject)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in values):
            raise DotsDialogueError("trusted_principal_missing", 403)
        encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DedicatedDotsPrincipal:
    """Server-owned identity for the one approved stdio/Tunnel connection.

    This is deliberately not an OAuth principal.  Its fingerprint binds the
    fixed app, plugin, Tunnel, runtime, profile and Aoi owner.  The one Chat is
    persisted separately on ``DotsDialogueBinding`` and cannot be rotated.
    """

    app_id: str
    plugin_id: str
    tunnel_id: str
    runtime_alias: str
    profile_alias: str
    owner_user_id: uuid.UUID
    conversation_id: uuid.UUID

    @property
    def fingerprint(self) -> str:
        values = (
            self.app_id, self.plugin_id, self.tunnel_id, self.runtime_alias,
            self.profile_alias, str(_require_uuid(self.owner_user_id, "owner")),
        )
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in values):
            raise DotsDialogueError("trusted_principal_missing", 403)
        encoded = _canonical(["aoitalk.dots.dedicated.v1", *values])
        _require_uuid(self.conversation_id, "conversation_id")
        return hashlib.sha256(encoded).hexdigest()


DialoguePrincipal = VerifiedMcpPrincipal | DedicatedDotsPrincipal


def _principal_fingerprint(principal: DialoguePrincipal | None) -> str:
    if not isinstance(principal, (VerifiedMcpPrincipal, DedicatedDotsPrincipal)):
        raise DotsDialogueError("trusted_principal_missing", 403)
    return principal.fingerprint


@dataclass(frozen=True)
class DialogueRequestReceipt:
    request_id: uuid.UUID
    conversation_id: uuid.UUID
    state: str
    created_at: datetime
    user_message_id: uuid.UUID


@dataclass(frozen=True)
class DialogueToolRead:
    request_id: uuid.UUID
    owner_user_id: uuid.UUID
    binding_id: uuid.UUID
    conversation_id: uuid.UUID
    generation: int
    message: str = field(repr=False)
    state: str
    expires_at: datetime

    @property
    def original_text(self) -> str:
        """Return the server-stored owner text under the read-only contract."""
        return self.message


@dataclass(frozen=True)
class OutboxDelivery:
    lease_id: uuid.UUID
    outbox_id: uuid.UUID
    subscription_id: str
    callback_url: str = field(repr=False)
    signing_secret: str = field(repr=False)
    event_id: str = field(repr=False)
    body: bytes = field(repr=False)
    previous_signing_secret: str | None = field(default=None, repr=False)


def _sha256(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validated_webhook_secret(value: str) -> str:
    import base64
    if not isinstance(value, str) or not value.startswith("whsec_"):
        raise DotsDialogueError("invalid_webhook_secret", 422)
    encoded = value[6:]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise DotsDialogueError("invalid_webhook_secret", 422) from None
    if not 24 <= len(decoded) <= 64:
        raise DotsDialogueError("invalid_webhook_secret", 422)
    return value


def _utcnow() -> datetime:
    return datetime.utcnow()


def _require_uuid(value: Any, name: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise DotsDialogueError(f"invalid_{name}", 422) from None


def _active_binding(binding: DotsDialogueBinding | None, *, owner_id: uuid.UUID | None = None,
                    fingerprint: str | None = None, now: datetime) -> DotsDialogueBinding:
    if binding is None or binding.state != "active" or binding.owner_confirmed_at is None:
        raise DotsDialogueError("binding_unavailable", 404)
    if owner_id is not None and binding.owner_user_id != owner_id:
        raise DotsDialogueError("binding_unavailable", 404)
    if fingerprint is not None and not hmac.compare_digest(binding.principal_fingerprint, fingerprint):
        raise DotsDialogueError("binding_unavailable", 404)
    return binding


def _conversation_has_dots_route(conversation: ConversationSession) -> bool:
    """Read the persisted per-Chat route without treating request data as authority."""
    context = getattr(conversation, "context", None)
    if not isinstance(context, dict):
        return False
    settings = context.get("chat_llm_settings")
    if not isinstance(settings, dict):
        return False
    main_route = settings.get("main_route")
    if not isinstance(main_route, dict):
        return False
    provider = main_route.get("provider")
    model = main_route.get("model")
    return (
        isinstance(provider, str)
        and provider.strip().lower() == "gpt-dots"
        and isinstance(model, str)
        and model.strip().lower() == "dedicated-chat"
    )


class DotsDialogueService:
    """Transactional storage operations. Constructed explicitly; enabled is false by default."""

    def __init__(self, *, enabled: bool = False, now=_utcnow, token_factory=None):
        self.enabled = enabled is True
        self._now = now
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def _enabled(self) -> None:
        if not self.enabled:
            raise DotsDialogueError("dots_dialogue_disabled", 404)

    async def list_bindings(self, session: AsyncSession, *, owner_user_id: Any) -> list[dict[str, Any]]:
        self._enabled()
        owner_id = _require_uuid(owner_user_id, "owner")
        rows = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.owner_user_id == owner_id,
        ).order_by(DotsDialogueBinding.created_at, DotsDialogueBinding.id))).scalars().all()
        return [{"binding_id": str(row.id), "intended_chat_label": row.intended_chat_label,
                 "state": row.state, "generation": row.generation,
                 "owner_confirmed_at": row.owner_confirmed_at.isoformat() + "Z"}
                for row in rows]

    async def get_dedicated_chat_availability(
        self,
        session: AsyncSession,
        *,
        owner_user_id: Any,
        principal: DialoguePrincipal | None,
        conversation_id: Any | None = None,
    ) -> dict[str, Any]:
        """Project a requested owner Chat against the fixed DOTS connection.

        ``principal.conversation_id`` remains the immutable connection anchor.
        A requested ``conversation_id`` is only a read-only target projection;
        it never changes the binding or the dispatcher scope.  Omitting it
        preserves the legacy fixed-Chat projection.
        """
        self._enabled()
        owner_id = _require_uuid(owner_user_id, "owner")
        if not isinstance(principal, DedicatedDotsPrincipal):
            return {"selectable": False, "state": "connection_unavailable"}
        if _require_uuid(principal.owner_user_id, "owner") != owner_id:
            return {"selectable": False, "state": "binding_unavailable"}
        anchor_conversation_id = _require_uuid(principal.conversation_id, "conversation_id")
        target_conversation_id = (
            _require_uuid(conversation_id, "conversation_id")
            if conversation_id is not None
            else anchor_conversation_id
        )
        conversation = await session.get(ConversationSession, target_conversation_id)
        if (
            conversation is None
            or str(conversation.user_id) != str(owner_id)
            or conversation.deleted_at is not None
        ):
            return {"selectable": False, "state": "conversation_unavailable"}
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.principal_fingerprint == principal.fingerprint,
            DotsDialogueBinding.owner_user_id == owner_id,
            DotsDialogueBinding.dedicated_conversation_id == anchor_conversation_id,
        ))).scalar_one_or_none()
        if binding is None:
            return {"selectable": False, "state": "binding_unavailable"}
        result: dict[str, Any] = {
            "selectable": False,
            "state": "binding_unavailable",
            "owner_user_id": str(owner_id),
            "binding_id": str(binding.id),
            "conversation_id": str(target_conversation_id),
            "target_chat_id": str(target_conversation_id),
            "connection_anchor_chat_id": str(anchor_conversation_id),
            "generation": binding.generation,
            "subscription_state": None,
            "subscription_expires_at": None,
            "latest_request_id": None,
            "latest_request_state": None,
        }
        if binding.state != "active" or binding.owner_confirmed_at is None:
            return result

        now = self._now()
        subscription = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.binding_id == binding.id,
            DotsDialogueSubscription.owner_user_id == owner_id,
            DotsDialogueSubscription.principal_fingerprint == principal.fingerprint,
            DotsDialogueSubscription.generation == binding.generation,
        ))).scalar_one_or_none()
        if subscription is None:
            result["state"] = "subscription_missing"
        else:
            expires_at = subscription.expires_at
            result["subscription_state"] = subscription.state
            result["subscription_expires_at"] = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if subscription.state != "active":
                result["state"] = "subscription_revoked"
            elif expires_at <= now:
                result["state"] = "subscription_expired"
            else:
                result["selectable"] = True
                result["state"] = "active"

        latest_request = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.binding_id == binding.id,
            DotsDialogueRequest.owner_user_id == owner_id,
            DotsDialogueRequest.generation == binding.generation,
            DotsDialogueRequest.conversation_id == target_conversation_id,
        ).order_by(DotsDialogueRequest.sequence.desc()).limit(1))).scalar_one_or_none()
        if latest_request is not None:
            latest_state = latest_request.state
            if latest_request.expires_at <= now and latest_state in {"pending", "delivery_accepted", "read"}:
                latest_state = "expired"
            result["latest_request_id"] = str(latest_request.id)
            result["latest_request_state"] = latest_state
        return result

    async def confirm_binding(self, session: AsyncSession, *, owner_user_id: Any,
                              principal: DialoguePrincipal | None, intended_chat_label: str,
                              owner_confirmed: bool) -> DotsDialogueBinding:
        """Create/rotate a binding after normal owner auth and server-owned principal validation."""
        self._enabled()
        owner_id = _require_uuid(owner_user_id, "owner")
        fingerprint = _principal_fingerprint(principal)
        dedicated_conversation_id = None
        if isinstance(principal, DedicatedDotsPrincipal):
            dedicated_owner_id = _require_uuid(principal.owner_user_id, "owner")
            dedicated_conversation_id = _require_uuid(principal.conversation_id, "conversation_id")
            if dedicated_owner_id != owner_id:
                raise DotsDialogueError("binding_unavailable", 404)
            conversation = await session.get(ConversationSession, dedicated_conversation_id)
            if (conversation is None or str(conversation.user_id) != str(owner_id)
                    or conversation.deleted_at is not None):
                raise DotsDialogueError("conversation_unavailable", 404)
        label = str(intended_chat_label or "").strip()
        if not owner_confirmed:
            raise DotsDialogueError("owner_confirmation_required", 403)
        if not label or len(label) > MAX_LABEL_CHARS:
            raise DotsDialogueError("invalid_chat_label", 422)

        statement = (select(DotsDialogueBinding)
                     .where(DotsDialogueBinding.principal_fingerprint == fingerprint)
                     .with_for_update())
        binding = (await session.execute(statement)).scalar_one_or_none()
        now = self._now()
        if binding is None:
            binding = DotsDialogueBinding(
                owner_user_id=owner_id,
                principal_fingerprint=fingerprint,
                dedicated_conversation_id=dedicated_conversation_id,
                intended_chat_label=label,
                owner_confirmed_at=now,
                generation=1,
                state="active",
            )
            try:
                async with session.begin_nested():
                    session.add(binding)
                    await session.flush()
            except IntegrityError:
                existing = (await session.execute(statement)).scalar_one_or_none()
                if existing is None or existing.owner_user_id != owner_id:
                    raise DotsDialogueError("principal_owner_conflict", 409) from None
                binding = existing
        else:
            if binding.owner_user_id != owner_id:
                raise DotsDialogueError("principal_owner_conflict", 409)
            if isinstance(principal, DedicatedDotsPrincipal):
                if binding.dedicated_conversation_id != dedicated_conversation_id:
                    raise DotsDialogueError("dedicated_conversation_immutable", 409)
                if binding.state != "active" or binding.owner_confirmed_at is None:
                    raise DotsDialogueError("binding_unavailable", 409)
                # Repeated owner confirmation is idempotent for this dedicated
                # connection.  It must not rotate its generation or revive a
                # revoked subscription/request set.
                return binding
            if binding.dedicated_conversation_id is not None:
                raise DotsDialogueError("binding_unavailable", 409)
            binding.generation += 1
            binding.intended_chat_label = label
            binding.owner_confirmed_at = now
            binding.state = "active"
            binding.cursor_position = 0
            binding.next_sequence = 0
            subs = (await session.execute(select(DotsDialogueSubscription).where(
                DotsDialogueSubscription.binding_id == binding.id))).scalars().all()
            for sub in subs:
                sub.state = "revoked"
            requests = (await session.execute(select(DotsDialogueRequest).where(
                DotsDialogueRequest.binding_id == binding.id,
                DotsDialogueRequest.generation < binding.generation,
                DotsDialogueRequest.state.in_(("pending", "delivery_accepted", "read"))))).scalars().all()
            for request in requests:
                request.state = "cancelled"
                outbox = await session.get(DotsDialogueOutbox, request.id)
                if outbox is not None and outbox.state != "acked":
                    outbox.state = "cancelled"
        await session.flush()
        return binding

    async def revoke_binding(self, session: AsyncSession, *, owner_user_id: Any,
                             binding_id: Any) -> None:
        self._enabled()
        owner_id = _require_uuid(owner_user_id, "owner")
        binding_uuid = _require_uuid(binding_id, "binding_id")
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.id == binding_uuid,
            DotsDialogueBinding.owner_user_id == owner_id,
        ).with_for_update())).scalar_one_or_none()
        if binding is None:
            raise DotsDialogueError("binding_unavailable", 404)
        if binding.state == "revoked":
            return
        binding.state = "revoked"
        binding.generation += 1
        subs = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.binding_id == binding.id))).scalars().all()
        for sub in subs:
            sub.state = "revoked"
        requests = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.binding_id == binding.id,
            DotsDialogueRequest.state.in_(("pending", "delivery_accepted", "read"))))).scalars().all()
        for request in requests:
            request.state = "cancelled"
            outbox = await session.get(DotsDialogueOutbox, request.id)
            if outbox is not None and outbox.state != "acked":
                outbox.state = "cancelled"
        await session.flush()

    async def list_events(self, session: AsyncSession, *, principal: DialoguePrincipal | None) -> list[dict[str, Any]]:
        self._enabled()
        fingerprint = _principal_fingerprint(principal)
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.principal_fingerprint == fingerprint,
            DotsDialogueBinding.dedicated_conversation_id == (
                principal.conversation_id if isinstance(principal, DedicatedDotsPrincipal) else None
            ),
            DotsDialogueBinding.state == "active",
            DotsDialogueBinding.owner_confirmed_at.is_not(None),
        ))).scalar_one_or_none()
        if binding is None:
            return []
        return [{
            "name": DOTS_DIALOGUE_EVENT,
            "description": "Deliver an owner-authored AoiTalk request to the owner-confirmed DOTS chat.",
            "delivery": ["webhook"],
            "inputSchema": ({
                "type": "object", "properties": {}, "required": [], "additionalProperties": False,
            } if isinstance(principal, DedicatedDotsPrincipal) else {
                "type": "object", "properties": {"binding_id": {"type": "string", "format": "uuid"}},
                "required": ["binding_id"], "additionalProperties": False,
            }),
            "payloadSchema": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string", "format": "uuid"},
                    "conversation_id": {"type": "string", "format": "uuid"},
                    "binding_id": {"type": "string", "format": "uuid"},
                    "generation": {"type": "integer"},
                    "delivery_token": {"type": "string"},
                },
            "required": ["request_id", "conversation_id", "binding_id", "generation", "delivery_token"],
                "additionalProperties": False,
            },
        }]

    async def subscribe(self, session: AsyncSession, *, principal: DialoguePrincipal | None,
                        name: str, arguments: dict[str, Any], callback_url: str, signing_secret: str,
                        ttl_ms: int | None, cursor: str | None) -> dict[str, Any]:
        self._enabled()
        fingerprint = _principal_fingerprint(principal)
        required_arguments = set() if isinstance(principal, DedicatedDotsPrincipal) else {"binding_id"}
        if name != DOTS_DIALOGUE_EVENT or not isinstance(arguments, dict) or set(arguments) != required_arguments:
            raise DotsDialogueError("invalid_subscription", 422)
        now = self._now()
        binding_query = select(DotsDialogueBinding).where(
            DotsDialogueBinding.principal_fingerprint == fingerprint,
            DotsDialogueBinding.dedicated_conversation_id == (
                principal.conversation_id if isinstance(principal, DedicatedDotsPrincipal) else None
            ),
        )
        if not isinstance(principal, DedicatedDotsPrincipal):
            binding_query = binding_query.where(
                DotsDialogueBinding.id == _require_uuid(arguments.get("binding_id"), "binding_id"),
            )
        binding = (await session.execute(binding_query.with_for_update())).scalar_one_or_none()
        _active_binding(binding, fingerprint=fingerprint, now=now)
        if not isinstance(callback_url, str) or len(callback_url) > 2048:
            raise DotsDialogueError("invalid_callback", 422)
        signing_secret = _validated_webhook_secret(signing_secret)
        callback_hash = _sha256(callback_url)
        arguments_hash = _sha256(_canonical(arguments))
        subscription_id = "sub_" + _sha256(_canonical([fingerprint, callback_url, name, arguments]))[:64]
        sub = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.id == subscription_id,
        ).with_for_update())).scalar_one_or_none()
        if sub is not None and (sub.binding_id != binding.id or sub.owner_user_id != binding.owner_user_id
                                or sub.principal_fingerprint != fingerprint
                                or sub.callback_hash != callback_hash or sub.arguments_hash != arguments_hash):
            raise DotsDialogueError("binding_already_subscribed", 409)
        current_generation_sub = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.binding_id == binding.id,
            DotsDialogueSubscription.generation == binding.generation,
            DotsDialogueSubscription.state == "active",
        ).with_for_update())).scalar_one_or_none()
        if current_generation_sub is not None and (sub is None or current_generation_sub.id != sub.id):
            raise DotsDialogueError("binding_already_subscribed", 409)
        if sub is not None and sub.generation != binding.generation:
            if sub.generation > binding.generation or sub.state != "revoked":
                raise DotsDialogueError("binding_confirmation_required", 403)
        resume_position = 0
        if cursor is not None:
            if sub is None:
                raise DotsDialogueError("cursor_unavailable", 409)
            cursor_row = (await session.execute(select(DotsDialogueOutbox).where(
                DotsDialogueOutbox.subscription_id == sub.id,
                DotsDialogueOutbox.generation == binding.generation,
                DotsDialogueOutbox.cursor_after == _require_uuid(cursor, "cursor"),
                DotsDialogueOutbox.sequence <= binding.cursor_position,
            ))).scalar_one_or_none()
            if cursor_row is None:
                raise DotsDialogueError("cursor_unavailable", 409)
            resume_position = cursor_row.sequence
        if ttl_ms is not None and (not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0):
            raise DotsDialogueError("invalid_ttl", 422)
        ttl = timedelta(milliseconds=ttl_ms) if ttl_ms is not None else timedelta(hours=1)
        expires_at = now + min(ttl, MAX_SUBSCRIPTION_TTL)
        if sub is None:
            sub = DotsDialogueSubscription(
                id=subscription_id, binding_id=binding.id, owner_user_id=binding.owner_user_id,
                principal_fingerprint=fingerprint, generation=binding.generation,
                event_name=name, arguments_hash=arguments_hash, callback_hash=callback_hash,
                callback_url=callback_url, signing_secret=signing_secret,
                expires_at=expires_at, state="active",
            )
            session.add(sub)
        else:
            if sub.state == "revoked" and sub.generation == binding.generation:
                raise DotsDialogueError("binding_confirmation_required", 403)
            old_secret = sub.signing_secret
            if not hmac.compare_digest(old_secret, signing_secret):
                sub.previous_signing_secret = old_secret
                sub.secret_rotation_until = now + timedelta(minutes=5)
            sub.signing_secret = signing_secret
            sub.expires_at = expires_at
            sub.generation = binding.generation
            sub.state = "active"
        replay_rows = (await session.execute(select(DotsDialogueOutbox).where(
            DotsDialogueOutbox.subscription_id == sub.id,
            DotsDialogueOutbox.generation == binding.generation,
            DotsDialogueOutbox.sequence > resume_position,
            DotsDialogueOutbox.state == "acked",
        ).order_by(DotsDialogueOutbox.sequence).with_for_update())).scalars().all()
        failed_gap = (await session.execute(select(DotsDialogueOutbox.request_id).where(
            DotsDialogueOutbox.subscription_id == sub.id,
            DotsDialogueOutbox.generation == binding.generation,
            DotsDialogueOutbox.sequence > resume_position,
            DotsDialogueOutbox.state.in_(("failed", "cancelled")),
        ).limit(1))).first() is not None
        for replay in replay_rows:
            replay.state = "pending"
            replay.acknowledged_at = None
            replay.next_attempt_at = now
        if replay_rows:
            binding.cursor_position = min(binding.cursor_position, resume_position)
        response_cursor = cursor
        if not replay_rows and response_cursor is None and binding.cursor_position > 0:
            latest = (await session.execute(select(DotsDialogueOutbox.cursor_after).where(
                DotsDialogueOutbox.subscription_id == sub.id,
                DotsDialogueOutbox.generation == binding.generation,
                DotsDialogueOutbox.sequence == binding.cursor_position,
                DotsDialogueOutbox.state == "acked",
            ))).scalar_one_or_none()
            response_cursor = str(latest) if latest is not None else None
        await session.flush()
        return {"id": sub.id, "refreshBefore": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cursor": response_cursor, "truncated": failed_gap}

    async def unsubscribe(self, session: AsyncSession, *, principal: DialoguePrincipal | None,
                          name: str, arguments: dict[str, Any], callback_url: str) -> dict[str, Any]:
        self._enabled()
        fingerprint = _principal_fingerprint(principal)
        required_arguments = set() if isinstance(principal, DedicatedDotsPrincipal) else {"binding_id"}
        if name != DOTS_DIALOGUE_EVENT or not isinstance(arguments, dict) or set(arguments) != required_arguments:
            raise DotsDialogueError("invalid_subscription", 422)
        binding_query = select(DotsDialogueBinding).where(
            DotsDialogueBinding.principal_fingerprint == fingerprint,
            DotsDialogueBinding.dedicated_conversation_id == (
                principal.conversation_id if isinstance(principal, DedicatedDotsPrincipal) else None
            ),
        )
        if not isinstance(principal, DedicatedDotsPrincipal):
            binding_query = binding_query.where(
                DotsDialogueBinding.id == _require_uuid(arguments.get("binding_id"), "binding_id"),
            )
        binding = (await session.execute(binding_query.with_for_update())).scalar_one_or_none()
        if binding is None:
            return {}
        sub = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.binding_id == binding.id,
            DotsDialogueSubscription.generation == binding.generation,
            DotsDialogueSubscription.callback_hash == _sha256(callback_url),
        ).with_for_update())).scalar_one_or_none()
        if sub is None:
            return {}
        sub.state = "revoked"
        binding.state = "awaiting_confirmation"
        binding.generation += 1
        requests = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.binding_id == binding.id,
            DotsDialogueRequest.state.in_(("pending", "delivery_accepted", "read"))))).scalars().all()
        for request in requests:
            request.state = "cancelled"
            outbox = await session.get(DotsDialogueOutbox, request.id)
            if outbox is not None and outbox.state != "acked":
                outbox.state = "cancelled"
        await session.flush()
        return {}

    async def enqueue_request(self, session: AsyncSession, *, owner_user_id: Any, binding_id: Any,
                              conversation_id: Any, idempotency_key: str, message: str,
                              principal: DialoguePrincipal | None = None) -> DialogueRequestReceipt:
        self._enabled()
        owner_id = _require_uuid(owner_user_id, "owner")
        binding_uuid = _require_uuid(binding_id, "binding_id")
        conversation_uuid = _require_uuid(conversation_id, "conversation_id")
        now = self._now()
        binding_query = select(DotsDialogueBinding).where(
            DotsDialogueBinding.id == binding_uuid,
            DotsDialogueBinding.owner_user_id == owner_id,
        )
        principal_fingerprint = None
        if principal is not None:
            principal_fingerprint = _principal_fingerprint(principal)
            binding_query = binding_query.where(
                DotsDialogueBinding.principal_fingerprint == principal_fingerprint,
            )
            if isinstance(principal, DedicatedDotsPrincipal):
                binding_query = binding_query.where(
                    DotsDialogueBinding.dedicated_conversation_id
                    == _require_uuid(principal.conversation_id, "conversation_id"),
                )
            else:
                binding_query = binding_query.where(
                    DotsDialogueBinding.dedicated_conversation_id.is_(None),
                )
        binding = (await session.execute(binding_query.with_for_update())).scalar_one_or_none()
        _active_binding(binding, owner_id=owner_id, fingerprint=principal_fingerprint, now=now)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise DotsDialogueError("invalid_idempotency_key", 422)
        if not isinstance(message, str) or not message or len(message) > MAX_MESSAGE_CHARS:
            raise DotsDialogueError("invalid_message", 422)
        conversation = (await session.execute(select(ConversationSession).where(
            ConversationSession.id == conversation_uuid,
            ConversationSession.user_id == str(owner_id),
            ConversationSession.deleted_at.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if conversation is None:
            raise DotsDialogueError("conversation_unavailable", 404)
        if (binding.dedicated_conversation_id is not None
                and not _conversation_has_dots_route(conversation)):
            raise DotsDialogueError("dots_dialogue_not_selected", 409)
        key_hash = _sha256(idempotency_key)
        fingerprint = _sha256(_canonical([str(conversation_uuid), message]))
        existing = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.binding_id == binding.id,
            DotsDialogueRequest.generation == binding.generation,
            DotsDialogueRequest.idempotency_hash == key_hash,
        ).with_for_update())).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != fingerprint:
                raise DotsDialogueError("idempotency_conflict", 409)
            return DialogueRequestReceipt(
                existing.id, existing.conversation_id, existing.state, existing.created_at,
                uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:dots-dialogue:user:{existing.id}"),
            )

        # A replay of the same immutable request remains recoverable after its
        # subscription expires. New requests still require the current active
        # lease, and replay never creates another outbox row.
        sub = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.binding_id == binding.id,
            DotsDialogueSubscription.generation == binding.generation,
            DotsDialogueSubscription.state == "active",
            DotsDialogueSubscription.expires_at > now,
        ).with_for_update())).scalar_one_or_none()
        if sub is None:
            raise DotsDialogueError("active_subscription_required", 409)

        binding.next_sequence += 1
        request_id = uuid.uuid4()
        token = self._token_factory()
        if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
            raise DotsDialogueError("delivery_token_unavailable", 500)
        request = DotsDialogueRequest(
            id=request_id, owner_user_id=owner_id, binding_id=binding.id,
            subscription_id=sub.id, conversation_id=conversation_uuid,
            generation=binding.generation, sequence=binding.next_sequence,
            idempotency_hash=key_hash, request_hash=fingerprint, message=message,
            delivery_token_hash=_sha256(token), delivery_token=token,
            state="pending", expires_at=now + DEFAULT_REQUEST_TTL,
        )
        user_message_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:dots-dialogue:user:{request_id}")
        parent = (await session.execute(select(ConversationMessage).where(
            ConversationMessage.session_id == conversation.id,
            ConversationMessage.deleted_at.is_(None),
            or_(ConversationMessage.is_active_branch.is_(True), ConversationMessage.is_active_branch.is_(None)),
        ).order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(1).with_for_update())).scalar_one_or_none()
        sibling_predicate = (ConversationMessage.parent_message_id == parent.id
                             if parent is not None else ConversationMessage.parent_message_id.is_(None))
        sibling_index = int((await session.scalar(select(func.count()).where(
            ConversationMessage.session_id == conversation.id,
            sibling_predicate,
            ConversationMessage.deleted_at.is_(None),
        ))) or 0)
        session.add(ConversationMessage(
            id=user_message_id,
            session_id=conversation.id,
            role="user",
            content=message,
            client_message_id=f"dots-dialogue:{request_id}:user",
            sender_type="user",
            sender_id=str(owner_id),
            sender_display_name="Owner",
            parent_message_id=parent.id if parent is not None else None,
            message_metadata={
                "external_dots_dialogue": True,
                "dots_dialogue_kind": "request",
                "request_id": str(request_id),
                "client_message_id": idempotency_key,
            },
            created_at=now,
            updated_at=now,
            is_active_branch=True,
            branch_index=sibling_index,
        ))
        conversation.message_count = int(conversation.message_count or 0) + 1
        conversation.last_activity = now
        outbox = DotsDialogueOutbox(
            request_id=request_id, subscription_id=sub.id,
            generation=binding.generation, sequence=binding.next_sequence,
            cursor_after=uuid.uuid4(), state="pending",
            attempt_count=0, next_attempt_at=now,
        )
        session.add(request)
        await session.flush()
        session.add(outbox)
        await session.flush()
        return DialogueRequestReceipt(
            request.id, request.conversation_id, request.state, request.created_at, user_message_id,
        )

    async def enqueue_dedicated_request(
        self,
        session: AsyncSession,
        *,
        owner_user_id: Any,
        principal: DedicatedDotsPrincipal | None,
        idempotency_key: str,
        message: str,
    ) -> DialogueRequestReceipt:
        """Queue to the one owner-confirmed conversation without client IDs."""
        self._enabled()
        if not isinstance(principal, DedicatedDotsPrincipal):
            raise DotsDialogueError("trusted_principal_missing", 403)
        owner_id = _require_uuid(owner_user_id, "owner")
        if owner_id != _require_uuid(principal.owner_user_id, "owner"):
            raise DotsDialogueError("binding_unavailable", 404)
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.principal_fingerprint == principal.fingerprint,
            DotsDialogueBinding.owner_user_id == owner_id,
            DotsDialogueBinding.dedicated_conversation_id == _require_uuid(principal.conversation_id, "conversation_id"),
            DotsDialogueBinding.state == "active",
            DotsDialogueBinding.owner_confirmed_at.is_not(None),
        ).with_for_update())).scalar_one_or_none()
        if binding is None:
            raise DotsDialogueError("binding_unavailable", 404)
        return await self.enqueue_request(
            session,
            owner_user_id=owner_id,
            binding_id=binding.id,
            conversation_id=principal.conversation_id,
            idempotency_key=idempotency_key,
            message=message,
            principal=principal,
        )

    async def read_for_principal(self, session: AsyncSession, *, principal: DialoguePrincipal | None,
                                 request_id: Any, delivery_token: str) -> DialogueToolRead:
        self._enabled()
        fingerprint = _principal_fingerprint(principal)
        if not isinstance(delivery_token, str) or not 32 <= len(delivery_token) <= 256:
            raise DotsDialogueError("request_unavailable", 404)
        request_uuid = _require_uuid(request_id, "request_id")
        now = self._now()
        reference = (await session.execute(select(
            DotsDialogueRequest.binding_id,
            DotsDialogueRequest.owner_user_id,
        ).where(DotsDialogueRequest.id == request_uuid))).one_or_none()
        if reference is None:
            raise DotsDialogueError("request_unavailable", 404)
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.id == reference.binding_id,
            DotsDialogueBinding.owner_user_id == reference.owner_user_id,
            DotsDialogueBinding.principal_fingerprint == fingerprint,
            DotsDialogueBinding.dedicated_conversation_id == (
                principal.conversation_id if isinstance(principal, DedicatedDotsPrincipal) else None
            ),
        ).with_for_update())).scalar_one_or_none()
        if binding is None:
            raise DotsDialogueError("request_unavailable", 404)
        request = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.id == request_uuid,
            DotsDialogueRequest.binding_id == binding.id,
            DotsDialogueRequest.owner_user_id == binding.owner_user_id,
        ).with_for_update())).scalar_one_or_none()
        if request is None:
            raise DotsDialogueError("request_unavailable", 404)
        sub = await session.get(DotsDialogueSubscription, request.subscription_id)
        _active_binding(binding, owner_id=request.owner_user_id, fingerprint=fingerprint, now=now)
        if (binding.generation != request.generation or sub is None or sub.state != "active"
                or sub.generation != request.generation or sub.expires_at <= now
                or request.expires_at <= now or request.state in {"cancelled", "expired", "delivery_failed"}
                or not hmac.compare_digest(request.delivery_token_hash, _sha256(delivery_token))):
            raise DotsDialogueError("request_unavailable", 404)
        if request.state == "pending":
            raise DotsDialogueError("request_not_delivered", 409)
        if request.state not in {"delivery_accepted", "read", "replied"}:
            raise DotsDialogueError("request_unavailable", 404)
        conversation = (await session.execute(select(ConversationSession.id).where(
            ConversationSession.id == request.conversation_id,
            ConversationSession.user_id == str(request.owner_user_id),
            ConversationSession.deleted_at.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if conversation is None:
            raise DotsDialogueError("conversation_unavailable", 404)
        if request.state == "delivery_accepted":
            request.state = "read"
            await session.flush()
        return DialogueToolRead(
            request.id,
            request.owner_user_id,
            binding.id,
            request.conversation_id,
            request.generation,
            request.message,
            request.state,
            request.expires_at,
        )

    async def reply_for_principal(self, session: AsyncSession, *, principal: DialoguePrincipal | None,
                                  request_id: Any, delivery_token: str, operation_id: str,
                                  text_value: str) -> dict[str, Any]:
        self._enabled()
        fingerprint = _principal_fingerprint(principal)
        if not isinstance(text_value, str) or not text_value or len(text_value) > MAX_MESSAGE_CHARS:
            raise DotsDialogueError("invalid_reply", 422)
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise DotsDialogueError("invalid_operation_id", 422)
        read = await self.read_for_principal(session, principal=principal, request_id=request_id,
                                             delivery_token=delivery_token)
        now = self._now()
        request = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.id == read.request_id,
        ).with_for_update())).scalar_one()
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.id == request.binding_id,
        ).with_for_update())).scalar_one()
        _active_binding(binding, owner_id=request.owner_user_id, fingerprint=fingerprint, now=now)
        if binding.generation != request.generation or request.expires_at <= now or request.state in {"cancelled", "expired"}:
            raise DotsDialogueError("request_unavailable", 404)
        operation_hash = _sha256(operation_id)
        body_hash = _sha256(_canonical([str(request.id), text_value]))
        existing = (await session.execute(select(DotsDialogueOperationReceipt).where(
            DotsDialogueOperationReceipt.binding_id == binding.id,
            DotsDialogueOperationReceipt.generation == binding.generation,
            DotsDialogueOperationReceipt.operation_hash == operation_hash,
        ).with_for_update())).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != body_hash or existing.request_id != request.id:
                raise DotsDialogueError("operation_conflict", 409)
            return {"accepted": True, "receipt_id": str(existing.id), "request_id": str(request.id)}
        if request.state == "replied":
            raise DotsDialogueError("reply_already_accepted", 409)
        conversation = (await session.execute(select(ConversationSession).where(
            ConversationSession.id == request.conversation_id,
            ConversationSession.user_id == str(request.owner_user_id),
            ConversationSession.deleted_at.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if conversation is None:
            raise DotsDialogueError("conversation_unavailable", 404)
        user_message_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:dots-dialogue:user:{request.id}")
        parent = (await session.execute(select(ConversationMessage).where(
            ConversationMessage.session_id == conversation.id,
            ConversationMessage.id == user_message_id,
            ConversationMessage.deleted_at.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if parent is None:
            # A DOTS reply must attach to the exact server-created request
            # message. Falling back to another active message can cross a
            # branch/request boundary, so a missing or tombstoned parent is a
            # hard failure.
            raise DotsDialogueError("request_unavailable", 404)
        sibling_predicate = ConversationMessage.parent_message_id == parent.id
        sibling_index = int((await session.scalar(select(func.count()).where(
            ConversationMessage.session_id == conversation.id,
            sibling_predicate,
            ConversationMessage.deleted_at.is_(None),
        ))) or 0)
        receipt = DotsDialogueOperationReceipt(
            owner_user_id=request.owner_user_id, binding_id=binding.id, request_id=request.id,
            generation=request.generation, operation_hash=operation_hash,
            request_hash=body_hash, state="accepted",
        )
        request.reply = text_value
        request.state = "replied"
        request.replied_at = now
        assistant_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:dots-dialogue:{request.id}")
        active_child_count = int((await session.scalar(select(func.count()).where(
            ConversationMessage.session_id == conversation.id,
            ConversationMessage.parent_message_id == parent.id,
            ConversationMessage.deleted_at.is_(None),
            ConversationMessage.is_active_branch.is_(True),
            ConversationMessage.id != assistant_id,
        ))) or 0)
        # A late DOTS answer is still correlated to its exact request user
        # row, but it must not reactivate that branch after the owner has
        # continued from the same parent. Match ConversationRepository's
        # explicit-parent semantics: retain the late sibling as inactive.
        assistant_is_active = bool(parent.is_active_branch) and active_child_count == 0
        conversation_message = (await session.execute(select(ConversationMessage).where(
            ConversationMessage.session_id == conversation.id,
            ConversationMessage.id == assistant_id,
        ))).scalar_one_or_none()
        if conversation_message is None:
            conversation_message = ConversationMessage(
                id=assistant_id,
                session_id=conversation.id,
                role="assistant",
                content=text_value,
                client_message_id=f"dots-dialogue:{request.id}",
                sender_type="external",
                sender_id=str(binding.id),
                sender_display_name="External DOTS",
                parent_message_id=parent.id,
                message_metadata={"external_dots_dialogue": True, "request_id": str(request.id)},
                created_at=now,
                updated_at=now,
                is_active_branch=assistant_is_active,
                branch_index=sibling_index,
            )
            session.add(conversation_message)
            conversation.message_count = int(conversation.message_count or 0) + 1
        conversation.last_activity = now
        session.add(receipt)
        await session.flush()
        return {"accepted": True, "receipt_id": str(receipt.id), "request_id": str(request.id)}

    async def cancel_request(self, session: AsyncSession, *, owner_user_id: Any, request_id: Any) -> None:
        self._enabled()
        owner_id = _require_uuid(owner_user_id, "owner")
        request_uuid = _require_uuid(request_id, "request_id")
        request = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.id == request_uuid,
            DotsDialogueRequest.owner_user_id == owner_id,
        ).with_for_update())).scalar_one_or_none()
        if request is None:
            raise DotsDialogueError("request_unavailable", 404)
        if request.state == "replied":
            raise DotsDialogueError("request_already_replied", 409)
        if request.state == "cancelled":
            return
        request.state = "cancelled"
        outbox = await session.get(DotsDialogueOutbox, request.id)
        if outbox is not None and outbox.state != "acked":
            outbox.state = "cancelled"
        await session.flush()

    async def get_owner_request(self, session: AsyncSession, *, owner_user_id: Any,
                                request_id: Any) -> dict[str, Any]:
        self._enabled()
        owner_id = _require_uuid(owner_user_id, "owner")
        request_uuid = _require_uuid(request_id, "request_id")
        request = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.id == request_uuid,
            DotsDialogueRequest.owner_user_id == owner_id,
        ))).scalar_one_or_none()
        if request is None:
            raise DotsDialogueError("request_unavailable", 404)
        conversation = (await session.execute(select(ConversationSession.id).where(
            ConversationSession.id == request.conversation_id,
            ConversationSession.user_id == str(owner_id),
            ConversationSession.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if conversation is None:
            raise DotsDialogueError("conversation_unavailable", 404)
        if request.expires_at <= self._now() and request.state in {"pending", "delivery_accepted", "read"}:
            request.state = "expired"
            outbox = await session.get(DotsDialogueOutbox, request.id)
            if outbox is not None and outbox.state != "acked":
                outbox.state = "cancelled"
            await session.flush()
        return {"request_id": str(request.id), "owner_user_id": str(request.owner_user_id),
                "binding_id": str(request.binding_id),
                "conversation_id": str(request.conversation_id),
                "target_chat_id": str(request.conversation_id),
                "generation": request.generation, "original_text": request.message,
                "message": request.message, "state": request.state, "reply": request.reply,
                "created_at": request.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "delivery_accepted_at": request.delivery_accepted_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if request.delivery_accepted_at else None,
                "replied_at": request.replied_at.strftime("%Y-%m-%dT%H:%M:%SZ") if request.replied_at else None}

    async def lease_next_delivery(self, session: AsyncSession, *, now: datetime | None = None,
                                  principal_fingerprint: str | None = None,
                                  dedicated_conversation_id: Any | None = None) -> OutboxDelivery | None:
        """Lease one due event with a short fence for a future dispatcher."""
        self._enabled()
        current = now or self._now()
        if principal_fingerprint is not None and (
            not isinstance(principal_fingerprint, str)
            or len(principal_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in principal_fingerprint)
        ):
            raise DotsDialogueError("trusted_principal_missing", 403)
        dedicated_conversation_uuid = None
        if principal_fingerprint is not None:
            if dedicated_conversation_id is None:
                raise DotsDialogueError("trusted_principal_missing", 403)
            dedicated_conversation_uuid = _require_uuid(dedicated_conversation_id, "conversation_id")
        elif dedicated_conversation_id is not None:
            raise DotsDialogueError("trusted_principal_missing", 403)

        def candidate_query():
            query = select(DotsDialogueOutbox.request_id)
            if principal_fingerprint is not None:
                query = query.join(DotsDialogueRequest, DotsDialogueRequest.id == DotsDialogueOutbox.request_id).join(
                    DotsDialogueBinding, DotsDialogueBinding.id == DotsDialogueRequest.binding_id,
                ).where(
                    DotsDialogueBinding.principal_fingerprint == principal_fingerprint,
                    DotsDialogueBinding.dedicated_conversation_id == dedicated_conversation_uuid,
                )
            return query

        candidate_id = await session.scalar(
            candidate_query().where(DotsDialogueOutbox.state == "pending",
                                    DotsDialogueOutbox.next_attempt_at <= current)
            .order_by(DotsDialogueOutbox.next_attempt_at, DotsDialogueOutbox.sequence).limit(1)
        )
        if candidate_id is None:
            candidate_id = await session.scalar(
                candidate_query().where(DotsDialogueOutbox.state == "leased",
                                        DotsDialogueOutbox.leased_until <= current)
                .order_by(DotsDialogueOutbox.leased_until)
                .limit(1)
            )
        if candidate_id is None:
            return None
        request_ref = (await session.execute(select(
            DotsDialogueRequest.binding_id,
        ).where(DotsDialogueRequest.id == candidate_id))).scalar_one_or_none()
        if request_ref is None:
            return None
        # All writer paths lock binding before request/outbox. Keep this order
        # so owner revocation cannot deadlock with a dispatcher lease.
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.id == request_ref,
        ).with_for_update())).scalar_one_or_none()
        request = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.id == candidate_id,
        ).with_for_update())).scalar_one_or_none()
        outbox = (await session.execute(select(DotsDialogueOutbox).where(
            DotsDialogueOutbox.request_id == candidate_id,
        ).with_for_update())).scalar_one_or_none()
        if outbox is None or request is None or binding is None:
            return None
        sub = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.id == outbox.subscription_id,
        ).with_for_update())).scalar_one_or_none()
        # The candidate query above is non-locking. A competing worker may
        # lease that row before this transaction obtains locks. Re-read after
        # locking and leave a live lease untouched.
        if outbox.state == "leased":
            if outbox.leased_until is None or outbox.leased_until > current:
                return None
            if request.state not in {"pending", "delivery_accepted", "read", "replied"}:
                outbox.state = "cancelled"
                outbox.lease_id = None
                outbox.leased_until = None
                await session.flush()
                return None
            if request.expires_at <= current:
                outbox.state = "cancelled"
                if request.state != "replied":
                    request.state = "expired"
                outbox.lease_id = None
                outbox.leased_until = None
                await session.flush()
                return None
            if outbox.attempt_count >= MAX_PENDING_DELIVERIES:
                outbox.state = "failed"
                if request.state != "replied":
                    request.state = "delivery_failed"
                outbox.lease_id = None
                outbox.leased_until = None
                await session.flush()
                return None
            outbox.state = "pending"
            outbox.lease_id = None
            outbox.leased_until = None
        elif outbox.state != "pending":
            # Another transaction already acked, cancelled, or failed this
            # stale candidate; its locked state transition is authoritative.
            return None
        elif outbox.next_attempt_at > current:
            return None

        # Keep revocation/expiry cancellation separate from candidate races.
        if (binding.state != "active" or sub is None or sub.state != "active"
                or outbox.generation != binding.generation
                or request.generation != binding.generation or sub.generation != binding.generation):
            outbox.state = "cancelled"
            outbox.lease_id = None
            outbox.leased_until = None
            if request.state != "replied":
                request.state = "cancelled"
            await session.flush()
            return None
        if request.expires_at <= current or sub.expires_at <= current:
            outbox.state = "cancelled"
            outbox.lease_id = None
            outbox.leased_until = None
            if request.state != "replied":
                request.state = "expired"
            await session.flush()
            return None
        if request.state not in {"pending", "delivery_accepted", "read", "replied"}:
            outbox.state = "cancelled"
            outbox.lease_id = None
            outbox.leased_until = None
            await session.flush()
            return None
        conversation = (await session.execute(select(ConversationSession.id).where(
            ConversationSession.id == request.conversation_id,
            ConversationSession.user_id == str(request.owner_user_id),
            ConversationSession.deleted_at.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if conversation is None:
            outbox.state = "cancelled"
            outbox.lease_id = None
            outbox.leased_until = None
            if request.state != "replied":
                request.state = "cancelled"
            await session.flush()
            return None
        lease_id = uuid.uuid4()
        outbox.state = "leased"
        outbox.attempt_count += 1
        outbox.lease_id = lease_id
        outbox.leased_until = current + DELIVERY_LEASE
        event_id, body = await self.build_event_body(session, outbox_id=outbox.request_id)
        previous_secret = sub.previous_signing_secret if sub.secret_rotation_until and sub.secret_rotation_until > current else None
        if previous_secret is None and sub.previous_signing_secret is not None:
            sub.previous_signing_secret = None
            sub.secret_rotation_until = None
        return OutboxDelivery(lease_id, outbox.request_id, sub.id, sub.callback_url,
                              sub.signing_secret, event_id, body, previous_secret)

    async def mark_delivery_result(self, session: AsyncSession, *, outbox_id: Any, lease_id: Any,
                                   http_status: int, error_code: str | None = None) -> str:
        """Record one transport result; 2xx means accepted, never answered."""
        self._enabled()
        outbox_uuid, lease_uuid = _require_uuid(outbox_id, "outbox_id"), _require_uuid(lease_id, "lease_id")
        request_ref = (await session.execute(select(DotsDialogueRequest.binding_id).where(
            DotsDialogueRequest.id == outbox_uuid,
        ))).scalar_one_or_none()
        if request_ref is None:
            raise DotsDialogueError("delivery_lease_stale", 409)
        binding = (await session.execute(select(DotsDialogueBinding).where(
            DotsDialogueBinding.id == request_ref,
        ).with_for_update())).scalar_one_or_none()
        if binding is None:
            raise DotsDialogueError("delivery_lease_stale", 409)
        request = (await session.execute(select(DotsDialogueRequest).where(
            DotsDialogueRequest.id == outbox_uuid,
            DotsDialogueRequest.binding_id == binding.id,
        ).with_for_update())).scalar_one_or_none()
        if request is None:
            raise DotsDialogueError("delivery_lease_stale", 409)
        outbox = (await session.execute(select(DotsDialogueOutbox).where(
            DotsDialogueOutbox.request_id == outbox_uuid,
        ).with_for_update())).scalar_one_or_none()
        if outbox is None or outbox.lease_id != lease_uuid:
            raise DotsDialogueError("delivery_lease_stale", 409)
        if outbox.state == "cancelled":
            if isinstance(http_status, int) and not isinstance(http_status, bool) and 0 <= http_status <= 599:
                outbox.last_http_status = http_status
            outbox.lease_id = None
            outbox.leased_until = None
            await session.flush()
            return "cancelled"
        if outbox.state != "leased":
            raise DotsDialogueError("delivery_lease_stale", 409)
        sub = (await session.execute(select(DotsDialogueSubscription).where(
            DotsDialogueSubscription.id == outbox.subscription_id,
            DotsDialogueSubscription.binding_id == binding.id,
        ).with_for_update())).scalar_one_or_none()
        now = self._now()
        if outbox.leased_until is None or outbox.leased_until <= now:
            raise DotsDialogueError("delivery_lease_stale", 409)
        if not isinstance(http_status, int) or isinstance(http_status, bool) or not 0 <= http_status <= 599:
            http_status = 0
        outbox.last_http_status = http_status
        allowed_errors = {"callback_dns_failed", "callback_delivery_failed", "callback_response_too_large",
                          "callback_challenge_invalid", "callback_challenge_rejected", "invalid_callback_url"}
        outbox.last_error_code = error_code if isinstance(error_code, str) and error_code in allowed_errors else None
        if (binding is None or sub is None or binding.state != "active" or sub.state != "active"
                or binding.generation != request.generation or sub.generation != request.generation
                or request.expires_at <= now or sub.expires_at <= now or request.state == "cancelled"):
            outbox.state = "cancelled"
            request.state = "cancelled" if request.state != "replied" else request.state
            await session.flush()
            return "cancelled"
        if 200 <= http_status < 300:
            outbox.state = "acked"
            outbox.acknowledged_at = now
            request.delivery_accepted_at = now
            if request.state == "pending":
                request.state = "delivery_accepted"
            # The contiguous cursor advances only across acknowledged events.
            while binding.cursor_position < binding.next_sequence:
                following = (await session.execute(select(DotsDialogueOutbox).where(
                    DotsDialogueOutbox.subscription_id == sub.id,
                    DotsDialogueOutbox.generation == binding.generation,
                    DotsDialogueOutbox.sequence == binding.cursor_position + 1,
                ))).scalar_one_or_none()
                if following is None or following.state != "acked":
                    break
                binding.cursor_position += 1
            await session.flush()
            return "delivery_accepted"
        retryable = http_status in {408, 425, 429} or 500 <= http_status <= 599 or http_status == 0
        if retryable and outbox.attempt_count < MAX_PENDING_DELIVERIES and request.expires_at > now:
            outbox.state = "pending"
            outbox.lease_id = None
            outbox.leased_until = None
            delay = min(60, 2 ** max(outbox.attempt_count - 1, 0))
            outbox.next_attempt_at = now + timedelta(seconds=delay)
            await session.flush()
            return "retry_pending"
        outbox.state = "failed"
        if request.state != "replied":
            request.state = "delivery_failed"
        await session.flush()
        return "failed"

    async def build_event_body(self, session: AsyncSession, *, outbox_id: Any) -> tuple[str, bytes]:
        """Create the exact stable event bytes used for HMAC signing; never log or return them in APIs."""
        self._enabled()
        request_uuid = _require_uuid(outbox_id, "outbox_id")
        outbox = await session.get(DotsDialogueOutbox, request_uuid)
        if outbox is None:
            raise DotsDialogueError("delivery_unavailable", 404)
        request = await session.get(DotsDialogueRequest, request_uuid)
        binding = await session.get(DotsDialogueBinding, request.binding_id) if request is not None else None
        if request is None or binding is None:
            raise DotsDialogueError("delivery_unavailable", 404)
        conversation = (await session.execute(select(ConversationSession.id).where(
            ConversationSession.id == request.conversation_id,
            ConversationSession.user_id == str(request.owner_user_id),
            ConversationSession.deleted_at.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if conversation is None:
            raise DotsDialogueError("conversation_unavailable", 404)
        cursor = None
        if binding.cursor_position > 0:
            contiguous_cursor = (await session.execute(select(DotsDialogueOutbox.cursor_after).where(
                DotsDialogueOutbox.subscription_id == outbox.subscription_id,
                DotsDialogueOutbox.generation == binding.generation,
                DotsDialogueOutbox.sequence == binding.cursor_position,
                DotsDialogueOutbox.state == "acked",
            ))).scalar_one_or_none()
            if contiguous_cursor is not None:
                cursor = str(contiguous_cursor)
        event_id = "evt_" + request.id.hex
        payload = {
            "eventId": event_id,
            "name": DOTS_DIALOGUE_EVENT,
            "timestamp": request.created_at.isoformat(timespec="seconds") + "Z",
            "data": {
                "request_id": str(request.id),
                "conversation_id": str(request.conversation_id),
                "binding_id": str(binding.id),
                "generation": request.generation,
                "delivery_token": request.delivery_token,
            },
            # A later event may be delivered before an earlier one. Expose
            # only the contiguous acknowledged watermark as a resume cursor.
            "cursor": cursor,
        }
        body = _canonical(payload)
        if len(body) > 256 * 1024:
            raise DotsDialogueError("event_too_large", 413)
        return event_id, body
