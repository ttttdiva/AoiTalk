#!/usr/bin/env python3
"""Validate the bounded, read-only Active Directory LDAPS transport.

This utility deliberately does *not* authenticate a user, perform a directory
user search, or send a write operation.  It is a transport preflight only:

* configuration is loaded from ``AOITALK_AD_*`` environment variables;
* static, explicitly allow-listed FQDNs (or a single bounded SRV lookup) are
  selected;
* a TLS 1.2+ LDAPS connection is established with certificate validation; and
* the LDAP RootDSE is read with one BASE-scope request.

No password, bind identity, service credential, object values, or certificate
details are printed.  A target operator must provide the actual AD endpoint,
CA trust, and any approved test credential separately; this script never
accepts credentials.  The default invocation only validates configuration.
Use ``--check`` to permit the bounded network probe.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import sys
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_PORT = 636
MAX_ENDPOINTS_HARD_LIMIT = 4
MAX_CONNECT_TIMEOUT_SECONDS = 15.0
MAX_OPERATION_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_OPERATION_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ENDPOINTS = 4
DEFAULT_LOGIN_ATTRIBUTE = "sAMAccountName"


class TransportConfigError(ValueError):
    """Configuration is missing or violates the transport safety policy."""


class TransportProbeError(RuntimeError):
    """A bounded transport probe failed without exposing raw details."""


@dataclass(frozen=True)
class ADTransportConfig:
    """Validated transport-only settings.

    ``servers`` and ``srv_record`` are operator-provided names.  They are
    retained only for selection and diagnostics; no credentials are modeled.
    ``base_dn`` is accepted for compatibility with application configuration,
    but this checker always reads RootDSE with an empty base DN.  Application
    authentication/search code owns its own base-DN policy.
    """

    enabled: bool = False
    discovery_mode: str = "static"
    servers: tuple[str, ...] = ()
    srv_record: str | None = None
    allowed_dns_suffixes: tuple[str, ...] = ()
    port: int = DEFAULT_PORT
    base_dn: str = ""
    login_attribute: str = DEFAULT_LOGIN_ATTRIBUTE
    bind_template: str | None = None
    ca_file: Path | None = None
    use_system_ca: bool = True
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS
    max_endpoints: int = DEFAULT_MAX_ENDPOINTS


@dataclass(frozen=True)
class ProbeResult:
    """Safe, serialisable outcome for one endpoint."""

    endpoint: str
    port: int
    status: str
    tls_version: str | None = None
    cipher: str | None = None
    rootdse: bool = False
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "endpoint": self.endpoint,
            "port": self.port,
            "status": self.status,
            "rootdse": self.rootdse,
        }
        if self.tls_version:
            payload["tls_version"] = self.tls_version
        if self.cipher:
            payload["cipher"] = self.cipher
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class ValidationReport:
    """Overall safe validator output."""

    status: str
    checked_at: str
    discovery_mode: str
    configured_endpoint_count: int
    results: tuple[ProbeResult, ...] = ()
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "discovery_mode": self.discovery_mode,
            "configured_endpoint_count": self.configured_endpoint_count,
            "results": [result.as_dict() for result in self.results],
            "errors": list(self.errors),
        }


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    normalised = value.strip().casefold()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise TransportConfigError("boolean setting has an invalid value")


def _parse_int(value: str | None, *, default: int, label: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value, 10)
    except (TypeError, ValueError) as exc:
        raise TransportConfigError(f"{label} must be an integer") from exc


def _parse_float(value: str | None, *, default: float, label: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TransportConfigError(f"{label} must be a number") from exc


def _normalise_hostname(value: str, *, label: str = "endpoint") -> str:
    """Return an ASCII lower-case FQDN, rejecting IPs and unsafe names."""

    # DNS APIs commonly return absolute names with a single trailing dot;
    # canonicalise that presentation before applying the FQDN policy.
    candidate = value.strip().rstrip(".")
    if not candidate:
        raise TransportConfigError(f"{label} must not be empty")
    if len(candidate) > 253:
        raise TransportConfigError(f"{label} must be a bounded FQDN")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise TransportConfigError(f"{label} must be an FQDN, not an IP address")
    if any(char.isspace() for char in candidate) or "/" in candidate or "\\" in candidate:
        raise TransportConfigError(f"{label} contains unsafe characters")
    try:
        ascii_name = candidate.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise TransportConfigError(f"{label} is not a valid FQDN") from exc
    labels = ascii_name.split(".")
    if len(labels) < 2 or any(
        not part
        or len(part) > 63
        or part[0] == "-"
        or part[-1] == "-"
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in part)
        for part in labels
    ):
        raise TransportConfigError(f"{label} must be a valid FQDN")
    return ascii_name


def _normalise_suffix(value: str, *, label: str = "allowed suffix") -> str:
    candidate = value.strip().lstrip(".")
    return _normalise_hostname(candidate, label=label)


def _normalise_srv_record(value: str, suffixes: Sequence[str]) -> str:
    candidate = value.strip().rstrip(".").casefold()
    prefix = "_ldaps._tcp."
    if not candidate.startswith(prefix):
        raise TransportConfigError("AOITALK_AD_SRV_RECORD must be an _ldaps._tcp record")
    suffix = _normalise_hostname(
        candidate[len(prefix) :], label="AOITALK_AD_SRV_RECORD suffix"
    )
    if not is_allowed_hostname(suffix, suffixes):
        raise TransportConfigError("AOITALK_AD_SRV_RECORD is outside the DNS allowlist")
    return f"{prefix}{suffix}"


def is_allowed_hostname(hostname: str, suffixes: Iterable[str]) -> bool:
    """Check an exact DNS-label suffix match (not a substring match)."""

    try:
        host = _normalise_hostname(hostname)
        normalised_suffixes = tuple(_normalise_suffix(item) for item in suffixes)
    except TransportConfigError:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in normalised_suffixes)


def load_config(environ: Mapping[str, str] | None = None) -> ADTransportConfig:
    """Load and validate transport configuration without network activity."""

    env = os.environ if environ is None else environ
    # A service/bind credential is deliberately not part of the Enterprise
    # transport contract.  Reject common accidental names instead of silently
    # accepting a secret that an operator expected the checker to use.
    for forbidden_key in (
        "AOITALK_AD_PASSWORD",
        "AOITALK_AD_BIND_PASSWORD",
        "AOITALK_AD_SERVICE_PASSWORD",
        "AOITALK_AD_TOKEN",
    ):
        if str(env.get(forbidden_key, "")).strip():
            raise TransportConfigError(f"{forbidden_key} is not accepted")
    enabled = _parse_bool(env.get("AOITALK_AD_ENABLED"), default=False)
    discovery_mode = (env.get("AOITALK_AD_DISCOVERY_MODE") or "static").strip().casefold()
    if discovery_mode not in {"static", "srv"}:
        raise TransportConfigError("AOITALK_AD_DISCOVERY_MODE must be static or srv")

    servers = _split_csv(env.get("AOITALK_AD_SERVERS"))
    srv_record = (env.get("AOITALK_AD_SRV_RECORD") or "").strip() or None
    suffix_values = _split_csv(
        env.get("AOITALK_AD_ALLOWED_DNS_SUFFIXES")
        or env.get("AOITALK_AD_ALLOWED_SUFFIXES")
        or env.get("AOITALK_AD_ALLOWED_SUFFIX")
    )
    # Static endpoint strings are validated even when AD is disabled.  This
    # catches a malformed generated config before an operator enables AD.
    normalised_servers = tuple(
        _normalise_hostname(server, label="AOITALK_AD_SERVERS entry") for server in servers
    )
    if len(normalised_servers) > MAX_ENDPOINTS_HARD_LIMIT:
        raise TransportConfigError(
            f"AOITALK_AD_SERVERS may contain at most {MAX_ENDPOINTS_HARD_LIMIT} entries"
        )
    normalised_suffixes = tuple(
        _normalise_suffix(suffix, label="AOITALK_AD_ALLOWED_DNS_SUFFIXES entry")
        for suffix in suffix_values
    )
    normalised_srv = (
        _normalise_srv_record(srv_record, normalised_suffixes)
        if srv_record
        else None
    )
    if discovery_mode == "srv" and normalised_srv is None and len(normalised_suffixes) == 1:
        # Keep discovery deterministic when an operator supplies exactly one
        # approved DNS suffix: the conventional LDAPS SRV name is derived
        # from that suffix, never from arbitrary DNS input.
        normalised_srv = f"_ldaps._tcp.{normalised_suffixes[0]}"

    port = _parse_int(env.get("AOITALK_AD_PORT"), default=DEFAULT_PORT, label="AOITALK_AD_PORT")
    if port != DEFAULT_PORT:
        raise TransportConfigError("LDAPS transport requires AOITALK_AD_PORT=636")
    max_endpoints = _parse_int(
        env.get("AOITALK_AD_MAX_ENDPOINTS"),
        default=DEFAULT_MAX_ENDPOINTS,
        label="AOITALK_AD_MAX_ENDPOINTS",
    )
    if not 1 <= max_endpoints <= MAX_ENDPOINTS_HARD_LIMIT:
        raise TransportConfigError(
            f"AOITALK_AD_MAX_ENDPOINTS must be between 1 and {MAX_ENDPOINTS_HARD_LIMIT}"
        )
    connect_timeout = _parse_float(
        env.get("AOITALK_AD_CONNECT_TIMEOUT_SECONDS"),
        default=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        label="AOITALK_AD_CONNECT_TIMEOUT_SECONDS",
    )
    operation_timeout = _parse_float(
        env.get("AOITALK_AD_OPERATION_TIMEOUT_SECONDS"),
        default=DEFAULT_OPERATION_TIMEOUT_SECONDS,
        label="AOITALK_AD_OPERATION_TIMEOUT_SECONDS",
    )
    if not 0.1 <= connect_timeout <= MAX_CONNECT_TIMEOUT_SECONDS:
        raise TransportConfigError(
            f"AOITALK_AD_CONNECT_TIMEOUT_SECONDS must be between 0.1 and {MAX_CONNECT_TIMEOUT_SECONDS:g}"
        )
    if not 0.1 <= operation_timeout <= MAX_OPERATION_TIMEOUT_SECONDS:
        raise TransportConfigError(
            f"AOITALK_AD_OPERATION_TIMEOUT_SECONDS must be between 0.1 and {MAX_OPERATION_TIMEOUT_SECONDS:g}"
        )

    ca_value = (env.get("AOITALK_AD_CA_FILE") or "").strip()
    ca_file = Path(ca_value).expanduser() if ca_value else None
    use_system_ca = _parse_bool(env.get("AOITALK_AD_USE_SYSTEM_CA"), default=True)
    if not use_system_ca and ca_file is None:
        raise TransportConfigError(
            "AOITALK_AD_USE_SYSTEM_CA=false requires AOITALK_AD_CA_FILE"
        )
    if ca_file is not None:
        try:
            metadata = ca_file.stat()
        except OSError as exc:
            raise TransportConfigError("AOITALK_AD_CA_FILE cannot be read") from exc
        if ca_file.is_symlink() or not ca_file.is_file() or metadata.st_size <= 0:
            raise TransportConfigError("AOITALK_AD_CA_FILE must be a non-empty regular file")

    if enabled:
        if not normalised_suffixes:
            raise TransportConfigError(
                "AOITALK_AD_ALLOWED_DNS_SUFFIXES is required when AD is enabled"
            )
        if discovery_mode == "static" and not normalised_servers:
            raise TransportConfigError(
                "AOITALK_AD_SERVERS is required for static AD discovery"
            )
        if discovery_mode == "srv" and not normalised_srv:
            raise TransportConfigError(
                "AOITALK_AD_SRV_RECORD is required for SRV AD discovery"
            )
        for server in normalised_servers:
            if not is_allowed_hostname(server, normalised_suffixes):
                raise TransportConfigError(
                    "AOITALK_AD_SERVERS contains a host outside the configured DNS allowlist"
                )
        if len(normalised_servers) > max_endpoints:
            raise TransportConfigError(
                "AOITALK_AD_SERVERS exceeds AOITALK_AD_MAX_ENDPOINTS"
            )
        if normalised_srv:
            srv_suffix = normalised_srv[len("_ldaps._tcp.") :]
            if not is_allowed_hostname(srv_suffix, normalised_suffixes):
                raise TransportConfigError(
                    "AOITALK_AD_SRV_RECORD is outside the configured DNS allowlist"
                )

    return ADTransportConfig(
        enabled=enabled,
        discovery_mode=discovery_mode,
        servers=normalised_servers,
        srv_record=normalised_srv,
        allowed_dns_suffixes=normalised_suffixes,
        port=port,
        base_dn=(env.get("AOITALK_AD_BASE_DN") or "").strip(),
        login_attribute=(env.get("AOITALK_AD_LOGIN_ATTRIBUTE") or DEFAULT_LOGIN_ATTRIBUTE).strip(),
        bind_template=(env.get("AOITALK_AD_BIND_TEMPLATE") or "").strip() or None,
        ca_file=ca_file,
        use_system_ca=use_system_ca,
        connect_timeout_seconds=connect_timeout,
        operation_timeout_seconds=operation_timeout,
        max_endpoints=max_endpoints,
    )


def _resolve_srv(config: ADTransportConfig) -> tuple[str, ...]:
    """Resolve one configured SRV name with dnspython's bounded lifetime."""

    if not config.srv_record:
        raise TransportConfigError("SRV discovery requires AOITALK_AD_SRV_RECORD")
    try:
        import dns.resolver  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TransportProbeError("SRV discovery dependency unavailable") from exc
    try:
        answers = dns.resolver.resolve(
            config.srv_record,
            "SRV",
            lifetime=config.operation_timeout_seconds,
        )
    except Exception as exc:  # dnspython has several resolver-specific errors
        raise TransportProbeError("SRV lookup failed") from exc
    candidates: list[tuple[int, int, str, int]] = []
    for answer in answers:
        try:
            target = _normalise_hostname(str(answer.target).rstrip("."), label="SRV target")
            port = int(answer.port)
            priority = int(answer.priority)
            weight = int(answer.weight)
        except (AttributeError, TypeError, ValueError, TransportConfigError) as exc:
            raise TransportProbeError("SRV response contained an invalid target") from exc
        if port != DEFAULT_PORT:
            # Never follow an SRV record to plaintext LDAP or an arbitrary port.
            continue
        if not is_allowed_hostname(target, config.allowed_dns_suffixes):
            continue
        candidates.append((priority, -weight, target, port))
    candidates.sort()
    return tuple(target for _priority, _weight, target, _port in candidates[: config.max_endpoints])


