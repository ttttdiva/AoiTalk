"""Admin-scoped command workspace confinement.

The default remains the trusted harness root. An authenticated admin may
persist exemptions for specific active admin users and for specific Agent
revision calls made by those users. A username string is never an exemption.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

UNCONFINED_KEY = "local_agent_tasks.command_workspace_unconfined"
_ABSOLUTE_MESSAGE = "workspace must be an absolute approved directory"
_UNAVAILABLE_MESSAGE = "workspace is unavailable"
_NON_ROOT_DIR_MESSAGE = "workspace must be an existing non-root directory"
_ADMISSION_REPARSE_MESSAGE = "workspace cannot contain a symbolic link or reparse point"
_ALLOWLIST_MESSAGE = "workspace is outside the configured approved roots"


def _uuid_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(UUID(value.strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def _uuid_list(value: Any, *, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of user ids")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        parsed = _uuid_text(item if isinstance(item, str) else "")
        if parsed is None or parsed in seen:
            if parsed in seen:
                continue
            raise ValueError(f"{label} must contain UUID user ids")
        seen.add(parsed)
        result.append(parsed)
    return result


def empty_unconfined_setting() -> dict[str, Any]:
    return {"user_ids": [], "agent_calls": []}


def normalize_unconfined_setting(value: Any) -> dict[str, Any]:
    """Return the canonical exemption document. Unknown fields are rejected."""

    if value is None:
        return empty_unconfined_setting()
    if not isinstance(value, Mapping):
        raise ValueError("command workspace exemption must be an object")
    allowed = {"user_ids", "agent_calls"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("command workspace exemption contains an unknown field")
    user_ids = _uuid_list(value.get("user_ids"), label="user_ids")
    raw_calls = value.get("agent_calls", [])
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, (list, tuple)):
        raise ValueError("agent_calls must be a list")
    calls: list[dict[str, Any]] = []
    seen_calls: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in raw_calls:
        if not isinstance(item, Mapping):
            raise ValueError("agent_calls entries must be objects")
        extra = set(item) - {"agent_id", "revision_id", "caller_user_ids"}
        if extra:
            raise ValueError("agent_calls contains an unknown field")
        agent_id = _uuid_text(item.get("agent_id") if isinstance(item.get("agent_id"), str) else "")
        revision_id = _uuid_text(item.get("revision_id") if isinstance(item.get("revision_id"), str) else "")
        if agent_id is None or revision_id is None:
            raise ValueError("agent_calls require agent_id and revision_id UUIDs")
        callers = _uuid_list(item.get("caller_user_ids"), label="caller_user_ids")
        if not callers:
            raise ValueError("agent_calls require at least one caller user id")
        key = (agent_id, revision_id, tuple(callers))
        if key in seen_calls:
            continue
        seen_calls.add(key)
        calls.append({
            "agent_id": agent_id,
            "revision_id": revision_id,
            "caller_user_ids": callers,
        })
    return {"user_ids": user_ids, "agent_calls": calls}


def stored_unconfined_setting(config: Any) -> dict[str, Any]:
    from .local_agent_tasks import _cfg

    raw = _cfg(config, UNCONFINED_KEY, None)
    if raw in (None, "", {}):
        return empty_unconfined_setting()
    try:
        return normalize_unconfined_setting(raw)
    except ValueError:
        return empty_unconfined_setting()


def _active_admin(owner_role: str, owner_is_active: bool) -> bool:
    return bool(owner_is_active) and str(owner_role or "").strip().lower() == "admin"


def command_workspace_unconfined(
    config: Any,
    *,
    owner_user_id: Any,
    owner_role: str,
    owner_is_active: bool,
    agent_id: Any,
    agent_revision_id: Any,
) -> bool:
    """True only for a DB-active admin selected by stored UUID, never by username."""

    if not _active_admin(owner_role, owner_is_active):
        return False
    owner = _uuid_text(str(owner_user_id) if owner_user_id is not None else "")
    if owner is None:
        return False
    policy = stored_unconfined_setting(config)
    if owner in set(policy["user_ids"]):
        return True
    agent = _uuid_text(str(agent_id) if agent_id is not None else "")
    revision = _uuid_text(str(agent_revision_id) if agent_revision_id is not None else "")
    if agent is None or revision is None:
        return False
    for call in policy["agent_calls"]:
        if call["agent_id"] == agent and call["revision_id"] == revision and owner in set(call["caller_user_ids"]):
            return True
    return False


def inspect_existing_workspace(raw: Any) -> Path:
    """Absolute existing directory that is not a filesystem root or reparse."""

    from .harness_execution_scope_service import _path_has_any_link_or_reparse
    from .local_agent_tasks import LocalAgentTaskError

    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1024 or not Path(raw).is_absolute():
        raise LocalAgentTaskError("invalid_workspace", _ABSOLUTE_MESSAGE, 422)
    try:
        lexical = Path(raw).expanduser().absolute()
        if _path_has_any_link_or_reparse(lexical):
            raise LocalAgentTaskError("workspace_not_allowed", _ADMISSION_REPARSE_MESSAGE, 403)
        resolved = lexical.resolve(strict=True)
    except LocalAgentTaskError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalAgentTaskError("invalid_workspace", _UNAVAILABLE_MESSAGE, 422) from exc
    if not resolved.is_dir() or resolved.parent == resolved:
        raise LocalAgentTaskError("invalid_workspace", _NON_ROOT_DIR_MESSAGE, 422)
    return resolved


def _workspace_in_allowed_roots(config: Any, target: Path) -> bool:
    from .agent_task_invocation import _approved_workspace_roots, _path_within

    return any(_path_within(target, root) for root in _approved_workspace_roots(config))


def evaluate_task_workspace(
    config: Any,
    workspace: str,
    *,
    owner_user_id: Any,
    owner_role: str,
    owner_is_active: bool,
    agent_id: Any,
    agent_revision_id: Any,
    command: bool = False,
) -> Path:
    """Apply allowlist and harness confinement unless this actor is exempt.

    Exempt subjects may use any existing absolute directory. Command containment
    then uses the workspace parent so the scope factory's strict-descendant
    check can succeed without adding that parent to read/write/command roots.
    """

    from .local_agent_task_adapter import command_workspace_root
    from .local_agent_tasks import LocalAgentTaskError

    target = inspect_existing_workspace(workspace)
    if command_workspace_unconfined(
        config,
        owner_user_id=owner_user_id,
        owner_role=owner_role,
        owner_is_active=owner_is_active,
        agent_id=agent_id,
        agent_revision_id=agent_revision_id,
    ):
        return target.parent if command else target
    if not _workspace_in_allowed_roots(config, target):
        raise LocalAgentTaskError("workspace_not_allowed", _ALLOWLIST_MESSAGE, 403)
    if command:
        return command_workspace_root(config, str(target))
    return target


def admit_task_workspace(
    config: Any,
    workspace: str,
    *,
    owner_user_id: Any,
    owner_role: str,
    owner_is_active: bool,
    agent_id: Any,
    agent_revision_id: Any,
) -> Path:
    """Validate a task workspace after owner/agent/revision identity is known."""

    return evaluate_task_workspace(
        config,
        workspace,
        owner_user_id=owner_user_id,
        owner_role=owner_role,
        owner_is_active=owner_is_active,
        agent_id=agent_id,
        agent_revision_id=agent_revision_id,
        command=False,
    )


def resolve_command_workspace_root(
    config: Any,
    workspace: str,
    *,
    owner_user_id: Any,
    owner_role: str,
    owner_is_active: bool,
    agent_id: Any,
    agent_revision_id: Any,
) -> Path:
    """Return the command containment root for this authenticated actor."""

    return evaluate_task_workspace(
        config,
        workspace,
        owner_user_id=owner_user_id,
        owner_role=owner_role,
        owner_is_active=owner_is_active,
        agent_id=agent_id,
        agent_revision_id=agent_revision_id,
        command=True,
    )


async def ensure_unconfined_subjects(value: Mapping[str, Any]) -> None:
    """Reject exemptions that are not active admin users or a real revision pair.

    The lookup reads durable user role and revision ownership. It does not
    accept a display name or a client-supplied role.
    """

    user_ids = set(value.get("user_ids") or [])
    for call in value.get("agent_calls") or []:
        user_ids.update(call.get("caller_user_ids") or [])
    calls = list(value.get("agent_calls") or [])
    if not user_ids and not calls:
        return
    from uuid import UUID as UUIDType

    from ..memory.database import get_database_manager
    from ..memory.models import AgentRevision, User

    db = get_database_manager()
    session = db.get_session()
    if hasattr(session, "__await__"):
        session = await session
    try:
        for user_id in sorted(user_ids):
            user = await session.get(User, UUIDType(user_id))
            if (
                user is None
                or not bool(getattr(user, "is_active", False))
                or str(getattr(user, "role", "") or "").strip().lower() != "admin"
            ):
                raise ValueError("command workspace exemption user must be an active admin")
        for call in calls:
            revision = await session.get(AgentRevision, UUIDType(call["revision_id"]))
            if revision is None or str(revision.agent_id) != call["agent_id"]:
                raise ValueError("command workspace exemption revision does not belong to the agent")
    finally:
        await session.close()
