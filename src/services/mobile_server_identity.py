"""Durable server identity proof for the native mobile client.

The mobile client can reach one AoiTalk installation through several network
aliases (for example a LAN address and a portal URL).  A URL fingerprint is
therefore only a routing hint.  This service owns the installation identity
key and signs a client supplied nonce so the client can bind all durable
mobile data to the installation that it explicitly trusted.

The private key is generated once and kept in an existing ``data/`` storage
area.  The path is partitioned by a digest of the active PostgreSQL endpoint,
database, schema, and readonly connection address/port when those values are
available.  A database or endpoint switch consequently cannot silently reuse
another installation's key.  The response contains no private material and
the nonce is never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

try:  # SQLAlchemy is present in the server runtime, but keep unit imports light.
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
except Exception:  # pragma: no cover - exercised only by minimal import harnesses
    text = None  # type: ignore[assignment]
    make_url = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

IDENTITY_PROTOCOL_VERSION = 2
IDENTITY_ALGORITHM = "Ed25519"
IDENTITY_DOMAIN = b"AoiTalk-Mobile-Server-Identity/v2\x00"
IDENTITY_FILE_VERSION = 1
DEFAULT_IDENTITY_FILENAME = "mobile_server_identity"
NONCE_MIN_LENGTH = 16
NONCE_MAX_LENGTH = 256
_NONCE_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_ALIAS_ENV = "AOITALK_MOBILE_IDENTITY_ALIASES"


class MobileServerIdentityError(RuntimeError):
    """Base error for identity storage/proof failures."""


class MobileServerIdentityUnavailable(MobileServerIdentityError):
    """The durable identity could not be loaded or created safely."""


class MobileServerIdentityStorageMismatch(MobileServerIdentityUnavailable):
    """A key file was created for a different durable storage scope."""


class MobileServerIdentityAliasNotApproved(MobileServerIdentityError):
    """A requested namespace is not in the server-owned approved aliases."""


def _harden_identity_file_acl(path: Path) -> None:
    """Restrict the private identity file before any key bytes are written.

    POSIX uses mode 0600.  On Windows, ``chmod`` does not remove inherited
    NTFS ACEs, so use the built-in ``icacls`` command with only the current
    account, Administrators, and SYSTEM.  Command output is captured and
    discarded; the path is non-secret, while key contents never enter argv or
    logs.
    """

    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise MobileServerIdentityUnavailable(
                "identity storage permissions are unavailable"
            ) from exc
        return

    try:
        owner = subprocess.run(
            ["whoami"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise MobileServerIdentityUnavailable(
            "identity storage owner is unavailable"
        ) from exc
    if not owner or len(owner) > 256 or any(ord(char) < 32 for char in owner):
        raise MobileServerIdentityUnavailable("identity storage owner is invalid")
    try:
        result = subprocess.run(
            [
                "icacls",
                os.fspath(path),
                "/inheritance:r",
                "/grant:r",
                f"{owner}:F",
                "BUILTIN\\Administrators:F",
                "NT AUTHORITY\\SYSTEM:F",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise MobileServerIdentityUnavailable(
            "identity storage ACL is unavailable"
        ) from exc
    if result.returncode != 0:
        raise MobileServerIdentityUnavailable("identity storage ACL is unavailable")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: Any, *, expected_length: int, field: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise MobileServerIdentityUnavailable(f"invalid identity {field}")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise MobileServerIdentityUnavailable(f"invalid identity {field}") from exc
    if len(decoded) != expected_length:
        raise MobileServerIdentityUnavailable(f"invalid identity {field}")
    return decoded


def normalize_nonce(value: str) -> str:
    """Validate the opaque, non-secret challenge nonce.

    The mobile client generates a high entropy nonce.  Keeping the server
    validation ASCII-only avoids signing ambiguous Unicode normalizations and
    prevents control characters from entering a URL or log line.
    """

    nonce = str(value or "")
    if (
        len(nonce) < NONCE_MIN_LENGTH
        or len(nonce) > NONCE_MAX_LENGTH
        or not _NONCE_RE.fullmatch(nonce)
    ):
        raise ValueError("invalid identity nonce")
    return nonce


def normalize_target_namespace(value: str) -> str:
    """Normalize an approved full HTTP(S) namespace without trusting headers."""

    raw = str(value or "").strip()
    if not raw or "\\" in raw:
        raise ValueError("invalid identity target namespace")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("invalid identity target namespace")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid identity target namespace")
    if parsed.query or parsed.fragment:
        raise ValueError("invalid identity target namespace")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid identity target namespace") from exc
    path = parsed.path or "/"
    for segment in path.split("/"):
        decoded = unquote(segment)
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            raise ValueError("invalid identity target namespace")
    path = re.sub(r"/+", "/", path).rstrip("/") or "/"
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{scheme}://{authority}{'' if path == '/' else path}"


def identity_challenge_message(
    server_id: str,
    nonce: str,
    target_namespace: str,
) -> bytes:
    """Return the versioned bytes signed by the server key."""

    normalized_id = str(server_id or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized_id):
        raise ValueError("invalid server identity")
    normalized_nonce = normalize_nonce(nonce)
    normalized_target = normalize_target_namespace(target_namespace)
    return (
        IDENTITY_DOMAIN
        + normalized_id.encode("ascii")
        + b"\x00"
        + normalized_nonce.encode("ascii")
        + b"\x00"
        + normalized_target.encode("utf-8")
    )


def verify_identity_shape(value: Mapping[str, Any]) -> bool:
    """Check the non-cryptographic shape of a proof response.

    Actual Ed25519 verification belongs to the native client.  This helper is
    intentionally strict so clients can fail closed before invoking their
    platform verifier.
    """

    if value.get("protocol_version") != IDENTITY_PROTOCOL_VERSION:
        return False
    if value.get("algorithm") != IDENTITY_ALGORITHM:
        return False
    server_id = value.get("server_id")
    nonce = value.get("nonce")
    target_namespace = value.get("target_namespace")
    public_key = value.get("public_key")
    signature = value.get("signature")
    if not isinstance(server_id, str) or not _DIGEST_RE.fullmatch(server_id.lower()):
        return False
    if not isinstance(nonce, str) or not isinstance(target_namespace, str):
        return False
    try:
        normalize_nonce(nonce)
        normalize_target_namespace(target_namespace)
        public_bytes = _b64decode(public_key, expected_length=32, field="public_key")
        signature_bytes = _b64decode(signature, expected_length=64, field="signature")
    except (ValueError, MobileServerIdentityUnavailable):
        return False
    return hashlib.sha256(public_bytes).hexdigest() == server_id.lower() and len(signature_bytes) == 64


class MobileServerIdentityService:
    """Load/create one durable Ed25519 identity and sign mobile challenges."""

    def __init__(
        self,
        *,
        db_manager: Any | None = None,
        storage_path: str | os.PathLike[str] | None = None,
        storage_scope: str | None = None,
        gateway_services_config_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._db_manager = db_manager
        configured_path = storage_path or os.environ.get(
            "AOITALK_MOBILE_SERVER_IDENTITY_PATH",
            os.environ.get("AOITALK_SERVER_IDENTITY_PATH", ""),
        )
        self._configured_path = (
            Path(configured_path).expanduser().resolve() if configured_path else None
        )
        self._configured_scope = str(storage_scope or "").strip() or None
        configured_gateway_path = gateway_services_config_path or os.environ.get(
            "AOITALK_GATEWAY_SERVICES_CONFIG_PATH",
            "",
        )
        self._gateway_services_config_path = (
            Path(configured_gateway_path).expanduser().resolve()
            if configured_gateway_path
            else Path(__file__).resolve().parents[3]
            / "85_CaddyGateway"
            / "config"
            / "services.json"
        )
        self._identity: dict[str, Any] | None = None
        self._identity_path: Path | None = None
        self._storage_scope: str | None = None
        self._load_lock = threading.RLock()

    @staticmethod
    def _implicit_target_namespaces(request: Any | None) -> set[str]:
        if request is None:
            return set()
        scope = getattr(request, "scope", {}) or {}
        server = scope.get("server")
        if not isinstance(server, (tuple, list)) or len(server) < 2:
            return set()
        host = str(server[0] or "").strip()
        try:
            port = int(server[1])
        except (TypeError, ValueError):
            return set()
        if not host or port < 1 or port > 65535:
            return set()
        scheme = str(scope.get("scheme") or "http").strip().lower()
        if scheme not in {"http", "https"}:
            scheme = "http"
        root_path = str(scope.get("root_path") or "").strip() or "/"
        hosts = {"127.0.0.1", "::1", "localhost"}
        if host not in {"0.0.0.0", "::", "", "[::]"}:
            hosts.add(host)
        try:
            local_name = socket.gethostname()
            for item in socket.getaddrinfo(local_name, None):
                address = str(item[4][0] or "").strip()
                if address and address not in {"0.0.0.0", "::"}:
                    hosts.add(address)
        except OSError:
            pass
        aliases: set[str] = set()
        for candidate in hosts:
            authority = f"[{candidate}]" if ":" in candidate and not candidate.startswith("[") else candidate
            try:
                aliases.add(normalize_target_namespace(f"{scheme}://{authority}:{port}{root_path}"))
            except ValueError:
                continue
        return aliases

    def approved_target_namespaces(self, request: Any | None = None) -> set[str]:
        """Return only operator-owned aliases plus kernel-bound local origins."""

        aliases: set[str] = set()
        raw = os.environ.get(_IDENTITY_ALIAS_ENV)
        if raw is not None and raw.strip():
            try:
                configured = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise MobileServerIdentityUnavailable(
                    "identity alias configuration is invalid"
                ) from exc
            if not isinstance(configured, list) or not all(
                isinstance(item, str) for item in configured
            ):
                raise MobileServerIdentityUnavailable(
                    "identity alias configuration is invalid"
                )
            for item in configured:
                try:
                    aliases.add(normalize_target_namespace(item))
                except ValueError as exc:
                    raise MobileServerIdentityUnavailable(
                        "identity alias configuration is invalid"
                    ) from exc
        aliases.update(self._implicit_target_namespaces(request))
        aliases.update(self._gateway_target_namespaces(request))
        if not aliases:
            raise MobileServerIdentityUnavailable("identity aliases are unavailable")
        return aliases

    @staticmethod
    def _local_backend_host(host: str) -> bool:
        normalized = str(host or "").strip().lower().strip("[]")
        if normalized in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            import ipaddress

            address = ipaddress.ip_address(normalized)
            if address.is_loopback:
                return True
        except ValueError:
            pass
        try:
            local_hosts = {socket.gethostname().lower()}
            for item in socket.getaddrinfo(socket.gethostname(), None):
                local_hosts.add(str(item[4][0] or "").strip().lower().strip("[]"))
            return normalized in local_hosts
        except OSError:
            return False

    @staticmethod
    def _service_port_matches(request: Any | None, upstream: Any) -> bool:
        if request is None or not isinstance(upstream, str):
            return False
        try:
            parsed = urlsplit(f"//{upstream.strip()}")
            upstream_host = parsed.hostname or ""
            upstream_port = parsed.port
            server = (getattr(request, "scope", {}) or {}).get("server")
            server_port = int(server[1]) if isinstance(server, (tuple, list)) and len(server) > 1 else None
        except (TypeError, ValueError):
            return False
        return bool(
            upstream_port
            and server_port == upstream_port
            and MobileServerIdentityService._local_backend_host(upstream_host)
        )

    def _gateway_target_namespaces(self, request: Any | None) -> set[str]:
        """Read adjacent Gateway declarations without trusting request headers."""

        path = self._gateway_services_config_path
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return set()
        if not isinstance(config, dict):
            return set()
        site = config.get("site")
        services = config.get("services")
        if not isinstance(site, dict) or not isinstance(services, list):
            return set()
        service = next(
            (item for item in services if isinstance(item, dict) and item.get("id") == "at"),
            None,
        )
        if not isinstance(service, dict):
            return set()
        connection = service.get("connection")
        if not isinstance(connection, dict) or connection.get("enabled") is not True:
            return set()
        if not self._service_port_matches(request, connection.get("apiUpstream")):
            return set()
        prefix = service.get("publicPrefix")
        if not isinstance(prefix, str) or not prefix.startswith("/"):
            return set()
        aliases: set[str] = set()

        def add_origin(origin: Any) -> None:
            if not isinstance(origin, str):
                return
            try:
                parsed = urlsplit(origin.strip())
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    return
                normalized_origin = normalize_target_namespace(
                    f"{parsed.scheme}://{parsed.netloc}"
                )
                upstream_origin = service.get("upstreamOrigin")
                if isinstance(upstream_origin, str):
                    upstream_parsed = urlsplit(upstream_origin)
                    if (
                        upstream_parsed.scheme
                        and upstream_parsed.hostname
                        and normalize_target_namespace(
                            f"{upstream_parsed.scheme}://{upstream_parsed.netloc}"
                        )
                        == normalized_origin
                    ):
                        return
                aliases.add(normalize_target_namespace(f"{normalized_origin}{prefix}"))
            except (TypeError, ValueError):
                return

        add_origin(site.get("origin"))
        transport_origins = site.get("transportOrigins")
        if isinstance(transport_origins, list):
            for origin in transport_origins:
                add_origin(origin)

        profile_path = path.parent / "profiles" / "ddns.json"
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            public_host = profile.get("publicHost") if isinstance(profile, dict) else None
            ports = profile.get("ports") if isinstance(profile, dict) else None
            at_port = ports.get("at") if isinstance(ports, dict) else None
            if isinstance(public_host, str) and isinstance(at_port, int) and 1 <= at_port <= 65535:
                add_origin(f"https://{public_host}:{at_port}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return aliases

    async def challenge(
        self,
        nonce: str,
        target_namespace: str | None = None,
        *,
        request: Any | None = None,
    ) -> dict[str, Any]:
        """Sign ``nonce`` only for an operator-approved target namespace."""

        normalized_nonce = normalize_nonce(nonce)
        approved = self.approved_target_namespaces(request)
        if target_namespace is None:
            if len(approved) != 1:
                raise MobileServerIdentityAliasNotApproved(
                    "identity target namespace is required"
                )
            normalized_target = next(iter(approved))
        else:
            try:
                normalized_target = normalize_target_namespace(target_namespace)
            except ValueError as exc:
                raise MobileServerIdentityAliasNotApproved(
                    "identity target namespace is invalid"
                ) from exc
        if normalized_target not in approved:
            raise MobileServerIdentityAliasNotApproved(
                "identity target namespace is not approved"
            )
        identity = await self._ensure_identity()
        message = identity_challenge_message(
            identity["server_id"],
            normalized_nonce,
            normalized_target,
        )
        private_key = identity["private_key"]
        signature = private_key.sign(message)
        # Do not include the raw message or private key in a response.  The
        # client reconstructs the exact versioned message locally.
        return {
            "protocol_version": IDENTITY_PROTOCOL_VERSION,
            "algorithm": IDENTITY_ALGORITHM,
            "server_id": identity["server_id"],
            "public_key": identity["public_key"],
            "nonce": normalized_nonce,
            "target_namespace": normalized_target,
            "signature": _b64encode(signature),
        }

    async def server_metadata(self) -> dict[str, Any]:
        """Return stable public identity metadata without a challenge."""

        identity = await self._ensure_identity()
        return {
            "protocol_version": IDENTITY_PROTOCOL_VERSION,
            "algorithm": IDENTITY_ALGORITHM,
            "server_id": identity["server_id"],
            "public_key": identity["public_key"],
        }

    async def _ensure_identity(self) -> dict[str, Any]:
        storage_scope = await self._resolve_storage_scope()
        path = self._path_for_scope(storage_scope)
        with self._load_lock:
            if (
                self._identity is not None
                and self._identity_path == path
                and self._storage_scope == storage_scope
            ):
                return self._identity
            identity = self._load_or_create(path, storage_scope)
            self._identity = identity
            self._identity_path = path
            self._storage_scope = storage_scope
            return identity

    async def _resolve_storage_scope(self) -> str:
        if self._configured_scope:
            return self._normalize_scope(self._configured_scope)

        manager = self._db_manager
        get_session = getattr(manager, "get_session", None)
        if manager is not None and (not callable(get_session) or text is None):
            raise MobileServerIdentityUnavailable(
                "database identity scope is unavailable"
            )
        if manager is not None:
            session = None
            try:
                session = await get_session()
                result = await session.execute(
                    text(
                        "select current_database() as database_name, "
                        "current_schema() as schema_name, "
                        "coalesce(inet_server_addr()::text, '') as server_address, "
                        "coalesce(inet_server_port(), 0) as server_port"
                    )
                )
                row = result.first()
                if row is None:
                    raise MobileServerIdentityUnavailable(
                        "database identity scope is unavailable"
                    )
                database = getattr(row, "database_name", None)
                schema = getattr(row, "schema_name", None)
                server_address = getattr(row, "server_address", None)
                server_port = getattr(row, "server_port", None)
                if database is None or schema is None:
                    database, schema = row[0], row[1]
                if server_address is None:
                    server_address = row[2]
                if server_port is None:
                    server_port = row[3]
                database_text = str(database or "").strip()
                schema_text = str(schema or "").strip()
                server_address_text = str(server_address or "").strip()
                server_port_text = str(server_port or "").strip()
                endpoint = self._database_endpoint(manager)
                if not all(
                    (
                        database_text,
                        schema_text,
                        server_address_text,
                        server_port_text,
                        endpoint,
                    )
                ):
                    raise MobileServerIdentityUnavailable(
                        "database identity scope is unavailable"
                    )
                return self._scope_digest(
                    "postgresql\x00"
                    f"endpoint={endpoint}\x00"
                    f"database={database_text}\x00"
                    f"schema={schema_text}\x00"
                    f"server={server_address_text}:{server_port_text}"
                )
            except MobileServerIdentityUnavailable:
                raise
            except Exception:
                # A live DB manager exists, so an unknown DB/cluster identity
                # must fail closed. Falling back to an environment DSN here
                # could reuse a key after a cluster switch or credential
                # rotation.
                logger.debug("Mobile server identity DB scope probe unavailable")
                raise MobileServerIdentityUnavailable(
                    "database identity scope is unavailable"
                ) from None
            finally:
                if session is not None:
                    try:
                        await session.close()
                    except Exception:
                        logger.debug("Mobile server identity DB session close failed")

        # A no-DB/legacy server may use an explicitly selected identity file.
        # The file path itself is the persistent binding; AOITALK_DATA_DIR
        # alone is not sufficient authority to identify a server.
        if self._configured_path is not None:
            return self._scope_digest(
                f"filesystem\x00{self._configured_path.as_posix()}"
            )
        raise MobileServerIdentityUnavailable(
            "database identity scope is unavailable"
        )

    @staticmethod
    def _database_endpoint(manager: Any) -> str:
        """Return a credential-free effective DB endpoint binding."""

        raw_url = str(getattr(manager, "database_url", "") or "").strip()
        if raw_url and make_url is not None:
            try:
                parsed = make_url(raw_url)
                driver = str(parsed.drivername or "").split("+", 1)[0]
                host = str(parsed.host or "").strip().lower()
                port = str(parsed.port or "").strip()
                database = str(parsed.database or "").strip()
                if host and database:
                    return f"{driver}\x00{host}\x00{port}\x00{database}"
            except Exception:
                pass

        config = getattr(manager, "config", None)
        host = str(getattr(config, "postgres_host", "") or "").strip().lower()
        port = str(getattr(config, "postgres_port", "") or "").strip()
        database = str(getattr(config, "postgres_db", "") or "").strip()
        if host and database:
            return f"postgresql\x00{host}\x00{port}\x00{database}"
        return ""

    @staticmethod
    def _scope_digest(value: str) -> str:
        return "scope-" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_scope(value: str) -> str:
        text_value = str(value or "").strip()
        if not text_value or len(text_value) > 160:
            raise MobileServerIdentityUnavailable("invalid identity storage scope")
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", text_value):
            raise MobileServerIdentityUnavailable("invalid identity storage scope")
        return text_value

    def _path_for_scope(self, storage_scope: str) -> Path:
        if self._configured_path is not None:
            return self._configured_path
        root_value = os.environ.get("AOITALK_DATA_DIR", "")
        if root_value:
            root = Path(root_value).expanduser().resolve()
        else:
            root = Path(__file__).resolve().parents[2] / "data"
        digest = hashlib.sha256(storage_scope.encode("ascii")).hexdigest()[:32]
        return root / f"{DEFAULT_IDENTITY_FILENAME}-{digest}.json"

    @staticmethod
    def _load_or_create(path: Path, storage_scope: str) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        lock_fd: int | None = None
        started = time.monotonic()
        while lock_fd is None:
            try:
                lock_fd = os.open(
                    os.fspath(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                # A crashed process must not leave a lock forever.  The lock
                # contains no secret; only age is inspected for recovery.
                try:
                    if time.time() - lock_path.stat().st_mtime > 30:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() - started > 10:
                    raise MobileServerIdentityUnavailable("identity storage is locked")
                time.sleep(0.02)
        try:
            if path.exists():
                _harden_identity_file_acl(path)
                raw = path.read_text(encoding="utf-8")
            else:
                # A missing file is created below with a 0600 descriptor,
                # then hardened before the private key payload is written.
                raw = None
            if raw is not None:
                if not raw.strip():
                    raise MobileServerIdentityUnavailable(
                        "identity storage is empty"
                    )
                return MobileServerIdentityService._parse_identity(raw, storage_scope)
            identity = MobileServerIdentityService._generate_identity(storage_scope)
            payload = {
                "version": IDENTITY_FILE_VERSION,
                "algorithm": IDENTITY_ALGORITHM,
                "storage_scope": storage_scope,
                "server_id": identity["server_id"],
                "public_key": identity["public_key"],
                "private_key": _b64encode(
                    identity["private_key"].private_bytes(
                        serialization.Encoding.Raw,
                        serialization.PrivateFormat.Raw,
                        serialization.NoEncryption(),
                    )
                ),
            }
            temp_path = path.with_name(
                f".{path.name}.{secrets.token_hex(8)}.tmp"
            )
            temp_fd: int | None = None
            try:
                temp_fd = os.open(
                    os.fspath(temp_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                _harden_identity_file_acl(temp_path)
                with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as handle:
                    temp_fd = None
                    json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
                _harden_identity_file_acl(path)
            finally:
                if temp_fd is not None:
                    try:
                        os.close(temp_fd)
                    except OSError:
                        pass
                temp_path.unlink(missing_ok=True)
            return identity
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _generate_identity(storage_scope: str) -> dict[str, Any]:
        private_key = Ed25519PrivateKey.generate()
        public_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return {
            "private_key": private_key,
            "public_key": _b64encode(public_bytes),
            "server_id": hashlib.sha256(public_bytes).hexdigest(),
            "storage_scope": storage_scope,
        }

    @staticmethod
    def _parse_identity(raw: str, storage_scope: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MobileServerIdentityUnavailable("identity storage is invalid") from exc
        if not isinstance(payload, dict):
            raise MobileServerIdentityUnavailable("identity storage is invalid")
        if payload.get("version") != IDENTITY_FILE_VERSION or payload.get("algorithm") != IDENTITY_ALGORITHM:
            raise MobileServerIdentityUnavailable("identity storage version is unsupported")
        persisted_scope = payload.get("storage_scope")
        if persisted_scope != storage_scope:
            raise MobileServerIdentityStorageMismatch("identity storage scope changed")
        private_bytes = _b64decode(payload.get("private_key"), expected_length=32, field="private_key")
        public_bytes = _b64decode(payload.get("public_key"), expected_length=32, field="public_key")
        expected_server_id = hashlib.sha256(public_bytes).hexdigest()
        if payload.get("server_id") != expected_server_id:
            raise MobileServerIdentityUnavailable("identity storage fingerprint is invalid")
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        derived_public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if not secrets.compare_digest(derived_public, public_bytes):
            raise MobileServerIdentityUnavailable("identity storage key pair is invalid")
        return {
            "private_key": private_key,
            "public_key": _b64encode(public_bytes),
            "server_id": expected_server_id,
            "storage_scope": storage_scope,
        }


__all__ = [
    "IDENTITY_ALGORITHM",
    "IDENTITY_DOMAIN",
    "IDENTITY_PROTOCOL_VERSION",
    "MobileServerIdentityError",
    "MobileServerIdentityAliasNotApproved",
    "MobileServerIdentityService",
    "MobileServerIdentityStorageMismatch",
    "MobileServerIdentityUnavailable",
    "identity_challenge_message",
    "normalize_target_namespace",
    "normalize_nonce",
    "verify_identity_shape",
]