def resolve_endpoints(config: ADTransportConfig) -> tuple[str, ...]:
    """Return at most the configured maximum of allow-listed FQDNs."""

    if not config.enabled:
        return ()
    if config.discovery_mode == "static":
        return config.servers[: config.max_endpoints]
    endpoints = _resolve_srv(config)
    if not endpoints:
        raise TransportProbeError("SRV lookup returned no allow-listed LDAPS targets")
    return endpoints


def _encode_length(length: int) -> bytes:
    if length < 0:
        raise ValueError("negative BER length")
    if length < 0x80:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _ber_tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _encode_length(len(payload)) + payload


def _ber_integer(value: int) -> bytes:
    if value < 0:
        raise ValueError("only non-negative BER integers are supported")
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _ber_tlv(0x02, raw)


def build_rootdse_request(*, message_id: int = 1, time_limit_seconds: int = 10) -> bytes:
    """Build one LDAP BASE-scope RootDSE request (no credentials)."""

    # SearchRequest ::= [APPLICATION 3] SEQUENCE {
    #   baseObject LDAPDN, scope ENUMERATED, derefAliases ENUMERATED,
    #   sizeLimit INTEGER, timeLimit INTEGER, typesOnly BOOLEAN,
    #   filter Filter, attributes AttributeSelection }
    attrs = _ber_tlv(
        0x30,
        b"".join(
            _ber_tlv(0x04, value.encode("ascii"))
            for value in (
                "supportedLDAPVersion",
                "defaultNamingContext",
                "rootDomainNamingContext",
            )
        ),
    )
    request = b"".join(
        (
            _ber_tlv(0x04, b""),
            _ber_tlv(0x0A, b"\x00"),
            _ber_tlv(0x0A, b"\x00"),
            _ber_integer(1),
            _ber_integer(max(1, int(time_limit_seconds))),
            _ber_tlv(0x01, b"\x00"),
            # present(objectClass) context-specific primitive tag [7].
            _ber_tlv(0x87, b"objectClass"),
            attrs,
        )
    )
    protocol_op = _ber_tlv(0x63, _ber_tlv(0x30, request))
    # LDAPMessage ::= SEQUENCE { messageID MessageID, protocolOp CHOICE }
    return _ber_tlv(0x30, _ber_integer(message_id) + protocol_op)


