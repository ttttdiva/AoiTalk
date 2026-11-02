"""Ordinary Chat turns for Muse through an owner-only side session.

Muse is a remote agent.  A Chat-tab turn is a *natural-language* exchange:
the owner's text is delivered as-is and Muse's natural reply is returned as the
ordinary assistant message.  There is no tool JSON contract on this path; the
strict scoped tool loop stays a separate, explicit development opt-in.

Delivery reuses the existing Muse remote stack unchanged:

* one owner-only CLI development *side session* per ``(owner, Chat)``.  Its id
  is the service's deterministic ``uuid5(owner, "muse-chat:v1:<chat>")`` so a
  Chat can never address another Chat's or an unknown Muse session;
* the existing remote-target resolver, credential/identity re-checks, privacy
  gateway, nonce correlation and durable Run/Work rows (``send`` with
  ``muse_tool_loop_opt_in=False``);
* ``send_key = "muse-chat:v1:<turn>"`` makes a retried Chat turn idempotent:
  the same key replays the same Run and is never sent twice.

The foreground request performs one immediate state check and returns a
deterministic waiting receipt when Muse has not finished; nothing is resent and
Muse is not asked to cancel (that would poison the binding).  A reply that
lands later is recovered read-only from the side session by
:func:`recover_muse_chat_history` and stored once with a deterministic id.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

PROVIDER_ID = "muse-remote"
MODEL_ID = "muse-chat"
SIDE_SESSION_KEY_PREFIX = "muse-chat:v1:"
SEND_KEY_PREFIX = "muse-chat:v1:"
SIDE_SESSION_TITLE = "Muse Chat"
# A Muse turn may remain queued remotely for a long time.  Keep the durable
# native receipt alive for the same window used by history recovery so a
# process restart cannot make a still-valid late reply unrecoverable after the
# old thirty-minute default expires.
RECOVERY_WINDOW = timedelta(days=14)
MAX_RECOVERY_REPLIES = 3
RECOVERY_REQUEST_TIMEOUT_SECONDS = 8.0
RECOVERY_MIN_INTERVAL_SECONDS = 15.0
REPLY_NAMESPACE = uuid.UUID("0e7e7c7a-4d55-5b6f-9c1d-6d7573656368")
_REMOTE_RECONCILE_ATTEMPTS: dict[str, float] = {}
_REMOTE_RECONCILE_ATTEMPTS_LOCK = threading.Lock()
_REMOTE_RECONCILE_DIAGNOSTICS = frozenset(
    {
        "confirmed",
        "history_unavailable",
        "history_nonterminal",
        "history_unconfirmed",
        "binding_mismatch",
        "authority_unavailable",
        "binding_persist_failed",
        "recovery_state_ineligible",
        "no_pending_nonce",
        "tool_loop_metadata_mismatch",
        "tool_loop_recovery_required",
        "settlement_unavailable",
        "settlement_conflict",
        "reconcile_throttled",
        "reconcile_unavailable",
        "reconcile_unconfirmed",
    }
)

STATE_REPLIED = "replied"
STATE_PENDING = "pending"
STATE_UNCERTAIN = "uncertain"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

_TRUE = {"1", "true", "yes", "on"}


class MuseChatUnavailable(ValueError):
    """Muse Chat is not configured/allowed for this owner (nothing was sent)."""

    def __init__(self, code: str, user_message: str) -> None:
        self.code = code
        self.user_message = user_message
        super().__init__(user_message)


class MuseChatError(RuntimeError):
    """A turn was admitted but no reply can be shown now."""

    def __init__(self, code: str, user_message: str, *, run_id: str | None = None) -> None:
        self.code = code
        self.user_message = user_message
        self.run_id = run_id
        super().__init__(user_message)


@dataclass(frozen=True)
class MuseChatSettings:
    agent_id: str
    allowed_owner_ids: frozenset[str]
    native_link: bool = False
    # Optional exact AgentRevision pin supplied by the server-owned remote
    # target.  When omitted, the CLI session admission resolves the active
    # revision for ``agent_id`` and validates its v2 ``chat`` route.
    agent_revision_id: str | None = None

    def owner_allowed(self, owner_id: str) -> bool:
        return bool(owner_id) and owner_id in self.allowed_owner_ids


@dataclass(frozen=True)
class MuseChatProblem:
    code: str
    message: str


@dataclass(frozen=True)
class MuseChatTurnResult:
    state: str
    run_id: str | None
    side_session_id: str
    text: str | None = None
    created: bool = True


# -- configuration -----------------------------------------------------------


def _config_value(config: Any, dotted: str, default: Any = None) -> Any:
    parts = dotted.split(".")
    if isinstance(config, Mapping):
        current: Any = config
        for part in parts:
            if not isinstance(current, Mapping):
                return default
            current = current.get(part, default if part == parts[-1] else {})
        return current
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    try:
        root = getter(parts[0], None)
    except Exception:
        return default
    if isinstance(root, Mapping):
        return _config_value(root, ".".join(parts[1:]), default) if len(parts) > 1 else root
    return default


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in _TRUE


def _id_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _enterprise(config: Any) -> bool:
    try:
        from ..llm.deployment_resolver import resolve_llm_deployment

        if resolve_llm_deployment(config) is not None:
            return True
    except Exception:
        return True  # unknown deployment state: fail closed
    try:
        from ..features import Features

        return bool(Features.is_enterprise())
    except Exception:
        return False


def _remote_view_enabled() -> bool:
    """Same feature gate as ``CLIDevelopmentSessionService._assert_remote_admission``."""

    try:
        from ..features import Features

        return bool(Features.is_enabled("remote_server_view"))
    except Exception:
        return False


def _owners_with_credential_ref(target: Mapping[str, Any], owners: frozenset[str]) -> frozenset[str]:
    """Owners that admission would give a credential reference (presence only)."""

    if target.get("credential_ref") not in (None, ""):
        return owners
    ready: set[str] = set()
    for key in ("owner_credential_refs", "credentials_by_owner", "credential_refs"):
        refs = target.get(key)
        if isinstance(refs, Mapping):
            ready.update(owner for owner in owners if refs.get(owner) not in (None, ""))
    return frozenset(ready)


def resolve_muse_chat_settings(
    config: Any,
) -> tuple[MuseChatSettings | None, MuseChatProblem | None]:
    """Explicit, secret-free admission for Muse Chat.  No I/O to Muse."""

    if config is None:
        return None, MuseChatProblem("CONFIG_MISSING", "設定を読み込めません。")
    native_link = _config_value(config, "muse_chat.native_link", False)
    if not isinstance(native_link, bool):
        return None, MuseChatProblem(
            "NATIVE_LINK_INVALID", "設定 muse_chat.native_link はbooleanで指定してください。"
        )
    if _enterprise(config):
        return None, MuseChatProblem("ENTERPRISE_DISABLED", "Enterprise構成ではMuse Chatを利用できません。")
    if not _truthy(_config_value(config, "cli_development_sessions.enabled", False)):
        return None, MuseChatProblem(
            "SIDE_SESSIONS_DISABLED",
            "設定 cli_development_sessions.enabled が true ではないため、Museの送信を実行するWork laneがありません。",
        )
    target = _config_value(config, "remote_targets.muse", {})
    if not isinstance(target, Mapping):
        return None, MuseChatProblem("TARGET_NOT_CONFIGURED", "設定 remote_targets.muse が未設定です。")
    for key in ("enabled", "egress_enabled"):
        if not _truthy(target.get(key)):
            return None, MuseChatProblem(
                f"TARGET_{key.upper()}_FALSE",
                f"設定 remote_targets.muse.{key} が true ではありません。",
            )
    if not _remote_view_enabled():
        return None, MuseChatProblem(
            "REMOTE_VIEW_FEATURE_DISABLED",
            "feature remote_server_view が無効のため、Muse remote targetを利用できません。",
        )
    owners = frozenset(_id_list(target.get("allowed_owner_ids")))
    if not owners:
        return None, MuseChatProblem("OWNER_ALLOWLIST_EMPTY", "remote_targets.muse.allowed_owner_ids が空です。")
    owners = _owners_with_credential_ref(target, owners)
    if not owners:
        return None, MuseChatProblem(
            "CREDENTIAL_REF_MISSING",
            "許可ownerに対するMuse認証参照 (remote_targets.muse の credential_ref / owner_credential_refs) がありません。",
        )
    agents = _id_list(target.get("allowed_agent_ids"))
    chat_agent = str(target.get("chat_agent_id") or "").strip()
    if chat_agent:
        if chat_agent not in agents:
            return None, MuseChatProblem(
                "CHAT_AGENT_NOT_ALLOWED",
                "remote_targets.muse.chat_agent_id が allowed_agent_ids に含まれていません。",
            )
    elif len(agents) == 1:
        chat_agent = agents[0]
    else:
        return None, MuseChatProblem(
            "CHAT_AGENT_AMBIGUOUS",
            "Chatで使うMuse Agentを一意に決められません（remote_targets.muse.chat_agent_id を設定してください）。",
        )
    revision_id = None
    for key in ("chat_agent_revision_id", "agent_revision_id", "revision_id"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            revision_id = value.strip()
            break
    return MuseChatSettings(
        agent_id=chat_agent,
        allowed_owner_ids=owners,
        agent_revision_id=revision_id,
        native_link=native_link,
    ), None


def muse_chat_catalog_status(config: Any) -> tuple[bool, MuseChatProblem | None]:
    settings, problem = resolve_muse_chat_settings(config)
    return settings is not None, problem


def require_muse_chat_settings(config: Any) -> MuseChatSettings:
    settings, problem = resolve_muse_chat_settings(config)
    if settings is None:
        assert problem is not None
        raise MuseChatUnavailable(problem.code, problem.message)
    return settings


# -- identities ----------------------------------------------------------------


def side_session_idempotency_key(chat_id: str) -> str:
    return f"{SIDE_SESSION_KEY_PREFIX}{chat_id}"


def side_session_id(owner_id: str, chat_id: str) -> str:
    """Mirror of ``CLIDevelopmentSessionService._session_identity``."""

    from .cli_development_sessions import CLIDevelopmentSessionService

    return str(CLIDevelopmentSessionService._session_identity(owner_id, side_session_idempotency_key(chat_id)))


def turn_send_key(turn_key: str) -> str:
    return f"{SEND_KEY_PREFIX}{turn_key}"


def turn_key_from_send_key(send_key: Any) -> str | None:
    value = str(send_key or "")
    if not value.startswith(SEND_KEY_PREFIX):
        return None
    turn = value[len(SEND_KEY_PREFIX):]
    return turn or None


def late_reply_message_id(side_sid: str, run_id: str) -> str:
    return str(uuid.uuid5(REPLY_NAMESPACE, f"{side_sid}:{run_id}"))


def _status_to_state(status: str) -> str:
    status = str(status or "").lower()
    if status == "completed":
        return STATE_REPLIED
    if status in {"queued", "running"}:
        return STATE_PENDING
    if status in {"cancelled", "cancelling"}:
        return STATE_CANCELLED
    if status == "failed":
        return STATE_FAILED
    return STATE_UNCERTAIN


def _claim_remote_reconcile_attempt(run_id: str, *, now: float | None = None) -> bool:
    """Apply the 15-second per-run history-reconcile interval in this process."""

    current = time.monotonic() if now is None else float(now)
    with _REMOTE_RECONCILE_ATTEMPTS_LOCK:
        last = _REMOTE_RECONCILE_ATTEMPTS.get(run_id)
        if last is not None and current - last < RECOVERY_MIN_INTERVAL_SECONDS:
            return False
        _REMOTE_RECONCILE_ATTEMPTS[run_id] = current
        if len(_REMOTE_RECONCILE_ATTEMPTS) > 1024:
            cutoff = current - RECOVERY_WINDOW.total_seconds()
            for stale_id, attempted_at in tuple(_REMOTE_RECONCILE_ATTEMPTS.items()):
                if attempted_at < cutoff:
                    _REMOTE_RECONCILE_ATTEMPTS.pop(stale_id, None)
            excess = len(_REMOTE_RECONCILE_ATTEMPTS) - 1024
            if excess > 0:
                oldest = sorted(
                    _REMOTE_RECONCILE_ATTEMPTS.items(), key=lambda item: item[1]
                )[:excess]
                for stale_id, _attempted_at in oldest:
                    _REMOTE_RECONCILE_ATTEMPTS.pop(stale_id, None)
        return True


# -- side-session port -------------------------------------------------------------


class SideSessionPort(Protocol):
    async def chat_owned(self, chat_id: str, owner_id: str) -> bool: ...

    async def ensure_session(
        self,
        *,
        owner_id: str,
        chat_id: str,
        agent_id: str,
        agent_revision_id: str | None = None,
    ) -> str: ...

    async def send(self, *, side_sid: str, owner_id: str, content: str, send_key: str) -> tuple[str, bool]: ...

    async def run_state(self, *, side_sid: str, owner_id: str, run_id: str) -> tuple[str, str | None]: ...

    async def recent_replies(
        self, *, side_sid: str, owner_id: str, since: datetime, limit: int
    ) -> list[tuple[str, str, str]]: ...


class CLISideSessionPort:
    """Production port over :class:`CLIDevelopmentSessionService` (DB-backed)."""

    def __init__(self, config: Any, *, service: Any | None = None) -> None:
        if service is None:
            from .cli_development_sessions import CLIDevelopmentSessionService

            service = CLIDevelopmentSessionService(config=config)
        self.service = service

    async def chat_owned(self, chat_id: str, owner_id: str) -> bool:
        from ..memory.models import ConversationSession
        from .cli_development_sessions import CLI_DEVELOPMENT_CONTEXT_KEY

        db = await self.service._session()
        try:
            row = await db.get(ConversationSession, uuid.UUID(str(chat_id)))
            if row is None or getattr(row, "deleted_at", None) is not None:
                return False
            context = row.context if isinstance(row.context, Mapping) else {}
            # A CLI/Muse development session is never itself a Chat target.
            return str(row.user_id) == owner_id and CLI_DEVELOPMENT_CONTEXT_KEY not in context
        except (ValueError, TypeError):
            return False
        finally:
            await self.service._close(db)

    async def _owner_role(self, owner_id: str) -> str | None:
        from ..memory.models import User

        db = await self.service._session()
        try:
            user = await db.get(User, uuid.UUID(owner_id))
            if user is None or not getattr(user, "is_active", False):
                return None
            return str(getattr(user, "role", "") or "") or None
        except (ValueError, TypeError):
            return None
        finally:
            await self.service._close(db)

    async def ensure_session(
        self,
        *,
        owner_id: str,
        chat_id: str,
        agent_id: str,
        agent_revision_id: str | None = None,
    ) -> str:
        role = await self._owner_role(owner_id)
        request = {
            "owner_id": owner_id,
            "agent_id": agent_id,
            "provider": PROVIDER_ID,
            "idempotency_key": side_session_idempotency_key(chat_id),
            "actor_role": role,
            "title": SIDE_SESSION_TITLE,
            "role_context_mode": "raw",
        }
        if agent_revision_id is not None:
            request["agent_revision_id"] = agent_revision_id
        projection = await self.service.create_session(request)
        return str(projection.get("session_id") or "")

    async def send(self, *, side_sid: str, owner_id: str, content: str, send_key: str) -> tuple[str, bool]:
        receipt = await self.service.send(
            side_sid,
            {
                "owner_id": owner_id,
                "content": content,
                "send_key": send_key,
                "muse_tool_loop_opt_in": False,
            },
        )
        return str(receipt.get("run_id") or ""), bool(receipt.get("created"))

    async def run_state(self, *, side_sid: str, owner_id: str, run_id: str) -> tuple[str, str | None]:
        from ..memory.models import AgentRun, AgentWorkItem, ConversationMessage, ConversationSession
        from .cli_development_sessions import REMOTE_SESSION_SOURCE_TYPE

        db = await self.service._session()
        try:
            row = await db.get(ConversationSession, uuid.UUID(side_sid))
            run = await db.get(AgentRun, uuid.UUID(run_id))
            if (
                row is None
                or run is None
                or str(row.user_id) != owner_id
                or str(run.session_id) != side_sid
                or str(run.user_id or "") != owner_id
                or str(run.run_type or "") != REMOTE_SESSION_SOURCE_TYPE
            ):
                return STATE_UNCERTAIN, None
            work = await db.get(AgentWorkItem, run.work_item_id) if run.work_item_id else None
            state = _status_to_state(self.service._status_from_rows(self.service._context(row), run, work))
            if state != STATE_REPLIED:
                return state, None
            result = run.result if isinstance(run.result, Mapping) else {}
            message_id = str(result.get("assistant_message_id") or "")
            message = await db.get(ConversationMessage, uuid.UUID(message_id)) if message_id else None
            metadata = getattr(message, "message_metadata", None)
            if (
                message is None
                or str(message.session_id) != side_sid
                or str(message.role or "") != "assistant"
                or not isinstance(metadata, Mapping)
                or str(metadata.get("run_id") or "") != run_id
                or not isinstance(message.content, str)
            ):
                return STATE_UNCERTAIN, None
            return STATE_REPLIED, message.content
        except (ValueError, TypeError):
            return STATE_UNCERTAIN, None
        finally:
            await self.service._close(db)

    async def recent_replies(
        self, *, side_sid: str, owner_id: str, since: datetime, limit: int
    ) -> list[tuple[str, str, str]]:
        from sqlalchemy import desc, select

        from ..memory.models import AgentRun
        from .cli_development_sessions import REMOTE_SESSION_SOURCE_TYPE
        from .muse_remote_execution import MuseErrorCode

        # AgentRun.created_at is a timezone-naive DateTime storing UTC. Keep
        # the public caller contract timezone-aware, but bind UTC-naive values
        # to this column so PostgreSQL does not compare timestamptz with
        # timestamp without time zone.
        if since.tzinfo is not None and since.utcoffset() is not None:
            since = since.astimezone(timezone.utc).replace(tzinfo=None)

        db = await self.service._session()
        try:
            runs = (
                (
                    await db.execute(
                        select(AgentRun)
                        .where(
                            AgentRun.session_id == uuid.UUID(side_sid),
                            AgentRun.user_id == owner_id,
                            AgentRun.run_type == REMOTE_SESSION_SOURCE_TYPE,
                            AgentRun.created_at >= since,
                        )
                        .order_by(desc(AgentRun.created_at))
                        .limit(max(1, limit * 3))
                    )
                )
                .scalars()
                .all()
            )
            candidates = [
                (
                    str(run.id),
                    str(run.client_message_id or ""),
                    str(run.status or ""),
                    str(run.error or ""),
                )
                for run in runs
            ]
        finally:
            await self.service._close(db)

        # A normal Chat send may have ended as failed/uncertain locally even
        # though Muse later committed a terminal reply. Ask the existing
        # strict reconnect reconciler to inspect provider history once for the
        # most recent eligible Muse Chat turn. It independently verifies the
        # nonce, owner, run/work, binding, and terminal events before settling;
        # this path never invokes send/execute or requeues work.
        uncertain_errors = {
            str(MuseErrorCode.SEND_RESPONSE_UNCORRELATED),
            "cli_process_failed",
        }
        reconcile_candidate = next(
            (
                (run_id, error_code)
                for run_id, send_key, status, error_code in candidates
                if turn_key_from_send_key(send_key) is not None
                and status == "failed"
                and error_code in uncertain_errors
            ),
            None,
        )
        if reconcile_candidate is not None:
            reconcile = getattr(
                self.service, "_reconcile_remote_history_on_reconnect", None
            )
            run_id, _error_code = reconcile_candidate
            if not callable(reconcile):
                diagnostic = "reconcile_unavailable"
            elif not _claim_remote_reconcile_attempt(run_id):
                diagnostic = "reconcile_throttled"
            else:
                try:
                    outcome = await reconcile(side_sid, owner_id=owner_id)
                    raw_diagnostic = (
                        outcome.get("reconciliation_diagnostic")
                        if isinstance(outcome, Mapping)
                        else None
                    )
                    diagnostic = (
                        str(raw_diagnostic)
                        if str(raw_diagnostic or "")
                        in _REMOTE_RECONCILE_DIAGNOSTICS
                        else "reconcile_unconfirmed"
                    )
                except Exception:
                    diagnostic = "reconcile_unavailable"
            logger.info("Muse Chat remote history reconciliation: %s", diagnostic)

        replies: list[tuple[str, str, str]] = []
        for run_id, send_key, _status, _error_code in candidates:
            turn = turn_key_from_send_key(send_key)
            if turn is None:
                continue
            state, text = await self.run_state(side_sid=side_sid, owner_id=owner_id, run_id=run_id)
            if state == STATE_REPLIED and text:
                replies.append((run_id, turn, text))
            if len(replies) >= limit:
                break
        return replies


# -- service -------------------------------------------------------------------------


class MuseChatService:
    """Owner-scoped natural-language Muse turn for one Chat."""

    def __init__(
        self,
        settings: MuseChatSettings,
        port: SideSessionPort,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.port = port
        self._sleep = sleep
        self._clock = clock

    async def _side_session(self, owner_id: str, chat_id: str) -> str:
        if not self.settings.owner_allowed(owner_id):
            raise MuseChatUnavailable("OWNER_NOT_ALLOWED", "このユーザーはMuse Chatの利用を許可されていません。")
        if not await self.port.chat_owned(chat_id, owner_id):
            raise MuseChatUnavailable("CHAT_NOT_OWNED", "このChatの所有者ではないため、Museへ送信できません。")
        expected = side_session_id(owner_id, chat_id)
        session_kwargs: dict[str, Any] = {
            "owner_id": owner_id,
            "chat_id": chat_id,
            "agent_id": self.settings.agent_id,
        }
        if self.settings.agent_revision_id is not None:
            session_kwargs["agent_revision_id"] = self.settings.agent_revision_id
        actual = await self.port.ensure_session(
            **session_kwargs,
        )
        if actual != expected:
            # Never deliver into a session this Chat did not derive.
            raise MuseChatUnavailable("SIDE_SESSION_MISMATCH", "Muse Chatの送信先セッションが一致しないため送信しません。")
        return expected

    async def send_turn(
        self,
        owner_id: str,
        chat_id: str,
        turn_key: str,
        text: str,
        *,
        cancel_event: Any | None = None,
    ) -> MuseChatTurnResult:
        owner_id = str(owner_id or "").strip()
        chat_id = str(chat_id or "").strip()
        turn_key = str(turn_key or "").strip()
        if not owner_id or not chat_id or not turn_key:
            raise MuseChatUnavailable("CHAT_SESSION_REQUIRED", "Muse ChatはログインユーザーのChatセッション内でのみ送信できます。")
        if not isinstance(text, str) or not text.strip():
            raise MuseChatUnavailable("EMPTY_INPUT", "送信する本文がありません。")
        side_sid = await self._side_session(owner_id, chat_id)
        run_id, created = await self.port.send(
            side_sid=side_sid,
            owner_id=owner_id,
            content=text,
            send_key=turn_send_key(turn_key),
        )
        if not run_id:
            return MuseChatTurnResult(STATE_UNCERTAIN, None, side_sid, created=created)
        # The Chat request is deliberately a send-and-return boundary.  A
        # foreground request must never hold the websocket open for the whole
        # remote generation window: the durable row and the recovery worker
        # own the later observation.  One immediate read is still useful for a
        # very fast reply, but it is the only state check made by this turn.
        state, reply = await self.port.run_state(
            side_sid=side_sid, owner_id=owner_id, run_id=run_id
        )
        if state != STATE_PENDING:
            return MuseChatTurnResult(
                state,
                run_id,
                side_sid,
                text=reply if state == STATE_REPLIED else None,
                created=created,
            )
        if cancel_event is not None and cancel_event.is_set():
            # Local stop only: no remote cancel, no resend.
            return MuseChatTurnResult(STATE_CANCELLED, run_id, side_sid, created=created)
        return MuseChatTurnResult(STATE_PENDING, run_id, side_sid, created=created)


# -- late reply recovery -------------------------------------------------------------


async def _message_or_none(repository: Any, message_id: str) -> Any:
    try:
        return await repository.get_message_by_id(message_id)
    except ValueError:
        return None


def _late_run_id(row: Any) -> str | None:
    metadata = getattr(row, "message_metadata", None)
    muse = metadata.get("muse_chat") if isinstance(metadata, Mapping) else None
    if isinstance(muse, Mapping) and muse.get("late_reply"):
        return str(muse.get("run_id") or "") or None
    return None


async def _late_reply_placement(repository: Any, chat_id: str, pending_row: Any) -> tuple[str | None, bool]:
    """``(original user id, replace waiting display)`` for a late reply.

    The reply is always an explicit child of the original user turn, never of
    the waiting assistant row (``add_message`` without a parent would attach it
    to the latest active row).  It replaces the waiting display only while that
    row is still the active leaf; otherwise newer turns stay active and the
    reply remains an inactive sibling under the original user turn.
    """

    if str(getattr(pending_row, "role", "")) == "user":
        parent = getattr(pending_row, "id", None)
    else:
        parent = getattr(pending_row, "parent_message_id", None)
    if not parent:
        return None, False
    active = await repository.get_active_branch_messages(chat_id)
    is_leaf = bool(active) and str(active[-1].id) == str(pending_row.id)
    return str(parent), is_leaf


async def _retry_active_late_reply_switch(
    repository: Any,
    chat_id: str,
    reply_row: Any,
    pending_row: Any,
) -> None:
    """Retry an interrupted conditional switch while the original path is active."""

    switch = getattr(repository, "switch_active_branch", None)
    if not callable(switch):
        return
    active = await repository.get_active_branch_messages(chat_id)
    if not active:
        return
    leaf_id = str(active[-1].id)
    reply_id = str(getattr(reply_row, "id", ""))
    if leaf_id == reply_id:
        return
    pending_id = str(getattr(pending_row, "id", ""))
    reply_parent_id = str(getattr(reply_row, "parent_message_id", "") or "")
    if leaf_id not in {pending_id, reply_parent_id}:
        # A newer user turn owns the active path. The saved reply remains an
        # inactive sibling reachable from its original turn.
        return
    switched = await switch(
        chat_id,
        reply_id,
        expected_active_leaf_id=leaf_id,
    )
    if switched is False:
        active_after = await repository.get_active_branch_messages(chat_id)
        if active_after and str(active_after[-1].id) == leaf_id:
            # Leave the journal row unacknowledged so a later recovery pass
            # retries instead of treating an invisible reply as delivered.
            raise RuntimeError("Muse Chat late reply branch switch did not complete")


async def _persist_late_reply(
    repository: Any, chat_id: str, side_sid: str, run_id: str, turn_key: str, text: str
) -> bool:
    from .agent_run_service import sanitize_assistant_display_text

    message_id = late_reply_message_id(side_sid, run_id)
    existing = await _message_or_none(repository, message_id)
    if existing is not None:
        if _late_run_id(existing) == run_id:
            pending_row = await _message_or_none(repository, turn_key)
            if (
                pending_row is not None
                and str(getattr(pending_row, "session_id", "")) == chat_id
            ):
                await _retry_active_late_reply_switch(
                    repository, chat_id, existing, pending_row
                )
        return False
    pending_row = await _message_or_none(repository, turn_key)
    if pending_row is None or str(getattr(pending_row, "session_id", "")) != chat_id:
        # No Chat row for this turn: the parent is unknown, so do not guess.
        return False
    content = sanitize_assistant_display_text(text)
    if not content:
        return False
    if str(getattr(pending_row, "role", "")) == "assistant" and str(getattr(pending_row, "content", "") or "") == content:
        return False  # delivered in time on the normal path
    parent = getattr(pending_row, "parent_message_id", None)
    metadata = {
        "client_message_id": f"muse-chat-reply:{run_id}",
        "muse_chat": {
            "run_id": run_id,
            "late_reply": True,
            "pending_message_id": str(pending_row.id),
            "reply_to_message_id": str(parent) if parent else None,
        },
    }
    original_user_id, replace_waiting = await _late_reply_placement(repository, chat_id, pending_row)
    if original_user_id is None:
        return False  # the original user turn is unknown: do not guess a parent
    try:
        # Stored inactive first so a newer turn is never displaced; the active
        # path is switched below only if the waiting row is still the leaf.
        saved = await repository.add_message(
            session_id=chat_id,
            role="assistant",
            content=content,
            metadata=metadata,
            message_id=message_id,
            explicit_parent_message_id=original_user_id,
            is_active_branch=False,
        )
    except Exception:
        existing = await _message_or_none(repository, message_id)
        if existing is not None:
            if _late_run_id(existing) == run_id:
                await _retry_active_late_reply_switch(
                    repository, chat_id, existing, pending_row
                )
            return False
        raise
    if getattr(saved, "_idempotency_replayed", False):
        await _retry_active_late_reply_switch(repository, chat_id, saved, pending_row)
        return False  # a concurrent recovery already stored this reply
    if replace_waiting and hasattr(repository, "switch_active_branch"):
        # Conditional switch: the repository re-checks the active leaf under the
        # session-row lock in the same transaction.  On a mismatch (a newer turn
        # arrived) nothing changes and the reply stays an inactive child of U1.
        switched = await repository.switch_active_branch(
            chat_id, message_id, expected_active_leaf_id=str(pending_row.id)
        )
        if switched is False:
            active = await repository.get_active_branch_messages(chat_id)
            if active and str(active[-1].id) == str(pending_row.id):
                raise RuntimeError(
                    "Muse Chat late reply branch switch did not complete"
                )
    return True


async def _close_side_session_port(port: Any) -> None:
    close = getattr(port, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # Closing the native socket is best-effort and must not affect history.
        pass


async def recover_muse_chat_history(
    config: Any,
    *,
    session: Any,
    requester_id: str,
    repository: Any,
    port_factory: Callable[[Any], SideSessionPort] | None = None,
    timeout: float | None = None,
    now: datetime | None = None,
) -> int:
    """Read-only recovery of late Muse replies into this Chat (owner only)."""

    if config is None or session is None:
        return 0
    owner_id = str(getattr(session, "user_id", "") or "")
    if not owner_id or str(requester_id or "") != owner_id:
        return 0
    settings, _problem = resolve_muse_chat_settings(config)
    if settings is None or not settings.owner_allowed(owner_id):
        return 0
    chat_id = str(getattr(session, "id", "") or "")
    if not chat_id:
        return 0
    side_sid = side_session_id(owner_id, chat_id)
    if port_factory is not None:
        port = port_factory(config)
    elif settings.native_link:
        from .muse_native_port import NativeMuseSidePort

        port = NativeMuseSidePort(config)
    else:
        port = CLISideSessionPort(config)
    try:
        since = (now or datetime.now(timezone.utc)) - RECOVERY_WINDOW
        try:
            replies = await asyncio.wait_for(
                port.recent_replies(
                    side_sid=side_sid,
                    owner_id=owner_id,
                    since=since,
                    limit=MAX_RECOVERY_REPLIES,
                ),
                RECOVERY_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout,
            )
        except asyncio.TimeoutError:
            logger.info("Muse Chat history recovery skipped: history_timeout")
            return 0
        except Exception:  # read-only: never fail the history read
            logger.info("Muse Chat history recovery skipped: history_read_unavailable")
            return 0
        inserted = 0
        for run_id, turn_key, text in replies:
            try:
                persisted = await _persist_late_reply(
                    repository, chat_id, side_sid, run_id, turn_key, text
                )
                if persisted:
                    inserted += 1
                acknowledge = getattr(port, "acknowledge_recovered_reply", None)
                if callable(acknowledge):
                    late_id = late_reply_message_id(side_sid, run_id)
                    already_saved = await _message_or_none(repository, late_id) is not None
                    pending_row = await _message_or_none(repository, turn_key)
                    from .agent_run_service import sanitize_assistant_display_text

                    expected_content = sanitize_assistant_display_text(text)
                    delivered_in_turn = (
                        pending_row is not None
                        and str(getattr(pending_row, "role", "")) == "assistant"
                        and str(getattr(pending_row, "content", "") or "") == expected_content
                    )
                    if persisted or already_saved or delivered_in_turn:
                        await acknowledge(
                            side_sid=side_sid, owner_id=owner_id, run_id=run_id
                        )
            except Exception:
                # A history read must remain available even if one recovered row
                # cannot be committed. Do not log provider text or exception data.
                logger.info(
                    "Muse Chat history recovery skipped: reply_persistence_unavailable"
                )
        return inserted
    finally:
        await _close_side_session_port(port)


__all__ = [
    "MODEL_ID",
    "PROVIDER_ID",
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_PENDING",
    "STATE_REPLIED",
    "STATE_UNCERTAIN",
    "CLISideSessionPort",
    "MuseChatError",
    "MuseChatProblem",
    "MuseChatService",
    "MuseChatSettings",
    "MuseChatTurnResult",
    "MuseChatUnavailable",
    "late_reply_message_id",
    "muse_chat_catalog_status",
    "recover_muse_chat_history",
    "require_muse_chat_settings",
    "resolve_muse_chat_settings",
    "side_session_id",
    "turn_send_key",
]
