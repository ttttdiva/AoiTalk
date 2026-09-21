"""User-scoped Hydrus integration credential resolution.

The canonical store is ``user_hydrus_credentials.encrypted_payload``.  This
module deliberately keeps the normal runtime resolver blind to process-global
``HYDRUS_*`` values.  Legacy values are exposed only through an explicit
owner-claim helper for a one-time authenticated migration path.
"""

from __future__ import annotations

import hmac
import logging
import os
import socket  # compatibility export; policy uses the same module object
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import select

from ...memory.database import get_db_session
from ...memory.models.users import UserHydrusCredential
from .policy import (
    DEFAULT_HYDRUS_API_URL,
    HydrusEndpointPolicyError,
    HydrusEndpointResolutionError,
    allow_private_hosts,
    is_loopback_host,
    is_private_host,
    native_local_windows_personal,
    validate_hydrus_api_url_strict as _canonical_validate_hydrus_api_url,
)

logger = logging.getLogger(__name__)


class HydrusCredentialError(RuntimeError):
    """Typed, secret-free failure while resolving one user's integration."""

    code = "hydrus_credential_unreadable"
    status_code = 500


class HydrusNotConfiguredError(HydrusCredentialError):
    code = "hydrus_not_configured"
    status_code = 409


class HydrusCredentialStoreError(HydrusCredentialError):
    code = "hydrus_credential_store_unavailable"
    status_code = 503


class HydrusLegacyOwnerError(HydrusCredentialError):
    """Legacy env values may only be consumed after an explicit owner claim."""

    code = "hydrus_legacy_owner_ambiguous"
    status_code = 409


@dataclass(frozen=True)
class HydrusCredentialCandidate:
    """In-memory legacy candidate; never serialize this object to a response."""

    api_url: str
    access_key: str

    def as_mapping(self) -> dict[str, str]:
        return {"api_url": self.api_url, "access_key": self.access_key}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _allow_private_hosts() -> bool:
    """Compatibility alias for callers/tests; policy remains canonical."""

    return allow_private_hosts()


def _private_host(host: str) -> bool:
    """Compatibility alias for the canonical host classifier."""

    return is_private_host(host)


def _allowed_api_url(value: str) -> str | None:
    """Synchronous syntax/classification check kept for legacy callers/tests.

    Full DNS validation is asynchronous and performed by
    :func:`validate_hydrus_api_url`.  Loopback is never unlocked by the broad
    private-host opt-in; it requires the native Windows Personal marker.
    """

    try:
        from .policy import _parse_origin

        normalized, hostname, _kind = _parse_origin(value)
    except (HydrusEndpointPolicyError, TypeError, ValueError):
        return None
    if is_loopback_host(hostname):
        return normalized if native_local_windows_personal() else None
    if is_private_host(hostname) and not allow_private_hosts():
        return None
    return normalized


async def validate_hydrus_api_url(value: str) -> str | None:
    """Validate and normalize a Hydrus endpoint, returning ``None`` on reject.

    This preserves the optional-return API used by older call sites.  Routes
    needing a user-facing machine code should use the strict variant below.
    """

    try:
        return await _canonical_validate_hydrus_api_url(value)
    except (HydrusEndpointPolicyError, HydrusEndpointResolutionError):
        return None


async def validate_hydrus_api_url_strict(value: str) -> str:
    """Canonical strict validator exported from the credential boundary."""

    return await _canonical_validate_hydrus_api_url(value)


def legacy_hydrus_env_configured() -> bool:
    """Return whether both legacy env values exist, without exposing either."""

    return bool(
        os.environ.get("HYDRUS_ACCESS_KEY", "").strip()
        and (os.environ.get("HYDRUS_API_URL", "").strip() or DEFAULT_HYDRUS_API_URL)
    )


