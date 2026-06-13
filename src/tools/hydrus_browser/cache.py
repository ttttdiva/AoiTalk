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


class ThumbnailCache:
    """サムネイル画像のインメモリLRUキャッシュ"""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    ):
        self._cache: OrderedDict[int, Tuple[str, bytes]] = OrderedDict()
        self._max_entries = max_entries
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._current_size = 0

    def get(self, file_id: int) -> Optional[Tuple[str, bytes]]:
        """キャッシュからサムネイル取得。ヒット時はLRU順を更新。"""
        if file_id in self._cache:
            self._cache.move_to_end(file_id)
            return self._cache[file_id]
        return None

    def put(self, file_id: int, content_type: str, data: bytes) -> None:
        """サムネイルをキャッシュに追加。容量超過時はLRUエントリを削除。"""
        entry_size = len(data)

        # 既存エントリの更新
        if file_id in self._cache:
            old_ct, old_data = self._cache.pop(file_id)
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

        self._cache[file_id] = (content_type, data)
        self._current_size += entry_size

    def clear(self) -> None:
        """キャッシュをクリア"""
        self._cache.clear()
        self._current_size = 0

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
