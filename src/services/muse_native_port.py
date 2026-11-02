"""Native Muse implementation of the Chat side-session port.

The ordinary Muse Chat service speaks in terms of a deterministic side-session
id and a Chat turn key.  The native link layer speaks in terms of a persistent
Muse thread and a UUID ``TurnRef``.  This module is the small, deliberately
boring adapter between those contracts.

There are two important boundaries here:

* the existing ``CLISideSessionPort`` remains the owner check.  A native
  thread is never used to infer Chat ownership;
* ``AgentLinkService`` owns retry/idempotency/recovery.  This port invokes it
  once and never retries a send itself.

The only durable state owned by this adapter is the Chat-to-Muse-thread map.
It lives beside the agent-link journal so opening a journal for a test or a
workspace gives both pieces of state the same lifetime.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
import re
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any

from .agent_link.codes import AgentLinkFailure, LinkErrorCode
from .agent_link.contracts import Channel, Observation, Phase, SendReceipt, TurnRef
from .agent_link.journal import SQLiteAgentLinkJournal
from .agent_link.service import AgentLinkService
from .muse_chat_service import (
    RECOVERY_WINDOW,
    SIDE_SESSION_TITLE,
    STATE_FAILED,
    STATE_PENDING,
    STATE_REPLIED,
    STATE_UNCERTAIN,
    SideSessionPort,
    MuseChatUnavailable,
    turn_key_from_send_key,
)
from .muse_remote_execution import MuseError, MuseErrorCode, REMOTE_AGENT_DELEGATE


# The UUID is intentionally independent from the reply-message namespace in
# muse_chat_service.  UUID turn keys retain their identity, which lets a
# reopened process recover the Chat key; arbitrary keys use this domain UUID5
# and are retained in memory for the lifetime of the port.
TURN_NAMESPACE = uuid.UUID("2bf4dce9-ae4f-5a7c-8a5d-8f25e1374a6e")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_NODE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_IDENTIFIER_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_TABLE = "agent_link_threads"
_THREAD_CREATION_LOCKS: dict[tuple[str, str, str], tuple[threading.Lock, int]] = {}
_THREAD_CREATION_LOCKS_GUARD = threading.Lock()


def _retain_thread_creation_lock(
    key: tuple[str, str, str],
) -> threading.Lock:
    with _THREAD_CREATION_LOCKS_GUARD:
        existing = _THREAD_CREATION_LOCKS.get(key)
        if existing is None:
            lock = threading.Lock()
            _THREAD_CREATION_LOCKS[key] = (lock, 1)
            return lock
        lock, references = existing
        _THREAD_CREATION_LOCKS[key] = (lock, references + 1)
        return lock


def _release_thread_creation_lock_reference(
    key: tuple[str, str, str], lock: threading.Lock
) -> None:
    with _THREAD_CREATION_LOCKS_GUARD:
        existing = _THREAD_CREATION_LOCKS.get(key)
        if existing is None or existing[0] is not lock:
            return
        if existing[1] <= 1:
            del _THREAD_CREATION_LOCKS[key]
        else:
            _THREAD_CREATION_LOCKS[key] = (lock, existing[1] - 1)


async def _acquire_thread_creation_lock(lock: threading.Lock) -> None:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.01)


def _awaitable(value: Any) -> Awaitable[Any] | None:
    return value if inspect.isawaitable(value) else None


async def _maybe_await(value: Any) -> Any:
    awaited = _awaitable(value)
    return await awaited if awaited is not None else value


def _safe_identifier(value: Any) -> str:
    rendered = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(rendered):
        raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
    return rendered


def _safe_timestamp(value: Any) -> float:
    try:
        rendered = float(value)
    except (TypeError, ValueError, OverflowError):
        raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None
    if not math.isfinite(rendered):
        raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
    return rendered


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _chat_side_session_id(owner_id: str, chat_id: str) -> str:
    """Mirror ``muse_chat_service.side_session_id`` without eager DB imports."""

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aoitalk:cli-development-session:v1:{owner_id}:muse-chat:v1:{chat_id}",
        )
    )


def _sanitize_native_failure(
    error: BaseException, fallback: LinkErrorCode = LinkErrorCode.LINK_INTERNAL
) -> AgentLinkFailure:
    """Project an arbitrary native/client error to a fixed safe code.

    Native auth objects already redact themselves.  This extra boundary is
    still needed for injected clients and filesystem implementations: a
    third-party exception may otherwise echo a cookie value in ``str(error)``
    or ``repr(error)``.
    """

    if isinstance(error, AgentLinkFailure):
        return error
    return AgentLinkFailure(fallback)


class _ScopedMuseRecoveryLink:
    """Recovery link that cannot observe another owner's Muse thread."""

    def __init__(self, delegate: Any, *, owner_id: str, thread_ref: str) -> None:
        self._delegate = delegate
        self._owner_id = owner_id
        self._thread_ref = thread_ref

    async def prepare(self, turn: TurnRef, text: str) -> Any:
        return await _maybe_await(self._delegate.prepare(turn, text))

    async def send(self, prepared: Any) -> Any:
        return await _maybe_await(self._delegate.send(prepared))

    async def observe(
        self, turn: TurnRef, baseline: Mapping[str, object]
    ) -> Observation:
        if turn.owner_id != self._owner_id or turn.thread_ref != self._thread_ref:
            # AgentLinkService keeps this row recoverable and performs no
            # provider I/O.  The caller's requested owner/thread can still be
            # observed normally through this wrapper.
            return Observation(
                phase=Phase.SENT,
                complete=False,
                next_poll_after_s=1.0,
            )
        return await _maybe_await(self._delegate.observe(turn, baseline))

    async def health(self) -> Any:
        return await _maybe_await(self._delegate.health())

    async def close(self) -> Any:
        return await _maybe_await(self._delegate.close())


