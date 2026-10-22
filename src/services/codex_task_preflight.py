"""Managed Codex configuration preflight for Operations tasks.

The Operations Codex adapter starts an authenticated, owned app-server child.
Before that happens, this module reads managed requirements from an official
Codex daemon through its local control socket.  Normal preflight never starts
a daemon, edits Codex configuration, or reads authentication files.  The
deployment-owned native worker may explicitly call
``ensure_managed_codex_daemon`` after its policy/owner/CLI-adoption gate; that
hook performs one bounded official ``daemon start`` only for an explicit
stopped state and never restarts or stops a shared daemon.
"""

from __future__ import annotations

import math
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..llm.codex_task_completion import (
    SUPPORTED_CODEX_VERSIONS,
    CodexTaskProtocolError,
    _default_codex_home,
    codex_app_server_command,
    is_supported_codex_user_agent,
)
from ..llm.codex_proxy_websocket import CodexProxyWebSocketTransport


OFFICIAL_OPENAI_BASE_URL = "https://chatgpt.com/backend-api/codex"
OFFICIAL_CHATGPT_BASE_URL = "https://chatgpt.com/backend-api/"
_DAEMON_ENSURE_TIMEOUT_SECONDS = 15.0
_DAEMON_ENSURE_LOCK = threading.Lock()


class CodexTaskPreflightError(RuntimeError):
    """A managed Codex requirement could not be verified safely."""


def default_control_socket() -> Path:
    """Return the official local daemon control socket path.

    ``CODEX_HOME`` is deployment configuration, not task input.  The default
    mirrors Codex's documented local daemon layout and does not create or
    modify anything.
    """

    configured = os.environ.get("CODEX_APP_SERVER_CONTROL_SOCKET", "").strip()
    if configured:
        return Path(configured)
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        codex_home = str(_default_codex_home(os.environ))
    return Path(codex_home) / "app-server-control" / "app-server-control.sock"


def _daemon_output(result: Any) -> str:
    """Return bounded, private status text for stopped-daemon detection.

    The official CLI owns the status wording and may include local paths.  The
    text is used only as an in-process discriminator and is never included in
    an exception or task result.
    """

    values = []
    for name in ("stdout", "stderr"):
        value = getattr(result, name, "")
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            values.append(str(value))
    return "\n".join(values)[-4096:].casefold()


def _daemon_status_is_explicitly_stopped(result: Any, socket_path: Path) -> bool:
    """Recognize only a local connection-refused daemon status.

    A non-zero ``daemon version`` result can also mean an invalid home,
    malformed configuration, permission failure, or a different CLI problem.
    Those cases must not trigger ``daemon start``.  A missing expected socket
    or the Windows/POSIX connection-refused diagnostics are the only accepted
    stopped states.
    """

    if not socket_path.exists():
        return True
    output = _daemon_output(result)
    return (
        "os error 10061" in output
        or "winerror 10061" in output
        or "connection refused" in output
        or "econnrefused" in output
        or "os error 111" in output
    )


def _run_daemon_command(command: list[str], timeout_seconds: float) -> Any:
    """Run one official daemon lifecycle command without a shell."""

    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodexTaskPreflightError("codex_preflight_cli_unavailable") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexTaskPreflightError("codex_preflight_daemon_unavailable") from exc


