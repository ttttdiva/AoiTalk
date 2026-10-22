"""Prepare authorized originals for the shared EmbeddingGemma 2 vector space.

The caller must resolve the original through its current user/Space/Project ACL
before calling this module, and reauthorize it when serving a result. This
module preserves those source references; a path or an indexed ACL payload is
never itself authorization. OCR and ASR remain lexical/citation evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHUNK_SAMPLES = 480_000
VIDEO_CHUNK_SECONDS = 32.0
STREAM_OFFSET_PACKET_BUDGET = 64
MAIL_CHUNK_CHARACTERS = 1536
MAX_MAIL_BYTES = 64 * 1024 * 1024
MAX_MAIL_TEXT_CHARACTERS = 2_000_000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".mpga", ".m4a", ".ogg", ".opus", ".aac", ".wma"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg"}


class MediaPreparationError(RuntimeError):
    """An explicit migration failure/exclusion, never an empty success."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


@dataclass
class PreparedMediaChunk:
    id: str
    modality: str
    model_input: dict[str, Any]
    lexical_text: str
    metadata: dict[str, Any]


@dataclass
class MediaDecodeBudget:
    """Request-local allocation reservations, checked before decoder output."""
    maximum_bytes: int
    maximum_units: int
    consumed_bytes: int = 0
    consumed_units: int = 0

    def check(self, amount, *, working=6, units=1):
        if amount < 0 or self.consumed_bytes + amount * working > self.maximum_bytes:
            raise MediaPreparationError("decoded_budget_exceeded", "展開前のメディア処理予算を超えています")
        if self.consumed_units + units > self.maximum_units:
            raise MediaPreparationError("unit_budget_exceeded", "全query unitを展開する予算がありません")

    def reserve(self, amount, *, working=6):
        self.check(amount, working=working)
        self.consumed_bytes += amount
        self.consumed_units += 1


def time_ranges(duration: float, maximum: float) -> list[tuple[float, float]]:
    """Partition the complete source timeline, including a short final tail."""
    if not math.isfinite(duration) or duration <= 0:
        raise MediaPreparationError("invalid_duration", "メディアの有効な長さを取得できません")
    if not math.isfinite(maximum) or maximum <= 0:
        raise ValueError("maximum must be positive and finite")
    return [(i * maximum, min((i + 1) * maximum, duration))
            for i in range(math.ceil(duration / maximum))]


def verify_full_coverage(ranges: Sequence[tuple[float, float]], duration: float) -> bool:
    if not math.isfinite(duration) or duration <= 0 or not ranges:
        return False
    cursor = 0.0
    for start, end in ranges:
        if not math.isfinite(start) or not math.isfinite(end):
            return False
        if abs(start - cursor) > 1e-6 or end <= start or end > duration + 1e-6:
            return False
        cursor = end
    return abs(cursor - duration) <= 1e-6


