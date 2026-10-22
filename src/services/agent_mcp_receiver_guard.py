"""Formal Agent MCP receiver with per-call durable authority checks.

This module is the only bundled MCP entry point for the formal Agent
``agent_activity``/``dialogue8`` servers.  The provider process environment is
an untrusted transport envelope: every call re-reads the current Run, Work
lease, session, immutable binding, execution manifest, and Agent authority
before binding AoiTalk ContextVars.  Missing database configuration, stale
leases, foreign proof values, and unknown tools fail closed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any, AsyncIterator, Mapping
from uuid import UUID


FORMAL_AGENT_MCP_SERVERS = frozenset(
    {"agent_activity", "agent_dialogue", "dialogue8", "agent_dialogue8"}
)
FORMAL_AGENT_ACTIVITY_TOOLS = frozenset({"agent_activity"})
FORMAL_AGENT_DIALOGUE_TOOLS = frozenset(
    {
        "list_agent_workers",
        "delegate_agent_work",
        "get_agent_work_status",
        "send_agent_work_message",
        "answer_agent_work_question",
        "escalate_agent_work_question",
        "stop_agent_work",
        "resume_agent_work",
    }
)
FORMAL_AGENT_WORKER_DENIED_TOOLS = frozenset({"stop_agent_work", "resume_agent_work"})
_ACTIVE_RUN_STATES = frozenset(
    {"queued", "running", "waiting", "active", "awaiting_approval", "uncertain"}
)
_FORMAL_ENV = {
    "run_id": "AOITALK_AGENT_RUN_ID",
    "work_item_id": "AOITALK_AGENT_WORK_ITEM_ID",
    "session_id": "AOITALK_AGENT_SESSION_ID",
    "agent_id": "AOITALK_AGENT_ID",
    "agent_revision_id": "AOITALK_AGENT_REVISION_ID",
    "binding_id": "AOITALK_AGENT_BINDING_ID",
    "binding_snapshot_hash": "AOITALK_AGENT_BINDING_SNAPSHOT_HASH",
    "capability_manifest_digest": "AOITALK_AGENT_CAPABILITY_MANIFEST_DIGEST",
    "authority_digest": "AOITALK_AGENT_AUTHORITY_DIGEST",
    "lease_owner": "AOITALK_AGENT_LEASE_OWNER",
    "lease_token": "AOITALK_AGENT_LEASE_TOKEN",
}
_REQUIRED_DB_ENV = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)


class AgentMcpReceiverDenied(PermissionError):
    """The formal MCP call has no current server-side authority."""

    code = "agent_mcp_receiver_denied"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


def _clean(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 512 or "\x00" in text:
        raise AgentMcpReceiverDenied(f"{label} is missing or invalid")
    return text


def _same(left: Any, right: Any) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _uuid_key(value: Any) -> UUID | str:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return str(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif value:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_formal_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for field, env_name in _FORMAL_ENV.items():
        values[field] = _clean(os.environ.get(env_name), env_name)
    # DatabaseManager defaults POSTGRES_PORT to 5432. A formal child must
    # receive an explicit database connection envelope; it may never silently
    # connect using that default or a guessed URL.
    missing_db = [name for name in _REQUIRED_DB_ENV if name not in os.environ]
    if missing_db:
        raise AgentMcpReceiverDenied(
            "formal Agent MCP database connection envelope is incomplete",
            code="agent_mcp_database_env_missing",
        )
    return values


@dataclass(frozen=True, slots=True)
class AgentMcpReceiverContext:
    server_name: str
    tool_name: str
    run_id: str
    work_item_id: str
    session_id: str
    owner_id: str
    agent_id: str
    agent_revision_id: str
    binding_id: str
    binding_snapshot_hash: str
    capability_manifest_digest: str
    authority_digest: str
    lease_owner: str
    lease_token: str
    agent_dialogue_id: str | None
    agent_dialogue_role: str | None
    control_revision: int | None
    binding_snapshot: Mapping[str, Any]
    execution_config: Mapping[str, Any]

    @property
    def worker(self) -> bool:
        return self.agent_dialogue_role == "worker"

    def safe_dict(self) -> dict[str, Any]:
        """Return audit data without the opaque lease token."""

        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "run_id": self.run_id,
            "work_item_id": self.work_item_id,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "binding_id": self.binding_id,
            "binding_snapshot_hash": self.binding_snapshot_hash,
            "capability_manifest_digest": self.capability_manifest_digest,
            "authority_digest": self.authority_digest,
            "lease_owner": self.lease_owner,
            "agent_dialogue_id": self.agent_dialogue_id,
            "agent_dialogue_role": self.agent_dialogue_role,
            "control_revision": self.control_revision,
        }


def _allowed_tool(server_name: str, tool_name: str, role: str | None) -> bool:
    server = str(server_name or "").strip().casefold()
    tool = str(tool_name or "").strip().casefold()
    if server not in FORMAL_AGENT_MCP_SERVERS:
        return False
    if server == "agent_activity":
        return tool in FORMAL_AGENT_ACTIVITY_TOOLS
    if tool not in FORMAL_AGENT_DIALOGUE_TOOLS:
        return False
    return not (str(role or "").casefold() == "worker" and tool in FORMAL_AGENT_WORKER_DENIED_TOOLS)


async def verify_formal_agent_mcp_call(
    server_name: str,
    tool_name: str,
    *,
    db_manager: Any | None = None,
    db_session: Any | None = None,
    authority_resolver: Any | None = None,
    now: datetime | None = None,
) -> AgentMcpReceiverContext:
    """Re-read all durable authority for one formal MCP tool call."""

    normalized_server = str(server_name or "").strip().casefold()
    normalized_tool = str(tool_name or "").strip().casefold()
    if normalized_server not in FORMAL_AGENT_MCP_SERVERS:
        raise AgentMcpReceiverDenied("unapproved formal MCP server", code="agent_mcp_server_denied")
    values = _required_formal_env()

    role_hint = None
    own_db = db_session is None
    db = db_session
    manager = db_manager
    if db is None:
        from ..memory.database import get_database_manager

        manager = manager or get_database_manager()
        raw = manager.get_session()
        db = await raw if inspect.isawaitable(raw) else raw

    async def _get(model: Any, value: str) -> Any:
        getter = getattr(db, "get", None)
        if not callable(getter):
            raise AgentMcpReceiverDenied("formal MCP database reader is unavailable")
        result = getter(model, _uuid_key(value))
        return await result if inspect.isawaitable(result) else result

    try:
        from ..memory.models import (
            Agent,
            AgentExecutionBinding,
            AgentRevision,
            AgentRun,
            AgentWorkItem,
            ConversationSession,
        )

        run = await _get(AgentRun, values["run_id"])
        work = await _get(AgentWorkItem, values["work_item_id"])
        session = await _get(ConversationSession, values["session_id"])
        agent = await _get(Agent, values["agent_id"])
        revision = await _get(AgentRevision, values["agent_revision_id"])
        binding = await _get(AgentExecutionBinding, values["binding_id"])
        if any(item is None for item in (run, work, session, agent, revision, binding)):
            raise AgentMcpReceiverDenied("formal MCP authority row is unavailable")

        for label, observed, expected in (
            ("run.id", run.id, values["run_id"]),
            ("run.session_id", run.session_id, values["session_id"]),
            ("run.agent_id", run.agent_id, values["agent_id"]),
            ("run.agent_revision_id", run.agent_revision_id, values["agent_revision_id"]),
            ("run.work_item_id", run.work_item_id, values["work_item_id"]),
            ("work.id", work.id, values["work_item_id"]),
            ("work.active_agent_run_id", work.active_agent_run_id, values["run_id"]),
            ("work.conversation_session_id", work.conversation_session_id, values["session_id"]),
            ("work.assigned_agent_id", work.assigned_agent_id, values["agent_id"]),
            ("work.agent_revision_id", work.agent_revision_id, values["agent_revision_id"]),
            ("session.id", session.id, values["session_id"]),
            ("session.agent_id", session.agent_id, values["agent_id"]),
            ("agent.id", agent.id, values["agent_id"]),
            ("revision.id", revision.id, values["agent_revision_id"]),
            ("revision.agent_id", revision.agent_id, values["agent_id"]),
            ("binding.id", binding.id, values["binding_id"]),
            ("binding.session_id", binding.session_id, values["session_id"]),
            ("binding.agent_id", binding.agent_id, values["agent_id"]),
            ("binding.agent_revision_id", binding.agent_revision_id, values["agent_revision_id"]),
        ):
            if not _same(observed, expected):
                raise AgentMcpReceiverDenied(f"formal MCP {label} mismatch", code="agent_mcp_binding_changed")
        if getattr(session, "deleted_at", None) is not None:
            raise AgentMcpReceiverDenied("conversation session is deleted", code="agent_mcp_session_stale")
        if not _same(getattr(session, "user_id", None), getattr(run, "user_id", None)):
            raise AgentMcpReceiverDenied("run and session owner differ", code="agent_mcp_owner_changed")
        if str(getattr(run, "status", "") or "").casefold() not in _ACTIVE_RUN_STATES:
            raise AgentMcpReceiverDenied("AgentRun is not active", code="agent_mcp_run_stale")
        if str(getattr(agent, "state", "") or "").casefold() != "active":
            raise AgentMcpReceiverDenied("Agent is not active", code="agent_mcp_agent_stale")
        if str(getattr(work, "state", "") or "").casefold() != "running":
            raise AgentMcpReceiverDenied("Work lease is not running", code="agent_mcp_lease_stale")
        work_pair = (
            str(getattr(work, "source_type", "") or ""),
            str(getattr(work, "execution_adapter", "") or ""),
        )
        if work_pair not in {
            ("chat_turn", "interactive_chat"),
            ("agent_dialogue", "dialogue_transport"),
        }:
            raise AgentMcpReceiverDenied("Work source is not a formal Agent lane", code="agent_mcp_work_denied")
        if not _same(work.lease_owner, values["lease_owner"]) or str(work.lease_token or "") != values["lease_token"]:
            raise AgentMcpReceiverDenied("Work lease owner/token mismatch", code="agent_mcp_lease_foreign")
        expiry = _as_utc(getattr(work, "lease_expires_at", None))
        if expiry is None or expiry <= (now or _now()).astimezone(timezone.utc):
            raise AgentMcpReceiverDenied("Work lease has expired", code="agent_mcp_lease_expired")

        binding_hash = _clean(getattr(binding, "snapshot_hash", None), "binding snapshot hash")
        if binding_hash != values["binding_snapshot_hash"]:
            raise AgentMcpReceiverDenied("Agent binding snapshot mismatch", code="agent_mcp_binding_changed")
        metadata = _mapping(getattr(run, "run_metadata", None))
        metadata_binding = _mapping(metadata.get("agent_binding"))
        if metadata_binding and (
            not _same(metadata_binding.get("binding_id"), values["binding_id"])
            or str(metadata_binding.get("snapshot_hash") or "") != binding_hash
        ):
            raise AgentMcpReceiverDenied("persisted run binding changed", code="agent_mcp_binding_changed")
        manifest = getattr(run, "resolved_execution_manifest", None)
        manifest_hash = _clean(getattr(run, "execution_manifest_hash", None), "execution manifest hash")
        if not isinstance(manifest, Mapping) or _digest(manifest) != manifest_hash:
            raise AgentMcpReceiverDenied("execution manifest is not current", code="agent_mcp_manifest_changed")
        for manifest_key, expected in (
            ("agent_id", values["agent_id"]),
            ("agent_revision_id", values["agent_revision_id"]),
        ):
            if manifest.get(manifest_key) not in (None, "") and not _same(
                manifest.get(manifest_key), expected
            ):
                raise AgentMcpReceiverDenied(
                    f"execution manifest {manifest_key} changed",
                    code="agent_mcp_manifest_changed",
                )

        declared_capabilities = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in (getattr(binding, "capability_ceiling_json", None) or [])
                    if str(item).strip()
                }
            )
        )
        required_work_caps = {
            str(item).strip()
            for item in (getattr(work, "required_capabilities_json", None) or [])
            if str(item).strip()
        }
        if "aoi_tools" not in set(declared_capabilities) or (
            required_work_caps and "aoi_tools" not in required_work_caps
        ):
            raise AgentMcpReceiverDenied("aoi_tools capability is not pinned", code="agent_mcp_capability_denied")

        project_id = getattr(run, "project_id", None) or getattr(work, "project_id", None)
        if authority_resolver is None:
            from .agent_authority import AgentAuthorityResolver

            authority_resolver = AgentAuthorityResolver(db_manager=manager)
        resolve = getattr(authority_resolver, "resolve", None)
        if not callable(resolve):
            raise AgentMcpReceiverDenied("current Agent authority resolver is unavailable")
        read_decision = resolve(
            agent_id=values["agent_id"],
            revision_id=values["agent_revision_id"],
            project_id=project_id,
            required_capability="workspace_read",
        )
        read_decision = await read_decision if inspect.isawaitable(read_decision) else read_decision
        if read_decision is None or not bool(getattr(read_decision, "allowed", False)):
            raise AgentMcpReceiverDenied("current Agent authority denies workspace_read", code="agent_mcp_authority_denied")
        write_decision = resolve(
            agent_id=values["agent_id"],
            revision_id=values["agent_revision_id"],
            project_id=project_id,
            required_capability="workspace_write",
        )
        write_decision = await write_decision if inspect.isawaitable(write_decision) else write_decision
        writable = (
            "workspace_write" in declared_capabilities
            and write_decision is not None
            and bool(getattr(write_decision, "allowed", False))
        )
        authority_caps = tuple(sorted(str(item) for item in (getattr(read_decision, "capabilities", ()) or ())))
        authority_digest = _digest(
            {
                "agent_id": values["agent_id"],
                "agent_revision_id": values["agent_revision_id"],
                "project_id": str(project_id) if project_id else None,
                "capabilities": authority_caps,
                "workspace_write": writable,
            }
        )
        if authority_digest != values["authority_digest"]:
            raise AgentMcpReceiverDenied("current Agent authority digest changed", code="agent_mcp_authority_changed")
        capability_digest = _digest(
            {
                "execution_manifest_hash": manifest_hash.lower(),
                "binding_snapshot_hash": binding_hash,
                "declared_capabilities": declared_capabilities,
                "authority_digest": authority_digest,
            }
        )
        if capability_digest != values["capability_manifest_digest"]:
            raise AgentMcpReceiverDenied("capability manifest digest changed", code="agent_mcp_capability_changed")

        dialogue_marker = _mapping(metadata.get("agent_dialogue"))
        dialogue_id = str(
            dialogue_marker.get("dialogue_id")
            or dialogue_marker.get("agent_dialogue_id")
            or ""
        ).strip() or None
        role = str(dialogue_marker.get("role") or "").strip().casefold() or None
        if not _allowed_tool(normalized_server, normalized_tool, role):
            raise AgentMcpReceiverDenied("formal MCP tool is outside the current receiver", code="agent_mcp_tool_denied")
        try:
            control_revision = int(dialogue_marker.get("control_revision")) if dialogue_marker.get("control_revision") is not None else None
        except (TypeError, ValueError):
            control_revision = None
        binding_snapshot = (
            binding.to_snapshot() if callable(getattr(binding, "to_snapshot", None)) else {}
        )
        execution_config = {
            "scope_kind": "agent_dialogue" if dialogue_id else "aoidots",
            "agent_run_id": values["run_id"],
            "source_agent_run_id": values["run_id"],
            "run_id": values["run_id"],
            "work_item_id": values["work_item_id"],
            "agent_id": values["agent_id"],
            "agent_revision_id": values["agent_revision_id"],
            "binding_id": values["binding_id"],
            "binding_snapshot_hash": binding_hash,
            "capability_manifest_digest": capability_digest,
            "authority_digest": authority_digest,
            **({"agent_dialogue_id": dialogue_id} if dialogue_id else {}),
            **({"dialogue_id": dialogue_id} if dialogue_id else {}),
            **({"role": role} if role else {}),
            **({"control_revision": control_revision} if control_revision is not None else {}),
        }
        return AgentMcpReceiverContext(
            server_name=normalized_server,
            tool_name=normalized_tool,
            run_id=values["run_id"],
            work_item_id=values["work_item_id"],
            session_id=values["session_id"],
            owner_id=_clean(getattr(run, "user_id", None), "run owner"),
            agent_id=values["agent_id"],
            agent_revision_id=values["agent_revision_id"],
            binding_id=values["binding_id"],
            binding_snapshot_hash=binding_hash,
            capability_manifest_digest=capability_digest,
            authority_digest=authority_digest,
            lease_owner=values["lease_owner"],
            lease_token=values["lease_token"],
            agent_dialogue_id=dialogue_id,
            agent_dialogue_role=role,
            control_revision=control_revision,
            binding_snapshot=binding_snapshot,
            execution_config=execution_config,
        )
    finally:
        if own_db and db is not None:
            close = getattr(db, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result


@asynccontextmanager
async def formal_agent_mcp_call_context(
    server_name: str,
    tool_name: str,
    *,
    db_manager: Any | None = None,
    db_session: Any | None = None,
    authority_resolver: Any | None = None,
) -> AsyncIterator[AgentMcpReceiverContext]:
    """Verify one call, bind exact ContextVars, and always reset them."""

    context = await verify_formal_agent_mcp_call(
        server_name,
        tool_name,
        db_manager=db_manager,
        db_session=db_session,
        authority_resolver=authority_resolver,
    )
    from .agent_run_service import reset_current_agent_run_id, set_current_agent_run_id
    from .turn_context import reset_turn_context, set_turn_context

    run_token = set_current_agent_run_id(context.run_id)
    turn_token = set_turn_context(
        user_id=context.owner_id,
        session_id=context.session_id,
        agent_id=context.agent_id,
        agent_revision_id=context.agent_revision_id,
        agent_execution_binding=context.binding_snapshot,
        agent_role=context.agent_dialogue_role or "operator",
        agent_dialogue_id=context.agent_dialogue_id,
        agent_dialogue_role=context.agent_dialogue_role,
        agent_dialogue_control_revision=context.control_revision,
        agent_execution_config=context.execution_config,
    )
    try:
        yield context
    finally:
        reset_turn_context(turn_token)
        reset_current_agent_run_id(run_token)


def build_formal_agent_mcp_server(server_name: str) -> Any:
    """Build one bundled FastMCP server with guarded first-party tools."""

    from mcp.server.fastmcp import FastMCP

    normalized_server = str(server_name or "").strip().casefold()
    if normalized_server not in FORMAL_AGENT_MCP_SERVERS:
        raise ValueError("unknown formal Agent MCP server")
    mcp = FastMCP(f"aoitalk-{normalized_server}")
    from ..tools.agent_activity import build_agent_activity_tool
    from ..tools.agent_dialogue import build_agent_dialogue_tools

    definitions = (
        [build_agent_activity_tool()]
        if normalized_server == "agent_activity"
        else build_agent_dialogue_tools()
    )
    for definition in definitions:
        function = definition.function

        def _factory(tool_name: str, target: Any, description: str):
            @wraps(target)
            async def guarded(*args: Any, **kwargs: Any) -> Any:
                async with formal_agent_mcp_call_context(normalized_server, tool_name):
                    result = target(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result

            mcp.tool(name=tool_name, description=description)(guarded)

        _factory(definition.name, function, definition.description)
    return mcp


def main(server_name: str | None = None) -> None:
    selected = str(server_name or os.environ.get("AOITALK_AGENT_MCP_SERVER") or "").strip().casefold()
    if not selected:
        raise SystemExit("formal Agent MCP server name is required")
    build_formal_agent_mcp_server(selected).run()


__all__ = [
    "AgentMcpReceiverContext",
    "AgentMcpReceiverDenied",
    "FORMAL_AGENT_MCP_SERVERS",
    "build_formal_agent_mcp_server",
    "formal_agent_mcp_call_context",
    "main",
    "verify_formal_agent_mcp_call",
]