def claim_legacy_hydrus_credentials(
    principal_id: str,
    *,
    owner_claim: str | None,
) -> dict[str, str]:
    """Read legacy env credentials only for an explicit principal claim.

    This helper is intentionally not called by the normal resolver.  A caller
    (for example an authenticated one-time migration endpoint) must provide the
    current session principal and an equal owner claim.  No role/first-user or
    process-global fallback is inferred here.
    """

    normalized_principal = str(principal_id or "").strip()
    normalized_claim = str(owner_claim or "").strip()
    if not normalized_principal or not normalized_claim or not hmac.compare_digest(
        normalized_principal, normalized_claim
    ):
        raise HydrusLegacyOwnerError(
            "legacy Hydrus configuration requires an explicit owner claim"
        )
    access_key = os.environ.get("HYDRUS_ACCESS_KEY", "")
    api_url = os.environ.get("HYDRUS_API_URL", "") or DEFAULT_HYDRUS_API_URL
    if not access_key.strip():
        raise HydrusNotConfiguredError("Hydrus legacy configuration is not present")
    # URL policy is applied again by the eventual settings write and runtime
    # resolver.  Keeping this source helper side-effect free avoids storing or
    # logging an unvalidated endpoint during an ambiguous migration attempt.
    return {"api_url": api_url.strip().rstrip("/"), "access_key": access_key.strip()}


def legacy_hydrus_env_values_for_owner(
    principal_id: str,
    *,
    owner_claim: str | None,
) -> HydrusCredentialCandidate:
    """Typed owner-gated variant for migration callers."""

    return HydrusCredentialCandidate(
        **claim_legacy_hydrus_credentials(principal_id, owner_claim=owner_claim)
    )


async def load_hydrus_credentials(user_id: str) -> Optional[dict[str, str]]:
    """Return credentials owned by ``user_id`` or ``None`` when unavailable.

    This compatibility wrapper remains fail-closed and hides storage/crypto
    diagnostics.  HTTP routes use :func:`resolve_hydrus_credentials` to retain
    typed, safe error categories.
    """

    try:
        return await resolve_hydrus_credentials(user_id)
    except HydrusCredentialError:
        return None
    except (HydrusEndpointPolicyError, HydrusEndpointResolutionError):
        return None
    except Exception:
        logger.warning("Hydrus credential lookup failed for user scope")
        return None


async def resolve_hydrus_credentials(user_id: str) -> dict[str, str]:
    """Resolve one authenticated user's encrypted Hydrus credentials.

    Unlike :func:`load_hydrus_credentials`, this strict API retains typed
    failure causes for HTTP routes.  It never consults process-global legacy
    environment values.
    """

    if not user_id or len(str(user_id)) > 128:
        raise HydrusNotConfiguredError("Hydrus connection is not configured")
    session = None
    try:
        session = await get_db_session()
        result = await session.execute(
            select(UserHydrusCredential)
            .where(
                UserHydrusCredential.user_id == str(user_id),
                UserHydrusCredential.enabled.is_(True),
            )
            .order_by(UserHydrusCredential.updated_at.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if not row:
            raise HydrusNotConfiguredError("Hydrus connection is not configured")
        try:
            payload = row.payload
        except Exception:
            logger.warning("Hydrus credential decryption failed for user scope")
            raise HydrusCredentialError(
                "Hydrus credentials could not be decrypted"
            ) from None
        value = _as_mapping(payload)
        api_url = value.get("apiUrl") or value.get("api_url")
        access_key = value.get("accessKey") or value.get("access_key")
        if not isinstance(api_url, str) or not api_url.strip():
            raise HydrusCredentialError("Hydrus credentials are malformed")
        if not isinstance(access_key, str) or not access_key.strip():
            raise HydrusCredentialError("Hydrus credentials are malformed")
        safe_url = await validate_hydrus_api_url_strict(api_url)
        return {"api_url": safe_url, "access_key": access_key}
    except (HydrusCredentialError, HydrusEndpointPolicyError, HydrusEndpointResolutionError):
        raise
    except Exception:
        logger.warning("Hydrus credential store lookup failed for user scope")
        raise HydrusCredentialStoreError("Hydrus credential store unavailable") from None
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
