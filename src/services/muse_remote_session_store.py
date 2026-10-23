"""Server owned Muse session bindings and remote target ports.

The remote Muse adapter deliberately has no access to application settings or
database models.  This module is the small integration boundary that supplies
those values while keeping credential and provider state private to the
ConversationSession row.

``ConversationSession.context`` is already durable JSON, so a separate schema
or migration is unnecessary for the side-thread binding.  The public model
projection removes the ``muse_remote`` key before a session is serialized.
"""

from __future__ import annotations

import copy
import inspect
import os
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select

from ..memory.models import ConversationSession
from .muse_remote_execution import (
    MuseBinding,
    MuseCredentialResolution,
    MuseError,
    MuseErrorCode,
    MUSE_EXECUTION_TARGET,
    MUSE_PROVIDER,
    REMOTE_AGENT_DELEGATE,
)


MUSE_REMOTE_CONTEXT_KEY = "muse_remote"


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read a dotted config value without invoking arbitrary methods."""

    if isinstance(config, Mapping):
        node: Any = config
        for part in str(key).split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return default if node is None else node
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        except Exception:
            return default
        return default if value is None else value
    return default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bounded(value: Any, *, limit: int = 512) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) <= limit else ""


def _allowlisted(value: str, raw: Any) -> bool:
    """Require an explicit non-empty allowlist and an exact member match."""

    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = list(raw)
    else:
        values = []
    normalized = {_bounded(item, limit=255) for item in values if _bounded(item, limit=255)}
    return bool(normalized) and value in normalized


def _safe_uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _binding_dict(binding: MuseBinding) -> dict[str, Any]:
    """Serialize only the adapter's private binding fields to JSON."""

    normalized = binding.normalized()
    value = asdict(normalized)
    return {str(key): item for key, item in value.items() if item is not None}


def _binding_from_context(value: Any) -> MuseBinding | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("binding") if isinstance(value.get("binding"), Mapping) else value
    try:
        return MuseBinding(**dict(raw)).normalized()
    except Exception:
        return None