def ensure_managed_codex_daemon(
    *,
    deployment_authorized: bool = False,
    socket_path: str | Path | None = None,
    timeout_seconds: float = _DAEMON_ENSURE_TIMEOUT_SECONDS,
    command_runner: Callable[[list[str], float], Any] | None = None,
) -> bool:
    """Ensure the existing official managed daemon is running at most once.

    This is an explicit deployment hook.  Normal preflight never calls it;
    the deployment-owned native worker may call it only after its policy,
    owner, CLI identity and human-adoption checks have passed.  The helper
    does not read credentials, change ``CODEX_HOME`` or pass configuration
    overrides.  It checks ``daemon version`` first, calls the official
    ``daemon start`` only for an explicit stopped state, and performs one
    bounded post-start status check.  It never restarts or stops a shared
    daemon and never retries an unchanged failure.

    Returns ``True`` when this call issued ``daemon start`` and ``False`` when
    the managed daemon was already running.
    """

    if deployment_authorized is not True:
        raise CodexTaskPreflightError(
            "codex_preflight_daemon_start_not_authorized"
        )
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise CodexTaskPreflightError("codex_preflight_daemon_unavailable") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise CodexTaskPreflightError("codex_preflight_daemon_unavailable")

    socket = Path(socket_path) if socket_path is not None else default_control_socket()
    if not socket.is_absolute():
        raise CodexTaskPreflightError("codex_preflight_daemon_unavailable")
    runner = command_runner or _run_daemon_command
    try:
        executable = codex_app_server_command()[0]
    except CodexTaskProtocolError as exc:
        raise CodexTaskPreflightError("codex_preflight_cli_unavailable") from exc

    def run(action: str) -> Any:
        return runner([str(executable), "app-server", "daemon", action], timeout)

    # Multiple native tasks can reach this hook concurrently.  Serialize the
    # status/start/status sequence so they cannot issue duplicate starts.  A
    # later independent task may perform a new single ensure if the daemon
    # subsequently stops; there is no retry loop inside one invocation.
    with _DAEMON_ENSURE_LOCK:
        status = run("version")
        if getattr(status, "returncode", 1) == 0:
            return False
        if not _daemon_status_is_explicitly_stopped(status, socket):
            raise CodexTaskPreflightError("codex_preflight_daemon_unavailable")

        started = run("start")
        if getattr(started, "returncode", 1) != 0:
            raise CodexTaskPreflightError("codex_preflight_daemon_unavailable")
        verified = run("version")
        if getattr(verified, "returncode", 1) != 0:
            raise CodexTaskPreflightError("codex_preflight_daemon_unavailable")
        return True


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CodexTaskPreflightError(code)
    return value


