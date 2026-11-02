"""Explicit owner/admin approvals for bounded Muse work scopes.

Approval rows are private, owner-namespaced AppConfigSetting documents.  This
module never infers approval from a prompt, creates a workspace, or accepts a
command from its caller. Transactions belong to the calling service/route.
"""

from __future__ import annotations

import copy
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from ._muse_pytest_basetemp import PytestBasetempError, resolve_pytest_basetemp
from .muse_authorized_scope import (
    SCOPE_SCHEMA,
    MuseAuthorizedScopeIdentity,
    build_muse_authorized_scope,
)

STORE_SCHEMA = "muse-authorized-scope-approvals-v1"


class MuseAuthorizedScopeStoreError(ValueError):
    def __init__(self, code: str, http_status: int = 409):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class MuseAuthorizedScopeRequiresReapproval(MuseAuthorizedScopeStoreError):
    """A valid legacy writable approval requires a new explicit approval."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def owner_key(owner_id: str) -> str:
    return "muse_scope:" + str(UUID(owner_id))


def fixed_pytest_command(scope_id: str, target: str) -> str:
    """One literal verification command; target is validated as a scope file."""
    UUID(scope_id)
    try:
        basetemp = resolve_pytest_basetemp(
            purpose="approved", identity=scope_id, repository_root=repository_root()
        )
    except PytestBasetempError:
        raise MuseAuthorizedScopeStoreError("muse_scope_basetemp_unavailable", 503) from None
    return subprocess.list2cmdline([
        r".\venv\Scripts\python.exe", "-m", "pytest", "-q", "-p",
        "no:cacheprovider", f"--basetemp={basetemp}", f"./{target}",
    ])


def _legacy_fixed_pytest_command(scope_id: str, target: str) -> str:
    return subprocess.list2cmdline([
        r".\venv\Scripts\python.exe", "-m", "pytest", "-q", "-p",
        "no:cacheprovider", f"--basetemp=.local/pytest-muse-approved-{scope_id}", f"./{target}",
    ])


def _validate_entry(entry: Any, identity: MuseAuthorizedScopeIdentity) -> dict[str, Any]:
    try:
        if not isinstance(entry, Mapping) or entry.get("status") != "active":
            raise ValueError()
        record = entry["record"]
        scope = build_muse_authorized_scope(record, expected_identity=identity)
        UUID(entry["session_id"])
        UUID(entry["binding_id"])
        target = entry.get("pytest_target")
        common_valid = (
            scope.workspace_root == repository_root()
            and scope.digest == entry.get("digest")
            and entry.get("binding_snapshot_hash") == identity.binding_snapshot_hash
            and bool(entry.get("session_id"))
            and bool(entry.get("binding_id"))
        )
        if scope.readonly:
            if not (common_valid and not scope.commands and not scope.editable_paths and target in (None, "")):
                raise ValueError()
        else:
            if not (common_valid and isinstance(target, str) and target in scope.editable_paths
                    and target.endswith(".py")):
                raise ValueError()
            if scope.commands == (_legacy_fixed_pytest_command(scope.scope_id, target),):
                raise MuseAuthorizedScopeRequiresReapproval("muse_scope_requires_reapproval")
            if scope.commands != (fixed_pytest_command(scope.scope_id, target),):
                raise ValueError()
        return copy.deepcopy(record)
    except MuseAuthorizedScopeStoreError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise MuseAuthorizedScopeStoreError("muse_scope_record_invalid") from None


def _record_matches_identity(
    record: Any, identity: MuseAuthorizedScopeIdentity
) -> bool:
    if not isinstance(record, Mapping):
        return False
    if "binding_snapshot_hash" in record:
        return all(
            record.get(key) == value
            for key, value in identity.as_dict().items()
        )
    # Historical records used the AgentRevision UUID and immutable binding
    # hash under names that implied an ExecutionProfile. Accept only the old
    # exact identity where those values equal the current AgentRevision pin.
    return (
        record.get("owner_id") == identity.owner_id
        and record.get("agent_id") == identity.agent_id
        and record.get("agent_revision_id") == identity.agent_revision_id
        and record.get("execution_profile_id") == identity.agent_revision_id
        and record.get("execution_profile_revision") == identity.binding_snapshot_hash
    )


def _records(document: Any) -> list[dict[str, Any]]:
    if document == {}:
        return []
    if (
        not isinstance(document, dict)
        or document.get("schema") != STORE_SCHEMA
        or not isinstance(document.get("records"), list)
        or any(not isinstance(item, dict) for item in document["records"])
    ):
        raise MuseAuthorizedScopeStoreError("muse_scope_store_invalid")
    return document["records"]


class MuseAuthorizedScopeStore:
    async def _row(self, session: Any, owner_id: str, *, lock: bool = False) -> Any:
        from sqlalchemy import select
        from ..memory.models import AppConfigSetting

        key = owner_key(owner_id)
        if lock:
            # Production is PostgreSQL; SQLite is used only by the isolated tests.
            if session.get_bind().dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            elif session.get_bind().dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            else:
                raise MuseAuthorizedScopeStoreError("muse_scope_store_unavailable", 503)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.execute(insert(AppConfigSetting).values(
                key=key, value={}, created_at=now, updated_at=now,
            ).on_conflict_do_nothing(index_elements=["key"]))
        query = select(AppConfigSetting).where(AppConfigSetting.key == key)
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).scalar_one_or_none()

    async def read_active_for_identity(
        self, session: Any, identity: MuseAuthorizedScopeIdentity, *,
        session_id: str, binding_id: str,
    ) -> dict[str, Any] | None:
        """Read only: exact identity and invocation binding, with no fallback."""
        try:
            UUID(session_id)
            UUID(binding_id)
        except (AttributeError, TypeError, ValueError):
            raise MuseAuthorizedScopeStoreError("muse_scope_identifier_invalid", 422) from None
        row = await self._row(session, identity.owner_id)
        entries = _records(row.value if row is not None else {})
        matches = [
            item
            for item in entries
            if item.get("status") == "active"
            and _record_matches_identity(item.get("record"), identity)
        ]
        if len(matches) > 1:
            raise MuseAuthorizedScopeStoreError("muse_scope_approval_ambiguous")
        if not matches:
            return None
        if matches[0].get("session_id") != session_id or matches[0].get("binding_id") != binding_id:
            raise MuseAuthorizedScopeStoreError("muse_scope_approval_binding_mismatch")
        return _validate_entry(matches[0], identity)

    async def approve(
        self, session: Any, *, identity: MuseAuthorizedScopeIdentity,
        session_id: str, binding_id: str, files: Mapping[str, str],
        pytest_target: str | None = None, readonly: bool = False,
    ) -> dict[str, str]:
        """Called only after the route independently proves the owned binding."""
        UUID(session_id)
        UUID(binding_id)
        row = await self._row(session, identity.owner_id, lock=True)
        document = copy.deepcopy(row.value)
        entries = _records(document)
        if len(entries) >= 256:
            raise MuseAuthorizedScopeStoreError("muse_scope_approval_history_full")
        if any(
            item.get("status") == "active"
            and _record_matches_identity(item.get("record"), identity)
            for item in entries
        ):
            raise MuseAuthorizedScopeStoreError("muse_scope_approval_exists")
        if not isinstance(files, Mapping):
            raise MuseAuthorizedScopeStoreError("muse_scope_files_invalid", 422)
        scope_id = str(uuid4())
        if type(readonly) is not bool:
            raise MuseAuthorizedScopeStoreError("muse_scope_readonly_invalid", 422)
        if readonly:
            if pytest_target not in (None, ""):
                raise MuseAuthorizedScopeStoreError("muse_scope_pytest_target_forbidden", 422)
            if not isinstance(files, Mapping) or any(mode != "read" for mode in files.values()):
                raise MuseAuthorizedScopeStoreError("muse_scope_readonly_files_invalid", 422)
            commands: list[str] = []
        else:
            if not isinstance(pytest_target, str) or not pytest_target.strip():
                raise MuseAuthorizedScopeStoreError("muse_scope_pytest_target_required", 422)
            commands = [fixed_pytest_command(scope_id, pytest_target)]
        record = {
            "schema": SCOPE_SCHEMA, "enabled": True, "scope_id": scope_id,
            **identity.as_dict(), "workspace_root": str(repository_root()),
            "files": dict(files), "commands": commands, "readonly": readonly,
        }
        scope = build_muse_authorized_scope(record, expected_identity=identity)
        if not readonly and (
            not isinstance(pytest_target, str)
            or pytest_target not in scope.editable_paths
            or not pytest_target.endswith(".py")
        ):
            raise MuseAuthorizedScopeStoreError("muse_scope_pytest_target_invalid", 422)
        entry = {
            "record": record, "digest": scope.digest, "status": "active",
            "session_id": session_id, "binding_id": binding_id,
            "binding_snapshot_hash": identity.binding_snapshot_hash,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        if not readonly:
            entry["pytest_target"] = pytest_target
        _validate_entry(entry, identity)
        entries.append(entry)
        row.value = {"schema": STORE_SCHEMA, "records": entries}
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.flush()
        return {"scope_id": scope_id, "digest": scope.digest, "status": "active"}

    async def revoke(
        self, session: Any, *, identity: MuseAuthorizedScopeIdentity, scope_id: str,
    ) -> dict[str, str]:
        UUID(scope_id)
        row = await self._row(session, identity.owner_id, lock=True)
        document = copy.deepcopy(row.value)
        entries = _records(document)
        matches = [
            item
            for item in entries
            if isinstance(item.get("record"), dict)
            and item["record"].get("scope_id") == scope_id
            and _record_matches_identity(item.get("record"), identity)
        ]
        if len(matches) != 1:
            raise MuseAuthorizedScopeStoreError("muse_scope_approval_not_found", 404)
        item = matches[0]
        item["status"] = "revoked"
        item["revoked_at"] = datetime.now(timezone.utc).isoformat()
        row.value = {"schema": STORE_SCHEMA, "records": entries}
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.flush()
        return {"scope_id": scope_id, "digest": item["digest"], "status": "revoked"}
