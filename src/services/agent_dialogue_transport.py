"""Provider-free AgentDialogue transport over the common AgentWork ledger.

Transport owns delivery leases, order, idempotency, handoff and terminal
receipts.  Dialogue job/control/input/auth state transitions belong to the
dialogue service.  Atomic child Chat Run/Work/outbox creation belongs to the
bridge seam below.  Dialogue rows store references only; text remains in the
encrypted ``conversation_messages`` table.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from ..memory.database import get_database_manager
from ..memory.models import (
    Agent,
    AgentDialogue,
    AgentDialogueDelivery,
    AgentDialogueInput,
    AgentDialogueReservation,
    AgentDialogueUsage,
    AgentRevision,
    AgentRun,
    AgentRunEvent,
    AgentWorkItem,
    AgentWorkEvent,
    ConversationDispatchOutbox,
    ConversationMessage,
    ConversationSession,
    User,
)
from .agent_dialogue_projection import project_dialogue
from .agent_work_runtime import (
    AgentWorkCoordinator,
    ExecutionOutcome,
    InteractiveAdmissionError,
    WorkClaim,
)

logger = logging.getLogger(__name__)

SOURCE_TYPE = "agent_dialogue"
EXECUTION_ADAPTER = "dialogue_transport"
MAX_DEPTH = 32
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_RUN_STATES = frozenset({"queued", "running", "awaiting_approval", "uncertain"})
_ACTIVE_WORK_STATES = frozenset({"pending", "claimed", "running", "awaiting_approval", "uncertain"})
_TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
_SAFE_TERMINAL_FAILURE_PREFIXES = ("agent_route_", "interactive_admission_")
_BLOCKED_AUTHORITY_PREFIX = "interactive_admission_authority_"
_PERMANENT_DIALOGUE_ADMISSION_PREFIXES = ("dialogue_answer_root_",)
_ANSWER_RECOVERY_BUDGET_FAILURE_CODES = frozenset(
    {
        "dialogue_turn_budget_exhausted",
        "dialogue_attempt_budget_exhausted",
        "dialogue_token_budget_exhausted",
        "dialogue_cost_budget_exhausted",
    }
)


class DialogueDispatchBridgeProtocol:
    """Explicit atomic bridge seam; no signature probing or provider calls."""

    async def dispatch_trusted_agent_dialogue(
        self, *, session: Any, dialogue: Any, delivery: Any,
        source_message: Any, provenance_hash: str,
    ) -> Mapping[str, Any]:
        raise NotImplementedError


class DialogueTransportError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = str(code or "agent_dialogue_transport_error")[:96]
        super().__init__(message or self.code)


class DialogueTransportConflict(DialogueTransportError, ValueError):
    pass


class DialogueTransportUnavailable(DialogueTransportError):
    pass


def _now() -> datetime:
    return datetime.utcnow()


def _id(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _text(value: Any, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def _uuid(value: Any, name: str, *, required: bool = True) -> uuid.UUID | None:
    if value in (None, ""):
        if required:
            raise DialogueTransportError(f"{name}_required")
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DialogueTransportError(f"{name}_invalid") from exc


def _same_uuid(left: Any, right: Any) -> bool:
    """Compare UUID columns across ORM/driver representations."""

    if left in (None, "") or right in (None, ""):
        return left in (None, "") and right in (None, "")
    try:
        return uuid.UUID(str(left)) == uuid.UUID(str(right))
    except (TypeError, ValueError, AttributeError):
        return False


def _permanent_dialogue_admission_code(error: BaseException) -> str | None:
    """Return an immutable Root binding denial without guessing its cause."""

    if not isinstance(error, ValueError):
        return None
    code = _text(getattr(error, "code", None), 96).casefold()
    if not code or not code.startswith(_PERMANENT_DIALOGUE_ADMISSION_PREFIXES):
        return None
    return code


def _run_has_dialogue_input(run: Any) -> bool:
    """Return whether a failed Run is the durable question pause attempt."""

    if _text(_field(run, "error"), 96).casefold() == "dialogue_input_pending":
        return True
    metadata = _field(run, "run_metadata", {})
    if not isinstance(metadata, Mapping):
        return False
    return bool(
        metadata.get("dialogue_input_pending") is True
        or metadata.get("dialogue_input_id")
    )


def _is_missing_dialogue_input_table_error(exc: BaseException) -> bool:
    """Recognize only the optional input-ledger table absence.

    SQLite reports this as an OperationalError, while PostgreSQL drivers
    commonly wrap SQLSTATE 42P01 in ProgrammingError/UndefinedTableError.
    Keep the target-table check alongside the driver-specific marker so an
    unrelated error whose text happens to mention the table is re-raised.
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        text = str(current).casefold()
        if re.search(r"no such table\s*:\s*[`\"']?agent_dialogue_inputs\b", text):
            return True
        if re.search(r"(?:relation|table)\s+[`\"']?agent_dialogue_inputs[`\"']?\s+(?:does not exist|not found)", text):
            sqlstate = {
                str(getattr(current, "sqlstate", "")).casefold(),
                str(getattr(current, "pgcode", "")).casefold(),
            }
            if "42p01" in sqlstate or "undefinedtable" in type(current).__name__.casefold():
                return True
        for nested in (
            getattr(current, "orig", None),
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _run_work_state_matches(run: Any, work: Any) -> bool:
    """Keep terminal Run and Work state as one typed execution fact.

    A durable question pause is represented by a failed Run and blocked Work;
    all other terminal callbacks require the same terminal state on both rows.
    Active source rows may still be in any common claim/running state while a
    delivery is being acknowledged.
    """

    status = _text(_field(run, "status"), 32).casefold()
    state = _text(_field(work, "state"), 32).casefold()
    if status in _TERMINAL_RUN_STATES:
        if status == "failed" and state == "blocked" and _run_has_dialogue_input(run):
            return True
        return state == status
    if status in _ACTIVE_RUN_STATES:
        return state in _ACTIVE_WORK_STATES
    return False


def _blocked_authority_shape(run: Any, work: Any) -> str | None:
    """Return the exact typed pre-provider authority block code, if valid."""

    if _text(_field(run, "status"), 32).casefold() != "failed" or _text(_field(work, "state"), 32).casefold() != "blocked":
        return None
    if _text(_field(work, "outcome_classification"), 32).casefold() != "blocked":
        return None
    code = _text(_field(work, "safe_error_code"), 96).casefold()
    if not code.startswith(_BLOCKED_AUTHORITY_PREFIX) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
        return None
    evidence_codes: list[str] = [code]
    evidence_values: list[Any] = []
    for evidence in (
        _field(run, "run_metadata", {}),
        _field(run, "result", {}),
        _field(work, "metadata_json", {}),
        _field(work, "result_summary_json", {}),
    ):
        if not isinstance(evidence, Mapping):
            continue
        if evidence.get("safe_error_code") not in (None, ""):
            evidence_codes.append(_text(evidence.get("safe_error_code"), 96).casefold())
        if "provider_started" in evidence:
            evidence_values.append(evidence.get("provider_started"))
    if any(item != code for item in evidence_codes):
        return None
    if any(value is True for value in evidence_values) or not any(value is False for value in evidence_values):
        return None
    return code


async def _blocked_authority_proof(session: Any, run: Any, work: Any) -> bool:
    """Require server event evidence for a pre-provider blocked admission."""

    code = _blocked_authority_shape(run, work)
    if code is None:
        return False
    values: list[Any] = []
    codes: list[str] = []
    try:
        run_events = (
            await session.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run.id)
                .order_by(AgentRunEvent.sequence.desc())
                .limit(16)
            )
        ).scalars().all()
        work_events = (
            await session.execute(
                select(AgentWorkEvent)
                .where(AgentWorkEvent.work_item_id == work.id)
                .order_by(AgentWorkEvent.sequence.desc())
                .limit(16)
            )
        ).scalars().all()
    except Exception:
        return False
    for event in [*run_events, *work_events]:
        payload = _field(event, "payload", _field(event, "payload_json", {}))
        if isinstance(payload, Mapping):
            if payload.get("safe_error_code") not in (None, ""):
                codes.append(_text(payload.get("safe_error_code"), 96).casefold())
            if "provider_started" in payload:
                values.append(payload.get("provider_started"))
        event_code = _text(_field(event, "safe_error_code"), 96).casefold()
        if event_code:
            codes.append(event_code)
    if any(item != code for item in codes):
        return False
    return any(value is False for value in values) and not any(value is True for value in values)


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _field(row: Any, name: str, default: Any = None) -> Any:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _canonical(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            name = str(key)
            output[name[:96]] = _canonical(item, depth + 1)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_canonical(item, depth + 1) for item in list(value)[:64]]
    return None


def sha256_digest(value: Any) -> str:
    if isinstance(value, bytes):
        material = value
    elif isinstance(value, str):
        material = value.encode("utf-8")
    else:
        material = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


request_digest = sha256_digest


