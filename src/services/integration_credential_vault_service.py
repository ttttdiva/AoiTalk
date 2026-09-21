"""Human-managed encrypted credentials and a trusted, pinned execution seam.

Connection CRUD remains in Operations. This module does not authorize actions;
callers of the internal execution seam must already have resolved their Agent
and action policy authority. Never expose that seam as an HTTP/tool endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, update

from ..features import Features
from ..memory.models import ExternalConnection, Project, User
from ..memory.models.integration_credentials import IntegrationCredential, IntegrationCredentialAuditEvent
from ..security.integration_credential_crypto import (
    IntegrationCredentialCryptoError, IntegrationCredentialKeyUnavailable,
    canonical_payload, decrypt_integration_credential, encrypt_integration_credential,
    integration_credential_ciphertext_key_id,
)


PROVIDER_CAPABILITIES = MappingProxyType({
    "procurement": ("procurement.place_order",),
    "openai_realtime_sip": ("telephony.control",),
})
_EVIDENCE = object()
_SAFE_CODES = frozenset({"provider_verified", "provider_unavailable", "provider_unsupported",
                         "provider_unauthorized", "provider_response_invalid", "verification_pending",
                         "credential_disabled", "key_unavailable", "ciphertext_invalid"})


class IntegrationCredentialError(Exception):
    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _uuid(value: Any) -> UUID:
    try:
        return UUID(str(value))
    except Exception:
        raise IntegrationCredentialError("integration_identifier_invalid", 422) from None


def _field(actor: Any, key: str, default: Any = None) -> Any:
    return actor.get(key, default) if isinstance(actor, dict) else getattr(actor, key, default)


def require_integration_features(provider_key: str | None = None) -> None:
    try:
        allowed = Features.virtual_company() is True and Features.autonomous_agent_runtime() is True
        if provider_key == "openai_realtime_sip":
            allowed = allowed and Features.voice_input() is True
    except Exception:
        allowed = False
    if not allowed:
        raise IntegrationCredentialError("integration_unavailable", 404)


class SecretCredentialHandle:
    """Explicit, in-memory-only access. Accidental serialization is rejected."""

    __slots__ = ("__payload",)

    def __init__(self, payload: dict[str, str]):
        self.__payload = dict(payload)

    def reveal(self) -> dict[str, str]:
        return dict(self.__payload)

    def __repr__(self) -> str:
        return "SecretCredentialHandle([REDACTED])"

    def __reduce_ex__(self, protocol):
        raise TypeError("integration_secret_serialization_forbidden")


@dataclass(frozen=True)
class IntegrationCredentialVerificationResult:
    status: str
    capabilities: dict[str, str]
    provider_code: str
    _evidence: Any = field(default=None, repr=False, compare=False)
    _binding: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class IntegrationCredentialVerifierPolicy:
    """Constructor-only trusted code, never loaded from DB/browser/config.

    The callback may establish credential validity, not choose capabilities.
    Isolated QA can register a deterministic procurement callback here without
    patching validation, cryptography, hashes, or evidence generation.
    """

    capabilities: tuple[str, ...]
    verify: Callable[[SecretCredentialHandle], Awaitable[bool]] = field(repr=False)


def _verification_binding(provider_key: str, payload: dict[str, str]) -> str:
    return hashlib.sha256(canonical_payload([provider_key, payload])).hexdigest()


class IntegrationCredentialProviderVerifier:
    """Code-owned providers; injected transport supports tests, never browsers.

