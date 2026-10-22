"""Concrete owned-stdio official login. No token import or personal fallback."""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .codex_app_profiles import CHILD_ENV_KEYS, OAUTH_SCOPES, CodexProfileError, _directory
from .codex_profile_artifact import CodexArtifact
from .codex_profile_storage import WindowsPrivateStorage, verify_startup_policy


def public_account(value) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise CodexProfileError("codex_profile_account_unverified")
    account, routing = value.get("account"), value.get("workspaceRouting")
    if (not isinstance(account, dict) or account.get("type") != "chatgpt"
            or not isinstance(routing, dict) or routing.get("backendOrigin") != "https://chatgpt.com"
            or routing.get("accountRoutingOverride") != "NO_CONSTRAINT"
            or not isinstance(account.get("email"), str) or not account["email"]
            or not isinstance(routing.get("chatgptAccountId"), str) or not routing["chatgptAccountId"]):
        raise CodexProfileError("codex_profile_account_unverified")
    return account["email"], routing["chatgptAccountId"], routing["backendOrigin"]


class CodexOfficialProfileConnector:
    """Trusted deployment dependency. Constructor does not inspect OS/storage."""
    def __init__(self, *, artifact: CodexArtifact, expected_account: tuple[str, str, str]):
        if type(artifact) is not CodexArtifact or not artifact.package.is_absolute() or not artifact.checksums.is_absolute() or (not isinstance(expected_account, tuple)
                or len(expected_account) != 3 or not all(isinstance(x, str) and x for x in expected_account)
                or expected_account[2] != "https://chatgpt.com"):
            raise CodexProfileError("codex_profile_deployment_unverified")
        self.artifact, self.expected_account = artifact, expected_account

    def disclosure(self) -> dict:
        return {"artifact": self.artifact.disclosure(), "expected_account": list(self.expected_account),
                "startup_policy": "absent_local_managed_and_project_config_strict_owned_stdio",
                "reconnect": "fresh_home_only_no_authenticated_child_restart"}

    def binding(self) -> dict:
        return {"package_path": str(self.artifact.package), "checksums_path": str(self.artifact.checksums),
                "contract": self.disclosure()}

    def prepare(self, service, record) -> "CodexOfficialLoginSession":
        # Called only after service consumes an owner-bound approval receipt.
        service._check_connection(record)
        storage = WindowsPrivateStorage()
        home, root_identity, home_identity = storage.prepare(service.root, record["profile_id"], service.allowed_roots)
        service._check_connection(record)
        verify_startup_policy(home)
        runtime = service.root / "runtime"
        if not runtime.exists():
            storage.create_directory(runtime)
        storage.verify_directory(runtime)
        bundle = runtime / self.artifact.disclosure()["sha256"]
        artifact = self.artifact.prepare(bundle, storage)
        service._check_connection(record)
        session = CodexOfficialLoginSession(service, record, self, storage, home,
                                            root_identity, home_identity, artifact)
        try:
            session.start()
            return session
        except Exception:
            session.close()
            raise


