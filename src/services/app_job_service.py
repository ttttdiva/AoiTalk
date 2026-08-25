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
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ..memory.models import App, AppArtifact, AppJob, AppRelease, AppTarget, ProjectApp, User
from .app_service import AppService, permission_at_least
from .app_manifest_service import AppManifestError, parse_manifest_text
from .app_operation_lock import AppOperationLockError, app_operation_lock
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
    env["AOITALK_APP_INPUT_JSON"] = json.dumps(input_json or {}, ensure_ascii=False)
    env["AOITALK_APP_RUNNER"] = runner_env_marker()
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
    runtime_root = (instance_path / ".runtime" / str(job_id)).resolve()
    instance_root = instance_path.resolve()
    try:
        runtime_root.relative_to(instance_root)
    except ValueError as exc:
        raise AppJobError("Release runtime path がProject App instance外です") from exc
    # rmtree + zip 全展開は Release サイズに比例した同期 I/O で、しかもここは
    # App operation lock と App/AppJob 行ロックを保持中。event loop 上で回すと
    # 同一プロセスの他リクエストごと止まるため、必ずスレッドへ逃がす。
    await asyncio.to_thread(
        _extract_release_bundle,
        artifact_path=artifact_path,
        runtime_root=runtime_root,
    )
    return runtime_root


def _extract_release_bundle(*, artifact_path: Path, runtime_root: Path) -> None:
    """Blocking part of release materialization (rmtree + zip extraction)."""
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            for member in archive.infolist():
                try:
                    relative = normalize_app_relative_path(member.filename)
                except ValueError:
                    raise AppJobError("Release Artifactに不正なpathがあります") from None
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
                env=_safe_env(input_json, environment),
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


async def execute_app_job(
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


__all__ = [
    "AppJobError",
    "execute_app_job",
    "run_subprocess_job",
    "stop_running_job",
]
