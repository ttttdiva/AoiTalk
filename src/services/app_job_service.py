"""Manifest command runner for App build/test/run/package jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shlex
import shutil
import subprocess
import sys
import stat
import threading
import time
import zipfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from ..memory.models import App, AppArtifact, AppJob, AppRelease, AppTarget, ProjectApp, User
from .app_service import AppService, permission_at_least
from .app_manifest_service import AppManifestError, parse_manifest_text
from .app_operation_lock import (
    AppOperationLockError,
    app_job_input_reference_lock,
    app_operation_lock,
)
from .app_job_execution_policy import (
    ServerJobExecutionDenied,
    assert_user_may_start_server_job,
)
from .app_job_isolation import (
    AppJobIsolationError,
    pop_owned_process,
    require_isolation_contract,
    runner_env_marker,
    spawn_isolated_process,
    stop_owned_job,
)
from .app_storage import (
    ensure_app_instance,
    ensure_app_workspace,
    get_app_instance_path,
    get_app_workspace_path,
    resolve_app_artifact_file,
    normalize_app_relative_path,
    verify_file_integrity,
)


logger = logging.getLogger(__name__)


class AppJobError(RuntimeError):
    """Job execution failure."""


_RUNNING_PROCESSES: dict[str, subprocess.Popen[str]] = {}
#: 取消要求のマーカー。値は要求時刻（monotonic）で、TTL/件数上限で刈り取る。
_CANCELLED_JOBS: dict[str, float] = {}
_PROCESS_LOCK = threading.Lock()
_COMMAND_FORBIDDEN_CHARS = set("\x00\r\n;&|<>`%")

#: 実行中 Job の durable status を確認する間隔（秒）。
#: 短すぎるとJob 1件あたりのセッション取得回数が跳ね上がるため、
#: 取消の反映遅延と接続消費のバランスでこの値にしている。
JOB_STATUS_POLL_INTERVAL_SECONDS = 3.0

#: 異常終了時にサブプロセス worker の後始末を待つ上限（秒）。
_PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 30.0

#: 取消マーカーの保持上限。`_clear_job_cancellation` が届かない経路
#: （別ワーカーが実行中の Job など）でも無制限に溜めない。
_CANCELLED_JOB_TTL_SECONDS = 6 * 60 * 60
_CANCELLED_JOBS_MAX = 2_000
_RUNTIME_OWNER_MARKER = ".aoitalk-runtime-owner.json"


def _parse_command(command: str) -> list[str]:
    """Turn a validated manifest command into argv without invoking a shell."""
    value = str(command or "").strip()
    if not value:
        raise AppJobError("command が空です")
    if any(character in _COMMAND_FORBIDDEN_CHARS for character in value):
        raise AppJobError("command にshell演算子・改行・環境変数展開を含めることはできません")
    try:
        argv = shlex.split(value, posix=os.name != "nt")
    except ValueError as exc:
        raise AppJobError(f"command の引用符が不正です: {exc}") from exc
    if os.name == "nt":
        argv = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}
            else token
            for token in argv
        ]
    if not argv:
        raise AppJobError("command をargvへ分解できません")
    return argv


def _command_config(target: AppTarget, job_type: str) -> dict[str, Any]:
    snapshot = target.manifest_snapshot if isinstance(target.manifest_snapshot, dict) else {}
    return _command_config_from_snapshot(snapshot, target.target_key, job_type)


def _command_config_from_snapshot(snapshot: dict[str, Any], target_key: str, job_type: str) -> dict[str, Any]:
    target_snapshot = snapshot
    if "targets" in snapshot:
        raw_targets = snapshot.get("targets")
        target_snapshot = raw_targets.get(target_key) if isinstance(raw_targets, dict) else None
        if not isinstance(target_snapshot, dict):
            raise AppJobError(f"ReleaseにTarget {target_key} がありません")
    raw = target_snapshot.get(job_type)
    if isinstance(raw, str):
        return {"command": raw}
    if isinstance(raw, dict):
        return dict(raw)
    raise AppJobError(f"target {target_key} に {job_type} command がありません")


def _safe_env(input_json: dict[str, Any], extra: dict[str, str] | None = None) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "LANG",
        "LC_ALL",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    # Jobs are launched by the same AoiTalk runtime that owns the App.  On
    # Windows the parent process may be a system Python shim while the
    # application dependencies (pytest, openpyxl, etc.) live in the active
    # virtualenv.  Prepend the current interpreter's bin directory rather than
    # widening the environment to arbitrary host variables; manifest commands
    # such as ``python -m pytest`` then resolve to the verified runtime.
    runtime_bins = [Path(sys.executable).resolve().parent]
    # ``main.py`` may supervise the backend with a system Python while the
    # repository's declared dependencies live in its virtualenv.  Prefer that
    # project-local runtime when it exists, then retain the active interpreter
    # directory as a fallback for packaged deployments.
    repo_root = Path(__file__).resolve().parents[2]
    # The source tree convention is <repo>/venv/<bin-dir>.
    project_venv_bin = repo_root / "venv" / ("Scripts" if os.name == "nt" else "bin")
    if project_venv_bin.is_dir():
        runtime_bins.insert(0, project_venv_bin)
    existing_path = str(env.get("PATH") or "")
    env["PATH"] = os.pathsep.join(
        [str(item) for item in runtime_bins] + ([existing_path] if existing_path else [])
    )
    env["AOITALK_APP_INPUT_JSON"] = json.dumps(input_json or {}, ensure_ascii=False)
    env["AOITALK_APP_RUNNER"] = runner_env_marker()
    # Keep durable AppJob logs decodable across Windows system/virtualenv
    # interpreters.  The runner writes UTF-8 and the API exposes the same file
    # to the WebUI; without these explicit flags a child Python may emit the
    # console code page (for example CP932), causing log inspection to fail.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()


async def _materialize_release_target(
    *,
    app: App,
    target: AppTarget,
    release_id: UUID,
    project_id: UUID,
    instance_path: Path,
    session: Any,
    workspace_root: str | os.PathLike[str] | None,
    job_id: UUID,
) -> Path:
    """Expand the immutable runtime bundle into a project instance sandbox."""
    artifact = await session.scalar(
        select(AppArtifact).where(
            AppArtifact.release_id == release_id,
            AppArtifact.target_id == target.id,
            AppArtifact.artifact_type == "runtime_bundle",
        ).limit(1)
    )
    if artifact is None:
        raise AppJobError("固定ReleaseのTarget Artifactがありません")
    artifact_name = Path(str(artifact.filename or "")).name
    if not artifact_name or artifact_name in {".", ".."}:
        raise AppJobError("Release Artifact filename が不正です")
    artifact_path = resolve_app_artifact_file(
        app.id,
        release_id,
        artifact_name,
        workspace_root=workspace_root,
    )
    try:
        verify_file_integrity(
            artifact_path,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )
    except (OSError, ValueError) as exc:
        raise AppJobError(f"固定ReleaseのArtifactを検証できません: {exc}") from exc
    runtime_parent = instance_path / ".runtime"
    runtime_entry = runtime_parent / str(job_id)
    # A retry must create a fresh directory for the same durable Job.  It must
    # never follow a junction/symlink at the boundary or reuse an old
    # directory by name, because that could remove another attempt's tree.
    if _is_reparse_or_symlink(runtime_parent) or _is_reparse_or_symlink(runtime_entry):
        raise AppJobError("Release runtime sandbox にreparse pathがあります")
    runtime_parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_symlink(runtime_parent):
        raise AppJobError("Release runtime sandbox にreparse pathがあります")
    try:
        runtime_entry.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise AppJobError("Release runtime sandbox が既に存在します") from exc
    if _is_reparse_or_symlink(runtime_entry):
        raise AppJobError("Release runtime sandbox にreparse pathがあります")
    runtime_root = runtime_entry.resolve()
    instance_root = instance_path.resolve()
    try:
        runtime_root.relative_to(instance_root)
    except ValueError as exc:
        raise AppJobError("Release runtime path がProject App instance外です") from exc
    runtime_identity = _capture_runtime_identity(runtime_root)
    if runtime_identity is None:
        raise AppJobError("Release runtime sandbox のidentityを確定できません")
    try:
        _write_runtime_owner_marker(
            runtime_root,
            job_id=job_id,
            app_id=app.id,
            project_id=project_id,
            release_id=release_id,
            identity=runtime_identity,
        )
    except (OSError, TypeError, ValueError) as exc:
        _remove_runtime_directory_by_identity(runtime_root, runtime_identity)
        raise AppJobError("Release runtime sandbox のowner markerを保存できません") from exc
    if not _register_runtime_sandbox(job_id, runtime_root, runtime_identity):
        _remove_runtime_directory_by_identity(runtime_root, runtime_identity)
        raise AppJobError("Release runtime sandbox のidentityを登録できません")
    # rmtree + zip 全展開は Release サイズに比例した同期 I/O で、しかもここは
    # App operation lock と App/AppJob 行ロックを保持中。event loop 上で回すと
    # 同一プロセスの他リクエストごと止まるため、必ずスレッドへ逃がす。
    await asyncio.to_thread(
        _extract_release_bundle,
        artifact_path=artifact_path,
        runtime_root=runtime_root,
        expected_identity=runtime_identity,
    )
    return runtime_root


def _extract_release_bundle(
    *,
    artifact_path: Path,
    runtime_root: Path,
    expected_identity: tuple[int, int, int] | None = None,
) -> None:
    """Blocking part of release materialization (new directory + extraction)."""
    runtime_root = Path(runtime_root)
    if expected_identity is None:
        if _is_reparse_or_symlink(runtime_root.parent) or _is_reparse_or_symlink(runtime_root):
            raise AppJobError("Release runtime sandbox にreparse pathがあります")
        runtime_root.parent.mkdir(parents=True, exist_ok=True)
        try:
            runtime_root.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise AppJobError("Release runtime sandbox が既に存在します") from exc
        expected_identity = _capture_runtime_identity(runtime_root)
        if expected_identity is None:
            raise AppJobError("Release runtime sandbox のidentityを確定できません")
    elif not _runtime_identity_matches(runtime_root, expected_identity):
        raise AppJobError("Release runtime sandbox がmaterialize中に置換されました")
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            for member in archive.infolist():
                try:
                    relative = normalize_app_relative_path(member.filename)
                except ValueError:
                    raise AppJobError("Release Artifactに不正なpathがあります") from None
                if relative.casefold() == _RUNTIME_OWNER_MARKER.casefold():
                    raise AppJobError("Release Artifactがruntime owner markerを上書きします")
                destination = (runtime_root / Path(relative)).resolve()
                try:
                    destination.relative_to(runtime_root)
                except ValueError as exc:
                    raise AppJobError("Release Artifactがruntime sandbox外へ出ます") from exc
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target_file:
                    shutil.copyfileobj(source, target_file)
    except (zipfile.BadZipFile, OSError) as exc:
        raise AppJobError(f"Release Artifactを展開できません: {exc}") from exc
    if not _runtime_identity_matches(runtime_root, expected_identity):
        raise AppJobError("Release runtime sandbox がmaterialize後に置換されました")


_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_RUNTIME_SANDBOX_IDENTITIES: dict[str, tuple[Path, tuple[int, int, int]]] = {}
_RUNTIME_SANDBOX_LOCK = threading.Lock()


def _capture_runtime_identity(path: Path) -> tuple[int, int, int] | None:
    """Capture the filesystem identity of one newly-created directory."""
    try:
        # lstat is required here: stat would follow a link inserted between
        # the mkdir and this ownership check and record the target directory
        # as if it were this job's sandbox.
        item_stat = path.lstat()
    except (FileNotFoundError, OSError):
        return None
    if stat.S_ISLNK(item_stat.st_mode):
        return None
    if getattr(item_stat, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    ):
        return None
    if not stat.S_ISDIR(item_stat.st_mode):
        return None
    # st_dev/st_ino are the stable directory identity on POSIX and Windows.
    # Windows also exposes creation time, which distinguishes a rapid
    # same-path replacement when a file index is reused.
    creation_ns = int(item_stat.st_ctime_ns) if os.name == "nt" else 0
    return (int(item_stat.st_dev), int(item_stat.st_ino), creation_ns)


def _runtime_identity_matches(path: Path, identity: tuple[int, int, int]) -> bool:
    return _capture_runtime_identity(path) == identity


def _write_runtime_owner_marker(
    runtime_root: Path,
    *,
    job_id: str | UUID,
    app_id: str | UUID,
    project_id: str | UUID,
    release_id: str | UUID,
    identity: tuple[int, int, int],
) -> None:
    """Persist the exact runtime owner so a later process can reconcile it."""

    if not _runtime_identity_matches(runtime_root, identity):
        raise OSError("runtime sandbox identity changed before owner marker creation")
    marker_path = runtime_root / _RUNTIME_OWNER_MARKER
    marker = {
        "schema": "aoitalk.app_job_runtime_owner.v1",
        "job_id": str(UUID(str(job_id))),
        "app_id": str(UUID(str(app_id))),
        "project_id": str(UUID(str(project_id))),
        "release_id": str(UUID(str(release_id))),
        "runtime_path": os.path.normcase(str(runtime_root.resolve(strict=True))),
        "identity": [int(value) for value in identity],
        "attempt_id": uuid4().hex,
    }
    with marker_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(marker, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if not _runtime_identity_matches(runtime_root, identity):
        raise OSError("runtime sandbox identity changed after owner marker creation")


def _read_runtime_owner_marker(
    job: Any,
    runtime_root: Path,
) -> tuple[int, int, int] | None:
    marker_path = runtime_root / _RUNTIME_OWNER_MARKER
    if _is_reparse_or_symlink(marker_path) or not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        expected_path = os.path.normcase(str(runtime_root.resolve(strict=True)))
        identity = tuple(int(value) for value in marker.get("identity", ()))
        expected = {
            "schema": "aoitalk.app_job_runtime_owner.v1",
            "job_id": str(UUID(str(getattr(job, "id", "")))),
            "app_id": str(UUID(str(getattr(job, "app_id", "")))),
            "project_id": str(UUID(str(getattr(job, "project_id", "")))),
            "release_id": str(UUID(str(getattr(job, "release_id", "")))),
            "runtime_path": expected_path,
        }
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
        return None
    if not isinstance(marker, Mapping) or any(marker.get(key) != value for key, value in expected.items()):
        return None
    if len(identity) != 3 or not str(marker.get("attempt_id") or ""):
        return None
    identity_value = (identity[0], identity[1], identity[2])
    if not _runtime_identity_matches(runtime_root, identity_value):
        return None
    return identity_value


def _register_runtime_sandbox(
    job_id: str | UUID,
    runtime_root: Path,
    identity: tuple[int, int, int] | None = None,
) -> bool:
    identity = identity or _capture_runtime_identity(runtime_root)
    if identity is None:
        return False
    if _capture_runtime_identity(runtime_root) != identity:
        return False
    try:
        canonical_path = runtime_root.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return False
    if _capture_runtime_identity(runtime_root) != identity:
        return False
    with _RUNTIME_SANDBOX_LOCK:
        _RUNTIME_SANDBOX_IDENTITIES[str(job_id)] = (canonical_path, identity)
    return True


def _registered_runtime_identity(job_id: str | UUID, runtime_root: Path) -> tuple[int, int, int] | None:
    key = str(job_id)
    with _RUNTIME_SANDBOX_LOCK:
        registered = _RUNTIME_SANDBOX_IDENTITIES.get(key)
    if registered is None:
        return None
    registered_path, registered_identity = registered
    try:
        canonical_path = runtime_root.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        canonical_path = None
    if canonical_path is None or os.path.normcase(str(canonical_path)) != os.path.normcase(str(registered_path)):
        with _RUNTIME_SANDBOX_LOCK:
            if _RUNTIME_SANDBOX_IDENTITIES.get(key) == registered:
                _RUNTIME_SANDBOX_IDENTITIES.pop(key, None)
        return None
    if not _runtime_identity_matches(runtime_root, registered_identity):
        with _RUNTIME_SANDBOX_LOCK:
            if _RUNTIME_SANDBOX_IDENTITIES.get(key) == registered:
                _RUNTIME_SANDBOX_IDENTITIES.pop(key, None)
        return None
    return registered_identity


def _forget_runtime_sandbox(job_id: str | UUID, runtime_root: Path) -> None:
    key = str(job_id)
    with _RUNTIME_SANDBOX_LOCK:
        registered = _RUNTIME_SANDBOX_IDENTITIES.get(key)
        if registered is None:
            return
        registered_path, _ = registered
        if os.path.normcase(str(registered_path)) == os.path.normcase(str(runtime_root)):
            _RUNTIME_SANDBOX_IDENTITIES.pop(key, None)


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return whether ``path`` is a link/reparse entry.

    Runtime cleanup is deliberately conservative.  ``Path.resolve`` is not
    enough here because it follows a junction before the caller can decide
    whether the entry belongs to this job.  A failed stat is also treated as
    unsafe so an inaccessible entry is left for an operator to inspect.
    """
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        item_stat = path.lstat()
        return bool(
            getattr(item_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        return True


def _contains_reparse_entry(root: Path) -> bool:
    """Check a runtime tree without following links or junctions."""
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            return False
        except OSError:
            return True
        for entry in entries:
            entry_path = Path(entry.path)
            if _is_reparse_or_symlink(entry_path):
                return True
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry_path)
            except OSError:
                return True
    return False


def _remove_runtime_directory_by_identity(
    runtime_root: Path,
    identity: tuple[int, int, int],
) -> bool:
    """Remove only the exact, non-reparse directory created by this attempt."""

    if not _runtime_identity_matches(runtime_root, identity) or _contains_reparse_entry(runtime_root):
        return False
    if not _runtime_identity_matches(runtime_root, identity) or _is_reparse_or_symlink(runtime_root):
        return False
    try:
        shutil.rmtree(runtime_root)
    except OSError:
        return False
    return not runtime_root.exists()


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _value_references_path(value: Any, root: Path) -> bool:
    """Find an absolute persisted reference to a runtime path.

    AppJob result payloads are command-defined JSON.  Treat every absolute
    path value as a possible output reference rather than guessing a small
    list of field names.  This preserves a command's output when it has been
    recorded under the ephemeral runtime, while ordinary status/error text
    remains untouched.
    """
    if isinstance(value, (str, os.PathLike)) and not isinstance(value, bytes):
        raw = str(value).strip()
        if not raw:
            return False
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                return False
            return _path_is_under(candidate.resolve(strict=False), root)
        except (OSError, RuntimeError, ValueError):
            return False
    if isinstance(value, Mapping):
        return any(_value_references_path(item, root) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_value_references_path(item, root) for item in value)
    return False


def _cleanup_owned_runtime_sandbox(
    job: Any,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    referenced_inputs: Any = (),
) -> bool:
    """Remove one terminal AppJob's exact release runtime sandbox.

    The instance itself is durable and remains untouched.  Cleanup is allowed
    only when a durable terminal row still owns the deterministic
    ``.runtime/<job-id>`` path.  Missing/active/unknown rows, output/log
    references, collisions, and reparse entries are all retained.
    """
    if str(getattr(job, "status", "")) not in _TERMINAL_JOB_STATUSES:
        return False
    # A terminal status without its durable end timestamp is an incomplete
    # claim.  Keep the sandbox until a later reconciliation can prove the
    # owner has really reached a terminal state.
    if getattr(job, "ended_at", None) is None:
        return False

    job_uuid = getattr(job, "id", None)
    app_id = getattr(job, "app_id", None)
    project_id = getattr(job, "project_id", None)
    release_id = getattr(job, "release_id", None)
    if not job_uuid or not app_id or not project_id or not release_id:
        return False
    try:
        job_uuid = UUID(str(job_uuid))
        UUID(str(release_id))
        instance_root = get_app_instance_path(
            project_id,
            app_id,
            workspace_root=workspace_root,
        )
    except (AttributeError, TypeError, ValueError, OSError):
        return False

    runtime_parent = instance_root / ".runtime"
    runtime_root = runtime_parent / str(job_uuid)
    try:
        runtime_parent.relative_to(instance_root)
        runtime_root.relative_to(instance_root)
    except ValueError:
        return False

    # Do not follow a reparse point at any part of the cleanup boundary.  A
    # regular file at this exact name is a collision, not a job sandbox.
    if _is_reparse_or_symlink(runtime_parent) or _is_reparse_or_symlink(runtime_root):
        return False
    if not runtime_root.exists():
        return False
    if not runtime_root.is_dir():
        return False
    marker_identity = _read_runtime_owner_marker(job, runtime_root)
    if marker_identity is None:
        return False
    registered_identity = _registered_runtime_identity(job_uuid, runtime_root)
    if registered_identity is not None and registered_identity != marker_identity:
        return False
    runtime_identity = marker_identity
    if _contains_reparse_entry(runtime_root):
        return False

    result_json = getattr(job, "result_json", None)
    if _value_references_path(result_json, runtime_root):
        return False
    if _value_references_path(referenced_inputs, runtime_root):
        return False
    log_path = getattr(job, "log_path", None)
    if _value_references_path(log_path, runtime_root):
        return False

    try:
        # Recheck immediately before the destructive operation.  This closes
        # the ordinary collision race without following or force-removing an
        # entry that changed while the tree was inspected.
        if (
            _is_reparse_or_symlink(runtime_root)
            or not runtime_root.is_dir()
            or not _runtime_identity_matches(runtime_root, runtime_identity)
        ):
            return False
        shutil.rmtree(runtime_root)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        logger.warning("App job runtime sandbox の後始末に失敗しました: %s", runtime_root, exc_info=True)
        return False
    _forget_runtime_sandbox(job_uuid, runtime_root)
    return True


async def _cleanup_job_runtime_sandbox(
    db_manager: Any,
    job_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Re-read durable Job ownership before cleaning a runtime sandbox."""
    try:
        job_uuid = UUID(str(job_id))
    except (AttributeError, TypeError, ValueError):
        return False
    probe_session = None
    session = None
    app_lock = None
    lock_acquired = False
    reference_lock = None
    reference_lock_acquired = False
    try:
        # Keep cleanup in the same App -> AppJob lock order as creation and
        # execution.  This prevents a new queued job from committing an input
        # reference while an older runtime is being removed.
        probe_session = await db_manager.get_session()
        probe_job = await probe_session.scalar(
            select(AppJob).where(AppJob.id == job_uuid).limit(1)
        )
        app_id = getattr(probe_job, "app_id", None) if probe_job is not None else None
        if app_id is None:
            return False
        await probe_session.close()
        probe_session = None
        app_lock = app_operation_lock(app_id, workspace_root=workspace_root)
        await app_lock.acquire()
        lock_acquired = True
        reference_lock = app_job_input_reference_lock(workspace_root=workspace_root)
        await reference_lock.acquire()
        reference_lock_acquired = True

        session = await db_manager.get_session()
        await session.scalar(
            select(App.id).where(App.id == app_id).with_for_update().limit(1)
        )
        job = await session.scalar(
            select(AppJob).where(AppJob.id == job_uuid).with_for_update().limit(1)
        )
        if job is None:
            return False
        referenced_inputs = [getattr(job, "input_json", None)]
        if getattr(job, "app_id", None) and getattr(job, "project_id", None):
            referenced_inputs.extend(
                list(
                    await session.scalars(
                        select(AppJob.input_json).where(
                            AppJob.status.in_({"queued", "running"}),
                            AppJob.id != job_uuid,
                        )
                    )
                )
            )
        return await asyncio.to_thread(
            _cleanup_owned_runtime_sandbox,
            job,
            workspace_root=workspace_root,
            referenced_inputs=referenced_inputs,
        )
    except Exception:
        # Cleanup is best-effort and must never turn a durable terminal result
        # into a second failure.  Leaving the exact sandbox is safer than
        # guessing when the durable owner cannot be read.
        logger.warning("App job runtime sandbox の所有状態を確認できません", exc_info=True)
        return False
    finally:
        if session is not None:
            await session.close()
        if probe_session is not None:
            await probe_session.close()
        if reference_lock_acquired and reference_lock is not None:
            reference_lock.release()
        if lock_acquired and app_lock is not None:
            app_lock.release()


def _existing_runtime_job_ids(instance_root: Path) -> list[UUID]:
    """List UUID-named direct runtime children without following reparse points."""

    runtime_parent = instance_root / ".runtime"
    if _is_reparse_or_symlink(runtime_parent) or not runtime_parent.is_dir():
        return []
    job_ids: list[UUID] = []
    try:
        with os.scandir(runtime_parent) as entries:
            for entry in entries:
                try:
                    job_id = UUID(entry.name)
                    runtime_entry = Path(entry.path)
                    if _is_reparse_or_symlink(runtime_entry):
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except (OSError, TypeError, ValueError):
                    continue
                job_ids.append(job_id)
    except OSError:
        return []
    return job_ids


async def _reconcile_terminal_app_runtime_sandboxes(
    session: Any,
    current_job: Any,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> int:
    """Recover marked terminal runtimes present in one app instance.

    A process can stop after the durable Job becomes terminal but before its
    normal ``finally`` block runs.  A newly queued Job for the same instance
    scans only that instance's existing ``.runtime`` children, then checks
    each candidate's durable owner row.  It never scans historical Job rows.
    """

    reference_lock = None
    reference_lock_acquired = False
    try:
        reference_lock = app_job_input_reference_lock(workspace_root=workspace_root)
        await reference_lock.acquire()
        reference_lock_acquired = True
        try:
            if (
                str(getattr(current_job, "status", "")) != "queued"
                or not getattr(current_job, "app_id", None)
                or not getattr(current_job, "project_id", None)
            ):
                return 0
            instance_root = get_app_instance_path(
                current_job.project_id,
                current_job.app_id,
                workspace_root=workspace_root,
            )
            candidate_ids = await asyncio.to_thread(_existing_runtime_job_ids, instance_root)
            if not candidate_ids:
                return 0
            terminal_jobs: list[Any] = []
            for offset in range(0, len(candidate_ids), 256):
                batch = candidate_ids[offset : offset + 256]
                terminal_jobs.extend(
                    list(
                        await session.scalars(
                            select(AppJob)
                            .where(
                                AppJob.id.in_(batch),
                                AppJob.app_id == current_job.app_id,
                                AppJob.project_id == current_job.project_id,
                                AppJob.status.in_(_TERMINAL_JOB_STATUSES),
                                AppJob.ended_at.is_not(None),
                            )
                            .with_for_update()
                        )
                    )
                )
            active_inputs = list(
                await session.scalars(
                    select(AppJob.input_json).where(
                        AppJob.status.in_({"queued", "running"}),
                    )
                )
            )
        except Exception:
            logger.warning("App job terminal runtime reconciliation lookup failed", exc_info=True)
            return 0

        removed = 0
        for terminal_job in terminal_jobs:
            if await asyncio.to_thread(
                _cleanup_owned_runtime_sandbox,
                terminal_job,
                workspace_root=workspace_root,
                referenced_inputs=active_inputs,
            ):
                removed += 1
        return removed
    finally:
        if reference_lock_acquired and reference_lock is not None:
            reference_lock.release()


def _prune_cancelled_jobs_locked(now: float) -> None:
    """Drop cancellation markers that no execution path will ever clear.

    ``_clear_job_cancellation`` は「この worker が実行した Job」でしか呼ばれない。
    別 worker が実行中の Job を停止した場合などにマーカーだけが残るため、
    TTL と件数上限で必ず回収する。
    """
    expired = [
        key
        for key, marked_at in _CANCELLED_JOBS.items()
        if now - marked_at > _CANCELLED_JOB_TTL_SECONDS
    ]
    for key in expired:
        _CANCELLED_JOBS.pop(key, None)
    while len(_CANCELLED_JOBS) > _CANCELLED_JOBS_MAX:
        oldest = min(_CANCELLED_JOBS, key=lambda key: _CANCELLED_JOBS[key])
        _CANCELLED_JOBS.pop(oldest, None)


def stop_running_job(job_id: str | UUID) -> bool:
    key = str(job_id)
    with _PROCESS_LOCK:
        _CANCELLED_JOBS[key] = time.monotonic()
        _prune_cancelled_jobs_locked(_CANCELLED_JOBS[key])
        process = _RUNNING_PROCESSES.get(key)
    if stop_owned_job(job_id):
        return True
    if process is None:
        return False
    _kill_process_tree(process)
    return True


def _job_was_cancelled(job_id: str | UUID) -> bool:
    with _PROCESS_LOCK:
        return str(job_id) in _CANCELLED_JOBS


def _clear_job_cancellation(job_id: str | UUID) -> None:
    with _PROCESS_LOCK:
        _CANCELLED_JOBS.pop(str(job_id), None)


def run_subprocess_job(
    *,
    job_id: str | UUID,
    command: str,
    cwd: Path,
    log_path: Path,
    input_json: dict[str, Any],
    timeout_seconds: int = 900,
    environment: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one already-validated manifest command in a fixed cwd."""
    argv = _parse_command(command)
    cwd = cwd.resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    log_path = log_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.utcnow()
    try:
        require_isolation_contract(config)
    except AppJobIsolationError as exc:
        return {
            "status": "failed",
            "exit_code": None,
            "log_path": log_path,
            "duration_seconds": 0.0,
            "error": str(exc),
        }
    child_env = _safe_env(input_json, environment)
    # Resolve the managed Python executable explicitly.  Windows can ignore a
    # child environment's PATH when searching a bare ``python`` argv, which
    # otherwise selects a system interpreter without AoiTalk's dependencies.
    # The manifest remains portable (``python ...``); only the parsed argv
    # passed to CreateProcess is rebound to the trusted runtime path.
    if argv and Path(argv[0]).name.casefold() in {"python", "python.exe"}:
        managed_python = shutil.which(argv[0], path=child_env.get("PATH"))
        if managed_python:
            argv[0] = managed_python
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        log.write(f"$ {command}\n")
        log.write(f"cwd={cwd}\n")
        log.flush()
        if _job_was_cancelled(job_id):
            log.write("\n[AoiTalk] cancelled before process start\n")
            return {
                "status": "cancelled",
                "exit_code": None,
                "log_path": log_path,
                "duration_seconds": max(0.0, (datetime.utcnow() - started).total_seconds()),
            }
        try:
            process = spawn_isolated_process(
                job_id=job_id,
                argv=argv,
                cwd=cwd,
                env=child_env,
                log_file=log,
                config=config,
            )
        except AppJobIsolationError as exc:
            log.write(f"\n[AoiTalk] isolation error: {exc}\n")
            return {
                "status": "failed",
                "exit_code": None,
                "log_path": log_path,
                "duration_seconds": max(0.0, (datetime.utcnow() - started).total_seconds()),
                "error": str(exc),
            }
        with _PROCESS_LOCK:
            _RUNNING_PROCESSES[str(job_id)] = process
        try:
            if _job_was_cancelled(job_id):
                stop_owned_job(job_id)
            if process.stdin is not None:
                if not _job_was_cancelled(job_id):
                    process.stdin.write(json.dumps(input_json or {}, ensure_ascii=False))
                process.stdin.close()
            try:
                exit_code = process.wait(timeout=max(1, min(timeout_seconds, 86_400)))
                status = "cancelled" if _job_was_cancelled(job_id) else "succeeded" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                stop_owned_job(job_id)
                exit_code = process.wait(timeout=10)
                status = "failed"
                log.write("\n[AoiTalk] timeout: process tree stopped\n")
        finally:
            with _PROCESS_LOCK:
                _RUNNING_PROCESSES.pop(str(job_id), None)
            pop_owned_process(job_id)
    return {
        "status": status,
        "exit_code": exit_code,
        "log_path": log_path,
        "duration_seconds": max(0.0, (datetime.utcnow() - started).total_seconds()),
    }


async def _mark_job_failed(
    db_manager: Any,
    job_id: str | UUID,
    message: str,
) -> dict[str, Any] | None:
    """Persist a terminal failure for a Job that never reached execution.

    呼び出し元は両方とも bare ``asyncio.create_task`` で例外を回収しないため、
    ここで終了状態を書かないと ``queued`` のまま永久に「実行待ち」に見える。
    """
    session = None
    try:
        session = await db_manager.get_session()
        job = await session.scalar(
            select(AppJob).where(AppJob.id == UUID(str(job_id))).limit(1)
        )
        if job is None:
            return None
        if job.status in {"queued", "running"}:
            job.status = "failed"
            job.result_json = {"error": message}
            job.ended_at = datetime.utcnow()
            await session.commit()
        return job.to_dict()
    finally:
        if session is not None:
            await session.close()
        _clear_job_cancellation(job_id)


def _discard_task_outcome(task: "asyncio.Task[Any]") -> None:
    """Consume a worker task's outcome so its exception is never left unretrieved."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("App job subprocess worker が失敗しました", exc_info=error)


async def _stop_and_reap_process_task(
    job_id: str | UUID,
    process_task: "asyncio.Task[dict[str, Any]] | None",
) -> None:
    """Stop the subprocess and reap its worker task before abandoning a Job.

    ``stop_running_job`` は Windows で ``taskkill`` を最大10秒待つ同期処理であり、
    ``subprocess.TimeoutExpired`` を送出しうる。ここで面倒を見ないと、DB を
    failed にした後もサブプロセスと worker task だけが残り続ける。
    """
    try:
        await asyncio.shield(asyncio.to_thread(stop_running_job, job_id))
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("App job %s の停止要求に失敗しました", job_id, exc_info=True)
    if process_task is None:
        return
    if not process_task.done():
        try:
            await asyncio.wait({process_task}, timeout=_PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            pass
    if process_task.done():
        _discard_task_outcome(process_task)
    else:
        # to_thread のスレッドは中断できないので、終了時に結果だけ回収する。
        process_task.add_done_callback(_discard_task_outcome)


async def _execute_app_job(
    db_manager: Any,
    job_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    timeout_seconds: int = 900,
    deployment_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Transition a queued DB job through process execution and persist result."""
    try:
        job_uuid = UUID(str(job_id))
    except (AttributeError, TypeError, ValueError):
        _clear_job_cancellation(job_id)
        return None
    # All App lifecycle paths use App -> AppJob lock order.  Read the Job once
    # to discover its App and release the connection *before* waiting for the
    # lock: acquisition can block for AppOperationLock の既定 300 秒 で、同一App
    # へ複数 Job を投入すると待機数だけ接続がアイドル保持されてしまう。
    session = await db_manager.get_session()
    try:
        job = await session.scalar(
            select(AppJob).where(AppJob.id == job_uuid).limit(1)
        )
        if job is None:
            _clear_job_cancellation(job_id)
            return None
        if job.status != "queued":
            _clear_job_cancellation(job_id)
            return job.to_dict()
        app_id = job.app_id
    finally:
        await session.close()

    # Snapshot the Target/command while holding the same App lock used by
    # Manifest edits and Release creation.  The lock is released before the
    # long-running subprocess starts, so later edits cannot change the command
    # already selected for this Job.
    try:
        app_lock = app_operation_lock(app_id, workspace_root=workspace_root)
        await app_lock.acquire()
    except (AppOperationLockError, OSError) as exc:
        logger.warning("App job %s のoperation lockを獲得できません", job_id, exc_info=True)
        return await _mark_job_failed(
            db_manager, job_id, f"App operation lock を獲得できません: {exc}"
        )

    # ``get_session`` 自体の失敗でも lock を必ず解放するため try の内側で取る。
    session = None
    try:
        session = await db_manager.get_session()
        app = await session.scalar(select(App).where(App.id == app_id).with_for_update().limit(1))
        job = await session.scalar(
            select(AppJob).where(AppJob.id == job_uuid).with_for_update().limit(1)
        )
        if job is None:
            _clear_job_cancellation(job_id)
            return None
        if job.status != "queued":
            _clear_job_cancellation(job_id)
            return job.to_dict()
        await _reconcile_terminal_app_runtime_sandboxes(
            session,
            job,
            workspace_root=workspace_root,
        )
        target = await session.scalar(select(AppTarget).where(AppTarget.id == job.target_id).limit(1)) if job.target_id else None
        if target is None or app is None or target.app_id != app.id:
            job.status = "failed"
            job.result_json = {"error": "target or app not found"}
            job.ended_at = datetime.utcnow()
            await session.commit()
            _clear_job_cancellation(job_id)
            return job.to_dict()
        if app.archived_at is not None:
            job.status = "failed"
            job.result_json = {"error": "archived App cannot execute jobs"}
            job.ended_at = datetime.utcnow()
            await session.commit()
            _clear_job_cancellation(job_id)
            return job.to_dict()
        starter = await session.scalar(select(User).where(User.id == job.started_by).limit(1)) if job.started_by else None
        required_permission = (
            "runner" if job.job_type == "run"
            else "maintainer" if job.job_type == "package"
            else "developer"
        )
        app_service = AppService()
        actual_permission = await app_service.permission_for_app(
            session,
            app,
            user_id=starter.id if starter else UUID(int=0),
            user_role=starter.role if starter else None,
            project_id=job.project_id,
        )
        project_access = True
        if job.project_id is not None:
            binding = await session.scalar(select(ProjectApp).where(
                ProjectApp.project_id == job.project_id,
                ProjectApp.app_id == app.id,
                ProjectApp.enabled.is_(True),
            ).limit(1))
            if binding is None:
                job.status = "cancelled"
                job.result_json = {"error": "Project App binding is no longer enabled"}
                job.ended_at = datetime.utcnow()
                await session.commit()
                _clear_job_cancellation(job_id)
                return job.to_dict()
            project_access = await app_service.project_access(
                session,
                project_id=job.project_id,
                user_id=starter.id if starter else UUID(int=0),
                user_role=starter.role if starter else None,
            )
        if (
            starter is None
            or not starter.is_active
            or not project_access
            or not permission_at_least(actual_permission, required_permission)
        ):
            job.status = "cancelled"
            job.result_json = {"error": "Job開始者のProject/App権限が失われました"}
            job.ended_at = datetime.utcnow()
            await session.commit()
            _clear_job_cancellation(job_id)
            return job.to_dict()
        try:
            assert_user_may_start_server_job(
                user_role=starter.role if starter else None,
                config=deployment_config,
            )
            require_isolation_contract(deployment_config)
        except (ServerJobExecutionDenied, AppJobIsolationError) as exc:
            job.status = "failed"
            job.result_json = {"error": str(exc)}
            job.ended_at = datetime.utcnow()
            await session.commit()
            _clear_job_cancellation(job_id)
            return job.to_dict()
        try:
            effective_target: dict[str, Any] = (
                target.manifest_snapshot
                if isinstance(target.manifest_snapshot, dict)
                else {}
            )
            if job.release_id:
                release = await session.scalar(
                    select(AppRelease).where(
                        AppRelease.id == job.release_id,
                        AppRelease.app_id == app.id,
                        AppRelease.status == "published",
                    ).limit(1)
                )
                if release is None:
                    raise AppJobError("実行対象のReleaseがAppに属していないか公開されていません")
                source_artifact = await session.scalar(
                    select(AppArtifact).where(
                        AppArtifact.release_id == release.id,
                        AppArtifact.artifact_type == "source_bundle",
                    ).limit(1)
                )
                if source_artifact is None:
                    raise AppJobError("固定ReleaseのSource Bundleがありません")
                source_path = resolve_app_artifact_file(
                    app.id,
                    release.id,
                    Path(str(source_artifact.filename)).name,
                    workspace_root=workspace_root,
                )
                try:
                    verify_file_integrity(
                        source_path,
                        expected_sha256=source_artifact.sha256,
                        expected_size_bytes=source_artifact.size_bytes,
                    )
                except (OSError, ValueError) as exc:
                    raise AppJobError(f"固定ReleaseのSource Bundleを検証できません: {exc}") from exc
                # ここは DRAFT 固定でよい（というより STRICT にできない）。
                # 固定Release の Manifest は zip の中から読むため、実在チェックの
                # 基準となる workspace が存在せず、ValidationMode.STRICT は
                # 原理的に指定できない（workspace=None での STRICT は TypeError）。
                # entrypoint / build.output の実在は Release 作成時に
                # app_release_service が STRICT で検証済みであり、Runtime Bundle は
                # その時点の成果物を固めた不変物なので、ここで再検証する必要も無い。
                try:
                    with zipfile.ZipFile(source_path) as archive:
                        manifest_text = archive.read("aoitalk.app.yaml").decode("utf-8")
                    release_manifest = parse_manifest_text(manifest_text)
                except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile, AppManifestError) as exc:
                    raise AppJobError("固定ReleaseのManifestを読み込めません") from exc
                release_targets = release_manifest.get("targets")
                if not isinstance(release_targets, dict):
                    raise AppJobError("固定ReleaseにTarget定義がありません")
                effective_target = release_targets.get(target.target_key)
                if not isinstance(effective_target, dict):
                    raise AppJobError(f"固定ReleaseにTarget {target.target_key} がありません")
                target_snapshot = release_manifest
            else:
                target_snapshot = target.manifest_snapshot if isinstance(target.manifest_snapshot, dict) else {}
            execution_host = str(effective_target.get("execution_host") or target.execution_host or "")
            if job.job_type == "run" and execution_host not in {"aoitalk", "server"}:
                raise AppJobError(
                    f"Targetのexecution_host={execution_host} はサーバー実行に対応していません"
                )
            command_config = _command_config_from_snapshot(
                target_snapshot, target.target_key, job.job_type
            )
            command = str(command_config.get("command") or "").strip()
            app_workspace = ensure_app_workspace(app.id, name=app.name, workspace_root=workspace_root)
            cwd = app_workspace
            instance_path: Path | None = None
            if job.project_id:
                instance_path = ensure_app_instance(job.project_id, app.id, workspace_root=workspace_root)
                log_path = instance_path / "logs" / f"{job.id}.log"
            else:
                log_path = app_workspace / "logs" / f"{job.id}.log"
            if job.release_id:
                if instance_path is None:
                    raise AppJobError("固定Releaseの実行にはProject App instanceが必要です")
                cwd = await _materialize_release_target(
                    app=app,
                    target=target,
                    release_id=job.release_id,
                    project_id=job.project_id,
                    instance_path=instance_path,
                    session=session,
                    workspace_root=workspace_root,
                    job_id=job.id,
                )
            job.status = "running"
            job.started_at = datetime.utcnow()
            job.log_path = str(log_path)
            await session.commit()
            input_json = job.input_json if isinstance(job.input_json, dict) else {}
            environment = {
                "AOITALK_APP_WORKSPACE": str(cwd),
                "AOITALK_APP_INSTANCE_DIR": str(instance_path) if instance_path else "",
            }
        except Exception as exc:
            job.status = "failed"
            job.result_json = {"error": str(exc)}
            job.ended_at = datetime.utcnow()
            await session.commit()
            _clear_job_cancellation(job_id)
            return job.to_dict()
    finally:
        if session is not None:
            await session.close()
        app_lock.release()

    # stop_running_job can win the race after the DB row was claimed as
    # running but before the subprocess thread reaches Popen.  The thread
    # checks the cancellation marker too, and this guard prevents a cancelled
    # row from being left running when no process was ever started.
    if _job_was_cancelled(job_id):
        session = await db_manager.get_session()
        try:
            current = await session.scalar(select(AppJob).where(AppJob.id == job_uuid).limit(1))
            if current is not None and current.status == "running":
                current.status = "cancelled"
                current.ended_at = datetime.utcnow()
                await session.commit()
            _clear_job_cancellation(job_id)
            return current.to_dict() if current is not None else None
        finally:
            await session.close()

    process_task: "asyncio.Task[dict[str, Any]] | None" = None
    try:
        process_task = asyncio.create_task(asyncio.to_thread(
            run_subprocess_job,
            job_id=job_id,
            command=command,
            cwd=cwd,
            log_path=log_path,
            input_json=input_json,
            timeout_seconds=timeout_seconds,
            environment=environment,
            config=deployment_config,
        ))
        # stop_running_job is intentionally process-local because it owns the
        # Popen handle.  Polling the durable status bridges API workers: a
        # stop request committed by another worker still reaches this worker
        # and terminates its process tree.
        while True:
            done, _ = await asyncio.wait({process_task}, timeout=JOB_STATUS_POLL_INTERVAL_SECONDS)
            if done:
                result = await process_task
                break
            try:
                poll_session = await db_manager.get_session()
                try:
                    persisted_status = await poll_session.scalar(
                        select(AppJob.status).where(AppJob.id == job_uuid).limit(1)
                    )
                finally:
                    await poll_session.close()
            except Exception:
                # A transient polling connection failure must not orphan the
                # subprocess or turn a successful command into a false error.
                continue
            if persisted_status in {None, "cancelled"}:
                # taskkill は同期で最大10秒かかるので event loop から逃がす。
                await asyncio.to_thread(stop_running_job, job_id)
    except BaseException as exc:
        # asyncio.CancelledError は BaseException 派生で ``except Exception`` に
        # 捕まらない。放置するとサブプロセスと worker task が生き残り、DB 行は
        # running のまま固着するため、キャンセルでも必ず後始末してから返す。
        await _stop_and_reap_process_task(job_id, process_task)
        if not isinstance(exc, Exception):
            _clear_job_cancellation(job_id)
            raise
        logger.warning("App job %s の実行監視が失敗しました", job_id, exc_info=True)
        result = {"status": "failed", "exit_code": None, "error": str(exc)}

    session = await db_manager.get_session()
    try:
        job = await session.scalar(
            select(AppJob).where(AppJob.id == job_uuid).with_for_update().limit(1)
        )
        if job is None:
            _clear_job_cancellation(job_id)
            return None
        if job.status != "cancelled":
            job.status = result["status"]
            job.exit_code = result.get("exit_code")
            job.result_json = {
                key: (str(value) if isinstance(value, Path) else value)
                for key, value in result.items()
                if key != "log_path"
            }
            job.ended_at = datetime.utcnow()
        await session.commit()
        _clear_job_cancellation(job_id)
        return job.to_dict()
    finally:
        await session.close()


async def execute_app_job(
    db_manager: Any,
    job_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    timeout_seconds: int = 900,
    deployment_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Execute a Job and reconcile its exact terminal runtime sandbox."""
    # If a process stopped after persisting a terminal Job but before its
    # finally cleanup, the next invocation can recover the exact runtime from
    # its durable in-directory owner marker before the job is inspected.
    await _cleanup_job_runtime_sandbox(
        db_manager,
        job_id,
        workspace_root=workspace_root,
    )
    try:
        return await _execute_app_job(
            db_manager,
            job_id,
            workspace_root=workspace_root,
            timeout_seconds=timeout_seconds,
            deployment_config=deployment_config,
        )
    finally:
        # This re-reads the durable row, so task cancellation or a retry race
        # cannot cause cleanup while the Job is still active or unknown.
        await _cleanup_job_runtime_sandbox(
            db_manager,
            job_id,
            workspace_root=workspace_root,
        )


__all__ = [
    "AppJobError",
    "execute_app_job",
    "run_subprocess_job",
    "stop_running_job",
]
