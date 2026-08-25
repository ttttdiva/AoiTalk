"""App専用 filesystem 境界。

App の source / Project instance / release artifact は Project workspace と別の
namespaceに保存する。すべての公開 helper は UUID と canonical relative path を
検証し、resolve 後に所属 root を再確認するため、別 App/Project や traversal を
越境できない。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from uuid import UUID


logger = logging.getLogger(__name__)


README_TEMPLATE = """# {name}

## 目的
{description}

## 利用者

## Target一覧

## 入力

## 出力

## 実行方法

## Projectとの関係

## 必要権限

## 開発方法

## Build手順

## Test手順

## 制約

## 既知の問題

## リリース履歴
"""

DEFAULT_MANIFEST = """schema_version: 1
name: {name}
description: {description}
overview:
  purpose: {description}
  audience: Appを利用する業務担当者と開発担当者
  input:
    label: 業務データ
    detail: 利用者が指定した入力データを受け取ります。
  process:
    label: 業務データ処理
    detail: 入力データを業務ルールに沿って処理します。
  output:
    label: 処理結果・成果物
    detail: 処理結果または生成ファイルを確認します。
  steps:
    - 入力データまたは対象ファイルを用意する
    - Appの処理を実行する
    - 処理結果と出力ファイルを確認する
  method: starter
  confidence: 0.2
targets:
  aoitalk:
    display_name: AoiTalk
    surface: embedded_web
    runtime: static_web
    execution_host: aoitalk
    entrypoint: src/index.html