def _safe_refs(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    keys = {
        "dialogue_id", "delivery_id", "source_session_id", "source_agent_id", "source_agent_revision_id",
        "source_run_id", "source_work_item_id", "source_message_id", "receiver_session_id", "receiver_agent_id",
        "receiver_agent_revision_id", "receiver_run_id", "receiver_work_item_id", "receiver_message_id",
        "transport_work_item_id", "root_session_id", "worker_session_id", "root_agent_id", "root_agent_revision_id",
        "worker_agent_id", "worker_agent_revision_id", "project_id", "task_id", "control_revision",
        "expected_control_revision", "depth", "sequence", "causation_id", "provenance_hash", "owner_user_id",
        "input_id", "kind", "source_type", "execution_adapter",
        "resource_key", "workspace_resource_key",
    }
    result: dict[str, Any] = {}
    for key in keys:
        item = raw.get(key)
        if item not in (None, "") and isinstance(item, (str, int, bool)):
            result[key] = str(item) if key.endswith("_id") else item
    return result


@dataclass(frozen=True)
class DialogueDeliveryRequest:
    dialogue_id: str
    source_session_id: str
    receiver_session_id: str
    idempotency_key: str
    request_digest: str
    source_agent_id: str | None = None
    source_agent_revision_id: str | None = None
    source_run_id: str | None = None
    source_work_item_id: str | None = None
    source_message_id: str | None = None
    receiver_agent_id: str | None = None
    receiver_agent_revision_id: str | None = None
    kind: str = "request"
    sequence: int | None = None
    causation_id: str | None = None
    depth: int = 0
    expected_control_revision: int = 0
    available_at: datetime | None = None
    deadline: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def normalized(self) -> "DialogueDeliveryRequest":
        digest = _text(self.request_digest, 64).casefold()
        if not _text(self.idempotency_key, 255):
            raise DialogueTransportError("idempotency_key_required")
        if not _SHA256.fullmatch(digest):
            raise DialogueTransportError("request_digest_invalid")
        kind = _text(self.kind, 32).casefold() or "request"
        if kind not in {"request", "response", "control", "event"}:
            raise DialogueTransportError("delivery_kind_invalid")
        depth = int(self.depth or 0)
        if not 0 <= depth <= MAX_DEPTH:
            raise DialogueTransportError("dialogue_depth_exceeded")
        expected = int(self.expected_control_revision or 0)
        if expected < 0:
            raise DialogueTransportError("control_revision_invalid")
        sequence = None if self.sequence is None else int(self.sequence)
        if sequence is not None and sequence < 0:
            raise DialogueTransportError("delivery_sequence_invalid")
        available = _datetime(self.available_at)
        deadline = _datetime(self.deadline)
        if self.available_at not in (None, "") and available is None:
            raise DialogueTransportError("availability_invalid")
        if self.deadline not in (None, "") and deadline is None:
            raise DialogueTransportError("deadline_invalid")
        if available is not None and deadline is not None and deadline < available:
            raise DialogueTransportError("deadline_before_availability")
        return DialogueDeliveryRequest(
            dialogue_id=str(_uuid(self.dialogue_id, "dialogue_id")),
            source_session_id=str(_uuid(self.source_session_id, "source_session_id")),
            receiver_session_id=str(_uuid(self.receiver_session_id, "receiver_session_id")),
            idempotency_key=_text(self.idempotency_key, 255), request_digest=digest,
            source_agent_id=_id(self.source_agent_id), source_agent_revision_id=_id(self.source_agent_revision_id),
            source_run_id=_id(self.source_run_id), source_work_item_id=_id(self.source_work_item_id),
            source_message_id=_id(self.source_message_id), receiver_agent_id=_id(self.receiver_agent_id),
            receiver_agent_revision_id=_id(self.receiver_agent_revision_id), kind=kind, sequence=sequence,
            causation_id=_text(self.causation_id, 255) or None, depth=depth,
            expected_control_revision=expected, available_at=available, deadline=deadline,
            metadata=_safe_refs(self.metadata),
        )


@dataclass(frozen=True)
class TransportReceipt:
    delivery_id: str
    dialogue_id: str
    state: str
    transport_work_item_id: str | None = None
    receiver_run_id: str | None = None
    receiver_work_item_id: str | None = None
    receiver_message_id: str | None = None
    response_delivery_id: str | None = None
    response_digest: str | None = None
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class AgentDialogueTransport:
    source_type = SOURCE_TYPE
    execution_adapter = EXECUTION_ADAPTER

    def __init__(self, db_manager: Any | None = None, *, dispatch_bridge: DialogueDispatchBridgeProtocol | None = None, agent_run_service: Any | None = None, coordinator: AgentWorkCoordinator | None = None, config: Any | None = None, worker_id: str = "dialogue-transport", clock: Callable[[], datetime] | None = None) -> None:
        self._db_manager = db_manager or get_database_manager()
        if dispatch_bridge is not None and agent_run_service is not None:
            raise DialogueTransportError("dialogue_bridge_duplicate")
        # Transitional callers pass the canonical AgentRunService object. A
        # direct transport construction resolves that same canonical bridge
        # lazily; no legacy create_or_get/fallback signature is inferred.
        if dispatch_bridge is None and agent_run_service is None:
            from .agent_run_service import AgentRunService

            agent_run_service = AgentRunService(self._db_manager, config=config)
        self.dispatch_bridge = dispatch_bridge or agent_run_service
        if self.dispatch_bridge is not None and not callable(getattr(self.dispatch_bridge, "dispatch_trusted_agent_dialogue", None)):
            raise DialogueTransportUnavailable("dialogue_dispatch_bridge_unavailable")
        self.config = config
        self._clock = clock or _now
        # ``enabled=None`` keeps the autonomous feature gate intact while the
        # common coordinator's interactive lane remains available for this
        # provider-free transport.
        self.coordinator = coordinator or AgentWorkCoordinator(self._db_manager, worker_id=worker_id, max_concurrency=128)

    def _now(self) -> datetime:
        value = self._clock()
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value

    async def _session(self) -> Any:
        manager = self._db_manager or get_database_manager()
        if manager is None or not callable(getattr(manager, "get_session", None)):
            raise DialogueTransportUnavailable("database_unavailable")
        return await _maybe(manager.get_session())

    @staticmethod
    async def _close(session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            await _maybe(close())

    @staticmethod
    async def _rollback(session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            try:
                await _maybe(rollback())
            except Exception:
                logger.debug("dialogue transport rollback failed", exc_info=True)

    @staticmethod
    async def _one(session: Any, statement: Any) -> Any | None:
        result = await session.execute(statement)
        scalars = getattr(result, "scalars", None)
        if callable(scalars):
            return scalars().first()
        scalar = getattr(result, "scalar_one_or_none", None)
        return scalar() if callable(scalar) else None

    @staticmethod
    def _projection(row: Any) -> dict[str, Any]:
        method = getattr(row, "to_safe_dict", None) or getattr(row, "to_dict", None)
        value = method() if callable(method) else dict(row) if isinstance(row, Mapping) else {}
        return dict(value or {}) if isinstance(value, Mapping) else {}

    @staticmethod
    def _provenance(dialogue: Any, source_run: Any | None, source_work: Any | None, source_message: Any | None) -> str:
        return sha256_digest(_safe_refs({
            "dialogue_id": _field(dialogue, "id"), "root_session_id": _field(dialogue, "root_session_id"),
            "worker_session_id": _field(dialogue, "worker_session_id"), "root_agent_id": _field(dialogue, "root_agent_id"),
            "root_agent_revision_id": _field(dialogue, "root_agent_revision_id"), "worker_agent_id": _field(dialogue, "worker_agent_id"),
            "worker_agent_revision_id": _field(dialogue, "worker_agent_revision_id"), "source_run_id": _field(source_run, "id"),
            "source_work_item_id": _field(source_work, "id"), "source_message_id": _field(source_message, "id"),
            "control_revision": _field(dialogue, "control_revision"), "owner_user_id": _field(dialogue, "owner_user_id"),
        }))

    @staticmethod
    def _admission_provenance(dialogue: Any, source_run: Any, source_work: Any, source_message: Any) -> str:
        """Hash the active source context proved before durable admission.

        Lease tokens never enter the digest or metadata.  The owner/token
        presence, state and expiry are nevertheless pinned so admission proof
        cannot be substituted with an unleased source Work row.
        """

        payload = {
            "binding_hash": AgentDialogueTransport._provenance(dialogue, source_run, source_work, source_message),
            "source_run_status": _text(_field(source_run, "status")),
            "source_work_state": _text(_field(source_work, "state")),
            "source_work_active_run_id": _id(_field(source_work, "active_agent_run_id")),
            "source_work_lease_owner": _text(_field(source_work, "lease_owner")),
            "source_work_lease_expires_at": str(_field(source_work, "lease_expires_at") or ""),
        }
        return sha256_digest(payload)

    async def _lock_dialogue(self, session: Any, dialogue_id: uuid.UUID, *, nowait: bool = False) -> Any:
        try:
            result = await session.execute(
                select(AgentDialogue)
                .where(AgentDialogue.id == dialogue_id)
                .with_for_update(nowait=nowait)
            )
            dialogue = result.scalars().first()
        except Exception:
            if nowait:
                raise
            dialogue = await session.get(AgentDialogue, dialogue_id)
        if dialogue is None:
            raise DialogueTransportError("dialogue_not_found")
        return dialogue

    async def _validate_dialogue(self, session: Any, dialogue: Any, expected_revision: int) -> None:
        if _text(_field(dialogue, "state")).casefold() in {"cancelled", "expired", "deleted"}:
            raise DialogueTransportError("dialogue_not_active")
        owner_uuid = _uuid(_field(dialogue, "owner_user_id"), "owner_user_id")
        root = await session.get(ConversationSession, _uuid(_field(dialogue, "root_session_id"), "root_session_id"))
        worker = await session.get(ConversationSession, _uuid(_field(dialogue, "worker_session_id"), "worker_session_id"))
        owner = await session.get(User, owner_uuid)
        if root is None or _field(root, "deleted_at") is not None:
            raise DialogueTransportError("root_session_unavailable")
        if worker is None or _field(worker, "deleted_at") is not None or root.id == worker.id:
            raise DialogueTransportError("worker_session_unavailable")
        if owner is None or _field(owner, "is_active", True) is False:
            raise DialogueTransportError("human_owner_unavailable")
        if _text(_field(root, "user_id")) not in {str(owner_uuid), _text(_field(dialogue, "owner_user_id"))}:
            raise DialogueTransportError("human_anchor_owner_mismatch")
        for role, agent_id, revision_id in (("root", dialogue.root_agent_id, dialogue.root_agent_revision_id), ("worker", dialogue.worker_agent_id, dialogue.worker_agent_revision_id)):
            agent = await session.get(Agent, _uuid(agent_id, f"{role}_agent_id"))
            revision = await session.get(AgentRevision, _uuid(revision_id, f"{role}_agent_revision_id"))
            if agent is None or _text(agent.state).casefold() != "active":
                raise DialogueTransportError(f"{role}_agent_inactive")
            if revision is None or revision.agent_id != agent.id:
                raise DialogueTransportError(f"{role}_revision_mismatch")
        if int(dialogue.control_revision or 0) != int(expected_revision):
            raise DialogueTransportConflict("control_revision_conflict")
        if dialogue.deadline is not None and dialogue.deadline <= self._now():
            raise DialogueTransportError("dialogue_deadline_expired")

    async def _validate_cross_dialogue_causation(
        self,
        session: Any,
        dialogue: Any,
        request: DialogueDeliveryRequest,
    ) -> None:
        """Validate a server-derived Root receipt parent for a fresh child.

        A request in a new Dialogue normally starts at depth zero.  Any
        nonzero depth/causation pair must point to the exact response/event
        Delivery that admitted the current Root Run.  This keeps a client or
        model from manufacturing a causal parent by writing metadata alone.
        """

        if request.causation_id in (None, ""):
            if int(request.depth or 0) != 0:
                raise DialogueTransportError("dialogue_causation_required")
            return
        if int(request.depth or 0) <= 0:
            raise DialogueTransportError("dialogue_causal_depth_invalid")
        parent_delivery = await session.get(
            AgentDialogueDelivery,
            _uuid(request.causation_id, "causation_id"),
        )
        if parent_delivery is None:
            raise DialogueTransportError("dialogue_causation_parent_missing")
        parent_dialogue = await session.get(AgentDialogue, parent_delivery.dialogue_id)
        if parent_dialogue is None:
            raise DialogueTransportError("dialogue_causation_parent_missing")
        if (
            _text(parent_delivery.kind, 32).casefold() not in {"response", "event"}
            or _text(parent_delivery.state, 32).casefold() in {"failed", "cancelled", "expired"}
            or not _same_uuid(parent_delivery.receiver_session_id, dialogue.root_session_id)
            or not _same_uuid(parent_delivery.receiver_agent_id, dialogue.root_agent_id)
            or not _same_uuid(parent_delivery.receiver_agent_revision_id, dialogue.root_agent_revision_id)
            or not _same_uuid(parent_delivery.receiver_run_id, request.source_run_id)
            or not _same_uuid(parent_delivery.receiver_work_item_id, request.source_work_item_id)
            or not _same_uuid(parent_dialogue.owner_user_id, dialogue.owner_user_id)
            or not _same_uuid(parent_dialogue.root_session_id, dialogue.root_session_id)
            or not _same_uuid(parent_dialogue.root_agent_id, dialogue.root_agent_id)
            or int(request.depth or 0) != int(getattr(parent_dialogue, "causal_depth", 0) or 0) + 1
            or int(getattr(dialogue, "causal_depth", 0) or 0) != int(request.depth or 0)
        ):
            raise DialogueTransportConflict("dialogue_causation_binding_conflict")
        if int(parent_delivery.expected_control_revision or 0) != int(parent_dialogue.control_revision or 0):
            raise DialogueTransportConflict("dialogue_causation_control_revision_conflict")
        authorization = dialogue.authorization_json if isinstance(dialogue.authorization_json, Mapping) else {}
        chain = authorization.get("causal_chain") if isinstance(authorization.get("causal_chain"), Mapping) else {}
        try:
            chain_depth = None if chain.get("depth") in (None, "") else int(chain.get("depth"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise DialogueTransportConflict("dialogue_causation_metadata_conflict") from exc
        if (
            _text(chain.get("parent_dialogue_id"), 255) not in {"", str(parent_dialogue.id)}
            or _text(chain.get("parent_delivery_id"), 255) not in {"", str(parent_delivery.id)}
            or chain_depth not in (None, int(request.depth or 0))
        ):
            raise DialogueTransportConflict("dialogue_causation_metadata_conflict")

    async def _session_busy(self, session: Any, session_id: uuid.UUID, except_run_id: uuid.UUID | None = None) -> bool:
        conditions: list[Any] = [AgentWorkItem.state.in_(tuple(_ACTIVE_WORK_STATES)), AgentWorkItem.execution_adapter == "interactive_chat"]
        if hasattr(AgentWorkItem, "conversation_session_id"):
            conditions.append(AgentWorkItem.conversation_session_id == session_id)
        else:
            conditions.extend([AgentWorkItem.source_type == "chat_turn", AgentWorkItem.concurrency_key == f"conversation:{session_id}"])
        if await self._one(session, select(AgentWorkItem).where(*conditions).limit(1)) is not None:
            return True
        run_conditions: list[Any] = [AgentRun.session_id == session_id, AgentRun.status.in_(tuple(_ACTIVE_RUN_STATES))]
        if except_run_id is not None:
            run_conditions.append(AgentRun.id != except_run_id)
        return await self._one(session, select(AgentRun.id).where(*run_conditions).limit(1)) is not None

    async def _next_sequence(self, session: Any, dialogue_id: uuid.UUID) -> int:
        # Keep the allocator identical to AgentRunService: one Dialogue row
        # lock plus an explicit flush makes staged rows visible even when the
        # caller uses autoflush=False.  Every request/control/return lane
        # therefore shares one monotonically increasing sequence authority.
        await session.execute(
            select(AgentDialogue.id)
            .where(AgentDialogue.id == dialogue_id)
            .with_for_update()
        )
        await session.flush()
        current = await session.scalar(
            select(func.max(AgentDialogueDelivery.sequence)).where(
                AgentDialogueDelivery.dialogue_id == dialogue_id
            )
        )
        return 0 if current is None else int(current) + 1

    async def _existing(self, session: Any, dialogue_id: uuid.UUID, key: str) -> Any | None:
        return await self._one(session, select(AgentDialogueDelivery).where(AgentDialogueDelivery.dialogue_id == dialogue_id, AgentDialogueDelivery.idempotency_key == key))

    async def _transport_work(self, session: Any, dialogue: Any, delivery: Any, available: datetime | None) -> Any:
        metadata_refs = _safe_refs({
            "dialogue_id": dialogue.id,
            "delivery_id": delivery.id,
            "source_message_id": delivery.source_message_id,
            "receiver_session_id": delivery.receiver_session_id,
            "expected_control_revision": delivery.expected_control_revision,
            "sequence": delivery.sequence,
            "kind": delivery.kind,
            "source_type": SOURCE_TYPE,
            "execution_adapter": EXECUTION_ADAPTER,
            **(_field(dialogue, "authorization_json", {}) if isinstance(_field(dialogue, "authorization_json", {}), Mapping) else {}),
        })
        shared_resource = metadata_refs.get("resource_key") or metadata_refs.get("workspace_resource_key")
        # Transport retries are a separate finite delivery budget.  The
        # dialogue max_attempts/token/cost reservation belongs to the worker
        # Run admission and terminal usage charge; reserving it here would
        # double-charge every handoff.
        candidate = {
            "source_type": SOURCE_TYPE, "source_id": str(delivery.id), "source_revision": delivery.request_digest,
            "intent_key": f"dialogue_handoff:{_text(delivery.idempotency_key, 180)}", "domain": "conversation",
            "conversation_session_id": str(delivery.receiver_session_id), "execution_adapter": EXECUTION_ADAPTER,
            "assigned_agent_id": None, "agent_revision_id": None, "priority": 100, "not_before": available,
            "deadline": delivery.deadline, "max_attempts": 5, "concurrency_key": shared_resource or f"conversation:{delivery.receiver_session_id}",
            "causation_id": delivery.causation_id, "causal_depth": int(delivery.depth or 0),
            "metadata": metadata_refs,
        }
        result = await self.coordinator.admit_transport(candidate, session=session)
        if not result:
            raise DialogueTransportError("transport_work_admission_denied")
        work = await session.get(AgentWorkItem, _uuid(result["id"], "transport_work_item_id"))
        if work is None:
            raise DialogueTransportError("transport_work_missing")
        delivery.transport_work_item_id = work.id
        return work

    async def cancel_interactive_source_in_session(
        self,
        session: Any,
        *,
        run: Any,
        work: Any,
        reason: str = "cancelled",
        validate_only: bool = False,
    ) -> int:
        """Cancel unconsumed Delivery/transport rows for one exact source pair.

        This is intentionally a same-transaction helper for the admin Work
        cancel path.  It accepts only a terminal failed/cancelled interactive
        source Run and a blocked Work row.  A leased delivery, a receiver
        binding, or any foreign/stale transport Work makes the whole operation
        fail before the first row is mutated.  ``validate_only`` performs the
        same locked preflight without changing any Delivery/transport row so
        an owner can run its provider cancellation hook only after the DB
        contract is proven.
        """

        run_uuid = _uuid(_field(run, "id"), "source_run_id")
        work_uuid = _uuid(_field(work, "id"), "source_work_item_id")
        if (
            run_uuid is None
            or work_uuid is None
            or _text(_field(run, "status"), 32).casefold()
            not in {"failed", "cancelled", "queued", "running", "awaiting_approval", "uncertain"}
            or (
                _text(_field(run, "status"), 32).casefold()
                in {"failed", "cancelled"}
                and _text(_field(work, "state"), 32).casefold() != "blocked"
            )
            or (
                _text(_field(run, "status"), 32).casefold()
                in {"queued", "running", "awaiting_approval", "uncertain"}
                and _text(_field(work, "state"), 32).casefold()
                not in {"pending", "claimed", "running", "awaiting_approval", "uncertain"}
            )
            or not _same_uuid(_field(run, "work_item_id"), work_uuid)
            or _text(_field(work, "source_type"), 64).casefold() != "chat_turn"
            or _text(_field(work, "execution_adapter"), 64).casefold() != "interactive_chat"
            or (
                _field(work, "active_agent_run_id") not in (None, "")
                and not _same_uuid(_field(work, "active_agent_run_id"), run_uuid)
            )
        ):
            raise DialogueTransportConflict("interactive_cancel_source_binding_invalid")

        rows = (
            await session.execute(
                select(AgentDialogueDelivery)
                .where(
                    AgentDialogueDelivery.source_work_item_id == work_uuid,
                    AgentDialogueDelivery.kind.in_(("response", "event")),
                )
                .order_by(AgentDialogueDelivery.sequence, AgentDialogueDelivery.id)
                .with_for_update(nowait=True)
            )
        ).scalars().all()
        plans: list[tuple[Any, Any, Mapping[str, Any], Mapping[str, Any]]] = []
        for delivery in rows:
            state = _text(delivery.state, 32).casefold()
            if state in {"cancelled", "failed", "expired", "acked"}:
                # Terminal history is immutable.  A cancelled child is
                # already idempotently settled; all other terminal states are
                # consumed history and are left untouched.
                transport_id = _uuid(
                    delivery.transport_work_item_id,
                    "transport_work_item_id",
                    required=False,
                )
                if transport_id is not None:
                    transport_result = await session.execute(
                        select(AgentWorkItem)
                        .where(AgentWorkItem.id == transport_id)
                        .with_for_update(nowait=True)
                    )
                    terminal_transport = transport_result.scalars().first()
                    if terminal_transport is not None and _text(
                        terminal_transport.state, 32
                    ).casefold() in {
                        "pending",
                        "claimed",
                        "running",
                        "awaiting_approval",
                        "uncertain",
                    }:
                        raise DialogueTransportConflict(
                            "interactive_cancel_terminal_delivery_transport_live"
                        )
                continue
            if state not in {"pending", "available"}:
                raise DialogueTransportConflict("interactive_cancel_delivery_live")
            if not _same_uuid(delivery.source_run_id, run_uuid):
                raise DialogueTransportConflict("interactive_cancel_source_run_mismatch")
            if (
                delivery.receiver_run_id is not None
                or delivery.receiver_work_item_id is not None
                or delivery.receiver_message_id is not None
            ):
                raise DialogueTransportConflict("interactive_cancel_delivery_receiver_bound")
            dialogue = await self._lock_dialogue(
                session,
                _uuid(delivery.dialogue_id, "dialogue_id"),
                nowait=True,
            )
            expected_source_session = dialogue.worker_session_id
            expected_source_agent = dialogue.worker_agent_id
            expected_source_revision = dialogue.worker_agent_revision_id
            expected_receiver_session = dialogue.root_session_id
            expected_receiver_agent = dialogue.root_agent_id
            expected_receiver_revision = dialogue.root_agent_revision_id
            if (
                not _same_uuid(delivery.source_session_id, expected_source_session)
                or not _same_uuid(delivery.source_agent_id, expected_source_agent)
                or not _same_uuid(delivery.source_agent_revision_id, expected_source_revision)
                or not _same_uuid(delivery.receiver_session_id, expected_receiver_session)
                or not _same_uuid(delivery.receiver_agent_id, expected_receiver_agent)
                or not _same_uuid(delivery.receiver_agent_revision_id, expected_receiver_revision)
            ):
                raise DialogueTransportConflict("interactive_cancel_delivery_binding_invalid")
            if (
                int(delivery.expected_control_revision or 0)
                != int(dialogue.control_revision or 0)
            ):
                raise DialogueTransportConflict("interactive_cancel_control_revision_changed")
            # Reuse the normal immutable source validator.  It proves the
            # dialogue owner/session/agent/revision/message/run/work tuple and
            # retains the failed+blocked input-pause exception.
            await self._validate_delivery_source(session, dialogue, delivery)
            transport_id = _uuid(delivery.transport_work_item_id, "transport_work_item_id")
            if transport_id is None:
                raise DialogueTransportConflict("interactive_cancel_transport_missing")
            transport_result = await session.execute(
                select(AgentWorkItem)
                .where(AgentWorkItem.id == transport_id)
                .with_for_update(nowait=True)
            )
            transport_work = transport_result.scalars().first()
            if transport_work is None:
                raise DialogueTransportConflict("interactive_cancel_transport_missing")
            transport_state = _text(transport_work.state, 32).casefold()
            metadata = (
                transport_work.metadata_json
                if isinstance(transport_work.metadata_json, Mapping)
                else {}
            )
            if (
                _text(transport_work.source_type, 64).casefold() != SOURCE_TYPE
                or _text(transport_work.execution_adapter, 64).casefold() != EXECUTION_ADAPTER
                or _text(transport_work.source_id, 255) != str(delivery.id)
                or _text(transport_work.source_revision, 128) != _text(delivery.request_digest, 128)
                or _text(transport_work.intent_key, 255)
                != f"dialogue_handoff:{_text(delivery.idempotency_key, 180)}"
                or not _same_uuid(transport_work.conversation_session_id, delivery.receiver_session_id)
                or transport_work.assigned_agent_id is not None
                or transport_work.agent_revision_id is not None
                or transport_work.active_agent_run_id is not None
                or transport_work.lease_owner is not None
                or transport_work.lease_token is not None
                or transport_work.lease_expires_at is not None
                or transport_state != "pending"
                or not self._transport_metadata_matches_delivery(
                    metadata,
                    dialogue=dialogue,
                    delivery=delivery,
                )
                or (
                    metadata.get("dialogue_id") not in (None, "")
                    and not _same_uuid(metadata.get("dialogue_id"), dialogue.id)
                )
                or (
                    metadata.get("delivery_id") not in (None, "")
                    and not _same_uuid(metadata.get("delivery_id"), delivery.id)
                )
            ):
                raise DialogueTransportConflict("interactive_cancel_transport_binding_invalid")
            plans.append(
                (
                    delivery,
                    transport_work,
                    {
                        "state": delivery.state,
                        "updated_at": delivery.updated_at,
                        "receiver_run_id": delivery.receiver_run_id,
                        "receiver_work_item_id": delivery.receiver_work_item_id,
                        "receiver_message_id": delivery.receiver_message_id,
                    },
                    {
                        "state": transport_work.state,
                        "updated_at": transport_work.updated_at,
                        "active_agent_run_id": transport_work.active_agent_run_id,
                        "lease_owner": transport_work.lease_owner,
                        "lease_token": transport_work.lease_token,
                        "lease_expires_at": transport_work.lease_expires_at,
                    },
                )
            )

        if not plans:
            return 0
        if validate_only:
            return len(plans)
        now = self._now()
        safe_reason = _text(reason, 500) or "cancelled"
        for delivery, transport_work, delivery_snapshot, transport_snapshot in plans:
            previous_state = _text(transport_work.state, 32) or "pending"
            delivery_conditions = [
                AgentDialogueDelivery.id == delivery.id,
                AgentDialogueDelivery.state == delivery_snapshot["state"],
                AgentDialogueDelivery.updated_at == delivery_snapshot["updated_at"],
            ]
            for field in ("receiver_run_id", "receiver_work_item_id", "receiver_message_id"):
                value = delivery_snapshot[field]
                column = getattr(AgentDialogueDelivery, field)
                delivery_conditions.append(column.is_(None) if value is None else column == value)
            transport_conditions = [
                AgentWorkItem.id == transport_work.id,
                AgentWorkItem.state == transport_snapshot["state"],
                AgentWorkItem.updated_at == transport_snapshot["updated_at"],
            ]
            for field in ("active_agent_run_id", "lease_owner", "lease_token", "lease_expires_at"):
                value = transport_snapshot[field]
                column = getattr(AgentWorkItem, field)
                transport_conditions.append(column.is_(None) if value is None else column == value)
            with session.no_autoflush:
                delivery_cas = await session.execute(
                    update(AgentDialogueDelivery)
                    .where(*delivery_conditions)
                    .values(state="cancelled", available_at=None, updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(delivery_cas, "rowcount", 0) or 0) != 1:
                    raise DialogueTransportConflict("interactive_cancel_delivery_cas_lost")
                transport_values: dict[str, Any] = {
                    "state": "cancelled",
                    "outcome_classification": "cancelled",
                    "safe_error_code": "cancelled",
                    "escalation_reason": safe_reason,
                    "result_summary": safe_reason,
                    "not_before": None,
                    "next_attempt_at": None,
                    "completed_at": now,
                    "cancelled_at": now,
                    "updated_at": now,
                    "active_agent_run_id": None,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                }
                if isinstance(transport_work.budget_reservation_json, Mapping) and transport_work.budget_reservation_json:
                    transport_values["budget_reservation_json"] = self.coordinator._settle_budget(
                        dict(transport_work.budget_reservation_json),
                        ExecutionOutcome(classification="cancelled"),
                        state="cancelled",
                    )
                transport_cas = await session.execute(
                    update(AgentWorkItem)
                    .where(*transport_conditions)
                    .values(**transport_values)
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(transport_cas, "rowcount", 0) or 0) != 1:
                    raise DialogueTransportConflict("interactive_cancel_transport_cas_lost")
            delivery.state = "cancelled"
            delivery.available_at = None
            delivery.updated_at = now
            transport_work.state = "cancelled"
            transport_work.outcome_classification = "cancelled"
            transport_work.safe_error_code = "cancelled"
            transport_work.escalation_reason = safe_reason
            transport_work.result_summary = safe_reason
            transport_work.not_before = None
            transport_work.next_attempt_at = None
            transport_work.completed_at = now
            transport_work.cancelled_at = now
            transport_work.updated_at = now
            transport_work.active_agent_run_id = None
            transport_work.lease_owner = None
            transport_work.lease_token = None
            transport_work.lease_expires_at = None
            if "budget_reservation_json" in transport_values:
                transport_work.budget_reservation_json = transport_values["budget_reservation_json"]
            await self.coordinator._append_event_in_session(
                session,
                transport_work,
                event_type="work.cancelled",
                actor_kind="service",
                actor_id=self.coordinator.worker_id,
                agent_run_id=None,
                from_state=previous_state,
                to_state="cancelled",
                causation_id=transport_work.causation_id,
                causal_depth=int(transport_work.causal_depth or 0),
                payload={
                    "reason": safe_reason,
                    "source": "interactive_work_cancel",
                    "delivery_id": str(delivery.id),
                    "source_run_id": str(run_uuid),
                    "source_work_item_id": str(work_uuid),
                },
            )
        return len(plans)

    @staticmethod
    def _transport_metadata_matches_delivery(
        metadata: Mapping[str, Any],
        *,
        dialogue: Any,
        delivery: Any,
    ) -> bool:
        expected = {
            "expected_control_revision": int(delivery.expected_control_revision or 0),
            "sequence": int(delivery.sequence or 0),
            "kind": _text(delivery.kind, 32),
            "source_type": SOURCE_TYPE,
            "execution_adapter": EXECUTION_ADAPTER,
        }
        for key, value in expected.items():
            observed = metadata.get(key)
            if isinstance(value, int):
                try:
                    if int(observed) != value:
                        return False
                except (TypeError, ValueError):
                    return False
            elif str(observed or "") != str(value):
                return False
        # Older transport Work rows predate UUID references in the bounded
        # metadata projection.  Their source_id/source_revision and the
        # locked Delivery/Dialogue tuple remain the canonical binding.  When
        # these optional UUID references exist, they must still match exactly.
        for key, value in {
            "dialogue_id": dialogue.id,
            "delivery_id": delivery.id,
            "source_message_id": delivery.source_message_id,
            "receiver_session_id": delivery.receiver_session_id,
        }.items():
            observed = metadata.get(key)
            if observed not in (None, "") and str(observed) != str(value):
                return False
        return True

    async def enqueue_delivery(self, request: DialogueDeliveryRequest | Mapping[str, Any] | None = None, *, session: Any | None = None, content: str | None = None, defer_if_busy: bool = True, **values: Any) -> dict[str, Any]:
        raw = dict(request.__dict__ if isinstance(request, DialogueDeliveryRequest) else request or {})
        raw.update({key: value for key, value in values.items() if value is not None})
        if not raw.get("request_digest"):
            if content is None:
                raise DialogueTransportError("request_digest_required")
            raw["request_digest"] = sha256_digest(content)
        normalized = DialogueDeliveryRequest(**{key: raw.get(key) for key in DialogueDeliveryRequest.__dataclass_fields__}).normalized()
        owns = session is None
        db_session = session or await self._session()
        dialogue_uuid = _uuid(normalized.dialogue_id, "dialogue_id")
        try:
            dialogue = await self._lock_dialogue(db_session, dialogue_uuid)
            await self._validate_dialogue(db_session, dialogue, normalized.expected_control_revision)
            source_session_uuid = _uuid(normalized.source_session_id, "source_session_id")
            receiver_session_uuid = _uuid(normalized.receiver_session_id, "receiver_session_id")
            # Worker returns use the dedicated typed lane below.  Keep the
            # ordinary request/source validation root-only; response/event
            # deliveries must be exactly worker-session to root-session and
            # cannot fall through into the root request branch.
            if normalized.kind in {"response", "event"}:
                if source_session_uuid != dialogue.worker_session_id or receiver_session_uuid != dialogue.root_session_id:
                    raise DialogueTransportError("worker_return_direction_invalid")
                if (
                    _uuid(normalized.source_agent_id, "source_agent_id") != dialogue.worker_agent_id
                    or _uuid(normalized.source_agent_revision_id, "source_agent_revision_id") != dialogue.worker_agent_revision_id
                    or _uuid(normalized.receiver_agent_id, "receiver_agent_id") != dialogue.root_agent_id
                    or _uuid(normalized.receiver_agent_revision_id, "receiver_agent_revision_id") != dialogue.root_agent_revision_id
                ):
                    raise DialogueTransportError("worker_return_agent_binding_mismatch")
                worker_result = await self.enqueue_worker_return(
                    dialogue_id=str(dialogue.id),
                    source_run_id=str(_uuid(normalized.source_run_id, "source_run_id")),
                    source_work_item_id=_id(normalized.source_work_item_id),
                    source_message_id=str(_uuid(normalized.source_message_id, "source_message_id")),
                    kind=normalized.kind,
                    expected_control_revision=normalized.expected_control_revision,
                    idempotency_key=normalized.idempotency_key,
                    request_digest=normalized.request_digest,
                    sequence=normalized.sequence,
                    causation_id=normalized.causation_id,
                    depth=normalized.depth if normalized.depth else None,
                    available_at=normalized.available_at,
                    deadline=normalized.deadline,
                    session=db_session,
                )
                if owns:
                    await db_session.commit()
                return worker_result
            if normalized.kind == "request":
                if source_session_uuid != dialogue.root_session_id or receiver_session_uuid != dialogue.worker_session_id:
                    raise DialogueTransportError("delivery_session_mismatch")
                if _uuid(normalized.source_agent_id, "source_agent_id") != dialogue.root_agent_id or _uuid(normalized.source_agent_revision_id, "source_agent_revision_id") != dialogue.root_agent_revision_id:
                    raise DialogueTransportError("source_agent_binding_mismatch")
                if _uuid(normalized.receiver_agent_id, "receiver_agent_id") != dialogue.worker_agent_id or _uuid(normalized.receiver_agent_revision_id, "receiver_agent_revision_id") != dialogue.worker_agent_revision_id:
                    raise DialogueTransportError("receiver_agent_binding_mismatch")
                await self._validate_cross_dialogue_causation(db_session, dialogue, normalized)
            elif normalized.kind == "control":
                if source_session_uuid != dialogue.root_session_id or receiver_session_uuid != dialogue.worker_session_id:
                    raise DialogueTransportError("delivery_direction_invalid")
                if _uuid(normalized.source_agent_id, "source_agent_id") != dialogue.root_agent_id or _uuid(normalized.source_agent_revision_id, "source_agent_revision_id") != dialogue.root_agent_revision_id:
                    raise DialogueTransportError("source_agent_binding_mismatch")
                if _uuid(normalized.receiver_agent_id, "receiver_agent_id") != dialogue.worker_agent_id or _uuid(normalized.receiver_agent_revision_id, "receiver_agent_revision_id") != dialogue.worker_agent_revision_id:
                    raise DialogueTransportError("receiver_agent_binding_mismatch")
            existing = await self._existing(db_session, dialogue_uuid, normalized.idempotency_key)
            if existing is not None:
                if existing.request_digest != normalized.request_digest:
                    raise DialogueTransportConflict("idempotency_digest_conflict")
                if int(existing.expected_control_revision or 0) != normalized.expected_control_revision or existing.kind != normalized.kind:
                    raise DialogueTransportConflict("idempotency_binding_conflict")
                if owns:
                    await db_session.commit()
                return {**self._projection(existing), "idempotent": True}
            sequence = normalized.sequence if normalized.sequence is not None else await self._next_sequence(db_session, dialogue_uuid)
            same = await self._one(db_session, select(AgentDialogueDelivery).where(AgentDialogueDelivery.dialogue_id == dialogue_uuid, AgentDialogueDelivery.sequence == sequence))
            if same is not None:
                if same.idempotency_key != normalized.idempotency_key or same.request_digest != normalized.request_digest:
                    raise DialogueTransportConflict("delivery_sequence_conflict")
                if owns:
                    await db_session.commit()
                return {**self._projection(same), "idempotent": True}
            source_message_id = _uuid(normalized.source_message_id, "source_message_id", required=False)
            source_message = await db_session.get(ConversationMessage, source_message_id) if source_message_id else None
            if source_message_id and (source_message is None or source_message.session_id != dialogue.root_session_id):
                raise DialogueTransportError("source_message_mismatch")
            if normalized.kind == "request" and source_message is not None:
                if _text(source_message.role).casefold() != "assistant" or _text(source_message.sender_type).casefold() != "agent" or source_message.sender_id != str(dialogue.root_agent_id):
                    raise DialogueTransportError("source_message_agent_binding_mismatch")
            source_run_id = _uuid(normalized.source_run_id, "source_run_id", required=False)
            source_run = await db_session.get(AgentRun, source_run_id) if source_run_id else None
            if source_run_id and (source_run is None or source_run.session_id != dialogue.root_session_id):
                raise DialogueTransportError("source_run_mismatch")
            if source_run is not None and source_run.parent_run_id is not None:
                raise DialogueTransportError("cross_agent_parent_run_forbidden")
            if source_run is not None:
                if _text(source_run.status).casefold() not in {"queued", "running", "awaiting_approval"}:
                    raise DialogueTransportError("source_run_admission_not_active")
                if source_run.user_id not in (None, str(dialogue.owner_user_id)):
                    raise DialogueTransportError("source_run_human_owner_mismatch")
                if source_run.agent_id not in (None, dialogue.root_agent_id) or source_run.agent_revision_id not in (None, dialogue.root_agent_revision_id):
                    raise DialogueTransportError("source_run_agent_binding_mismatch")
            source_work_id = _uuid(normalized.source_work_item_id, "source_work_item_id", required=False)
            if source_work_id is None and source_run is not None:
                source_work_id = _uuid(source_run.work_item_id, "source_work_item_id", required=False)
            source_work = await db_session.get(AgentWorkItem, source_work_id) if source_work_id else None
            if source_run is not None and source_work is None:
                raise DialogueTransportError("source_work_admission_required")
            if source_work is not None and source_work.conversation_session_id not in (None, dialogue.root_session_id):
                raise DialogueTransportError("source_work_session_mismatch")
            if source_work is not None and source_work.assigned_agent_id not in (None, dialogue.root_agent_id):
                raise DialogueTransportError("source_work_agent_binding_mismatch")
            if source_work is not None and source_work.agent_revision_id not in (None, dialogue.root_agent_revision_id):
                raise DialogueTransportError("source_work_revision_binding_mismatch")
            if source_work is not None and source_work.active_agent_run_id not in (None, source_run_id):
                raise DialogueTransportError("source_work_run_mismatch")
            if source_work is not None:
                if _text(source_work.state).casefold() not in {"claimed", "running"} or source_work.active_agent_run_id != source_run_id:
                    raise DialogueTransportError("source_work_admission_not_active")
                if not source_work.lease_owner or not source_work.lease_token or source_work.lease_expires_at is None or source_work.lease_expires_at <= self._now():
                    raise DialogueTransportError("source_work_lease_invalid")
            available = normalized.available_at or self._now()
            if defer_if_busy and normalized.kind == "request":
                if await self._session_busy(db_session, dialogue.root_session_id, source_run_id):
                    available = max(available, self._now() + timedelta(seconds=1))
                if await self._session_busy(db_session, dialogue.worker_session_id):
                    available = max(available, self._now() + timedelta(seconds=1))
            delivery = AgentDialogueDelivery(
                id=uuid.uuid4(), dialogue_id=dialogue_uuid, source_session_id=_uuid(normalized.source_session_id, "source_session_id"),
                source_agent_id=_uuid(normalized.source_agent_id, "source_agent_id", required=False), source_agent_revision_id=_uuid(normalized.source_agent_revision_id, "source_agent_revision_id", required=False),
                source_run_id=source_run_id, source_work_item_id=source_work_id, source_message_id=source_message_id,
                receiver_session_id=_uuid(normalized.receiver_session_id, "receiver_session_id"), receiver_agent_id=_uuid(normalized.receiver_agent_id, "receiver_agent_id", required=False), receiver_agent_revision_id=_uuid(normalized.receiver_agent_revision_id, "receiver_agent_revision_id", required=False),
                kind=normalized.kind, state="pending" if available > self._now() else "available", sequence=sequence, causation_id=normalized.causation_id,
                depth=normalized.depth, expected_control_revision=normalized.expected_control_revision, idempotency_key=normalized.idempotency_key, request_digest=normalized.request_digest,
                available_at=available, deadline=normalized.deadline or dialogue.deadline, attempt_count=0, created_at=self._now(), updated_at=self._now(),
            )
            db_session.add(delivery)
            await db_session.flush()
            work = await self._transport_work(db_session, dialogue, delivery, available)
            admission_provenance = normalized.metadata.get("provenance_hash") if isinstance(normalized.metadata, Mapping) else None
            if admission_provenance is not None and not _SHA256.fullmatch(str(admission_provenance).casefold()):
                raise DialogueTransportError("provenance_hash_invalid")
            provenance_hash = str(admission_provenance).casefold() if admission_provenance else self._provenance(dialogue, source_run, source_work, source_message)
            binding_hash = self._provenance(dialogue, source_run, source_work, source_message)
            admission_hash = self._admission_provenance(dialogue, source_run, source_work, source_message)
            work.metadata_json = {**dict(work.metadata_json or {}), "provenance_hash": admission_hash, "binding_hash": binding_hash, **_safe_refs(normalized.metadata)}
            if owns:
                await db_session.commit()
            return {**self._projection(delivery), "transport_work_item_id": str(work.id), "provenance_hash": admission_hash, "binding_hash": binding_hash, "idempotent": False}
        except IntegrityError as exc:
            if owns:
                await self._rollback(db_session)
            raise DialogueTransportConflict("delivery_duplicate") from exc
        except Exception:
            if owns:
                await self._rollback(db_session)
            raise
        finally:
            if owns:
                await self._close(db_session)

    enqueue = enqueue_delivery
    admit_delivery = enqueue_delivery

    async def enqueue_worker_return(
        self,
        *,
        dialogue_id: str,
        source_run_id: str,
        source_work_item_id: str | None = None,
        source_message_id: str,
        kind: str = "event",
        expected_control_revision: int,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        sequence: int | None = None,
        causation_id: str | None = None,
        depth: int | None = None,
        available_at: datetime | None = None,
        deadline: datetime | None = None,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Create a typed worker-to-root return without widening requests.

        Worker progress and explicit worker responses use a separate, narrow
        admission lane.  The ordinary request path remains root-only.  The
        source Run/Work/message are re-read from the worker session and must
        still be the current leased attempt; this method never changes their
        lifecycle or invokes a provider.
        """

        dialogue_uuid = _uuid(dialogue_id, "dialogue_id")
        source_run_uuid = _uuid(source_run_id, "source_run_id")
        source_message_uuid = _uuid(source_message_id, "source_message_id")
        requested_work_uuid = _uuid(source_work_item_id, "source_work_item_id", required=False)
        return_kind = _text(kind, 32).casefold() or "event"
        if return_kind not in {"event", "response"}:
            raise DialogueTransportError("worker_return_kind_invalid")
        expected_revision = int(expected_control_revision or 0)
        if expected_revision < 0:
            raise DialogueTransportError("control_revision_invalid")
        requested_depth = None if depth is None else int(depth)
        if requested_depth is not None and (requested_depth < 0 or requested_depth > MAX_DEPTH):
            raise DialogueTransportError("dialogue_depth_exceeded")
        owns = session is None
        db_session = session or await self._session()
        try:
            dialogue = await self._lock_dialogue(db_session, dialogue_uuid)
            await self._validate_dialogue(db_session, dialogue, expected_revision)

            worker_session_id = _uuid(dialogue.worker_session_id, "worker_session_id")
            root_session_id = _uuid(dialogue.root_session_id, "root_session_id")
            worker_agent_id = _uuid(dialogue.worker_agent_id, "worker_agent_id")
            worker_revision_id = _uuid(dialogue.worker_agent_revision_id, "worker_agent_revision_id")
            root_agent_id = _uuid(dialogue.root_agent_id, "root_agent_id")
            root_revision_id = _uuid(dialogue.root_agent_revision_id, "root_agent_revision_id")
            worker_session = await db_session.get(ConversationSession, worker_session_id)
            root_session = await db_session.get(ConversationSession, root_session_id)
            if (
                worker_session is None
                or root_session is None
                or _text(worker_session.user_id) != str(dialogue.owner_user_id)
                or _text(root_session.user_id) != str(dialogue.owner_user_id)
            ):
                raise DialogueTransportError("worker_return_human_owner_mismatch")

            source_run = await db_session.get(AgentRun, source_run_uuid)
            if source_run is None:
                raise DialogueTransportError("worker_source_run_not_found")
            if (
                _text(source_run.run_type).casefold() != "chat_turn"
                or _text(source_run.status).casefold() not in _ACTIVE_RUN_STATES | _TERMINAL_RUN_STATES
                or source_run.parent_run_id is not None
                or not _same_uuid(source_run.session_id, worker_session_id)
                or not _same_uuid(source_run.agent_id, worker_agent_id)
                or not _same_uuid(source_run.agent_revision_id, worker_revision_id)
                or _text(source_run.user_id) != str(dialogue.owner_user_id)
            ):
                raise DialogueTransportError("worker_source_run_invalid")
            source_work_uuid = requested_work_uuid or _uuid(source_run.work_item_id, "source_work_item_id", required=False)
            source_work = await db_session.get(AgentWorkItem, source_work_uuid) if source_work_uuid else None
            now = self._now()
            if (
                source_work is None
                or not _same_uuid(source_work.id, source_run.work_item_id)
                or not _same_uuid(source_work.conversation_session_id, worker_session_id)
                or not _same_uuid(source_work.assigned_agent_id, worker_agent_id)
                or not _same_uuid(source_work.agent_revision_id, worker_revision_id)
                or _text(source_work.execution_adapter).casefold() != "interactive_chat"
                or (
                    source_work.active_agent_run_id not in (None, "")
                    and not _same_uuid(source_work.active_agent_run_id, source_run.id)
                )
            ):
                raise DialogueTransportError("worker_source_work_invalid")

            source_message = await db_session.get(ConversationMessage, source_message_uuid)
            message_metadata = source_message.message_metadata if source_message is not None and isinstance(source_message.message_metadata, Mapping) else {}
            if (
                source_message is None
                or not _same_uuid(source_message.session_id, worker_session_id)
                or _text(source_message.role).casefold() != "assistant"
                or _text(source_message.sender_type).casefold() != "agent"
                or _text(source_message.sender_id) != str(worker_agent_id)
                or _text(message_metadata.get("agent_dialogue_id")) != str(dialogue.id)
                or _text(message_metadata.get("agent_run_id")) != str(source_run.id)
                or source_message.deleted_at is not None
            ):
                raise DialogueTransportError("worker_source_message_invalid")

            message_body = getattr(source_message, "content", None) or getattr(source_message, "_content", None) or ""
            message_digest = sha256_digest(message_body)
            digest = _text(request_digest, 64).casefold() if request_digest else message_digest
            if not _SHA256.fullmatch(digest) or digest != message_digest:
                raise DialogueTransportConflict("worker_return_digest_conflict")
            canonical_key = f"worker-return:{dialogue.id}:{source_message.id}:{return_kind}"
            if idempotency_key is not None and not _text(idempotency_key, 255):
                raise DialogueTransportError("idempotency_key_required")
            # Message identity plus typed kind is the canonical key.  The
            # service may provide a stable caller key, but terminal receipts
            # must converge with a direct response even when those call sites
            # chose different textual prefixes.
            return_key = canonical_key

            # A retry after the worker Run/Work has settled must converge to
            # the already admitted return.  Check the immutable source
            # identity and digest first, then allow this idempotent path to
            # bypass the live lease requirement below.
            existing = await self._existing(db_session, dialogue_uuid, return_key)
            if existing is not None:
                if (
                    existing.request_digest != digest
                    or existing.kind != return_kind
                    or int(existing.expected_control_revision or 0) != expected_revision
                    or not _same_uuid(existing.source_message_id, source_message_uuid)
                    or not _same_uuid(existing.source_run_id, source_run_uuid)
                    or not _same_uuid(existing.source_work_item_id, source_work.id)
                    or not _same_uuid(existing.source_session_id, worker_session_id)
                    or not _same_uuid(existing.source_agent_id, worker_agent_id)
                    or not _same_uuid(existing.source_agent_revision_id, worker_revision_id)
                    or not _same_uuid(existing.receiver_session_id, root_session_id)
                    or not _same_uuid(existing.receiver_agent_id, root_agent_id)
                    or not _same_uuid(existing.receiver_agent_revision_id, root_revision_id)
                ):
                    raise DialogueTransportConflict("worker_return_idempotency_conflict")
                if requested_depth is not None and int(existing.depth or 0) != requested_depth:
                    raise DialogueTransportConflict("worker_return_depth_conflict")
                if owns:
                    await db_session.commit()
                return {**self._projection(existing), "idempotent": True}

            if _text(source_run.status).casefold() not in _ACTIVE_RUN_STATES:
                raise DialogueTransportError("worker_source_run_terminal")
            if (
                _text(source_work.state).casefold() not in {"claimed", "running"}
                or not source_work.lease_owner
                or not source_work.lease_token
                or source_work.lease_expires_at is None
                or source_work.lease_expires_at <= now
            ):
                raise DialogueTransportError("worker_source_work_lease_invalid")
            source_delivery = await self._one(
                db_session,
                select(AgentDialogueDelivery)
                .where(
                    AgentDialogueDelivery.dialogue_id == dialogue_uuid,
                    AgentDialogueDelivery.receiver_run_id == source_run_uuid,
                    AgentDialogueDelivery.receiver_work_item_id == source_work.id,
                    AgentDialogueDelivery.receiver_session_id == worker_session_id,
                    AgentDialogueDelivery.receiver_agent_id == worker_agent_id,
                    AgentDialogueDelivery.receiver_agent_revision_id == worker_revision_id,
                    AgentDialogueDelivery.state == "delivered",
                )
                .order_by(desc(AgentDialogueDelivery.sequence))
                .limit(1),
            )
            if source_delivery is None:
                raise DialogueTransportError("worker_source_delivery_invalid")
            if int(source_delivery.expected_control_revision or 0) != expected_revision:
                raise DialogueTransportConflict("worker_source_delivery_control_revision")
            return_depth = min(MAX_DEPTH, int(source_delivery.depth or 0) + 1)
            if requested_depth is not None and requested_depth != return_depth:
                raise DialogueTransportConflict("worker_return_depth_conflict")
            if return_depth > int(dialogue.max_depth or MAX_DEPTH):
                raise DialogueTransportError("dialogue_depth_exceeded")
            causation = str(source_delivery.id)
            if causation_id not in (None, "") and _text(causation_id, 255) != causation:
                raise DialogueTransportConflict("worker_return_causation_conflict")
            available = _datetime(available_at) or now
            delivery_deadline = _datetime(deadline) or dialogue.deadline
            if delivery_deadline is not None and delivery_deadline <= now:
                raise DialogueTransportError("delivery_deadline_expired")
            if delivery_deadline is not None and delivery_deadline < available:
                raise DialogueTransportError("deadline_before_availability")
            if await self._session_busy(db_session, root_session_id):
                available = max(available, now + timedelta(seconds=1))
                if delivery_deadline is not None and available > delivery_deadline:
                    raise DialogueTransportError("delivery_deadline_expired")

            delivery_sequence = int(sequence) if sequence is not None else await self._next_sequence(db_session, dialogue_uuid)
            if delivery_sequence < 0:
                raise DialogueTransportError("delivery_sequence_invalid")
            same = await self._one(
                db_session,
                select(AgentDialogueDelivery).where(
                    AgentDialogueDelivery.dialogue_id == dialogue_uuid,
                    AgentDialogueDelivery.sequence == delivery_sequence,
                ),
            )
            if same is not None:
                if same.idempotency_key != return_key or same.request_digest != digest:
                    raise DialogueTransportConflict("delivery_sequence_conflict")
                if owns:
                    await db_session.commit()
                return {**self._projection(same), "idempotent": True}
            delivery = AgentDialogueDelivery(
                id=uuid.uuid4(),
                dialogue_id=dialogue_uuid,
                source_session_id=worker_session_id,
                source_agent_id=worker_agent_id,
                source_agent_revision_id=worker_revision_id,
                source_run_id=source_run_uuid,
                source_work_item_id=source_work.id,
                source_message_id=source_message_uuid,
                receiver_session_id=root_session_id,
                receiver_agent_id=root_agent_id,
                receiver_agent_revision_id=root_revision_id,
                kind=return_kind,
                state="pending" if available > now else "available",
                sequence=delivery_sequence,
                causation_id=causation,
                depth=return_depth,
                expected_control_revision=expected_revision,
                idempotency_key=return_key,
                request_digest=digest,
                available_at=available,
                deadline=delivery_deadline,
                attempt_count=0,
                created_at=now,
                updated_at=now,
            )
            db_session.add(delivery)
            await db_session.flush()
            work = await self._transport_work(db_session, dialogue, delivery, available)
            delivery.transport_work_item_id = work.id
            if owns:
                await db_session.commit()
            return {**self._projection(delivery), "transport_work_item_id": str(work.id), "idempotent": False}
        except IntegrityError as exc:
            if owns:
                await self._rollback(db_session)
            raise DialogueTransportConflict("worker_return_duplicate") from exc
        except Exception:
            if owns:
                await self._rollback(db_session)
            raise
        finally:
            if owns:
                await self._close(db_session)

    async def claim_transport(self, limit: int = 1, *, work_item_id: str | None = None, source_type: str | None = None, execution_adapter: str | None = None) -> list[WorkClaim]:
        """Claim only the narrow dialogue Work lane through common runtime."""

        return await self.coordinator.claim_transport(limit=limit, work_item_id=work_item_id, source_type=source_type, execution_adapter=execution_adapter)

    async def release_transport(self, claim: WorkClaim | Mapping[str, Any], *, retry_at: datetime | None = None) -> bool:
        return await self.coordinator.release_transport(claim, retry_at=retry_at)

    async def settle_transport(self, claim: WorkClaim | Mapping[str, Any], outcome: Any) -> dict[str, Any] | None:
        return await self.coordinator.settle_transport(claim, outcome, source_ok=True)

    @staticmethod
    def _notification_receipt_ack_requested(
        delivery: Any,
        result: Mapping[str, Any],
    ) -> bool:
        """Allow the terminal notification ACK contract only for events."""

        requested = bool(result.get("notification_receipt_ack"))
        if requested and _text(_field(delivery, "kind"), 32).casefold() != "event":
            raise DialogueTransportError("notification_receipt_ack_kind_invalid")
        return requested

    async def handoff(self, claim: WorkClaim | Mapping[str, Any]) -> dict[str, Any]:
        normalized = self.coordinator._coerce_claim(claim)
        session = await self._session()
        try:
            work = (
                await session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == _uuid(normalized.work_item_id, "work_item_id"))
                    .with_for_update()
                )
            ).scalars().first()
            if work is None or work.source_type != SOURCE_TYPE or work.execution_adapter != EXECUTION_ADAPTER or work.assigned_agent_id is not None:
                raise DialogueTransportError("transport_work_binding_invalid")
            now = self._now()
            if (
                work.state != "claimed"
                or work.lease_owner != normalized.lease_owner
                or work.lease_token != normalized.lease_token
                or work.lease_expires_at is None
                or work.lease_expires_at <= now
            ):
                raise DialogueTransportError("transport_lease_lost")
            renewed = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == work.id,
                    AgentWorkItem.state == "claimed",
                    AgentWorkItem.lease_owner == normalized.lease_owner,
                    AgentWorkItem.lease_token == normalized.lease_token,
                    AgentWorkItem.lease_expires_at > now,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self.coordinator.lease_seconds),
                    updated_at=now,
                )
            )
            if int(getattr(renewed, "rowcount", 0) or 0) != 1:
                raise DialogueTransportError("transport_lease_lost")
            work.lease_expires_at = now + timedelta(seconds=self.coordinator.lease_seconds)
            work.heartbeat_at = now
            delivery = await session.get(AgentDialogueDelivery, _uuid(work.source_id, "delivery_id"))
            if delivery is None:
                raise DialogueTransportError("delivery_not_found")
            dialogue = await self._lock_dialogue(session, delivery.dialogue_id)
            delivery_kind = _text(delivery.kind, 32).casefold()
            if delivery_kind in {"request", "control"}:
                expected_source_session = dialogue.root_session_id
                expected_source_agent = dialogue.root_agent_id
                expected_source_revision = dialogue.root_agent_revision_id
                expected_receiver_session = dialogue.worker_session_id
                expected_receiver_agent = dialogue.worker_agent_id
                expected_receiver_revision = dialogue.worker_agent_revision_id
            elif delivery_kind in {"response", "event"}:
                expected_source_session = dialogue.worker_session_id
                expected_source_agent = dialogue.worker_agent_id
                expected_source_revision = dialogue.worker_agent_revision_id
                expected_receiver_session = dialogue.root_session_id
                expected_receiver_agent = dialogue.root_agent_id
                expected_receiver_revision = dialogue.root_agent_revision_id
            else:
                raise DialogueTransportError("delivery_kind_invalid")
            if (
                delivery.source_session_id != expected_source_session
                or delivery.source_agent_id != expected_source_agent
                or delivery.source_agent_revision_id != expected_source_revision
                or delivery.receiver_session_id != expected_receiver_session
                or delivery.receiver_agent_id != expected_receiver_agent
                or delivery.receiver_agent_revision_id != expected_receiver_revision
            ):
                raise DialogueTransportError("delivery_direction_invalid")
            if delivery.receiver_run_id and delivery.state in {"delivered", "acked"}:
                projection = self._projection(delivery)
                # Release the FOR UPDATE lock before the common coordinator
                # opens its token-fenced settlement session.  Calling it while
                # this transaction still owns the Work row deadlocks on
                # PostgreSQL.
                await session.rollback()
                await self.coordinator.settle_transport(normalized, ExecutionOutcome(classification="succeeded", result_summary="dialogue handoff already committed"))
                return {**projection, "idempotent": True}
            await self._validate_dialogue(session, dialogue, int(delivery.expected_control_revision or 0))
            now = self._now()
            if delivery.deadline is not None and delivery.deadline <= now:
                delivery.state = "expired"
                await session.commit()
                await self.coordinator.settle_transport(normalized, ExecutionOutcome(classification="permanent", error_code="delivery_deadline_expired"), source_ok=True)
                return {**self._projection(delivery), "expired": True}
            if delivery.available_at is not None and delivery.available_at > now:
                projection = self._projection(delivery)
                deferred_at = delivery.available_at
                await session.rollback()
                await self.coordinator.release_transport(normalized, retry_at=deferred_at)
                return {**projection, "deferred": True}
            source_message = await session.get(ConversationMessage, delivery.source_message_id)
            if source_message is None or source_message.session_id != delivery.source_session_id:
                raise DialogueTransportError("source_message_mismatch")
            source_run = await session.get(AgentRun, delivery.source_run_id) if delivery.source_run_id else None
            source_work = await session.get(AgentWorkItem, delivery.source_work_item_id) if delivery.source_work_item_id else None
            expected_hash = self._provenance(dialogue, source_run, source_work, source_message)
            work_metadata = work.metadata_json if isinstance(work.metadata_json, Mapping) else {}
            stored_binding_hash = work_metadata.get("binding_hash")
            stored_hash = work_metadata.get("provenance_hash")
            if stored_binding_hash and stored_binding_hash != expected_hash:
                raise DialogueTransportError("source_provenance_conflict")
            bridge = self.dispatch_bridge
            if bridge is None or not callable(getattr(bridge, "dispatch_trusted_agent_dialogue", None)):
                raise DialogueTransportUnavailable("dialogue_dispatch_bridge_unavailable")
            try:
                result = await bridge.dispatch_trusted_agent_dialogue(
                    session=session,
                    dialogue=dialogue,
                    delivery=delivery,
                    source_message=source_message,
                    provenance_hash=stored_hash or expected_hash,
                )
            except InteractiveAdmissionError as exc:
                # A different live Root/Chat execution owns this conversation
                # resource.  This is a deferral, not a provider attempt: drop
                # the transport lease and return it to pending so a four
                # second backoff cannot consume all five transport attempts.
                if exc.code not in {
                    "interactive_session_busy",
                    "interactive_shared_resource_busy",
                    "agent_concurrency_limit",
                    "dialogue_human_question_pending",
                }:
                    raise
                await session.rollback()
                await self.coordinator.release_transport(
                    normalized,
                    retry_at=self._now() + timedelta(seconds=4),
                )
                raise DialogueTransportUnavailable(
                    "dialogue_handoff_deferred"
                ) from exc
            if not isinstance(result, Mapping):
                raise DialogueTransportUnavailable("dialogue_dispatch_bridge_invalid_receipt")
            notification_receipt_ack = self._notification_receipt_ack_requested(
                delivery,
                result,
            )
            receiver_run_id = _uuid(result.get("receiver_run_id") or result.get("run_id"), "receiver_run_id")
            if result.get("parent_run_id") is not None:
                raise DialogueTransportError("cross_agent_parent_run_forbidden")
            result_session_id = result.get("session_id")
            if result_session_id is None or str(result_session_id) != str(expected_receiver_session):
                raise DialogueTransportError("receiver_run_session_mismatch")
            if result.get("user_id") is None or str(result.get("user_id")) != str(dialogue.owner_user_id):
                raise DialogueTransportError("receiver_run_human_owner_mismatch")
            result_agent_id = result.get("agent_id")
            result_revision_id = result.get("agent_revision_id")
            if (
                result_agent_id is None
                or str(result_agent_id) != str(expected_receiver_agent)
            ) or (
                result_revision_id is None
                or str(result_revision_id) != str(expected_receiver_revision)
            ):
                raise DialogueTransportError("receiver_run_agent_binding_mismatch")
            receiver_work_id = _uuid(result.get("receiver_work_item_id") or result.get("work_item_id"), "receiver_work_item_id")
            receiver_message_id = _uuid(result.get("receiver_message_id") or result.get("message_id") or result.get("trigger_message_id"), "receiver_message_id")
            delivery.receiver_run_id, delivery.receiver_work_item_id, delivery.receiver_message_id = receiver_run_id, receiver_work_id, receiver_message_id
            if notification_receipt_ack:
                receipt_digest = _text(result.get("response_digest"), 64).casefold()
                if receipt_digest and _SHA256.fullmatch(receipt_digest):
                    delivery.response_digest = receipt_digest
            delivery.state, delivery.updated_at = (
                "acked" if notification_receipt_ack else "delivered",
                now,
            )
            await session.commit()
            settled = await self.coordinator.settle_transport(normalized, ExecutionOutcome(classification="succeeded", result_summary="dialogue handoff committed", result={"delivery_id": str(delivery.id), "receiver_run_id": str(receiver_run_id)}), source_ok=True)
            return {
                **self._projection(delivery),
                "created": bool(result.get("created", True)),
                "idempotent": notification_receipt_ack,
                "settlement": settled,
            }
        except DialogueTransportUnavailable as exc:
            # ``dialogue_handoff_deferred`` is the deliberate busy/input
            # deferral raised after the claim has already been refunded.  Do
            # not let the broad failure path convert it into a consumed
            # ``dialogue_handoff_failed`` outcome.
            await self._rollback(session)
            if str(exc) == "dialogue_handoff_deferred":
                raise
            raise
        except DialogueTransportError:
            await self._rollback(session)
            raise
        except Exception as exc:
            await self._rollback(session)
            permanent_code = _permanent_dialogue_admission_code(exc)
            if permanent_code is not None:
                try:
                    await self.coordinator.settle_transport(
                        normalized,
                        ExecutionOutcome(
                            classification="permanent",
                            error_code=permanent_code,
                            error_message="dialogue identity admission was rejected",
                        ),
                        source_ok=True,
                    )
                except Exception:
                    logger.debug(
                        "Failed to persist permanent dialogue admission denial",
                        exc_info=True,
                    )
                raise DialogueTransportUnavailable(permanent_code) from exc
            # A handoff that reached an unexpected runtime/provider-free
            # exception has consumed a transport attempt.  Do not return it
            # through ``release_transport`` (which intentionally refunds an
            # attempt for busy/availability deferrals).  The common settle
            # path records retry_wait/dead-letter state with its normal
            # bounded attempt accounting; if that settlement is unavailable,
            # the caller still receives the original failure for recovery.
            try:
                await self.coordinator.settle_transport(
                    normalized,
                    ExecutionOutcome(
                        classification="transient",
                        error_code="dialogue_handoff_failed",
                        error_message="dialogue handoff failed",
                    ),
                    source_ok=True,
                )
            except Exception:
                logger.debug("Failed to settle unexpected dialogue handoff failure", exc_info=True)
            raise DialogueTransportUnavailable("dialogue_handoff_failed") from exc
        finally:
            await self._close(session)

    dispatch = handoff
    handoff_claim = handoff

    async def _validate_receiver_run(
        self,
        session: Any,
        dialogue: Any,
        delivery: Any,
        receiver_run: Any,
        requested_work_item_id: uuid.UUID | None = None,
        *,
        strict_delivery: bool = False,
    ) -> Any:
        """Validate the receiver role before acknowledging a delivery."""

        kind = _text(delivery.kind, 32).casefold()
        if kind in {"request", "control"}:
            expected_session = dialogue.worker_session_id
            expected_agent = dialogue.worker_agent_id
            expected_revision = dialogue.worker_agent_revision_id
        elif kind in {"response", "event"}:
            expected_session = dialogue.root_session_id
            expected_agent = dialogue.root_agent_id
            expected_revision = dialogue.root_agent_revision_id
        else:
            raise DialogueTransportError("delivery_kind_invalid")
        if (
            not _same_uuid(delivery.receiver_session_id, expected_session)
            or not _same_uuid(delivery.receiver_agent_id, expected_agent)
            or not _same_uuid(delivery.receiver_agent_revision_id, expected_revision)
        ):
            raise DialogueTransportError("receiver_binding_mismatch")
        if receiver_run is None:
            raise DialogueTransportError("receiver_binding_mismatch")
        if delivery.receiver_run_id not in (None, "") and not _same_uuid(delivery.receiver_run_id, receiver_run.id):
            raise DialogueTransportError("receiver_binding_mismatch")
        if strict_delivery and not _same_uuid(delivery.receiver_run_id, receiver_run.id):
            raise DialogueTransportError("receiver_binding_mismatch")
        receiver_status = _text(getattr(receiver_run, "status", None), 32).casefold()
        if (
            receiver_run is None
            or _text(receiver_run.run_type).casefold() != "chat_turn"
            or receiver_status not in _TERMINAL_RUN_STATES
            or receiver_run.parent_run_id is not None
            or not _same_uuid(receiver_run.session_id, expected_session)
            or not _same_uuid(receiver_run.agent_id, expected_agent)
            or not _same_uuid(receiver_run.agent_revision_id, expected_revision)
            or _text(receiver_run.user_id) != str(dialogue.owner_user_id)
        ):
            raise DialogueTransportError("receiver_binding_mismatch")
        work_id = requested_work_item_id or _uuid(delivery.receiver_work_item_id, "receiver_work_item_id", required=False) or _uuid(receiver_run.work_item_id, "receiver_work_item_id", required=False)
        if work_id is None or not _same_uuid(receiver_run.work_item_id, work_id):
            raise DialogueTransportError("receiver_work_binding_mismatch")
        work = await session.get(AgentWorkItem, work_id)
        state_matches = _run_work_state_matches(receiver_run, work)
        if not state_matches:
            state_matches = await _blocked_authority_proof(session, receiver_run, work)
        if (
            work is None
            or not _same_uuid(work.id, receiver_run.work_item_id)
            or not _same_uuid(work.conversation_session_id, expected_session)
            or not _same_uuid(work.assigned_agent_id, expected_agent)
            or not _same_uuid(work.agent_revision_id, expected_revision)
            or _text(work.source_type).casefold() != "chat_turn"
            or _text(work.execution_adapter).casefold() != "interactive_chat"
            or not state_matches
            or (
                work.active_agent_run_id not in (None, "")
                and not _same_uuid(work.active_agent_run_id, receiver_run.id)
            )
        ):
            raise DialogueTransportError("receiver_work_binding_mismatch")
        if delivery.receiver_work_item_id not in (None, "") and not _same_uuid(delivery.receiver_work_item_id, work.id):
            raise DialogueTransportError("receiver_work_binding_mismatch")
        return work

    async def _validate_delivery_source(self, session: Any, dialogue: Any, delivery: Any) -> Any:
        """Validate the immutable source side before a terminal callback."""

        kind = _text(delivery.kind, 32).casefold()
        if kind in {"request", "control"}:
            expected_session = dialogue.root_session_id
            expected_agent = dialogue.root_agent_id
            expected_revision = dialogue.root_agent_revision_id
        elif kind in {"response", "event"}:
            expected_session = dialogue.worker_session_id
            expected_agent = dialogue.worker_agent_id
            expected_revision = dialogue.worker_agent_revision_id
        else:
            raise DialogueTransportError("delivery_kind_invalid")
        source_run_id = _uuid(delivery.source_run_id, "source_run_id")
        source_work_id = _uuid(delivery.source_work_item_id, "source_work_item_id")
        source_message_id = _uuid(delivery.source_message_id, "source_message_id")
        source_run = await session.get(AgentRun, source_run_id)
        source_work = await session.get(AgentWorkItem, source_work_id)
        source_message = await session.get(ConversationMessage, source_message_id)
        if (
            source_run is None
            or source_work is None
            or source_message is None
            or _text(source_run.run_type).casefold() != "chat_turn"
            or source_run.parent_run_id is not None
            or not _same_uuid(source_run.session_id, expected_session)
            or not _same_uuid(source_run.agent_id, expected_agent)
            or not _same_uuid(source_run.agent_revision_id, expected_revision)
            or _text(source_run.user_id) != str(dialogue.owner_user_id)
            or not _same_uuid(source_work.id, source_run.work_item_id)
            or not _same_uuid(source_work.conversation_session_id, expected_session)
            or not _same_uuid(source_work.assigned_agent_id, expected_agent)
            or not _same_uuid(source_work.agent_revision_id, expected_revision)
            or _text(source_work.source_type).casefold() != "chat_turn"
            or _text(source_work.execution_adapter).casefold() != "interactive_chat"
            or not (
                _run_work_state_matches(source_run, source_work)
                or await _blocked_authority_proof(session, source_run, source_work)
            )
            or (
                source_work.active_agent_run_id not in (None, "")
                and not _same_uuid(source_work.active_agent_run_id, source_run.id)
            )
        ):
            raise DialogueTransportError("source_provenance_mismatch")
        metadata = source_message.message_metadata if isinstance(source_message.message_metadata, Mapping) else {}
        if (
            not _same_uuid(source_message.session_id, expected_session)
            or _text(source_message.role).casefold() != "assistant"
            or _text(source_message.sender_type).casefold() != "agent"
            or _text(source_message.sender_id) != str(expected_agent)
            or _text(metadata.get("agent_dialogue_id")) != str(dialogue.id)
            or source_message.deleted_at is not None
        ):
            raise DialogueTransportError("source_message_mismatch")
        if kind in {"response", "event"} and _text(metadata.get("agent_run_id")) != str(source_run.id):
            raise DialogueTransportError("source_message_run_mismatch")
        return source_run

    @staticmethod
    def _validate_terminal_message(dialogue: Any, run: Any, message: Any, expected_agent_id: uuid.UUID) -> None:
        if message is None or run is None:
            raise DialogueTransportError("receiver_message_mismatch")
        metadata = message.message_metadata if isinstance(message.message_metadata, Mapping) else {}
        if (
            not _same_uuid(message.session_id, run.session_id)
            or _text(message.role).casefold() != "assistant"
            or _text(message.sender_type).casefold() != "agent"
            or _text(message.sender_id) != str(expected_agent_id)
            or _text(metadata.get("agent_dialogue_id")) != str(dialogue.id)
            or _text(metadata.get("agent_run_id")) != str(run.id)
            or message.deleted_at is not None
        ):
            raise DialogueTransportError("receiver_message_mismatch")

    async def receipt(self, delivery_id: str, *, receiver_run_id: str, receiver_work_item_id: str | None = None, receiver_message_id: str | None = None, response_digest: str | None = None, state: str = "succeeded", input_id: str | None = None, session: Any | None = None) -> dict[str, Any]:
        delivery_uuid, receiver_uuid = _uuid(delivery_id, "delivery_id"), _uuid(receiver_run_id, "receiver_run_id")
        digest = _text(response_digest, 64).casefold() if response_digest else None
        if digest is not None and not _SHA256.fullmatch(digest):
            raise DialogueTransportError("response_digest_invalid")
        owns = session is None
        db_session = session or await self._session()
        try:
            delivery = await db_session.get(AgentDialogueDelivery, delivery_uuid)
            if delivery is None:
                raise DialogueTransportError("delivery_not_found")
            dialogue = await self._lock_dialogue(db_session, delivery.dialogue_id)
            if int(dialogue.control_revision or 0) != int(delivery.expected_control_revision or 0):
                raise DialogueTransportConflict("late_dialogue_receipt_control_revision")
            await self._validate_delivery_source(db_session, dialogue, delivery)
            requested_work = _uuid(receiver_work_item_id, "receiver_work_item_id", required=False)
            stored_work = _uuid(
                delivery.receiver_work_item_id,
                "receiver_work_item_id",
                required=False,
            )
            if requested_work is not None and stored_work is not None and requested_work != stored_work:
                raise DialogueTransportError("receiver_work_binding_mismatch")
            effective_work = requested_work or stored_work
            receiver_run = await db_session.get(AgentRun, receiver_uuid)
            validated_work = await self._validate_receiver_run(
                db_session,
                dialogue,
                delivery,
                receiver_run,
                effective_work,
            )
            if delivery.state in {"acked", "failed", "cancelled", "expired"}:
                if owns:
                    await db_session.commit()
                return {**self._projection(delivery), "idempotent": True}
            message_uuid = _uuid(receiver_message_id or delivery.receiver_message_id, "receiver_message_id", required=False)
            if message_uuid is None:
                raise DialogueTransportError("receiver_message_required")
            message = await db_session.get(ConversationMessage, message_uuid)
            expected_agent = dialogue.worker_agent_id if _text(delivery.kind).casefold() in {"request", "control"} else dialogue.root_agent_id
            if message is None:
                raise DialogueTransportError("receiver_message_mismatch")
            self._validate_terminal_message(dialogue, receiver_run, message, expected_agent)
            delivery.receiver_run_id = receiver_uuid
            delivery.receiver_work_item_id = validated_work.id
            delivery.receiver_message_id = message_uuid
            delivery.response_digest = digest or sha256_digest(getattr(message, "_content", None) or getattr(message, "content", "") or "")
            delivery.state, delivery.updated_at = ("acked" if state in {"succeeded", "acked", "completed", "input_pending"} else "failed"), self._now()
            response = None
            delivery_kind = _text(delivery.kind).casefold()
            if delivery.state == "acked" and delivery_kind in {"request", "control"}:
                response = await self._ensure_response(
                    db_session,
                    dialogue,
                    delivery,
                    kind="event" if state == "input_pending" else "response",
                    input_id=input_id,
                )
            elif delivery.state == "failed" and delivery_kind in {"request", "control"}:
                # A typed safe failure message, when present in the encrypted
                # worker session, is delivered exactly once to the root.  The
                # input-pending branch above uses its input id and therefore
                # cannot double-notify as a generic failure.
                response = await self._ensure_response(db_session, dialogue, delivery, kind="event")
            event_failure = False
            if _text(delivery.kind).casefold() == "event" and _text(state, 32).casefold() in {"succeeded", "acked", "completed"}:
                source_event = await db_session.get(ConversationMessage, delivery.source_message_id) if delivery.source_message_id else None
                source_metadata = source_event.message_metadata if source_event is not None and isinstance(source_event.message_metadata, Mapping) else {}
                event_failure = bool(
                    source_metadata.get("safe_error_code")
                    and _text(source_metadata.get("safe_error_code"), 96).casefold() != "dialogue_input_pending"
                )
            self._apply_receipt_dialogue_state(
                dialogue,
                delivery,
                state=state,
                input_id=input_id,
                event_failure=event_failure,
                now=self._now(),
            )
            if owns:
                await db_session.commit()
            return {**self._projection(delivery), "dialogue": project_dialogue(dialogue), "response_digest": delivery.response_digest, "response_delivery_id": _id(response.id) if response else None, "idempotent": False}
        except Exception:
            if owns:
                await self._rollback(db_session)
            raise
        finally:
            if owns:
                await self._close(db_session)

    record_receipt = receipt
    acknowledge = receipt

    @staticmethod
    def _apply_receipt_dialogue_state(
        dialogue: Any,
        delivery: Any,
        *,
        state: str,
        input_id: str | None = None,
        event_failure: bool = False,
        now: datetime,
    ) -> None:
        """Fold a terminal worker/root receipt into the dialogue state.

        A progress/event delivery is intentionally observational.  Request
        and response receipts are the terminal execution truth; an input
        pause remains waiting until a new finite answer turn is admitted.
        Existing cancellation/expiry fences are never reopened by a late
        receipt.
        """

        current = _text(_field(dialogue, "state"), 32).casefold()
        if current in {"cancelled", "expired"}:
            return
        kind = _text(_field(delivery, "kind"), 32).casefold()
        normalized = _text(state, 32).casefold()
        if kind == "event" and input_id in (None, "") and normalized not in {"failed", "cancelled", "expired"} and not event_failure:
            return
        if normalized == "input_pending" or input_id not in (None, ""):
            next_state = "waiting"
        elif kind == "request":
            # The request receipt creates a response/event delivery.  Keep
            # the dialogue admissible until that root delivery is handed off
            # and acknowledged; otherwise the bridge would reject its own
            # terminal notification as a delivery for a failed/succeeded
            # dialogue.
            return
        elif normalized in {"succeeded", "acked", "completed"} and kind == "response":
            next_state = "succeeded"
        elif normalized in {"succeeded", "acked", "completed"} and kind == "event" and event_failure:
            next_state = "failed"
        elif normalized in {"failed", "cancelled", "expired"}:
            next_state = "cancelled" if normalized == "cancelled" else "failed" if normalized == "failed" else "expired"
        else:
            return
        setattr(dialogue, "state", next_state)
        setattr(dialogue, "updated_at", now)

    async def _ensure_response(self, session: Any, dialogue: Any, delivery: Any, *, kind: str = "response", input_id: str | None = None) -> Any:
        if (
            _text(delivery.kind, 32).casefold() not in {"request", "control"}
            or not _same_uuid(delivery.source_session_id, dialogue.root_session_id)
            or not _same_uuid(delivery.source_agent_id, dialogue.root_agent_id)
            or not _same_uuid(delivery.source_agent_revision_id, dialogue.root_agent_revision_id)
            or not _same_uuid(delivery.receiver_session_id, dialogue.worker_session_id)
            or not _same_uuid(delivery.receiver_agent_id, dialogue.worker_agent_id)
            or not _same_uuid(delivery.receiver_agent_revision_id, dialogue.worker_agent_revision_id)
            or delivery.receiver_run_id is None
            or delivery.receiver_work_item_id is None
            or delivery.receiver_message_id is None
        ):
            raise DialogueTransportError("worker_return_source_binding_invalid")
        receiver_run = await session.get(AgentRun, delivery.receiver_run_id)
        await self._validate_receiver_run(session, dialogue, delivery, receiver_run, _uuid(delivery.receiver_work_item_id, "receiver_work_item_id"))
        receiver_message = await session.get(ConversationMessage, delivery.receiver_message_id)
        self._validate_terminal_message(dialogue, receiver_run, receiver_message, dialogue.worker_agent_id)
        key = f"input:{input_id}:{delivery.id}" if input_id else f"worker-return:{dialogue.id}:{delivery.receiver_message_id}:{kind}"
        existing = await self._existing(session, dialogue.id, key)
        if existing is not None:
            return existing
        available = self._now()
        if await self._session_busy(session, dialogue.root_session_id):
            available += timedelta(seconds=1)
        response = AgentDialogueDelivery(
            id=uuid.uuid4(), dialogue_id=dialogue.id, source_session_id=dialogue.worker_session_id,
            source_agent_id=dialogue.worker_agent_id, source_agent_revision_id=dialogue.worker_agent_revision_id,
            source_run_id=delivery.receiver_run_id, source_work_item_id=delivery.receiver_work_item_id, source_message_id=delivery.receiver_message_id,
            receiver_session_id=dialogue.root_session_id, receiver_agent_id=dialogue.root_agent_id, receiver_agent_revision_id=dialogue.root_agent_revision_id,
            kind=kind, state="pending" if available > self._now() else "available", sequence=await self._next_sequence(session, dialogue.id),
            causation_id=str(input_id or delivery.id), depth=min(MAX_DEPTH, int(delivery.depth or 0) + 1), expected_control_revision=int(dialogue.control_revision or 0),
            idempotency_key=key, request_digest=delivery.response_digest, available_at=available, deadline=dialogue.deadline, attempt_count=0,
            created_at=self._now(), updated_at=self._now(),
        )
        session.add(response)
        await session.flush()
        await self._transport_work(session, dialogue, response, available)
        return response

    @staticmethod
    def _answer_root_provider_evidence(*values: Any) -> bool:
        """Reject any persisted provider/usage marker during answer recovery."""

        usage_keys = {
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "total_tokens",
        }
        for value in values:
            if not isinstance(value, Mapping):
                continue
            if value.get("provider_started") is True:
                return True
            if isinstance(value.get("usage"), Mapping):
                return True
            if any(key in value for key in usage_keys):
                return True
            for nested_key in ("agent_dialogue", "result", "metadata"):
                nested = value.get(nested_key)
                if isinstance(nested, Mapping) and AgentDialogueTransport._answer_root_provider_evidence(nested):
                    return True
        return False

    @staticmethod
    async def _prove_failed_answer_root_recovery(
        session: Any,
        *,
        dialogue: Any,
        input_row: Any,
        answer_message: Any,
        canonical_client_id: str,
    ) -> str | None:
        """Return one exact old answer Root Run that may be superseded.

        A durable answer can outlive a failed pre-provider Root admission.  A
        matching client key alone is insufficient: active, provider-started,
        charged, usage-unknown, stale, or foreign rows must remain terminal.
        The returned Run is historical evidence only; callers create a fresh
        idempotency key and never reactivate it.
        """

        existing_runs = (
            await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.session_id == dialogue.root_session_id,
                    AgentRun.user_id == str(dialogue.owner_user_id),
                    AgentRun.client_message_id == canonical_client_id,
                )
                .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
                .with_for_update()
            )
        ).scalars().all()
        if len(existing_runs) != 1:
            return None
        old_run = existing_runs[0]
        marker = (
            old_run.run_metadata.get("agent_dialogue")
            if isinstance(old_run.run_metadata, Mapping)
            else None
        )
        if not isinstance(marker, Mapping):
            marker = {}
        if (
            _text(old_run.status, 32).casefold() != "failed"
            or old_run.ended_at is None
            or old_run.started_at is not None
            or old_run.root_run_id != old_run.id
            or _text(old_run.run_type, 32).casefold() != "chat_turn"
            or old_run.session_id != dialogue.root_session_id
            or old_run.user_id != str(dialogue.owner_user_id)
            or old_run.agent_id != dialogue.root_agent_id
            or old_run.agent_revision_id != dialogue.root_agent_revision_id
            or old_run.trigger_message_id != answer_message.id
            or old_run.work_item_id is None
        ):
            return None
        if marker:
            for field, expected in (
                ("dialogue_id", str(dialogue.id)),
                ("input_id", str(input_row.id)),
                ("source_human_message_id", str(answer_message.id)),
            ):
                observed = marker.get(field)
                if observed not in (None, "") and _text(observed) != expected:
                    return None
            role = marker.get("role")
            if role not in (None, "") and _text(role, 32).casefold() != "root":
                return None
            for field, expected in (
                ("input_revision", int(input_row.revision or 0)),
                ("control_revision", int(dialogue.control_revision or 0)),
            ):
                observed = marker.get(field)
                if observed not in (None, "") and int(observed) != expected:
                    return None
            if (
                "source_human_answered" in marker
                and marker.get("source_human_answered") is not True
            ):
                return None
            answer_anchor = marker.get("source_human_answer")
            if answer_anchor is not None:
                if not isinstance(answer_anchor, Mapping):
                    return None
                if (
                    _text(answer_anchor.get("message_id")) != str(answer_message.id)
                    or _text(answer_anchor.get("session_id"))
                    != str(dialogue.root_session_id)
                    or _text(answer_anchor.get("owner_user_id"))
                    != str(dialogue.owner_user_id)
                    or _text(answer_anchor.get("body_digest"))
                    != hashlib.sha256(
                        str(answer_message.content or "").encode("utf-8")
                    ).hexdigest()
                ):
                    return None
        if AgentDialogueTransport._answer_root_provider_evidence(
            old_run.run_metadata,
            old_run.result,
            old_run.validation,
        ):
            return None

        work = (
            await session.execute(
                select(AgentWorkItem)
                .where(AgentWorkItem.id == old_run.work_item_id)
                .with_for_update()
            )
        ).scalars().first()
        outbox = (
            await session.execute(
                select(ConversationDispatchOutbox)
                .where(ConversationDispatchOutbox.run_id == old_run.id)
                .with_for_update()
            )
        ).scalars().first()
        if (
            work is None
            or outbox is None
            or _text(work.state, 32).casefold() != "blocked"
            or _text(work.outcome_classification, 32).casefold() != "blocked"
            or work.conversation_session_id != dialogue.root_session_id
            or work.assigned_agent_id != dialogue.root_agent_id
            or work.agent_revision_id != dialogue.root_agent_revision_id
            or _text(work.source_id) != str(old_run.id)
            or _text(work.source_type, 64).casefold() != "chat_turn"
            or _text(work.execution_adapter, 64).casefold() != "interactive_chat"
            or int(work.attempt_count or 0) != 0
            or work.active_agent_run_id is not None
            or work.started_at is not None
            or work.lease_owner not in (None, "")
            or work.lease_token not in (None, "")
            or work.lease_expires_at is not None
            or _text(outbox.status, 32).casefold()
            not in {"failed", "deadletter", "cancelled"}
            or outbox.run_id != old_run.id
            or outbox.session_id != dialogue.root_session_id
            or outbox.user_id != str(dialogue.owner_user_id)
            or outbox.client_message_id != canonical_client_id
            or outbox.lease_owner not in (None, "")
            or outbox.lease_token not in (None, "")
            or outbox.lease_expires_at is not None
        ):
            return None
        canonical_code = _text(old_run.error, 96).casefold()
        if canonical_code not in _ANSWER_RECOVERY_BUDGET_FAILURE_CODES:
            return None
        failure_codes: list[str] = []
        for value in (
            old_run.error,
            old_run.result,
            old_run.run_metadata,
            work.safe_error_code,
            work.metadata_json,
            work.result_summary_json,
            outbox.payload,
        ):
            if isinstance(value, Mapping):
                for key in ("safe_error_code", "error", "code"):
                    code = _text(value.get(key), 96).casefold()
                    if code:
                        failure_codes.append(code)
            elif value not in (None, ""):
                failure_codes.append(_text(value, 96).casefold())
        if (
            (
                _text(work.safe_error_code, 96).casefold()
                and _text(work.safe_error_code, 96).casefold() != canonical_code
            )
            or any(code != canonical_code for code in failure_codes)
        ):
            return None
        if AgentDialogueTransport._answer_root_provider_evidence(
            work.metadata_json,
            work.result_summary_json,
            outbox.payload,
        ):
            return None

        run_events = (
            await session.execute(
                select(AgentRunEvent).where(AgentRunEvent.run_id == old_run.id)
            )
        ).scalars().all()
        work_events = (
            await session.execute(
                select(AgentWorkEvent).where(AgentWorkEvent.work_item_id == work.id)
            )
        ).scalars().all()
        if not run_events or not work_events:
            return None

        def event_proof(events: Sequence[Any], *, work_event: bool) -> bool:
            proved = False
            for event in events:
                payload = (
                    _field(event, "payload_json", {})
                    if work_event
                    else _field(event, "payload", {})
                )
                payload = payload if isinstance(payload, Mapping) else {}
                event_code = _text(
                    _field(event, "safe_error_code", None)
                    or payload.get("safe_error_code")
                    or payload.get("error_code"),
                    96,
                ).casefold()
                if event_code and event_code != canonical_code:
                    return False
                if "provider_started" in payload:
                    if payload.get("provider_started") is not False:
                        return False
                    if event_code != canonical_code:
                        if not (
                            work_event
                            and not event_code
                            and payload.get("source")
                            == "agent_dialogue_budget_admission"
                            and _text(payload.get("run_id")) == str(old_run.id)
                            and _text(payload.get("dialogue_id")) == str(dialogue.id)
                        ):
                            return False
                    proved = True
            return proved

        if not event_proof(run_events, work_event=False) or not event_proof(
            work_events, work_event=True
        ):
            return None

        try:
            from .agent_dialogue_budget import get_dialogue_budget_owner

            budget_owner = await get_dialogue_budget_owner(session, dialogue, lock=True)
        except Exception:
            return None
        budget_root_id = getattr(budget_owner, "root_dialogue_id", None)
        if budget_root_id not in (None, "") and _text(
            getattr(dialogue, "budget_root_dialogue_id", None)
        ) != _text(budget_root_id):
            return None

        usage_rows = (
            await session.execute(
                select(AgentDialogueUsage)
                .where(AgentDialogueUsage.run_id == old_run.id)
                .with_for_update()
            )
        ).scalars().all()
        if usage_rows:
            return None
        reservations = (
            await session.execute(
                select(AgentDialogueReservation)
                .where(AgentDialogueReservation.run_id == old_run.id)
                .with_for_update()
            )
        ).scalars().all()
        if len(reservations) > 1:
            return None
        if reservations:
            reservation = reservations[0]
            if (
                reservation.dialogue_id != dialogue.id
                or reservation.run_id != old_run.id
                or _text(reservation.reservation_key, 160)
                != f"run:{old_run.id}"
                or (
                    budget_root_id not in (None, "")
                    and reservation.budget_root_dialogue_id != budget_root_id
                )
                or (
                    budget_root_id in (None, "")
                    and reservation.budget_root_dialogue_id not in (None, "")
                )
                or _text(reservation.state, 32).casefold() != "released"
                or int(reservation.charged_tokens or 0) != 0
                or int(reservation.charged_cost_micros or 0) != 0
            ):
                return None

        # The proof above only establishes that the historical pre-provider
        # reservation was released.  Before handing the answer back to the
        # normal dispatch bridge, re-run the same finite admission checks for
        # both the shared owner and the current local Dialogue scope.  One
        # unit is the smallest possible new turn/attempt/capacity demand; a
        # positive remaining capacity is admitted by the normal Root path,
        # while an unchanged exhausted budget is rejected before any new Run.
        try:
            from .agent_dialogue_budget import (
                DialogueBudgetError,
                assert_dialogue_budget_available,
            )

            for budget_scope in (budget_owner, dialogue):
                assert_dialogue_budget_available(
                    budget_scope,
                    turns=1,
                    attempts=1,
                    tokens=1,
                    cost_micros=1,
                )
        except DialogueBudgetError:
            return None
        return str(old_run.id)

    async def recover_deferred_human_answers(self, limit: int = 16) -> int:
        """Resume committed Human answers after an exact Root question ends.

        ``AgentDialogueInput`` is the durable continuation pointer.  The
        answer transaction may commit while an exact event-created Root Run
        still owns the root session; recovery must wait for that Run/Work to
        become terminal and then call the normal canonical Human dispatch.
        No provider is replayed and no client nonce is required.
        """

        bounded_limit = max(1, min(int(limit or 1), 64))
        scan_limit = min(512, max(64, bounded_limit * 8))
        session = await self._session()
        candidates: list[dict[str, str]] = []
        try:
            rows = (
                await session.execute(
                    select(AgentDialogueInput, AgentDialogue)
                    .join(AgentDialogue, AgentDialogue.id == AgentDialogueInput.dialogue_id)
                    .where(
                        AgentDialogueInput.state == "answered",
                        AgentDialogueInput.answer_message_id.is_not(None),
                        AgentDialogue.control_revision
                        == AgentDialogueInput.expected_control_revision,
                        AgentDialogue.state.notin_(("cancelled", "expired", "succeeded")),
                    )
                    .order_by(AgentDialogueInput.updated_at, AgentDialogueInput.id)
                    .limit(scan_limit)
                )
            ).all()
            for input_row, dialogue in rows:
                source_run_id = _uuid(
                    input_row.source_run_id, "source_run_id", required=False
                )
                source_work_id = _uuid(
                    input_row.source_work_item_id,
                    "source_work_item_id",
                    required=False,
                )
                source_message_id = _uuid(
                    input_row.source_message_id,
                    "source_message_id",
                    required=False,
                )
                answer_id = _uuid(
                    input_row.answer_message_id,
                    "answer_message_id",
                    required=False,
                )
                if None in (source_run_id, source_work_id, source_message_id, answer_id):
                    continue
                answer_message = await session.get(ConversationMessage, answer_id)
                answer_metadata = (
                    answer_message.message_metadata
                    if answer_message is not None
                    and isinstance(answer_message.message_metadata, Mapping)
                    else {}
                )
                if (
                    answer_message is None
                    or answer_message.session_id != dialogue.root_session_id
                    or _text(answer_message.role).casefold() != "user"
                    or _text(answer_message.sender_type).casefold() not in {"user", "human"}
                    or not _same_uuid(answer_message.sender_id, dialogue.owner_user_id)
                    or answer_metadata.get("human_answer_source") is not True
                    or _text(answer_metadata.get("agent_dialogue_id"))
                    != str(dialogue.id)
                    or _text(answer_metadata.get("agent_dialogue_input_id"))
                    != str(input_row.id)
                    or int(answer_metadata.get("input_revision", -1))
                    != int(input_row.revision or 0)
                    or int(answer_metadata.get("control_revision", -1))
                    != int(dialogue.control_revision or 0)
                ):
                    continue
                source_run = await session.get(AgentRun, source_run_id)
                source_work = await session.get(AgentWorkItem, source_work_id)
                source_message = await session.get(ConversationMessage, source_message_id)
                source_metadata = (
                    source_message.message_metadata
                    if source_message is not None
                    and isinstance(source_message.message_metadata, Mapping)
                    else {}
                )
                if (
                    source_run is None
                    or source_work is None
                    or source_message is None
                    or source_run.session_id != dialogue.worker_session_id
                    or source_run.agent_id != dialogue.worker_agent_id
                    or source_run.agent_revision_id != dialogue.worker_agent_revision_id
                    or source_run.work_item_id != source_work.id
                    or source_work.conversation_session_id != dialogue.worker_session_id
                    or source_work.assigned_agent_id != dialogue.worker_agent_id
                    or source_work.agent_revision_id != dialogue.worker_agent_revision_id
                    or source_message.session_id != dialogue.worker_session_id
                    or _text(source_message.role).casefold() != "assistant"
                    or _text(source_message.sender_type).casefold() != "agent"
                    or not _same_uuid(source_message.sender_id, dialogue.worker_agent_id)
                    or _text(source_metadata.get("agent_dialogue_id"))
                    != str(dialogue.id)
                    or _text(source_metadata.get("agent_dialogue_input_id"))
                    != str(input_row.id)
                ):
                    continue
                canonical_client_id = (
                    f"dialogue-human-answer:{input_row.id}:"
                    f"{int(input_row.revision or 0)}:{answer_id}"
                )
                existing_runs = (
                    await session.execute(
                        select(AgentRun).where(
                            AgentRun.session_id == dialogue.root_session_id,
                            AgentRun.user_id == str(dialogue.owner_user_id),
                            AgentRun.client_message_id == canonical_client_id,
                        )
                    )
                ).scalars().all()
                recovery_from_run_id = None
                if existing_runs:
                    recovery_from_run_id = await self._prove_failed_answer_root_recovery(
                        session,
                        dialogue=dialogue,
                        input_row=input_row,
                        answer_message=answer_message,
                        canonical_client_id=canonical_client_id,
                    )
                    if recovery_from_run_id is None:
                        # Preserve idempotence for active/already-complete
                        # rows and fail closed for unverifiable collisions.
                        continue
                # An exact event-created Root question Run still owns the
                # session; wait for its terminal callback/restart recovery.
                exact_event_active = False
                for root_run in (
                    await session.execute(
                        select(AgentRun).where(
                            AgentRun.session_id == dialogue.root_session_id,
                            AgentRun.run_type == "chat_turn",
                        )
                    )
                ).scalars().all():
                    marker = (
                        root_run.run_metadata.get("agent_dialogue")
                        if isinstance(root_run.run_metadata, Mapping)
                        else None
                    )
                    if not isinstance(marker, Mapping):
                        continue
                    if (
                        _text(marker.get("dialogue_id")) != str(dialogue.id)
                        or _text(marker.get("role")).casefold() != "root"
                        or _text(marker.get("delivery_kind")).casefold() != "event"
                        or _text(marker.get("source_run_id")) != str(source_run_id)
                        or _text(marker.get("source_work_item_id")) != str(source_work_id)
                        or _text(marker.get("source_message_id")) != str(source_message_id)
                    ):
                        continue
                    if _text(root_run.status).casefold() in _ACTIVE_RUN_STATES:
                        exact_event_active = True
                        break
                if exact_event_active:
                    continue
                candidate = {
                    "dialogue_id": str(dialogue.id),
                    "input_id": str(input_row.id),
                    "answer_message_id": str(answer_id),
                    "owner_user_id": str(dialogue.owner_user_id),
                    "control_revision": str(int(dialogue.control_revision or 0)),
                    "input_revision": str(int(input_row.revision or 0)),
                    "client_message_id": canonical_client_id,
                }
                if recovery_from_run_id is not None:
                    candidate["recovery_from_run_id"] = recovery_from_run_id
                candidates.append(candidate)
                if len(candidates) >= bounded_limit:
                    break
        except OperationalError as exc:
            # The dialogue Input ledger is optional during rolling schema
            # upgrades. Existing terminal receipt recovery must remain
            # usable until that table is installed.
            if "agent_dialogue_inputs" in str(exc).casefold():
                return 0
            raise
        finally:
            await self._close(session)
        bridge = self.dispatch_bridge
        dispatch = getattr(bridge, "dispatch_dialogue_human_input", None)
        if not callable(dispatch):
            return 0
        recovered = 0
        for candidate in candidates:
            try:
                result = await dispatch(
                    dialogue_id=candidate["dialogue_id"],
                    input_id=candidate["input_id"],
                    answer_message_id=candidate["answer_message_id"],
                    owner_user_id=candidate["owner_user_id"],
                    expected_control_revision=int(candidate["control_revision"]),
                    expected_input_revision=int(candidate["input_revision"]),
                    client_message_id=candidate["client_message_id"],
                    recovery_from_run_id=candidate.get("recovery_from_run_id"),
                )
                if isinstance(result, Mapping) and result.get("created") is True:
                    recovered += 1
            except InteractiveAdmissionError as exc:
                if exc.code not in {
                    "interactive_session_busy",
                    "interactive_shared_resource_busy",
                    "agent_concurrency_limit",
                    "dialogue_human_answer_deferred",
                }:
                    logger.debug("Deferred Human answer recovery rejected", exc_info=True)
            except Exception:
                logger.debug("Deferred Human answer recovery unavailable", exc_info=True)
        return recovered

    async def reconcile_terminal_receipts(self, limit: int = 16) -> int:
        """Recover delivered worker requests whose terminal callback was lost.

        This is a bounded, provider-free pass over already-settled transport
        Work.  It never claims or replays a provider attempt; the ordinary
        callback/receipt path revalidates the exact worker Run/Work/message
        and its idempotency key.
        """

        bounded_limit = max(1, min(int(limit or 1), 64))
        scan_limit = min(512, max(64, bounded_limit * 8))
        now = self._now()
        session = await self._session()
        try:
            rows = (
                await session.execute(
                    select(AgentDialogueDelivery.id, AgentRun.id)
                    .join(AgentDialogue, AgentDialogue.id == AgentDialogueDelivery.dialogue_id)
                    .join(AgentRun, AgentRun.id == AgentDialogueDelivery.receiver_run_id)
                    .join(AgentWorkItem, AgentWorkItem.id == AgentDialogueDelivery.transport_work_item_id)
                    .where(
                        AgentDialogueDelivery.kind.in_(("request", "control", "response")),
                        AgentDialogueDelivery.state == "delivered",
                        AgentDialogueDelivery.receiver_run_id.is_not(None),
                        AgentDialogueDelivery.receiver_work_item_id.is_not(None),
                        AgentWorkItem.state == "succeeded",
                        AgentRun.status.in_(tuple(_TERMINAL_RUN_STATES)),
                        AgentDialogue.control_revision == AgentDialogueDelivery.expected_control_revision,
                        or_(
                            AgentDialogueDelivery.available_at.is_(None),
                            AgentDialogueDelivery.available_at <= now,
                            AgentRun.status.in_(("failed", "cancelled")),
                        ),
                        or_(AgentDialogueDelivery.deadline.is_(None), AgentDialogueDelivery.deadline > now),
                    )
                    .order_by(AgentDialogueDelivery.updated_at, AgentDialogueDelivery.id)
                    .limit(scan_limit)
                )
            ).all()
        finally:
            await self._close(session)
        recovered = 0
        for delivery_id, run_id in rows:
            if recovered >= bounded_limit:
                break
            value = await self.record_worker_terminal(
                run_id=str(run_id),
                delivery_id=str(delivery_id),
                recover_missing_failure=True,
            )
            if value is not None:
                recovered += 1
        recovered += await self.recover_deferred_human_answers(limit=bounded_limit)
        return recovered

    async def _close_failed_receiver_delivery(
        self,
        session: Any,
        dialogue: Any,
        delivery: Any,
        run: Any,
        receiver_work: Any,
    ) -> dict[str, Any] | None:
        """Close a delivered delivery whose receiver Run ended without a reply.

        A provider-time failure persists no assistant message, so no receipt
        can ever be built and the delivery would stay ``delivered`` while the
        projection reports ``receiving_result``.  The terminal Run/Work pair
        was already validated by the caller; this folds that server-observed
        fact into the delivery and Dialogue without replaying the provider,
        materializing a message, or reading ``AgentRun.error``.  A pending
        Human question keeps the Dialogue waiting, and failures the Worker
        materializer owns (route/admission codes) are left to that path.
        """

        run_status = _text(_field(run, "status"), 32).casefold()
        if run_status not in {"failed", "cancelled"} or _text(delivery.state, 32).casefold() != "delivered":
            return None
        worker_delivery = _text(delivery.kind, 32).casefold() in {"request", "control"}
        safe_error_code = _text(
            getattr(receiver_work, "safe_error_code", None) or getattr(receiver_work, "blocker_code", None)
        ).casefold()
        if worker_delivery and safe_error_code.startswith(_SAFE_TERMINAL_FAILURE_PREFIXES):
            return None
        try:
            # The input ledger is optional during rolling upgrades.  Keep a
            # missing-table error inside a SAVEPOINT so PostgreSQL does not
            # leave the outer transaction aborted before the state fold.
            async with session.begin_nested():
                pending_input = (
                    await session.execute(
                        select(AgentDialogueInput.id)
                        .where(
                            AgentDialogueInput.dialogue_id == delivery.dialogue_id,
                            AgentDialogueInput.state == "pending",
                        )
                        .limit(1)
                    )
                ).first()
        except (OperationalError, ProgrammingError) as exc:
            if not _is_missing_dialogue_input_table_error(exc):
                raise
            pending_input = None
        terminal = "cancelled" if run_status == "cancelled" else "failed"
        now = self._now()
        # A handoff may have stored the provider's pre-user trigger in this
        # slot.  A no-receipt close must not leave that row looking like a
        # terminal assistant message or expose its digest to a later retry.
        delivery.receiver_message_id = None
        delivery.response_digest = None
        delivery.state, delivery.updated_at = terminal, now
        current_dialogue_state = _text(_field(dialogue, "state"), 32).casefold()
        if pending_input is not None:
            if current_dialogue_state not in {"cancelled", "expired", "succeeded", "failed"}:
                dialogue.state, dialogue.updated_at = "waiting", now
        elif current_dialogue_state not in {"cancelled", "expired", "succeeded", "failed"}:
            dialogue.state, dialogue.updated_at = terminal, now
        return {
            **self._projection(delivery),
            "dialogue": project_dialogue(dialogue),
            "closed_without_receipt": True,
            "idempotent": False,
        }

    async def record_worker_terminal(
        self,
        *,
        run_id: str | None = None,
        delivery_id: str | None = None,
        data: Mapping[str, Any] | None = None,
        recover_missing_failure: bool = False,
    ) -> dict[str, Any] | None:
        marker = data.get("agent_dialogue") if isinstance(data, Mapping) and isinstance(data.get("agent_dialogue"), Mapping) else {}
        run_id, delivery_id = run_id or _id(marker.get("receiver_run_id")) or _id(marker.get("run_id")), delivery_id or _id(marker.get("delivery_id") or marker.get("agent_dialogue_delivery_id"))
        if not run_id or not delivery_id:
            return None
        session = await self._session()
        try:
            delivery = await session.get(AgentDialogueDelivery, _uuid(delivery_id, "delivery_id"))
            if delivery is None:
                return None
            dialogue = await self._lock_dialogue(session, delivery.dialogue_id)
            run = await session.get(AgentRun, _uuid(run_id, "receiver_run_id"))
            if run is None:
                return None
            await self._validate_delivery_source(session, dialogue, delivery)
            receiver_work = await self._validate_receiver_run(
                session,
                dialogue,
                delivery,
                run,
                strict_delivery=True,
            )
            worker_delivery = _text(delivery.kind, 32).casefold() in {"request", "control"}
            run_metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
            input_pending = _text(run.error).casefold() == "dialogue_input_pending" or run_metadata.get("dialogue_input_pending") is True or bool(run_metadata.get("dialogue_input_id"))
            if input_pending and not worker_delivery:
                logger.warning("root receiver callback cannot create worker input receipt")
                return None
            input_id = _id(run_metadata.get("dialogue_input_id")) if input_pending else None
            message = None
            if input_pending:
                pending_input = None
                if input_id:
                    pending_input = await session.get(AgentDialogueInput, _uuid(input_id, "dialogue_input_id"))
                if pending_input is None:
                    pending_input = (
                        await session.execute(
                            select(AgentDialogueInput)
                            .where(
                                AgentDialogueInput.dialogue_id == delivery.dialogue_id,
                                AgentDialogueInput.source_run_id == run.id,
                                AgentDialogueInput.state == "pending",
                            )
                            .order_by(AgentDialogueInput.revision.desc())
                            .limit(1)
                        )
                    ).scalars().first()
                if pending_input is None or pending_input.source_run_id != run.id or pending_input.source_message_id is None:
                    logger.warning("dialogue input pending receipt has no exact input/message correlation")
                    return None
                if pending_input.source_work_item_id not in (None, run.work_item_id):
                    logger.warning("dialogue input pending receipt Work correlation is stale")
                    return None
                input_id = str(pending_input.id)
                message = await session.get(ConversationMessage, pending_input.source_message_id)
            else:
                # Never select the latest assistant text blindly: a callback
                # for an older/failed Run can otherwise publish another turn's
                # response.  The assistant row must carry this exact Run id.
                messages = (
                    await session.execute(
                        select(ConversationMessage)
                        .where(
                            ConversationMessage.session_id == run.session_id,
                            ConversationMessage.role == "assistant",
                            ConversationMessage.deleted_at.is_(None),
                        )
                        .order_by(ConversationMessage.created_at.desc())
                        .limit(64)
                    )
                ).scalars().all()
                for candidate in messages:
                    metadata = candidate.message_metadata if isinstance(candidate.message_metadata, Mapping) else {}
                    if str(metadata.get("agent_run_id") or "") == str(run.id):
                        message = candidate
                        break
            if message is None and recover_missing_failure and worker_delivery:
                safe_error_code = _text(
                    getattr(receiver_work, "safe_error_code", None)
                    or getattr(receiver_work, "blocker_code", None)
                ).casefold()
                if (
                    _text(run.status).casefold() in {"failed", "cancelled"}
                    and bool(
                        re.fullmatch(r"[a-z][a-z0-9_]{0,95}", safe_error_code)
                        and safe_error_code.startswith(_SAFE_TERMINAL_FAILURE_PREFIXES)
                    )
                ):
                    materialized = await self.dispatch_bridge.materialize_agent_dialogue_terminal_failure(
                        session,
                        dialogue,
                        delivery,
                        run,
                        receiver_work,
                        safe_error_code,
                    )
                    if isinstance(materialized, Mapping):
                        message_id = _uuid(
                            materialized.get("receiver_message_id")
                            or materialized.get("message_id")
                            or materialized.get("id"),
                            "receiver_message_id",
                            required=False,
                        )
                        if message_id is not None:
                            message = await session.get(ConversationMessage, message_id)
            if message is None and recover_missing_failure and not input_pending:
                closed = await self._close_failed_receiver_delivery(
                    session, dialogue, delivery, run, receiver_work
                )
                if closed is not None:
                    await session.commit()
                    return closed
            if message is None:
                logger.warning("dialogue terminal receipt has no exact assistant message correlation")
                return None
            expected_agent = dialogue.worker_agent_id if worker_delivery else dialogue.root_agent_id
            try:
                self._validate_terminal_message(dialogue, run, message, expected_agent)
            except DialogueTransportError:
                logger.warning("dialogue terminal receipt message binding is invalid")
                return None
            value = await self.receipt(
                delivery_id,
                receiver_run_id=run_id,
                receiver_work_item_id=_id(run.work_item_id),
                receiver_message_id=str(message.id),
                response_digest=sha256_digest(getattr(message, "_content", None) or getattr(message, "content", "") or ""),
                state="input_pending" if input_pending else ("succeeded" if _text(run.status).casefold() == "succeeded" else "failed"),
                input_id=input_id,
                session=session,
            )
            await session.commit()
            return value
        except Exception:
            await self._rollback(session)
            logger.warning("dialogue terminal callback was deferred", exc_info=True)
            return None
        finally:
            await self._close(session)

    on_server_callback = record_worker_terminal
    handle_server_callback = record_worker_terminal

    async def handle_recovered_dispatch(self, data: Mapping[str, Any]) -> dict[str, Any] | None:
        marker = data.get("agent_dialogue") or data.get("_dialogue_transport")
        return _safe_refs(marker) if isinstance(marker, Mapping) else None


async def prove_failed_answer_root_recovery(
    session: Any,
    *,
    dialogue: Any,
    input_row: Any,
    answer_message: Any,
    canonical_client_id: str,
) -> str | None:
    """Re-run the exact recovery proof inside a caller-owned transaction."""

    return await AgentDialogueTransport._prove_failed_answer_root_recovery(
        session,
        dialogue=dialogue,
        input_row=input_row,
        answer_message=answer_message,
        canonical_client_id=canonical_client_id,
    )


AgentDialogueTransportService = AgentDialogueTransport
DialogueTransport = AgentDialogueTransport

__all__ = [
    "AgentDialogueTransport", "AgentDialogueTransportService", "DialogueDeliveryRequest", "DialogueDispatchBridgeProtocol",
    "DialogueTransport", "DialogueTransportConflict", "DialogueTransportError", "DialogueTransportUnavailable", "TransportReceipt",
    "EXECUTION_ADAPTER", "SOURCE_TYPE", "request_digest", "sha256_digest",
]
