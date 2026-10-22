"""Server-owned multimodal query preparation; index readiness is irrelevant.

Only canonical SourceRefs cross the public internal boundary. Decoders see a
live authorized original, and every source is checked again after inference.
Prepared vectors are request-local capabilities, never API/model arguments.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
from typing import Any, Literal
import uuid

from .index_io import run_index_io
from .media import MediaPreparationError, MediaPreparationService, PreparedMediaChunk, MediaDecodeBudget
from ..services.search_sources import SourceRef, SourceUnavailable

TARGETS = ("docs", "knowledge", "conversation", "media", "files")
MAX_QUERY_UNITS = 256
SOURCE_KINDS = {"chat_attachment", "task_attachment", "docs_attachment", "knowledge_document",
                "generated_media", "generation_output", "meeting_audio", "conversation_message",
                "workspace_file"}
TEXT_SUFFIXES = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml",
                 ".html", ".css", ".sh", ".ps1", ".sql", ".rs", ".go", ".java", ".c",
                 ".cpp", ".h", ".cs", ".toml", ".xml", ".csv", ".log"}
LOCATOR_KEYS = ("page_number", "page_count", "start_seconds", "end_seconds", "frame_index",
                "frame_count", "frame_indices", "sampling_fps", "text_start", "text_end", "text_length", "audio_from_video")
_SEAL = object()


@dataclass(frozen=True)
class QuerySelector:
    """Inclusive pages/frames, half-open seconds/characters. None means all."""
    pages: tuple[int, ...] = ()
    seconds: tuple[float, float] | None = None
    characters: tuple[int, int] | None = None
    frames: tuple[int, ...] = ()

    def __post_init__(self):
        if any(type(p) is not int or p < 1 for p in self.pages):
            raise ValueError("invalid_page_selector")
        if (len(self.frames) > MAX_QUERY_UNITS
                or any(type(p) is not int or p < 0 for p in self.frames)
                or len(set(self.frames)) != len(self.frames)):
            raise ValueError("invalid_frame_selector")
        for bounds in (self.seconds, self.characters):
            if bounds is not None and (len(bounds) != 2 or any(not math.isfinite(x) for x in bounds)
                                       or bounds[0] < 0 or bounds[1] <= bounds[0]):
                raise ValueError("invalid_range_selector")
        if self.characters is not None and any(type(x) is not int for x in self.characters):
            raise ValueError("invalid_character_selector")
        if sum(bool(x) for x in (self.pages, self.seconds, self.characters, self.frames)) > 1:
            raise ValueError("mixed_selectors_unsupported")


@dataclass(frozen=True)
class QuerySource:
    ref: SourceRef | None = None
    text: str | None = None
    selector: QuerySelector = field(default_factory=QuerySelector)
    # Server-owned provenance, never a client/model permission flag.
    authority: Literal["current_context", "selected_scope"] = "current_context"

    def __post_init__(self):
        if not isinstance(self.authority, str) or self.authority not in {"current_context", "selected_scope"}:
            raise ValueError("invalid_query_source_authority")
        if (self.ref is None) == (self.text is None):
            raise ValueError("exactly_one_ref_or_text_required")
        if self.ref is not None:
            if not isinstance(self.ref, SourceRef) or self.ref.kind not in SOURCE_KINDS:
                raise ValueError("unsupported_query_source")
            parts = self.ref.id.split(":")
            uuid.UUID(parts[0])
            if self.ref.kind == "chat_attachment":
                if len(parts) != 2 or not parts[1].isdigit():
                    raise ValueError("invalid_attachment_locator")
            elif len(parts) != 1:
                raise ValueError("invalid_source_locator")
        elif not isinstance(self.text, str) or not self.text:
            raise ValueError("empty_query_text")


@dataclass(frozen=True)
class QuerySourceResolvers:
    """Separate current-turn originals from explicitly selected search refs."""
    current_context: Any
    selected_scope: Any


async def resolve_query_source(resolver, source):
    if isinstance(resolver, QuerySourceResolvers):
        resolver = getattr(resolver, source.authority)
    elif source.authority != "current_context":
        # Legacy callers retain their current-context resolver, but cannot
        # silently interpret the new selected-scope authority as that scope.
        raise SourceUnavailable(reason="query_source_authority_unavailable")
    return await resolver.resolve(source.ref)


@dataclass(frozen=True)
class QueryBudget:
    max_sources: int = 16
    max_units: int = MAX_QUERY_UNITS
    max_source_bytes: int = 128 * 1024 * 1024
    max_decoded_bytes: int = 256 * 1024 * 1024
    max_tokens: int = 8192
    max_partitions: int = 256
    max_search_calls: int = 2048

    def __post_init__(self):
        ceilings = (16, MAX_QUERY_UNITS, 128 * 1024 * 1024, 256 * 1024 * 1024, 8192, 256, 2048)
        if any(type(value) is not int or not 1 <= value <= ceiling
               for value, ceiling in zip(self.__dict__.values(), ceilings)):
            raise ValueError("invalid_server_budget")


@dataclass(frozen=True)
class QueryPlan:
    instruction: str = ""
    sources: tuple[QuerySource, ...] = ()
    mode: str = "combined"
    scope: str = "current_context"
    targets: tuple[str, ...] = TARGETS
    limit: int = 20
    budget: QueryBudget = field(default_factory=QueryBudget)
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1 or self.mode not in {"combined", "separate"}:
            raise ValueError("unsupported_query_plan")
        if self.scope not in {"current_context", "project", "space", "all_readable"}:
            raise ValueError("unsupported_query_scope")
        if not self.targets or set(self.targets) - set(TARGETS) or len(set(self.targets)) != len(self.targets):
            raise ValueError("unsupported_query_targets")
        if not isinstance(self.instruction, str) or not (self.instruction.strip() or self.sources):
            raise ValueError("empty_query")
        if any(not isinstance(source, QuerySource) for source in self.sources):
            raise ValueError("invalid_query_source")
        if any(tag in self.instruction or any(tag in (source.text or "") for source in self.sources)
               for tag in ("<|image|>", "<|audio|>", "<|video|>")):
            raise ValueError("reserved_query_placeholder")
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError("invalid_query_limit")
        if len(self.sources) > self.budget.max_sources:
            raise ValueError("source_budget_exceeded")


class QueryPreparationError(RuntimeError):
    def __init__(self, reason, source_status):
        self.reason, self.source_status = reason, source_status
        super().__init__(reason)


@dataclass(frozen=True)
class PreparedQueryUnit:
    vector: tuple[float, ...]
    lexical_text: str
    matched_query_sources: tuple[dict, ...]
    _seal: object = field(repr=False)


@dataclass(frozen=True)
class PreparedQueryVideo:
    path: Path
    _seal: object = field(repr=False)


def require_query_video(value):
    if not isinstance(value, PreparedQueryVideo) or value._seal is not _SEAL:
        raise PermissionError("server_prepared_video_required")
    return value.path


def require_prepared(unit):
    if not isinstance(unit, PreparedQueryUnit) or unit._seal is not _SEAL:
        raise PermissionError("server_prepared_query_required")
    return list(unit.vector)


def prepared_dense_cosine(query, dense, dimension=None):
    """Cosine of a prepared query and one stored dense vector, or ``None``.

    Hybrid indexes expose a partition-local RRF point score. The stored dense
    vector is the only score comparable across partitions and lanes. Missing,
    malformed, non-finite or zero-norm vectors fail closed with ``None``.
    """
    if not isinstance(query, (list, tuple)) or not isinstance(dense, (list, tuple)):
        return None
    if len(query) == 0 or len(query) != len(dense):
        return None
    if isinstance(dimension, int) and len(query) != dimension:
        return None

    def finite_values(values):
        result = []
        for value in values:
            if isinstance(value, (bool, str, bytes)):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(number):
                return None
            result.append(number)
        return result

    query_values = finite_values(query)
    dense_values = finite_values(dense)
    if query_values is None or dense_values is None:
        return None
    query_norm = math.sqrt(sum(value * value for value in query_values))
    dense_norm = math.sqrt(sum(value * value for value in dense_values))
    if not math.isfinite(query_norm) or not math.isfinite(dense_norm) or not query_norm or not dense_norm:
        return None
    score = sum(left * right for left, right in zip(query_values, dense_values)) / (query_norm * dense_norm)
    if not math.isfinite(score):
        return None
    return max(-1.0, min(1.0, score))


@dataclass
class PreparedQuery:
    units: list[PreparedQueryUnit]
    source_status: list[dict]
    _snapshots: list[tuple[QuerySource, str]] = field(repr=False)


def source_hash(source):
    digest = hashlib.sha256()
    if source.path is not None:
        with source.path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    else:
        digest.update(source.text.encode("utf-8"))
    # Scope/title changes also invalidate an in-flight query.
    import json
    digest.update(json.dumps([source.title, source.metadata], sort_keys=True, default=str).encode())
    return digest.hexdigest()


class MediaQueryPreparer:
    def __init__(self, embedding, preparation=None):
        self.embedding = embedding
        self.preparation = preparation or MediaPreparationService()

    async def reauthorize(self, prepared, resolver):
        for source, expected in prepared._snapshots:
            current = await resolve_query_source(resolver, source)
            if await run_index_io(source_hash, current) != expected:
                raise SourceUnavailable(reason="query_source_changed")

    def _video_selection(self, chunk, selector, decode_budget):
        """Select within server-decoded clips; no caller paths reach ffmpeg."""
        from PIL import Image
        meta = chunk.metadata
        left, right = meta["start_seconds"], meta["end_seconds"]
        clip = Path(chunk.model_input["video"])
        if selector.frames:
            # Frames address the existing 1fps sample timeline, zero-based.
            origin = meta.get("timeline_start_seconds", 0)
            indices = [i for i in selector.frames if left <= origin + i < right]
            if not indices:
                return
            # Canonical clips already reserve their frames. Extracted PIL
            # images are additional retained allocations and decoder work.
            frame_count = meta.get("frame_count", 0)
            frame_bytes = meta.get("decoded_frame_bytes", 0)
            if (type(frame_count) is not int or not 1 <= frame_count <= 32
                    or type(frame_bytes) is not int or frame_bytes <= 0
                    or frame_bytes % frame_count):
                raise MediaPreparationError("decoded_budget_exceeded", "動画フレームの展開サイズを確認できません")
            frame_bytes //= frame_count
            decode_budget.check(frame_bytes * len(indices), units=len(indices))
            for index in indices:
                decode_budget.reserve(frame_bytes)
                timestamp = origin + index
                result = self.preparation._run([self.preparation._executable("ffmpeg"), "-v", "error",
                    "-nostdin", "-ss", str(timestamp-left), "-i", str(clip), "-frames:v", "1",
                    "-f", "image2pipe", "-c:v", "png", "pipe:1"])
                import io
                with Image.open(io.BytesIO(result.stdout)) as image:
                    if image.width * image.height * 3 > frame_bytes:
                        raise MediaPreparationError("decoded_budget_exceeded", "動画フレームが予約サイズを超えています")
                    yield PreparedMediaChunk(f"{chunk.id}:frame:{index}", "image", {"image": image.convert("RGB").copy()}, "",
                        {**meta, "frame_index": index, "start_seconds": timestamp, "end_seconds": min(timestamp+1, right)})
            return
        if selector.seconds:
            start, end = max(selector.seconds[0], left), min(selector.seconds[1], right)
            if end <= start:
                return
            if start != left or end != right:
                # Keep native video FPS metadata for an exact selected interval.
                key = hashlib.sha256(f"query:{chunk.id}:{start}:{end}".encode()).hexdigest()
                selected_clip = self.preparation.cache_dir / f"{key}.mkv"
                if not selected_clip.is_file():
                    temporary = selected_clip.with_name(f"{key}.{uuid.uuid4().hex}.mkv")
                    try:
                        self.preparation._run([self.preparation._executable("ffmpeg"), "-v", "error", "-nostdin",
                            "-ss", str(start-left), "-i", str(clip), "-an", "-vf",
                            f"trim=duration={end-start},setpts=PTS-STARTPTS,fps=1:start_time=0:round=up",
                            "-frames:v", "32", "-c:v", "ffv1", str(temporary)])
                        temporary.replace(selected_clip)
                    finally:
                        temporary.unlink(missing_ok=True)
                chunk.model_input = {"video": str(selected_clip)}
                meta.update(start_seconds=start, end_seconds=end, frame_count=math.ceil(end-start))
        yield chunk

    @staticmethod
    def _text_chunks(text, key):
        for start in range(0, len(text), 1536):
            end = min(start + 1536, len(text))
            yield PreparedMediaChunk(f"{key}:{start}", "text", {"text": text[start:end]}, text[start:end],
                                     {"text_start": start, "text_end": end, "text_length": len(text)})

    def _chunks(self, source, budget):
        if source.path is None:
            yield from self._text_chunks(source.text, source.ref.key)
        elif (Path(source.title).suffix.lower() in TEXT_SUFFIXES
              or str(source.metadata.get("mime_type", "")).startswith("text/")):
            # Strict decode: a binary/invalid text file is never a lossy query.
            with source.path.open("r", encoding="utf-8-sig") as stream:
                length = sum(len(text) for text in iter(lambda: stream.read(1536), ""))
            with source.path.open("r", encoding="utf-8-sig") as stream:
                offset = 0
                while text := stream.read(1536):
                    yield PreparedMediaChunk(f"{source.ref.key}:{offset}", "text", {"text": text}, text,
                        {"text_start": offset, "text_end": offset + len(text), "text_length": length})
                    offset += len(text)
        else:
            for chunk in self.preparation.iter_prepare(source.path, source_id=source.ref.key,
                    source_kind=source.ref.kind, metadata={**source.metadata, "file_name": source.title},
                    mime_type=source.metadata.get("mime_type"), decode_budget=budget):
                # Mail preparation builds Document inputs; query uses its raw evidence.
                if chunk.modality == "text":
                    chunk.model_input = {"text": chunk.lexical_text}
                yield chunk

    @staticmethod
    def _selected(chunk, selector):
        meta = chunk.metadata
        if selector.pages:
            if "page_number" not in meta:
                raise MediaPreparationError("unsupported_page_selector", "ページ位置を指定できません")
            return meta.get("page_number") in selector.pages
        if selector.frames:
            if "frame_index" not in meta:
                raise MediaPreparationError("unsupported_frame_selector", "動画は秒区間を指定してください")
            return meta["frame_index"] in selector.frames
        if selector.characters:
            if chunk.modality != "text":
                raise MediaPreparationError("unsupported_character_selector", "文字位置を指定できません")
            start, end = selector.characters
            left, right = meta["text_start"], meta["text_end"]
            if start >= right or end <= left:
                return False
            chunk.lexical_text = chunk.lexical_text[max(start - left, 0):min(end, right) - left]
            chunk.model_input = {"text": chunk.lexical_text}
            meta.update(text_start=max(start, left), text_end=min(end, right))
        if selector.seconds:
            if "start_seconds" not in meta:
                raise MediaPreparationError("unsupported_time_selector", "時間区間を指定できません")
            start, end = selector.seconds
            left, right = meta["start_seconds"], meta["end_seconds"]
            if start >= right or end <= left:
                return False
            if chunk.modality == "audio":
                chunk.model_input = {"audio": chunk.model_input["audio"][round(max(start-left, 0)*16000):round((min(end,right)-left)*16000)]}
                meta.update(start_seconds=max(start, left), end_seconds=min(end, right))
        return True

    @staticmethod
    def _size(chunk):
        if chunk.modality == "image":
            value = chunk.model_input["image"]
            return value.width * value.height * 3
        if chunk.modality == "audio":
            return chunk.model_input["audio"].nbytes
        if chunk.modality == "video":
            return max(chunk.metadata.get("decoded_frame_bytes", 0), Path(chunk.model_input["video"]).stat().st_size)
        return len(chunk.lexical_text.encode("utf-8"))

    async def prepare(self, plan, resolver):
        statuses, snapshots, groups, decoded = [], [], [], 0
        decode_budget = MediaDecodeBudget(plan.budget.max_decoded_bytes, plan.budget.max_units)
        sources = plan.sources or (QuerySource(text=plan.instruction),)
        try:
            if sum(len(source.selector.frames) for source in sources) > plan.budget.max_units:
                raise MediaPreparationError("unit_budget_exceeded", "選択したquery unitが処理予算を超えています")
            originals = {}
            # Resolve/hash every source before decoding any of them. A bad
            # final attachment cannot cause earlier originals to be decoded.
            for index, query_source in enumerate(sources):
                key = query_source.ref.key if query_source.ref else f"inline:{index}"
                statuses.append({"source": key, "status": "pending", "total_units": None,
                                 "processed_units": 0, "processed_ranges": []})
                if query_source.ref:
                    original = await resolve_query_source(resolver, query_source)
                    if original.path is not None and original.path.stat().st_size > plan.budget.max_source_bytes:
                        raise MediaPreparationError("source_budget_exceeded", "原本の処理予算を超えています")
                    originals[index] = original
                    snapshots.append((query_source, await run_index_io(source_hash, original)))
            for index, query_source in enumerate(sources):
                key = query_source.ref.key if query_source.ref else f"inline:{index}"
                status = statuses[index]
                if query_source.ref:
                    iterator = self._chunks(originals[index], decode_budget)
                else:
                    iterator = self._text_chunks(query_source.text, key)
                selected, total = [], 0
                try:
                    def next_chunk():
                        return next(iterator, None)
                    while (chunk := await run_index_io(next_chunk)) is not None:
                        if "_query_decode_budget" not in chunk.metadata:
                            decode_budget.reserve(self._size(chunk) * (4 if chunk.modality == "text" else 1))
                        total += 1
                        if total + sum(s.get("total_units") or 0 for s in statuses[:index]) > plan.budget.max_units:
                            raise MediaPreparationError("unit_budget_exceeded", "全query unitを処理する予算がありません")
                        selections = (self._video_selection(chunk, query_source.selector, decode_budget)
                            if chunk.modality == "video" and (query_source.selector.seconds or query_source.selector.frames)
                            else iter(()) if query_source.selector.frames and chunk.modality == "audio" and chunk.metadata.get("audio_from_video")
                            else iter((chunk,)) if self._selected(chunk, query_source.selector) else iter(()))
                        try:
                            while (selection := await run_index_io(next, selections, None)) is not None:
                                if len(selected) + sum(s["processed_units"] for s in statuses[:index]) >= plan.budget.max_units:
                                    raise MediaPreparationError("unit_budget_exceeded", "選択したquery unitが処理予算を超えています")
                                decoded += self._size(selection)
                                if decoded > plan.budget.max_decoded_bytes:
                                    raise MediaPreparationError("decoded_budget_exceeded", "展開後の処理予算を超えています")
                                citation = {k: selection.metadata[k] for k in LOCATOR_KEYS if k in selection.metadata}
                                selected.append((selection, {"source": key, "unit_id": selection.id, "citation": citation}))
                                status["processed_units"] = len(selected)
                                status["processed_ranges"].append(citation)
                        finally:
                            if hasattr(selections, "close"):
                                await run_index_io(selections.close)
                    status["total_units"] = total
                finally:
                    await run_index_io(iterator.close)
                if not selected:
                    raise MediaPreparationError("empty_query_selection", "選択したquery範囲がありません")
                pages = {c.metadata.get("page_number") for c, _ in selected}
                frames = {c.metadata.get("frame_index") for c, _ in selected}
                if set(query_source.selector.pages) - pages or set(query_source.selector.frames) - frames:
                    raise MediaPreparationError("query_selector_out_of_range", "指定したquery unitがありません")
                if query_source.selector.characters and query_source.selector.characters[1] > selected[-1][0].metadata["text_length"]:
                    raise MediaPreparationError("query_selector_out_of_range", "文字範囲が原本を超えています")
                if query_source.selector.seconds:
                    start, end = query_source.selector.seconds
                    cursor = start
                    intervals = sorted((c.metadata["start_seconds"], c.metadata["end_seconds"]) for c, _ in selected)
                    for left, right in intervals:
                        if right <= cursor:
                            continue
                        if left > cursor + 1e-6:
                            raise MediaPreparationError("query_selector_coverage_incomplete", "指定時間範囲に原本の空白があります")
                        cursor = max(cursor, right)
                    if cursor < end - 1e-6:
                        raise MediaPreparationError("query_selector_out_of_range", "時間範囲が原本を超えています")
                status.update(status="prepared", processed_units=len(selected), processed_ranges=[m["citation"] for _, m in selected])
                groups.append(selected)
            prepared = PreparedQuery([], statuses, snapshots)
            await self.reauthorize(prepared, resolver)
            # Linear aligned windows, never the Cartesian product of sources.
            windows = ([[item] for group in groups for item in group] if plan.mode == "separate"
                       else [[group[min(i, len(group)-1)] for group in groups] for i in range(max(map(len, groups)))])
            # Reserve repeated window media and IPC serialization headroom
            # before PNG/base64 packing. A short image is not free to repeat.
            window_bytes = sum(self._size(chunk) for window in windows for chunk, _ in window)
            if plan.sources:
                window_bytes += len(windows) * len(plan.instruction) * 4
            if window_bytes * 8 > plan.budget.max_decoded_bytes:
                raise MediaPreparationError("query_window_budget_exceeded", "query window展開の処理予算を超えています")
            native, matches, lexical = [], [], []
            for window in windows:
                text_parts = [plan.instruction] if plan.sources and plan.instruction else []
                item, evidence, words = {}, [], []
                for chunk, match in window:
                    evidence.append(match)
                    if chunk.modality == "text":
                        text_parts.append(chunk.lexical_text)
                        words.append(chunk.lexical_text)
                    else:
                        text_parts.append(f"<|{chunk.modality}|>")
                        value = chunk.model_input[chunk.modality]
                        if chunk.modality == "video":
                            value = PreparedQueryVideo(Path(value), _SEAL)
                        item.setdefault(chunk.modality, []).append(value)
                text = "\n".join(text_parts)
                if item:
                    # Single media-only input needs no fabricated text/prompt.
                    if len(window) > 1 or words or (plan.sources and plan.instruction):
                        item["text"] = text
                    native.append({key: value[0] if key != "text" and len(value) == 1 else value for key, value in item.items()})
                else:
                    native.append(text)
                lexical.append("\n".join([plan.instruction if plan.sources else "", *words]).strip())
                matches.append(tuple(evidence))
            vectors = await self.embedding.encode_query_inputs(native, max_tokens=plan.budget.max_tokens)
            if len(vectors) != len(native):
                error = self.embedding.last_error or "worker_failed"
                reason = ("token_budget_exceeded" if "query_token_budget_exceeded" in error else
                          "worker_timeout" if "timeout" in error.lower() else
                          "worker_stopped" if any(x in error.lower() for x in ("stopped", "exited", "brokenpipe", "unavailable"))
                          else "worker_failed")
                raise MediaPreparationError(reason, reason)
            await self.reauthorize(prepared, resolver)
            prepared.units = [PreparedQueryUnit(tuple(v), text, match, _SEAL) for v, text, match in zip(vectors, lexical, matches)]
            for status in statuses:
                status["status"] = "complete"
            return prepared
        except (SourceUnavailable, MediaPreparationError, UnicodeError, OSError, RuntimeError, ValueError) as exc:
            reason = getattr(exc, "reason", "query_decode_failed")
            for status in statuses:
                if status["status"] != "complete":
                    status.update(status="failed", reason=reason)
            for index in range(len(statuses), len(sources)):
                source = sources[index]
                statuses.append({"source": source.ref.key if source.ref else f"inline:{index}", "status": "not_processed",
                                 "total_units": None, "processed_units": 0, "processed_ranges": [], "reason": reason})
            raise QueryPreparationError(reason, statuses) from exc
