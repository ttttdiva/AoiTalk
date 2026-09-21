"""Bounded, read-only Active Directory password authentication transport.

This module intentionally owns only the *transport* side of AD authentication.
It never stores an AD password, creates a service-account credential, performs
an AD write, or falls back to a local password check.  ``ldap3`` and
``dnspython`` are imported lazily so Personal deployments (and tests that do
not install enterprise dependencies) remain importable.

The public API is deliberately small:

``ActiveDirectoryConfig.from_env``
    Parse and validate the Enterprise-only, LDAPS-only configuration.
``ActiveDirectoryClient.authenticate``
    Bind as the supplied user, read the bounded identity attributes, and
    return an immutable ``ActiveDirectoryIdentity``.

The implementation uses synchronous ldap3 calls in a worker thread.  LDAP is
not an asyncio-native protocol and running it directly in the event loop would
allow a slow or unreachable controller to stall every HTTP request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import ssl
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)

MAX_ENDPOINTS_HARD_LIMIT = 4
DEFAULT_PORT = 636
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_OPERATION_TIMEOUT_SECONDS = 10.0
MIN_TIMEOUT_SECONDS = 0.1
MAX_CONNECT_TIMEOUT_SECONDS = 15.0
MAX_OPERATION_TIMEOUT_SECONDS = 30.0
DEFAULT_LOGIN_ATTRIBUTE = "sAMAccountName"
DEFAULT_DISCOVERY_MODE = "static"


class ActiveDirectoryError(RuntimeError):
    """Base class for safe AD transport failures.

    Error messages intentionally contain no bind names, passwords, endpoint
    names, LDAP diagnostic strings, or distinguished names.  Callers should
    use ``code`` for audit/HTTP mapping and expose only ``safe_message``.
    """

    code = "ad_configuration_unavailable"
    safe_message = "Active Directory authentication is unavailable"

    def __init__(self, message: str | None = None, *, cause: BaseException | None = None):
        super().__init__(message or self.safe_message)
        self.cause = cause


class ActiveDirectoryConfigurationError(ActiveDirectoryError):
    code = "ad_configuration_unavailable"
    safe_message = "Active Directory authentication is not configured"


class ActiveDirectoryUnreachableError(ActiveDirectoryError):
    code = "ad_unreachable"
    safe_message = "Active Directory could not be reached"


class ActiveDirectoryTLSFailure(ActiveDirectoryError):
    code = "ad_tls_failure"
    safe_message = "Active Directory TLS validation failed"


class ActiveDirectoryIdentityLookupError(ActiveDirectoryError):
    code = "ad_identity_lookup_failed"
    safe_message = "Active Directory identity lookup failed"


class ActiveDirectoryInvalidCredentials(ActiveDirectoryError):
    code = "invalid_credentials"
    safe_message = "Invalid credentials"


class ActiveDirectoryAccountDisabled(ActiveDirectoryError):
    code = "account_disabled"
    safe_message = "Account is disabled"


class ActiveDirectoryProvisioningError(ActiveDirectoryError):
    code = "ad_provisioning_conflict"
    safe_message = "Active Directory account provisioning conflict"


class DiscoveryMode(str, Enum):
    STATIC = "static"
    SRV = "srv"


def _env_value(environ: Mapping[str, str], *keys: str, default: str = "") -> str:
    """Return the first present environment value without logging it."""

    for key in keys:
        value = environ.get(key)
        if value is not None:
            return str(value).strip()
    return default


def _parse_bool(value: str, *, key: str, default: bool = False) -> bool:
    if value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ActiveDirectoryConfigurationError(f"Invalid {key} setting")


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _normalize_dns_name(value: str, *, field: str) -> str:
    """Validate and canonicalize a DNS name, rejecting all IP literals."""

    candidate = str(value or "").strip().rstrip(".").lower()
    if not candidate:
        raise ActiveDirectoryConfigurationError(f"Invalid {field} setting")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ActiveDirectoryConfigurationError(f"Invalid {field} setting")

    # ``socket.getfqdn`` is deliberately not used here: validation must not
    # perform an unbounded DNS lookup.  IDNA normalization keeps the allowlist
    # comparison deterministic for internationalized names.
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeEncodeError) as exc:
        raise ActiveDirectoryConfigurationError(f"Invalid {field} setting") from exc
    if len(candidate) > 253 or "." not in candidate:
        raise ActiveDirectoryConfigurationError(f"Invalid {field} setting")
    labels = candidate.split(".")
    label_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    if any(not label_re.fullmatch(label) for label in labels):
        raise ActiveDirectoryConfigurationError(f"Invalid {field} setting")
    return candidate


def _validate_suffixes(suffixes: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for suffix in suffixes:
        normalized_suffix = _normalize_dns_name(suffix, field="AOITALK_AD_ALLOWED_DNS_SUFFIXES")
        if normalized_suffix not in normalized:
            normalized.append(normalized_suffix)
    if not normalized:
        raise ActiveDirectoryConfigurationError(
            "AOITALK_AD_ALLOWED_DNS_SUFFIXES is required"
        )
    return tuple(normalized)


def _is_allowed_host(host: str, suffixes: Sequence[str]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


@dataclass(frozen=True, slots=True)
class ActiveDirectoryConfig:
    """Validated Enterprise AD transport settings.

    ``servers`` are static FQDNs only.  In ``srv`` mode they are optional and
    endpoints are resolved at operation time from the explicitly configured
    SRV record.  No IP address, wildcard, subnet, or arbitrary DNS discovery
    is accepted.
    """

    enabled: bool = False
    discovery_mode: DiscoveryMode = DiscoveryMode.STATIC
    servers: tuple[str, ...] = ()
    srv_record: str | None = None
    allowed_dns_suffixes: tuple[str, ...] = ()
    port: int = DEFAULT_PORT
    base_dn: str | None = None
    login_attribute: str = DEFAULT_LOGIN_ATTRIBUTE
    bind_template: str = "{username}@{domain}"
    domain: str | None = None
    ca_file: str | None = None
    use_system_ca: bool = True
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS
    max_endpoints: int = MAX_ENDPOINTS_HARD_LIMIT
    authority: str | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        enterprise: bool | None = None,
    ) -> "ActiveDirectoryConfig":
        env = os.environ if environ is None else environ
        enabled = _parse_bool(
            _env_value(env, "AOITALK_AD_ENABLED"),
            key="AOITALK_AD_ENABLED",
            default=False,
        )
        if enterprise is None:
            # Import lazily to keep this transport usable by isolated tests and
            # small tooling without importing the full runtime feature graph.
            try:
                from ..features import Features

                enterprise = Features.is_enterprise()
            except Exception:
                enterprise = False
        if enabled and not enterprise:
            raise ActiveDirectoryConfigurationError(
                "Active Directory authentication is Enterprise-only"
            )

        raw_mode = _env_value(
            env,
            "AOITALK_AD_DISCOVERY_MODE",
            "AOITALK_AD_DISCOVERY",
            default=DEFAULT_DISCOVERY_MODE,
        ).lower()
        try:
            discovery_mode = DiscoveryMode(raw_mode or DEFAULT_DISCOVERY_MODE)
        except ValueError as exc:
            raise ActiveDirectoryConfigurationError(
                "Invalid AOITALK_AD_DISCOVERY_MODE setting"
            ) from exc

        raw_suffixes = _env_value(
            env,
            "AOITALK_AD_ALLOWED_DNS_SUFFIXES",
            "AOITALK_AD_ALLOWED_SUFFIXES",
            "AOITALK_AD_ALLOWED_DNS_SUFFIX",
        )
        suffixes: tuple[str, ...] = ()
        if raw_suffixes:
            suffixes = _validate_suffixes(_parse_csv(raw_suffixes))
        elif enabled:
            # Fail closed when enabled.  A disabled config may remain the
            # default empty value so Personal startup does not need AD vars.
            raise ActiveDirectoryConfigurationError(
                "AOITALK_AD_ALLOWED_DNS_SUFFIXES is required"
            )

        raw_servers = _parse_csv(_env_value(env, "AOITALK_AD_SERVERS"))
        if len(raw_servers) > MAX_ENDPOINTS_HARD_LIMIT:
            raise ActiveDirectoryConfigurationError("Too many Active Directory servers")
        servers: list[str] = []
        for server in raw_servers:
            host = _normalize_dns_name(server, field="AOITALK_AD_SERVERS")
            if suffixes and not _is_allowed_host(host, suffixes):
                raise ActiveDirectoryConfigurationError("Active Directory server is outside the DNS allowlist")
            if host not in servers:
                servers.append(host)

        srv_record = _env_value(env, "AOITALK_AD_SRV_RECORD") or None
        if discovery_mode is DiscoveryMode.STATIC and enabled and not servers:
            raise ActiveDirectoryConfigurationError("AOITALK_AD_SERVERS is required")
        if discovery_mode is DiscoveryMode.SRV:
            if not srv_record:
                # Derive the conventional LDAPS SRV record only from the
                # explicit allowlist (never from arbitrary user input).
                if len(suffixes) == 1:
                    srv_record = f"_ldaps._tcp.{suffixes[0]}"
                else:
                    raise ActiveDirectoryConfigurationError("AOITALK_AD_SRV_RECORD is required")
            srv_record = _validate_srv_record(srv_record, suffixes)
            if enabled and not suffixes:
                raise ActiveDirectoryConfigurationError("AOITALK_AD_ALLOWED_DNS_SUFFIXES is required")

        raw_port = _env_value(env, "AOITALK_AD_PORT", default=str(DEFAULT_PORT))
        try:
            port = int(raw_port or DEFAULT_PORT)
        except ValueError as exc:
            raise ActiveDirectoryConfigurationError("Invalid AOITALK_AD_PORT setting") from exc
        # Only LDAPS 636 is supported.  Refusing arbitrary LDAP ports prevents
        # a caller from silently downgrading transport security.
        if port != DEFAULT_PORT:
            raise ActiveDirectoryConfigurationError("Active Directory requires LDAPS on port 636")

        base_dn = _env_value(env, "AOITALK_AD_BASE_DN") or None
        if enabled and not base_dn:
            raise ActiveDirectoryConfigurationError("AOITALK_AD_BASE_DN is required")
        login_attribute = _env_value(
            env,
            "AOITALK_AD_LOGIN_ATTRIBUTE",
            default=DEFAULT_LOGIN_ATTRIBUTE,
        ) or DEFAULT_LOGIN_ATTRIBUTE
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", login_attribute):
            raise ActiveDirectoryConfigurationError("Invalid AOITALK_AD_LOGIN_ATTRIBUTE setting")

        bind_template = _env_value(
            env,
            "AOITALK_AD_BIND_TEMPLATE",
            default="{username}@{domain}",
        ) or "{username}@{domain}"
        if any(token not in {"username", "domain"} for token in _template_fields(bind_template)):
            raise ActiveDirectoryConfigurationError("Invalid AOITALK_AD_BIND_TEMPLATE setting")
        if "{username}" not in bind_template:
            raise ActiveDirectoryConfigurationError("AOITALK_AD_BIND_TEMPLATE must contain {username}")

        raw_domain = _env_value(env, "AOITALK_AD_DOMAIN") or (suffixes[0] if suffixes else "")
        domain = _normalize_dns_name(raw_domain, field="AOITALK_AD_DOMAIN") if raw_domain else None
        ca_file = _env_value(env, "AOITALK_AD_CA_FILE") or None
        if ca_file:
            path = Path(ca_file).expanduser()
            if not path.is_file():
                raise ActiveDirectoryConfigurationError("AOITALK_AD_CA_FILE is not readable")
            ca_file = str(path)
        use_system_ca = _parse_bool(
            _env_value(env, "AOITALK_AD_USE_SYSTEM_CA", default="true"),
            key="AOITALK_AD_USE_SYSTEM_CA",
            default=True,
        )
        if enabled and not use_system_ca and not ca_file:
            raise ActiveDirectoryConfigurationError(
                "AOITALK_AD_CA_FILE is required when system CA validation is disabled"
            )

        connect_timeout = _parse_timeout(
            _env_value(env, "AOITALK_AD_CONNECT_TIMEOUT_SECONDS", default=str(DEFAULT_CONNECT_TIMEOUT_SECONDS)),
            "AOITALK_AD_CONNECT_TIMEOUT_SECONDS",
            maximum=MAX_CONNECT_TIMEOUT_SECONDS,
        )
        operation_timeout = _parse_timeout(
            _env_value(env, "AOITALK_AD_OPERATION_TIMEOUT_SECONDS", default=str(DEFAULT_OPERATION_TIMEOUT_SECONDS)),
            "AOITALK_AD_OPERATION_TIMEOUT_SECONDS",
            maximum=MAX_OPERATION_TIMEOUT_SECONDS,
        )
        try:
            max_endpoints = int(
                _env_value(env, "AOITALK_AD_MAX_ENDPOINTS", default=str(MAX_ENDPOINTS_HARD_LIMIT))
                or MAX_ENDPOINTS_HARD_LIMIT
            )
        except ValueError as exc:
            raise ActiveDirectoryConfigurationError("Invalid AOITALK_AD_MAX_ENDPOINTS setting") from exc
        if not 1 <= max_endpoints <= MAX_ENDPOINTS_HARD_LIMIT:
            raise ActiveDirectoryConfigurationError("Invalid AOITALK_AD_MAX_ENDPOINTS setting")
        if len(servers) > max_endpoints:
            raise ActiveDirectoryConfigurationError("Too many Active Directory servers")

        authority = _env_value(env, "AOITALK_AD_AUTHORITY") or domain or (suffixes[0] if suffixes else None)
        if authority:
            # Authority is an immutable binding namespace, not a secret.  Keep
            # it DNS-shaped to prevent callers from smuggling arbitrary scope.
            authority = _normalize_dns_name(authority, field="AOITALK_AD_AUTHORITY")

        return cls(
            enabled=enabled,
            discovery_mode=discovery_mode,
            servers=tuple(servers),
            srv_record=srv_record,
            allowed_dns_suffixes=suffixes,
            port=port,
            base_dn=base_dn,
            login_attribute=login_attribute,
            bind_template=bind_template,
            domain=domain,
            ca_file=ca_file,
            use_system_ca=use_system_ca,
            connect_timeout_seconds=connect_timeout,
            operation_timeout_seconds=operation_timeout,
            max_endpoints=max_endpoints,
            authority=authority,
        )

    def validate(self, *, enterprise: bool | None = None) -> None:
        """Validate an already-constructed config before use."""

        if not isinstance(self.enabled, bool):
            raise ActiveDirectoryConfigurationError("Active Directory enabled flag is invalid")
        try:
            discovery_mode = DiscoveryMode(self.discovery_mode)
        except (TypeError, ValueError) as exc:
            raise ActiveDirectoryConfigurationError(
                "Invalid Active Directory discovery mode"
            ) from exc
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ActiveDirectoryConfigurationError("Active Directory port is invalid")
        if not isinstance(self.max_endpoints, int) or isinstance(self.max_endpoints, bool):
            raise ActiveDirectoryConfigurationError("Invalid Active Directory endpoint bound")
        if not 1 <= self.max_endpoints <= MAX_ENDPOINTS_HARD_LIMIT:
            raise ActiveDirectoryConfigurationError("Invalid Active Directory endpoint bound")
        if not isinstance(self.use_system_ca, bool):
            raise ActiveDirectoryConfigurationError("Active Directory CA mode is invalid")

        try:
            suffixes = tuple(
                _normalize_dns_name(
                    suffix,
                    field="AOITALK_AD_ALLOWED_DNS_SUFFIXES",
                )
                for suffix in self.allowed_dns_suffixes
            )
        except (TypeError, ActiveDirectoryError) as exc:
            raise ActiveDirectoryConfigurationError(
                "Active Directory DNS allowlist is invalid",
                cause=exc,
            ) from exc

        normalized_servers: list[str] = []
        for server in self.servers:
            host = _normalize_dns_name(server, field="Active Directory server")
            if host not in normalized_servers:
                normalized_servers.append(host)

        if self.base_dn is not None and (
            not isinstance(self.base_dn, str)
            or len(self.base_dn) > 4096
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in self.base_dn)
        ):
            raise ActiveDirectoryConfigurationError("Active Directory base DN is invalid")
        if not isinstance(self.login_attribute, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9-]{0,63}", self.login_attribute
        ):
            raise ActiveDirectoryConfigurationError("Active Directory login attribute is invalid")
        if not isinstance(self.bind_template, str):
            raise ActiveDirectoryConfigurationError("Active Directory bind template is invalid")
        template_fields = _template_fields(self.bind_template)
        if any(field not in {"username", "domain"} for field in template_fields):
            raise ActiveDirectoryConfigurationError("Active Directory bind template is invalid")
        if "{username}" not in self.bind_template or len(self.bind_template) > 512:
            raise ActiveDirectoryConfigurationError("Active Directory bind template must contain {username}")
        if "{password}" in self.bind_template:
            raise ActiveDirectoryConfigurationError("Active Directory bind template is invalid")
        for name, value in (("domain", self.domain), ("authority", self.authority)):
            if value is not None:
                _normalize_dns_name(value, field=f"Active Directory {name}")
        for name, value in (
            ("connect", self.connect_timeout_seconds),
            ("operation", self.operation_timeout_seconds),
        ):
            maximum = (
                MAX_CONNECT_TIMEOUT_SECONDS
                if name == "connect"
                else MAX_OPERATION_TIMEOUT_SECONDS
            )
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not MIN_TIMEOUT_SECONDS <= float(value) <= maximum
            ):
                raise ActiveDirectoryConfigurationError(
                    f"Invalid Active Directory {name} timeout"
                )
        if self.ca_file is not None:
            try:
                ca_path = Path(self.ca_file).expanduser()
                if ca_path.is_symlink() or not ca_path.is_file():
                    raise OSError("CA path is not a regular file")
            except (OSError, TypeError, ValueError) as exc:
                raise ActiveDirectoryConfigurationError(
                    "Active Directory CA file is not readable",
                    cause=exc,
                ) from exc

        if self.enabled:
            if enterprise is False:
                raise ActiveDirectoryConfigurationError(
                    "Active Directory authentication is Enterprise-only"
                )
            if self.port != DEFAULT_PORT:
                raise ActiveDirectoryConfigurationError("Active Directory requires LDAPS on port 636")
            if not suffixes:
                raise ActiveDirectoryConfigurationError("Active Directory DNS allowlist is required")
            if discovery_mode is DiscoveryMode.STATIC and not normalized_servers:
                raise ActiveDirectoryConfigurationError("Active Directory server list is required")
            if discovery_mode is DiscoveryMode.SRV and not self.srv_record:
                raise ActiveDirectoryConfigurationError("Active Directory SRV record is required")
            if len(normalized_servers) > min(self.max_endpoints, MAX_ENDPOINTS_HARD_LIMIT):
                raise ActiveDirectoryConfigurationError("Too many Active Directory servers")
            if not self.base_dn:
                raise ActiveDirectoryConfigurationError("Active Directory base DN is required")
            if not self.use_system_ca and not self.ca_file:
                raise ActiveDirectoryConfigurationError(
                    "Active Directory CA file is required when system CA validation is disabled"
                )
        if discovery_mode is DiscoveryMode.SRV and self.srv_record:
            if not isinstance(self.srv_record, str):
                raise ActiveDirectoryConfigurationError("Active Directory SRV record is invalid")
            _validate_srv_record(self.srv_record, suffixes)
        for host in normalized_servers:
            if not _is_allowed_host(host, suffixes):
                raise ActiveDirectoryConfigurationError("Active Directory server is outside the DNS allowlist")

    def endpoint_hosts(self) -> tuple[tuple[str, int], ...]:
        """Resolve the bounded endpoint set (without opening any connection)."""

        self.validate()
        if not self.enabled:
            raise ActiveDirectoryConfigurationError()
        discovery_mode = DiscoveryMode(self.discovery_mode)
        suffixes = tuple(
            _normalize_dns_name(suffix, field="AOITALK_AD_ALLOWED_DNS_SUFFIXES")
            for suffix in self.allowed_dns_suffixes
        )
        servers = tuple(
            _normalize_dns_name(server, field="Active Directory server")
            for server in self.servers
        )
        if discovery_mode is DiscoveryMode.STATIC:
            return tuple((host, DEFAULT_PORT) for host in servers[: self.max_endpoints])
        if not self.srv_record:
            raise ActiveDirectoryConfigurationError("Active Directory SRV record is missing")
        try:
            import dns.resolver  # type: ignore
        except ImportError as exc:
            raise ActiveDirectoryConfigurationError(
                "Active Directory DNS discovery dependency is unavailable"
            ) from exc
        try:
            answers = dns.resolver.resolve(
                self.srv_record,
                "SRV",
                lifetime=self.operation_timeout_seconds,
                search=False,
            )
        except Exception as exc:
            # DNS errors are deliberately not included in the safe message.
            raise ActiveDirectoryUnreachableError(cause=exc) from exc
        endpoints: list[tuple[str, int]] = []
        for answer in answers:
            try:
                target = _normalize_dns_name(str(answer.target), field="Active Directory SRV target")
                port = int(answer.port)
            except (TypeError, ValueError, ActiveDirectoryError):
                continue
            if port != DEFAULT_PORT or not _is_allowed_host(target, suffixes):
                continue
            endpoint = (target, DEFAULT_PORT)
            if endpoint not in endpoints:
                endpoints.append(endpoint)
            if len(endpoints) >= self.max_endpoints:
                break
        if not endpoints:
            raise ActiveDirectoryUnreachableError()
        return tuple(endpoints)

    def bind_name(self, username: str) -> str:
        if not isinstance(username, str) or not username or len(username) > 255:
            raise ActiveDirectoryInvalidCredentials()
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in username):
            raise ActiveDirectoryInvalidCredentials()
        try:
            result = self.bind_template.format(
                username=username,
                domain=self.domain or "",
            )
        except (KeyError, ValueError, IndexError) as exc:
            raise ActiveDirectoryConfigurationError(cause=exc) from exc
        # An empty domain is fine when an explicit {username}-only template is
        # supplied.  Never permit a template to inject a second credential.
        if not result or len(result) > 512 or "{password}" in self.bind_template:
            raise ActiveDirectoryConfigurationError()
        return result


def _template_fields(value: str) -> tuple[str, ...]:
    fields: list[str] = []
    for match in re.finditer(r"\{([^{}]+)\}", value):
        fields.append(match.group(1).split("!", 1)[0].split(":", 1)[0])
    # Any unmatched braces are rejected by a conservative check below.
    if value.count("{") != value.count("}"):
        return ("__invalid__",)
    return tuple(fields)


def _validate_srv_record(value: str, suffixes: Sequence[str]) -> str:
    candidate = value.strip().rstrip(".").lower()
    if not candidate.startswith("_ldaps._tcp."):
        raise ActiveDirectoryConfigurationError("AOITALK_AD_SRV_RECORD must be _ldaps._tcp")
    suffix = candidate[len("_ldaps._tcp.") :]
    normalized = _normalize_dns_name(suffix, field="AOITALK_AD_SRV_RECORD")
    if not _is_allowed_host(normalized, suffixes):
        raise ActiveDirectoryConfigurationError("AOITALK_AD_SRV_RECORD is outside the DNS allowlist")
    return f"_ldaps._tcp.{normalized}"


def _parse_timeout(
    value: str,
    key: str,
    *,
    minimum: float = MIN_TIMEOUT_SECONDS,
    maximum: float = MAX_OPERATION_TIMEOUT_SECONDS,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ActiveDirectoryConfigurationError(f"Invalid {key} setting") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ActiveDirectoryConfigurationError(f"Invalid {key} setting")
    return result


def normalize_object_guid(value: Any) -> UUID:
    """Normalize an AD ``objectGUID`` value to a UUID.

    AD returns GUID bytes in little-endian field order.  ``UUID(bytes_le=…)``
    is therefore required; interpreting those bytes as network-order would
    create a different identity and could bind a user to the wrong account.
    """

    if isinstance(value, UUID):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) != 16:
            raise ActiveDirectoryIdentityLookupError()
        try:
            return UUID(bytes_le=raw)
        except (ValueError, AttributeError) as exc:
            raise ActiveDirectoryIdentityLookupError(cause=exc) from exc
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return normalize_object_guid(value[0])
    if isinstance(value, str):
        try:
            return UUID(value.strip("{} "))
        except (ValueError, AttributeError) as exc:
            raise ActiveDirectoryIdentityLookupError(cause=exc) from exc
    raise ActiveDirectoryIdentityLookupError()


@dataclass(frozen=True, slots=True)
class ActiveDirectoryIdentity:
    """Minimal identity data returned after a successful user bind."""

    object_guid: UUID
    username: str
    display_name: str | None = None
    email: str | None = None
    authority: str | None = None
    is_active: bool = True

    @property
    def external_id(self) -> UUID:
        return self.object_guid

    @property
    def object_guid_str(self) -> str:
        return str(self.object_guid)


def _entry_value(entry: Any, attribute: str) -> Any:
    """Extract one ldap3 entry attribute without invoking arbitrary methods."""

    attrs = getattr(entry, "entry_attributes_as_dict", None)
    if isinstance(attrs, Mapping) and attribute in attrs:
        value = attrs[attribute]
    else:
        attr_obj = getattr(entry, attribute, None)
        value = getattr(attr_obj, "value", attr_obj)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _safe_filter_escape(value: str) -> str:
    try:
        from ldap3.utils.conv import escape_filter_chars  # type: ignore

        return escape_filter_chars(value)
    except Exception:
        # RFC 4515 escaping fallback; this is used only when ldap3's utility
        # cannot be imported (for example, an isolated unit test).
        escaped: list[str] = []
        for char in value:
            code = ord(char)
            if char == "\\":
                escaped.append(r"\5c")
            elif char == "*":
                escaped.append(r"\2a")
            elif char == "(":
                escaped.append(r"\28")
            elif char == ")":
                escaped.append(r"\29")
            elif code == 0:
                escaped.append(r"\00")
            elif code < 0x20 or code >= 0x7F:
                # RFC 4515 escapes UTF-8 octets, not Unicode code points.
                # This matters for non-ASCII directory login names and for
                # controls represented by multibyte encodings.
                escaped.extend("\\" + byte.to_bytes(1, "big").hex() for byte in char.encode("utf-8"))
            else:
                escaped.append(char)
        return "".join(escaped)


class ActiveDirectoryClient:
    """Bounded LDAPS user-bind client.

    ``connection_factory`` is intentionally injectable for deterministic unit
    tests.  It receives ``(ldap_module, server, bind_name, password, tls)``
    and must return an ldap3-compatible connection object.
    """

    def __init__(
        self,
        config: ActiveDirectoryConfig,
        *,
        connection_factory: Any | None = None,
        ldap_module: Any | None = None,
    ):
        self.config = config
        self._connection_factory = connection_factory
        self._ldap_module = ldap_module

    async def authenticate(self, username: str, password: str) -> ActiveDirectoryIdentity:
        """Bind as ``username`` over LDAPS and read one immutable identity."""

        # Do not log the arguments: both username and password are sensitive
        # at this boundary, and the username may be an email/UPN.
        return await asyncio.to_thread(self._authenticate_sync, username, password)

    def _authenticate_sync(self, username: str, password: str) -> ActiveDirectoryIdentity:
        self.config.validate()
        if not self.config.enabled:
            raise ActiveDirectoryConfigurationError()
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise ActiveDirectoryInvalidCredentials()
        bind_name = self.config.bind_name(username)
        try:
            ldap = self._ldap_module or _import_ldap3()
        except ImportError as exc:
            raise ActiveDirectoryConfigurationError(
                "Active Directory LDAP dependency is unavailable",
                cause=exc,
            ) from exc

        endpoints = self.config.endpoint_hosts()
        last_unreachable: BaseException | None = None
        for host, port in endpoints:
            conn = None
            try:
                tls = _build_tls(ldap, self.config)
                server = _build_server(ldap, host, port, tls, self.config)
                conn = self._make_connection(ldap, server, bind_name, password, tls)
                bound = conn.bind()
                if not bound:
                    result = getattr(conn, "result", {}) or {}
                    code = str(result.get("result", ""))
                    if code in {"533", "775"}:
                        raise ActiveDirectoryAccountDisabled()
                    if code in {"49", "52", "53"}:
                        raise ActiveDirectoryInvalidCredentials()
                    raise ActiveDirectoryUnreachableError()
                identity = self._lookup_identity(conn, username)
                return identity
            except ActiveDirectoryInvalidCredentials:
                raise
            except ActiveDirectoryAccountDisabled:
                raise
            except ActiveDirectoryIdentityLookupError:
                raise
            except ActiveDirectoryTLSFailure:
                # TLS errors are deterministic for this endpoint; trying
                # another configured FQDN cannot repair a bad trust chain and
                # can obscure an operator misconfiguration.
                raise
            except (ssl.SSLError, ssl.CertificateError) as exc:
                raise ActiveDirectoryTLSFailure(cause=exc) from exc
            except Exception as exc:
                if _looks_like_disabled_bind_error(exc):
                    raise ActiveDirectoryAccountDisabled(cause=exc) from exc
                if _looks_like_invalid_bind_error(exc):
                    raise ActiveDirectoryInvalidCredentials(cause=exc) from exc
                if _looks_like_tls_error(exc):
                    raise ActiveDirectoryTLSFailure(cause=exc) from exc
                last_unreachable = exc
                continue
            finally:
                _unbind_quietly(conn)
        raise ActiveDirectoryUnreachableError(cause=last_unreachable)

    def _make_connection(
        self,
        ldap: Any,
        server: Any,
        bind_name: str,
        password: str,
        tls: Any,
    ) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory(ldap, server, bind_name, password, tls)
        kwargs = {
            "user": bind_name,
            "password": password,
            "authentication": getattr(ldap, "SIMPLE", "SIMPLE"),
            "auto_bind": False,
            "receive_timeout": self.config.operation_timeout_seconds,
        }
        try:
            return ldap.Connection(server, **kwargs)
        except TypeError:
            # Small fakes often expose only the core ldap3 constructor
            # arguments; production ldap3 supports receive_timeout.
            kwargs.pop("receive_timeout", None)
            return ldap.Connection(server, **kwargs)

    def _lookup_identity(self, conn: Any, username: str) -> ActiveDirectoryIdentity:
        ldap = self._ldap_module or _import_ldap3()
        search_scope = getattr(ldap, "SUBTREE", "SUBTREE")
        escaped_username = _safe_filter_escape(username)
        search_filter = (
            f"(&(objectCategory=person)"
            f"({self.config.login_attribute}={escaped_username}))"
        )
        attributes = [
            "objectGUID",
            self.config.login_attribute,
            "displayName",
            "mail",
            "userAccountControl",
        ]
        try:
            ok = conn.search(
                search_base=self.config.base_dn,
                search_filter=search_filter,
                search_scope=search_scope,
                attributes=attributes,
                size_limit=2,
            )
        except Exception as exc:
            if _looks_like_tls_error(exc):
                raise ActiveDirectoryTLSFailure(cause=exc) from exc
            raise ActiveDirectoryIdentityLookupError(cause=exc) from exc
        entries = list(getattr(conn, "entries", ()) or ())
        if not ok or not entries:
            raise ActiveDirectoryIdentityLookupError()
        if len(entries) > 1:
            raise ActiveDirectoryIdentityLookupError()
        entry = entries[0]
        guid = normalize_object_guid(_entry_value(entry, "objectGUID"))
        login = _entry_value(entry, self.config.login_attribute)
        if not isinstance(login, str) or not login.strip() or len(login.strip()) > 100:
            raise ActiveDirectoryIdentityLookupError()
        login = login.strip()
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in login):
            raise ActiveDirectoryIdentityLookupError()
        display_name = _entry_value(entry, "displayName")
        email = _entry_value(entry, "mail")
        uac = _entry_value(entry, "userAccountControl")
        try:
            disabled = bool(int(uac or 0) & 0x2)
        except (TypeError, ValueError):
            disabled = False
        identity = ActiveDirectoryIdentity(
            object_guid=guid,
            username=login,
            display_name=(display_name.strip() if isinstance(display_name, str) and display_name.strip() else None),
            email=(email.strip() if isinstance(email, str) and email.strip() else None),
            authority=self.config.authority,
            is_active=not disabled,
        )
        if disabled:
            raise ActiveDirectoryAccountDisabled()
        return identity

def _build_tls(ldap: Any, config: ActiveDirectoryConfig) -> Any:
    try:
        kwargs: dict[str, Any] = {
            "validate": ssl.CERT_REQUIRED,
            "version": ssl.PROTOCOL_TLS_CLIENT,
        }
        if config.ca_file:
            kwargs["ca_certs_file"] = config.ca_file
        return ldap.Tls(**kwargs)
    except (ssl.SSLError, OSError, TypeError) as exc:
        raise ActiveDirectoryTLSFailure(cause=exc) from exc


def _build_server(ldap: Any, host: str, port: int, tls: Any, config: ActiveDirectoryConfig) -> Any:
    kwargs: dict[str, Any] = {
        "use_ssl": True,
        "port": port,
        "tls": tls,
        "connect_timeout": config.connect_timeout_seconds,
        "get_info": getattr(ldap, "NONE", None),
    }
    try:
        return ldap.Server(host, **kwargs)
    except TypeError:
        kwargs.pop("get_info", None)
        return ldap.Server(host, **kwargs)


def _import_ldap3() -> Any:
    import ldap3  # type: ignore

    return ldap3


def _unbind_quietly(conn: Any) -> None:
    if conn is None:
        return
    try:
        unbind = getattr(conn, "unbind", None)
        if callable(unbind):
            unbind()
    except Exception:
        # Do not log third-party exception details at a credential boundary;
        # an LDAP adapter is not required to redact bind material.
        logger.debug("Active Directory connection cleanup failed")


def _looks_like_tls_error(exc: BaseException) -> bool:
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("ssl", "tls", "certificate", "hostname mismatch", "cert verify"))


def _looks_like_invalid_bind_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "invalid credentials",
            "invalid credential",
            "invalid creds",
            "ldap result 49",
            "resultcode=49",
            "data 52e",
        )
    )


def _looks_like_disabled_bind_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("data 533", "data 775", "resultcode=533", "resultcode=775"))


# Compatibility aliases used by integration code and focused tests.
ADConfig = ActiveDirectoryConfig
ADIdentity = ActiveDirectoryIdentity
ADClient = ActiveDirectoryClient
ActiveDirectoryTLSFailureError = ActiveDirectoryTLSFailure

__all__ = [
    "ADClient",
    "ADConfig",
    "ADIdentity",
    "ActiveDirectoryAccountDisabled",
    "ActiveDirectoryClient",
    "ActiveDirectoryConfig",
    "ActiveDirectoryConfigurationError",
    "ActiveDirectoryError",
    "ActiveDirectoryIdentity",
    "ActiveDirectoryIdentityLookupError",
    "ActiveDirectoryInvalidCredentials",
    "ActiveDirectoryProvisioningError",
    "ActiveDirectoryTLSFailure",
    "ActiveDirectoryTLSFailureError",
    "ActiveDirectoryUnreachableError",
    "DiscoveryMode",
    "normalize_object_guid",
]