"""

APP_IGNORED_PATHS = {
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "cache",
    "logs",
    "runtime data",
    "secrets",
}
_PRIVATE_FILE_NAMES = {
    "credentials",
    "credential",
    # The VBA workflow uses this file as a local credential source.  It may
    # exist in an App workspace for the local build/run flow, but it must not
    # cross the App file API or be committed to App Git.
    "device_list.csv",
    "業務備忘録.txt",
    "id_rsa",
    "id_ed25519",
}
_PRIVATE_FILE_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".secret", ".secrets")
# 書き込み保護 file。private（読み書き・Git 管理から除外）ではなく、
# 「Git 管理対象のまま App file API / Source Bundle からは上書き・削除できない」
# という別カテゴリとして扱う。`.gitignore` を private にしてしまうと secrets や
# runtime data を Git から締め出す唯一の防壁が Git 管理外になるため、
# protected として正本ルールをこちら側で維持する。
_PROTECTED_FILE_NAMES = {".gitignore"}

# App Git が必ず持っていなければならない ignore ルール。既存 workspace に
# 欠落があれば ``ensure_app_gitignore`` が安全側へ補正する。
REQUIRED_APP_IGNORE_RULES: tuple[str, ...] = (
    "node_modules/",
    "venv/",
    ".venv/",
    "__pycache__/",
    "dist/",
    "build/",
    "cache/",
    "logs/",
    "runtime data/",
    "secrets/",
    "**/device_list.csv",
    "**/業務備忘録.txt",
    ".env",
    ".env.*",
    "*.env",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.secret",
    "*.secrets",
)
APP_GITIGNORE_TEXT = "\n".join(REQUIRED_APP_IGNORE_RULES) + "\n"

_TEXT_FILE_NAMES = {"dockerfile", "makefile", ".gitignore"}
_TEXT_FILE_SUFFIXES = {
    ".bas", ".bat", ".cfg", ".cls", ".cmd", ".conf", ".css", ".csv",
    ".frm", ".htm", ".html", ".ini", ".js", ".json", ".jsx", ".log",
    ".md", ".mjs", ".ps1", ".py", ".sh", ".sql", ".toml", ".ts",
    ".tsx", ".tsv", ".txt", ".vba", ".xml", ".yaml", ".yml",
}
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


class AppStorageError(ValueError):
    """App storage path or namespace validation failure."""


def get_workspaces_root(workspace_root: str | os.PathLike[str] | None = None) -> Path:
    value = workspace_root or os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    root = Path(value).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _parse_id(value: str | UUID, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AppStorageError(f"{label} は UUID で指定してください") from exc


def _under(root: Path, target: Path, *, allow_root: bool = True) -> Path:
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        relative = target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise AppStorageError("storage root の外側へアクセスできません") from exc
    if not allow_root and not relative.parts:
        raise AppStorageError("storage root 自体は対象にできません")
    return target_resolved


def _is_storage_link_or_reparse(path: Path) -> bool:
    """Reject POSIX symlinks and Windows junction/reparse entries."""
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


def _reject_symlink_components(root: Path, relative: str) -> None:
    """Reject symlink aliases even when they resolve back inside the namespace.

    Checking only the final resolved path is insufficient: ``public.txt`` can
    point at ``.env`` inside the same App workspace and would otherwise bypass
    the private-name checks applied to the user supplied path.
    """
    current = root
    for component in Path(relative).parts:
        current = current / component
        if _is_storage_link_or_reparse(current):
            raise AppStorageError("シンボリックリンク経由のstorageアクセスは許可されません")


def _namespace_path(root: Path, namespace: str, relative: str) -> Path:
    """Resolve a storage namespace without allowing namespace aliases."""
    namespace_root = root / namespace
    if _is_storage_link_or_reparse(namespace_root):
        raise AppStorageError("storage namespace のシンボリックリンクは許可されません")
    _reject_symlink_components(namespace_root, relative)
    return _under(namespace_root, namespace_root / Path(relative))


def get_app_workspace_path(
    app_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    app_uuid = _parse_id(app_id, "app_id")
    root = get_workspaces_root(workspace_root)
    return _namespace_path(root, "_apps", f"app_{app_uuid}")


def get_app_instance_path(
    project_id: str | UUID,
    app_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    project_uuid = _parse_id(project_id, "project_id")
    app_uuid = _parse_id(app_id, "app_id")
    root = get_workspaces_root(workspace_root)
    return _namespace_path(
        root,
        "_app_instances",
        f"project_{project_uuid}/app_{app_uuid}",
    )


def get_app_artifact_path(
    app_id: str | UUID,
    release_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    app_uuid = _parse_id(app_id, "app_id")
    release_uuid = _parse_id(release_id, "release_id")
    root = get_workspaces_root(workspace_root)
    return _namespace_path(
        root,
        "_app_artifacts",
        f"app_{app_uuid}/release_{release_uuid}",
    )


def ensure_app_workspace(
    app_id: str | UUID,
    *,
    name: str = "AoiTalk App",
    description: str = "",
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    workspace = get_app_workspace_path(app_id, workspace_root=workspace_root)
    workspace.mkdir(parents=True, exist_ok=True)
    for directory in ("src", "tests", "docs", "schemas", "targets", ".agents"):
        (workspace / directory).mkdir(parents=True, exist_ok=True)
    index = resolve_workspace_file(workspace, "src/index.html")
    if not index.exists():
        index.write_text(
            "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\"><title>AoiTalk App</title></head><body><main><h1>AoiTalk App</h1><p>この App の UI を開発してください。</p></main></body></html>\n",
            encoding="utf-8",
            newline="\n",
        )
    readme = resolve_workspace_file(workspace, "README.md")
    if not readme.exists():
        readme.write_text(
            README_TEMPLATE.format(
                name=name.strip() or "AoiTalk App",
                description=description.strip() or "",
            ),
            encoding="utf-8",
            newline="\n",
        )
    manifest = resolve_workspace_file(workspace, "aoitalk.app.yaml")
    if not manifest.exists():
        manifest.write_text(
            DEFAULT_MANIFEST.format(
                name=(name.strip() or "AoiTalk App").replace("\n", " "),
                description=(description.strip() or "").replace("\n", " "),
            ),
            encoding="utf-8",
            newline="\n",
        )
    gitignore = resolve_workspace_file(workspace, ".gitignore")
    if not gitignore.exists():
        gitignore.write_text(APP_GITIGNORE_TEXT, encoding="utf-8", newline="\n")
    return workspace


def ensure_app_instance(
    project_id: str | UUID,
    app_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    instance = get_app_instance_path(project_id, app_id, workspace_root=workspace_root)
    for directory in ("data", "input", "output", "logs"):
        (instance / directory).mkdir(parents=True, exist_ok=True)
    return instance


def ensure_app_artifact(
    app_id: str | UUID,
    release_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    artifact = get_app_artifact_path(app_id, release_id, workspace_root=workspace_root)
    artifact.mkdir(parents=True, exist_ok=True)
    return artifact


def normalize_app_relative_path(value: str, *, allow_empty: bool = False) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        if allow_empty:
            return ""
        raise AppStorageError("相対パスを指定してください")
    if raw.startswith(("/", "//")) or _DRIVE_PATH.match(raw):
        raise AppStorageError("絶対パスは指定できません")
    parts = tuple(part for part in raw.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise AppStorageError("path traversal は許可されていません")
    if any("\x00" in part for part in parts):
        raise AppStorageError("NUL を含むパスは指定できません")
    if any(part.lower() == ".git" for part in parts):
        raise AppStorageError(".git は App file API から操作できません")
    return "/".join(parts)


def canonical_app_source_path(value: str) -> str:
    """Normalize the two manifest/document filenames used as App roots.

    Source imports can come from Windows or ZIP files whose filenames differ
    only by case.  Keep the workspace names stable so README and Manifest
    lookup never depends on the host filesystem's case-sensitivity.
    """
    normalized = normalize_app_relative_path(value)
    if normalized.casefold() == "readme.md":
        return "README.md"
    if normalized.casefold() == "aoitalk.app.yaml":
        return "aoitalk.app.yaml"
    return normalized


def is_private_app_path(value: str) -> bool:
    """Return whether a path is runtime-private and must not be exposed."""
    try:
        normalized = normalize_app_relative_path(value)
    except (TypeError, ValueError, AppStorageError):
        return True
    parts = [part.lower() for part in normalized.split("/")]
    if any(part in APP_IGNORED_PATHS or part == ".git" for part in parts):
        return True
    return is_credential_app_path(normalized)


def is_credential_app_path(value: str) -> bool:
    """Return whether a path contains credentials or locally managed secrets.

    一式ダウンロードでは、credential を含めるかどうかを利用者がGUIで選ぶ。
    source/file API の既存private判定はこの分類を内包したまま維持する。
    """
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw.startswith(("/", "//")) or _DRIVE_PATH.match(raw):
        return True
    parts_raw = tuple(part for part in raw.split("/") if part)
    if not parts_raw or any(part in {".", ".."} or "\x00" in part for part in parts_raw):
        return True
    normalized = "/".join(parts_raw)
    parts = [part.lower() for part in normalized.split("/")]
    if "secrets" in parts:
        return True
    filename = parts[-1]
    return (
        filename in _PRIVATE_FILE_NAMES
        or filename == ".env"
        or filename.startswith(".env.")
        or filename.endswith(_PRIVATE_FILE_SUFFIXES)
    )


def is_protected_app_path(value: str) -> bool:
    """Return whether a path is write-protected while staying under Git control.

    ``private`` とは意味が違う。private は「App からも Git からも見せない」
    だが、protected は「Git 管理対象として必ず残すが、App file API や
    Source Bundle からは上書き・削除させない」を意味する。現在の対象は
    `.gitignore` のみで、階層を問わず保護する（下位 `.gitignore` の否定
    ルールで上位の secrets 除外を無効化できてしまうため）。
    """
    try:
        normalized = normalize_app_relative_path(value)
    except (TypeError, ValueError, AppStorageError):
        # 判定できない path は deny 側に倒す。呼び出し側の正規化で先に弾かれる。
        return True
    return normalized.rsplit("/", 1)[-1].casefold() in _PROTECTED_FILE_NAMES


def missing_app_ignore_rules(text: str) -> list[str]:
    """Return the required ignore rules that are absent from ``.gitignore`` text."""
    existing = {line.strip() for line in str(text or "").splitlines() if line.strip()}
    return [rule for rule in REQUIRED_APP_IGNORE_RULES if rule not in existing]


def ensure_app_gitignore(workspace: str | os.PathLike[str]) -> list[str]:
    """必須 ignore ルールを検査し、欠落分だけを追記する。

    旧実装や外部から取り込んだ workspace は `.gitignore` が無い / 弱い
    ことがあり、そのまま checkpoint すると secrets や runtime data が App Git
    に入ってしまう。既存の記述は消さず末尾へ追記するだけにして、利用者が
    足した独自ルールを壊さない。追記したルールを返す（無変更なら空リスト）。
    """
    path = resolve_workspace_file(Path(workspace), ".gitignore")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(APP_GITIGNORE_TEXT, encoding="utf-8", newline="\n")
        return list(REQUIRED_APP_IGNORE_RULES)
    # newline="" で読み書きし、既存行の改行コードを書き換えない。
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        current = handle.read()
    missing = missing_app_ignore_rules(current)
    if not missing:
        return []
    separator = "" if not current or current.endswith(("\n", "\r")) else "\n"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(current + separator + "\n".join(missing) + "\n")
    return missing


def is_sensitive_app_path(value: str) -> bool:
    """Return whether a path must never be served by an embedded App asset route."""
    try:
        normalized = normalize_app_relative_path(value)
    except (TypeError, ValueError, AppStorageError):
        return True
    parts = [part.lower() for part in normalized.split("/")]
    if any(part in {".git", "logs", "secrets", "runtime data"} for part in parts):
        return True
    return is_credential_app_path(normalized)


def is_embedded_app_path(value: str, *, allow_build_output: bool = False) -> bool:
    """Return whether an App-relative path is unsafe for embedded delivery.

    ``dist`` and ``build`` are ignored by source browsing/Git, but they are
    valid published static-web output directories.  The embedded route opts
    into that narrow exception while keeping secrets, logs, runtime data,
    symlink traversal, and dependency directories blocked.
    """
    try:
        normalized = normalize_app_relative_path(value)
    except (TypeError, ValueError, AppStorageError):
        return True
    parts = [part.lower() for part in normalized.split("/")]
    ignored_parts = APP_IGNORED_PATHS - ({"dist", "build"} if allow_build_output else set())
    if any(part in ignored_parts or part in {".git", "node_modules"} for part in parts):
        return True
    return is_sensitive_app_path(normalized)


def is_text_app_path(value: str) -> bool:
    """Return whether the App content API may decode a path as UTF-8 text."""
    try:
        normalized = normalize_app_relative_path(value)
    except (TypeError, ValueError, AppStorageError):
        return False
    filename = normalized.rsplit("/", 1)[-1].casefold()
    return filename in _TEXT_FILE_NAMES or Path(filename).suffix in _TEXT_FILE_SUFFIXES


def resolve_app_file(
    app_id: str | UUID,
    relative_path: str,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    relative = normalize_app_relative_path(relative_path)
    workspace = get_app_workspace_path(app_id, workspace_root=workspace_root)
    _reject_symlink_components(workspace, relative)
    return _under(workspace, workspace / Path(relative))


def resolve_workspace_file(workspace: Path, relative_path: str) -> Path:
    """Resolve a file under an already selected workspace without symlink aliases."""
    relative = normalize_app_relative_path(relative_path)
    workspace = Path(workspace)
    _reject_symlink_components(workspace, relative)
    return _under(workspace, workspace / Path(relative))


def resolve_app_instance_path(
    project_id: str | UUID,
    app_id: str | UUID,
    relative_path: str,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    relative = normalize_app_relative_path(relative_path)
    instance = get_app_instance_path(project_id, app_id, workspace_root=workspace_root)
    _reject_symlink_components(instance, relative)
    return _under(instance, instance / Path(relative))


def resolve_app_artifact_file(
    app_id: str | UUID,
    release_id: str | UUID,
    relative_path: str,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    relative = normalize_app_relative_path(relative_path)
    artifact = get_app_artifact_path(app_id, release_id, workspace_root=workspace_root)
    _reject_symlink_components(artifact, relative)
    return _under(artifact, artifact / Path(relative))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file_integrity(
    path: Path,
    *,
    expected_sha256: str | None,
    expected_size_bytes: int | None,
) -> None:
    """Verify a persisted Release artifact before it is read or served."""
    if not path.is_file():
        raise AppStorageError("Artifact file がありません")
    if expected_size_bytes is not None and path.stat().st_size != int(expected_size_bytes):
        raise AppStorageError("Artifact size が保存済みメタデータと一致しません")
    if expected_sha256 and sha256_file(path).casefold() != str(expected_sha256).casefold():
        raise AppStorageError("Artifact SHA-256 が保存済みメタデータと一致しません")


def list_app_files(
    app_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, object]]:
    workspace = get_app_workspace_path(app_id, workspace_root=workspace_root)
    if not workspace.exists():
        return []
    result: list[dict[str, object]] = []
    for path in sorted(workspace.rglob("*")):
        lower_parts = {part.lower() for part in path.parts}
        if ".git" in lower_parts or lower_parts.intersection(APP_IGNORED_PATHS):
            continue
        if _is_storage_link_or_reparse(path):
            continue
        if not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(workspace.resolve())
        except (OSError, ValueError):
            continue
        relative = path.relative_to(workspace).as_posix()
        if is_private_app_path(relative):
            continue
        result.append(
            {
                "path": relative,
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return result


def normalize_app_bundle_member(name: str) -> str | None:
    """Return the canonical workspace path for a Source Bundle member.

    取り込み対象外（``.git`` 配下・private/runtime file・書き込み保護 file）は
    ``None`` を返してスキップさせる。絶対 path や ``..`` を含む member は
    不正な Bundle なので ``AppStorageError`` にして取り込み自体を止める。
    """
    raw = str(name or "").replace("\\", "/").strip()
    if not raw:
        return None
    if raw.startswith("/") or _DRIVE_PATH.match(raw):
        raise AppStorageError("Source Bundle に絶対パスのentryがあります")
    parts = tuple(part for part in raw.split("/") if part)
    if not parts:
        return None
    if any(part in {".", ".."} for part in parts):
        raise AppStorageError("Source Bundle に path traversal のentryがあります")
    if any("\x00" in part for part in parts):
        raise AppStorageError("Source Bundle に NUL を含むentryがあります")
    if any(part.lower() == ".git" for part in parts):
        return None
    normalized = canonical_app_source_path("/".join(parts))
    if is_private_app_path(normalized) or is_protected_app_path(normalized):
        return None
    return normalized


def iter_app_source_files(
    workspace: str | os.PathLike[str],
    *,
    skip_protected: bool = True,
) -> Iterator[tuple[Path, str]]:
    """Yield ``(absolute_path, relative_posix)`` for copyable App source files.

    ``.git``・private/runtime 領域・symlink（symlink ディレクトリ配下を含む）は
    必ず除外するため、Fork や Bundle 生成が別 App の領域や secrets を巻き込む
    ことがない。``skip_protected`` が真なら `.gitignore` も除外する。コピー先の
    `.gitignore` は常に ``ensure_app_gitignore`` が正本ルールで作り直す。
    """
    root = Path(workspace)
    if not root.is_dir():
        return
    root_resolved = root.resolve()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in sorted(directories)
            if name.lower() != ".git"
            and name.lower() not in APP_IGNORED_PATHS
            and not (current_path / name).is_symlink()
        ]
        for filename in sorted(filenames):
            path = current_path / filename
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if is_private_app_path(relative):
                continue
            if skip_protected and is_protected_app_path(relative):
                continue
            try:
                path.resolve(strict=True).relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            yield path, relative


class AppWorkspaceJournal:
    """App workspace 変更の補償(rollback)ジャーナル。

    変更前のファイルを同一 filesystem 上の backup 領域へ ``move`` し、
    ``rollback()`` で記録を逆順にたどって元の場所へ ``move`` し戻す。
    変更前に存在しなかった path は rollback で削除するので、失敗した更新の
    痕跡が workspace に残らない。Windows ではディレクトリ全体の atomic rename
    が使えないため、ファイル単位の move を journal で束ねてこの性質を作る。
    """

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        backup_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._backup_root = (
            Path(backup_root) if backup_root is not None else self._workspace.parent
        )
        self._backup_dir: Path | None = None
        self._entries: list[tuple[str, bool]] = []
        self._stashed: set[str] = set()

    @property
    def entries(self) -> tuple[tuple[str, bool], ...]:
        return tuple(self._entries)

    def _ensure_backup_dir(self) -> Path:
        if self._backup_dir is None:
            self._backup_root.mkdir(parents=True, exist_ok=True)
            self._backup_dir = Path(
                tempfile.mkdtemp(prefix=".app-rollback-", dir=self._backup_root)
            )
        return self._backup_dir

    def stash(self, *relative_paths: str) -> None:
        """変更前の状態を退避する。存在しない path も「無かった」事実として記録する。"""
        for relative in relative_paths:
            normalized = normalize_app_relative_path(relative)
            if normalized in self._stashed:
                continue
            target = resolve_workspace_file(self._workspace, normalized)
            existed = target.is_file()
            if existed:
                backup = self._ensure_backup_dir() / Path(normalized)
                backup.parent.mkdir(parents=True, exist_ok=True)
                _move_file(target, backup)
            self._entries.append((normalized, existed))
            self._stashed.add(normalized)

    def rollback(self) -> None:
        """記録を逆順にたどって workspace を変更前の状態へ戻す。"""
        for normalized, existed in reversed(self._entries):
            target = self._workspace / Path(normalized)
            try:
                if existed:
                    if self._backup_dir is None:
                        continue
                    backup = self._backup_dir / Path(normalized)
                    if not backup.is_file():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _move_file(backup, target)
                elif target.is_file():
                    target.unlink()
            except OSError:
                logger.exception("App workspace rollback failed: %s", normalized)
        self._entries.clear()
        self._stashed.clear()

    def close(self) -> None:
        if self._backup_dir is not None:
            shutil.rmtree(self._backup_dir, ignore_errors=True)
            self._backup_dir = None

    def __enter__(self) -> "AppWorkspaceJournal":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            self.rollback()
        self.close()
        return False


def _move_file(source: Path, destination: Path) -> None:
    """Move one file, falling back to copy+unlink across filesystems."""
    try:
        os.replace(source, destination)
    except OSError:
        shutil.copy2(source, destination)
        source.unlink()


def stage_app_source_bundle(
    archive_path: str | os.PathLike[str],
    staging: str | os.PathLike[str],
    *,
    max_files: int = 5000,
    max_file_bytes: int = 50 * 1024 * 1024,
    max_total_bytes: int = 100 * 1024 * 1024,
) -> list[str]:
    """Source Bundle を staging へ展開し、取り込む相対 path を返す。

    本番 workspace には一切触れないので、ここで失敗しても App は無傷。
    ``.git``・private/runtime file・書き込み保護 file は展開段階で落とす。
    """
    staging_root = Path(staging)
    staging_root.mkdir(parents=True, exist_ok=True)
    imported: list[str] = []
    seen: set[str] = set()
    total_size = 0
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > max_files:
            raise AppStorageError("Source Bundleのファイル数が上限を超えています")
        for member in members:
            if member.file_size > max_file_bytes:
                raise AppStorageError("Source Bundleの単一ファイルサイズが上限を超えています")
            total_size += member.file_size
            if total_size > max_total_bytes:
                raise AppStorageError("Source Bundleの展開サイズが上限を超えています")
            normalized = normalize_app_bundle_member(member.filename)
            if normalized is None:
                continue
            key = normalized.casefold()
            if key in seen:
                raise AppStorageError(f"同じpathが重複しています: {normalized}")
            seen.add(key)
            if member.is_dir():
                continue
            destination = staging_root / Path(normalized)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source_file, destination.open("wb") as target_file:
                shutil.copyfileobj(source_file, target_file)
            imported.append(normalized)
    return imported


def swap_app_workspace_files(
    workspace: str | os.PathLike[str],
    staging: str | os.PathLike[str],
    relative_paths: Iterable[str],
    journal: AppWorkspaceJournal,
) -> list[str]:
    """staging の成果物を workspace へ move で差し替える。

    1 path ごとに「元をバックアップへ move → 新しいものを本番位置へ move」を
    行い、すべての move を ``journal`` に記録する。途中で失敗しても
    ``journal.rollback()`` を呼べば元の workspace が完全に復元される。

    Bundle に含まれない既存ファイルは触らない（merge セマンティクス）。
    import が利用者の手書きファイルを黙って消さないための意図的な選択で、
    差し替えの原子性はこの journal が担保する。
    """
    workspace_root = Path(workspace)
    staging_root = Path(staging)
    applied: list[str] = []
    for relative in relative_paths:
        source = staging_root / Path(normalize_app_relative_path(relative))
        if not source.is_file():
            continue
        journal.stash(relative)
        destination = resolve_workspace_file(workspace_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _move_file(source, destination)
        applied.append(normalize_app_relative_path(relative))
    return applied


def remove_app_instance(
    project_id: str | UUID,
    app_id: str | UUID | None = None,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Remove one App instance or all instances for a deleted Project.

    ``app_id`` is intentionally optional: project deletion removes the whole
    ``project_<id>`` subtree, while unlinking a single App removes only that
    App's instance. Neither form can reach ``_apps`` source or artifacts.
    """
    project_uuid = _parse_id(project_id, "project_id")
    root = get_workspaces_root(workspace_root)
    project_root = root / "_app_instances" / f"project_{project_uuid}"
    namespace_root = root / "_app_instances"
    if namespace_root.is_symlink() or project_root.is_symlink():
        raise AppStorageError("storage namespace のシンボリックリンクは許可されません")
    if app_id is None:
        target = _namespace_path(root, "_app_instances", f"project_{project_uuid}")
    else:
        app_uuid = _parse_id(app_id, "app_id")
        target = _namespace_path(
            root,
            "_app_instances",
            f"project_{project_uuid}/app_{app_uuid}",
        )
    if not target.exists() and not target.is_symlink():
        return False
    if target.is_symlink() or target.is_file():
        target.unlink()
    else:
        import shutil

        shutil.rmtree(target)
    return True


