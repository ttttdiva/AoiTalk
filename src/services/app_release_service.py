"""Release and artifact creation for Apps."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import App, AppArtifact, AppJob, AppRelease, AppTarget, User
from .app_git_service import AppGitError, AppGitService
from .app_job_execution_policy import (
    ServerJobExecutionDenied,
    assert_user_may_start_server_job,
)
from .app_job_service import AppJobError, run_subprocess_job
from .app_operation_lock import app_operation_lock
from .app_manifest_service import (
    AppManifestError,
    ValidationMode,
    load_app_manifest,
    validate_manifest_workspace,
)
from .app_storage import (
    APP_IGNORED_PATHS,
    AppStorageError,
    ensure_app_artifact,
    get_app_workspace_path,
    get_workspaces_root,
    is_private_app_path,
    is_sensitive_app_path,
    normalize_app_relative_path,
    resolve_app_file,
    resolve_workspace_file,
    sha256_file,
)


class AppReleaseError(ValueError):
    """Release precondition or packaging failure."""


def _release_temp_dir(prefix: str) -> Path:
    """Create ephemeral Release staging below the repository scratch root.

    Release packaging is a normal server operation, but the staging tree is
    still disposable task data.  Keeping it below ``.local`` avoids creating
    sibling directories outside the repository on Windows and in CI.
    """
    scratch_root = Path(__file__).resolve().parents[2] / ".local"
    scratch_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=scratch_root))


def _assert_release_inputs_unchanged(
    git: AppGitService,
    app: App,
    workspace: Path,
    *,
    revision: str,
    manifest_hash: str,
    readme_hash: str,
) -> tuple[dict[str, Any], str, str]:
    """Ensure preflight did not change the revision or release declaration.

    Build/test commands are untrusted App code.  A command that edits a
    tracked source, Manifest, or README after the initial checkpoint must not
    be allowed to produce a Release whose metadata points at a different
    source tree.
    """
    post_status = git.status(app.id)
    if not post_status.get("clean") or str(post_status.get("revision") or "") != str(revision):
        raise AppReleaseError(
            "Release preflight後にApp workspaceまたはGit revisionが変化しました。"
            "変更をcheckpointしてから再試行してください"
        )
    try:
        post_manifest, _post_text, post_manifest_hash = load_app_manifest(workspace)
    except AppManifestError as exc:
        raise AppReleaseError(str(exc)) from exc
    if post_manifest_hash != manifest_hash:
        raise AppReleaseError(
            "Release preflight後にManifestが変化しました。checkpoint後に再試行してください"
        )
    readme_path = resolve_workspace_file(workspace, "README.md")
    if not readme_path.exists() or sha256_file(readme_path) != readme_hash:
        raise AppReleaseError(
            "Release preflight後にREADME.mdが変化しました。checkpoint後に再試行してください"
        )
    return post_manifest, post_manifest_hash, readme_hash


async def _run_release_preflight(
    session: AsyncSession,
    app: App,
    targets: list[AppTarget],
    *,
    manifest: dict[str, Any],
    created_by: UUID,
    workspace: Path,
    deployment_config: dict[str, Any] | None = None,
) -> None:
    """Run manifest build/test commands and persist their durable job results."""
    starter = await session.scalar(select(User).where(User.id == created_by).limit(1))
    manifest_targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    for target in targets:
        snapshot = manifest_targets.get(target.target_key)
        if not isinstance(snapshot, dict):
            raise AppReleaseError(
                f"Release対象のTarget {target.target_key} がManifestにありません"
            )
        for job_type in ("build", "test"):
            raw = snapshot.get(job_type)
            command = raw if isinstance(raw, str) else raw.get("command") if isinstance(raw, dict) else None
            if not isinstance(command, str) or not command.strip():
                continue
            try:
                assert_user_may_start_server_job(
                    user_role=starter.role if starter else None,
                    config=deployment_config,
                )
            except ServerJobExecutionDenied as exc:
                raise AppReleaseError(str(exc)) from exc
            job = AppJob(
                app_id=app.id,
                target_id=target.id,
                job_type=job_type,
                status="queued",
                input_json={"release_preflight": True},
                started_by=created_by,
            )
            session.add(job)
            await session.flush()
            log_path = workspace / "logs" / f"release-preflight-{job.id}-{job_type}.log"
            job.status = "running"
            job.started_at = datetime.utcnow()
            job.log_path = str(log_path)
            await session.commit()
            try:
                result = await asyncio.to_thread(
                    run_subprocess_job,
                    job_id=job.id,
                    command=command,
                    cwd=workspace,
                    log_path=log_path,
                    input_json=job.input_json or {},
                    timeout_seconds=900,
                    environment={
                        "AOITALK_APP_WORKSPACE": str(workspace),
                        "AOITALK_APP_INSTANCE_DIR": "",
                    },
                    config=deployment_config,
                )
            except (AppJobError, OSError, subprocess.SubprocessError) as exc:
                result = {"status": "failed", "exit_code": None, "error": str(exc)}
            job.status = str(result.get("status") or "failed")
            job.exit_code = result.get("exit_code")
            job.result_json = {
                key: str(value) if isinstance(value, Path) else value
                for key, value in result.items()
                if key != "log_path"
            }
            job.ended_at = datetime.utcnow()
            await session.commit()
            if job.status != "succeeded":
                raise AppReleaseError(
                    f"{target.target_key} の {job_type} preflight に失敗しました"
                )


def _copy_tree_for_bundle(
    source: Path,
    destination: Path,
    *,
    include: set[str] | None = None,
    allow_ignored: bool = False,
) -> None:
    """Copy a validated subset of a workspace into a bundle staging dir.

    ``dist`` and ``build`` are ignored by ordinary App source operations, but
    a static target's immutable Runtime Bundle is deliberately built from the
    selected files inside one of those output directories.  The caller must
    opt in *and* provide an explicit include set for that exception; private
    files and symlinks remain rejected in every mode.
    """
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        relative_parts = {part.lower() for part in Path(relative).parts}
        if include is not None and relative not in include:
            continue
        if ".git" in relative_parts or (not allow_ignored and any(part in APP_IGNORED_PATHS for part in relative_parts)):
            continue
        if (is_sensitive_app_path(relative) if allow_ignored else is_private_app_path(relative)):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _zip_directory(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


async def create_app_release(
    session: AsyncSession,
    app: App,
    *,
    version: str,
    created_by: UUID,
    changelog: str = "",
    workspace_root: str | Path | None = None,
    deployment_config: dict[str, Any] | None = None,
) -> AppRelease:
    """Create one immutable Release while serializing App operations."""

    async with app_operation_lock(app.id, workspace_root=workspace_root):
        await session.scalar(
            select(App.id).where(App.id == app.id).with_for_update()
        )
        active_job = await session.scalar(
            select(AppJob.id)
            .where(
                AppJob.app_id == app.id,
                AppJob.status.in_({"queued", "running"}),
            )
            .limit(1)
        )
        if active_job is not None:
            raise AppReleaseError(
                "実行中または待機中のApp JobがあるためReleaseを作成できません。Jobを停止してから再試行してください"
            )
        return await _create_app_release_unlocked(
            session,
            app,
            version=version,
            created_by=created_by,
            changelog=changelog,
            workspace_root=workspace_root,
            deployment_config=deployment_config,
        )


async def _create_app_release_unlocked(
    session: AsyncSession,
    app: App,
    *,
    version: str,
    created_by: UUID,
    changelog: str = "",
    workspace_root: str | Path | None = None,
    deployment_config: dict[str, Any] | None = None,
) -> AppRelease:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version.strip()):
        raise AppReleaseError("version は英数字で始まる安全なタグ形式で指定してください")
    version = version.strip()
    workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
    git = AppGitService(workspace_root=workspace_root)
    try:
        status = git.status(app.id)
    except AppGitError as exc:
        raise AppReleaseError(str(exc)) from exc
    if not status.get("clean"):
        raise AppReleaseError("Release作成前に App workspace の変更を checkpoint してください")
    revision = status.get("revision")
    if not revision:
        raise AppReleaseError("Release対象の Git revision がありません")
    try:
        # ここは DRAFT のまま据え置く。preflight が build を走らせて成果物を作るため、
        # この時点で entrypoint / build.output が無いのは正常な状態である。
        manifest, manifest_text, manifest_hash = load_app_manifest(workspace)
    except AppManifestError as exc:
        raise AppReleaseError(str(exc)) from exc
    readme_path = resolve_workspace_file(workspace, "README.md")
    if not readme_path.exists():
        raise AppReleaseError("README.md がありません")
    readme_hash = sha256_file(readme_path)
    version = str(version or "").strip()
    if not version or len(version) > 80:
        raise AppReleaseError("version は必須です")
    duplicate = await session.scalar(
        select(AppRelease).where(AppRelease.app_id == app.id, AppRelease.version == version).limit(1)
    )
    if duplicate:
        raise AppReleaseError("同じ version の Release が既に存在します")

    targets = list((await session.scalars(
        select(AppTarget).where(AppTarget.app_id == app.id).order_by(AppTarget.target_key)
    )).all())
    if not targets:
        raise AppReleaseError("Release対象のTargetがありません")
    manifest_targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    if set(manifest_targets) != {target.target_key for target in targets}:
        raise AppReleaseError(
            "ManifestとApp Targetの派生データが一致しません。Target同期後に再試行してください"
        )
    for target in targets:
        snapshot = manifest_targets.get(target.target_key)
        if not isinstance(snapshot, dict):
            raise AppReleaseError(f"Target {target.target_key} のManifest定義が不正です")
        if any(
            getattr(target, field) != snapshot.get(field)
            for field in ("surface", "runtime", "execution_host", "entrypoint")
        ):
            raise AppReleaseError(
                f"ManifestとApp Target {target.target_key} の派生データが一致しません。"
                "Target同期後に再試行してください"
            )
    await _run_release_preflight(
        session,
        app,
        targets,
        manifest=manifest,
        created_by=created_by,
        workspace=workspace,
        deployment_config=deployment_config,
    )

    # Preflight may run arbitrary App-owned build/test code.  Re-read the
    # canonical Manifest and Git state after it completes, then require the
    # same checkpoint/hash that was captured before preflight.
    manifest, manifest_hash, readme_hash = _assert_release_inputs_unchanged(
        git,
        app,
        workspace,
        revision=str(revision),
        manifest_hash=manifest_hash,
        readme_hash=readme_hash,
    )

    # build 実行後に entrypoint / build.output の実在を必須化する。
    # validate API・Target同期と同じ検証関数で、モードだけをSTRICTへ切り替える。
    try:
        validate_manifest_workspace(manifest, workspace, mode=ValidationMode.STRICT)
    except AppManifestError as exc:
        raise AppReleaseError(str(exc)) from exc

    release = AppRelease(
        app_id=app.id,
        version=version,
        git_revision=str(revision),
        manifest_hash=manifest_hash,
        readme_hash=readme_hash,
        changelog=changelog.strip() or None,
        status="published",
        created_by=created_by,
    )
    session.add(release)
    await session.flush()
    artifact_root = ensure_app_artifact(app.id, release.id, workspace_root=workspace_root)

    root = get_workspaces_root(workspace_root)
    source_staging = _release_temp_dir("aoitalk-app-source-")
    try:
        _copy_tree_for_bundle(workspace, source_staging)
        source_zip = artifact_root / "source-bundle.zip"
        _zip_directory(source_staging, source_zip)
    finally:
        shutil.rmtree(source_staging, ignore_errors=True)
    source_artifact = AppArtifact(
        release_id=release.id,
        target_id=targets[0].id,
        artifact_type="source_bundle",
        file_path=source_zip.relative_to(root).as_posix(),
        filename=source_zip.name,
        sha256=sha256_file(source_zip),
        size_bytes=source_zip.stat().st_size,
    )
    session.add(source_artifact)

    for target in targets:
        staging = _release_temp_dir(f"aoitalk-app-{target.target_key}-")
        try:
            snapshot = manifest_targets.get(target.target_key)
            if not isinstance(snapshot, dict):
                raise AppReleaseError(f"Target {target.target_key} のManifest定義が不正です")
            build = snapshot.get("build") if isinstance(snapshot.get("build"), dict) else {}
            output = build.get("output") if isinstance(build.get("output"), str) else None
            if output:
                try:
                    output_path = resolve_app_file(app.id, output, workspace_root=workspace_root)
                except AppStorageError as exc:
                    raise AppReleaseError(str(exc)) from exc
                if not output_path.exists() or not output_path.is_dir():
                    raise AppReleaseError(
                        f"{target.target_key} のbuild.outputが存在しないかディレクトリではありません: {output}"
                    )
                try:
                    output_entrypoint = PurePosixPath(
                        normalize_app_relative_path(target.entrypoint)
                    ).relative_to(PurePosixPath(normalize_app_relative_path(output)))
                except (AppStorageError, ValueError) as exc:
                    raise AppReleaseError(
                        f"{target.target_key} のentrypointがbuild.output配下にありません"
                    ) from exc
                if not (output_path / Path(output_entrypoint)).is_file():
                    raise AppReleaseError(
                        f"{target.target_key} のbuild.outputにentrypointがありません: {target.entrypoint}"
                    )
                _copy_tree_for_bundle(output_path, staging)
            elif target.runtime == "static_web":
                # Preserve the entrypoint directory so relative CSS, JS and
                # image URLs remain valid in the immutable Runtime Bundle.
                entrypoint = normalize_app_relative_path(target.entrypoint)
                try:
                    entrypoint_file = resolve_app_file(app.id, entrypoint, workspace_root=workspace_root)
                except AppStorageError as exc:
                    raise AppReleaseError(str(exc)) from exc
                entrypoint_dir = entrypoint_file.parent.resolve()
                try:
                    entrypoint_dir.relative_to(workspace.resolve())
                except ValueError as exc:
                    raise AppReleaseError("static_web entrypoint がApp workspace外です") from exc
                include = {
                    path.relative_to(workspace).as_posix()
                    for path in entrypoint_dir.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                include.update({target.entrypoint, "README.md", "aoitalk.app.yaml"})
                _copy_tree_for_bundle(workspace, staging, include=include, allow_ignored=True)
            elif target.execution_host in {"aoitalk", "server"}:
                # Server-side Actions may invoke a generated script that
                # imports sibling modules or reads a checked-in config file.
                # Bundle the safe source tree for this target while retaining
                # the normal private/runtime exclusions.  The immutable
                # Runtime Bundle is still bounded by the App workspace root.
                _copy_tree_for_bundle(workspace, staging)
            else:
                _copy_tree_for_bundle(workspace, staging, include={target.entrypoint, "README.md", "aoitalk.app.yaml"})
            if not any(path.is_file() for path in staging.rglob("*")):
                raise AppReleaseError(f"{target.target_key} のRuntime Bundleにファイルがありません")
            runtime_zip = artifact_root / f"runtime-{target.target_key}.zip"
            _zip_directory(staging, runtime_zip)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        session.add(
            AppArtifact(
                release_id=release.id,
                target_id=target.id,
                artifact_type="runtime_bundle",
                file_path=runtime_zip.relative_to(root).as_posix(),
                filename=runtime_zip.name,
                sha256=sha256_file(runtime_zip),
                size_bytes=runtime_zip.stat().st_size,
            )
        )

    try:
        git.create_release_tag(app.id, f"app/{app.slug}/v{version}", str(revision))
    except AppGitError as exc:
        raise AppReleaseError(str(exc)) from exc
    app.updated_at = datetime.utcnow()
    await session.flush()
    return release


__all__ = ["AppReleaseError", "create_app_release"]
