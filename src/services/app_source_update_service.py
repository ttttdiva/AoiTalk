"""既存 App workspace へ任意のフォルダ/ZIPを安全に更新するサービス。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from uuid import UUID

import yaml

from .app_git_service import AppGitService
from .app_manifest_service import (
    AppManifestError,
    load_app_manifest,
    parse_manifest_text,
    sync_manifest_targets_unlocked,
    validate_manifest_workspace,
)
from .app_operation_lock import app_operation_lock
from .app_source_import_service import generate_import_metadata
from .app_storage import (
    AppStorageError,
    DEFAULT_MANIFEST,
    README_TEMPLATE,
    get_app_workspace_path,
    get_workspaces_root,
    is_private_app_path,
    normalize_app_relative_path,
    resolve_app_file,
    sha256_file,
)


MAX_FILES = 5_000
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_TOTAL_SIZE = 250 * 1024 * 1024
IMPORT_TTL = timedelta(hours=2)
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# device_list.csv is required by the local Office workflow but contains
# credential columns. It may be staged and applied, but it is never exposed by
# the App file API and is ignored by App Git.
_PROTECTED_IMPORT_NAMES = {"device_list.csv"}
_REJECTED_IMPORT_NAMES = {"sync_credentials_from_memo.py", "業務備忘録.txt"}
_APP_ROOT_DIRECTORIES = {
    "src",
    "tests",
    "docs",
    "schemas",
    "targets",
    "macro",
    "xlsmビルド元",
    ".agents",
}
logger = logging.getLogger(__name__)


class AppSourceUpdateError(RuntimeError):
    """User-facing source update error."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _hash_bytes(path: Path) -> str:
    return sha256_file(path)


