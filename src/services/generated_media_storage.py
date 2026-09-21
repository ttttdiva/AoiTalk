"""Physical media roots are deployment config; DB paths retain generated_media/."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from .storage_io import StorageError, StorageUnavailable, check_io_cancelled, storage_io
from .storage_roots import StorageRoot, assert_no_links, lexical_path, read_registry, relative_parts


@dataclass(frozen=True)
class MediaRoot(StorageRoot):
    mount: StorageRoot | None = None
    legacy_default: bool = False

    def require_online(self, *, write: bool = False) -> Path:
        if self.mount is not None:
            self.mount.require_online(write=write)
        if self.legacy_default:
            # Preserve existing no-configuration first-run behavior only.
            assert_no_links(self.path, allow_missing=True)
            self.path.mkdir(parents=True, exist_ok=True)
        return super().require_online(write=write)

    @property
    def io_key(self) -> str:
        return self.mount.io_key if self.mount else super().io_key


def get_media_root(default: Path | str = "data/generated_media") -> MediaRoot:
    configured = os.environ.get("AOITALK_GENERATED_MEDIA_DIR", "").strip()
    selected = lexical_path(configured or default)
    mount = read_registry().containing(selected) if configured else None
    if configured and mount is None:
        raise StorageUnavailable("generated_mediaの保存先を追加ストレージとして登録してください。未登録先への保存は行いません")
    return MediaRoot(id="generated-media", name="Generated Media", root_path=str(selected),
                     external=False, mount=mount, legacy_default=not bool(configured))


def media_relative_path(logical_path: str) -> str:
    parts = relative_parts(logical_path)
    if len(parts) < 2 or parts[0] != "generated_media":
        raise StorageError("invalid_media_path", "生成メディアの相対パスが無効です", 400)
    return "/".join(parts[1:])


def resolve_path(logical_path: str, default: Path | str) -> Path:
    return get_media_root(default).resolve(media_relative_path(logical_path))


async def write_media(logical_path: str, data: bytes, default: Path | str) -> None:
    root = get_media_root(default)
    relative = media_relative_path(logical_path)
    def write():
        destination = root.resolve(relative, write=True)
        if not destination.parent.is_dir():
            raise StorageUnavailable("generated_mediaの保存先ディレクトリを先に用意してください")
        temporary = destination.with_name(f".aoitalk-storage-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            check_io_cancelled()
            root.resolve(relative, write=True)
            os.replace(temporary, destination)
        except OSError as exc:
            raise StorageUnavailable("生成メディアを保存できません。保存先の接続・空き容量を確認してください") from exc
        finally:
            try:
                root.require_online(write=True)
                assert_no_links(temporary, allow_missing=True)
                temporary.unlink(missing_ok=True)
            except (OSError, StorageError):
                pass
    await storage_io.run(root.io_key, write, mutation=True)


async def remove_media(logical_path: str, default: Path | str) -> bool:
    root = get_media_root(default)
    relative = media_relative_path(logical_path)
    def remove():
        target = root.resolve(relative, write=True)
        check_io_cancelled()
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            root.require_online(write=True)
            return False
        except OSError as exc:
            raise StorageUnavailable("生成メディアの削除を保留しました") from exc
    return await storage_io.run(root.io_key, remove, mutation=True)


async def ensure_media_online(default: Path | str) -> None:
    root = get_media_root(default)
    await storage_io.run(root.io_key, root.require_online, timeout=3)
