"""Secure Media Operations credential vault service.

This service is intentionally separate from the generic ExternalConnection
CRUD.  ExternalConnection remains a credential-less provider binding while
this service owns encrypted payloads, verification state and immutable audit.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import hmac
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    ExternalConnection,
    MediaPlatformCredential,
    MediaPlatformCredentialAuditEvent,
    PlatformAccount,
    PlatformAccountRevision,
)
from ..memory.models.media_operations_credentials import _safe_snapshot, media_credential_state_hash
from ..security.media_credential_crypto import (
    MediaCredentialCryptoError,
    MediaCredentialKeyUnavailable,
    canonical_payload,
    decrypt_media_credential,
    encrypt_media_credential,
    media_credential_ciphertext_key_id,
)
from .media_credential_package import (
    CredentialPackage,
    CredentialPackageError,
    parse_credential_package,
)
from .media_credential_provider_verifier import (
    CredentialVerificationResult,
    MediaCredentialProviderVerifier,
    has_trusted_identity_evidence,
)
from .media_provider_capability_registry import bound_credential_capabilities
from .media_operations_service import (
    MediaCredentialVaultUnavailableError,
    MediaOperationsAuthorizationError,
    MediaOperationsConflictError,
    MediaOperationsNotFoundError,
    MediaOperationsValidationError,
    _actor_field,
    _actor_id,
    _as_uuid,
    _idempotency_key,
    _required_text,
    sha256_json,
)


from .media_operations_setup_service import MediaOperationsSetupService, _normalize_account_revision


_STATUS_TO_CONNECTION = {
    "verification_pending": "pending",
    "verified": "verified",
    "invalid": "invalid",
    "unsupported": "unsupported",
    "disabled": "disabled",
    "key_unavailable": "key_unavailable",
}
_MEDIA_PLATFORMS = frozenset({"x", "pixiv", "patreon", "youtube", "instagram", "dlsite"})
_SAFE_PROVIDER_CODES = frozenset(
    {
        "credential_type_unsupported",
        "missing_access_token",
        "provider_unsupported",
        "provider_unavailable",
        "unauthorized",
        "provider_retryable",
        "provider_response_unknown",
        "identity_mismatch",
        "identity_match",
        "ciphertext_invalid",
        "credential_expired",
        "key_unavailable",
    }
)
_UNKNOWN_CAPABILITIES = {
    "identity": "unknown",
    "publish": "unknown",
    "media": "unknown",
    "analytics": "unknown",
}
_ALLOWED_VERIFICATION_STATUSES = frozenset(
    {
        "verification_pending",
        "verified",
        "invalid",
        "unsupported",
        "key_unavailable",
        "disabled",
    }
)


def _credential_payload_expired(payload: Mapping[str, Any]) -> bool:
    """Return whether a normalized stored payload is past its expiry.

    Upload validation already normalizes expiry values before encryption.  A
    verification retry must nevertheless re-check them because a credential
    can remain stored for hours or days after the initial upload.  The helper
    accepts the canonical API-token/OAuth shape and the canonical cookie
    shapes (X's ``expires`` map and Netscape's ``cookies`` list) without
    inspecting or logging any secret values.
    """

    now = int(time.time())

    def expired(value: Any) -> bool:
        if value in (None, ""):
            return False
        if isinstance(value, bool):
            return True
        try:
            return int(value) <= now
        except (TypeError, ValueError, OverflowError):
            return True

    if "expires_at" in payload and expired(payload.get("expires_at")):
        return True
    expires = payload.get("expires")
    if isinstance(expires, Mapping):
        if any(expired(value) for value in expires.values()):
            return True
    cookies = payload.get("cookies")
    if isinstance(cookies, list):
        for cookie in cookies:
            if isinstance(cookie, Mapping) and expired(cookie.get("expires")):
                return True
    return False


def _parse_package_for_command(
    raw: bytes | str,
    *,
    platform: str,
    connection_type: str,
) -> tuple[CredentialPackage, CredentialPackageError | None]:
    """Parse a package while preserving idempotent replay after expiry.

    A retry of an already-committed command must replay its safe result even
    when a short-lived token has expired since the original request.  We first
    perform the normal expiry check; only an ``expired`` failure is reparsed
    with expiry disabled so the canonical request hash can be compared to the
    immutable audit event.  New commands still fail closed with the original
    validation error.
    """

    try:
        return (
            parse_credential_package(
                raw,
                platform=platform,
                connection_type=connection_type,
            ),
            None,
        )
    except CredentialPackageError as exc:
        if exc.code != "expired":
            raise
        try:
            replay_package = parse_credential_package(
                raw,
                platform=platform,
                connection_type=connection_type,
                allow_expired=True,
            )
        except CredentialPackageError:
            raise exc
        return replay_package, exc


def _safe_actor_type(actor: Any) -> str:
    raw = str(_actor_field(actor, "actor_type", "unknown") or "unknown").strip().lower()
    role = str(_actor_field(actor, "role", "") or "").strip().lower()
    if raw == "human" and role == "admin":
        return "admin"
    if raw == "admin" and role == "admin":
        return "admin"
    return "human" if raw == "human" else "unknown"


def _safe_provider_code(value: Any) -> str | None:
    if value is None:
        return None
    try:
        rendered = str(value).strip().lower()
    except Exception:
        return "provider_response_unknown"
    return rendered if rendered in _SAFE_PROVIDER_CODES else "provider_response_unknown"


def _safe_verification_status(value: Any) -> str | None:
    """Normalize an extension verifier status without trusting its type."""

    try:
        rendered = str(value or "").strip().lower()
    except Exception:
        return None
    return rendered if rendered in _ALLOWED_VERIFICATION_STATUSES else None


def _safe_remote_account_ref(value: Any) -> str | None:
    """Accept only bounded scalar identity references from an adapter."""

    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int)):
        return None
    try:
        rendered = str(value).strip()
    except Exception:
        return None
    return rendered if rendered and len(rendered) <= 255 else None


def _normalize_verification_capabilities(value: Any) -> dict[str, str]:
    """Return a closed capability map; malformed extensions fail closed."""

    if not isinstance(value, Mapping):
        return dict(_UNKNOWN_CAPABILITIES)
    result: dict[str, str] = {}
    try:
        for key in ("identity", "publish", "media", "analytics"):
            raw = value.get(key, "unknown")
            if not isinstance(raw, str):
                return dict(_UNKNOWN_CAPABILITIES)
            rendered = raw.strip().lower()
            if rendered not in {"unknown", "available", "unsupported"}:
                return dict(_UNKNOWN_CAPABILITIES)
            result[key] = rendered
    except Exception:
        return dict(_UNKNOWN_CAPABILITIES)
    return result


def _normalize_verification_result(value: Any) -> CredentialVerificationResult:
    """Normalize provider extension output to a safe, closed result type.

    Custom adapters are untrusted extension boundaries.  Any malformed field
    (including hostile ``__str__``/mapping implementations) is converted to a
    retryable unknown result; a verified state is only retained when the
    built-in result's private evidence marker survives unchanged.
    """

    if isinstance(value, Mapping):
        try:
            status = _safe_verification_status(value.get("status")) or "verification_pending"
            capabilities = _normalize_verification_capabilities(value.get("capabilities"))
            remote_account_ref = _safe_remote_account_ref(value.get("remote_account_ref"))
            provider_code = _safe_provider_code(value.get("provider_code"))
        except Exception:
            return CredentialVerificationResult(
                "verification_pending",
                dict(_UNKNOWN_CAPABILITIES),
                provider_code="provider_response_unknown",
            )
        return CredentialVerificationResult(
            status,
            capabilities,
            remote_account_ref,
            provider_code,
        )
    if type(value) is not CredentialVerificationResult:
        return CredentialVerificationResult(
            "verification_pending",
            dict(_UNKNOWN_CAPABILITIES),
            provider_code="provider_response_unknown",
        )
    try:
        # ``dict`` subclasses can override iteration/getitem and raise after
        # this helper returns (for example at the ``result_caps = dict(...)``
        # boundary below).  Preserve the built-in evidence marker only when
        # the result carries an ordinary, immutable-shape dictionary.
        if type(value.capabilities) is not dict:
            raise ValueError
        status = _safe_verification_status(value.status)
        capabilities = _normalize_verification_capabilities(value.capabilities)
        remote_account_ref = _safe_remote_account_ref(value.remote_account_ref)
        provider_code = _safe_provider_code(value.provider_code)
        # Do not rebuild a valid built-in result: doing so would discard its
        # private provider-evidence marker.  Preserve only an exactly typed,
        # closed result; malformed adapters are downgraded below.
        if status is None:
            raise ValueError
        if capabilities != value.capabilities:
            raise ValueError
        if value.remote_account_ref is not None and remote_account_ref is None:
            raise ValueError
        if value.provider_code is not None and provider_code == "provider_response_unknown":
            raise ValueError
        return value
    except Exception:
        return CredentialVerificationResult(
            "verification_pending",
            dict(_UNKNOWN_CAPABILITIES),
            provider_code="provider_response_unknown",
        )


class MediaCredentialVaultService(MediaOperationsSetupService):
    """ACL-scoped, human-only encrypted credential lifecycle."""

    @staticmethod
    def _account_revision_content(revision: PlatformAccountRevision) -> dict[str, Any]:
        """Rebuild the canonical content object used by PlatformAccount hashes."""

        return {
            "display_name": revision.display_name,
            "publish_capability": revision.publish_capability,
            "media_capability": revision.media_capability,
            "analytics_capability": revision.analytics_capability,
            "credential_status": revision.credential_status,
            "remote_url": revision.remote_url,
            "locale": revision.locale,
            "timezone": revision.timezone,
            "supported_content_modes": list(revision.supported_content_modes_json or []),
            "disclosure_defaults": dict(revision.disclosure_defaults_json or {}),
            "rating_defaults": dict(revision.rating_defaults_json or {}),
            "adapter_ref": revision.adapter_ref,
        }

    @classmethod
    def _platform_account_create_hash(
        cls,
        account: PlatformAccount,
        revision_content: Mapping[str, Any],
    ) -> str:
        """Use the same immutable input shape as ``create_platform_account``."""

        return sha256_json(
            {
                "project_id": str(account.project_id) if account.project_id is not None else None,
                "persona_id": str(account.persona_id) if account.persona_id is not None else None,
                "connection_id": str(account.connection_id) if account.connection_id is not None else None,
                "account_type": account.account_type,
                "status": account.status,
                "platform": account.platform,
                "account_ref": account.account_ref,
                "revision": dict(revision_content),
            }
        )

    def _resolve(self, session: Any | None) -> Any:
        return self._resolve_session(session)

    async def _assert_human(self, actor: Any) -> UUID:
        actor_id = _actor_id(actor)
        is_agent = bool(_actor_field(actor, "is_agent", False))
        actor_type = str(_actor_field(actor, "actor_type", "unknown") or "unknown").strip().lower()
        role = str(_actor_field(actor, "role", "") or "").strip().lower()
        if is_agent or actor_type not in {"human", "admin"} or (actor_type == "admin" and role != "admin"):
            raise MediaOperationsAuthorizationError("credential commands require a human principal")
        return actor_id

    async def _account(self, session: Any, actor: Any, account_id: UUID | str, *, write: bool = False, lock: bool = False) -> PlatformAccount:
        account = await self._get_platform_account_row(session, account_id, for_update=lock)
        await self._assert_entity_access(session, actor, account, permission="write" if write else "read")
        return account

    async def _credential(self, session: Any, account_id: UUID, *, lock: bool = False) -> MediaPlatformCredential | None:
        statement = select(MediaPlatformCredential).where(MediaPlatformCredential.platform_account_id == account_id).limit(1)
        if lock:
            statement = statement.with_for_update()
        return await self._scalar(session, statement)

    async def _credential_by_id(self, session: Any, credential_id: UUID, *, lock: bool = False) -> MediaPlatformCredential | None:
        statement = select(MediaPlatformCredential).where(MediaPlatformCredential.id == credential_id).limit(1)
        if lock:
            statement = statement.with_for_update()
        return await self._scalar(session, statement)

    async def _assert_credential_binding(
        self,
        session: Any,
        account: PlatformAccount,
        credential: MediaPlatformCredential,
        *,
        lock_connection: bool = True,
    ) -> None:
        if (
            credential.platform_account_id != account.id
            or credential.owner_user_id != account.owner_user_id
            or credential.project_id != account.project_id
            or credential.connection_id != account.connection_id
        ):
            raise MediaOperationsConflictError("Platform credential binding is invalid")
        expected_state_hash = media_credential_state_hash(
            revision=int(credential.revision or 0),
            status=credential.status,
            connection_type=credential.connection_type,
            payload_digest=credential.payload_digest,
            capabilities=credential.capabilities if isinstance(credential.capabilities, dict) else {},
            verification_code=credential.verification_code,
            encryption_key_id=credential.encryption_key_id,
        )
        if not credential.state_hash or not hmac.compare_digest(str(credential.state_hash), expected_state_hash):
            raise MediaOperationsConflictError("Platform credential state integrity is invalid")
        try:
            embedded_key_id = media_credential_ciphertext_key_id(credential.encrypted_payload)
        except MediaCredentialCryptoError:
            raise MediaOperationsConflictError("Platform credential ciphertext metadata is invalid") from None
        if not hmac.compare_digest(embedded_key_id, str(credential.encryption_key_id or "")):
            raise MediaOperationsConflictError("Platform credential key metadata is inconsistent")
        statement = (
            select(ExternalConnection)
            .where(ExternalConnection.id == credential.connection_id)
            .limit(1)
        )
        if lock_connection:
            statement = statement.with_for_update()
        connection = await self._scalar(session, statement)
        if (
            connection is None
            or connection.owner_user_id != account.owner_user_id
            or connection.project_id != account.project_id
            or str(connection.provider_key).strip().lower() != str(account.platform).strip().lower()
            or str(connection.remote_account_ref or "") != str(account.account_ref or "")
            or connection.credential_ref != f"credential://media-platform/{credential.id}"
        ):
            raise MediaOperationsConflictError("External connection binding is invalid")

    async def _latest_revision(self, session: Any, account_id: UUID, *, lock: bool = False) -> PlatformAccountRevision:
        statement = (
            select(PlatformAccountRevision)
            .where(PlatformAccountRevision.platform_account_id == account_id)
            .order_by(PlatformAccountRevision.version.desc(), PlatformAccountRevision.id.desc())
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        revision = await self._scalar(session, statement)
        if revision is None:
            raise MediaOperationsConflictError("PlatformAccount revision history is incomplete")
        return revision

    def _scope_key(self, owner_user_id: UUID, project_id: UUID | None) -> str:
        return f"{owner_user_id}:{project_id or 'personal'}"

    async def _find_audit(self, session: Any, scope: str, key: str) -> MediaPlatformCredentialAuditEvent | None:
        return await self._scalar(
            session,
            select(MediaPlatformCredentialAuditEvent)
            .where(
                MediaPlatformCredentialAuditEvent.idempotency_scope == scope,
                MediaPlatformCredentialAuditEvent.idempotency_key == key,
            )
            .limit(1),
        )

    async def _replay_audit_result(
        self,
        session: Any,
        account: PlatformAccount,
        credential: MediaPlatformCredential,
        *,
        key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        """Return an idempotent result, or raise on hash reuse.

        This is deliberately called again after a verify network round-trip
        while the account row is locked.  A concurrent retry with the same
        key must replay the committed result instead of becoming a stale-write
        conflict merely because the first request advanced the revision.
        """

        existing = await self._find_audit(
            session,
            self._scope_key(account.owner_user_id, account.project_id),
            key,
        )
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise MediaOperationsConflictError(
                "idempotency key was already used with a different credential command"
            )
        await self._assert_credential_binding(session, account, credential)
        return self._audit_result(existing, credential, account)

    async def _append_audit(
        self,
        session: Any,
        *,
        credential: MediaPlatformCredential,
        account: PlatformAccount,
        actor: Any,
        event_type: str,
        request_hash: str,
        idempotency_key: str,
        snapshot: Mapping[str, Any],
    ) -> MediaPlatformCredentialAuditEvent:
        scope = self._scope_key(credential.owner_user_id, credential.project_id)
        previous = await self._scalar(
            session,
            select(MediaPlatformCredentialAuditEvent)
            .where(MediaPlatformCredentialAuditEvent.credential_id == credential.id)
            .order_by(MediaPlatformCredentialAuditEvent.sequence.desc(), MediaPlatformCredentialAuditEvent.id.desc())
            .limit(1),
        )
        previous_hash = previous.event_hash if previous is not None else None
        sequence = int(previous.sequence) + 1 if previous is not None else 1
        event_id = uuid4()
        created_at = datetime.utcnow()
        safe_snapshot = _safe_snapshot(dict(snapshot))
        event_hash = sha256_json(
            {
                "id": str(event_id),
                "credential_id": str(credential.id),
                "event_type": event_type,
                "sequence": sequence,
                "request_hash": request_hash,
                "idempotency_scope": scope,
                "idempotency_key": idempotency_key,
                "prev_event_hash": previous_hash,
                "snapshot": safe_snapshot,
                "created_at": created_at.isoformat(),
            }
        )
        event = MediaPlatformCredentialAuditEvent(
            id=event_id,
            credential_id=credential.id,
            platform_account_id=account.id,
            owner_user_id=credential.owner_user_id,
            project_id=credential.project_id,
            event_type=event_type,
            sequence=sequence,
            actor_id=_actor_id(actor),
            actor_type=_safe_actor_type(actor),
            snapshot_json=safe_snapshot,
            request_hash=request_hash,
            idempotency_scope=scope,
            idempotency_key=idempotency_key,
            prev_event_hash=previous_hash,
            event_hash=event_hash,
            created_at=created_at,
        )
        session.add(event)
        await self._flush_only(session)
        return event

    def _safe_result(self, credential: MediaPlatformCredential, account: PlatformAccount | None = None) -> dict[str, Any]:
        result = credential.to_safe_dict()
        if account is not None:
            result["platform"] = account.platform
            result["account_ref"] = account.account_ref
        return result

    def _audit_result(
        self,
        event: MediaPlatformCredentialAuditEvent,
        credential: MediaPlatformCredential,
        account: PlatformAccount | None = None,
    ) -> dict[str, Any]:
        """Return the original safe response persisted with an audit event.

        Idempotency is a response contract, not merely a duplicate-write
        guard.  Keep a compatibility fallback for audit rows created before
        response snapshots were added.
        """

        snapshot = event.snapshot_json
        if isinstance(snapshot, Mapping):
            saved = snapshot.get("result")
            if isinstance(saved, Mapping):
                return dict(_safe_snapshot(dict(saved)))
        return self._safe_result(credential, account)

    async def get_credential(self, session: Any | None = None, actor: Any | None = None, account_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError("actor and platform_account_id are required")
        account = await self._account(session, actor, account_id)
        credential = await self._credential(session, account.id)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        await self._assert_credential_binding(session, account, credential)
        return self._safe_result(credential, account)

    async def create_credential(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        account_id: UUID | str | None = None,
        *,
        package: bytes | str | None = None,
        credential_bytes: bytes | str | None = None,
        connection_type: str,
        idempotency_key: Any,
        request_hash_override: str | None = None,
        credential_id: UUID | None = None,
        audit_platform_account: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError("actor and platform_account_id are required")
        await self._assert_human(actor)
        account = await self._account(session, actor, account_id, write=True, lock=True)
        raw = package if package is not None else credential_bytes
        if raw is None:
            raise MediaOperationsValidationError("credential package is required")
        try:
            parsed, parse_error = _parse_package_for_command(
                raw,
                platform=account.platform,
                connection_type=connection_type,
            )
        except CredentialPackageError as exc:
            raise MediaOperationsValidationError(str(exc)) from None
        key = _idempotency_key(idempotency_key)
        scope = self._scope_key(account.owner_user_id, account.project_id)
        existing_audit = await self._find_audit(session, scope, key)
        request_hash = request_hash_override or sha256_json({"event": "add", "account_id": str(account.id), "connection_type": parsed.connection_type, "payload_digest": sha256_json(parsed.payload)})
        if existing_audit is not None:
            if existing_audit.request_hash != request_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different credential command")
            current = await self._credential(session, account.id)
            if current is None:
                raise MediaOperationsConflictError("credential idempotency record is incomplete")
            await self._assert_credential_binding(session, account, current)
            return self._audit_result(existing_audit, current, account)
        if parse_error is not None:
            raise MediaOperationsValidationError(str(parse_error)) from None
        current = await self._credential(session, account.id)
        if current is not None:
            await self._assert_credential_binding(session, account, current)
            raise MediaOperationsConflictError("platform credential already exists")

        credential_id = credential_id or uuid4()
        try:
            encrypted = encrypt_media_credential(parsed.payload, credential_id=credential_id, platform_account_id=account.id)
        except MediaCredentialKeyUnavailable:
            raise MediaCredentialVaultUnavailableError("credential encryption key is unavailable") from None
        except MediaCredentialCryptoError:
            raise MediaOperationsValidationError("credential package cannot be encrypted") from None
        connection: ExternalConnection | None = None
        if account.connection_id is not None:
            connection = await self._scalar(session, select(ExternalConnection).where(ExternalConnection.id == account.connection_id).with_for_update().limit(1))
            if connection is None:
                raise MediaOperationsConflictError("PlatformAccount connection is missing")
            if (
                connection.owner_user_id != account.owner_user_id
                or connection.project_id != account.project_id
                or str(connection.provider_key).strip().lower() != str(account.platform).strip().lower()
                or str(connection.remote_account_ref or "") != str(account.account_ref or "")
            ):
                raise MediaOperationsConflictError("PlatformAccount connection scope is invalid")
            expected_ref = f"credential://media-platform/{credential_id}"
            if connection.credential_ref not in (None, expected_ref):
                raise MediaOperationsConflictError("PlatformAccount connection already owns another credential reference")
            another_account = await self._scalar(
                session,
                select(PlatformAccount)
                .where(
                    PlatformAccount.connection_id == connection.id,
                    PlatformAccount.id != account.id,
                )
                .limit(1),
            )
            if another_account is not None:
                raise MediaOperationsConflictError("External connection is already bound to another PlatformAccount")
        else:
            # Resolve the immutable revision before mutating the account.  A
            # legacy row without revision history is not attachable and must
            # fail before any new connection can be flushed.
            await self._latest_revision(session, account.id)
            connection = ExternalConnection(
                id=uuid4(), owner_user_id=account.owner_user_id, project_id=account.project_id,
                provider_key=account.platform, display_name=account.account_ref,
                remote_account_ref=account.account_ref, credential_ref=f"credential://media-platform/{credential_id}",
                auth_status="pending", version=1, metadata_json={},
            )
            session.add(connection)
            await self._flush_only(session)
            account.connection_id = connection.id
        connection.credential_ref = f"credential://media-platform/{credential_id}"
        connection.auth_status = "pending"
        connection.version = int(connection.version or 1) + 1
        credential = MediaPlatformCredential(
            id=credential_id, platform_account_id=account.id, connection_id=connection.id,
            owner_user_id=account.owner_user_id, project_id=account.project_id,
            connection_type=parsed.connection_type, encrypted_payload=encrypted.ciphertext,
            encryption_key_id=encrypted.key_id, payload_digest=encrypted.payload_digest,
            revision=1, status="verification_pending",
            capabilities={"identity": "unknown", "publish": "unknown", "media": "unknown", "analytics": "unknown"},
            state_hash=media_credential_state_hash(revision=1, status="verification_pending", connection_type=parsed.connection_type, payload_digest=encrypted.payload_digest, capabilities={"identity": "unknown", "publish": "unknown", "media": "unknown", "analytics": "unknown"}, verification_code=None, encryption_key_id=encrypted.key_id),
            created_by=_actor_id(actor), created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        )
        session.add(credential)
        try:
            await self._flush_only(session)
            safe_credential_result = self._safe_result(credential, account)
            audit_result: Mapping[str, Any] = safe_credential_result
            if audit_platform_account is not None:
                # The add-character command returns a composite response.  A
                # retry must replay that exact safe projection rather than
                # rebuilding the platform account after later revisions have
                # been appended.
                audit_result = {
                    "platform_account": _safe_snapshot(dict(audit_platform_account)),
                    "credential": safe_credential_result,
                }
            await self._append_audit(
                session,
                credential=credential,
                account=account,
                actor=actor,
                event_type="add",
                request_hash=request_hash,
                idempotency_key=key,
                snapshot={
                    "status": credential.status,
                    "revision": credential.revision,
                    "capabilities": credential.capabilities,
                    "connection_type": credential.connection_type,
                    "result": audit_result,
                },
            )
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            raise MediaOperationsConflictError("credential command conflicted with an existing record") from exc
        return safe_credential_result

    async def add_platform_connection(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        character_id: UUID | str,
        platform: str | None = None,
        account_ref: str,
        display_name: str,
        connection_type: str,
        package: bytes | str,
        idempotency_key: Any,
        project_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a Character-owned PlatformAccount + vault row."""

        session = self._resolve(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        await self._assert_human(actor)
        persona = await self._get_persona_row(session, character_id, for_update=True)
        await self._assert_entity_access(session, actor, persona, permission="write")
        platform_value = _required_text(platform or "", "platform", 16).lower()
        if platform_value not in _MEDIA_PLATFORMS:
            raise MediaOperationsValidationError("platform is unsupported")
        account_ref_value = _required_text(account_ref, "account_ref", 255)
        name = _required_text(display_name, "display_name", 255)
        if project_id is not None and _as_uuid(project_id, "project_id", required=False) != persona.project_id:
            raise MediaOperationsValidationError("character and project scopes must match")
        key = _idempotency_key(idempotency_key)
        try:
            parsed_package, parse_error = _parse_package_for_command(
                package,
                platform=platform_value,
                connection_type=connection_type,
            )
        except CredentialPackageError as exc:
            raise MediaOperationsValidationError(str(exc)) from None
        account_id = uuid4()
        connection_id = uuid4()
        credential_id = uuid4()
        actor_uuid = _actor_id(actor)
        # Keep the stable owner boundary of the Character.  An administrator
        # may perform the write, but must not silently re-home a personal
        # account under the administrator's identity.
        account_owner_id = (
            persona.owner_user_id
            if persona.project_id is None
            else actor_uuid
        )
        # Character identity fields form the stable idempotency request hash;
        # the generated account UUID must not make a retried upload appear to
        # be a different command.
        request_hash = sha256_json({"event": "add", "character_id": str(persona.id), "platform": platform_value, "account_ref": account_ref_value, "display_name": name, "connection_type": parsed_package.connection_type, "payload_digest": sha256_json(parsed_package.payload)})
        scope = self._scope_key(account_owner_id, persona.project_id)
        existing_audit = await self._find_audit(session, scope, key)
        if existing_audit is not None:
            if existing_audit.request_hash != request_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different credential command")
            account_replay = await self._find_platform_account_by_idempotency(session, owner_user_id=account_owner_id, project_id=persona.project_id, idempotency_key=key)
            credential_replay = await self._credential(session, account_replay.id) if account_replay is not None else None
            if account_replay is None or credential_replay is None:
                raise MediaOperationsConflictError("credential idempotency record is incomplete")
            await self._assert_credential_binding(session, account_replay, credential_replay)
            saved_result = self._audit_result(existing_audit, credential_replay, account_replay)
            if (
                isinstance(saved_result, Mapping)
                and isinstance(saved_result.get("platform_account"), Mapping)
                and isinstance(saved_result.get("credential"), Mapping)
            ):
                return dict(_safe_snapshot(dict(saved_result)))
            return {
                "platform_account": await self._platform_detail(session, account_replay),
                "credential": saved_result,
            }
        if parse_error is not None:
            raise MediaOperationsValidationError(str(parse_error)) from None
        existing = await self._find_platform_account_identity(session, owner_user_id=account_owner_id, project_id=persona.project_id, platform=platform_value, account_ref=account_ref_value)
        if existing is not None:
            raise MediaOperationsConflictError("PlatformAccount identity already exists")
        # Construct the identity and initial unknown revision without invoking
        # the legacy service's commit boundary, preserving atomicity.
        account = PlatformAccount(
            id=account_id, owner_user_id=account_owner_id, project_id=persona.project_id, persona_id=persona.id,
            account_type="profile", remote_url=None, status="active", platform=platform_value,
            connection_id=connection_id,
            account_ref=account_ref_value,
            idempotency_key=key, created_by=_actor_id(actor), created_at=datetime.utcnow(),
        )
        connection = ExternalConnection(
            id=connection_id,
            owner_user_id=account_owner_id,
            project_id=persona.project_id,
            provider_key=platform_value,
            display_name=name,
            remote_account_ref=account_ref_value,
            credential_ref=None,
            auth_status="pending",
            version=1,
            metadata_json={},
        )
        content = _normalize_account_revision(display_name=name, publish_capability="unknown", media_capability="unknown", analytics_capability="unknown", credential_status="unknown")
        account.create_hash = self._platform_account_create_hash(account, content)
        revision = self._build_platform_revision(account=account, version=1, content=content, created_by=_actor_id(actor), idempotency_key=None)
        session.add(connection)
        session.add(account)
        session.add(revision)
        await self._flush_only(session)
        platform_account_result = await self._platform_detail(session, account)
        try:
            result = await self.create_credential(
                session,
                actor,
                account.id,
                package=package,
                connection_type=connection_type,
                idempotency_key=idempotency_key,
                request_hash_override=request_hash,
                credential_id=credential_id,
                audit_platform_account=platform_account_result,
            )
        except Exception:
            # Account/connection/revision rows were flushed above so the
            # credential service can resolve the account.  Roll back the
            # complete command when encryption or the second package parse
            # fails; otherwise a caller catching the error could commit an
            # orphan PlatformAccount without its vault credential.
            await self._rollback(session)
            raise
        return {"platform_account": platform_account_result, "credential": result}

    async def _append_account_credential_state(self, session: Any, actor: Any, account: PlatformAccount, credential: MediaPlatformCredential, *, credential_status: str, capabilities: Mapping[str, str]) -> None:
        latest = await self._latest_revision(session, account.id, lock=True)
        content = {
            "display_name": latest.display_name,
            "publish_capability": str(capabilities.get("publish", latest.publish_capability)),
            "media_capability": str(capabilities.get("media", latest.media_capability)),
            "analytics_capability": str(capabilities.get("analytics", latest.analytics_capability)),
            "credential_status": credential_status,
            "remote_url": latest.remote_url,
            "locale": latest.locale,
            "timezone": latest.timezone,
            "supported_content_modes": list(latest.supported_content_modes_json or []),
            "disclosure_defaults": dict(latest.disclosure_defaults_json or {}),
            "rating_defaults": dict(latest.rating_defaults_json or {}),
            "adapter_ref": latest.adapter_ref,
        }
        new_revision = self._build_platform_revision(account=account, version=int(latest.version) + 1, content=content, created_by=_actor_id(actor), idempotency_key=None)
        session.add(new_revision)

    async def _mutate_state(self, session: Any, actor: Any, account: PlatformAccount, credential: MediaPlatformCredential, *, event_type: str, request_hash: str, key: str, status: str, capabilities: Mapping[str, str] | None = None, encrypted: Any = None, provider_code: str | None = None) -> dict[str, Any]:
        now = datetime.utcnow()
        credential.revision = int(credential.revision or 1) + 1
        credential.status = status
        credential.updated_at = now
        # Timestamp fields describe the current state transition.  Clear
        # stale values so a disabled/invalid credential cannot look verified.
        credential.verification_started_at = None
        credential.verification_completed_at = None
        credential.disabled_at = None
        if status == "verified":
            credential.last_verified_at = now
            credential.verification_completed_at = now
        elif status == "verification_pending":
            credential.verification_started_at = now
        elif status == "disabled":
            credential.disabled_at = now
        credential.verification_code = provider_code
        if encrypted is not None:
            credential.encrypted_payload = encrypted.ciphertext
            credential.encryption_key_id = encrypted.key_id
            credential.payload_digest = encrypted.payload_digest
        if capabilities is not None:
            credential.capabilities = dict(capabilities)
        credential.state_hash = media_credential_state_hash(
            revision=int(credential.revision),
            status=credential.status,
            connection_type=credential.connection_type,
            payload_digest=credential.payload_digest,
            capabilities=credential.capabilities,
            verification_code=credential.verification_code,
            encryption_key_id=credential.encryption_key_id,
        )
        connection = await self._scalar(session, select(ExternalConnection).where(ExternalConnection.id == credential.connection_id).with_for_update().limit(1))
        if connection is None:
            raise MediaOperationsConflictError("External connection binding is invalid")
        if (
            connection.owner_user_id != account.owner_user_id
            or connection.project_id != account.project_id
            or str(connection.provider_key).strip().lower() != str(account.platform).strip().lower()
            or str(connection.remote_account_ref or "") != str(account.account_ref or "")
            or connection.credential_ref != f"credential://media-platform/{credential.id}"
        ):
            raise MediaOperationsConflictError("External connection binding is invalid")
        connection.auth_status = _STATUS_TO_CONNECTION.get(status, status)
        connection.version = int(connection.version or 1) + 1
        credential_status = "configured" if status == "verified" else ("invalid" if status == "invalid" else ("not_configured" if status == "disabled" else "unknown"))
        await self._append_account_credential_state(session, actor, account, credential, credential_status=credential_status, capabilities=credential.capabilities or {})
        await self._append_audit(
            session,
            credential=credential,
            account=account,
            actor=actor,
            event_type=event_type,
            request_hash=request_hash,
            idempotency_key=key,
            snapshot={
                "status": status,
                "revision": credential.revision,
                "capabilities": credential.capabilities,
                "provider_code": provider_code,
                "result": self._safe_result(credential, account),
            },
        )
        await self._flush_commit(session)
        return self._safe_result(credential, account)

    async def rotate_credential(self, session: Any | None = None, actor: Any | None = None, account_id: UUID | str | None = None, *, expected_revision: int, package: bytes | str, connection_type: str, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError("actor and platform_account_id are required")
        await self._assert_human(actor)
        account = await self._account(session, actor, account_id, write=True, lock=True)
        credential = await self._credential(session, account.id, lock=True)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        await self._assert_credential_binding(session, account, credential)
        try:
            parsed, parse_error = _parse_package_for_command(
                package,
                platform=account.platform,
                connection_type=connection_type,
            )
        except CredentialPackageError as exc:
            raise MediaOperationsValidationError(str(exc)) from None
        key = _idempotency_key(idempotency_key)
        request_hash = sha256_json({"event": "rotate", "account_id": str(account.id), "expected_revision": int(expected_revision), "connection_type": parsed.connection_type, "payload_digest": sha256_json(parsed.payload)})
        existing = await self._find_audit(session, self._scope_key(account.owner_user_id, account.project_id), key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different credential command")
            return self._audit_result(existing, credential, account)
        if parse_error is not None:
            raise MediaOperationsValidationError(str(parse_error)) from None
        if int(expected_revision) != int(credential.revision):
            raise MediaOperationsConflictError("stale media credential revision")
        try:
            encrypted = encrypt_media_credential(parsed.payload, credential_id=credential.id, platform_account_id=account.id)
        except MediaCredentialKeyUnavailable:
            raise MediaCredentialVaultUnavailableError("credential encryption key is unavailable") from None
        except MediaCredentialCryptoError:
            raise MediaOperationsValidationError("credential package cannot be encrypted") from None
        credential.connection_type = parsed.connection_type
        return await self._mutate_state(session, actor, account, credential, event_type="rotate", request_hash=request_hash, key=key, status="verification_pending", capabilities={"identity": "unknown", "publish": "unknown", "media": "unknown", "analytics": "unknown"}, encrypted=encrypted)

    async def verify_credential(self, session: Any | None = None, actor: Any | None = None, account_id: UUID | str | None = None, *, expected_revision: int, idempotency_key: Any, verifier: MediaCredentialProviderVerifier | None = None) -> dict[str, Any]:
        session = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError("actor and platform_account_id are required")
        await self._assert_human(actor)
        # Do not hold a database row lock across the provider network call.
        # Locking is reacquired immediately before applying the observed
        # result, with the expected revision checked again below.
        account = await self._account(session, actor, account_id, write=True, lock=False)
        credential = await self._credential(session, account.id, lock=False)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        await self._assert_credential_binding(
            session,
            account,
            credential,
            lock_connection=False,
        )
        key = _idempotency_key(idempotency_key)
        request_hash = sha256_json({"event": "verify", "account_id": str(account.id), "expected_revision": int(expected_revision)})
        existing = await self._find_audit(session, self._scope_key(account.owner_user_id, account.project_id), key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different credential command")
            return self._audit_result(existing, credential, account)
        if credential.status == "disabled":
            raise MediaOperationsConflictError(
                "disabled credential must be rotated before verification"
            )
        if int(expected_revision) != int(credential.revision):
            raise MediaOperationsConflictError("stale media credential revision")
        try:
            payload = decrypt_media_credential(credential.encrypted_payload, credential_id=credential.id, platform_account_id=account.id)
            if not isinstance(payload, dict):
                raise MediaCredentialCryptoError("credential payload is not an object")
            payload_digest = hashlib.sha256(canonical_payload(payload)).hexdigest()
            if not hmac.compare_digest(payload_digest, str(credential.payload_digest or "")):
                raise MediaCredentialCryptoError("credential payload integrity is invalid")
            if _credential_payload_expired(payload):
                raise CredentialPackageError(
                    "expired",
                    "credential payload is expired",
                )
            package = CredentialPackage(account.platform, credential.connection_type, payload)
        except MediaCredentialKeyUnavailable:
            account = await self._account(session, actor, account_id, write=True, lock=True)
            credential = await self._credential(session, account.id, lock=True)
            if credential is None:
                raise MediaOperationsNotFoundError("platform credential not found")
            replay = await self._replay_audit_result(
                session,
                account,
                credential,
                key=key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            if int(expected_revision) != int(credential.revision):
                raise MediaOperationsConflictError("stale media credential revision")
            await self._assert_credential_binding(session, account, credential)
            return await self._mutate_state(session, actor, account, credential, event_type="verify", request_hash=request_hash, key=key, status="key_unavailable", provider_code="key_unavailable")
        except CredentialPackageError as exc:
            account = await self._account(session, actor, account_id, write=True, lock=True)
            credential = await self._credential(session, account.id, lock=True)
            if credential is None:
                raise MediaOperationsNotFoundError("platform credential not found")
            replay = await self._replay_audit_result(
                session,
                account,
                credential,
                key=key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            if int(expected_revision) != int(credential.revision):
                raise MediaOperationsConflictError("stale media credential revision")
            await self._assert_credential_binding(session, account, credential)
            provider_code = "credential_expired" if exc.code == "expired" else "ciphertext_invalid"
            return await self._mutate_state(
                session,
                actor,
                account,
                credential,
                event_type="verify",
                request_hash=request_hash,
                key=key,
                status="invalid",
                provider_code=provider_code,
            )
        except MediaCredentialCryptoError:
            account = await self._account(session, actor, account_id, write=True, lock=True)
            credential = await self._credential(session, account.id, lock=True)
            if credential is None:
                raise MediaOperationsNotFoundError("platform credential not found")
            replay = await self._replay_audit_result(
                session,
                account,
                credential,
                key=key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            if int(expected_revision) != int(credential.revision):
                raise MediaOperationsConflictError("stale media credential revision")
            await self._assert_credential_binding(session, account, credential)
            return await self._mutate_state(session, actor, account, credential, event_type="verify", request_hash=request_hash, key=key, status="invalid", provider_code="ciphertext_invalid")
        try:
            result: CredentialVerificationResult = await (verifier or MediaCredentialProviderVerifier()).verify(package, account_ref=account.account_ref)
        except Exception:
            # Provider adapters must never let request/body details cross the
            # credential boundary.  Treat an unexpected adapter failure as a
            # retryable verification result.
            result = CredentialVerificationResult(
                "verification_pending",
                dict(_UNKNOWN_CAPABILITIES),
                provider_code="provider_unavailable",
            )
        result = _normalize_verification_result(result)
        result_caps = bound_credential_capabilities(
            account.platform,
            result.capabilities,
            credential_status=result.status,
        )
        # ``CredentialVerificationResult`` is an extension boundary: a
        # custom adapter must not be able to manufacture a verified state by
        # returning ``status='verified'`` alone.  Only the provider's stable
        # identity evidence (the canonical capability and code emitted by
        # the built-in verifier) may establish a verified credential.  Any
        # other claimed success is downgraded to a retryable, fail-closed
        # result so publication can never proceed on synthetic success.
        safe_provider_code = _safe_provider_code(result.provider_code)
        if result.status == "verified" and (
            not has_trusted_identity_evidence(result)
            or result_caps.get("identity") != "available"
            or safe_provider_code != "identity_match"
        ):
            result = CredentialVerificationResult(
                "verification_pending",
                dict(_UNKNOWN_CAPABILITIES),
                provider_code="provider_response_unknown",
            )
            result_caps = dict(_UNKNOWN_CAPABILITIES)
        account = await self._account(session, actor, account_id, write=True, lock=True)
        credential = await self._credential(session, account.id, lock=True)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        replay = await self._replay_audit_result(
            session,
            account,
            credential,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        if int(expected_revision) != int(credential.revision):
            raise MediaOperationsConflictError("stale media credential revision")
        await self._assert_credential_binding(session, account, credential)
        return await self._mutate_state(session, actor, account, credential, event_type="verify", request_hash=request_hash, key=key, status=result.status, capabilities=result_caps, provider_code=safe_provider_code)

    async def disable_credential(self, session: Any | None = None, actor: Any | None = None, account_id: UUID | str | None = None, *, expected_revision: int, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError("actor and platform_account_id are required")
        await self._assert_human(actor)
        account = await self._account(session, actor, account_id, write=True, lock=True)
        credential = await self._credential(session, account.id, lock=True)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        await self._assert_credential_binding(session, account, credential)
        key = _idempotency_key(idempotency_key)
        request_hash = sha256_json({"event": "disable", "account_id": str(account.id), "expected_revision": int(expected_revision)})
        existing = await self._find_audit(session, self._scope_key(account.owner_user_id, account.project_id), key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different credential command")
            return self._audit_result(existing, credential, account)
        if int(expected_revision) != int(credential.revision):
            raise MediaOperationsConflictError("stale media credential revision")
        return await self._mutate_state(session, actor, account, credential, event_type="disable", request_hash=request_hash, key=key, status="disabled", capabilities={"identity": "unknown", "publish": "unknown", "media": "unknown", "analytics": "unknown"})

    async def list_credential_audit(self, session: Any | None = None, actor: Any | None = None, account_id: UUID | str | None = None, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError("actor and platform_account_id are required")
        account = await self._account(session, actor, account_id, write=False)
        credential = await self._credential(session, account.id)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        await self._assert_credential_binding(session, account, credential)
        try:
            limit_value, offset_value = int(limit), int(offset)
        except (TypeError, ValueError):
            raise MediaOperationsValidationError("limit and offset must be integers") from None
        if limit_value < 1 or limit_value > 100 or offset_value < 0:
            raise MediaOperationsValidationError("limit or offset is out of range")
        rows = await self._scalars(session, select(MediaPlatformCredentialAuditEvent).where(MediaPlatformCredentialAuditEvent.credential_id == credential.id).order_by(MediaPlatformCredentialAuditEvent.sequence.desc(), MediaPlatformCredentialAuditEvent.id.desc()).limit(limit_value).offset(offset_value))
        return [row.to_safe_dict() for row in rows]

    # Explicit aliases keep naming stable for clients/tests that use the
    # command-oriented terminology from the Director design.
    create_platform_credential = create_credential
    get_platform_credential = get_credential
    rotate_platform_credential = rotate_credential
    verify_platform_credential = verify_credential
    disable_platform_credential = disable_credential
    list_platform_credential_audit = list_credential_audit


__all__ = ["MediaCredentialVaultService"]