def _read_exact(sock: socket.socket, length: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        timeout = deadline - __import__("time").monotonic()
        if timeout <= 0:
            raise TimeoutError("LDAP response timeout")
        sock.settimeout(timeout)
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("LDAP peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_ber_message(sock: socket.socket, deadline: float, *, max_bytes: int = 65536) -> bytes:
    first = _read_exact(sock, 2, deadline)
    if first[0] != 0x30:
        raise TransportProbeError("LDAP response was not a sequence")
    length_octet = first[1]
    if length_octet == 0x80:
        raise TransportProbeError("LDAP indefinite BER length is not permitted")
    if length_octet & 0x80:
        width = length_octet & 0x7F
        if width == 0 or width > 4:
            raise TransportProbeError("LDAP BER length is out of bounds")
        raw_length = _read_exact(sock, width, deadline)
        body_length = int.from_bytes(raw_length, "big")
        header = first + raw_length
    else:
        body_length = length_octet
        header = first
    if body_length < 2 or body_length > max_bytes:
        raise TransportProbeError("LDAP response exceeds the bounded size")
    return header + _read_exact(sock, body_length, deadline)


def _parse_tlv(payload: bytes, offset: int = 0) -> tuple[int, bytes, int]:
    if offset + 2 > len(payload):
        raise TransportProbeError("LDAP response was truncated")
    tag = payload[offset]
    length_octet = payload[offset + 1]
    cursor = offset + 2
    if length_octet == 0x80:
        raise TransportProbeError("LDAP response used an indefinite BER length")
    if length_octet & 0x80:
        width = length_octet & 0x7F
        if width == 0 or width > 4 or cursor + width > len(payload):
            raise TransportProbeError("LDAP response BER length is invalid")
        length = int.from_bytes(payload[cursor : cursor + width], "big")
        cursor += width
    else:
        length = length_octet
    end = cursor + length
    if end > len(payload):
        raise TransportProbeError("LDAP response was truncated")
    return tag, payload[cursor:end], end


def rootdse_result_code(message: bytes) -> int | None:
    """Extract LDAP resultCode from a SearchResultDone response."""

    message_tag, message_body, end = _parse_tlv(message)
    if message_tag != 0x30 or end != len(message):
        raise TransportProbeError("LDAP response outer sequence was malformed")
    message_id_tag, _message_id, cursor = _parse_tlv(message_body)
    if message_id_tag != 0x02:
        raise TransportProbeError("LDAP response message id was malformed")
    if cursor >= len(message_body):
        raise TransportProbeError("LDAP response had no protocol operation")
    protocol_tag, protocol_body, _ = _parse_tlv(message_body, cursor)
    if protocol_tag != 0x65:  # SearchResultDone
        return None
    _result_sequence_tag, result_body, _ = _parse_tlv(protocol_body)
    result_code_tag, result_code_body, _ = _parse_tlv(result_body)
    if result_code_tag != 0x0A or not result_code_body:
        raise TransportProbeError("LDAP resultCode was malformed")
    return int.from_bytes(result_code_body, "big", signed=False)


def _tls_context(config: ADTransportConfig) -> ssl.SSLContext:
    try:
        context = (
            ssl.create_default_context(cafile=str(config.ca_file))
            if config.ca_file is not None
            else ssl.create_default_context()
        )
        # ``create_default_context(cafile=...)`` intentionally uses the
        # supplied bundle alone.  When operators request both an explicit CA
        # and the system trust store, load the latter as an additive source.
        if config.ca_file is not None and config.use_system_ca:
            context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    except (OSError, ssl.SSLError) as exc:
        raise TransportConfigError("unable to construct the configured CA trust context") from exc
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    # A client certificate or key would turn this into an unapproved bind
    # path.  This checker intentionally never loads one.
    return context


def probe_endpoint(config: ADTransportConfig, endpoint: str) -> ProbeResult:
    """Probe one allow-listed endpoint with TLS and RootDSE only."""

    if not is_allowed_hostname(endpoint, config.allowed_dns_suffixes):
        return ProbeResult(endpoint, config.port, "allowlist_rejected", detail="endpoint outside allowlist")
    context = _tls_context(config)
    import time

    deadline = time.monotonic() + config.operation_timeout_seconds
    raw_socket: socket.socket | None = None
    tls_socket: ssl.SSLSocket | None = None
    try:
        raw_socket = socket.create_connection(
            (endpoint, config.port), timeout=config.connect_timeout_seconds
        )
        remaining = max(0.1, deadline - time.monotonic())
        raw_socket.settimeout(remaining)
        tls_socket = context.wrap_socket(raw_socket, server_hostname=endpoint)
        raw_socket = None
        tls_version = tls_socket.version()
        cipher_info = tls_socket.cipher()
        cipher = cipher_info[0] if cipher_info else None
        tls_socket.sendall(
            build_rootdse_request(
                time_limit_seconds=max(1, int(config.operation_timeout_seconds))
            )
        )
        while True:
            message = _read_ber_message(tls_socket, deadline)
            code = rootdse_result_code(message)
            if code is None:
                continue
            if code != 0:
                return ProbeResult(
                    endpoint,
                    config.port,
                    "rootdse_rejected",
                    tls_version=tls_version,
                    cipher=cipher,
                    detail=f"LDAP result code {code}",
                )
            return ProbeResult(
                endpoint,
                config.port,
                "ok",
                tls_version=tls_version,
                cipher=cipher,
                rootdse=True,
            )
    except socket.timeout:
        return ProbeResult(endpoint, config.port, "timeout", detail="bounded transport timeout")
    except ssl.CertificateError:
        return ProbeResult(endpoint, config.port, "tls_certificate_failure", detail="certificate validation failed")
    except ssl.SSLError:
        return ProbeResult(endpoint, config.port, "tls_failure", detail="TLS handshake failed")
    except (OSError, ConnectionError, TimeoutError):
        return ProbeResult(endpoint, config.port, "unreachable", detail="endpoint could not be reached")
    except TransportProbeError as exc:
        return ProbeResult(endpoint, config.port, "rootdse_failure", detail=str(exc))
    finally:
        if tls_socket is not None:
            try:
                tls_socket.close()
            except OSError:
                pass
        if raw_socket is not None:
            try:
                raw_socket.close()
            except OSError:
                pass


def validate_transport(config: ADTransportConfig, *, check_network: bool) -> ValidationReport:
    """Validate config and optionally execute the bounded network check."""

    checked_at = datetime.now(timezone.utc).isoformat()
    if not config.enabled:
        return ValidationReport(
            status="disabled",
            checked_at=checked_at,
            discovery_mode=config.discovery_mode,
            configured_endpoint_count=0,
        )
    if not check_network:
        count = len(config.servers) if config.discovery_mode == "static" else 0
        return ValidationReport(
            status="configuration_valid",
            checked_at=checked_at,
            discovery_mode=config.discovery_mode,
            configured_endpoint_count=count,
        )
    try:
        endpoints = resolve_endpoints(config)
    except (TransportConfigError, TransportProbeError) as exc:
        return ValidationReport(
            status="failed",
            checked_at=checked_at,
            discovery_mode=config.discovery_mode,
            configured_endpoint_count=0,
            errors=(str(exc),),
        )
    results = tuple(probe_endpoint(config, endpoint) for endpoint in endpoints)
    status = "ok" if results and all(item.status == "ok" for item in results) else "failed"
    return ValidationReport(
        status=status,
        checked_at=checked_at,
        discovery_mode=config.discovery_mode,
        configured_endpoint_count=len(endpoints),
        results=results,
    )


def _redacted_config(config: ADTransportConfig) -> dict[str, Any]:
    """Render config metadata without bind templates, DNs, or CA contents."""

    return {
        "enabled": config.enabled,
        "discovery_mode": config.discovery_mode,
        "server_count": len(config.servers),
        "srv_configured": bool(config.srv_record),
        "allowed_suffix_count": len(config.allowed_dns_suffixes),
        "port": config.port,
        "ca_file_configured": config.ca_file is not None,
        "use_system_ca": config.use_system_ca,
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "operation_timeout_seconds": config.operation_timeout_seconds,
        "max_endpoints": config.max_endpoints,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="perform the bounded LDAPS/TLS + RootDSE probe (otherwise config-only)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable redacted JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config()
        report = validate_transport(config, check_network=args.check)
    except TransportConfigError as exc:
        report = ValidationReport(
            status="configuration_error",
            checked_at=datetime.now(timezone.utc).isoformat(),
            discovery_mode="unknown",
            configured_endpoint_count=0,
            errors=(str(exc),),
        )

    payload = report.as_dict()
    payload["config"] = _redacted_config(config) if "config" in locals() else None
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        print(f"AD transport status: {report.status}")
        if config := locals().get("config"):
            print(json.dumps(_redacted_config(config), ensure_ascii=True, sort_keys=True))
        for result in report.results:
            print(f"- {result.endpoint}:{result.port} {result.status}")
        for error in report.errors:
            print(f"- {error}", file=sys.stderr)
    return 0 if report.status in {"disabled", "configuration_valid", "ok"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
