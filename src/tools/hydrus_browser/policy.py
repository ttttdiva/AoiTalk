"""Canonical policy for Hydrus Client API endpoints.

Hydrus is an authenticated, user-scoped integration.  The endpoint is still
untrusted input, however, so both the settings boundary and the Python proxy
must apply the same URL/SSRF rules.  Keep this module free of database and
client imports: it is intentionally usable by tests and by small adapters
which only need to validate a URL.

The normal Windows Personal launcher sets ``AOITALK_NATIVE_LOCAL``.  That
marker, together with the Windows/Personal profile checks, is the narrow
exception which permits the historical loopback Hydrus endpoint.  A generic
``HYDRUS_ALLOW_PRIVATE_HOSTS`` opt-in permits explicitly configured LAN
addresses, but it never broadens the loopback exception and never disables the
DNS rebinding guard.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import platform
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Iterable
from urllib.parse import urlparse


DEFAULT_HYDRUS_API_URL = "http://127.0.0.1:45869"
NATIVE_LOCAL_MARKER = "AOITALK_NATIVE_LOCAL"
ALLOW_PRIVATE_ENV_VARS = (
    "HYDRUS_ALLOW_PRIVATE_HOSTS",
    "AOITALK_HYDRUS_ALLOW_PRIVATE_URLS",
)


class HydrusEndpointKind(str, Enum):
    """Coarse endpoint class used for policy decisions and diagnostics."""

    LOOPBACK = "loopback"
    PRIVATE = "private"
    PUBLIC = "public"


class HydrusEndpointPolicyError(ValueError):
    """A syntactically valid URL is not permitted by endpoint policy."""

    code = "hydrus_endpoint_policy_rejected"
    status_code = 422


class HydrusEndpointResolutionError(HydrusEndpointPolicyError):
    """The hostname could not be authoritatively resolved for SSRF checks."""

    code = "hydrus_endpoint_resolution_failed"
    status_code = 502


@dataclass(frozen=True)
class HydrusEndpoint:
    """Validated endpoint metadata.

    ``url`` is normalized to an HTTP(S) origin with no trailing slash.  No
    credential material is stored in this object.
    """

    url: str
    hostname: str
    kind: HydrusEndpointKind


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def native_local_windows_personal(*, environ: dict[str, str] | None = None) -> bool:
    """Return whether this process is the trusted native Windows launcher.

    Do not infer the marker from a user role, request header, or merely from
    ``AOITALK_PROFILE=personal``.  Those values are not proof that the process
    is the packaged/local Windows installation and must not unlock loopback
    server-side fetches.
    """

    env = os.environ if environ is None else environ
    profile = (env.get("AOITALK_PROFILE") or env.get("AIVTUBER_ENV") or "personal").strip().lower()
    # Enterprise wins over contradictory legacy selectors, matching
    # ``Features.profile_name`` without importing the larger feature module.
    if (env.get("AOITALK_PROFILE") or "").strip().lower() == "enterprise":
        profile = "enterprise"
    if (env.get("AIVTUBER_ENV") or "").strip().lower() == "enterprise":
        profile = "enterprise"
    return (
        platform.system().lower() == "windows"
        and profile == "personal"
        and _truthy(env.get(NATIVE_LOCAL_MARKER))
        and not _truthy(env.get("AOITALK_DOCKER"))
    )


def allow_private_hosts(*, environ: dict[str, str] | None = None) -> bool:
    """Whether an administrator explicitly enabled LAN/private endpoints."""

    env = os.environ if environ is None else environ
    return any(_truthy(env.get(name)) for name in ALLOW_PRIVATE_ENV_VARS)


def _normalize_hostname(hostname: str) -> str:
    return hostname.strip().strip("[]").rstrip(".").lower()


def is_loopback_host(hostname: str) -> bool:
    """Recognise only loopback literals and localhost aliases."""

    normalized = _normalize_hostname(hostname)
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
        if address.version == 6 and address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_loopback
        return address.is_loopback
    except ValueError:
        return False


def is_private_host(hostname: str) -> bool:
    """Return whether a host is private, local, reserved, or otherwise non-public."""

    normalized = _normalize_hostname(hostname)
    if normalized == "localhost" or normalized.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if address.version == 4:
        octets = normalized.split(".")
        if len(octets) != 4:
            return False
        first, second = int(octets[0]), int(octets[1])
        return bool(
            first == 0
            or first == 10
            or (first == 100 and 64 <= second <= 127)
            or first == 127
            or (first == 169 and second == 254)
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 0)
            or (first == 192 and second == 0 and octets[2] == "2")
            or (first == 192 and second == 88 and octets[2] == "99")
            or (first == 192 and second == 168)
            or (first == 198 and second in {18, 19})
            or (first == 198 and second == 51)
            or (first == 203 and second == 0)
            or first >= 224
        )
    if address.ipv4_mapped is not None:
        return is_private_host(str(address.ipv4_mapped))
    return bool(
        normalized == "::"
        or normalized == "::1"
        or normalized.startswith(("fc", "fd"))
        or normalized.startswith(("fe8", "fe9", "fea", "feb"))
        or normalized.startswith("ff")
    )


def _is_forbidden_literal(hostname: str) -> bool:
    """Unspecified/reserved literals are never valid Hydrus destinations."""

    try:
        address = ipaddress.ip_address(_normalize_hostname(hostname))
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        return _is_forbidden_literal(str(address.ipv4_mapped))
    if address.is_unspecified or address.is_reserved or address.is_multicast:
        return True
    # Documentation/test ranges are not routable Hydrus destinations.  Keep
    # these rejected even when the administrator has opted into private/LAN
    # endpoints so the opt-in cannot turn a placeholder into a broad target.
    if address.version == 4:
        return any(
            address in ipaddress.ip_network(network)
            for network in (
                "192.0.2.0/24",
                "192.88.99.0/24",
                "198.51.100.0/24",
                "203.0.113.0/24",
            )
        )
    return any(
        address in ipaddress.ip_network(network)
        for network in (
            "100::/64",
            "2001:2::/48",
            "2001:10::/28",
            "2001:20::/28",
            "2001:db8::/32",
            "3fff::/20",
        )
    )


def _parse_origin(value: str) -> tuple[str, str, HydrusEndpointKind]:
    if not isinstance(value, str):
        raise HydrusEndpointPolicyError("Hydrus API URL must be a string")
    raw = value.strip()
    if not raw:
        raise HydrusEndpointPolicyError("Hydrus API URL is required")
    # Reject control characters before urlparse/HTTP clients normalize them;
    # otherwise a CR/LF or NUL can be interpreted differently by a proxy than
    # by this policy boundary.
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in raw):
        raise HydrusEndpointPolicyError("Hydrus API URL contains control characters")
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
    except (TypeError, ValueError):
        raise HydrusEndpointPolicyError("Hydrus API URL is invalid") from None
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise HydrusEndpointPolicyError("Hydrus API URL is invalid")
    if parsed.username or parsed.password:
        raise HydrusEndpointPolicyError("Hydrus API URL must not contain credentials")
    # Hydrus accepts an origin plus an optional path, but callers should not be
    # able to smuggle query fragments or another authority into the target.
    if parsed.query or parsed.fragment or "?" in raw or "#" in raw:
        raise HydrusEndpointPolicyError("Hydrus API URL must not contain query or fragment")
    normalized_host = _normalize_hostname(hostname)
    if _is_forbidden_literal(normalized_host):
        raise HydrusEndpointPolicyError("Hydrus API URL destination is not permitted")
    kind = (
        HydrusEndpointKind.LOOPBACK
        if is_loopback_host(normalized_host)
        else HydrusEndpointKind.PRIVATE
        if is_private_host(normalized_host)
        else HydrusEndpointKind.PUBLIC
    )
    # ``urlparse`` preserves a user supplied path.  Keep it (for compatibility
    # with installations mounted below a reverse proxy), but normalize the
    # trailing slash and avoid returning userinfo/fragment material.
    try:
        port = parsed.port
    except ValueError:
        raise HydrusEndpointPolicyError("Hydrus API URL port is invalid") from None
    path = parsed.path.rstrip("/")
    # ``hostname`` lower-casing is intentional; URL host names are
    # case-insensitive and this keeps cache/client scopes deterministic.
    netloc = normalized_host
    try:
        if ipaddress.ip_address(normalized_host).version == 6:
            netloc = f"[{netloc}]"
    except ValueError:
        pass
    if port is not None:
        netloc = f"{netloc}:{port}"
    normalized = f"{parsed.scheme.lower()}://{netloc}{path}"
    return normalized, normalized_host, kind


def _private_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(_normalize_hostname(address))
    except ValueError:
        return True
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return bool(
        is_private_host(str(parsed))
        or parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_unspecified
        or parsed.is_reserved
    )


def _resolve_addresses(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except (OSError, ValueError, UnicodeError) as exc:
        raise HydrusEndpointResolutionError("Hydrus API hostname could not be resolved") from exc
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else None
        if sockaddr:
            address = str(sockaddr[0])
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise HydrusEndpointResolutionError("Hydrus API hostname could not be resolved")
    return addresses


def _is_loopback_address(address: str) -> bool:
    """Return whether a resolved address is strictly a loopback address.

    ``localhost`` is a DNS name rather than a literal.  It is only equivalent
    to the literal loopback forms when *every* DNS answer is loopback.  Keep
    this check narrower than ``_private_address`` so a compromised hosts file
    cannot make ``localhost`` resolve to an RFC1918/LAN address and still pass
    the native-local exception.
    """

    try:
        parsed = ipaddress.ip_address(_normalize_hostname(address))
    except ValueError:
        return False
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped.is_loopback
    return parsed.is_loopback


async def validate_hydrus_endpoint(value: str) -> HydrusEndpoint:
    """Validate and classify one Hydrus URL, raising a typed safe error."""

    normalized, hostname, kind = _parse_origin(value)
    allow_private = allow_private_hosts()
    if kind is HydrusEndpointKind.LOOPBACK:
        if not native_local_windows_personal():
            raise HydrusEndpointPolicyError("Hydrus loopback endpoint requires native local policy")
        # Literal 127/8 and ::1 are intrinsically loopback.  ``localhost`` and
        # ``*.localhost`` are names, however, so resolve them and require every
        # answer to remain loopback.  This closes the hosts-file/DNS rebinding
        # gap while preserving the historical native-local endpoint.
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            addresses = await asyncio.to_thread(_resolve_addresses, hostname)
            if any(not _is_loopback_address(address) for address in addresses):
                raise HydrusEndpointPolicyError(
                    "Hydrus loopback hostname resolved to a non-loopback address"
                )
        return HydrusEndpoint(normalized, hostname, kind)

    if kind is HydrusEndpointKind.PRIVATE:
        if not allow_private:
            raise HydrusEndpointPolicyError("Hydrus private endpoint requires administrator opt-in")
        if "://" not in normalized:
            raise HydrusEndpointPolicyError("Hydrus API URL is invalid")
        # Private DNS names are still resolved before use.  This prevents an
        # opt-in private name from silently becoming an arbitrary public host,
        # while preserving the explicit LAN opt-in for private destinations.
        try:
            addresses = await asyncio.to_thread(_resolve_addresses, hostname)
        except HydrusEndpointResolutionError:
            raise
        if any(not _private_address(address) for address in addresses):
            raise HydrusEndpointPolicyError("Hydrus private endpoint resolved to a public address")
        return HydrusEndpoint(normalized, hostname, kind)

    # Always resolve public-looking names.  In particular, the private-host
    # opt-in does *not* disable this DNS rebinding/SSRF guard.
    addresses = await asyncio.to_thread(_resolve_addresses, hostname)
    if any(_private_address(address) for address in addresses):
        raise HydrusEndpointPolicyError("Hydrus API hostname resolves to a private address")
    return HydrusEndpoint(normalized, hostname, kind)


async def validate_hydrus_api_url_strict(value: str) -> str:
    """Strict validator used by request routes and settings adapters."""

    return (await validate_hydrus_endpoint(value)).url


async def resolve_hydrus_hostname(hostname: str) -> Iterable[str]:
    """Small test/diagnostic hook exposing the guarded resolver only."""

    return tuple(_resolve_addresses(hostname))


def endpoint_policy_error_code(error: BaseException) -> str:
    """Return a safe machine code for policy exceptions."""

    return getattr(error, "code", "hydrus_endpoint_policy_rejected")
