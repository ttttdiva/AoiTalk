"""User-scoped Hydrus integration credential lookup.

The Next.js settings route stores ``user_hydrus_credentials.encrypted_payload``
using the shared field-crypto implementation.  FastAPI reads that row by the
authenticated principal supplied by the server callback.  No process-wide
HYDRUS_* fallback is used here: an unconfigured user receives an empty result.
"""

from __future__ import annotations

import logging
import ipaddress
import asyncio
import os
import socket
from urllib.parse import urlparse
from typing import Any, Mapping, Optional

from sqlalchemy import select

from ...memory.database import get_db_session
from ...memory.models.users import UserHydrusCredential

logger = logging.getLogger(__name__)


def _allow_private_hosts() -> bool:
    return (
        os.environ.get("HYDRUS_ALLOW_PRIVATE_HOSTS", "").lower()
        or os.environ.get("AOITALK_HYDRUS_ALLOW_PRIVATE_URLS", "").lower()
    ) in {"1", "true", "yes"}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _private_host(host: str) -> bool:
    normalized = host.strip("[]").lower()
    if normalized == "localhost" or normalized.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    )


def _allowed_api_url(value: str) -> str | None:
    try:
        parsed = urlparse(value.strip())
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    if parsed.username or parsed.password:
        return None
    allow_private = _allow_private_hosts()
    if _private_host(hostname) and not allow_private:
        return None
    return value.strip().rstrip("/")


async def _resolves_to_private_host(hostname: str) -> bool:
    """DNS SSRF guard for public-looking hostnames.

    A hostname can resolve to a private address even when the URL itself does
    not contain an IP literal.  Resolve it before handing the endpoint to the
    Hydrus client.  Resolver failures fail closed: without an authoritative
    answer we cannot prove that the destination is public, so setup/request
    validation must reject it.  Administrators can explicitly opt into local
    endpoints with ``HYDRUS_ALLOW_PRIVATE_HOSTS=true``.
    """

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        # A DNS outage must not become an SSRF bypass.  Treat the unknown
        # destination as disallowed; callers that explicitly permit private
        # hosts skip this resolver check entirely.
        return True
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        if sockaddr and _private_host(str(sockaddr[0])):
            return True
    return False


async def validate_hydrus_api_url(value: str) -> str | None:
    """Validate and normalize a Hydrus endpoint before any outbound request."""

    safe_url = _allowed_api_url(value)
    if safe_url is None:
        return None
    if not _allow_private_hosts():
        try:
            parsed = urlparse(safe_url)
            if parsed.hostname and await _resolves_to_private_host(parsed.hostname):
                return None
        except ValueError:
            return None
    return safe_url


async def load_hydrus_credentials(user_id: str) -> Optional[dict[str, str]]:
    """Return credentials owned by *user_id*, or ``None`` when unavailable.

    ``user_id`` comes from the authenticated request callback, never from a
    query/body parameter.  SQL exceptions are intentionally hidden from the
    caller so secrets and database details do not leak through API responses.
    """

    if not user_id or len(user_id) > 128:
        return None
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
            return None
        try:
            payload = row.payload
        except Exception:
            logger.warning("Hydrus credential decryption failed for user scope")
            return None
        value = _as_mapping(payload)
        api_url = value.get("apiUrl") or value.get("api_url")
        access_key = value.get("accessKey") or value.get("access_key")
        if not isinstance(api_url, str) or not api_url.strip():
            return None
        if not isinstance(access_key, str) or not access_key:
            return None
        safe_url = await validate_hydrus_api_url(api_url)
        if safe_url is None:
            return None
        return {"api_url": safe_url, "access_key": access_key}
    except Exception:
        logger.warning("Hydrus credential lookup failed for user scope")
        return None
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