class MuseRemoteSessionStore:
    """Durable ``MuseBindingStore`` keyed by ConversationSession UUID."""

    def __init__(self, db_manager: Any | None = None) -> None:
        self._db_manager = db_manager

    async def _session(self) -> Any:
        manager = self._db_manager
        if manager is None:
            from ..memory.database import get_database_manager

            manager = get_database_manager()
        if manager is None or not callable(getattr(manager, "get_session", None)):
            raise RuntimeError("database manager is unavailable")
        value = manager.get_session()
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    async def _close(session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            value = close()
            if inspect.isawaitable(value):
                await value

    @staticmethod
    async def _rollback(session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            value = rollback()
            if inspect.isawaitable(value):
                await value

    @staticmethod
    def _session_id(binding_key: Any) -> uuid.UUID:
        value = _safe_uuid(binding_key)
        if value is None:
            raise ValueError("Muse binding key must be a ConversationSession UUID")
        return uuid.UUID(value)

    @staticmethod
    def _context(row: Any) -> dict[str, Any]:
        context = getattr(row, "context", None)
        return copy.deepcopy(dict(context)) if isinstance(context, Mapping) else {}

    async def get_binding(self, binding_key: str) -> MuseBinding | None:
        db = await self._session()
        try:
            row = await db.get(ConversationSession, self._session_id(binding_key))
            if row is None or getattr(row, "deleted_at", None) is not None:
                return None
            context = self._context(row)
            remote = context.get(MUSE_REMOTE_CONTEXT_KEY)
            return _binding_from_context(remote)
        finally:
            await self._close(db)

    async def persist_binding(self, binding_key: str, binding: MuseBinding) -> None:
        sid = self._session_id(binding_key)
        normalized = binding.normalized()
        if normalized.remote_target_key not in (None, str(sid)):
            raise ValueError("Muse binding target does not match session")
        db = await self._session()
        try:
            result = await db.execute(
                select(ConversationSession).where(ConversationSession.id == sid).with_for_update()
            )
            row = result.scalars().first()
            if row is None or getattr(row, "deleted_at", None) is not None:
                raise ValueError("ConversationSession is unavailable")
            context = self._context(row)
            context[MUSE_REMOTE_CONTEXT_KEY] = {"binding": _binding_dict(normalized)}
            row.context = context
            await db.commit()
        except Exception:
            await self._rollback(db)
            raise
        finally:
            await self._close(db)

    async def mark_unsafe(self, binding_key: str, *, reason: str) -> None:
        current = await self.get_binding(binding_key)
        if current is None:
            return
        updated = replace(
            current,
            safe_to_send=False,
            state="unsafe",
        )
        await self.persist_binding(binding_key, updated)

    async def dispose(self, binding_key: str) -> None:
        sid = self._session_id(binding_key)
        db = await self._session()
        try:
            result = await db.execute(
                select(ConversationSession).where(ConversationSession.id == sid).with_for_update()
            )
            row = result.scalars().first()
            if row is None:
                await db.rollback()
                return
            context = self._context(row)
            context.pop(MUSE_REMOTE_CONTEXT_KEY, None)
            row.context = context
            await db.commit()
        except Exception:
            await self._rollback(db)
            raise
        finally:
            await self._close(db)


class MuseRemoteTargetResolver:
    """Resolve a server owned Muse target for one admitted session.

    The caller supplies only identity.  Credential references, provider state
    roots, VM/node identity and the authentication fingerprint are read from
    the explicit ``remote_targets.muse`` server configuration.
    """

    def __init__(self, config: Any | None = None) -> None:
        self.config = config

    def _target(self) -> Mapping[str, Any]:
        return _as_mapping(_config_get(self.config, "remote_targets.muse", {}))

    @staticmethod
    def _credential_ref(target: Mapping[str, Any], owner_id: str) -> str:
        for key in ("owner_credential_refs", "credentials_by_owner", "credential_refs"):
            values = target.get(key)
            if isinstance(values, Mapping):
                value = values.get(owner_id)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        value = target.get("credential_ref")
        return value.strip() if isinstance(value, str) and value.strip() else ""

    async def resolve(
        self,
        *,
        owner_id: str,
        agent_id: str | None = None,
        revision_id: str | None = None,
        agent_revision_id: str | None = None,
        remote_target_key: str | None = None,
        session_id: str | None = None,
        required_capabilities: Any = (),
        **_: Any,
    ) -> MuseCredentialResolution:
        target = self._target()
        if not _as_bool(target.get("enabled"), default=False):
            raise MuseError(MuseErrorCode.REMOTE_TARGET_DISABLED)
        owner = _bounded(owner_id, limit=255)
        agent = _bounded(agent_id, limit=255)
        revision = _bounded(revision_id or agent_revision_id, limit=255)
        target_key = _safe_uuid(remote_target_key or session_id)
        if not owner or not agent or not revision or not target_key:
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        if not _allowlisted(owner, target.get("allowed_owner_ids")):
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        if not _allowlisted(agent, target.get("allowed_agent_ids")):
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        capabilities = {str(item).strip() for item in (required_capabilities or ()) if str(item).strip()}
        if capabilities != {REMOTE_AGENT_DELEGATE}:
            raise MuseError(MuseErrorCode.REMOTE_CAPABILITY_REQUIRED)
        credential_ref = self._credential_ref(target, owner)
        state_root = target.get("state_root")
        vm_id = _bounded(target.get("vm_id"), limit=255)
        node_id = _bounded(target.get("node_id"), limit=255) or None
        auth_fingerprint = _bounded(
            target.get("auth_fingerprint") or target.get("credential_identity_hash"),
            limit=256,
        )
        authenticated = _as_bool(target.get("authenticated"), default=False)
        if not credential_ref or not state_root or not vm_id or not auth_fingerprint:
            raise MuseError(MuseErrorCode.CREDENTIAL_UNAVAILABLE)
        if not authenticated:
            raise MuseError(MuseErrorCode.REMOTE_AUTHENTICATION_FAILED)
        if not _as_bool(target.get("egress_enabled"), default=False):
            raise MuseError(MuseErrorCode.OUTBOUND_GATE_REQUIRED)
        return MuseCredentialResolution(
            owner_id=owner,
            credential_ref=credential_ref,
            state_root=Path(str(state_root)),
            vm_id=vm_id,
            node_id=node_id,
            auth_fingerprint=auth_fingerprint,
            authenticated=True,
            remote_target_enabled=True,
            agent_id=agent,
            revision_id=revision,
            remote_target_key=target_key,
        )


class MuseRemotePrivacyGateway:
    """Small fail-closed egress gate used by the remote adapter."""

    def __init__(self, config: Any | None = None) -> None:
        self.config = config
        self._gateway: Any | None = None

    async def protect(self, payload: str, **kwargs: Any) -> str:
        target = _as_mapping(_config_get(self.config, "remote_targets.muse", {}))
        if not _as_bool(target.get("enabled"), default=False) or not _as_bool(
            target.get("egress_enabled"), default=False
        ):
            raise RuntimeError("Muse remote egress is disabled")
        try:
            from ..features import Features

            if Features.is_enterprise():
                raise RuntimeError("Muse remote egress is unavailable in Enterprise")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("Muse remote feature gate is unavailable") from None
        if self._gateway is None:
            from .outbound_privacy_service import OutboundPrivacyGateway

            self._gateway = OutboundPrivacyGateway(self.config)
        result = await self._gateway.protect(
            payload,
            provider=MUSE_PROVIDER,
            base_url=MUSE_EXECUTION_TARGET,
            source_kind="muse_remote_turn",
            model=None,
            descriptor={
                "action": "muse.session.send",
                "transport": "muse-cli",
                "destination": MUSE_EXECUTION_TARGET,
                "provider": MUSE_PROVIDER,
                "tool": "muse-cli",
                "model": "",
            },
        )
        value = getattr(result, "final_payload", None)
        if value is None:
            value = getattr(result, "payload", None)
        if not isinstance(value, str) or not value:
            raise RuntimeError("Muse remote privacy gate returned no payload")
        return value


__all__ = [
    "MUSE_REMOTE_CONTEXT_KEY",
    "MuseRemotePrivacyGateway",
    "MuseRemoteSessionStore",
    "MuseRemoteTargetResolver",
]