OpenAI checks access to the fixed realtime model via a read-only provider API.
It does not attest SIP trunk configuration, routing, or webhook-secret validity;
the telephony service must establish those separately before accepting calls.
No production procurement verifier exists, so it remains unavailable.
"""

    def __init__(self, *, client_factory=None,
                 provider_policies: Mapping[str, IntegrationCredentialVerifierPolicy] | None = None):
        self._client_factory = client_factory or httpx.AsyncClient
        policies = dict(provider_policies or {})
        for provider, policy in policies.items():
            if (provider not in PROVIDER_CAPABILITIES or type(policy) is not IntegrationCredentialVerifierPolicy
                    or type(policy.capabilities) is not tuple or not policy.capabilities
                    or any(type(key) is not str or key not in PROVIDER_CAPABILITIES[provider] for key in policy.capabilities)
                    or len(set(policy.capabilities)) != len(policy.capabilities) or not callable(policy.verify)):
                raise IntegrationCredentialError("integration_verifier_policy_invalid", 422)
        self._provider_policies = MappingProxyType(policies)

    def verified_capabilities(self, provider_key: str) -> dict[str, str] | None:
        policy = self._provider_policies.get(provider_key)
        if policy:
            return {key: "available" if key in policy.capabilities else "unknown"
                    for key in PROVIDER_CAPABILITIES[provider_key]}
        return {"telephony.control": "available"} if provider_key == "openai_realtime_sip" else None

    async def verify(self, *, provider_key: str, credential: SecretCredentialHandle) -> IntegrationCredentialVerificationResult:
        unknown = {key: "unknown" for key in PROVIDER_CAPABILITIES.get(provider_key, ())}
        policy = self._provider_policies.get(provider_key)
        if policy:
            payload = credential.reveal()
            try:
                valid = await policy.verify(credential)
                if valid is True:
                    return IntegrationCredentialVerificationResult("verified", self.verified_capabilities(provider_key),
                        "provider_verified", _EVIDENCE, _verification_binding(provider_key, payload))
                if valid is False:
                    return IntegrationCredentialVerificationResult("invalid", unknown, "provider_unauthorized")
            except Exception:
                pass
            return IntegrationCredentialVerificationResult("verification_pending", unknown, "provider_response_invalid")
        if provider_key != "openai_realtime_sip":
            return IntegrationCredentialVerificationResult("unsupported", unknown, "provider_unavailable")
        payload = credential.reveal()
        try:
            async with self._client_factory(timeout=10.0, follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", "https://api.openai.com/v1/models/gpt-realtime",
                                         headers={"Authorization": "Bearer " + payload["api_key"]}) as response:
                    if response.status_code in (401, 403):
                        return IntegrationCredentialVerificationResult("invalid", unknown, "provider_unauthorized")
                    if response.status_code != 200:
                        return IntegrationCredentialVerificationResult("verification_pending", unknown, "provider_unavailable")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 64 * 1024:
                            raise ValueError
                    data = json.loads(raw)
                    if type(data) is not dict or data.get("id") != "gpt-realtime" or data.get("object") != "model":
                        raise ValueError
            return IntegrationCredentialVerificationResult(
                "verified", {"telephony.control": "available"}, "provider_verified",
                _EVIDENCE, _verification_binding(provider_key, payload),
            )
        except Exception:
            # Never retain a provider exception, body, URL, or request object.
            return IntegrationCredentialVerificationResult("verification_pending", unknown, "provider_response_invalid")


def _validated_result(result: Any, provider_key: str, payload: dict[str, str],
                      verified_capabilities: dict[str, str] | None = None) -> IntegrationCredentialVerificationResult:
    unknown = {key: "unknown" for key in PROVIDER_CAPABILITIES.get(provider_key, ())}
    fallback = IntegrationCredentialVerificationResult("verification_pending", unknown, "provider_response_invalid")
    if type(result) is not IntegrationCredentialVerificationResult:
        return fallback
    if type(result.status) is not str or result.status not in {"verified", "invalid", "unsupported", "verification_pending"}:
        return fallback
    if type(result.provider_code) is not str or result.provider_code not in _SAFE_CODES:
        return fallback
    if type(result.capabilities) is not dict or set(result.capabilities) != set(unknown):
        return fallback
    if any(type(v) is not str or v not in {"available", "unknown", "unsupported"} for v in result.capabilities.values()):
        return fallback
    if result.status == "verified":
        if (verified_capabilities is None or result._evidence is not _EVIDENCE
                or result._binding != _verification_binding(provider_key, payload)
                or result.provider_code != "provider_verified"
                or result.capabilities != verified_capabilities):
            return fallback
    elif any(v == "available" for v in result.capabilities.values()):
        return fallback
    return result


class IntegrationCredentialVaultService:
    def __init__(self, *, key_provider=None, verifier=None, config=None):
        self.key_provider = key_provider
        self.verifier = verifier or IntegrationCredentialProviderVerifier()
        self.config = config

    async def _connection(self, session, connection_id, *, lock=False):
        require_integration_features()
        statement = select(ExternalConnection).where(ExternalConnection.id == _uuid(connection_id)).execution_options(populate_existing=True)
        if lock:
            statement = statement.with_for_update()
        connection = (await session.execute(statement)).scalar_one_or_none()
        if connection is None:
            raise IntegrationCredentialError("integration_connection_not_found", 404)
        require_integration_features(connection.provider_key)
        return connection

    async def _authorize(self, session, actor, connection):
        if (_field(actor, "actor_type") != "human" or _field(actor, "is_agent", False)
                or _field(actor, "role") != "admin"):
            raise IntegrationCredentialError("integration_admin_human_required", 403)
        user = await session.get(User, _uuid(_field(actor, "id") or _field(actor, "user_id")))
        if user is None or user.role != "admin" or user.is_active is not True:
            raise IntegrationCredentialError("integration_admin_human_required", 403)
        if connection.project_id is not None:
            project = await session.get(Project, connection.project_id)
            if project is None or project.deleted_at is not None:
                raise IntegrationCredentialError("integration_project_not_found", 404)
        return user.id

    async def _credential(self, session, connection):
        row = (await session.execute(select(IntegrationCredential).where(
            IntegrationCredential.connection_id == connection.id,
        ).execution_options(populate_existing=True))).scalar_one_or_none()
        if row and (row.owner_user_id != connection.owner_user_id or row.project_id != connection.project_id):
            raise IntegrationCredentialError("integration_scope_mismatch", 409)
        return row

    @staticmethod
    def _payload(provider_key, credential_kind, payload):
        if provider_key not in PROVIDER_CAPABILITIES:
            raise IntegrationCredentialError("integration_provider_unsupported", 422)
        allowed = {"api_key", "webhook_secret"} if provider_key == "openai_realtime_sip" else {"api_key"}
        if (credential_kind != "api_key" or type(payload) is not dict or "api_key" not in payload
                or not set(payload).issubset(allowed)):
            raise IntegrationCredentialError("integration_payload_invalid", 422)
        if any(type(value) is not str or not 1 <= len(value) <= 8192 or not value.strip()
               or any(ord(ch) < 32 or ord(ch) == 127 for ch in value) for value in payload.values()):
            raise IntegrationCredentialError("integration_payload_invalid", 422)
        return dict(payload)

    @staticmethod
    def _state_hash(row, connection):
        binding = [str(connection.id), str(connection.owner_user_id), str(connection.project_id),
                   connection.provider_key, connection.remote_account_ref, connection.credential_ref]
        state = [str(row.id), str(row.connection_id), row.credential_kind, row.revision, row.status,
                 row.payload_digest, row.encryption_key_id, row.capabilities_json, row.verification_code, binding]
        return hashlib.sha256(canonical_payload(state)).hexdigest()

    async def _fence(self, session, connection, row, expected_revision):
        if type(expected_revision) is not int or expected_revision != (row.revision if row else 0):
            raise IntegrationCredentialError("integration_credential_stale", 409)
        version = connection.version
        result = await session.execute(update(ExternalConnection).where(
            ExternalConnection.id == connection.id, ExternalConnection.version == version,
        ).values(version=version + 1).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise IntegrationCredentialError("integration_connection_stale", 409)
        connection.version = version + 1

    async def _finish(self, session, connection, row, actor_id, event_type):
        connection.credential_ref = f"credential://integration/{row.id}"
        connection.auth_status = "pending" if row.status == "verification_pending" else row.status
        row.state_hash = self._state_hash(row, connection)
        row.updated_at = datetime.utcnow()
        session.add(IntegrationCredentialAuditEvent(
            id=uuid4(), credential_id=row.id, connection_id=connection.id, revision=row.revision,
            event_type=event_type, actor_id=actor_id, actor_type="human", state_hash=row.state_hash,
            provider_code=row.verification_code,
        ))
        await session.flush()
        result = self._projection(row, connection)
        await session.commit()
        return result

    def _decrypt(self, row, connection):
        if integration_credential_ciphertext_key_id(row.encrypted_payload) != row.encryption_key_id:
            raise IntegrationCredentialCryptoError("integration_ciphertext_invalid")
        payload = decrypt_integration_credential(row.encrypted_payload, credential_id=row.id,
            connection_id=connection.id, key_provider=self.key_provider)
        if not hmac.compare_digest(hashlib.sha256(canonical_payload(payload)).hexdigest(), row.payload_digest):
            raise IntegrationCredentialCryptoError("integration_ciphertext_invalid")
        return self._payload(connection.provider_key, row.credential_kind, payload)

    async def upload_credential(self, session, actor, connection_id, *, credential_kind, payload, expected_revision=0):
        connection = await self._connection(session, connection_id, lock=True)
        actor_id = await self._authorize(session, actor, connection)
        row = await self._credential(session, connection)
        if row is None and connection.credential_ref:
            raise IntegrationCredentialError("integration_connection_already_bound", 409)
        payload = self._payload(connection.provider_key, credential_kind, payload)
        await self._fence(session, connection, row, expected_revision)
        is_new = row is None
        if is_new:
            row = IntegrationCredential(id=uuid4(), connection_id=connection.id,
                owner_user_id=connection.owner_user_id, project_id=connection.project_id,
                created_at=datetime.utcnow(), revision=0)
            session.add(row)
        encrypted = encrypt_integration_credential(payload, credential_id=row.id, connection_id=connection.id,
                                                    key_provider=self.key_provider)
        row.credential_kind = credential_kind
        row.encrypted_payload, row.encryption_key_id, row.payload_digest = encrypted.ciphertext, encrypted.key_id, encrypted.payload_digest
        row.revision += 1
        row.status, row.verification_code, row.verified_at = "verification_pending", "verification_pending", None
        row.capabilities_json = {key: "unknown" for key in PROVIDER_CAPABILITIES[connection.provider_key]}
        return await self._finish(session, connection, row, actor_id, "add" if is_new else "rotate")

    async def verify_credential(self, session, actor, connection_id, *, expected_revision):
        connection = await self._connection(session, connection_id, lock=True)
        actor_id = await self._authorize(session, actor, connection)
        row = await self._credential(session, connection)
        if row is None:
            raise IntegrationCredentialError("integration_credential_not_found", 404)
        if row.status == "disabled":
            raise IntegrationCredentialError("integration_credential_disabled", 409)
        await self._fence(session, connection, row, expected_revision)
        try:
            payload = self._decrypt(row, connection)
            try:
                async def verify(protected_metadata):
                    if protected_metadata != {"model": "gpt-realtime"}:
                        raise IntegrationCredentialError("integration_privacy_blocked")
                    return await self.verifier.verify(provider_key=connection.provider_key, credential=SecretCredentialHandle(payload))

                if connection.provider_key == "openai_realtime_sip":
                    from .outbound_privacy_service import EgressDescriptor, OutboundPrivacyGateway

                    project = await session.get(Project, connection.project_id) if connection.project_id else None
                    gateway = OutboundPrivacyGateway(self.config, user_id=str(actor_id),
                        project_metadata=getattr(project, "project_metadata", None))
                    # Only fixed model metadata is reviewable/auditable. The
                    # credential stays in the in-memory Authorization header
                    # inside the approved sender, as in LiveVoice/telephony.
                    result = await gateway.execute({"model": "gpt-realtime"}, provider="openai",
                        descriptor=EgressDescriptor(action="integration.credential.verify", transport="httpx",
                            destination="https://api.openai.com/v1/models/gpt-realtime", provider="openai",
                            model="gpt-realtime"), sender=verify, base_url="https://api.openai.com",
                        source_kind="integration_credential_verification", model="gpt-realtime")
                else:
                    result = await self.verifier.verify(provider_key=connection.provider_key, credential=SecretCredentialHandle(payload))
            except Exception:
                result = None
            trusted_caps = (self.verifier.verified_capabilities(connection.provider_key)
                            if type(self.verifier) is IntegrationCredentialProviderVerifier else None)
            result = _validated_result(result, connection.provider_key, payload, trusted_caps)
            row.status, row.verification_code, row.capabilities_json = result.status, result.provider_code, dict(result.capabilities)
        except IntegrationCredentialKeyUnavailable:
            row.status, row.verification_code = "key_unavailable", "key_unavailable"
            row.capabilities_json = {}
        except (IntegrationCredentialCryptoError, IntegrationCredentialError):
            row.status, row.verification_code = "invalid", "ciphertext_invalid"
            row.capabilities_json = {}
        row.revision += 1
        row.verified_at = datetime.utcnow() if row.status == "verified" else None
        return await self._finish(session, connection, row, actor_id, "verify")

    async def disable_credential(self, session, actor, connection_id, *, expected_revision):
        connection = await self._connection(session, connection_id, lock=True)
        actor_id = await self._authorize(session, actor, connection)
        row = await self._credential(session, connection)
        if row is None:
            raise IntegrationCredentialError("integration_credential_not_found", 404)
        await self._fence(session, connection, row, expected_revision)
        row.revision += 1
        row.status, row.verification_code, row.verified_at, row.capabilities_json = "disabled", "credential_disabled", None, {}
        return await self._finish(session, connection, row, actor_id, "disable")

    async def rekey_credential(self, session, actor, connection_id, *, expected_revision):
        """Admin-only maintenance: reencrypt with the active key, require reverify."""
        connection = await self._connection(session, connection_id, lock=True)
        actor_id = await self._authorize(session, actor, connection)
        row = await self._credential(session, connection)
        if row is None:
            raise IntegrationCredentialError("integration_credential_not_found", 404)
        payload = self._decrypt(row, connection)
        await self._fence(session, connection, row, expected_revision)
        encrypted = encrypt_integration_credential(payload, credential_id=row.id, connection_id=connection.id, key_provider=self.key_provider)
        row.encrypted_payload, row.encryption_key_id, row.payload_digest = encrypted.ciphertext, encrypted.key_id, encrypted.payload_digest
        row.revision += 1
        row.status, row.verification_code, row.verified_at, row.capabilities_json = "verification_pending", "verification_pending", None, {}
        return await self._finish(session, connection, row, actor_id, "rekey")

    def _readiness(self, row, connection, required_capabilities=()):
        status, reason, capabilities = "missing", "integration_credential_missing", {}
        if row:
            status, reason = row.status, "integration_credential_unverified"
            allowed = PROVIDER_CAPABILITIES.get(connection.provider_key, ())
            capabilities = {key: value for key, value in (row.capabilities_json or {}).items()
                            if key in allowed and type(value) is str and value in {"available", "unknown", "unsupported"}}
            if connection.provider_key not in PROVIDER_CAPABILITIES:
                reason = "integration_provider_unsupported"
            elif connection.credential_ref != f"credential://integration/{row.id}":
                reason = "integration_binding_changed"
            elif row.state_hash != self._state_hash(row, connection):
                reason = "integration_binding_changed"
            elif row.status == "verified" and connection.auth_status == "verified":
                try:
                    current_caps = (self.verifier.verified_capabilities(connection.provider_key)
                                    if type(self.verifier) is IntegrationCredentialProviderVerifier else None)
                    if current_caps is None or capabilities != current_caps:
                        reason = "integration_provider_unavailable"
                    elif any(key not in allowed or capabilities.get(key) != "available" for key in required_capabilities):
                        reason = "integration_capability_unavailable"
                    else:
                        self._decrypt(row, connection)
                        reason = "ready"
                except IntegrationCredentialKeyUnavailable:
                    status, reason, capabilities = "key_unavailable", "integration_key_unavailable", {}
                except (IntegrationCredentialCryptoError, IntegrationCredentialError):
                    status, reason, capabilities = "invalid", "integration_ciphertext_invalid", {}
        return {"ready": reason == "ready", "status": status, "reason_code": reason,
                "credential_id": str(row.id) if row else None, "revision": row.revision if row else None,
                "state_hash": row.state_hash if row else None, "connection_version": connection.version,
                "capabilities": capabilities}

    def _projection(self, row, connection):
        readiness = self._readiness(row, connection)
        safe = row.to_safe_dict() if row else None
        if safe:
            safe["capabilities"] = readiness["capabilities"]
            safe["status"] = readiness["status"]
        return {"credential": safe, "readiness": readiness}

    async def get_credential(self, session, actor, connection_id):
        connection = await self._connection(session, connection_id)
        await self._authorize(session, actor, connection)
        return self._projection(await self._credential(session, connection), connection)

    async def list_audit_events(self, session, actor, connection_id, *, limit=100):
        connection = await self._connection(session, connection_id)
        await self._authorize(session, actor, connection)
        rows = (await session.execute(select(IntegrationCredentialAuditEvent).where(
            IntegrationCredentialAuditEvent.connection_id == connection.id,
        ).order_by(IntegrationCredentialAuditEvent.created_at.desc(), IntegrationCredentialAuditEvent.id.desc())
          .limit(max(1, min(int(limit), 200))))).scalars().all()
        return {"items": [row.to_safe_dict() for row in rows]}

    async def _execution_scope(self, session, connection, owner_user_id, project_id):
        if (connection.owner_user_id != _uuid(owner_user_id)
                or connection.project_id != (_uuid(project_id) if project_id is not None else None)):
            raise IntegrationCredentialError("integration_scope_mismatch", 403)
        owner = await session.get(User, connection.owner_user_id)
        if owner is None or owner.is_active is not True:
            raise IntegrationCredentialError("integration_owner_unavailable", 403)
        if connection.project_id is not None:
            project = await session.get(Project, connection.project_id)
            if project is None or project.deleted_at is not None:
                raise IntegrationCredentialError("integration_project_not_found", 404)

    async def readiness_for_connection(self, session, connection_id, *, owner_user_id, project_id, required_capabilities=()):
        connection = await self._connection(session, connection_id)
        await self._execution_scope(session, connection, owner_user_id, project_id)
        return self._readiness(await self._credential(session, connection), connection, required_capabilities)

    async def resolve_for_execution(self, session, connection_id, *, owner_user_id, project_id,
                                    expected_revision, expected_state_hash, required_capabilities=()):
        connection = await self._connection(session, connection_id, lock=True)
        await self._execution_scope(session, connection, owner_user_id, project_id)
        row = await self._credential(session, connection)
        if (row is None or type(expected_revision) is not int or row.revision != expected_revision
                or type(expected_state_hash) is not str or not hmac.compare_digest(row.state_hash, expected_state_hash)):
            raise IntegrationCredentialError("integration_credential_stale", 409)
        readiness = self._readiness(row, connection, required_capabilities)
        if not readiness["ready"]:
            raise IntegrationCredentialError(readiness["reason_code"], 409)
        return SecretCredentialHandle(self._decrypt(row, connection))
