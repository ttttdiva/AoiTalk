"""キャラクターごとの Irodori 参照音声資産サービス。

参照音声用の専用テーブルは作らず、既存 ``characters.voice_parameters``
JSON をメタデータの正本として利用する。音声本体は ``data`` 配下に置き、
このサービス以外からはパスを組み立てない。アップロードと WASAPI 録音の
両方を同じ正規化経路に通すことで、Irodori に渡す音声を常に mono/PCM16
WAV に揃える。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import tempfile
import threading
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Optional

from sqlalchemy import select

from ..memory.database import get_db_session
from ..models.ecc_models import Character
from ..utils.uuid_utils import parse_uuid

try:  # ``numpy``/``soundfile`` are optional in light-weight test installs.
    import numpy as _np
except Exception:  # pragma: no cover - exercised on minimal installations
    _np = None

try:
    import soundfile as _sf
except Exception:  # pragma: no cover - exercised on minimal installations
    _sf = None


MAX_TOTAL_DURATION_SECONDS = 120.0
MAX_ASSET_BYTES = 100 * 1024 * 1024
MAX_DISPLAY_NAME_LENGTH = 200
ASSET_DIRECTORY_NAME = "character_voice_assets"


class CharacterVoiceAssetError(Exception):
    """参照音声操作のドメインエラー。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CharacterVoiceAssetNotFoundError(CharacterVoiceAssetError):
    def __init__(self, asset_id: str):
        super().__init__(f"参照音声が見つかりません: {asset_id}", status_code=404)


class CharacterVoiceAssetCharacterNotFoundError(CharacterVoiceAssetError):
    def __init__(self, character_id: str):
        super().__init__(f"キャラクターが見つかりません: {character_id}", status_code=404)


@dataclass(frozen=True)
class NormalizedAudio:
    """正規化済み PCM16 mono 音声。"""

    pcm_bytes: bytes
    sample_rate: int
    channels: int
    frame_count: int

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate

    @property
    def wav_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(self.pcm_bytes)
        return buffer.getvalue()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _asset_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return ""


def _asset_list(parameters: Any) -> list[dict[str, Any]]:
    """voice_parameters から順序を保った資産メタデータを取り出す。"""

    if not isinstance(parameters, Mapping):
        return []
    values = parameters.get("irodori_reference_assets", [])
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, Mapping):
            result.append(dict(value))
    return result


def _clean_display_name(value: Any, fallback: str = "reference") -> str:
    text = str(value or "").strip().replace("\x00", "")
    # A display name is metadata only; never allow it to become a path.
    text = text.replace("/", "_").replace("\\", "_")
    return (text or fallback)[:MAX_DISPLAY_NAME_LENGTH]


