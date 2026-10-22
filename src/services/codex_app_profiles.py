"""Server-owned, disabled-by-default Codex connection boundary.

Handoff is an inert disclosure intent, never a login RPC, approval or token
import. Public CLI responses cannot attest a daemon executable or its startup
credential store. Only the concrete owned official connector can activate a
consumed approval; the default deployment supplies no connector or owner/root.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


OAUTH_SCOPES = ("openid", "profile", "email", "offline_access",
                "api.connectors.read", "api.connectors.invoke")
DISCLOSURE_VERSION = 1
CONNECTION_CLI_VERSION = "0.160.0"
# OS plumbing only: no inherited credentials, proxy, issuer, endpoint, refresh,
# revoke, CLI configuration or arbitrary CODEX_* variables.
CHILD_ENV_KEYS = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
                            "TEMP", "TMP", "TMPDIR", "USERPROFILE", "APPDATA",
                            "LOCALAPPDATA", "HOME", "LANG", "LC_ALL", "LC_CTYPE"})


class CodexProfileError(RuntimeError):
    def __init__(self, code: str, http_status: int = 409):
        super().__init__(code)
        self.code, self.http_status = code, http_status


def _owner(value: str) -> str:
    try:
        normalized = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise CodexProfileError("codex_profile_owner_required", 403) from None
    if value != normalized:
        raise CodexProfileError("codex_profile_owner_required", 403)
    return normalized


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _directory(path: Path) -> tuple[int, int]:
    try:
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode)
                or path.is_symlink() or int(getattr(info, "st_file_attributes", 0)) & 0x400
                or path.resolve(strict=True) != path):
            raise OSError()
        # Inspect every parent as well; a canonical child below a replaced
        # directory must not silently acquire another credential namespace.
        for parent in path.parents:
            if parent.is_symlink() or int(getattr(parent.lstat(), "st_file_attributes", 0)) & 0x400:
                raise OSError()
        return info.st_dev, info.st_ino
    except (OSError, ValueError, RuntimeError):
        raise CodexProfileError("codex_profile_storage_unverified") from None


def _isolated_home(home: Path) -> None:
    # Metadata only: never read instructions or authentication files.
    for name in ("environments.toml", "AGENTS.md", "AGENTS.override.md", "auth.json", "config.toml"):
        try:
            (home / name).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise CodexProfileError("codex_profile_home_unverified") from None
        raise CodexProfileError("codex_profile_home_not_isolated")


class CodexProfileLease:
    """Issued only by the service; contains no credentials or public home input."""
    def __init__(self, service: "CodexAppProfiles", record: dict[str, Any]):
        self._service, self._record = service, record
        self.owner, self.profile_id, self.generation = record["owner"], record["profile_id"], record["generation"]
        self.home = service.root / self.profile_id
        self.secret_root = service.root
        self.account = record["account"]
        self._identity = record["home_identity"]
        self._root_identity = record["root_identity"]
        self.session = record.get("session")

    def check(self, *, owner: str | None = None, read_roots: Sequence[Path] = ()) -> None:
        with self._service._lock:
            current = self._service._records.get(self.owner)
            if (current is not self._record or current["state"] != "connected"
                    or current["generation"] != self.generation
                    or (owner is not None and owner != self.owner)):
                raise CodexProfileError("codex_profile_connection_invalidated")
        if _directory(self.home) != self._identity or _directory(self.secret_root) != self._root_identity:
            raise CodexProfileError("codex_profile_storage_changed")
        self._service.assert_no_secret_scope_overlap(read_roots)
        _isolated_home(self.home)
        if self.session is not None:
            self.session.check()

    @property
    def control_socket(self) -> Path:
        self.check()
        parent = self.home / "app-server-control"
        _directory(parent)
        socket = parent / "app-server-control.sock"
        try:
            info = socket.lstat()
            if socket.is_symlink() or int(getattr(info, "st_file_attributes", 0)) & 0x400 or socket.resolve(strict=True) != socket:
                raise OSError()
        except (OSError, RuntimeError):
            raise CodexProfileError("codex_profile_daemon_unverified") from None
        return socket

    def environment(self, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
        self.check()
        source = os.environ if inherited is None else inherited
        environment = {key: value for key, value in source.items() if key.upper() in CHILD_ENV_KEYS}
        environment.update(CODEX_HOME=str(self.home), CODEX_EXEC_SERVER_URL="none",
                           CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED="1")
        return environment

    def command(self, command: list[str]) -> list[str]:
        self.check()
        if "app-server" not in command:
            raise CodexProfileError("codex_profile_requires_app_server")
        index = command.index("app-server")
        return [*command[:index], "-c", 'cli_auth_credentials_store="keyring"', *command[index:]]

    def own_cleanup(self, callback: Callable[[], Any]) -> Callable[[], None]:
        with self._service._lock:
            self.check()
            self._record["cleanup"].append(callback)
        def release() -> None:
            with self._service._lock:
                if callback in self._record["cleanup"]:
                    self._record["cleanup"].remove(callback)
        return release

    def receipt(self) -> dict[str, Any]:
        return {"profile_id": self.profile_id, "generation": self.generation, "boot_id": self._service.boot_id}


class CodexAppProfiles:
    """Memory-only connection state; restart cannot restore a grant or lease.

    The default server constructs this with no owner/root. These are
    trusted deployment dependencies, never environment auto-enablement, app
    settings, request fields or a browser-supplied activation assertion.
    """
    def __init__(self, *, owner_id: str | None = None, root: Path | None = None,
                 allowed_roots: Sequence[Path] = (), connector=None):
        if connector is not None:
            from .codex_profile_connector import CodexOfficialProfileConnector
            if type(connector) is not CodexOfficialProfileConnector:
                raise CodexProfileError("codex_profile_deployment_unverified")
        self.owner_id = _owner(owner_id) if owner_id is not None else None
        self.root = Path(root) if root is not None else None
        self.allowed_roots = tuple(Path(p) for p in allowed_roots)
        self._connector = connector
        self.boot_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._generation = 0

    def _authorize(self, owner: str) -> None:
        _owner(owner)
        if self.owner_id is None or owner != self.owner_id:
            raise CodexProfileError("codex_profile_owner_not_authorized", 403)
        if self.root is None:
            raise CodexProfileError("codex_profile_storage_approval_required")

    def assert_no_secret_scope_overlap(self, roots: Sequence[Path]) -> None:
        """Protect the reserved secret namespace for every task and owner.

        No connection or grant is required for this denial. The default root
        is absent, so ordinary deployments perform no private path inspection.
        """
        if self.root is None:
            return
        if not self.root.is_absolute():
            raise CodexProfileError("codex_profile_storage_unverified")
        secret_root = self.root.resolve(strict=False)
        for root in roots:
            candidate = Path(root).resolve(strict=True)
            if _within(candidate, secret_root) or _within(secret_root, candidate):
                raise CodexProfileError("codex_profile_secret_scope_overlap", 403)

    def status(self, owner: str) -> dict[str, Any]:
        self._authorize(owner)
        with self._lock:
            record = self._records.get(owner)
            if record and record["state"] == "awaiting_user_approval":
                self._refresh_intent(record)
            state = record["state"] if record else "not_connected"
            if state == "connected":
                try:
                    CodexProfileLease(self, record).check()
                except CodexProfileError:
                    state = "connection_invalidated"
            return {"enabled": True, "state": state, "profile_id": record["profile_id"] if record else None,
                    "generation": record["generation"] if record else self._generation,
                    "connected": state == "connected", "model_ready": bool(state == "connected" and record.get("session")
                        and record["session"].model_contract_verified),
                    "login_started": bool(record and record.get("session")),
                    "connector_available": self._connector is not None}

    def _disclosure(self, profile_id: str) -> dict[str, Any]:
        # Lexical server-owned destination only. Handoff does not inspect or
        # create it, inspect keyring, or claim that deployment is provisioned.
        return {"version": DISCLOSURE_VERSION, "cli_version": CONNECTION_CLI_VERSION,
                "storage_destination": str(self.root / profile_id),
                "storage_provisioned": False, "credential_saved": False,
                "credential_store": "keyring_only",
                "windows_storage": "os_keyring_key_and_encrypted_codex_home_secrets",
                "credential_lifetime": "no_fixed_expiry_refresh_possible",
                "connection_lifetime": "memory_only_until_disconnect_or_restart",
                "oauth_scopes": list(OAUTH_SCOPES),
                "intended_use": "operations_read_only",
                "usage": "owner_attributed_observed_usage",
                "disconnect_effects": ["invalidate_intent_and_lease", "cancel_registered_owned_work"],
                "disconnect_removes_credentials": False, "disconnect_revokes_provider_grant": False,
                "official_logout_required_separately": True,
                "official_connector_available": self._connector is not None,
                "connector_spec": self._connector.disclosure() if self._connector is not None else None,
                "approval_receipt_seconds": 60, "login_wait_seconds": 300}

    def _intent_digest(self, record: Mapping[str, Any]) -> str:
        spec = {"owner": record["owner"], "boot_id": self.boot_id,
                "profile_id": record["profile_id"], "generation": record["generation"],
                "nonce": record["nonce"], "expires": record["expires"],
                "allowed_roots": [str(path) for path in self.allowed_roots],
                "connector_binding": self._connector.binding() if self._connector is not None else None,
                "disclosure": self._disclosure(record["profile_id"])}
        return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")).hexdigest()

    def _refresh_intent(self, record: dict[str, Any]) -> None:
        if (time.monotonic() >= record["expires"]
                or record["spec_digest"] != self._intent_digest(record)):
            record.update(state="handoff_invalidated", nonce=None)

    def handoff(self, owner: str) -> dict[str, Any]:
        self._authorize(owner)
        # Intent only: no directory creation, profile inspection, credential
        # storage, native process, account/login/start or automatic activation.
        with self._lock:
            current = self._records.get(owner)
            if current and current["state"] == "awaiting_user_approval":
                self._refresh_intent(current)
            if current and current["state"] in {"awaiting_user_approval", "approved", "preparing", "logging_in", "connected"}:
                raise CodexProfileError("codex_profile_handoff_exists")
            self._generation += 1
            record = {"owner": owner, "profile_id": uuid.uuid4().hex, "generation": self._generation,
                      "nonce": uuid.uuid4().hex, "expires": time.monotonic() + 300,
                      "state": "awaiting_user_approval", "cleanup": [], "account": None}
            record["spec_digest"] = self._intent_digest(record)
            self._records[owner] = record
            return {**self.status(owner), "nonce": record["nonce"], "expires_in_seconds": 300,
                    "intended_use": "operations_read_only", "credential_store": "keyring_only",
                    "oauth_scopes": list(OAUTH_SCOPES), "login_method": "official_browser_or_device",
                    "required_approvals": ["secret_storage_and_persistence", "oauth_scopes",
                                           "intended_use_usage_and_disconnect", "human_official_login"],
                    "token_import_supported": False, "spec_digest": record["spec_digest"],
                    "disclosure": self._disclosure(record["profile_id"])}

    def approve_handoff(self, owner: str, *, nonce: str, spec_digest: str,
                        accept_storage: bool, accept_scopes: bool,
                        accept_use_and_disconnect: bool, accept_official_login: bool) -> dict[str, Any]:
        self._authorize(owner)
        if self._connector is None:
            raise CodexProfileError("codex_profile_official_connector_unavailable")
        if any(value is not True for value in (accept_storage, accept_scopes, accept_use_and_disconnect, accept_official_login)):
            raise CodexProfileError("codex_profile_explicit_approval_required")
        with self._lock:
            record = self._records.get(owner)
            if record and record["state"] == "awaiting_user_approval":
                self._refresh_intent(record)
            if (not record or record["state"] != "awaiting_user_approval"
                    or record["nonce"] != nonce or record["spec_digest"] != spec_digest):
                raise CodexProfileError("codex_profile_handoff_invalidated")
            record.update(state="approved", approval_receipt=uuid.uuid4().hex,
                          approval_expires=time.monotonic() + 60)
            return {"approval_receipt": record["approval_receipt"], "expires_in_seconds": 60}

    def _check_connection(self, record: dict[str, Any]) -> None:
        self._authorize(record["owner"])
        with self._lock:
            if (self._records.get(record["owner"]) is not record
                    or record["state"] not in {"preparing", "logging_in", "connected"}
                    or record["spec_digest"] != self._intent_digest(record)
                    or (record["state"] != "connected" and time.monotonic() >= record["login_expires"])):
                raise CodexProfileError("codex_profile_connection_invalidated")

    def _fail_connection(self, record: dict[str, Any], code: str) -> None:
        with self._lock:
            if record["state"] == "disconnected":
                return
            callbacks = list(record["cleanup"])
            record.update(state="failed", nonce=None, approval_receipt=None, account=None,
                          session=None, cleanup=[], failure_code=code)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def begin_login(self, owner: str, *, approval_receipt: str) -> dict[str, Any]:
        self._authorize(owner)
        if self._connector is None:
            raise CodexProfileError("codex_profile_official_connector_unavailable")
        with self._lock:
            record = self._records.get(owner)
            if (not record or record["state"] != "approved"
                    or record.get("approval_receipt") != approval_receipt
                    or time.monotonic() >= record["approval_expires"]
                    or record["spec_digest"] != self._intent_digest(record)):
                raise CodexProfileError("codex_profile_approval_invalidated")
            record.update(state="preparing", approval_receipt=None, login_expires=time.monotonic() + 300)
        try:
            session = self._connector.prepare(self, record)
            with self._lock:
                self._check_connection(record)
                record.update(session=session, state="logging_in")
            return {"login_id": session.login_id, "auth_url": session.login_url, "expires_in_seconds": 300}
        except Exception as exc:
            self._fail_connection(record, exc.code if isinstance(exc, CodexProfileError) else "codex_profile_preparation_failed")
            raise CodexProfileError(exc.code if isinstance(exc, CodexProfileError) else "codex_profile_preparation_failed") from None

    def poll_login(self, owner: str) -> dict[str, Any]:
        self._authorize(owner)
        with self._lock:
            record = self._records.get(owner)
            if not record or record["state"] != "logging_in":
                return self.status(owner)
            session = record["session"]
        try:
            if session.poll():
                with self._lock:
                    self._check_connection(record)
                    record.update(state="connected", account=session.account,
                                  home_identity=session.home_identity, root_identity=session.root_identity)
            return self.status(owner)
        except Exception as exc:
            self._fail_connection(record, exc.code if isinstance(exc, CodexProfileError) else "codex_profile_login_verification_failed")
            raise CodexProfileError(exc.code if isinstance(exc, CodexProfileError) else "codex_profile_login_verification_failed") from None

    def lease(self, owner: str, *, receipt: Mapping[str, Any] | None = None) -> CodexProfileLease:
        self._authorize(owner)
        with self._lock:
            record = self._records.get(owner)
            if not record or record["state"] != "connected":
                raise CodexProfileError("codex_profile_not_connected")
            lease = CodexProfileLease(self, record)
            if receipt is not None and dict(receipt) != lease.receipt():
                raise CodexProfileError("codex_profile_connection_invalidated")
            lease.check()
            return lease

    def disconnect(self, owner: str) -> dict[str, Any]:
        self._authorize(owner)
        with self._lock:
            record = self._records.get(owner)
            self._generation += 1
            callbacks = list(record["cleanup"]) if record else []
            if record:
                record.update(state="disconnected", generation=self._generation, nonce=None, account=None,
                              approval_receipt=None, session=None, cleanup=[])
        # Revoke the lease first; only callbacks registered by this profile are
        # touched. Official credential logout/revocation is a separate human
        # step, not a claim made by this model-free connection boundary.
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass
        return self.status(owner)
