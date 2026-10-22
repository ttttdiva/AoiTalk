"""Secret-free provider readiness snapshots for audited MediaOps writes.

A checked-in capability policy only states that an adapter contract has been
reviewed.  A remote write is enabled only when this service binds that policy
to the current PlatformAccount revision, the exact verified credential state,
and the OAuth scopes that were actually granted.  Snapshots are append-only and
contain no token, cookie, provider response, request header, or filesystem path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..memory.models import MediaProviderCapabilitySnapshot
from .media_credential_vault_service import MediaCredentialVaultService
from .media_operations_service import (
    MediaOperationsConflictError,
    MediaOperationsNotFoundError,
    MediaOperationsValidationError,
)
from .media_provider_capability_registry import (
    CAPABILITY_OPERATIONS,
    CapabilityStatus,
    effective_operation_status,
    get_provider_capability,
)


_X_TEXT_SCOPES = frozenset({"tweet.read", "tweet.write", "users.read"})
_X_MEDIA_SCOPES = frozenset({"tweet.read", "tweet.write", "users.read", "media.write"})
_X_IDENTITY_SCOPES = frozenset({"users.read"})
_YOUTUBE_UPLOAD_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtube.upload",
    }
)


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _granted_scopes(payload: Mapping[str, Any]) -> list[str]:
    value = payload.get("scope")
    if value is None:
        value = payload.get("scopes")
    raw: Sequence[Any]
    if isinstance(value, str):
        raw = value.replace(",", " ").split()
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        raw = value
    else:
        raw = ()
    return sorted(
        {
            str(item).strip()
            for item in raw
            if isinstance(item, str)
            and str(item).strip()
            and len(str(item).strip()) <= 256
        }
    )[:64]


def _scope_allows(platform: str, operation: str, scopes: set[str]) -> bool:
    if platform == "x":
        if operation == "identity":
            return _X_IDENTITY_SCOPES.issubset(scopes)
        if operation == "text":
            return _X_TEXT_SCOPES.issubset(scopes)
        if operation in {"image", "video"}:
            return _X_MEDIA_SCOPES.issubset(scopes)
        if operation in {"oauth", "token", "refresh", "revoke"}:
            return bool(scopes)
        return True
    if platform == "youtube":
        read_scopes = _YOUTUBE_UPLOAD_SCOPES - {"https://www.googleapis.com/auth/youtube.upload"}
        read_scopes = read_scopes | {"https://www.googleapis.com/auth/youtube.readonly"}
        if operation == "identity":
            return bool(read_scopes.intersection(scopes))
        if operation in {"image", "video", "schedule"}:
            return bool(read_scopes.intersection(scopes)) and bool(_YOUTUBE_UPLOAD_SCOPES.intersection(scopes))
        if operation in {"oauth", "token", "refresh", "revoke"}:
            return bool(scopes)
        return True
    return False


def _account_supports_operation(revision: Any, operation: str) -> bool:
    publish = str(getattr(revision, "publish_capability", "") or "").lower()
    media = str(getattr(revision, "media_capability", "") or "").lower()
    credential = str(getattr(revision, "credential_status", "") or "").lower()
    if credential != "configured":
        return False
    if operation == "text":
        return publish == "available"
    if operation in {"image", "video", "schedule"}:
        return publish == "available" and media == "available"
    return True


def _coarse_capability_allows(credential: Any, operation: str) -> bool:
    capabilities = (
        credential.capabilities if isinstance(credential.capabilities, Mapping) else {}
    )
    if operation == "identity":
        return str(capabilities.get("identity") or "").lower() == "available"
    if operation == "text":
        return str(capabilities.get("publish") or "").lower() == "available"
    if operation in {"image", "video", "schedule"}:
        return (
            str(capabilities.get("publish") or "").lower() == "available"
            and str(capabilities.get("media") or "").lower() == "available"
        )
    return True


class MediaProviderReadinessService(MediaCredentialVaultService):
    """Create/list exact provider capability observations."""

    async def refresh(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        account_id: Any | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError(
                "actor and platform_account_id are required"
            )
        actor_id = await self._assert_human(actor)
        account = await self._account(
            resolved, actor, account_id, write=True, lock=False
        )
        credential = await self._credential(resolved, account.id, lock=False)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        await self._assert_credential_binding(
            resolved, account, credential, lock_connection=False
        )
        revision = await self._latest_revision(resolved, account.id, lock=False)
        platform = str(account.platform or "").strip().lower()
        policy = get_provider_capability(platform)
        if policy is None:
            raise MediaOperationsConflictError(
                "provider capability policy is unavailable"
            )

        granted_scopes: list[str] = []
        if str(credential.status or "").strip().lower() == "verified":
            async with self.execution_handle(
                resolved,
                platform_account_id=account.id,
                expected_state_hash=str(credential.state_hash or ""),
                expected_platform=platform,
            ) as handle:
                granted_scopes = _granted_scopes(handle.payload)
        scope_set = set(granted_scopes)
        observation = {
            "status": str(credential.status or "").strip().lower(),
            **(
                dict(credential.capabilities)
                if isinstance(credential.capabilities, Mapping)
                else {}
            ),
        }
        account_active = str(account.status or "").strip().lower() == "active"
        credential_verified = observation["status"] == "verified"
        account_type = str(account.account_type or "unknown").strip().lower()
        observed_at = datetime.utcnow()
        rows: list[MediaProviderCapabilitySnapshot] = []

        for operation in CAPABILITY_OPERATIONS:
            status = effective_operation_status(
                platform, operation, observation=observation
            )
            if status is CapabilityStatus.AUTOMATABLE:
                if not (
                    account_active
                    and credential_verified
                    and _account_supports_operation(revision, operation)
                    and _coarse_capability_allows(credential, operation)
                    and _scope_allows(platform, operation, scope_set)
                ):
                    status = CapabilityStatus.UNVERIFIED
            eligibility = (
                "eligible"
                if status is CapabilityStatus.AUTOMATABLE
                else "ineligible"
                if not account_active
                else "unverified"
            )
            safe_binding = {
                "owner_user_id": str(account.owner_user_id),
                "project_id": str(account.project_id) if account.project_id else None,
                "platform_account_id": str(account.id),
                "account_revision_id": str(revision.id),
                "account_revision": int(revision.version or 0),
                "credential_id": str(credential.id),
                "credential_revision": int(credential.revision or 0),
                "credential_state_hash": str(credential.state_hash or ""),
                "credential_scope": str(credential.connection_type or "unknown"),
                "account_type": account_type,
                "provider": platform,
                "operation": operation,
                "status": status.value,
                "registry_version": str(policy.registry_version),
                "granted_scopes": granted_scopes,
                "account_eligibility": eligibility,
                "adapter_key": str(policy.adapter_key or policy.adapter_ref),
                "adapter_version": str(policy.adapter_version),
            }
            snapshot_hash = _canonical_hash(safe_binding)
            existing = await self._scalar(
                resolved,
                select(MediaProviderCapabilitySnapshot)
                .where(
                    MediaProviderCapabilitySnapshot.platform_account_id
                    == account.id,
                    MediaProviderCapabilitySnapshot.operation == operation,
                    MediaProviderCapabilitySnapshot.credential_revision
                    == int(credential.revision or 0),
                    MediaProviderCapabilitySnapshot.account_revision
                    == int(revision.version or 0),
                    MediaProviderCapabilitySnapshot.snapshot_hash
                    == snapshot_hash,
                )
                .limit(1),
            )
            if existing is not None:
                rows.append(existing)
                continue
            idempotency_key = (
                f"provider-readiness:{account.id}:{revision.version}:"
                f"{credential.revision}:{operation}:{snapshot_hash[:16]}"
            )
            row = MediaProviderCapabilitySnapshot(
                owner_user_id=account.owner_user_id,
                project_id=account.project_id,
                platform_account_id=account.id,
                account_revision_id=revision.id,
                credential_id=credential.id,
                credential_state_hash=str(credential.state_hash or ""),
                credential_revision=int(credential.revision or 0),
                account_revision=int(revision.version or 0),
                credential_scope=str(credential.connection_type or "unknown"),
                account_type=account_type,
                provider=platform,
                operation=operation,
                status=status.value,
                registry_version=str(policy.registry_version),
                granted_scopes=granted_scopes,
                account_eligibility=eligibility,
                adapter_key=str(policy.adapter_key or policy.adapter_ref),
                adapter_version=str(policy.adapter_version),
                observed_at=observed_at,
                snapshot_hash=snapshot_hash,
                idempotency_key=idempotency_key,
                created_by=actor_id,
            )
            resolved.add(row)
            rows.append(row)

        await self._flush_commit(resolved)
        return self._projection(account, revision, credential, rows)

    async def resolve_execution_binding(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        account_id: Any | None = None,
        *,
        operation: str,
    ) -> dict[str, Any] | None:
        """Return the current server-only authority tuple for one operation.

        Unlike :meth:`refresh`, this method never decrypts a credential and is
        safe for the common Agent runtime.  It can only select a snapshot that
        a human already established against the exact current revisions.
        """

        resolved = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError(
                "actor and platform_account_id are required"
            )
        operation_key = str(operation or "").strip().lower()
        if operation_key not in CAPABILITY_OPERATIONS:
            raise MediaOperationsValidationError("provider operation is invalid")
        account = await self._account(
            resolved, actor, account_id, write=False, lock=False
        )
        credential = await self._credential(resolved, account.id, lock=False)
        if credential is None:
            return None
        revision = await self._latest_revision(resolved, account.id, lock=False)
        row = await self._scalar(
            resolved,
            select(MediaProviderCapabilitySnapshot)
            .where(
                MediaProviderCapabilitySnapshot.platform_account_id == account.id,
                MediaProviderCapabilitySnapshot.account_revision_id == revision.id,
                MediaProviderCapabilitySnapshot.credential_id == credential.id,
                MediaProviderCapabilitySnapshot.credential_revision
                == int(credential.revision or 0),
                MediaProviderCapabilitySnapshot.operation == operation_key,
                MediaProviderCapabilitySnapshot.status == "automatable",
                MediaProviderCapabilitySnapshot.account_eligibility == "eligible",
            )
            .order_by(
                MediaProviderCapabilitySnapshot.observed_at.desc(),
                MediaProviderCapabilitySnapshot.id.desc(),
            )
            .limit(1),
        )
        if row is None:
            return None
        if (
            str(row.credential_state_hash or "")
            != str(credential.state_hash or "")
            or int(row.account_revision or 0) != int(revision.version or 0)
            or str(account.status or "").strip().lower() != "active"
            or str(credential.status or "").strip().lower() != "verified"
        ):
            return None
        policy = get_provider_capability(str(account.platform or ""))
        if policy is None or (
            str(row.registry_version) != str(policy.registry_version)
            or str(row.adapter_key) != str(policy.adapter_key or policy.adapter_ref)
            or str(row.adapter_version) != str(policy.adapter_version)
        ):
            return None
        return {
            "capability_snapshot_id": str(row.id),
            "capability_snapshot_hash": str(row.snapshot_hash),
            "adapter_key": str(row.adapter_key),
            "adapter_version": str(row.adapter_version),
            "credential_state_hash": str(credential.state_hash),
            "operation": operation_key,
        }

    async def get(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        account_id: Any | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve(session)
        if actor is None or account_id is None:
            raise MediaOperationsValidationError(
                "actor and platform_account_id are required"
            )
        account = await self._account(
            resolved, actor, account_id, write=False, lock=False
        )
        credential = await self._credential(resolved, account.id, lock=False)
        if credential is None:
            raise MediaOperationsNotFoundError("platform credential not found")
        revision = await self._latest_revision(resolved, account.id, lock=False)
        result = await self._execute(
            resolved,
            select(MediaProviderCapabilitySnapshot)
            .where(
                MediaProviderCapabilitySnapshot.platform_account_id == account.id,
                MediaProviderCapabilitySnapshot.account_revision_id == revision.id,
                MediaProviderCapabilitySnapshot.credential_id == credential.id,
                MediaProviderCapabilitySnapshot.credential_revision
                == int(credential.revision or 0),
            )
            .order_by(
                MediaProviderCapabilitySnapshot.operation.asc(),
                MediaProviderCapabilitySnapshot.observed_at.desc(),
                MediaProviderCapabilitySnapshot.id.desc(),
            ),
        )
        rows = list(result.scalars().all())
        latest: dict[str, MediaProviderCapabilitySnapshot] = {}
        for row in rows:
            latest.setdefault(str(row.operation), row)
        return self._projection(
            account, revision, credential, list(latest.values())
        )

    @staticmethod
    def _projection(
        account: Any,
        revision: Any,
        credential: Any,
        rows: Sequence[MediaProviderCapabilitySnapshot],
    ) -> dict[str, Any]:
        ordered = sorted(rows, key=lambda row: str(row.operation))
        write_operations = {"text", "image", "video", "schedule"}
        write_capable = any(
            row.operation in write_operations
            and row.status == "automatable"
            and row.account_eligibility == "eligible"
            for row in ordered
        )
        return {
            "platform_account_id": str(account.id),
            "platform": str(account.platform),
            "account_status": str(account.status),
            "account_revision_id": str(revision.id),
            "account_revision": int(revision.version or 0),
            "credential_status": str(credential.status),
            "credential_revision": int(credential.revision or 0),
            "connection_type": str(credential.connection_type),
            "verified": str(credential.status) == "verified",
            "write_capable": write_capable,
            "read_only": str(credential.status) == "verified" and not write_capable,
            "operations": [row.to_safe_dict() for row in ordered],
        }


__all__ = ["MediaProviderReadinessService"]
