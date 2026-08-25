"""
Hydrus サムネイル用インメモリ LRU キャッシュ
"""
import os
import logging
from collections import OrderedDict
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# デフォルト設定
DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_SIZE_MB = 100


def _scope_matches(entry_scope: str, requested_scope: str) -> bool:
    """Match a principal component without an ambiguous string prefix.

    Integration scopes are encoded as ``<user-id>:<credential-fingerprint>``.
    Comparing the component before the delimiter prevents ``user-a`` from
    matching ``user-ab`` while still allowing cache statistics/clear operations
    to address all credentials belonging to one principal.
    """

    if entry_scope == requested_scope:
        return True
    owner, separator, _fingerprint = entry_scope.partition(":")
    return bool(separator) and owner == requested_scope


class ThumbnailCache:
    """サムネイル画像のインメモリLRUキャッシュ"""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    ):
        # key = (authenticated user/integration scope, Hydrus file id).  File
        # ids are local to a Hydrus instance and can collide across users.
        self._cache: OrderedDict[Tuple[str, int], Tuple[str, bytes]] = OrderedDict()
        self._max_entries = max_entries
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._current_size = 0

    def get(self, scope: str, file_id: int) -> Optional[Tuple[str, bytes]]:
        """キャッシュからサムネイル取得。ヒット時はLRU順を更新。"""
        key = (str(scope), int(file_id))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(
        self,
        scope: str,
        file_id: int,
        content_type: str | bytes,
        data: bytes,
    ) -> None:
        """サムネイルをキャッシュに追加。容量超過時はLRUエントリを削除。"""
        content_type = str(content_type)
        entry_size = len(data)

        # 既存エントリの更新
        key = (str(scope), int(file_id))
        if key in self._cache:
            old_ct, old_data = self._cache.pop(key)
            self._current_size -= len(old_data)

        # 容量確保
        while (
            self._cache
            and (
                len(self._cache) >= self._max_entries
                or self._current_size + entry_size > self._max_size_bytes
            )
        ):
            _, (_, evicted_data) = self._cache.popitem(last=False)
            self._current_size -= len(evicted_data)

        self._cache[key] = (content_type, data)
        self._current_size += entry_size

    def clear(self, scope: Optional[str] = None) -> None:
        """キャッシュをクリア。

        ``scope`` を渡した場合はそのユーザー/統合スコープだけを消去し、
        他のユーザーのサムネイルを巻き込まない。
        """
        if scope is None:
            self._cache.clear()
            self._current_size = 0
            return
        requested_scope = str(scope)
        for key in [
            key
            for key in self._cache
            if _scope_matches(key[0], requested_scope)
        ]:
            _content_type, data = self._cache.pop(key)
            self._current_size -= len(data)

    def stats_for(self, scope: str) -> dict:
        """Return statistics for one authenticated user scope only."""
        requested_scope = str(scope)
        entries = [
            (content_type, data)
            for (entry_scope, _file_id), (content_type, data) in self._cache.items()
            if _scope_matches(entry_scope, requested_scope)
        ]
        return {
            "entries": len(entries),
            "max_entries": self._max_entries,
            "size_mb": round(sum(len(data) for _, data in entries) / (1024 * 1024), 2),
            "max_size_mb": self._max_size_bytes // (1024 * 1024),
        }

    @property
    def stats(self) -> dict:
        return {
            "entries": len(self._cache),
            "max_entries": self._max_entries,
            "size_mb": round(self._current_size / (1024 * 1024), 2),
            "max_size_mb": self._max_size_bytes // (1024 * 1024),
        }


# モジュールレベルのシングルトン
_thumbnail_cache: Optional[ThumbnailCache] = None


def get_thumbnail_cache() -> ThumbnailCache:
    global _thumbnail_cache
    if _thumbnail_cache is None:
        max_entries = int(os.environ.get("HYDRUS_THUMB_CACHE_SIZE", DEFAULT_MAX_ENTRIES))
        max_size_mb = int(os.environ.get("HYDRUS_THUMB_CACHE_MB", DEFAULT_MAX_SIZE_MB))
        _thumbnail_cache = ThumbnailCache(max_entries=max_entries, max_size_mb=max_size_mb)
        logger.info(f"Hydrusサムネイルキャッシュ初期化: {max_entries}エントリ / {max_size_mb}MB")
    return _thumbnail_cache