def _string_or_none(value: Any, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CodexTaskPreflightError(code)
    return value.strip()


def validate_managed_requirements(response: Any) -> None:
    """Fail closed unless managed requirements preserve the OpenAI route.

    A null requirements value means no requirements.toml/MDM policy was
    configured.  When a provider policy is present, every endpoint-bearing
    provider definition must be explicit and must point at the official
    ChatGPT backend.  Unknown provider names or endpoint keys are rejected so
    a future schema change cannot silently widen the route.
    """

    envelope = _require_mapping(response, "codex_preflight_invalid_requirements_response")
    if "requirements" not in envelope:
        raise CodexTaskPreflightError("codex_preflight_missing_requirements")
    requirements = envelope.get("requirements")
    if requirements is None:
        return
    requirements = _require_mapping(requirements, "codex_preflight_invalid_requirements")

    provider = _string_or_none(
        requirements.get("modelProvider"), "codex_preflight_invalid_model_provider"
    )
    if provider is not None and provider != "openai":
        raise CodexTaskPreflightError("codex_preflight_non_openai_provider")

    chatgpt_base_url = _string_or_none(
        requirements.get("chatgptBaseUrl"), "codex_preflight_invalid_chatgpt_base_url"
    )
    if chatgpt_base_url is not None and chatgpt_base_url != OFFICIAL_CHATGPT_BASE_URL:
        raise CodexTaskPreflightError("codex_preflight_unapproved_chatgpt_origin")

    model_providers = requirements.get("modelProviders")
    if model_providers is None:
        return
    model_providers = _require_mapping(
        model_providers, "codex_preflight_invalid_model_providers"
    )
    if not model_providers or set(model_providers) != {"openai"}:
        raise CodexTaskPreflightError("codex_preflight_non_openai_provider_definition")
    openai = _require_mapping(
        model_providers.get("openai"), "codex_preflight_incomplete_openai_definition"
    )
    # 0.160.0 uses this provider endpoint when refreshing the model catalog.
    # No custom catalog origin has been reviewed for the Operations route.
    if {"model_catalog_url", "modelCatalogUrl"} & openai.keys():
        raise CodexTaskPreflightError("codex_preflight_unapproved_model_catalog_endpoint")

    # The requirements schema says provider definitions use config.toml field
    # names.  Accept only the two known official URL fields, and require one
    # to be present so an incomplete definition cannot fall through to a
    # user-level provider or an arbitrary default.
    endpoint_keys = {
        key for key in openai
        if key in {"base_url", "chatgpt_base_url", "chatgptBaseUrl", "baseUrl", "url", "origin"}
    }
    if not endpoint_keys:
        raise CodexTaskPreflightError("codex_preflight_incomplete_openai_definition")
    if endpoint_keys - {"base_url", "chatgpt_base_url", "chatgptBaseUrl"}:
        raise CodexTaskPreflightError("codex_preflight_unknown_openai_endpoint")
    if "base_url" in openai and openai["base_url"] != OFFICIAL_OPENAI_BASE_URL:
        raise CodexTaskPreflightError("codex_preflight_unapproved_provider_endpoint")
    for key in ("chatgpt_base_url", "chatgptBaseUrl"):
        if key in openai and openai[key] != OFFICIAL_CHATGPT_BASE_URL:
            raise CodexTaskPreflightError("codex_preflight_unapproved_chatgpt_origin")


def _rpc(
    transport: Any,
    *,
    method: str,
    params: Any,
    check: Callable[[], Any],
    deadline: float,
) -> Any:
    request_id = "aoitalk-preflight-" + method.replace("/", "-")
    transport.send({"id": request_id, "method": method, "params": params})
    while True:
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexTaskPreflightError("codex_preflight_timeout")
        try:
            message = transport.receive(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if not isinstance(message, Mapping):
            raise CodexTaskPreflightError("codex_preflight_invalid_rpc_message")
        if message.get("id") != request_id:
            # Notifications are expected during initialize.  A response for a
            # different request would indicate a shared/foreign transport.
            if "id" in message and "method" in message:
                raise CodexTaskPreflightError("codex_preflight_unexpected_server_request")
            if "method" in message:
                continue
            raise CodexTaskPreflightError("codex_preflight_foreign_rpc_response")
        if "error" in message:
            raise CodexTaskPreflightError("codex_preflight_rpc_failed")
        if "result" not in message:
            raise CodexTaskPreflightError("codex_preflight_missing_rpc_result")
        return message["result"]


class CodexManagedConfigPreflight:
    """Callable callback accepted by :class:`LocalAgentTaskRunner`."""

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        timeout_seconds: float = 5.0,
        transport_factory: Callable[..., Any] = CodexProxyWebSocketTransport,
        command_factory: Callable[[Path], list[str]] | None = None,
        auto_ensure_daemon: bool = False,
    ) -> None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.socket_path = Path(socket_path) if socket_path is not None else default_control_socket()
        self.timeout_seconds = timeout
        self.transport_factory = transport_factory
        self.command_factory = command_factory or self._default_command
        # Only the deployment-owned native runtime may opt into this after
        # its sealed policy/owner/CLI-adoption gate.  General server wiring
        # keeps the default fail-closed and never starts a daemon.
        self.auto_ensure_daemon = auto_ensure_daemon is True

    @staticmethod
    def _default_command(socket_path: Path) -> list[str]:
        try:
            executable = codex_app_server_command()[0]
        except CodexTaskProtocolError as exc:
            raise CodexTaskPreflightError("codex_preflight_cli_unavailable") from exc
        return [str(executable), "app-server", "proxy", "--sock", str(socket_path)]

    def ensure_managed_daemon(self, *, deployment_authorized: bool = False) -> bool:
        """Run the explicit deployment-owned daemon ensure hook.

        Callers must invoke this only after their deployment policy, owner,
        exact CLI identity and any human-adoption checks have passed, and must
        pass ``deployment_authorized=True``.  The regular ``__call__`` path
        intentionally does not invoke it.
        """

        return ensure_managed_codex_daemon(
            deployment_authorized=deployment_authorized,
            socket_path=self.socket_path,
        )

    def __call__(self, ctx: Any) -> None:
        check = ctx.check_running
        check()
        socket_path = self.socket_path
        profile = getattr(ctx, "codex_profile", None)
        if profile is not None:
            profile.check(owner=ctx.user_id, read_roots=[Path(ctx.spec.workspace)])
            if profile.session is not None:
                profile.session.preflight(profile, check, Path(ctx.spec.workspace))
                return
            socket_path = profile.control_socket
        if profile is None and self.auto_ensure_daemon:
            ensure_managed_codex_daemon(
                deployment_authorized=True,
                socket_path=socket_path,
            )
        if not socket_path.is_absolute() or not socket_path.exists():
            raise CodexTaskPreflightError("codex_preflight_daemon_unavailable")
        workspace = Path(ctx.spec.workspace)
        deadline = time.monotonic() + self.timeout_seconds

        def guard() -> None:
            check()
            if profile is not None:
                profile.check(owner=ctx.user_id, read_roots=[workspace])
            if time.monotonic() >= deadline:
                raise CodexTaskPreflightError("codex_preflight_timeout")

        transport = None
        try:
            command = self.command_factory(socket_path)
            transport = self.transport_factory(profile.command(command), workspace, profile=profile) if profile is not None else self.transport_factory(command, workspace)
            set_control = getattr(transport, "set_control", None)
            if callable(set_control):
                set_control(guard)
            initialized = _rpc(
                transport,
                method="initialize",
                params={
                    "clientInfo": {
                        "name": "aoitalk_operations_preflight",
                        "title": "AoiTalk Operations Preflight",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True, "explicitGatewayOauth": True},
                },
                check=guard,
                deadline=deadline,
            )
            user_agent = initialized.get("userAgent") if isinstance(initialized, Mapping) else None
            if not is_supported_codex_user_agent(user_agent):
                raise CodexTaskPreflightError("codex_preflight_unverified_cli_version")
            if profile is not None and initialized.get("codexHome") != str(profile.home):
                raise CodexTaskPreflightError("codex_preflight_app_profile_home_mismatch")
            transport.send({"method": "initialized"})
            requirements = _rpc(
                transport,
                method="configRequirements/read",
                params=None,
                check=guard,
                deadline=deadline,
            )
            validate_managed_requirements(requirements)
            if profile is not None:
                effective = _rpc(transport, method="config/read", params={"includeLayers": False, "cwd": str(workspace)}, check=guard, deadline=deadline)
                config = effective.get("config") if isinstance(effective, Mapping) else None
                if not isinstance(config, Mapping) or config.get("cli_auth_credentials_store") != "keyring":
                    raise CodexTaskPreflightError("codex_preflight_app_profile_store_unverified")
                account = _rpc(transport, method="account/read", params={"refreshToken": False}, check=guard, deadline=deadline)
                public, routing = account.get("account") or {}, account.get("workspaceRouting") or {}
                if (public.get("type") != "chatgpt" or routing.get("accountRoutingOverride") != "NO_CONSTRAINT"
                        or (public.get("email"), routing.get("chatgptAccountId"), routing.get("backendOrigin")) != profile.account):
                    raise CodexTaskPreflightError("codex_preflight_app_profile_account_mismatch")
            check()
        except CodexTaskPreflightError:
            raise
        except (CodexTaskProtocolError, OSError, ValueError, TypeError, queue.Empty) as exc:
            raise CodexTaskPreflightError("codex_preflight_transport_failed") from exc
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass


__all__ = [
    "CodexManagedConfigPreflight",
    "CodexTaskPreflightError",
    "OFFICIAL_CHATGPT_BASE_URL",
    "OFFICIAL_OPENAI_BASE_URL",
    "SUPPORTED_CODEX_VERSIONS",
    "default_control_socket",
    "ensure_managed_codex_daemon",
    "validate_managed_requirements",
]