def remove_app_source_and_artifacts(
    app_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Remove an App's canonical source and artifact namespaces.

    This is reserved for an explicit account/data cleanup path.  The normal
    App DELETE endpoint archives the App and deliberately keeps both
    namespaces.  Only the exact ``app_<uuid>`` directories are eligible; a
    namespace or App-directory symlink is never followed.
    """
    app_uuid = _parse_id(app_id, "app_id")
    root = get_workspaces_root(workspace_root)
    removed = False
    import shutil

    for namespace in ("_apps", "_app_artifacts"):
        namespace_root = root / namespace
        if namespace_root.is_symlink():
            raise AppStorageError("storage namespace のシンボリックリンクは許可されません")
        target = namespace_root / f"app_{app_uuid}"
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink():
            # Unlink the alias itself; never resolve or delete its target.
            target.unlink()
        else:
            _under(namespace_root, target, allow_root=False)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        removed = True
    return removed


__all__ = [
    "APP_GITIGNORE_TEXT",
    "APP_IGNORED_PATHS",
    "REQUIRED_APP_IGNORE_RULES",
    "AppStorageError",
    "AppWorkspaceJournal",
    "ensure_app_artifact",
    "ensure_app_gitignore",
    "ensure_app_instance",
    "ensure_app_workspace",
    "get_app_artifact_path",
    "get_app_instance_path",
    "get_app_workspace_path",
    "get_workspaces_root",
    "is_credential_app_path",
    "is_private_app_path",
    "is_embedded_app_path",
    "is_protected_app_path",
    "is_sensitive_app_path",
    "is_text_app_path",
    "iter_app_source_files",
    "list_app_files",
    "missing_app_ignore_rules",
    "normalize_app_bundle_member",
    "normalize_app_relative_path",
    "remove_app_instance",
    "stage_app_source_bundle",
    "swap_app_workspace_files",
    "remove_app_source_and_artifacts",
    "resolve_app_artifact_file",
    "resolve_app_file",
    "resolve_workspace_file",
    "resolve_app_instance_path",
    "sha256_file",
    "verify_file_integrity",
]