def _read_source_bytes(source: Any, *, max_bytes: int) -> bytes:
    """bytes/path/file-like を上限付きで読み込む。"""

    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    elif isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise CharacterVoiceAssetError(f"音声ファイルを読み込めません: {exc}") from exc
        if size > max_bytes:
            raise CharacterVoiceAssetError("音声ファイルが大きすぎます")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CharacterVoiceAssetError(f"音声ファイルを読み込めません: {exc}") from exc
    elif hasattr(source, "read"):
        reader = source
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = reader.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode()
            chunk = bytes(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise CharacterVoiceAssetError("音声ファイルが大きすぎます")
            chunks.append(chunk)
        data = b"".join(chunks)
    else:
        raise CharacterVoiceAssetError("音声データの形式が不正です")

    if not data:
        raise CharacterVoiceAssetError("音声ファイルが空です")
    if len(data) > max_bytes:
        raise CharacterVoiceAssetError("音声ファイルが大きすぎます")
    return data


def _decode_wave_fallback(data: bytes) -> tuple[Any, int, int]:
    """soundfile がない環境向けの WAV デコーダー。"""

    try:
        wav = wave.open(io.BytesIO(data), "rb")
    except (wave.Error, EOFError) as exc:
        raise CharacterVoiceAssetError("WAV/対応音声としてデコードできません") from exc

    with wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        width = wav.getsampwidth()
        frames = wav.getnframes()
        raw = wav.readframes(frames)
    if channels < 1 or sample_rate < 1 or frames < 1:
        raise CharacterVoiceAssetError("音声ヘッダーが不正です")
    if width not in (1, 2, 3, 4):
        raise CharacterVoiceAssetError("PCM 8/16/24/32-bit WAV のみ対応しています")
    if _np is None:
        # 依存なしでも mono/16-bit WAV は受け付ける。その他は明示的に拒否。
        if channels != 1 or width != 2:
            raise CharacterVoiceAssetError("音声の変換には numpy/soundfile が必要です")
        return raw, sample_rate, channels

    if width == 1:
        values = _np.frombuffer(raw, dtype=_np.uint8).astype(_np.float32)
        values = (values - 128.0) / 128.0
    elif width == 2:
        values = _np.frombuffer(raw, dtype="<i2").astype(_np.float32) / 32768.0
    elif width == 3:
        packed = _np.frombuffer(raw, dtype=_np.uint8).reshape(-1, 3)
        ints = (
            packed[:, 0].astype(_np.int32)
            | (packed[:, 1].astype(_np.int32) << 8)
            | (packed[:, 2].astype(_np.int32) << 16)
        )
        ints = _np.where(ints & 0x800000, ints - 0x1000000, ints)
        values = ints.astype(_np.float32) / 8388608.0
    else:
        values = _np.frombuffer(raw, dtype="<i4").astype(_np.float32) / 2147483648.0
    values = values.reshape(-1, channels)
    return values, sample_rate, channels


def normalize_audio(data: bytes | bytearray | memoryview, *, max_bytes: int = MAX_ASSET_BYTES) -> NormalizedAudio:
    """音声を finite な PCM16 mono WAV 用データへ変換する。

    ``soundfile`` は WAV 以外（FLAC/OGG 等）も扱える。依存がない環境では
    標準ライブラリの WAV デコーダーへフォールバックする。
    """

    raw = _read_source_bytes(data, max_bytes=max_bytes)
    try:
        if _sf is not None:
            array, sample_rate = _sf.read(
                io.BytesIO(raw), dtype="float32", always_2d=True
            )
            channels = int(array.shape[1]) if getattr(array, "ndim", 0) == 2 else 1
            frame_count = int(array.shape[0]) if getattr(array, "ndim", 0) else 0
        else:
            array, sample_rate, channels = _decode_wave_fallback(raw)
            if _np is not None and getattr(array, "ndim", 0) == 2:
                frame_count = int(array.shape[0])
            else:
                # The dependency-free fallback accepts only mono PCM16 WAV;
                # ``array`` is therefore two bytes per frame, not one.
                frame_count = len(array) // (max(channels, 1) * 2)
    except CharacterVoiceAssetError:
        raise
    except Exception as exc:
        raise CharacterVoiceAssetError("音声ファイルをデコードできません") from exc

    try:
        sample_rate = int(sample_rate)
    except (TypeError, ValueError) as exc:
        raise CharacterVoiceAssetError("サンプルレートが不正です") from exc
    if sample_rate < 8_000 or sample_rate > 192_000:
        raise CharacterVoiceAssetError("サンプルレートは 8kHz〜192kHz にしてください")
    if channels < 1 or channels > 64 or frame_count < 1:
        raise CharacterVoiceAssetError("音声のチャンネル数またはフレーム数が不正です")
    duration = frame_count / sample_rate
    if not math.isfinite(duration) or duration <= 0:
        raise CharacterVoiceAssetError("音声の長さが不正です")
    if duration > MAX_TOTAL_DURATION_SECONDS:
        raise CharacterVoiceAssetError("1つの参照音声は120秒以内にしてください")

    if _np is None:
        # Fallback path above only returns raw PCM for mono/16-bit WAV.
        pcm = bytes(array)
        return NormalizedAudio(pcm, sample_rate, 1, frame_count)

    array = _np.asarray(array, dtype=_np.float32)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    if array.ndim != 2 or array.shape[0] < 1:
        raise CharacterVoiceAssetError("音声データが空です")
    if not _np.isfinite(array).all():
        raise CharacterVoiceAssetError("音声データに NaN/Inf が含まれています")
    # Irodori の参照条件はモノラルで十分であり、複数チャンネルを平均して
    # クリッピングを防ぐ。PCM16 の量子化は little-endian を明示する。
    mono = array.mean(axis=1, dtype=_np.float32)
    mono = _np.clip(mono, -1.0, 1.0)
    pcm_array = _np.rint(mono * 32767.0).astype("<i2", copy=False)
    pcm = pcm_array.tobytes()
    return NormalizedAudio(pcm, sample_rate, 1, int(mono.shape[0]))


def _wav_metadata(normalized: NormalizedAudio) -> dict[str, Any]:
    return {
        "duration_seconds": round(float(normalized.duration_seconds), 6),
        "sample_rate": int(normalized.sample_rate),
        "channels": int(normalized.channels),
    }


class CharacterVoiceAssetService:
    """Character.voice_parameters を正本として音声資産を管理する。"""

    def __init__(
        self,
        storage_root: str | os.PathLike[str] | None = None,
        *,
        max_total_duration_seconds: float = MAX_TOTAL_DURATION_SECONDS,
        max_asset_bytes: int = MAX_ASSET_BYTES,
    ) -> None:
        # Match IrodoriTTSEngine's repository-root path policy.  The default
        # must not move when the process is launched from another cwd (e.g.
        # service_manager or a Windows shortcut); an explicit AOITALK_DATA_DIR
        # remains authoritative.
        self.repo_root = Path(__file__).resolve().parents[2]
        configured = storage_root
        explicit_storage_root = configured is not None
        if configured is None:
            configured = os.environ.get("AOITALK_DATA_DIR")
            explicit_storage_root = configured is not None and bool(str(configured).strip())
        if configured is None or not str(configured).strip():
            configured_path = self.repo_root / "data"
        else:
            configured_path = Path(str(configured)).expanduser()
            if not configured_path.is_absolute():
                configured_path = self.repo_root / configured_path
        self.storage_root = configured_path.resolve()
        self._repo_data_root = (self.repo_root / "data").resolve()
        # Irodori resolves relative references against the repository root.
        # Custom roots therefore need an absolute metadata path; only the
        # canonical repository ``data`` root may use ``data/...``.
        self._metadata_uses_repo_relative = (
            self.storage_root == self._repo_data_root and not explicit_storage_root
        )
        self.max_total_duration_seconds = float(max_total_duration_seconds)
        self.max_asset_bytes = int(max_asset_bytes)
        # FastAPI's TestClient and a few embedded callers can create a fresh
        # event loop per request.  Keep asyncio locks scoped to the loop that
        # will await them rather than binding one at service construction time.
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    async def _lock_for(self, character_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        key = (id(loop), str(character_id))
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _character_dir(self, character_uuid: uuid.UUID | str) -> Path:
        uid = str(character_uuid)
        root = self.storage_root.resolve()
        directory = (root / ASSET_DIRECTORY_NAME / uid).resolve()
        try:
            directory.relative_to(root)
        except ValueError as exc:  # pragma: no cover - constants make this unreachable
            raise CharacterVoiceAssetError("音声保存先が不正です", 500) from exc
        return directory

    def _resolve_relative_path(self, relative_path: Any, character_uuid: uuid.UUID | str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise CharacterVoiceAssetError("参照音声パスが不正です", 400)
        raw_text = relative_path.strip()
        try:
            raw_path = Path(raw_text).expanduser()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CharacterVoiceAssetError("参照音声パスが不正です", 400) from exc
        if raw_path.is_absolute():
            # Do not strip the leading slash/drive prefix: custom storage roots
            # persist canonical absolute paths in metadata.
            try:
                candidate = raw_path.resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise CharacterVoiceAssetError("参照音声パスが不正です", 400) from exc
        else:
            normalized = raw_text.replace("\\", "/").lstrip("/")
            pieces = normalized.split("/", 1)
            if (
                pieces[0].casefold() == "data"
                and len(pieces) == 2
                and self._metadata_uses_repo_relative
            ):
                candidate = (self.repo_root / normalized).resolve()
            elif (
                pieces[0].casefold() == self.storage_root.name.casefold()
                and len(pieces) == 2
            ):
                candidate = (self.storage_root.parent / normalized).resolve()
            else:
                # Accept legacy storage-relative values while keeping the
                # final containment check below strict.
                candidate = (self.storage_root / normalized).resolve()
        root = self.storage_root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise CharacterVoiceAssetError("参照音声パスが不正です", 400) from exc
        expected = self._character_dir(character_uuid)
        try:
            candidate.relative_to(expected)
        except ValueError as exc:
            raise CharacterVoiceAssetError("参照音声がキャラクター外を指しています", 400) from exc
        if candidate.suffix.lower() != ".wav":
            raise CharacterVoiceAssetError("参照音声は WAV のみです", 400)
        return candidate

    async def _character_row(self, session: Any, character_id: str, *, lock: bool = False) -> Character:
        uid = parse_uuid(str(character_id))
        char = None
        if uid is not None:
            stmt = select(Character).where(Character.id == uid)
            if lock:
                stmt = stmt.with_for_update()
            char = (await session.execute(stmt)).scalar_one_or_none()
        else:
            stmt = select(Character).where(Character.slug == str(character_id))
            if lock:
                stmt = stmt.with_for_update()
            char = (await session.execute(stmt)).scalar_one_or_none()
        if char is None:
            raise CharacterVoiceAssetCharacterNotFoundError(str(character_id))
        return char

    @staticmethod
    def _parameters(character: Character) -> dict[str, Any]:
        value = character.voice_parameters
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _total_duration(assets: Iterable[Mapping[str, Any]]) -> float:
        total = 0.0
        for asset in assets:
            duration = _safe_float(asset.get("duration_seconds"), -1.0)
            if duration < 0 or not math.isfinite(duration):
                raise CharacterVoiceAssetError("保存済み参照音声の長さが不正です", 500)
            total += duration
        return total

    async def list_assets(self, character_id: str) -> dict[str, Any]:
        async with await get_db_session() as session:
            char = await self._character_row(session, character_id)
            assets = _asset_list(char.voice_parameters)
        total = self._total_duration(assets)
        return {
            "assets": assets,
            "total_duration_seconds": round(total, 6),
            "max_duration_seconds": self.max_total_duration_seconds,
        }

    async def get_asset(self, character_id: str, asset_id: str) -> dict[str, Any]:
        normalized_id = _asset_id(asset_id)
        if not normalized_id:
            raise CharacterVoiceAssetNotFoundError(asset_id)
        async with await get_db_session() as session:
            char = await self._character_row(session, character_id)
            for asset in _asset_list(char.voice_parameters):
                if _asset_id(asset.get("id")) == normalized_id:
                    return asset
        raise CharacterVoiceAssetNotFoundError(asset_id)

    async def resolve_asset_path(self, character_id: str, asset_id: str) -> Path:
        asset = await self.get_asset(character_id, asset_id)
        uid = parse_uuid(str(character_id))
        if uid is None:
            async with await get_db_session() as session:
                char = await self._character_row(session, character_id)
                uid = char.id
        path = self._resolve_relative_path(asset.get("relative_path"), uid)
        if not path.is_file():
            raise CharacterVoiceAssetNotFoundError(asset_id)
        return path

    async def add_asset(
        self,
        character_id: str,
        source: Any,
        *,
        filename: str = "reference.wav",
        display_name: str | None = None,
        source_kind: str = "upload",
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        """ファイル/bytesを正規化し、資産 metadata と atomic file を登録する。"""

        raw = _read_source_bytes(source, max_bytes=self.max_asset_bytes)
        normalized = normalize_audio(raw, max_bytes=self.max_asset_bytes)
        if normalized.duration_seconds > self.max_total_duration_seconds:
            raise CharacterVoiceAssetError("参照音声は120秒以内にしてください")
        requested_id = _asset_id(asset_id) if asset_id else str(uuid.uuid4())
        if not requested_id:
            raise CharacterVoiceAssetError("asset id が不正です")
        try:
            char_uid = parse_uuid(str(character_id))
        except Exception:
            char_uid = None

        lock = await self._lock_for(str(character_id))
        temp_path: Optional[Path] = None
        final_path: Optional[Path] = None
        async with lock:
            async with await get_db_session() as session:
                char = await self._character_row(session, character_id, lock=True)
                char_uid = char.id
                parameters = self._parameters(char)
                assets = _asset_list(parameters)
                if any(_asset_id(a.get("id")) == requested_id for a in assets):
                    raise CharacterVoiceAssetError("同じ asset id が既に存在します", 409)
                total = self._total_duration(assets)
                if total + normalized.duration_seconds > self.max_total_duration_seconds + 1e-6:
                    raise CharacterVoiceAssetError(
                        f"参照音声の合計は{self.max_total_duration_seconds:g}秒以内にしてください"
                    )

                directory = self._character_dir(char_uid)
                directory.mkdir(parents=True, exist_ok=True)
                final_path = directory / f"{requested_id}.wav"
                # Atomic replace is constrained to our canonical character
                # directory.  Resolve the actual destination rather than
                # round-tripping through metadata (custom roots use absolute
                # metadata paths).
                final_path = final_path.resolve()
                try:
                    final_path.relative_to(directory.resolve())
                except ValueError as exc:  # pragma: no cover - UUID filename is canonical
                    raise CharacterVoiceAssetError("音声保存先が不正です", 500) from exc
                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{requested_id}.", suffix=".wav.part", dir=str(directory)
                )
                temp_path = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(normalized.wav_bytes)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temp_path, final_path)
                    temp_path = None
                except Exception:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    try:
                        if temp_path is not None:
                            temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise

                try:
                    stat = final_path.stat()
                    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
                    name = _clean_display_name(
                        display_name,
                        Path(str(filename or "reference.wav")).stem or "reference",
                    )
                    metadata_path = (
                        f"data/{ASSET_DIRECTORY_NAME}/{char_uid}/{requested_id}.wav"
                        if self._metadata_uses_repo_relative
                        else str(final_path)
                    )
                    metadata: dict[str, Any] = {
                        "id": requested_id,
                        "display_name": name,
                        "relative_path": metadata_path,
                        **_wav_metadata(normalized),
                        "size_bytes": int(stat.st_size),
                        "source": _clean_display_name(source_kind, "upload"),
                        "sha256": digest,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    assets.append(metadata)
                    parameters["irodori_reference_assets"] = assets
                    char.voice_parameters = parameters
                    await session.commit()
                    return metadata
                except Exception:
                    # Keep DB/file state all-or-nothing for ordinary failures.
                    try:
                        final_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise
            # session context exits here

        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return {}  # pragma: no cover - lock/session return always exits above

    async def add_asset_from_file(
        self,
        character_id: str,
        path: str | os.PathLike[str],
        *,
        display_name: str | None = None,
        source_kind: str = "wasapi_loopback",
    ) -> dict[str, Any]:
        return await self.add_asset(
            character_id,
            path,
            filename=Path(path).name,
            display_name=display_name,
            source_kind=source_kind,
        )

    async def delete_asset(self, character_id: str, asset_id: str) -> dict[str, Any]:
        normalized_id = _asset_id(asset_id)
        if not normalized_id:
            raise CharacterVoiceAssetNotFoundError(asset_id)
        lock = await self._lock_for(str(character_id))
        async with lock:
            async with await get_db_session() as session:
                char = await self._character_row(session, character_id, lock=True)
                parameters = self._parameters(char)
                assets = _asset_list(parameters)
                removed = next(
                    (asset for asset in assets if _asset_id(asset.get("id")) == normalized_id),
                    None,
                )
                if removed is None:
                    raise CharacterVoiceAssetNotFoundError(asset_id)
                # Resolve before committing so a malicious legacy path cannot be
                # used to delete outside the character directory.
                file_path = self._resolve_relative_path(removed.get("relative_path"), char.id)
                parameters["irodori_reference_assets"] = [
                    asset for asset in assets if _asset_id(asset.get("id")) != normalized_id
                ]
                char.voice_parameters = parameters
                await session.commit()
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    # Metadata is authoritative; orphan cleanup can happen later.
                    pass
                return removed

    async def reorder_assets(self, character_id: str, asset_ids: Iterable[str]) -> list[dict[str, Any]]:
        requested = [_asset_id(value) for value in asset_ids]
        if not requested or any(not value for value in requested) or len(set(requested)) != len(requested):
            raise CharacterVoiceAssetError("asset の順序指定が不正です")
        lock = await self._lock_for(str(character_id))
        async with lock:
            async with await get_db_session() as session:
                char = await self._character_row(session, character_id, lock=True)
                parameters = self._parameters(char)
                assets = _asset_list(parameters)
                by_id = {_asset_id(asset.get("id")): asset for asset in assets}
                if set(requested) != set(by_id) or len(requested) != len(by_id):
                    raise CharacterVoiceAssetError("asset の順序指定は登録済み資産と一致させてください")
                ordered = [by_id[value] for value in requested]
                parameters["irodori_reference_assets"] = ordered
                char.voice_parameters = parameters
                await session.commit()
                return ordered


_default_service: CharacterVoiceAssetService | None = None


def get_character_voice_asset_service() -> CharacterVoiceAssetService:
    global _default_service
    if _default_service is None:
        _default_service = CharacterVoiceAssetService()
    return _default_service


__all__ = [
    "ASSET_DIRECTORY_NAME",
    "MAX_ASSET_BYTES",
    "MAX_TOTAL_DURATION_SECONDS",
    "CharacterVoiceAssetError",
    "CharacterVoiceAssetNotFoundError",
    "CharacterVoiceAssetCharacterNotFoundError",
    "CharacterVoiceAssetService",
    "NormalizedAudio",
    "get_character_voice_asset_service",
    "normalize_audio",
]