class MediaPreparationService:
    """Decode page images and bounded media chunks without modifying originals.

    ``iter_prepare`` bounds audio/PDF working memory to one chunk/page. Video
    derivatives live in a deployment cache, separate from the source. Only this
    cache can be regenerated; neither successful preparation nor migration
    authorizes deletion of an original or an old index.
    """

    def __init__(self, cache_dir: str | Path = "cache/embeddinggemma2/media", *,
                 pdf_dpi: int = 144, timeout_seconds: float = 180):
        self.cache_dir = Path(cache_dir)
        self.pdf_dpi = pdf_dpi
        self.timeout_seconds = timeout_seconds
        if not 72 <= pdf_dpi <= 300 or timeout_seconds <= 0:
            raise ValueError("invalid media preparation limits")

    def prepare(self, file_path: str | Path, *, source_id: str,
                source_kind: str = "attachment", metadata: Mapping[str, Any] | None = None,
                lexical_text: str = "", lexical_segments: Sequence[Mapping[str, Any]] = (),
                mime_type: str | None = None) -> list[PreparedMediaChunk]:
        return list(self.iter_prepare(
            file_path, source_id=source_id, source_kind=source_kind, metadata=metadata,
            lexical_text=lexical_text, lexical_segments=lexical_segments, mime_type=mime_type,
        ))

    def iter_prepare(self, file_path: str | Path, *, source_id: str,
                     source_kind: str = "attachment", metadata: Mapping[str, Any] | None = None,
                     lexical_text: str = "", lexical_segments: Sequence[Mapping[str, Any]] = (),
                     mime_type: str | None = None, decode_budget: MediaDecodeBudget | None = None) -> Iterator[PreparedMediaChunk]:
        path = Path(file_path)
        if not source_id or not path.is_file():
            raise MediaPreparationError("missing_original", "メディア原本または識別子がありません")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        file_name = str((metadata or {}).get("file_name") or path.name)
        base = {
            **(metadata or {}), "source_id": str(source_id), "source_kind": source_kind,
            "source_file": str(path), "file_name": file_name, "content_hash": digest.hexdigest(),
        }
        if decode_budget is not None:
            base["_query_decode_budget"] = decode_budget
        suffix = Path(file_name).suffix.lower()
        mime = (mime_type or "").split(";", 1)[0].lower().strip()
        if suffix == ".pdf" or mime == "application/pdf":
            yield from self._pdf(path, base, lexical_text, lexical_segments)
        elif suffix in IMAGE_SUFFIXES or mime.startswith("image/"):
            yield from self._image(path, base, lexical_text, lexical_segments)
        elif suffix in AUDIO_SUFFIXES or mime.startswith("audio/"):
            yield from self._audio(path, base, lexical_text, lexical_segments)
        elif suffix in VIDEO_SUFFIXES or mime.startswith("video/"):
            yield from self._video(path, base, lexical_text, lexical_segments)
        elif suffix in {".msg", ".eml"} or mime in {"application/vnd.ms-outlook", "message/rfc822"}:
            yield from self._mail(path, base, suffix, mime)
        else:
            raise MediaPreparationError("unsupported_media", "このメディア形式には原本デコーダーがありません")

    def _mail(self, path: Path, base: dict[str, Any], suffix: str,
              mime: str) -> Iterator[PreparedMediaChunk]:
        """Reuse the canonical local mail parser; never save embedded attachments.

        Text locators address the normalized header/body evidence, not byte
        offsets in the compound MSG file. Original bytes remain unchanged.
        Each model input uses the same Document prefix as ordinary text.
        """
        from ..services.mail_parser import parse_eml_bytes, parse_msg_file
        from .embedding_identity import document_input

        if path.stat().st_size > MAX_MAIL_BYTES:
            raise MediaPreparationError("mail_size_limit", "メール原本が解析上限を超えています")
        budget = base.get("_query_decode_budget")
        if budget:
            budget.check(path.stat().st_size, working=8)
        try:
            is_msg = suffix == ".msg" or (suffix != ".eml" and mime == "application/vnd.ms-outlook")
            mail = parse_msg_file(path) if is_msg else parse_eml_bytes(path.read_bytes())
        except (ImportError, RuntimeError) as exc:
            # Dependency/decode failures must remain visible, never empty success.
            raise MediaPreparationError("mail_decoder_unavailable", "メール原本を解析できません") from exc
        except Exception as exc:
            raise MediaPreparationError("mail_decode_failed", "メール原本を解析できません") from exc
        headers = []
        for label, value in [("Subject", mail.subject), ("From", mail.sender),
                             ("To", ", ".join(mail.to)), ("Cc", ", ".join(mail.cc)),
                             ("Bcc", ", ".join(mail.bcc)), ("Date", mail.date),
                             ("Message-ID", mail.message_id)]:
            if value:
                headers.append(f"{label}: {value}")
        text = "\n\n".join(part for part in ["\n".join(headers), mail.body] if part).strip()
        if not text:
            raise MediaPreparationError("empty_mail", "メールに検索可能な本文・ヘッダーがありません")
        if len(text) > MAX_MAIL_TEXT_CHARACTERS:
            raise MediaPreparationError("mail_text_limit", "メール本文が解析上限を超えています")
        title = (mail.subject or base["file_name"])[:160]
        # No truncation: every character belongs to exactly one evidence unit.
        for start in range(0, len(text), MAIL_CHUNK_CHARACTERS):
            end = min(start + MAIL_CHUNK_CHARACTERS, len(text))
            evidence = text[start:end]
            if budget:
                budget.reserve(len(evidence.encode("utf-8")) * 4)
            yield self._chunk(
                "text", document_input(evidence, title), base, evidence,
                text_start=start, text_end=end, text_length=len(text),
                mail_format="msg" if is_msg else "eml", mail_decoder_version=1,
                embedded_attachments_indexed=False,
            )

    @staticmethod
    def _chunk(modality: str, value: Any, base: dict[str, Any], lexical_text: str,
               **locator: Any) -> PreparedMediaChunk:
        metadata = {**base, **locator, "modality": modality}
        identity = json.dumps([base["source_kind"], base["source_id"], base["content_hash"],
                               modality, locator], sort_keys=True, ensure_ascii=False)
        return PreparedMediaChunk(
            str(uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk-media:" + identity)), modality,
            {modality: value}, lexical_text, metadata,
        )

    @staticmethod
    def _lexical(text: str, segments: Sequence[Mapping[str, Any]], *,
                 page: int | None = None, start: float = 0, end: float = 0) -> str:
        selected = [text] if text else []
        for item in segments:
            if page is not None:
                try:
                    value = item.get("page_number", item.get("page"))
                    include = value is not None and int(value) == page
                except (TypeError, ValueError) as exc:
                    raise MediaPreparationError("invalid_lexical_locator", "OCRのページ位置が無効です") from exc
            else:
                try:
                    left = float(item.get("start_seconds", item.get("start", 0)))
                    right = float(item.get("end_seconds", item.get("end", left)))
                except (TypeError, ValueError):
                    raise MediaPreparationError("invalid_lexical_locator", "OCR/ASRの参照位置が無効です")
                include = math.isfinite(left) and math.isfinite(right) and left < end and right > start
            if include and item.get("text"):
                selected.append(str(item["text"]))
        return "\n".join(dict.fromkeys(selected))

    def _image(self, path: Path, base: dict[str, Any], text: str,
               segments: Sequence[Mapping[str, Any]]) -> Iterator[PreparedMediaChunk]:
        from PIL import Image
        try:
            with Image.open(path) as original:
                # A GIF/TIFF is indexed frame by frame, never silently as frame 0.
                for frame in range(getattr(original, "n_frames", 1)):
                    budget = base.get("_query_decode_budget")
                    if budget:
                        budget.reserve(original.width * original.height * 3)
                        reserved = original.width * original.height * 3
                    original.seek(frame)
                    if budget and original.width * original.height * 3 > reserved:
                        difference = original.width * original.height * 3 - reserved
                        budget.check(difference, units=0)
                        budget.consumed_bytes += difference
                    lexical = "\n".join(dict.fromkeys([part for part in [text, *[
                        str(item["text"]) for item in segments if item.get("text") and
                        item.get("frame_index", item.get("frame", frame)) == frame
                    ]] if part]))
                    yield self._chunk("image", original.convert("RGB").copy(), base, lexical,
                                      frame_index=frame, frame_count=getattr(original, "n_frames", 1))
        except (OSError, ValueError) as exc:
            raise MediaPreparationError("image_decode_failed", "画像原本を読み取れません") from exc

    def _pdf(self, path: Path, base: dict[str, Any], text: str,
             segments: Sequence[Mapping[str, Any]]) -> Iterator[PreparedMediaChunk]:
        try:
            import fitz
            from PIL import Image
        except ImportError as exc:
            raise MediaPreparationError("pdf_decoder_missing", "PDFページ画像の準備にPyMuPDFが必要です") from exc
        try:
            with fitz.open(path) as document:
                if document.needs_pass:
                    raise MediaPreparationError("encrypted_pdf", "暗号化PDFは認証済み復号原本が必要です")
                if len(document) == 0:
                    raise MediaPreparationError("empty_pdf", "PDFにページがありません")
                for index, page in enumerate(document):
                    budget = base.get("_query_decode_budget")
                    if budget:
                        # Pixmap/sample/PIL copies need working headroom too.
                        width = math.ceil(page.rect.width * self.pdf_dpi / 72) + 2
                        height = math.ceil(page.rect.height * self.pdf_dpi / 72) + 2
                        budget.reserve(width * height * 3)
                    pixmap = page.get_pixmap(dpi=self.pdf_dpi, alpha=False)
                    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                    lexical = self._lexical(text, segments, page=index + 1)
                    extracted = page.get_text("text").strip()
                    if extracted:
                        lexical = "\n".join(part for part in [lexical, extracted] if part)
                    yield self._chunk("image", image, base, lexical, page_number=index + 1,
                                      page_count=len(document), pdf_dpi=self.pdf_dpi)
        except MediaPreparationError:
            raise
        except Exception as exc:
            raise MediaPreparationError("pdf_decode_failed", "PDF原本のページを読み取れません") from exc

    def _executable(self, name: str) -> str:
        executable = shutil.which(name)
        if not executable:
            raise MediaPreparationError(f"{name}_missing", f"メディア原本の処理に{name}が必要です")
        return executable

    @staticmethod
    def _decoder_path(path: Path) -> str:
        # Older Windows ffmpeg builds (e.g. ImageMagick's 4.2) are not long-path
        # aware: paths at or beyond MAX_PATH fail with "No such file" although
        # ffprobe opens them. The extended-length prefix keeps the input seekable.
        resolved = os.path.abspath(path)
        if os.name != "nt" or len(resolved) < 248 or resolved.startswith('\\\\?\\'):
            return str(path)
        if resolved.startswith('\\\\'):
            return '\\\\?\\UNC' + resolved[1:]
        return '\\\\?\\' + resolved

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, capture_output=True, check=True, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise MediaPreparationError("media_decode_timeout", "メディアの準備が時間制限を超えました") from exc
        except (OSError, subprocess.CalledProcessError) as exc:
            raise MediaPreparationError("media_decode_failed", "メディア原本をデコードできません") from exc

    def _probe(self, path: Path, *, count_frames: bool = False) -> dict[str, Any]:
        result = self._run([self._executable("ffprobe"), "-v", "error", *(["-count_frames"] if count_frames else []), "-show_streams",
                            "-show_format", "-of", "json", self._decoder_path(path)])
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise MediaPreparationError("media_probe_failed", "メディアの形式を取得できません") from exc

    @staticmethod
    def _stream_duration(probe: dict[str, Any], stream: dict[str, Any]) -> float:
        value = stream.get("duration")
        if not value:
            tagged = stream.get("tags", {}).get("DURATION")
            if tagged:
                try:
                    hours, minutes, seconds = tagged.split(":")
                    value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                except (ValueError, TypeError):
                    value = None
        try:
            duration = float(value or probe.get("format", {}).get("duration", 0))
        except (TypeError, ValueError) as exc:
            raise MediaPreparationError("invalid_duration", "メディアストリームの長さを取得できません") from exc
        time_ranges(duration, duration)
        return duration

    def _stream_offset(self, path: Path, probe: dict[str, Any], kind: str) -> float:
        # Matroska may report stream.start_time=0 for a soundtrack whose first
        # decoded frame is several seconds later. ``#N`` counts input packets,
        # so a codec priming packet can produce no decoded frame. Keep the
        # probe bounded while allowing such packets before the first frame.
        result = self._run([
            self._executable("ffprobe"), "-v", "error", "-select_streams", f"{kind}:0",
            "-read_intervals", f"%+#{STREAM_OFFSET_PACKET_BUDGET}", "-show_frames", "-show_entries",
            "frame=pts_time,best_effort_timestamp_time", "-of", "json", self._decoder_path(path),
        ])
        try:
            frames = json.loads(result.stdout).get("frames")
            first = frames[0] if isinstance(frames, list) and frames else None
        except (AttributeError, TypeError, ValueError) as exc:
            raise MediaPreparationError("media_probe_failed", "原本ストリームの最初のフレームを取得できません") from exc
        if not isinstance(first, Mapping):
            raise MediaPreparationError("media_probe_failed", "原本ストリームの最初のフレームを取得できません")
        timestamp = first.get("pts_time")
        if timestamp in (None, "", "N/A"):
            timestamp = first.get("best_effort_timestamp_time")
        if timestamp in (None, "", "N/A"):
            raise MediaPreparationError("media_probe_failed", "原本ストリームの最初のフレーム時刻を取得できません")
        try:
            offset = float(timestamp) - float(
                probe.get("format", {}).get("start_time", 0))
        except (TypeError, ValueError) as exc:
            raise MediaPreparationError("invalid_duration", "メディアストリームの開始位置を取得できません") from exc
        if not math.isfinite(offset):
            raise MediaPreparationError("invalid_duration", "メディアストリームの開始位置が無効です")
        return max(0.0, offset)

    def _audio(self, path: Path, base: dict[str, Any], text: str,
               segments: Sequence[Mapping[str, Any]]) -> Iterator[PreparedMediaChunk]:
        probe = self._probe(path)
        stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
        if stream is None:
            raise MediaPreparationError("no_audio_stream", "原本に音声ストリームがありません")
        budget = base.get("_query_decode_budget")
        if budget:
            declared = float(stream.get("duration") or probe.get("format", {}).get("duration") or 0)
            if math.isfinite(declared) and declared > 0:
                budget.check(math.ceil(declared * AUDIO_SAMPLE_RATE) * 4, working=1,
                             units=math.ceil(declared / 30))
        offset = self._stream_offset(path, probe, "a")
        executable = self._executable("ffmpeg")
        args = [executable, "-v", "error", "-nostdin", "-i", self._decoder_path(path), "-map", "0:a:0",
                "-vn", "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE), "-af", "asetpts=N/SR/TB"]
        # Container durations include codec padding and may exceed decoded sound.
        # Count the actual resampled PCM timeline before partitioning it. This is
        # two linear decode passes, rather than repeatedly decoding a long prefix.
        measured = self._run([*args, "-progress", "pipe:1", "-nostats", "-f", "f32le", "-y", os.devnull])
        sizes = [line.split("=", 1)[1] for line in measured.stdout.decode().splitlines()
                 if line.startswith("total_size=")]
        size = int(sizes[-1]) if sizes else 0
        if size % 4:
            raise MediaPreparationError("invalid_audio_samples", "音声PCMのバイト数が無効です")
        duration = size / 4 / AUDIO_SAMPLE_RATE
        if budget:
            budget.check(size, working=1, units=math.ceil(duration / 30))
        ranges = time_ranges(duration, AUDIO_CHUNK_SAMPLES / AUDIO_SAMPLE_RATE)
        process = subprocess.Popen([*args, "-f", "f32le", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-audio-reader")
        try:
            for start, end in ranges:
                expected = round((end - start) * AUDIO_SAMPLE_RATE)
                if budget:
                    budget.reserve(expected * 4)
                try:
                    raw = reader.submit(process.stdout.read, expected * 4).result(timeout=self.timeout_seconds)
                except TimeoutError as exc:
                    raise MediaPreparationError("media_decode_timeout", "音声デコードが時間制限を超えました") from exc
                samples = np.frombuffer(raw, dtype="<f4").copy()
                if not 0 < samples.size <= AUDIO_CHUNK_SAMPLES or not np.isfinite(samples).all():
                    raise MediaPreparationError("invalid_audio_samples", "音声区間のサンプル数または値が無効です")
                if samples.size != expected:
                    raise MediaPreparationError("audio_coverage_incomplete", "デコード音声のサンプル数が原本と一致しません")
                yield self._chunk(
                    "audio", samples, base, self._lexical(text, segments, start=start + offset, end=end + offset),
                    start_seconds=start + offset, end_seconds=end + offset, duration_seconds=duration,
                    timeline_start_seconds=offset, sample_rate=AUDIO_SAMPLE_RATE, sample_count=int(samples.size),
                    full_coverage=verify_full_coverage(ranges, duration),
                )
            extra = reader.submit(process.stdout.read, 4).result(timeout=self.timeout_seconds)
            if extra or process.wait(timeout=self.timeout_seconds) != 0:
                raise MediaPreparationError("audio_coverage_incomplete", "デコード音声の全サンプルを検証できません")
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            raise MediaPreparationError("media_decode_timeout", "音声デコードが時間制限を超えました") from exc
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.shutdown(wait=True)
            process.stdout.close()

    def _video(self, path: Path, base: dict[str, Any], text: str,
               segments: Sequence[Mapping[str, Any]]) -> Iterator[PreparedMediaChunk]:
        probe = self._probe(path)
        streams = probe.get("streams", [])
        stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if stream is None:
            raise MediaPreparationError("no_video_stream", "原本に映像ストリームがありません")
        duration = self._stream_duration(probe, stream)
        budget = base.get("_query_decode_budget")
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        if budget:
            if width <= 0 or height <= 0 or width * height > 40_000_000:
                raise MediaPreparationError("decoded_budget_exceeded", "動画寸法がquery展開上限を超えています")
            budget.check(width * height * 3 * math.ceil(duration), working=1,
                         units=math.ceil(duration / VIDEO_CHUNK_SECONDS))
            budget.check(width * height * 3 * math.ceil(min(duration, VIDEO_CHUNK_SECONDS)))
        offset = self._stream_offset(path, probe, "v")
        ranges = time_ranges(duration, VIDEO_CHUNK_SECONDS)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for start, end in ranges:
            if budget:
                budget.reserve(width * height * 3 * math.ceil(end - start))
            # Passing a clip path lets the official processor retain FPS metadata.
            # Arrays of frames without FPS cannot construct the model timestamps.
            key = hashlib.sha256(f"v3-bitexact:{base['content_hash']}:{start}:{end}".encode()).hexdigest()
            clip = self.cache_dir / f"{key}.mkv"
            if not clip.is_file():
                temporary = self.cache_dir / f"{key}.{uuid.uuid4().hex}.mkv"
                try:
                    self._run([
                        self._executable("ffmpeg"), "-v", "error", "-nostdin", "-ss", str(start + offset),
                        "-i", self._decoder_path(path), "-map", "0:v:0", "-an",
                        "-vf", f"trim=duration={end - start},setpts=PTS-STARTPTS,fps=1:start_time=0:round=up",
                        "-frames:v", "32", "-c:v", "ffv1",
                        # Stable container bytes are part of candidate input_digest.
                        # v3 leaves old nondeterministic cache entries untouched.
                        "-fflags", "+bitexact", "-flags:v", "+bitexact",
                        "-map_metadata", "-1", str(temporary),
                    ])
                    self._probe(temporary)
                    temporary.replace(clip)
                finally:
                    temporary.unlink(missing_ok=True)
            clip_probe = self._probe(clip, count_frames=True)
            clip_stream = next((item for item in clip_probe.get("streams", [])
                                if item.get("codec_type") == "video"), {})
            frame_count = int(clip_stream.get("nb_read_frames", 0))
            if not 1 <= frame_count <= 32 or frame_count != math.ceil(end - start):
                raise MediaPreparationError("video_coverage_incomplete", "動画区間にデコード可能なフレームがありません")
            yield self._chunk(
                "video", str(clip.resolve()), base, self._lexical(text, segments, start=start + offset, end=end + offset),
                start_seconds=start + offset, end_seconds=end + offset, duration_seconds=duration,
                timeline_start_seconds=offset,
                sampling_fps=1, max_frames=32, frame_count=frame_count,
                **({"decoded_frame_bytes": width * height * 3 * frame_count} if budget else {}),
                full_coverage=verify_full_coverage(ranges, duration),
            )
        # Video embeddings encode visual frames. Index the accompanying sound
        # separately in the same vector space so spoken evidence is retained.
        if any(stream.get("codec_type") == "audio" for stream in streams):
            yield from self._audio(path, {**base, "audio_from_video": True}, text, segments)
