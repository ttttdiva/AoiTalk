"""Durable server transport for finite ``target_command`` App Actions.

This transport is intentionally separate from the PC Bridge adapter.  It
materializes the published Runtime Bundle, resolves one release-pinned command
key, verifies the bundle hash, and then hands the fixed argv to the independent
server supervisor.  It never executes a request command directly in the API
worker and never accepts a workspace path from the client.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from sqlalchemy import select

from ..memory.models import App, AppArtifact, AppRelease, AppTarget, User
from .app_action_run_service import (
    AppActionTransportError,
    canonical_json_hash,
    parse_action_ref,
)
from .app_action_server_journal import (
    MAX_LOG_BYTES,
    ServerActionJournal,
    ServerActionJournalError,
    cleanup_owned_directory,
    cleanup_unjournaled_run,
    owned_directory_identity,
    runtime_tree_sha256,
)
from .app_job_execution_policy import (
    ServerJobExecutionDenied,
    assert_user_may_start_server_job,
)
from .app_job_isolation import AppJobIsolationError, require_isolation_contract
from .app_manifest_service import parse_manifest_text
from .app_action_contract import validate_action_contract
from .app_storage import (
    AppStorageError,
    get_workspaces_root,
    normalize_app_relative_path,
    resolve_app_artifact_file,
    verify_file_integrity,
)
from .app_action_server_supervisor import spawn_supervisor_process


_COMMAND_FORBIDDEN_CHARS = set("\x00\r\n;&|<>`%")
_SAFE_COMMAND_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")
_MAX_BUNDLE_FILES = 10_000
_MAX_BUNDLE_BYTES = 512 * 1024 * 1024
_INPUT_STAGING_MARKER = "owner.json"


def _is_link_or_reparse(path: Path) -> bool:
    """Inspect one storage entry without following a link/reparse point."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _entry_exists(path: Path) -> bool:
    return path.exists() or _is_link_or_reparse(path)