class MuseNativeSideSessionPort(SideSessionPort):
    """Side-session port backed by :class:`~agent_link.muse_native.MuseLink`.

    ``link`` is the preferred test seam.  ``client`` may be a native-link
    object or a factory accepting any subset of ``resolution``, ``cookie``,
    ``auth``, ``gateway``, ``node_id`` and ``node_id_path``.  Production code
    leaves both unset and gets the in-process ``MuseLink`` stack.
    """

    def __init__(
        self,
        config: Any | None = None,
        *,
        journal: SQLiteAgentLinkJournal | None = None,
        service: AgentLinkService | Any | None = None,
        link: Any | None = None,
        native_link: Any | None = None,
        client: Any | None = None,
        cli_port: Any | None = None,
        ownership_port: Any | None = None,
        resolver: Any | None = None,
        target_resolver: Any | None = None,
        privacy_gateway: Any | None = None,
        revision_id: str | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._wall_clock = wall_clock
        self._static_link = link if link is not None else native_link
        self._client_factory = client
        self._ownership_port = cli_port or ownership_port
        self._resolver = resolver or target_resolver
        self._privacy_gateway = privacy_gateway
        self._revision_id = revision_id
        self._ownership: Any | None = None

        if journal is None and service is not None:
            candidate = getattr(service, "journal", None)
            if candidate is not None:
                journal = candidate
        if journal is None:
            journal = SQLiteAgentLinkJournal()
        self.journal = journal
        if service is None:
            # Muse Chat recovery is intentionally longer than the generic
            # agent-link default.  The Chat history path uses the same
            # fourteen-day window, so native rows must remain recoverable for
            # that entire period as well.
            service = AgentLinkService(
                journal, recovery_window_s=RECOVERY_WINDOW.total_seconds()
            )
        self.service = service

        database_path = getattr(journal, "path", None)
        if not isinstance(database_path, (str, os.PathLike)):
            raise ValueError("agent-link journal path is unavailable")
        self._database_path = Path(database_path)
        self._init_thread_schema()

        self._mapping_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._links: dict[str, Any] = {}
        self._resolutions: dict[str, Any] = {}
        self._agent_ids: dict[str, str] = {}
        self._session_threads: dict[tuple[str, str], str] = {}
        self._turns: dict[tuple[str, str, str], TurnRef] = {}
        self._turn_keys: dict[str, str] = {}
        self._reply_staging: dict[tuple[str, str, str], TurnRef] = {}

    def __repr__(self) -> str:
        return "MuseNativeSideSessionPort(<credentials and thread state redacted>)"

    # -- SQLite Chat/thread mapping -----------------------------------------

    def _init_thread_schema(self) -> None:
        try:
            connection = sqlite3.connect(
                os.fspath(self._database_path), timeout=5.0, isolation_level=None
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_TABLE} (
                        owner_id TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        thread_ref TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (owner_id, chat_id)
                    )
                    """
                )
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_owner_created "
                    f"ON {_TABLE}(owner_id, created_at)"
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        except AgentLinkFailure:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None

    def _connection(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                os.fspath(self._database_path), timeout=5.0, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None

    @staticmethod
    def _mapping_lock(
        locks: dict[tuple[str, str], asyncio.Lock], key: tuple[str, str]
    ) -> asyncio.Lock:
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    def _lookup_thread(self, owner_id: str, chat_id: str) -> str | None:
        connection = self._connection()
        try:
            row = connection.execute(
                f"SELECT thread_ref FROM {_TABLE} WHERE owner_id=? AND chat_id=?",
                (owner_id, chat_id),
            ).fetchone()
            if row is None:
                return None
            value = row["thread_ref"]
            if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
                raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
            return value
        except AgentLinkFailure:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    def _persist_thread(
        self, owner_id: str, chat_id: str, thread_ref: str, created_at: float
    ) -> str:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT thread_ref FROM {_TABLE} WHERE owner_id=? AND chat_id=?",
                (owner_id, chat_id),
            ).fetchone()
            if existing is not None:
                value = existing["thread_ref"]
                if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
                    raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
                connection.execute("COMMIT")
                return value
            connection.execute(
                f"INSERT INTO {_TABLE}(owner_id, chat_id, thread_ref, created_at) "
                "VALUES (?, ?, ?, ?)",
                (owner_id, chat_id, thread_ref, created_at),
            )
            connection.execute("COMMIT")
            return thread_ref
        except AgentLinkFailure:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except (KeyboardInterrupt, SystemExit):
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    def _records_for_owner(self, owner_id: str, *, limit: int = 1000) -> list[Any]:
        connection = self._connection()
        try:
            rows = connection.execute(
                f"SELECT owner_id, chat_id, thread_ref, created_at FROM {_TABLE} "
                "WHERE owner_id=? ORDER BY created_at, chat_id LIMIT ?",
                (owner_id, limit),
            ).fetchall()
            return rows
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    # -- target/auth/native-link boundary -----------------------------------

    def _target_resolver(self) -> Any:
        if self._resolver is None:
            from .muse_remote_session_store import MuseRemoteTargetResolver

            self._resolver = MuseRemoteTargetResolver(self.config)
        return self._resolver

    @staticmethod
    async def _call_resolver(resolver: Any, kwargs: Mapping[str, Any]) -> Any:
        callback = getattr(resolver, "resolve", None)
        if not callable(callback):
            if callable(resolver):
                callback = resolver
            else:
                raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
        call_kwargs = dict(kwargs)
        try:
            signature = inspect.signature(callback)
            parameters = signature.parameters
            if not any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            ):
                call_kwargs = {
                    key: value for key, value in call_kwargs.items() if key in parameters
                }
        except (TypeError, ValueError):
            pass
        try:
            return await _maybe_await(callback(**call_kwargs))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise

    async def _resolve_target(
        self,
        *,
        owner_id: str,
        agent_id: str,
        side_sid: str,
        agent_revision_id: str | None = None,
    ) -> Any:
        resolver = self._target_resolver()
        # The Chat adapter may provide the exact immutable revision selected by
        # the Agent service.  Native recovery keeps the server configured pin
        # as the next choice, then uses the historical identity fallback for
        # callers that predate explicit revision pins.
        revision_id = (
            agent_revision_id
            or self._revision_id
            or self._configured_revision_id()
            or agent_id
        )
        kwargs = {
            "owner_id": owner_id,
            "agent_id": agent_id,
            "revision_id": revision_id,
            "agent_revision_id": revision_id,
            "remote_target_key": side_sid,
            "session_id": side_sid,
            "required_capabilities": (REMOTE_AGENT_DELEGATE,),
            "current_authority": None,
            "authority": None,
        }
        try:
            resolution = await self._call_resolver(resolver, kwargs)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except MuseError:
            # MuseError already carries a closed, safe code vocabulary.
            raise
        except AgentLinkFailure:
            raise
        except BaseException:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED) from None
        if resolution is None:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)

        normalizer = getattr(resolution, "normalized", None)
        if callable(normalizer):
            try:
                resolution = normalizer()
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except MuseError:
                raise
            except BaseException:
                raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED) from None

        resolved_owner = _field(resolution, "owner_id")
        if resolved_owner not in (None, "", owner_id) or (
            _field(resolution, "remote_target_key") not in (None, "", side_sid)
        ):
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        resolved_agent = _field(resolution, "agent_id")
        if resolved_agent not in (None, "", agent_id):
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        for field_name in ("remote_target_enabled", "authenticated"):
            field_value = _field(resolution, field_name)
            if field_value is False:
                if field_name == "remote_target_enabled":
                    raise MuseError(MuseErrorCode.REMOTE_TARGET_DISABLED)
                raise MuseError(MuseErrorCode.REMOTE_AUTHENTICATION_FAILED)

        state_root = _field(resolution, "state_root")
        if state_root in (None, ""):
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        try:
            root = Path(state_root)
        except (TypeError, ValueError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED) from None
        if not root.is_absolute() or not root.is_dir():
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        return resolution

    @staticmethod
    def _link_like(value: Any) -> bool:
        return all(callable(getattr(value, name, None)) for name in ("prepare", "send", "observe"))

    async def _custom_link(
        self,
        factory: Any,
        *,
        resolution: Any,
        cookie: Any,
        auth: Any,
        gateway: Any,
        node_id: str | None,
        node_id_path: Path,
    ) -> Any:
        value = factory
        if callable(getattr(factory, "create_link", None)):
            callback = factory.create_link
        elif callable(factory):
            callback = factory
        else:
            return value
        kwargs = {
            "resolution": resolution,
            "cookie": cookie,
            "auth": auth,
            "gateway": gateway,
            "node_id": node_id,
            "node_id_path": node_id_path,
        }
        try:
            signature = inspect.signature(callback)
            parameters = signature.parameters
            if not any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            ):
                kwargs = {key: value for key, value in kwargs.items() if key in parameters}
            value = await _maybe_await(callback(**kwargs))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            raise _sanitize_native_failure(error) from None
        return value

    async def _build_link(self, owner_id: str, resolution: Any) -> Any:
        existing = self._links.get(owner_id)
        if existing is not None:
            return existing
        if self._static_link is not None:
            self._links[owner_id] = self._static_link
            return self._static_link
        # A supplied client object that already implements AgentLink is a
        # complete fake/test seam.  It must not force optional HTTP imports or
        # read a real cookie file merely to exercise the port contract.
        if self._client_factory is not None and callable(
            getattr(self._client_factory, "create_thread", None)
        ) and (
            not callable(self._client_factory) or self._link_like(self._client_factory)
        ):
            self._links[owner_id] = self._client_factory
            return self._client_factory

        root = Path(_field(resolution, "state_root"))
        config_root = root / ".config" / "muse-cli"
        cookie_path = config_root / "cookies.txt"
        raw_node = _field(resolution, "node_id")
        if not isinstance(raw_node, str) or not _NODE_ID_RE.fullmatch(raw_node):
            # The server resolver is expected to return the already approved
            # native device identity.  Falling back to node_id_path would let
            # this adapter create a new identity under the state root and
            # bypass that assertion.
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        node_id = raw_node
        try:
            # Keep optional native transport dependencies lazy.  Tests and
            # deployments that inject ``link`` do not need the HTTP/WebSocket
            # implementation imported at module import time.
            from .agent_link.muse_native.auth import AuthClient, CookieJar
            from .agent_link.muse_native.gateway import GatewayClient
            from .agent_link.muse_native.link import MuseLink

            # CookieJar.from_file is intentionally the only reader.  The path
            # is never put into an exception or a log message.
            cookie = CookieJar.from_file(cookie_path)
            auth = AuthClient(cookie)
            gateway = GatewayClient()
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            raise _sanitize_native_failure(error, LinkErrorCode.MUSE_AUTH_EXPIRED) from None

        node_id_path = config_root / "node-id"
        try:
            if self._client_factory is not None:
                value = await self._custom_link(
                    self._client_factory,
                    resolution=resolution,
                    cookie=cookie,
                    auth=auth,
                    gateway=gateway,
                    node_id=node_id,
                    node_id_path=node_id_path,
                )
            else:
                value = MuseLink(auth, gateway, node_id=node_id)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            raise _sanitize_native_failure(error) from None
        if hasattr(value, "link") and self._link_like(getattr(value, "link")):
            value = value.link
        if not self._link_like(value) or not callable(getattr(value, "create_thread", None)):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        self._links[owner_id] = value
        return value

    async def _link_for_owner(
        self, *, owner_id: str, agent_id: str | None, side_sid: str
    ) -> Any:
        existing = self._links.get(owner_id)
        if existing is not None:
            return existing
        if self._static_link is not None:
            self._links[owner_id] = self._static_link
            return self._static_link
        agent = agent_id or self._agent_ids.get(owner_id) or self._configured_agent_id()
        if not agent:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        resolution = await self._resolve_target(
            owner_id=owner_id, agent_id=agent, side_sid=side_sid
        )
        self._resolutions[owner_id] = resolution
        self._agent_ids[owner_id] = agent
        return await self._build_link(owner_id, resolution)

    def _outbound_privacy_gateway(self) -> Any:
        if self._privacy_gateway is None:
            from .muse_remote_session_store import MuseRemotePrivacyGateway

            self._privacy_gateway = MuseRemotePrivacyGateway(self.config)
        return self._privacy_gateway

    async def _protect_outbound(self, content: str) -> str:
        """Apply the same fail-closed egress/privacy gate as the CLI path."""

        gateway = self._outbound_privacy_gateway()
        callback = gateway if callable(gateway) else getattr(gateway, "protect", None)
        if not callable(callback):
            raise MuseChatUnavailable(
                "OUTBOUND_GATE_REQUIRED", "Museへの送信を許可できませんでした。"
            )
        try:
            value = await _maybe_await(callback(content))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            # The privacy provider may include the source text in an exception.
            # Never propagate it into Chat errors, logs, or diagnostics.
            raise MuseChatUnavailable(
                "OUTBOUND_GATE_DENIED", "Museへの送信を許可できませんでした。"
            ) from None
        if isinstance(value, str):
            protected = value
        else:
            protected = _field(value, "final_payload")
            if protected is None:
                protected = _field(value, "payload")
            if isinstance(value, Mapping) and protected is None:
                protected = value.get("final_payload", value.get("payload"))
        if not isinstance(protected, str) or not protected:
            raise MuseChatUnavailable(
                "OUTBOUND_GATE_DENIED", "Museへの送信を許可できませんでした。"
            )
        return protected

    def _configured_agent_id(self) -> str | None:
        target: Any = {}
        if isinstance(self.config, Mapping):
            remote = self.config.get("remote_targets", {})
            if isinstance(remote, Mapping):
                target = remote.get("muse", {})
        else:
            getter = getattr(self.config, "get", None)
            if callable(getter):
                try:
                    remote = getter("remote_targets", {})
                    if isinstance(remote, Mapping):
                        target = remote.get("muse", {})
                except Exception:
                    target = {}
        if not isinstance(target, Mapping):
            return None
        direct = target.get("chat_agent_id")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        allowed = target.get("allowed_agent_ids")
        if isinstance(allowed, str) and allowed.strip():
            return allowed.strip()
        if isinstance(allowed, (list, tuple, set, frozenset)) and len(allowed) == 1:
            value = next(iter(allowed))
            return str(value).strip() or None
        return None

    def _configured_revision_id(self) -> str | None:
        target: Any = {}
        if isinstance(self.config, Mapping):
            remote = self.config.get("remote_targets", {})
            if isinstance(remote, Mapping):
                target = remote.get("muse", {})
        else:
            getter = getattr(self.config, "get", None)
            if callable(getter):
                try:
                    remote = getter("remote_targets", {})
                    if isinstance(remote, Mapping):
                        target = remote.get("muse", {})
                except Exception:
                    target = {}
        if not isinstance(target, Mapping):
            return None
        for key in ("chat_agent_revision_id", "agent_revision_id", "revision_id"):
            value = target.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    # -- SideSessionPort ----------------------------------------------------

    async def _delegated_ownership_port(self) -> Any:
        if self._ownership_port is not None:
            return self._ownership_port
        if self._ownership is None:
            from .muse_chat_service import CLISideSessionPort

            self._ownership = CLISideSessionPort(self.config)
        return self._ownership

    async def chat_owned(self, chat_id: str, owner_id: str) -> bool:
        try:
            port = await self._delegated_ownership_port()
            method = getattr(port, "chat_owned", None)
            if not callable(method):
                return False
            return bool(await _maybe_await(method(chat_id, owner_id)))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return False

    async def ensure_session(
        self,
        *,
        owner_id: str,
        chat_id: str,
        agent_id: str,
        agent_revision_id: str | None = None,
    ) -> str:
        owner_id = _safe_identifier(owner_id)
        chat_id = _safe_identifier(chat_id)
        agent_id = _safe_identifier(agent_id)
        expected = _chat_side_session_id(owner_id, chat_id)
        resolution = await self._resolve_target(
            owner_id=owner_id,
            agent_id=agent_id,
            side_sid=expected,
            agent_revision_id=agent_revision_id,
        )
        self._resolutions[owner_id] = resolution
        self._agent_ids[owner_id] = agent_id
        lock = self._mapping_lock(self._mapping_locks, (owner_id, chat_id))
        async with lock:
            lock_key = (
                os.path.normcase(os.path.abspath(os.fspath(self._database_path))),
                owner_id,
                chat_id,
            )
            creation_lock = _retain_thread_creation_lock(lock_key)
            acquired = False
            try:
                await _acquire_thread_creation_lock(creation_lock)
                acquired = True
                existing = self._lookup_thread(owner_id, chat_id)
                if existing is None:
                    link = await self._build_link(owner_id, resolution)
                    try:
                        thread_ref = await _maybe_await(
                            link.create_thread(SIDE_SESSION_TITLE)
                        )
                    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                        raise
                    except BaseException as error:
                        raise _sanitize_native_failure(error) from None
                    if not isinstance(thread_ref, str) or not _SESSION_ID_RE.fullmatch(thread_ref):
                        raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
                    existing = self._persist_thread(
                        owner_id,
                        chat_id,
                        thread_ref,
                        _safe_timestamp(self._wall_clock()),
                    )
                self._session_threads[(owner_id, expected)] = existing
                await self._build_link(owner_id, resolution)
            finally:
                if acquired:
                    creation_lock.release()
                _release_thread_creation_lock_reference(lock_key, creation_lock)
        return expected

    @staticmethod
    def _turn_identity(send_key: str) -> tuple[str, str]:
        raw = turn_key_from_send_key(send_key)
        if raw is None:
            raw = str(send_key or "").strip()
        if not raw or not _IDENTIFIER_RE.fullmatch(raw):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        try:
            turn_id = str(uuid.UUID(raw))
        except (TypeError, ValueError, AttributeError):
            turn_id = str(uuid.uuid5(TURN_NAMESPACE, raw))
        return turn_id, raw

    def _thread_for_side(self, owner_id: str, side_sid: str) -> str | None:
        cached = self._session_threads.get((owner_id, side_sid))
        if cached is not None:
            return cached
        for row in self._records_for_owner(owner_id):
            try:
                chat_id = str(row["chat_id"])
                thread_ref = str(row["thread_ref"])
            except (KeyError, TypeError, ValueError):
                continue
            if _chat_side_session_id(owner_id, chat_id) == side_sid:
                self._session_threads[(owner_id, side_sid)] = thread_ref
                return thread_ref
        return None

    def _turn_from_record(self, record: Any) -> TurnRef | None:
        try:
            return TurnRef(
                turn_id=record.turn_id,
                channel=record.channel,
                owner_id=record.owner_id,
                thread_ref=record.thread_ref,
            )
        except (TypeError, ValueError, AttributeError):
            return None

    async def send(
        self, *, side_sid: str, owner_id: str, content: str, send_key: str
    ) -> tuple[str, bool]:
        owner_id = _safe_identifier(owner_id)
        side_sid = _safe_identifier(side_sid)
        turn_id, turn_key = self._turn_identity(send_key)
        thread_ref = self._thread_for_side(owner_id, side_sid)
        if thread_ref is None:
            raise AgentLinkFailure(LinkErrorCode.LINK_JOURNAL_CONFLICT)
        try:
            before = self.journal.get(turn_id)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None
        if before is not None and (
            before.owner_id != owner_id
            or before.channel is not Channel.MUSE
            or before.thread_ref != thread_ref
        ):
            raise AgentLinkFailure(LinkErrorCode.LINK_JOURNAL_CONFLICT)
        turn = self._turn_from_record(before) if before is not None else TurnRef(
            turn_id=turn_id,
            channel=Channel.MUSE,
            owner_id=owner_id,
            thread_ref=thread_ref,
        )
        if turn is None:
            raise AgentLinkFailure(LinkErrorCode.LINK_JOURNAL_CONFLICT)
        self._turns[(owner_id, side_sid, turn_id)] = turn
        self._turn_keys[turn_id] = turn_key
        link = await self._link_for_owner(
            owner_id=owner_id,
            agent_id=self._agent_ids.get(owner_id),
            side_sid=side_sid,
        )
        protected_content = await self._protect_outbound(content)
        try:
            receipt = await self.service.prepare_and_send(link, turn, protected_content)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as error:
            raise _sanitize_native_failure(error) from None
        if not isinstance(receipt, SendReceipt):
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
        try:
            after = self.journal.get(turn_id)
        except BaseException:
            after = None
        created = before is None and after is not None
        return turn_id, created

    @staticmethod
    def _chat_state(observation: Observation) -> tuple[str, str | None]:
        if observation.phase is Phase.REPLIED:
            if observation.complete and isinstance(observation.reply_text, str):
                return STATE_REPLIED, observation.reply_text
            # A partial/uncorrelated provider result is still waiting for a
            # terminal display.  It must never be turned into Chat failure.
            return STATE_PENDING, None
        if observation.phase in {Phase.SENT, Phase.UNCERTAIN, Phase.PREPARED}:
            return STATE_PENDING, None
        if observation.phase in {Phase.FAILED, Phase.REJECTED}:
            return STATE_FAILED, None
        if observation.phase is Phase.EXPIRED:
            return STATE_PENDING, None
        return STATE_UNCERTAIN, None

    async def run_state(
        self, *, side_sid: str, owner_id: str, run_id: str
    ) -> tuple[str, str | None]:
        try:
            owner_id = _safe_identifier(owner_id)
            side_sid = _safe_identifier(side_sid)
            run_id = str(uuid.UUID(str(run_id)))
            record = self.journal.get(run_id)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return STATE_UNCERTAIN, None
        if record is None or record.owner_id != owner_id or record.channel is not Channel.MUSE:
            return STATE_UNCERTAIN, None
        thread_ref = self._thread_for_side(owner_id, side_sid)
        if thread_ref is not None and record.thread_ref != thread_ref:
            return STATE_UNCERTAIN, None
        turn = self._turns.get((owner_id, side_sid, run_id)) or self._turn_from_record(record)
        if turn is None:
            return STATE_UNCERTAIN, None
        if record.phase in {Phase.FAILED, Phase.REJECTED}:
            return STATE_FAILED, None
        if record.phase is Phase.EXPIRED:
            return STATE_PENDING, None
        try:
            link = await self._link_for_owner(
                owner_id=owner_id,
                agent_id=self._agent_ids.get(owner_id),
                side_sid=side_sid,
            )
            observation = await self.service.observe_once(link, turn)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            # A failed history read leaves the durable SENT/UNCERTAIN row
            # untouched and therefore remains a pending Chat turn.
            return STATE_PENDING, None
        if not isinstance(observation, Observation):
            return STATE_PENDING, None
        state, text = self._chat_state(observation)
        if state == STATE_REPLIED and text is not None:
            self._reply_staging[(owner_id, side_sid, run_id)] = turn
        return state, text

    # -- recovery and post-persistence acknowledgement ---------------------

    @staticmethod
    def _since_timestamp(since: datetime, fallback: float) -> float:
        try:
            value = since
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return fallback

    def _recovery_candidates(
        self, *, owner_id: str, side_sid: str, since: float, now: float, limit: int
    ) -> list[Any]:
        thread_ref = self._thread_for_side(owner_id, side_sid)
        if thread_ref is None:
            return []
        try:
            candidates = self.journal.list_recoverable(
                now, channel=Channel.MUSE, limit=max(1, min(limit * 3, 1000))
            )
            candidates += self.journal.list_undelivered_replies(
                now, limit=max(1, min(limit * 3, 1000))
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return []
        unique: dict[str, Any] = {}
        for record in candidates:
            if (
                record.owner_id == owner_id
                and record.channel is Channel.MUSE
                and record.thread_ref == thread_ref
                and record.created_at >= since
            ):
                unique[record.turn_id] = record
        return sorted(unique.values(), key=lambda item: (item.created_at, item.turn_id))

    async def recent_replies(
        self, *, side_sid: str, owner_id: str, since: datetime, limit: int
    ) -> list[tuple[str, str, str]]:
        try:
            owner_id = _safe_identifier(owner_id)
            side_sid = _safe_identifier(side_sid)
            requested = max(1, min(int(limit), 100))
            now = _safe_timestamp(self._wall_clock())
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return []
        since_value = self._since_timestamp(since, now - 1800.0)
        candidates = self._recovery_candidates(
            owner_id=owner_id,
            side_sid=side_sid,
            since=since_value,
            now=now,
            limit=requested,
        )
        if not candidates:
            return []
        thread_ref = self._thread_for_side(owner_id, side_sid)
        if thread_ref is None:
            return []
        try:
            link = await self._link_for_owner(
                owner_id=owner_id,
                agent_id=self._agent_ids.get(owner_id),
                side_sid=side_sid,
            )
            # Keep recovery as the durable state transition/reconciliation
            # pass.  Its Observation type intentionally has no turn id, so
            # reply text is fetched below from each actual REPLIED journal row
            # rather than being paired with a compressed Observation list.
            scoped_link = _ScopedMuseRecoveryLink(
                link, owner_id=owner_id, thread_ref=thread_ref
            )
            await self.service.recover({Channel.MUSE: scoped_link}, now=now)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return []

        # AgentLinkService may skip candidates because of its per-turn poll
        # schedule, a missing link, or a journal conflict.  Re-observe each
        # row independently so a skipped candidate can never shift another
        # row's reply text onto the wrong run id.
        result: list[tuple[str, str, str]] = []
        for candidate in candidates:
            try:
                latest = self.journal.get(candidate.turn_id)
            except BaseException:
                continue
            if (
                latest is None
                or latest.phase is not Phase.REPLIED
                or latest.delivered_at is not None
            ):
                continue
            state, text = await self.run_state(
                side_sid=side_sid, owner_id=owner_id, run_id=latest.turn_id
            )
            if state != STATE_REPLIED or not isinstance(text, str):
                continue
            turn = self._turns.get((owner_id, side_sid, latest.turn_id))
            if turn is None:
                turn = self._turn_from_record(latest)
            if turn is None or not text:
                continue
            self._reply_staging[(owner_id, side_sid, latest.turn_id)] = turn
            turn_key = self._turn_keys.get(latest.turn_id, latest.turn_id)
            result.append((latest.turn_id, turn_key, text))
            if len(result) >= requested:
                break
        return result

    async def acknowledge_delivery(
        self,
        *,
        side_sid: str,
        owner_id: str,
        run_id: str,
    ) -> bool:
        """ACK one reply after the Chat repository has persisted it.

        ``recent_replies`` cannot carry a commit callback in its legacy tuple
        return type.  Callers therefore persist first, then invoke this method
        with the returned run id.  No ACK occurs during ``run_state`` or
        ``recent_replies``.
        """

        try:
            owner_id = _safe_identifier(owner_id)
            side_sid = _safe_identifier(side_sid)
            canonical_run_id = str(uuid.UUID(str(run_id)))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return False
        turn = self._reply_staging.get((owner_id, side_sid, canonical_run_id))
        if turn is None:
            turn = self._turns.get((owner_id, side_sid, canonical_run_id))
        if turn is None:
            try:
                record = self.journal.get(canonical_run_id)
            except BaseException:
                return False
            if (
                record is None
                or record.owner_id != owner_id
                or record.channel is not Channel.MUSE
                or record.thread_ref != self._thread_for_side(owner_id, side_sid)
            ):
                return False
            turn = self._turn_from_record(record)
        if turn is None:
            return False
        try:
            acknowledged = self.service.acknowledge_delivery(turn)
            acknowledged = bool(await _maybe_await(acknowledged))
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            return False
        if acknowledged:
            self._reply_staging.pop((owner_id, side_sid, canonical_run_id), None)
        return acknowledged

    async def acknowledge_recovered_reply(
        self, side_sid: str, owner_id: str, run_id: str
    ) -> bool:
        """Acknowledge a tuple returned by ``recent_replies`` after save.

        ``recover_muse_chat_history`` uses this optional method only after its
        repository write succeeds (or reports that the row was already
        present).  Keeping it separate from ``recent_replies`` prevents a
        failed Chat transaction from losing the native reply.
        """

        return await self.acknowledge_delivery(
            side_sid=side_sid, owner_id=owner_id, run_id=run_id
        )

    # A descriptive alias makes the post-persistence ordering obvious at call
    # sites while keeping the AgentLinkService term available to integrations.
    acknowledge_reply = acknowledge_delivery

    async def close(self) -> None:
        links: list[Any] = []
        if self._static_link is not None:
            links.append(self._static_link)
        links.extend(self._links.values())
        seen: set[int] = set()
        for link in links:
            marker = id(link)
            if marker in seen:
                continue
            seen.add(marker)
            close = getattr(link, "close", None)
            if not callable(close):
                continue
            try:
                await _maybe_await(close())
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException:
                # Closing is best-effort and must not expose native credential
                # or cookie exception text to the caller.
                continue


# Names used by early integration branches are kept as aliases, so the parent
# wiring can choose the descriptive spelling without creating wrappers.
MuseSideSessionPort = MuseNativeSideSessionPort
NativeMuseSideSessionPort = MuseNativeSideSessionPort
MuseNativePort = MuseNativeSideSessionPort
NativeMuseSidePort = MuseNativeSideSessionPort

__all__ = [
    "MuseNativeSideSessionPort",
    "MuseSideSessionPort",
    "NativeMuseSideSessionPort",
    "MuseNativePort",
    "NativeMuseSidePort",
    "TURN_NAMESPACE",
]