def _utcnow() -> datetime:
    """Return a naive UTC timestamp for the short-lived staging metadata."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_protected_path(path: str) -> bool:
    return Path(path).name.casefold() in {item.casefold() for item in _PROTECTED_IMPORT_NAMES}


def _is_rejected_path(path: str) -> bool:
    return Path(path).name.casefold() in {item.casefold() for item in _REJECTED_IMPORT_NAMES}


def _app_lock(app_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(app_id), threading.Lock())


def _safe_member_name(value: str) -> str:
    try:
        return normalize_app_relative_path(value)
    except AppStorageError as exc:
        raise AppSourceUpdateError(f"ZIP内のpathが不正です: {value}") from exc


def _is_symlink_member(member: zipfile.ZipInfo) -> bool:
    # Unix symlink mode is stored in the upper 16 bits of external_attr.
    return ((member.external_attr >> 16) & 0o170000) == 0o120000


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _is_untouched_starter_manifest(manifest: dict[str, object], app: object) -> bool:
    """Only replace the scaffold Manifest before any field was authored.

    比較対象の ``manifest`` は ``load_app_manifest`` が正規化した結果
    （``capabilities: []`` の補完、空 description の '' 化など）なので、
    テンプレートも同じ正規化を通してから比べる。素の ``yaml.safe_load`` と
    比較すると正規化差分で必ず False になり、この判定が死ぬ。
    """
    try:
        expected = parse_manifest_text(
            DEFAULT_MANIFEST.format(
                name=(str(getattr(app, "name", "AoiTalk App") or "AoiTalk App").strip() or "AoiTalk App").replace("\n", " "),
                description=(str(getattr(app, "description", "") or "").strip() or "").replace("\n", " "),
            )
        )
    except (AttributeError, TypeError, AppManifestError, yaml.YAMLError):
        return False
    return isinstance(expected, dict) and manifest == expected


def _is_untouched_starter_readme(workspace: Path, app: object) -> bool:
    readme = workspace / "README.md"
    if not readme.is_file() or readme.is_symlink():
        return False
    try:
        expected = README_TEMPLATE.format(
            name=str(getattr(app, "name", "AoiTalk App") or "AoiTalk App").strip() or "AoiTalk App",
            description=str(getattr(app, "description", "") or "").strip(),
        )
        return readme.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeError):
        return False


class AppSourceUpdateService:
    """Stage, preview and atomically apply App source updates.

    The source location is deliberately not persisted. Only a short-lived
    candidate under ``workspaces/cache`` is retained between preview/apply.
    """

    def __init__(self, *, workspace_root: str | os.PathLike[str] | None = None) -> None:
        self.workspace_root = workspace_root

    def _staging_root(self, app_id: str, import_id: str) -> Path:
        try:
            app_uuid = UUID(str(app_id))
            import_uuid = UUID(str(import_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AppSourceUpdateError("AppまたはimportのIDが不正です") from exc
        root = get_workspaces_root(self.workspace_root) / "cache" / "app_source_updates"
        path = (root / str(app_uuid) / str(import_uuid)).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _workspace_files(self, workspace: Path) -> dict[str, Path]:
        result: dict[str, Path] = {}
        if not workspace.exists():
            return result
        for path in workspace.rglob("*"):
            if path.is_symlink() or not path.is_file() or ".git" in {part.casefold() for part in path.relative_to(workspace).parts}:
                continue
            relative = path.relative_to(workspace).as_posix()
            parts = {part.casefold() for part in relative.split("/")}
            if parts.intersection({"node_modules", "venv", ".venv", "__pycache__", "dist", "build", "cache", "logs", "runtime data", "secrets"}):
                continue
            if is_private_app_path(relative) and not _is_protected_path(relative):
                continue
            result[relative.casefold()] = path
        return result

    def _extract_zip(self, archive_path: Path, destination: Path) -> None:
        try:
            archive = zipfile.ZipFile(archive_path)
        except zipfile.BadZipFile as exc:
            raise AppSourceUpdateError("ZIPファイルを読み込めません") from exc
        with archive:
            total_size = 0
            for member in archive.infolist():
                if _is_symlink_member(member):
                    raise AppSourceUpdateError("ZIP内のシンボリックリンクは取り込めません")
                if member.is_dir():
                    continue
                if member.file_size > MAX_FILE_SIZE:
                    raise AppSourceUpdateError("ZIP内ファイルのサイズが上限を超えています")
                total_size += member.file_size
                if total_size > MAX_TOTAL_SIZE:
                    raise AppSourceUpdateError("ZIP展開後の合計サイズが上限を超えています")
                relative = _safe_member_name(member.filename)
                target = (destination / relative).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError as exc:
                    raise AppSourceUpdateError("ZIPがstaging領域の外へ展開されます") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)

    def _normalize_incoming(self, root: Path, *, root_mode: str) -> tuple[dict[str, Path], list[dict[str, str]]]:
        files: list[tuple[str, Path]] = []
        for path in root.rglob("*"):
            if path.is_symlink():
                raise AppSourceUpdateError("シンボリックリンクは取り込めません")
            if path.is_file():
                files.append((path.relative_to(root).as_posix(), path))
        if not files:
            raise AppSourceUpdateError("取り込めるファイルがありません")

        normalized: list[tuple[str, Path]] = []
        for raw, path in files:
            relative = _safe_member_name(raw)
            if relative.casefold() == "readme.md":
                relative = "README.md"
            elif relative.casefold() == "aoitalk.app.yaml":
                relative = "aoitalk.app.yaml"
            normalized.append((relative, path))
        if root_mode == "strip_common" and normalized:
            first_parts = {path.split("/", 1)[0] for path, _ in normalized}
            common_root = next(iter(first_parts), "")
            if (
                len(first_parts) == 1
                and common_root.casefold() not in {item.casefold() for item in _APP_ROOT_DIRECTORIES}
                and all("/" in path for path, _ in normalized)
            ):
                normalized = [(path.split("/", 1)[1], source) for path, source in normalized]
        elif root_mode not in {"preserve", "strip_common"}:
            raise AppSourceUpdateError("root_mode が不正です")

        result: dict[str, Path] = {}
        rejected: list[dict[str, str]] = []
        seen: set[str] = set()
        total = 0
        for relative, source in normalized:
            key = relative.casefold()
            if key in seen:
                raise AppSourceUpdateError(f"同じpathが重複しています: {relative}")
            seen.add(key)
            size = source.stat().st_size
            total += size
            if size > MAX_FILE_SIZE or total > MAX_TOTAL_SIZE:
                raise AppSourceUpdateError("取り込みサイズが上限を超えています")
            if _is_rejected_path(relative):
                rejected.append({"path": relative, "reason": "Project外の認証情報参照を含む保守スクリプトです"})
                continue
            if is_private_app_path(relative) and not _is_protected_path(relative):
                rejected.append({"path": relative, "reason": "秘密情報またはruntime/private pathです"})
                continue
            result[relative] = source
        if len(result) > MAX_FILES:
            raise AppSourceUpdateError("取り込みファイル数が上限を超えています")
        if not result:
            raise AppSourceUpdateError("取り込めるファイルがありません")
        return result, rejected

    def _prepare_files(
        self,
        staging: Path,
        uploaded: Iterable[tuple[Path, str]],
        *,
        root_mode: str,
    ) -> tuple[dict[str, Path], list[dict[str, str]]]:
        incoming = staging / "incoming"
        expanded = staging / "expanded"
        incoming.mkdir(parents=True, exist_ok=True)
        expanded.mkdir(parents=True, exist_ok=True)
        total_upload_size = 0
        upload_count = 0
        for index, (source, relative_name) in enumerate(uploaded):
            upload_count += 1
            if upload_count > MAX_FILES:
                raise AppSourceUpdateError("取り込みファイル数が上限を超えています")
            try:
                upload_size = source.stat().st_size
            except OSError as exc:
                raise AppSourceUpdateError("アップロードファイルを読み込めません") from exc
            total_upload_size += upload_size
            if upload_size > MAX_TOTAL_SIZE or total_upload_size > MAX_TOTAL_SIZE:
                raise AppSourceUpdateError("アップロードサイズが上限を超えています")
            target = incoming / f"{index:05d}_{Path(relative_name).name or 'upload'}"
            _copy_file(source, target)
            # Only an explicitly dropped .zip is an import archive.  Office
            # files such as .xlsm are ZIP containers internally and must stay
            # intact as a single App source file.
            if Path(relative_name).suffix.casefold() == ".zip":
                self._extract_zip(target, expanded)
            else:
                relative = _safe_member_name(relative_name)
                destination = (expanded / relative).resolve()
                try:
                    destination.relative_to(expanded.resolve())
                except ValueError as exc:
                    raise AppSourceUpdateError("アップロードpathがstaging領域の外へ出ます") from exc
                _copy_file(target, destination)
        normalized, rejected = self._normalize_incoming(expanded, root_mode=root_mode)
        # ``strip_common`` changes the logical App path (for example
        # ``macro_FW申請処理/macro/foo.xlsm`` -> ``macro/foo.xlsm``).  Materialize
        # that normalized tree so preview/apply address the same files instead
        # of retaining the dropped folder name in staging.
        normalized_root = staging / "normalized"
        normalized_root.mkdir(parents=True, exist_ok=True)
        materialized: dict[str, Path] = {}
        for relative, source in normalized.items():
            target = (normalized_root / normalize_app_relative_path(relative)).resolve()
            try:
                target.relative_to(normalized_root.resolve())
            except ValueError as exc:
                raise AppSourceUpdateError("normalized pathがstaging外へ出ます") from exc
            _copy_file(source, target)
            materialized[relative] = target
        return materialized, rejected

    def create_preview(
        self,
        app_id: str,
        uploaded: Iterable[tuple[Path, str]],
        *,
        expected_revision: str | None = None,
        root_mode: str = "strip_common",
    ) -> dict[str, object]:
        import_id = str(uuid.uuid4())
        staging = self._staging_root(app_id, import_id)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            workspace = get_app_workspace_path(app_id, workspace_root=self.workspace_root)
            git = AppGitService(workspace_root=self.workspace_root)
            status = git.status(app_id)
            base_revision = status.get("revision")
            if expected_revision and expected_revision != base_revision:
                raise AppSourceUpdateError("Appのrevisionが変わっています。もう一度差分を確認してください", status_code=409)
            if status.get("dirty"):
                raise AppSourceUpdateError("App workspaceに未保存の変更があります。先にcheckpointしてください", status_code=409)
            incoming, rejected = self._prepare_files(staging, uploaded, root_mode=root_mode)
            current = self._workspace_files(workspace)
            current_files = sorted(
                (path.relative_to(workspace).as_posix() for path in current.values()),
                key=str.casefold,
            )
            entries: list[dict[str, object]] = []
            for relative, source in sorted(incoming.items(), key=lambda item: item[0].casefold()):
                existing = current.get(relative.casefold())
                incoming_hash = _hash_bytes(source)
                existing_hash = sha256_file(existing) if existing else None
                action = "unchanged" if existing_hash == incoming_hash else ("modify" if existing else "add")
                entries.append({
                    "path": relative,
                    "action": action,
                    "size_bytes": source.stat().st_size,
                    "incoming_sha256": incoming_hash,
                    "current_sha256": existing_hash,
                    "protected": _is_protected_path(relative),
                })
            manifest_entry = next((item for item in entries if str(item["path"]).casefold() == "aoitalk.app.yaml"), None)
            manifest_info: dict[str, object] = {"valid": True, "target_keys": []}
            if manifest_entry:
                manifest_text = next(source for path, source in incoming.items() if path.casefold() == "aoitalk.app.yaml").read_text(encoding="utf-8")
                try:
                    manifest = parse_manifest_text(manifest_text)
                    manifest_info["target_keys"] = list(manifest.get("targets", {}).keys())
                except (UnicodeDecodeError, AppManifestError) as exc:
                    manifest_info = {"valid": False, "error": str(exc), "target_keys": []}
            metadata = {
                "app_id": str(app_id),
                "import_id": import_id,
                "created_at": _utcnow().isoformat(),
                "base_revision": base_revision,
                "root_mode": root_mode,
                "files": [str(item["path"]) for item in entries if item["action"] != "unchanged"],
                "rejected": rejected,
                "manifest": manifest_info,
            }
            (staging / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            summary = {
                "added": sum(item["action"] == "add" for item in entries),
                "modified": sum(item["action"] == "modify" for item in entries),
                "unchanged": sum(item["action"] == "unchanged" for item in entries),
                "rejected": len(rejected),
            }
            return {
                "import_id": import_id,
                "base_revision": base_revision,
                "expires_at": (_utcnow() + IMPORT_TTL).isoformat(),
                "files": entries,
                "rejected": rejected,
                "summary": summary,
                "current_files": current_files,
                "manifest": manifest_info,
            }
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _load_metadata(self, app_id: str, import_id: str) -> tuple[Path, dict[str, object]]:
        try:
            parsed_id = UUID(import_id)
            if str(parsed_id) != import_id:
                raise ValueError
        except ValueError as exc:
            raise AppSourceUpdateError("import_id が不正です") from exc
        staging = self._staging_root(app_id, import_id)
        metadata_path = staging / "metadata.json"
        if not metadata_path.is_file():
            raise AppSourceUpdateError("差分プレビューが見つからないか期限切れです", status_code=404)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            created = datetime.fromisoformat(str(metadata.get("created_at")))
        except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AppSourceUpdateError("差分プレビューが壊れているか期限切れです", status_code=404) from exc
        if _utcnow() - created > IMPORT_TTL:
            shutil.rmtree(staging, ignore_errors=True)
            raise AppSourceUpdateError("差分プレビューの有効期限が切れています", status_code=410)
        if str(metadata.get("app_id")) != str(app_id):
            raise AppSourceUpdateError("Appとimportの組み合わせが不正です", status_code=403)
        return staging, metadata

    async def apply_async(
        self,
        app_id: str,
        import_id: str,
        *,
        expected_revision: str | None,
        delete_paths: Iterable[str] = (),
        session,
        app,
        app_service,
        actor: str = "app",
        actor_user_id: UUID | None = None,
    ) -> dict[str, object]:
        # Share the same per-App async lock as Manifest updates and Release
        # creation.  The old thread lock alone did not serialize those paths.
        async with app_operation_lock(app_id, workspace_root=self.workspace_root):
            return await self._apply_async_unlocked(
                app_id,
                import_id,
                expected_revision=expected_revision,
                delete_paths=delete_paths,
                session=session,
                app=app,
                app_service=app_service,
                actor=actor,
                actor_user_id=actor_user_id,
            )

    async def _apply_async_unlocked(
        self,
        app_id: str,
        import_id: str,
        *,
        expected_revision: str | None,
        delete_paths: Iterable[str] = (),
        session,
        app,
        app_service,
        actor: str = "app",
        actor_user_id: UUID | None = None,
    ) -> dict[str, object]:
        lock = _app_lock(app_id)
        if not lock.acquire(timeout=30):
            raise AppSourceUpdateError("Appが別の更新処理中です", status_code=409)
        staging: Path | None = None
        backups: dict[str, Path | None] = {}
        previous_revision: str | None = None
        checkpoint_revision: str | None = None
        try:
            staging, metadata = self._load_metadata(app_id, import_id)
            git = AppGitService(workspace_root=self.workspace_root)
            status = git.status(app_id)
            current_revision = status.get("revision")
            previous_revision = str(current_revision) if current_revision else None
            expected_revision = expected_revision or str(metadata.get("base_revision") or "")
            if expected_revision != current_revision or str(metadata.get("base_revision")) != str(current_revision):
                raise AppSourceUpdateError("Appのrevisionが変わっています。差分を再確認してください", status_code=409)
            if status.get("dirty"):
                raise AppSourceUpdateError("App workspaceに未保存の変更があります", status_code=409)
            rejected = metadata.get("rejected") or []
            if rejected:
                raise AppSourceUpdateError("取り込み拒否ファイルが残っています。対象から外して再確認してください")
            workspace = get_app_workspace_path(app_id, workspace_root=self.workspace_root)
            normalized_root = staging / "normalized"
            incoming_paths: list[str] = []
            for item in metadata.get("files", []):
                try:
                    incoming_paths.append(normalize_app_relative_path(str(item)))
                except AppStorageError as exc:
                    raise AppSourceUpdateError("プレビュー内の更新pathが不正です") from exc
            delete_normalized = [normalize_app_relative_path(item) for item in delete_paths]
            for relative in delete_normalized:
                if relative.casefold() in {"aoitalk.app.yaml", "readme.md"} or is_private_app_path(relative):
                    raise AppSourceUpdateError(f"削除できないpathです: {relative}")
            # Manifest and README can be generated when a starter App receives
            # its first real source update, so include both in the rollback
            # snapshot even when they were not part of the uploaded files.
            touched = set(incoming_paths) | set(delete_normalized) | {"aoitalk.app.yaml", "README.md"}
            backup_root = staging / "backup"
            for relative in touched:
                try:
                    target = resolve_app_file(app_id, relative, workspace_root=self.workspace_root)
                except AppStorageError as exc:
                    raise AppSourceUpdateError("更新pathがApp workspace外へ出ます") from exc
                if target.exists() and target.is_file():
                    backup = backup_root / relative
                    _copy_file(target, backup)
                    backups[relative] = backup
                else:
                    backups[relative] = None
            for relative in incoming_paths:
                source = (normalized_root / Path(relative)).resolve()
                try:
                    source.relative_to(normalized_root.resolve())
                except ValueError as exc:
                    raise AppSourceUpdateError("normalized pathがstaging外へ出ます") from exc
                if not source.is_file():
                    raise AppSourceUpdateError(f"incoming fileがありません: {relative}")
                destination = resolve_app_file(app_id, relative, workspace_root=self.workspace_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_file(source, destination)
            for relative in delete_normalized:
                target = resolve_app_file(app_id, relative, workspace_root=self.workspace_root)
                if target.exists() and target.is_file():
                    target.unlink()

            manifest, _text, _hash = load_app_manifest(workspace)
            generated_metadata = {"readme_generated": False}
            if not any(path.casefold() == "aoitalk.app.yaml" for path in incoming_paths) and _is_untouched_starter_manifest(manifest, app):
                workspace_files = [
                    path.relative_to(workspace).as_posix()
                    for path in self._workspace_files(workspace).values()
                ]
                metadata_files = sorted(set(workspace_files) | set(incoming_paths), key=str.casefold)
                replace_starter_readme = _is_untouched_starter_readme(workspace, app)
                generated_metadata = generate_import_metadata(
                    workspace=workspace,
                    app_name=getattr(app, "name", "AoiTalk App") or "AoiTalk App",
                    description=getattr(app, "description", "") or "",
                    source_path="D&D",
                    imported_files=metadata_files,
                    replace_starter_metadata=True,
                    replace_starter_readme=replace_starter_readme,
                )
                if not replace_starter_readme and not any(path.casefold() == "readme.md" for path in incoming_paths):
                    # Metadata generation must never overwrite a README that
                    # the user has already authored merely because a CSS or
                    # script file was updated later.
                    backup = backups.get("README.md")
                    if backup is not None and backup.is_file():
                        _copy_file(
                            backup,
                            resolve_app_file(app_id, "README.md", workspace_root=self.workspace_root),
                        )
                        # 既存 README を書き戻したときだけ「生成しなかった」ことにする。
                        # README が元から無かった場合は実際に新規生成しているので、
                        # ここで False にすると Docs node 同期がスキップされ、
                        # workspace と Docs が乖離する。
                        generated_metadata["readme_generated"] = False
                manifest, _text, _hash = load_app_manifest(workspace)
            validate_manifest_workspace(manifest, workspace)
            targets = await sync_manifest_targets_unlocked(session, app, workspace)
            node = None
            app.updated_at = _utcnow()
            revision = git.checkpoint(app_id, "App source D&D更新", actor=actor)
            checkpoint_revision = revision
            if any(path.casefold() == "readme.md" for path in incoming_paths) or generated_metadata.get("readme_generated"):
                node = await app_service.sync_readme_to_node(
                    session,
                    app,
                    actor_user_id or app.owner_user_id,
                )
            await session.commit()
            result = {
                "success": True,
                "revision": revision,
                "files": incoming_paths,
                "deleted": delete_normalized,
                "targets": [target.to_dict() for target in targets],
                "readme_node_id": str(node.id) if node is not None else str(app.readme_node_id) if app.readme_node_id else None,
            }
            shutil.rmtree(staging, ignore_errors=True)
            return result
        except Exception:
            await session.rollback()
            if (
                checkpoint_revision
                and previous_revision
                and checkpoint_revision != previous_revision
            ):
                try:
                    git.reset_to_revision(app_id, previous_revision)
                except Exception:
                    # Keep the original application error, but make the
                    # cross-store inconsistency visible for operational
                    # recovery instead of silently leaving a newer Git HEAD.
                    logger.exception(
                        "App source update compensation failed: app_id=%s previous_revision=%s checkpoint_revision=%s",
                        app_id,
                        previous_revision,
                        checkpoint_revision,
                    )
            if staging is not None:
                for relative, backup in backups.items():
                    try:
                        target = resolve_app_file(app_id, relative, workspace_root=self.workspace_root)
                        if backup is None:
                            if target.exists() and target.is_file():
                                target.unlink()
                        elif backup.exists():
                            _copy_file(backup, target)
                    except Exception:
                        # rollback 中の失敗で元の失敗原因を差し替えない。
                        # 1ファイル分の巻き戻しに失敗しても残りは戻し切る。
                        logger.exception(
                            "App source update rollback failed: app_id=%s path=%s",
                            app_id,
                            relative,
                        )
            raise
        finally:
            lock.release()


__all__ = ["AppSourceUpdateError", "AppSourceUpdateService"]