class ServerActionPreparationError(RuntimeError):
    """Release/configuration data cannot produce a safe server run."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class PreparedServerAction:
    runtime_root: Path
    runtime_tree_sha256: str
    command: tuple[str, ...]
    command_sha256: str
    input_path: Path
    output_path: Path
    output_manifest_path: Path
    log_path: Path
    isolation_config: dict[str, Any]
    runtime_identity: tuple[int, int]


@dataclass(frozen=True)
class _InputStagingReservation:
    run_id: str
    run_root: Path
    staging_root: Path
    file_root: Path
    marker_path: Path
    marker_token: str
    owner_user_id: str
    manifest_sha256: str
    owns_staging: bool
    complete: bool
    staging_identity: tuple[int, int] | None
    start_token: str | None = None


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_bytes(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _parse_command(command: str) -> list[str]:
    value = str(command or "").strip()
    if not value:
        raise ServerActionPreparationError("app_action_server_command_missing")
    if any(character in _COMMAND_FORBIDDEN_CHARS for character in value):
        raise ServerActionPreparationError("app_action_server_command_unsafe")
    try:
        argv = shlex.split(value, posix=os.name != "nt")
    except ValueError as exc:
        raise ServerActionPreparationError("app_action_server_command_invalid") from exc
    if os.name == "nt":
        argv = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}
            else token
            for token in argv
        ]
    if not argv or any(not isinstance(token, str) or not token for token in argv):
        raise ServerActionPreparationError("app_action_server_command_invalid")
    return argv


def _under(root: Path, candidate: Path) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
        return resolved
    except (OSError, ValueError) as exc:
        raise ServerActionPreparationError("app_action_server_path_outside_runtime") from exc


def _resolve_command_paths(argv: list[str], runtime_root: Path) -> list[str]:
    """Resolve script/path tokens against the immutable runtime directory."""

    result: list[str] = []
    for index, token in enumerate(argv):
        option_token = token.startswith("-")
        path_like = (
            token.startswith((".", "/", "\\"))
            or (not option_token and ("/" in token or "\\" in token))
            or (
                not option_token
                and Path(token).suffix.lower()
                in {".py", ".ps1", ".bat", ".cmd", ".sh", ".js", ".mjs", ".exe"}
            )
        )
        if not path_like:
            result.append(token)
            continue
        candidate = Path(token)
        if index == 0 and (candidate.is_absolute() or (len(token) > 1 and token[1] == ":")):
            # A release-pinned command may select the host interpreter (for
            # example the configured Python executable).  The script and all
            # relative assets remain rooted in the immutable bundle.
            if not candidate.is_file():
                raise ServerActionPreparationError("app_action_server_command_file_missing")
            result.append(str(candidate.resolve()))
            continue
        if candidate.is_absolute() or (len(token) > 1 and token[1] == ":"):
            path = candidate
        else:
            path = runtime_root / candidate
        resolved = _under(runtime_root, path)
        if not resolved.is_file():
            raise ServerActionPreparationError("app_action_server_command_file_missing")
        # The command itself is pinned to the bundle.  Absolute runtime paths
        # are normalized so a later cwd change cannot alter the target.
        result.append(str(resolved))
    return result


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    return bool(mode and (mode & 0o170000) == 0o120000)


def _extract_runtime_bundle(
    *, artifact_path: Path, runtime_root: Path, expected_sha256: str, expected_size_bytes: int
) -> tuple[int, int]:
    verify_file_integrity(
        artifact_path,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
    )
    if _is_link_or_reparse(runtime_root) or runtime_root.exists():
        # A retry must first prove that the prior attempt's exact transient
        # tree is removable.  Do not replace an existing directory here: a
        # collision may belong to an active/unknown run or another owner.
        raise ServerActionPreparationError("app_action_server_runtime_collision")
    try:
        runtime_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ServerActionPreparationError("app_action_server_runtime_collision") from exc
    runtime_identity = owned_directory_identity(runtime_root)
    if runtime_identity is None:
        raise ServerActionPreparationError("app_action_server_runtime_unavailable")
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_BUNDLE_FILES:
                raise ServerActionPreparationError("app_action_server_runtime_too_many_files")
            for info in members:
                try:
                    relative = normalize_app_relative_path(info.filename)
                except AppStorageError as exc:
                    raise ServerActionPreparationError("app_action_server_runtime_path_invalid") from exc
                key = relative.casefold()
                if key in seen:
                    raise ServerActionPreparationError("app_action_server_runtime_duplicate_path")
                seen.add(key)
                if _is_zip_symlink(info):
                    raise ServerActionPreparationError("app_action_server_runtime_link_invalid")
                total += int(info.file_size or 0)
                if total > _MAX_BUNDLE_BYTES:
                    raise ServerActionPreparationError("app_action_server_runtime_too_large")
                destination = (runtime_root / Path(relative)).resolve()
                try:
                    destination.relative_to(runtime_root.resolve())
                except ValueError as exc:
                    raise ServerActionPreparationError("app_action_server_runtime_path_invalid") from exc
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
    except ServerActionPreparationError:
        cleanup_owned_directory(runtime_root, runtime_identity)
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        cleanup_owned_directory(runtime_root, runtime_identity)
        raise ServerActionPreparationError("app_action_server_runtime_invalid") from exc
    except BaseException:
        cleanup_owned_directory(runtime_root, runtime_identity)
        raise
    return runtime_identity


def _read_runtime_manifest(runtime_root: Path, release: AppRelease) -> dict[str, Any]:
    manifest_path = runtime_root / "aoitalk.app.yaml"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ServerActionPreparationError("app_action_server_manifest_missing") from exc
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest.casefold() != str(release.manifest_hash or "").casefold():
        raise ServerActionPreparationError("app_action_server_manifest_hash_mismatch")
    try:
        return parse_manifest_text(text)
    except Exception as exc:
        raise ServerActionPreparationError("app_action_server_manifest_invalid") from exc


def _target_command_from_manifest(
    manifest: Mapping[str, Any], *, action_key: str, target_key: str, execution_target_id: str, mode: str
) -> list[str]:
    targets = manifest.get("targets")
    actions = manifest.get("actions")
    if not isinstance(targets, Mapping) or not isinstance(actions, Mapping):
        raise ServerActionPreparationError("app_action_server_action_missing")
    target = targets.get(target_key)
    action = actions.get(action_key)
    if not isinstance(target, Mapping) or not isinstance(action, Mapping):
        raise ServerActionPreparationError("app_action_server_action_missing")
    if str(action.get("execution_target_id") or "") != str(execution_target_id):
        raise ServerActionPreparationError("app_action_server_target_contract_mismatch")
    execution = action.get("execution")
    if not isinstance(execution, Mapping) or execution.get("kind") != "target_command":
        raise ServerActionPreparationError("app_action_server_kind_unsupported")
    if str(execution.get("mode") or "") != str(mode):
        raise ServerActionPreparationError("app_action_server_mode_mismatch")
    if mode != "finite":
        raise ServerActionPreparationError("app_action_server_mode_unsupported")
    command_key = str(execution.get("command_key") or "")
    if not _SAFE_COMMAND_KEY.fullmatch(command_key):
        raise ServerActionPreparationError("app_action_server_command_key_invalid")
    raw = target.get(command_key)
    if isinstance(raw, Mapping):
        raw = raw.get("command")
    if not isinstance(raw, str) or not raw.strip():
        raise ServerActionPreparationError("app_action_server_command_missing")
    return _parse_command(raw)


def _hydrate_action_schemas(action: Mapping[str, Any], runtime_root: Path) -> dict[str, Any]:
    result = copy.deepcopy(dict(action))
    for key in ("input_schema", "output_schema"):
        value = result.get(key)
        if not isinstance(value, str):
            continue
        try:
            path = _under(runtime_root, runtime_root / Path(normalize_app_relative_path(value)))
            result[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ServerActionPreparationError("app_action_server_schema_reference_invalid") from exc
    return result


def _safe_action_definition(action: Mapping[str, Any], runtime_root: Path) -> dict[str, Any]:
    execution = action.get("execution")
    safe: dict[str, Any] = {
        "label": action.get("label"),
        "description": action.get("description"),
        "target_key": action.get("target_key"),
        "input_schema": _hydrate_action_schemas({"input_schema": action.get("input_schema")}, runtime_root).get("input_schema") or {},
        "output_schema": _hydrate_action_schemas({"output_schema": action.get("output_schema")}, runtime_root).get("output_schema") or {},
        "execution_target_id": action.get("execution_target_id"),
        "capabilities": copy.deepcopy(action.get("capabilities") or []),
    }
    if isinstance(execution, Mapping):
        safe["execution"] = {
            key: copy.deepcopy(execution[key])
            for key in ("kind", "mode", "command_key", "saved_action_id", "sha256")
            + ("contract_hash",)
            if key in execution
        }
    return safe


def _verify_release_contract_hashes(
    manifest: Mapping[str, Any],
    runtime_root: Path,
    target_model: AppTarget,
    *,
    action_key: str,
    target_key: str,
    expected_action_spec_hash: str,
    expected_target_contract_hash: str,
    runtime_bundle_sha256: str | None = None,
    runtime_bundle_size_bytes: int | None = None,
) -> None:
    contract = validate_action_contract(manifest, strict=False)
    if not contract.valid or action_key not in contract.actions:
        raise ServerActionPreparationError("app_action_server_action_contract_invalid")
    action = _safe_action_definition(contract.actions[action_key], runtime_root)
    target_snapshot = manifest.get("targets", {}).get(target_key, {}) if isinstance(manifest.get("targets"), Mapping) else {}
    if not isinstance(target_snapshot, Mapping):
        target_snapshot = {}
    action["capabilities"] = [
        str(item)
        for item in (target_snapshot.get("capabilities") or [])
        if isinstance(item, str) and item.strip()
    ]
    target = {
        "id": str(target_model.id),
        "target_key": target_key,
        "display_name": target_snapshot.get("display_name") or target_key,
        "surface": target_snapshot.get("surface") or getattr(target_model, "surface", None) or "headless",
        "runtime": target_snapshot.get("runtime") or getattr(target_model, "runtime", None) or "executable",
        "execution_host": target_snapshot.get("execution_host") or getattr(target_model, "execution_host", None) or "download_only",
        "entrypoint": target_snapshot.get("entrypoint") or getattr(target_model, "entrypoint", None) or "",
    }
    if runtime_bundle_sha256:
        target["runtime_bundle_sha256"] = str(runtime_bundle_sha256)
    if runtime_bundle_size_bytes is not None:
        target["runtime_bundle_size_bytes"] = int(runtime_bundle_size_bytes)
    immutable_target = {key: value for key, value in target.items() if key != "id"}
    action_hash = hashlib.sha256(_canonical(action)).hexdigest()
    target_hash = hashlib.sha256(_canonical({"target": immutable_target, "action": action})).hexdigest()
    if action_hash.casefold() != str(expected_action_spec_hash or "").casefold():
        raise ServerActionPreparationError("app_action_server_action_spec_hash_mismatch")
    if target_hash.casefold() != str(expected_target_contract_hash or "").casefold():
        raise ServerActionPreparationError("app_action_server_target_contract_hash_mismatch")


class ServerAppActionTransport:
    """App Action transport backed by the local isolated supervisor."""

    def __init__(
        self,
        db_manager: Any,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        deployment_config: Mapping[str, Any] | None = None,
        supervisor_factory: Any | None = None,
    ) -> None:
        self.db_manager = db_manager
        self.workspace_root = workspace_root
        # Keep the composition-root Mapping live.  Config.set/save may replace
        # the entire ``apps`` branch while preserving the top-level object;
        # a shallow copy here would let already queued Runs use revoked target
        # or policy state until the process restarted.
        self.deployment_config = deployment_config if isinstance(deployment_config, Mapping) else {}
        self.supervisor_factory = supervisor_factory or spawn_supervisor_process
        action_root = get_workspaces_root(workspace_root) / "_app_action_runs"
        if _is_link_or_reparse(action_root):
            raise ServerActionPreparationError("app_action_server_storage_link_invalid")
        self._journal_path = action_root / "server-actions.sqlite3"
        self._connection_epoch = self._new_connection_epoch()

    @staticmethod
    def _new_connection_epoch() -> str:
        try:
            from pc_bridge.action_identity import boot_id

            boot = boot_id()
        except Exception:
            boot = f"process-{os.getpid()}"
        return f"server-{boot}-{os.getpid()}"[:128]

    def _ensure_journal_namespace(self) -> None:
        namespace = self._journal_path.parent
        if _is_link_or_reparse(namespace):
            raise ServerActionPreparationError("app_action_server_storage_link_invalid")
        namespace.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(namespace):
            raise ServerActionPreparationError("app_action_server_storage_link_invalid")

    def _journal(self, *, create: bool = False) -> ServerActionJournal | None:
        """Open the durable journal; read probes never create its namespace."""

        if create:
            self._ensure_journal_namespace()
        elif _is_link_or_reparse(self._journal_path):
            raise ServerActionPreparationError("app_action_server_journal_link_invalid")
        elif not self._journal_path.is_file():
            return None
        return ServerActionJournal(self._journal_path)

    def _execution_targets(self) -> Mapping[str, Any]:
        config: Any = self.deployment_config
        if not isinstance(config, Mapping):
            return {}
        apps = config.get("apps", config)
        if not isinstance(apps, Mapping):
            return {}
        actions = apps.get("actions", apps)
        if not isinstance(actions, Mapping):
            return {}
        targets = actions.get("execution_targets", apps.get("execution_targets", config.get("execution_targets", {})))
        return targets if isinstance(targets, Mapping) else {}

    def _target_config(self, execution_target_id: str) -> Mapping[str, Any] | None:
        target = self._execution_targets().get(str(execution_target_id))
        return target if isinstance(target, Mapping) else None

    def _isolation_snapshot(self) -> dict[str, Any]:
        jobs: Mapping[str, Any] = {}
        apps = self.deployment_config.get("apps") if isinstance(self.deployment_config, Mapping) else None
        if isinstance(apps, Mapping) and isinstance(apps.get("jobs"), Mapping):
            jobs = apps["jobs"]
        isolation = jobs.get("isolation") if isinstance(jobs.get("isolation"), Mapping) else {}
        return {
            "apps": {
                "jobs": {
                    "server_execution_enabled": bool(jobs.get("server_execution_enabled", False)),
                    "require_system_admin": bool(jobs.get("require_system_admin", True)),
                    "isolation": dict(isolation),
                }
            }
        }

    def _check_target(
        self,
        *,
        execution_target_id: str,
        owner_user_id: str,
        device_id: str,
        machine_id: str,
        expected_epoch: str | None = None,
        app_id: str | None = None,
    ) -> tuple[Mapping[str, Any], str]:
        target = self._target_config(execution_target_id)
        if target is None or target.get("enabled") is False:
            raise ServerActionPreparationError("app_action_server_target_unavailable")
        if target.get("owner_user_id") is not None and str(target.get("owner_user_id")) != str(owner_user_id):
            raise ServerActionPreparationError("app_action_server_target_owner_mismatch")
        if app_id is not None:
            allowed_apps = target.get("allowed_app_ids") or target.get("app_ids") or []
            if allowed_apps and str(app_id) not in {str(item) for item in allowed_apps}:
                raise ServerActionPreparationError("app_action_server_target_app_forbidden")
        for field, supplied in (("device_id", device_id), ("machine_id", machine_id)):
            configured = str(target.get(field) or "").strip()
            if configured and configured != str(supplied):
                raise ServerActionPreparationError("app_action_server_target_identity_mismatch")
        epoch = str(target.get("epoch") or target.get("connection_epoch") or self._connection_epoch)
        if expected_epoch and str(expected_epoch) != epoch:
            raise ServerActionPreparationError("app_action_server_connection_epoch_changed")
        return target, epoch

    async def _owner_role(self, owner_user_id: str) -> str | None:
        session = await self.db_manager.get_session()
        try:
            user = await session.scalar(select(User).where(User.id == UUID(str(owner_user_id))).limit(1))
            return getattr(user, "role", None) if user is not None else None
        finally:
            await session.close()

    async def availability(
        self,
        *,
        owner_user_id: str,
        device_id: str,
        machine_id: str,
        execution_target_id: str | None = None,
        execution_mode: str | None = None,
        actor_user_id: str | None = None,
        actor_user_role: str | None = None,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        if str(execution_mode or "finite") != "finite":
            return {"online": False, "error_code": "app_action_server_mode_unsupported"}
        try:
            policy_config = self.deployment_config
            role = actor_user_role or await self._owner_role(actor_user_id or owner_user_id)
            assert_user_may_start_server_job(user_role=role, config=policy_config)
            require_isolation_contract(policy_config)
            _target, epoch = self._check_target(
                execution_target_id=str(execution_target_id or ""),
                owner_user_id=str(owner_user_id),
                device_id=str(device_id),
                machine_id=str(machine_id),
            )
            return {
                "online": True,
                "machine_id": str(machine_id),
                "device_id": str(device_id),
                "connection_epoch": epoch,
            }
        except (ServerJobExecutionDenied, AppJobIsolationError, ServerActionPreparationError) as exc:
            return {"online": False, "error_code": getattr(exc, "code", "app_action_server_unavailable")}

    async def _prepare(
        self,
        payload: Mapping[str, Any],
        *,
        owner_user_id: str,
        actor_user_id: str | None = None,
        device_id: str,
        machine_id: str,
        expected_epoch: str | None,
    ) -> PreparedServerAction:
        try:
            app_id = UUID(str(payload.get("app_id")))
            release_id = UUID(str(payload.get("release_id")))
            run_id = UUID(str(payload.get("run_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ServerActionPreparationError("app_action_server_identity_invalid") from exc
        action_ref = str(payload.get("action_ref") or "")
        _release, action_key = parse_action_ref(action_ref, expected_release_id=release_id)
        if action_key != str(payload.get("action_key") or ""):
            raise ServerActionPreparationError("app_action_server_action_mismatch")
        target_key = str(payload.get("target_key") or "")
        execution_target_id = str(payload.get("execution_target_id") or "")
        mode = str(payload.get("execution_mode") or "")
        if mode != "finite":
            raise ServerActionPreparationError("app_action_server_mode_unsupported")
        _target_config, epoch = self._check_target(
            execution_target_id=execution_target_id,
            owner_user_id=str(owner_user_id),
            device_id=str(device_id),
            machine_id=str(machine_id),
            expected_epoch=expected_epoch,
            app_id=str(app_id),
        )
        role = await self._owner_role(str(actor_user_id or owner_user_id))
        assert_user_may_start_server_job(user_role=role, config=self.deployment_config)
        isolation_config = self._isolation_snapshot()
        require_isolation_contract(isolation_config)
        session = await self.db_manager.get_session()
        try:
            app = await session.scalar(select(App).where(App.id == app_id).limit(1))
            release = await session.scalar(
                select(AppRelease).where(
                    AppRelease.id == release_id,
                    AppRelease.app_id == app_id,
                    AppRelease.status == "published",
                ).limit(1)
            )
            target = await session.scalar(
                select(AppTarget).where(
                    AppTarget.app_id == app_id,
                    AppTarget.target_key == target_key,
                ).limit(1)
            )
            artifact = await session.scalar(
                select(AppArtifact).where(
                    AppArtifact.release_id == release_id,
                    AppArtifact.target_id == (target.id if target is not None else None),
                    AppArtifact.artifact_type == "runtime_bundle",
                ).limit(1)
            ) if target is not None else None
            if app is None or release is None or target is None or artifact is None:
                raise ServerActionPreparationError("app_action_server_release_unavailable")
            if str(getattr(app, "owner_user_id", "")) != str(owner_user_id):
                raise ServerActionPreparationError("app_action_server_owner_mismatch")
            artifact_name = Path(str(artifact.filename or "")).name
            artifact_path = resolve_app_artifact_file(
                app_id, release_id, artifact_name, workspace_root=self.workspace_root
            )
            artifact_sha = str(artifact.sha256 or "").lower()
            artifact_size = int(artifact.size_bytes or 0)
        except (AppStorageError, OSError, ValueError) as exc:
            raise ServerActionPreparationError("app_action_server_release_unavailable") from exc
        finally:
            await session.close()
        root = self._run_root(self.workspace_root, run_id)
        runtime_root = root / "runtime"
        extract_task = asyncio.create_task(
            asyncio.to_thread(
                _extract_runtime_bundle,
                artifact_path=artifact_path,
                runtime_root=runtime_root,
                expected_sha256=artifact_sha,
                expected_size_bytes=artifact_size,
            )
        )
        runtime_identity: tuple[int, int] | None = None
        try:
            runtime_identity = await asyncio.shield(extract_task)
            manifest = _read_runtime_manifest(runtime_root, release)
            _verify_release_contract_hashes(
                manifest,
                runtime_root,
                target,
                action_key=action_key,
                target_key=target_key,
                expected_action_spec_hash=str(payload.get("action_spec_hash") or ""),
                expected_target_contract_hash=str(payload.get("contract_hash") or ""),
                runtime_bundle_sha256=artifact_sha,
                runtime_bundle_size_bytes=artifact_size,
            )
            command = _target_command_from_manifest(
                manifest,
                action_key=action_key,
                target_key=target_key,
                execution_target_id=execution_target_id,
                mode=mode,
            )
            command = _resolve_command_paths(command, runtime_root)
            input_path = root / "input.json"
            output_path = root / "result.json"
            output_manifest_path = root / "output-manifest.json"
            log_path = root / "run.log"
            return PreparedServerAction(
                runtime_root=runtime_root,
                runtime_tree_sha256=runtime_tree_sha256(runtime_root),
                command=tuple(command),
                command_sha256=_sha256_bytes(command),
                input_path=input_path,
                output_path=output_path,
                output_manifest_path=output_manifest_path,
                log_path=log_path,
                isolation_config=isolation_config,
                runtime_identity=runtime_identity,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(extract_task)
            except BaseException:
                pass
            if runtime_identity is None and extract_task.done() and not extract_task.cancelled():
                try:
                    runtime_identity = extract_task.result()
                except BaseException:
                    runtime_identity = None
            if runtime_identity is not None:
                cleanup_owned_directory(runtime_root, runtime_identity)
            raise
        except BaseException:
            if runtime_identity is not None:
                cleanup_owned_directory(runtime_root, runtime_identity)
            raise

    @staticmethod
    def _run_root(workspace_root: str | os.PathLike[str] | None, run_id: str | UUID) -> Path:
        root = get_workspaces_root(workspace_root)
        namespace = root / "_app_action_runs"
        if _is_link_or_reparse(namespace):
            raise ServerActionPreparationError("app_action_server_storage_link_invalid")
        run_root = namespace / f"run_{UUID(str(run_id))}"
        if _is_link_or_reparse(run_root):
            raise ServerActionPreparationError("app_action_server_run_link_invalid")
        return run_root

    def _cleanup_unjournaled(self, run_id: str | UUID, *, remove_dirs: tuple[str, ...]) -> None:
        """Best-effort cleanup for the current pre-journal write boundary."""

        try:
            cleanup_unjournaled_run(self._journal_path, run_id, remove_dirs=remove_dirs)
        except Exception:
            # Cleanup is fail-closed.  The original operation's error remains
            # the caller-visible result and an ambiguous tree is left for the
            # ownership-aware workspace inventory.
            return

    async def stage_inputs(
        self,
        *,
        run_id: str,
        manifest: Mapping[str, Any],
        chunks: Any,
        **_kwargs: Any,
    ) -> None:
        try:
            await self._stage_inputs_impl(
                run_id=run_id,
                manifest=manifest,
                chunks=chunks,
                context=_kwargs,
            )
        except AppActionTransportError:
            raise
        except ServerActionPreparationError as exc:
            raise AppActionTransportError(code=exc.code, definitely_not_sent=True) from exc
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise AppActionTransportError(
                code="app_action_input_unavailable", definitely_not_sent=True
            ) from exc

    @staticmethod
    def _input_staging_fingerprint(
        run_id: str,
        files: list[Any],
        context: Mapping[str, Any],
    ) -> dict[str, str]:
        payload = context.get("payload") if isinstance(context.get("payload"), Mapping) else {}
        return {
            "run_id": str(run_id),
            "owner_user_id": str(context.get("owner_user_id") or ""),
            "app_id": str(context.get("app_id") or payload.get("app_id") or ""),
            "action_ref": str(context.get("action_ref") or payload.get("action_ref") or ""),
            "manifest_sha256": hashlib.sha256(_canonical(files)).hexdigest(),
        }

    @staticmethod
    def _validate_staged_input_files(file_root: Path, files: list[Any]) -> None:
        expected = {
            str(item.get("file_id")): item
            for item in files
            if isinstance(item, Mapping) and item.get("file_id")
        }
        for file_id, item in expected.items():
            if not re.fullmatch(r"af_[A-Za-z0-9_-]{32}", file_id):
                raise AppActionTransportError(code="app_action_input_file_id_invalid", definitely_not_sent=True)
            path = file_root / f"{file_id}.bin"
            if not path.is_file() or _is_link_or_reparse(path):
                raise AppActionTransportError(code="app_action_input_missing", definitely_not_sent=True)
            if path.stat().st_size != int(item.get("size_bytes") or -1):
                raise AppActionTransportError(code="app_action_input_size_mismatch", definitely_not_sent=True)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest.casefold() != str(item.get("sha256") or "").casefold():
                raise AppActionTransportError(code="app_action_input_hash_mismatch", definitely_not_sent=True)

    def _claim_input_staging(
        self,
        run_id: str,
        files: list[Any],
        context: Mapping[str, Any],
    ) -> _InputStagingReservation:
        try:
            root = self._run_root(self.workspace_root, run_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise AppActionTransportError(code="app_action_input_run_invalid", definitely_not_sent=True) from exc
        staging = root / "input-staging"
        file_root = staging / "files"
        marker_path = staging / _INPUT_STAGING_MARKER
        if _is_link_or_reparse(staging) or _is_link_or_reparse(file_root) or _is_link_or_reparse(marker_path):
            raise AppActionTransportError(code="app_action_input_path_invalid", definitely_not_sent=True)
        fingerprint = self._input_staging_fingerprint(run_id, files, context)
        if _entry_exists(staging):
            if not staging.is_dir() or not _entry_exists(marker_path) or not marker_path.is_file():
                raise ServerActionPreparationError("app_action_input_staging_collision")
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, TypeError, ValueError) as exc:
                raise ServerActionPreparationError("app_action_input_staging_collision") from exc
            if not isinstance(marker, Mapping) or any(marker.get(key) != value for key, value in fingerprint.items()):
                raise ServerActionPreparationError("app_action_input_staging_owner_mismatch")
            raw_identity = marker.get("staging_identity")
            try:
                marker_identity = (int(raw_identity[0]), int(raw_identity[1]))
            except (IndexError, TypeError, ValueError) as exc:
                raise ServerActionPreparationError("app_action_input_staging_collision") from exc
            if owned_directory_identity(staging) != marker_identity:
                raise ServerActionPreparationError("app_action_input_staging_collision")
            status = str(marker.get("status") or "")
            if status == "writing":
                raise ServerActionPreparationError("app_action_input_staging_busy")
            if status != "complete":
                raise ServerActionPreparationError("app_action_input_staging_collision")
            self._validate_staged_input_files(file_root, files)
            return _InputStagingReservation(
                run_id=str(run_id),
                run_root=root,
                staging_root=staging,
                file_root=file_root,
                marker_path=marker_path,
                marker_token=str(marker.get("token") or ""),
                owner_user_id=fingerprint["owner_user_id"],
                manifest_sha256=fingerprint["manifest_sha256"],
                owns_staging=False,
                complete=True,
                staging_identity=marker_identity,
            )
        try:
            staging.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ServerActionPreparationError("app_action_input_staging_busy") from exc
        staging_identity = owned_directory_identity(staging)
        if staging_identity is None:
            raise ServerActionPreparationError("app_action_input_staging_unavailable")
        marker_token = uuid4().hex
        marker = {
            **fingerprint,
            "status": "writing",
            "token": marker_token,
            "staging_identity": list(staging_identity),
        }
        try:
            with marker_path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(marker, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            file_root.mkdir(parents=False, exist_ok=False)
        except BaseException:
            cleanup_owned_directory(staging, staging_identity)
            raise
        return _InputStagingReservation(
            run_id=str(run_id),
            run_root=root,
            staging_root=staging,
            file_root=file_root,
            marker_path=marker_path,
            marker_token=marker_token,
            owner_user_id=fingerprint["owner_user_id"],
            manifest_sha256=fingerprint["manifest_sha256"],
            owns_staging=True,
            complete=False,
            staging_identity=staging_identity,
        )

    def _cleanup_staging_reservation(self, reservation: _InputStagingReservation) -> None:
        if not reservation.owns_staging or reservation.staging_identity is None:
            return
        try:
            marker = json.loads(reservation.marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError):
            return
        if not isinstance(marker, Mapping) or str(marker.get("token") or "") != reservation.marker_token:
            return
        raw_identity = marker.get("staging_identity")
        try:
            marker_identity = (int(raw_identity[0]), int(raw_identity[1]))
        except (IndexError, TypeError, ValueError):
            return
        if marker_identity != reservation.staging_identity:
            return
        if cleanup_owned_directory(reservation.staging_root, reservation.staging_identity):
            self._cleanup_unjournaled(reservation.run_id, remove_dirs=())

    def _claim_staging_for_start(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        owner_user_id: str,
    ) -> _InputStagingReservation | None:
        files = payload.get("input_files")
        if not isinstance(files, list) or not files:
            return None
        root = self._run_root(self.workspace_root, run_id)
        staging = root / "input-staging"
        marker_path = staging / _INPUT_STAGING_MARKER
        file_root = staging / "files"
        if not _entry_exists(staging):
            return None
        if (
            _is_link_or_reparse(staging)
            or _is_link_or_reparse(marker_path)
            or _is_link_or_reparse(file_root)
            or not marker_path.is_file()
        ):
            raise ServerActionPreparationError("app_action_input_staging_collision")
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise ServerActionPreparationError("app_action_input_staging_collision") from exc
        fingerprint = self._input_staging_fingerprint(
            run_id,
            files,
            {
                "owner_user_id": owner_user_id,
                "app_id": payload.get("app_id"),
                "action_ref": payload.get("action_ref"),
            },
        )
        if not isinstance(marker, Mapping) or any(marker.get(key) != value for key, value in fingerprint.items()):
            raise ServerActionPreparationError("app_action_input_staging_owner_mismatch")
        if str(marker.get("status") or "") != "complete":
            raise ServerActionPreparationError("app_action_input_staging_busy")
        raw_identity = marker.get("staging_identity")
        try:
            staging_identity = (int(raw_identity[0]), int(raw_identity[1]))
        except (IndexError, TypeError, ValueError) as exc:
            raise ServerActionPreparationError("app_action_input_staging_collision") from exc
        if owned_directory_identity(staging) != staging_identity:
            raise ServerActionPreparationError("app_action_input_staging_collision")
        start_token = uuid4().hex
        updated = dict(marker)
        updated["status"] = "starting"
        updated["start_token"] = start_token
        marker_path.write_text(
            json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        return _InputStagingReservation(
            run_id=str(run_id),
            run_root=root,
            staging_root=staging,
            file_root=file_root,
            marker_path=marker_path,
            marker_token=str(marker.get("token") or ""),
            owner_user_id=fingerprint["owner_user_id"],
            manifest_sha256=fingerprint["manifest_sha256"],
            owns_staging=False,
            complete=True,
            staging_identity=staging_identity,
            start_token=start_token,
        )

    def _cleanup_start_staging(self, reservation: _InputStagingReservation | None) -> None:
        if reservation is None or reservation.start_token is None or reservation.staging_identity is None:
            return
        try:
            marker = json.loads(reservation.marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError):
            return
        if not isinstance(marker, Mapping) or str(marker.get("start_token") or "") != reservation.start_token:
            return
        raw_identity = marker.get("staging_identity")
        try:
            marker_identity = (int(raw_identity[0]), int(raw_identity[1]))
        except (IndexError, TypeError, ValueError):
            return
        if marker_identity != reservation.staging_identity:
            return
        if cleanup_owned_directory(reservation.staging_root, reservation.staging_identity):
            self._cleanup_unjournaled(reservation.run_id, remove_dirs=())

    async def _stage_inputs_impl(
        self,
        *,
        run_id: str,
        manifest: Mapping[str, Any],
        chunks: Any,
        context: Mapping[str, Any],
    ) -> None:
        """Persist verified input chunks before the supervisor start fence."""

        files = manifest.get("files") if isinstance(manifest, Mapping) else None
        if not isinstance(files, list):
            raise AppActionTransportError(code="app_action_input_manifest_invalid", definitely_not_sent=True)
        expected = {
            str(item.get("file_id")): item
            for item in files
            if isinstance(item, Mapping) and item.get("file_id")
        }
        reservation = self._claim_input_staging(run_id, files, context)
        if reservation.complete:
            return
        try:
            for chunk in chunks:
                if not isinstance(chunk, Mapping) or str(chunk.get("run_id")) != str(run_id):
                    raise AppActionTransportError(code="app_action_input_chunk_invalid", definitely_not_sent=True)
                file_id = str(chunk.get("file_id") or "")
                if not re.fullmatch(r"af_[A-Za-z0-9_-]{32}", file_id):
                    raise AppActionTransportError(code="app_action_input_file_id_invalid", definitely_not_sent=True)
                item = expected.get(file_id)
                raw = chunk.get("data")
                if item is None or not isinstance(raw, (bytes, bytearray, memoryview)):
                    raise AppActionTransportError(code="app_action_input_chunk_invalid", definitely_not_sent=True)
                expected_ref = f"files/{file_id}.bin"
                if str(chunk.get("ref") or expected_ref) != expected_ref:
                    raise AppActionTransportError(code="app_action_input_ref_invalid", definitely_not_sent=True)
                try:
                    offset = int(chunk.get("offset"))
                except (TypeError, ValueError) as exc:
                    raise AppActionTransportError(code="app_action_input_offset_invalid", definitely_not_sent=True) from exc
                if offset < 0:
                    raise AppActionTransportError(code="app_action_input_offset_invalid", definitely_not_sent=True)
                path = reservation.file_root / f"{file_id}.bin"
                if _is_link_or_reparse(path):
                    raise AppActionTransportError(code="app_action_input_path_invalid", definitely_not_sent=True)
                data = bytes(raw)
                if len(data) > 4 * 1024 * 1024:
                    raise AppActionTransportError(code="app_action_input_chunk_too_large", definitely_not_sent=True)
                current_size = path.stat().st_size if path.exists() else 0
                if current_size != offset:
                    raise AppActionTransportError(code="app_action_input_offset_conflict", definitely_not_sent=True)
                with path.open("ab") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._validate_staged_input_files(reservation.file_root, files)
            (reservation.staging_root / "manifest.json").write_text(
                json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
                newline="\n",
            )
            marker = json.loads(reservation.marker_path.read_text(encoding="utf-8"))
            marker["status"] = "complete"
            reservation.marker_path.write_text(
                json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
                newline="\n",
            )
        except BaseException:
            self._cleanup_staging_reservation(reservation)
            raise

    def _materialize_staged_inputs(
        self, run_id: str, input_files: Any, run_root: Path
    ) -> tuple[int, int] | None:
        staging = self._run_root(self.workspace_root, run_id) / "input-staging"
        manifest = input_files
        if not isinstance(manifest, list):
            manifest_path = staging / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, list) or not manifest:
            return None
        source_root = staging / "files"
        if _is_link_or_reparse(staging) or _is_link_or_reparse(source_root):
            raise ServerActionPreparationError("app_action_server_input_path_invalid")
        input_root = run_root / "inputs"
        destination_root = input_root / "files"
        if _entry_exists(input_root) or _is_link_or_reparse(destination_root):
            raise ServerActionPreparationError("app_action_server_input_collision")
        try:
            input_root.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise ServerActionPreparationError("app_action_server_input_collision") from exc
        input_identity = owned_directory_identity(input_root)
        if input_identity is None:
            raise ServerActionPreparationError("app_action_server_input_unavailable")
        try:
            destination_root.mkdir(parents=False, exist_ok=False)
            for item in manifest:
                if not isinstance(item, Mapping):
                    raise ServerActionPreparationError("app_action_server_input_manifest_invalid")
                file_id = str(item.get("file_id") or "")
                if not re.fullmatch(r"af_[A-Za-z0-9_-]{32}", file_id):
                    raise ServerActionPreparationError("app_action_server_input_file_id_invalid")
                source = source_root / f"{file_id}.bin"
                destination = destination_root / f"{file_id}.bin"
                if not source.is_file() or _is_link_or_reparse(source):
                    raise ServerActionPreparationError("app_action_server_input_missing")
                expected_size = int(item.get("size_bytes") or -1)
                if source.stat().st_size != expected_size:
                    raise ServerActionPreparationError("app_action_server_input_size_mismatch")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if digest.casefold() != str(item.get("sha256") or "").casefold():
                    raise ServerActionPreparationError("app_action_server_input_hash_mismatch")
                shutil.copyfile(source, destination)
        except BaseException:
            cleanup_owned_directory(input_root, input_identity)
            raise
        return input_identity

    async def capture_outputs(
        self,
        *,
        run_id: str,
        owner_user_id: str,
        project_id: str | UUID | None = None,
        outputs: list[Any],
        action_ref: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Capture worker output bytes/path entries into opaque file storage."""

        journal = self._journal()
        if journal is None:
            raise AppActionTransportError(code="app_action_run_unknown")
        try:
            row = journal.get(run_id)
            if row is None:
                raise AppActionTransportError(code="app_action_run_unknown")
            from .app_action_file_service import AppActionFileService

            service = AppActionFileService(workspace_root=self.workspace_root)
            captured = await asyncio.to_thread(
                service.capture_outputs,
                outputs,
                run_id=run_id,
                app_id=row["app_id"],
                owner_user_id=owner_user_id,
                project_id=project_id or row.get("project_id"),
                action_id=action_ref or row.get("action_ref"),
                allowed_root=str(Path(row["runtime_root"]).resolve().parent),
            )
            if row.get("status") in {"succeeded", "failed", "cancelled"}:
                journal.cleanup_run_artifacts(run_id, owner_user_id=owner_user_id)
            return [item.to_dict() for item in captured]
        except AppActionTransportError:
            raise
        except Exception as exc:
            raise AppActionTransportError(code="app_action_output_file_unavailable") from exc
        finally:
            journal.close()

    async def download_outputs(
        self,
        *,
        run_id: str,
        outputs: list[Any],
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Resolve result file paths to byte payloads for FileAware capture."""

        journal = self._journal()
        if journal is None:
            raise AppActionTransportError(code="app_action_run_unknown")
        try:
            row = journal.get(run_id)
            if row is None:
                raise AppActionTransportError(code="app_action_run_unknown")
            root = Path(row["runtime_root"]).resolve().parent
            result: list[dict[str, Any]] = []
            for item in outputs:
                if not isinstance(item, Mapping):
                    continue
                source = item.get("source_path", item.get("path"))
                if source is None:
                    if item.get("data", item.get("bytes")) is not None:
                        result.append(dict(item))
                    continue
                path = Path(str(source))
                if not path.is_absolute():
                    path = root / path
                path = path.resolve()
                if not path.is_file():
                    runtime_candidate = (root / "runtime" / Path(str(source))).resolve()
                    if runtime_candidate.is_file():
                        path = runtime_candidate
                try:
                    path.relative_to(root)
                except ValueError as exc:
                    raise AppActionTransportError(code="app_action_output_path_invalid") from exc
                if path.is_symlink() or not path.is_file():
                    raise AppActionTransportError(code="app_action_output_file_missing")
                payload = path.read_bytes()
                item_copy = dict(item)
                item_copy["data"] = payload
                try:
                    item_copy["remote_ref"] = path.relative_to(root).as_posix()
                except ValueError:
                    item_copy["remote_ref"] = path.name
                item_copy.pop("source_path", None)
                item_copy.pop("path", None)
                item_copy.setdefault("filename", path.name)
                result.append(item_copy)
            return result
        except AppActionTransportError:
            raise
        except OSError as exc:
            raise AppActionTransportError(code="app_action_output_file_unavailable") from exc
        finally:
            journal.close()

    async def start(
        self,
        *,
        owner_user_id: str,
        device_id: str,
        machine_id: str,
        payload: Mapping[str, Any],
        expected_connection_epoch: str | None = None,
        actor_user_id: str | None = None,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping) or str(payload.get("execution_target_kind") or "server") != "server":
            raise AppActionTransportError(code="app_action_server_payload_invalid", definitely_not_sent=True)
        if str(payload.get("execution_mode") or "") != "finite":
            raise AppActionTransportError(code="app_action_server_mode_unsupported", definitely_not_sent=True)
        try:
            run_id = str(UUID(str(payload.get("run_id"))))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AppActionTransportError(code="app_action_server_run_id_invalid", definitely_not_sent=True) from exc
        journal = self._journal(create=True)
        if journal is None:  # pragma: no cover - create=True always opens it
            raise AppActionTransportError(code="app_action_server_journal_unavailable", definitely_not_sent=True)
        try:
            input_json = payload.get("input")
            if not isinstance(input_json, dict):
                raise AppActionTransportError(code="app_action_server_input_invalid", definitely_not_sent=True)
            request_fingerprint = {
                "app_id": str(payload.get("app_id") or ""),
                "project_id": str(payload.get("project_id") or ""),
                "release_id": str(payload.get("release_id") or ""),
                "action_key": str(payload.get("action_key") or ""),
                "action_ref": str(payload.get("action_ref") or ""),
                "action_spec_hash": str(payload.get("action_spec_hash") or ""),
                "target_key": str(payload.get("target_key") or ""),
                "target_contract_hash": str(payload.get("contract_hash") or ""),
                "owner_user_id": str(owner_user_id),
                "device_id": str(device_id),
                "machine_id": str(machine_id),
                "connection_epoch": str(expected_connection_epoch or self._connection_epoch),
                "mode": str(payload.get("execution_mode") or ""),
                "input_sha256": canonical_json_hash(input_json),
            }
            reserved = journal.reserve_start(run_id, request_fingerprint)
            if reserved:
                existing = journal.get(run_id)
                if existing is not None:
                    snapshot = journal.remote_snapshot(run_id)
                    if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
                        journal.cleanup_run_artifacts(run_id, owner_user_id=owner_user_id)
                    return snapshot
                pending = journal.pending_snapshot(run_id)
                if pending is not None:
                    # The first caller still owns materialization.  Return a
                    # receipt with the exact request identity; never touch its
                    # runtime tree or claim another spawn boundary.
                    pending["action_ref"] = str(payload.get("action_ref") or pending.get("action_ref") or "")
                    pending["machine_id"] = str(machine_id)
                    pending["device_id"] = str(device_id)
                    pending["contract_hash"] = str(payload.get("contract_hash") or pending.get("contract_hash") or "")
                    pending["connection_epoch"] = str(expected_connection_epoch or self._connection_epoch)
                    return pending
                raise AppActionTransportError(code="app_action_start_reserved", definitely_not_sent=False)
            prepared: PreparedServerAction | None = None
            materialize_task: asyncio.Task[Any] | None = None
            staging_reservation: _InputStagingReservation | None = None
            materialized_inputs_identity: tuple[int, int] | None = None
            try:
                staging_reservation = self._claim_staging_for_start(
                    run_id,
                    payload,
                    str(owner_user_id),
                )
                prepared = await self._prepare(
                    payload,
                    owner_user_id=str(owner_user_id),
                    actor_user_id=str(actor_user_id or "") or None,
                    device_id=str(device_id),
                    machine_id=str(machine_id),
                    expected_epoch=expected_connection_epoch,
                )
                materialize_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._materialize_staged_inputs,
                        run_id,
                        payload.get("input_files"),
                        prepared.runtime_root.parent,
                    )
                )
                try:
                    materialized_inputs_identity = await asyncio.shield(materialize_task)
                except asyncio.CancelledError:
                    try:
                        await asyncio.shield(materialize_task)
                    except BaseException:
                        pass
                    if materialize_task.done() and not materialize_task.cancelled():
                        try:
                            materialized_inputs_identity = materialize_task.result()
                        except BaseException:
                            materialized_inputs_identity = None
                    raise
            except asyncio.CancelledError:
                journal.abort_reservation(run_id, "app_action_server_prepare_cancelled")
                if prepared is not None:
                    cleanup_owned_directory(prepared.runtime_root, prepared.runtime_identity)
                if materialized_inputs_identity is not None:
                    cleanup_owned_directory(
                        prepared.runtime_root.parent / "inputs" if prepared is not None else self._run_root(self.workspace_root, run_id) / "inputs",
                        materialized_inputs_identity,
                    )
                self._cleanup_start_staging(staging_reservation)
                raise
            except (
                ServerActionPreparationError,
                ServerJobExecutionDenied,
                AppJobIsolationError,
                AppStorageError,
                ServerActionJournalError,
                OSError,
                TypeError,
                ValueError,
                UnicodeError,
            ) as exc:
                journal.abort_reservation(run_id, getattr(exc, "code", "app_action_server_prepare_failed"))
                if prepared is not None:
                    cleanup_owned_directory(prepared.runtime_root, prepared.runtime_identity)
                if materialized_inputs_identity is not None:
                    cleanup_owned_directory(
                        prepared.runtime_root.parent / "inputs" if prepared is not None else self._run_root(self.workspace_root, run_id) / "inputs",
                        materialized_inputs_identity,
                    )
                self._cleanup_start_staging(staging_reservation)
                raise AppActionTransportError(
                    code=getattr(exc, "code", "app_action_server_prepare_failed"),
                    definitely_not_sent=True,
                ) from exc
            record = {
                "run_id": run_id,
                "app_id": str(payload["app_id"]),
                "project_id": str(payload["project_id"]) if payload.get("project_id") else None,
                "release_id": str(payload["release_id"]),
                "action_key": str(payload["action_key"]),
                "action_ref": str(payload["action_ref"]),
                "action_spec_hash": str(payload["action_spec_hash"]),
                "target_key": str(payload["target_key"]),
                "target_contract_hash": str(payload["contract_hash"]),
                "owner_user_id": str(owner_user_id),
                "device_id": str(device_id),
                "machine_id": str(machine_id),
                "connection_epoch": str(expected_connection_epoch or self._connection_epoch),
                "mode": str(payload["execution_mode"]),
                "runtime_root": str(prepared.runtime_root),
                "runtime_tree_sha256": prepared.runtime_tree_sha256,
                "command": list(prepared.command),
                "command_sha256": prepared.command_sha256,
                "input": input_json,
                "input_sha256": canonical_json_hash(input_json),
                "input_files": payload.get("input_files") if isinstance(payload.get("input_files"), list) else [],
                "input_path": str(prepared.input_path),
                "output_path": str(prepared.output_path),
                "output_manifest_path": str(prepared.output_manifest_path),
                "log_path": str(prepared.log_path),
                "isolation": prepared.isolation_config,
            }
            _row, replayed = journal.prepare(record)
            if replayed:
                snapshot = journal.remote_snapshot(run_id)
                if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
                    journal.cleanup_run_artifacts(run_id, owner_user_id=owner_user_id)
                return snapshot
            journal.finish_reservation(run_id)
            if not journal.claim_spawn(run_id):
                snapshot = journal.remote_snapshot(run_id)
                if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
                    journal.cleanup_run_artifacts(run_id, owner_user_id=owner_user_id)
                return snapshot
            try:
                supervisor = self.supervisor_factory(journal.path, run_id)
            except (OSError, subprocess.SubprocessError, TypeError) as exc:
                journal.update(run_id, status="failed", execution_status="failed", error="supervisor_spawn_failed:" + type(exc).__name__)
                journal.cleanup_run_artifacts(run_id, owner_user_id=owner_user_id)
                return journal.remote_snapshot(run_id)
            journal.update(run_id, supervisor_pid=int(supervisor.pid))
            snapshot = journal.remote_snapshot(run_id)
            if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
                journal.cleanup_run_artifacts(run_id, owner_user_id=owner_user_id)
            return snapshot
        finally:
            journal.close()

    @staticmethod
    def _assert_identity(row: Mapping[str, Any], *, run_id: str, action_ref: str, machine_id: str) -> None:
        if str(row.get("run_id") or "") != str(run_id) or str(row.get("action_ref") or "") != str(action_ref) or str(row.get("machine_id") or "") != str(machine_id):
            raise AppActionTransportError(code="app_action_identity_mismatch")

    async def status(
        self,
        *,
        owner_user_id: str,
        device_id: str,
        machine_id: str,
        run_id: str,
        action_ref: str,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        journal = self._journal()
        if journal is None:
            raise AppActionTransportError(code="app_action_run_unknown")
        try:
            row = journal.get(run_id)
            if row is None:
                pending = journal.pending_snapshot(run_id)
                if pending is None:
                    raise AppActionTransportError(code="app_action_run_unknown")
                self._assert_identity(
                    pending,
                    run_id=str(run_id),
                    action_ref=str(action_ref),
                    machine_id=str(machine_id),
                )
                return pending
            self._assert_identity(row, run_id=str(run_id), action_ref=str(action_ref), machine_id=str(machine_id))
            if row.get("status") not in {"succeeded", "failed", "cancelled", "unknown"}:
                pid = row.get("supervisor_pid")
                alive = False
                if type(pid) is int and pid > 0:
                    try:
                        from pc_bridge.action_identity import process_identity, process_identity_matches, boot_id

                        identity = {"pid": pid, "created": row.get("supervisor_started")}
                        alive = process_identity_matches(identity, boot=row.get("supervisor_boot_id"), current_boot=boot_id())
                    except Exception:
                        alive = False
                if not alive:
                    age = max(0.0, __import__("time").time() - float(row.get("updated_at") or 0))
                    if age > 5:
                        journal.mark_unknown_if_active(run_id, "supervisor_unobserved")
            snapshot = journal.remote_snapshot(run_id)
            if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
                journal.cleanup_run_artifacts(run_id, owner_user_id=owner_user_id)
            return snapshot
        except ServerActionJournalError as exc:
            raise AppActionTransportError(code=exc.code) from exc
        finally:
            journal.close()

    async def logs(
        self,
        *,
        owner_user_id: str,
        device_id: str,
        machine_id: str,
        run_id: str,
        action_ref: str,
        cursor: int = 0,
        limit_bytes: int = MAX_LOG_BYTES,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        journal = self._journal()
        if journal is None:
            raise AppActionTransportError(code="app_action_run_unknown")
        try:
            row = journal.get(run_id)
            if row is None:
                raise AppActionTransportError(code="app_action_run_unknown")
            self._assert_identity(row, run_id=str(run_id), action_ref=str(action_ref), machine_id=str(machine_id))
            return journal.logs(run_id, cursor=max(0, int(cursor)), limit_bytes=max(1, min(int(limit_bytes), MAX_LOG_BYTES)))
        except (ServerActionJournalError, ValueError, TypeError) as exc:
            raise AppActionTransportError(code=getattr(exc, "code", "app_action_logs_unavailable")) from exc
        finally:
            journal.close()

    async def stop(
        self,
        *,
        owner_user_id: str,
        device_id: str,
        machine_id: str,
        run_id: str,
        action_ref: str,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        journal = self._journal()
        if journal is None:
            raise AppActionTransportError(code="app_action_run_unknown")
        try:
            row = journal.get(run_id)
            if row is None:
                raise AppActionTransportError(code="app_action_run_unknown")
            self._assert_identity(row, run_id=str(run_id), action_ref=str(action_ref), machine_id=str(machine_id))
            journal.request_stop(run_id)
            return journal.remote_snapshot(run_id)
        except ServerActionJournalError as exc:
            raise AppActionTransportError(code=exc.code) from exc
        finally:
            journal.close()


def get_app_action_server_transport(
    *,
    db_manager: Any,
    config: Mapping[str, Any] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    **_kwargs: Any,
) -> ServerAppActionTransport:
    """Build the production server Action transport at the composition root."""
    return ServerAppActionTransport(
        db_manager,
        workspace_root=workspace_root,
        deployment_config=config,
    )


# Short alias for callers that use the domain-first naming convention.
AppActionServerTransport = ServerAppActionTransport


__all__ = [
    "PreparedServerAction",
    "ServerActionPreparationError",
    "ServerAppActionTransport",
    "AppActionServerTransport",
    "get_app_action_server_transport",
    "_extract_runtime_bundle",
    "_parse_command",
    "_resolve_command_paths",
]