class CodexOfficialLoginSession:
    def __init__(self, service, record, connector, storage, home, root_identity, home_identity, artifact):
        self.service, self.record, self.connector = service, record, connector
        self.storage, self.home, self.artifact = storage, home, artifact
        self.root_identity, self.home_identity = root_identity, home_identity
        self.transport = None
        self.login_id, self.login_url = None, None
        self.completed = False
        self.account = None
        self.initialized = None
        self._handles, self._release = [], None
        self._lock, self._close_lock = threading.RLock(), threading.Lock()
        self._closed, self._serial = False, 0
        self._stop = threading.Event()
        self.router = None
        self.model_contract_verified = False

    def check(self) -> None:
        self.service._check_connection(self.record)
        if self._closed:
            raise CodexProfileError("codex_profile_connection_invalidated")
        if self.transport is not None and self.transport.process.poll() is not None:
            raise CodexProfileError("codex_profile_owned_process_exited")
        if (_directory(self.home) != self.home_identity
                or _directory(self.service.root) != self.root_identity):
            raise CodexProfileError("codex_profile_storage_changed")
        self.storage.verify_directory(self.home)
        self.storage.verify_directory(self.service.root)
        verify_startup_policy(self.home)

    def environment(self):
        import os
        self.check()
        value = {k: v for k, v in os.environ.items() if k.upper() in CHILD_ENV_KEYS}
        # rg is part of the verified package; no unverified PATH fallback.
        value.update(CODEX_HOME=str(self.home), CODEX_EXEC_SERVER_URL="none",
                     CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED="1",
                     PATH=str(self.artifact.root / "codex-path"))
        return value

    def own_cleanup(self, callback):
        self.service._check_connection(self.record)
        with self.service._lock:
            self.service._check_connection(self.record)
            self.record["cleanup"].append(callback)
        def release():
            with self.service._lock:
                if callback in self.record["cleanup"]:
                    self.record["cleanup"].remove(callback)
        return release

    def _notification(self, message) -> None:
        if "id" in message or "result" in message or "error" in message:
            raise CodexProfileError("codex_profile_unexpected_rpc_message")
        method = message.get("method")
        if method == "account/login/completed":
            params = message.get("params")
            if (not self.login_id or not isinstance(params, dict)
                    or params.get("loginId") != self.login_id or self.completed):
                raise CodexProfileError("codex_profile_login_id_mismatch")
            if params.get("success") is not True or params.get("error") is not None:
                raise CodexProfileError("codex_profile_official_login_failed")
            self.completed = True
        elif method not in {"account/updated", "account/rateLimits/updated"}:
            raise CodexProfileError("codex_profile_unexpected_notification")

    def rpc(self, method: str, params) -> dict:
        if self.router is not None:
            return self.router.control.rpc(method, params)
        self.check()
        self._serial += 1
        request_id = f"aoitalk-profile-{self._serial}"
        self.transport.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 20
        while True:
            self.check()
            if time.monotonic() >= deadline:
                raise CodexProfileError("codex_profile_rpc_timeout")
            try:
                message = self.transport.receive(timeout=0.05)
            except queue.Empty:
                continue
            if not isinstance(message, dict):
                raise CodexProfileError("codex_profile_invalid_rpc_message")
            if message.get("id") != request_id:
                self._notification(message)
                continue
            if "method" in message or "error" in message or not isinstance(message.get("result"), dict):
                raise CodexProfileError("codex_profile_rpc_failed")
            return message["result"]

    def verify_configuration(self) -> None:
        from .codex_task_preflight import validate_managed_requirements
        response = self.rpc("configRequirements/read", None)
        validate_managed_requirements(response)
        requirements = response["requirements"]
        if requirements is not None:
            permitted = {"modelProvider", "modelProviders", "chatgptBaseUrl", "cliAuthCredentialsStore", "allowedLoginMethods"}
            if (set(requirements) - permitted
                    or requirements.get("cliAuthCredentialsStore") not in {None, "keyring"}
                    or requirements.get("allowedLoginMethods") not in (None, ["chatgpt"])):
                raise CodexProfileError("codex_profile_managed_policy_unverified")
        config = self.rpc("config/read", {"includeLayers": False, "cwd": str(self.home)}).get("config")
        if (not isinstance(config, dict) or config.get("cli_auth_credentials_store") != "keyring"
                or config.get("model_provider") != "openai"
                or config.get("chatgpt_base_url") not in {None, "https://chatgpt.com/backend-api/"}
                or config.get("openai_base_url") not in {None, "https://chatgpt.com/backend-api/codex"}
                or config.get("model_providers") or config.get("instructions") or config.get("model_instructions_file")):
            raise CodexProfileError("codex_profile_effective_config_unverified")

    def start(self) -> None:
        from ..llm.codex_task_completion import _ProcessTransport, _startup_args
        self.check()
        self._handles = self.storage.lock_files([self.artifact.root / name for name in sorted(self.artifact.inventory)])
        self._handles += self.storage.lock_directories([self.service.root, self.home, self.artifact.root])
        self.artifact.check()  # Rehash after write/delete-denying handles exist.
        self._release = self.own_cleanup(self.close)
        args = _startup_args()
        command = [str(self.artifact.executable), *args, "--strict-config", "-c", 'cli_auth_credentials_store="keyring"']
        self.check()
        self.transport = _ProcessTransport(command, self.home, profile=self)
        self.transport.set_control(self.check)
        self.initialized = self.rpc("initialize", {
            "clientInfo": {"name": "aoitalk_profile", "title": "AoiTalk", "version": "1"},
            "capabilities": {"experimentalApi": True, "explicitGatewayOauth": True}})
        if (self.initialized.get("codexHome") != str(self.home)
                or not str(self.initialized.get("userAgent", "")).startswith("codex_cli_rs/0.160.0 ")):
            raise CodexProfileError("codex_profile_initialize_unverified")
        self.transport.send({"method": "initialized"})
        self.verify_configuration()
        if self.rpc("account/read", {"refreshToken": False}).get("account") is not None:
            raise CodexProfileError("codex_profile_existing_auth_rejected")
        result = self.rpc("account/login/start", {"type": "chatgpt"})
        login_id, url = result.get("loginId"), result.get("authUrl")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (result.get("type") != "chatgpt" or not isinstance(login_id, str) or not login_id
                or parsed is None or parsed.scheme != "https" or parsed.hostname != "auth.openai.com"
                or parsed.path != "/oauth/authorize" or parsed.username or parsed.password or parsed.port or parsed.fragment
                or parse_qs(parsed.query).get("scope") != [" ".join(OAUTH_SCOPES)]):
            raise CodexProfileError("codex_profile_login_handoff_unverified")
        self.login_id, self.login_url = login_id, url
        threading.Thread(target=self._watch_expiry, daemon=True).start()

    def _watch_expiry(self) -> None:
        while not self._stop.wait(0.25):
            try:
                self.check()
            except Exception:
                self.service._fail_connection(self.record, "codex_profile_connection_invalidated")
                return

    def poll(self) -> bool:
        with self._lock:
            self.check()
            # Concurrent status requests may have captured logging_in before
            # the first request installed the sole reader. Never drain raw
            # stdio or create another router after that transition.
            if self.router is not None:
                if self.account != self.connector.expected_account:
                    raise CodexProfileError("codex_profile_account_mismatch")
                return True
            for _ in range(32):
                try:
                    message = self.transport.receive(timeout=0.001)
                except queue.Empty:
                    break
                if not isinstance(message, dict):
                    raise CodexProfileError("codex_profile_invalid_rpc_message")
                self._notification(message)
            if not self.completed:
                return False
            self.verify_configuration()
            first = public_account(self.rpc("account/read", {"refreshToken": False}))
            second = public_account(self.rpc("account/read", {"refreshToken": False}))
            if first != second or first != self.connector.expected_account:
                raise CodexProfileError("codex_profile_account_mismatch")
            self.check()
            self.account = first
            from .codex_profile_stdio import OwnedStdioRouter
            self.router = OwnedStdioRouter(self)
            return True

    def preflight(self, lease, check, workspace) -> None:
        from .codex_profile_storage import require_absent
        with self._lock:
            lease.check(owner=lease.owner, read_roots=[workspace])
            if lease.session is not self or self.router is None or self.router.task is not None:
                raise CodexProfileError("codex_profile_owned_transport_unavailable")
            for ancestor in (workspace, *workspace.parents):
                require_absent(ancestor / ".codex" / "config.toml")
            previous = self.router.control._control
            def guard():
                check()
                lease.check(owner=lease.owner, read_roots=[workspace])
            self.router.control.set_control(guard)
            try:
                guard()
                self.verify_configuration()
                if public_account(self.rpc("account/read", {"refreshToken": False})) != lease.account:
                    raise CodexProfileError("codex_profile_account_mismatch")
                guard()
            except Exception:
                self.router.fail()
                raise
            finally:
                self.router.control.set_control(previous)

    def acquire_task(self, lease, check, workspace):
        with self._lock:
            self.preflight(lease, check, workspace)
            return self.router.open_task(lease, check, workspace)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        try:
            if self.transport is not None:
                self.transport.close()
        finally:
            if self.router is not None:
                self.router.close()
            if self._release is not None:
                self._release()
            self.storage.unlock_files(self._handles)
            self._handles = []
