"""キャラクター Irodori 参照音声資産・WASAPI 録音・試聴 API。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from ...services.character_service import CharacterNotFoundError, get_character
from ...services.character_voice_asset_service import (
    MAX_ASSET_BYTES,
    CharacterVoiceAssetCharacterNotFoundError,
    CharacterVoiceAssetError,
    CharacterVoiceAssetNotFoundError,
    CharacterVoiceAssetService,
    get_character_voice_asset_service,
)
from ...audio.wasapi_loopback_recorder import (
    CaptureNotFoundError,
    WasapiLoopbackError,
    WasapiLoopbackRecorder,
)
from ...tts.irodori_config import resolve_irodori_checkpoint
from .schemas import VoiceAssetOrderRequest, VoiceCaptureStartRequest, VoicePreviewRequest

logger = logging.getLogger(__name__)


async def _read_upload_bounded(file: UploadFile, *, max_bytes: int = MAX_ASSET_BYTES) -> bytes:
    """UploadFile を上限+1 byte まで読み、過大な body を早期拒否する。

    ``UploadFile.read()`` の無制限呼び出しは service 層の検証に到達する前に
    大きなメモリ確保を起こすため、multipart upload は chunk 読み込みにする。
    呼び出し側が所有する UploadFile は、成功/失敗を問わずここで close する。
    """

    limit = max(1, int(max_bytes))
    chunks: list[bytes] = []
    total = 0
    try:
        while total <= limit:
            chunk = await file.read(min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            payload = bytes(chunk)
            total += len(payload)
            if total > limit:
                raise CharacterVoiceAssetError(
                    f"音声ファイルが大きすぎます（上限 {limit} bytes）",
                    status_code=413,
                )
            chunks.append(payload)
        return b"".join(chunks)
    finally:
        close = getattr(file, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def _http_error(exc: Exception) -> HTTPException:
    status = getattr(exc, "status_code", 500)
    detail = getattr(exc, "message", None) or str(exc)
    return HTTPException(status_code=int(status), detail=detail)


def _asset_download_filename(metadata: Mapping[str, Any]) -> str:
    """Build a safe, single-extension filename for an inline WAV response.

    ``display_name`` is user-controlled metadata (and older records may not
    have gone through the current cleaner), so strip path/control characters
    before handing it to Starlette's ``FileResponse``.  The endpoint serves
    canonical WAV data regardless of the original display name; do not append
    ``.wav`` a second time when the name already has that suffix.
    """

    raw = str(metadata.get("display_name") or "reference")
    # Treat both separators as path separators so this remains safe when a
    # database created on another platform is served on Windows/Linux.
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        "_" if ord(char) < 0x20 or char in {'"', "\x7f"} else char
        for char in name
    ).strip(" .")
    if not name:
        name = "reference"
    # Collapse accidental repeated suffixes from legacy/UI metadata before
    # ensuring exactly one canonical extension.
    while name.lower().endswith(".wav.wav"):
        name = name[:-4]
    if not name.lower().endswith(".wav"):
        name += ".wav"
    if len(name) > 200:
        name = name[:-4][:196] + ".wav"
    return name


async def _call_nonblocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """同期モデル推論をイベントループ外で実行する。"""

    def invoke() -> Any:
        value = function(*args, **kwargs)
        if inspect.isawaitable(value):
            # The coroutine is created and consumed in the worker thread, so
            # model loading/inference cannot monopolize the request loop.
            return asyncio.run(value)
        return value

    return await asyncio.to_thread(invoke)


def _manager_from_app(app_instance: Any) -> Any:
    if app_instance is None:
        return None
    for attr in ("tts_manager", "voice_tts_manager", "voice_chat_mode"):
        candidate = getattr(app_instance, attr, None)
        if attr == "voice_chat_mode":
            candidate = getattr(candidate, "tts_manager", None)
        if candidate is not None:
            return candidate
    return None


def _tts_config_from_app(app_instance: Any) -> Any:
    """WebChatServer/Config の形を TTSManager が受け取れる形へ寄せる。"""

    config = getattr(app_instance, "config", None)
    nested = getattr(config, "config", None)
    if nested is not None:
        config = nested
    if isinstance(config, Mapping) or callable(getattr(config, "get", None)):
        return config
    return {}


async def _resolve_irodori_engine(
    app_instance: Any,
    *,
    checkpoint: Optional[str] = None,
    checkpoint_is_explicit: Optional[bool] = None,
    preview_state: Optional[dict[str, Any]] = None,
    init_lock: Optional[asyncio.Lock] = None,
) -> Any:
    """既存 live engine を優先し、専用 manager を遅延初期化する。

    WebChatServer は通常 TTSManager を直接保持しない。試聴だけの専用
    manager を用意し、live manager の ``current_engine`` を変更しない。
    ``preview_state`` と ``init_lock`` は router closure から渡され、同時
    リクエストによる重複モデル初期化を防ぐ。
    """

    if checkpoint_is_explicit is None:
        checkpoint_is_explicit = checkpoint is not None

    # Always derive an effective checkpoint for preview cache identity.  A
    # missing character selector must not inherit a stale v3 preview engine;
    # app-level explicit settings (including legacy v3) still win, otherwise
    # the shared resolver returns the v4.1 default.
    if checkpoint is None:
        app_config = _tts_config_from_app(app_instance)
        global_irodori = {}
        if isinstance(app_config, Mapping):
            tts_settings = app_config.get("tts_settings", {})
            if isinstance(tts_settings, Mapping):
                candidate = tts_settings.get("irodori_tts", {})
                if isinstance(candidate, Mapping):
                    global_irodori = candidate
        checkpoint = resolve_irodori_checkpoint({}, fallback_settings=global_irodori)

    live_manager = _manager_from_app(app_instance)
    live_engines = getattr(live_manager, "engines", None)
    live_engine = (
        live_engines.get("irodori_tts")
        if isinstance(live_engines, Mapping)
        else None
    )
    live_checkpoint = getattr(live_engine, "hf_checkpoint", None) if live_engine is not None else None
    if live_engine is not None and (
        (isinstance(live_checkpoint, str) and live_checkpoint.strip() == checkpoint)
        # A legacy live adapter may not expose checkpoint metadata.  Preserve
        # its historical reuse only for an implicitly resolved default; a
        # selector/local/legacy explicit request must be verified strictly.
        or (not checkpoint_is_explicit and not isinstance(live_checkpoint, str))
    ):
        if preview_state is not None:
            preview_state["live_manager"] = live_manager
        return live_engine

    state = preview_state if preview_state is not None else {}
    lock = init_lock or asyncio.Lock()
    async with lock:
        manager = state.get("manager")
        if manager is None:
            try:
                from ...tts.manager import TTSManager

                manager = TTSManager(_tts_config_from_app(app_instance))
                state["manager"] = manager
            except Exception:
                logger.exception("Irodori 試聴用 TTSManager の初期化に失敗しました")
                return None

        engines = getattr(manager, "engines", None)
        cache = state.setdefault("engines_by_checkpoint", {})
        cache_key = checkpoint
        engine = cache.get(cache_key) if isinstance(cache, Mapping) else None
        if engine is None:
            engine = engines.get("irodori_tts") if isinstance(engines, Mapping) else None
            existing_checkpoint = getattr(engine, "hf_checkpoint", None)
            if not isinstance(existing_checkpoint, str) or existing_checkpoint.strip() != checkpoint:
                engine = None
        if engine is not None:
            if isinstance(cache, dict):
                cache[cache_key] = engine
            return engine

        creator = getattr(manager, "create_irodori_tts_engine", None)
        if not callable(creator):
            return None
        try:
            try:
                engine = await _call_nonblocking(creator, hf_checkpoint=checkpoint)
            except TypeError:
                # Older test/integration managers did not yet expose the
                # keyword.  They may still be used when their engine does not
                # advertise a checkpoint; a known mismatch is never accepted.
                engine = await _call_nonblocking(creator)
                actual = getattr(engine, "hf_checkpoint", None)
                if not isinstance(actual, str) or actual.strip() != checkpoint:
                    # Keep compatibility for a legacy adapter only when this
                    # was an implicit default and no checkpoint identity is
                    # available. Explicit model requests must fail closed.
                    if checkpoint_is_explicit or isinstance(actual, str):
                        return None
            actual = getattr(engine, "hf_checkpoint", None)
            if not isinstance(actual, str) or actual.strip() != checkpoint:
                # Unknown legacy adapters are tolerated only for the implicit
                # default; a known checkpoint mismatch is always rejected.
                if checkpoint_is_explicit or isinstance(actual, str):
                    return None
        except Exception:
            logger.exception("Irodori-TTS エンジンの試聴初期化に失敗しました")
            return None
        if engine is None:
            return None
        register = getattr(manager, "register_engine", None)
        if callable(register):
            register("irodori_tts", engine)
        if isinstance(cache, dict):
            cache[cache_key] = engine
        # This is an isolated preview manager.  Changing its current_engine is
        # safe and allows the manager fallback branch below; never call
        # set_engine on the live manager.
        setter = getattr(manager, "set_engine", None)
        if callable(setter):
            try:
                setter("irodori_tts")
            except Exception:
                logger.debug("Irodori preview manager の engine 切替に失敗", exc_info=True)
        return engine


def _register_recorder_shutdown(app_instance: Any, recorder: Any) -> None:
    """WebChatServer lifespan の shutdown queue に recorder cleanup を登録。"""

    if app_instance is None:
        return
    shutdown_tasks = getattr(app_instance, "_shutdown_background_tasks", None)
    if not isinstance(shutdown_tasks, list):
        return
    registered = getattr(app_instance, "_aoitalk_voice_recorder_shutdowns", None)
    if not isinstance(registered, set):
        registered = set()
        try:
            setattr(app_instance, "_aoitalk_voice_recorder_shutdowns", registered)
        except Exception:
            return
    recorder_key = id(recorder)
    if recorder_key in registered:
        return

    async def shutdown_recorder() -> None:
        # shutdown() joins the worker and removes partial files; do not block
        # the server's lifespan event loop while doing so.
        result = await asyncio.to_thread(recorder.shutdown)
        if inspect.isawaitable(result):
            await result

    shutdown_tasks.append(shutdown_recorder)
    registered.add(recorder_key)


def build_character_voice_router(
    app_instance: Any = None,
    require_auth: Optional[Callable[..., Any]] = None,
    *,
    service: Optional[CharacterVoiceAssetService] = None,
    recorder: Optional[WasapiLoopbackRecorder] = None,
) -> APIRouter:
    """キャラクター管理配下の音声資産ルーターを構築する。

    ``build_character_voice_router(require_auth)`` という旧テスト向けの
    1引数呼び出しも受理する。
    """

    if require_auth is None and callable(app_instance):
        require_auth = app_instance
        app_instance = None
    if require_auth is None:
        async def require_auth(_request: Any) -> None:  # type: ignore[no-redef]
            return None

    asset_service = service or get_character_voice_asset_service()
    if recorder is None and app_instance is not None:
        # Reuse one recorder when the ECC router is assembled more than once;
        # this also makes shutdown hook registration idempotent.
        recorder = getattr(app_instance, "_aoitalk_voice_loopback_recorder", None)
    loopback = recorder or WasapiLoopbackRecorder()
    if recorder is None and app_instance is not None:
        try:
            setattr(app_instance, "_aoitalk_voice_loopback_recorder", loopback)
        except Exception:
            pass
    _register_recorder_shutdown(app_instance, loopback)
    router = APIRouter(prefix="/api/characters/manage", tags=["character-voice"])
    captures: dict[str, str] = {}
    finalized_captures: dict[str, dict[str, Any]] = {}
    finalize_lock = asyncio.Lock()
    preview_state: dict[str, Any] = {}
    preview_init_lock = asyncio.Lock()

    @router.get("/{character_id}/voice-assets")
    async def list_voice_assets(character_id: str, _=Depends(require_auth)):
        try:
            return JSONResponse(content={"success": True, **(await asset_service.list_assets(character_id))})
        except CharacterVoiceAssetError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("参照音声一覧取得エラー")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/{character_id}/voice-assets")
    async def upload_voice_asset(
        character_id: str,
        file: UploadFile = File(...),
        display_name: Optional[str] = Form(None),
        _=Depends(require_auth),
    ):
        try:
            content = await _read_upload_bounded(file)
            metadata = await asset_service.add_asset(
                character_id,
                content,
                filename=file.filename or "reference.wav",
                display_name=display_name,
                source_kind="upload",
            )
            return JSONResponse(content={"success": True, "asset": metadata}, status_code=201)
        except CharacterVoiceAssetError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("参照音声アップロードエラー")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def _voice_asset_response(character_id: str, asset_id: str) -> Response:
        try:
            metadata = await asset_service.get_asset(character_id, asset_id)
            path = await asset_service.resolve_asset_path(character_id, asset_id)
            return FileResponse(
                path,
                media_type="audio/wav",
                filename=_asset_download_filename(metadata),
                content_disposition_type="inline",
                headers={
                    # User recordings are privacy-sensitive; prevent browser
                    # and intermediary caches from retaining them.
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except CharacterVoiceAssetError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("参照音声取得エラー")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/{character_id}/voice-assets/{asset_id}/content")
    async def get_voice_asset_content(character_id: str, asset_id: str, _=Depends(require_auth)):
        return await _voice_asset_response(character_id, asset_id)

    @router.get("/{character_id}/voice-assets/{asset_id}")
    async def get_voice_asset(character_id: str, asset_id: str, _=Depends(require_auth)):
        return await _voice_asset_response(character_id, asset_id)

    @router.delete("/{character_id}/voice-assets/{asset_id}")
    async def delete_voice_asset(character_id: str, asset_id: str, _=Depends(require_auth)):
        try:
            removed = await asset_service.delete_asset(character_id, asset_id)
            return JSONResponse(content={"success": True, "asset": removed})
        except CharacterVoiceAssetError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("参照音声削除エラー")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.patch("/{character_id}/voice-assets/order")
    async def reorder_voice_assets(
        character_id: str,
        request: VoiceAssetOrderRequest,
        _=Depends(require_auth),
    ):
        try:
            assets = await asset_service.reorder_assets(character_id, request.asset_ids)
            return JSONResponse(content={"success": True, "assets": assets})
        except CharacterVoiceAssetError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("参照音声順序変更エラー")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/{character_id}/voice-assets/devices")
    async def list_loopback_devices(character_id: str, _=Depends(require_auth)):
        try:
            # Device enumeration is exposed under a character URL but must not
            # allow probing/recording for an arbitrary fake character.
            await get_character(character_id)
            devices = await asyncio.to_thread(loopback.list_devices)
            return JSONResponse(content={"success": True, "devices": devices})
        except CharacterNotFoundError as exc:
            raise _http_error(exc) from exc
        except WasapiLoopbackError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("WASAPIデバイス一覧取得エラー")
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/{character_id}/voice-assets/capture/start")
    async def start_loopback_capture(
        character_id: str,
        request: VoiceCaptureStartRequest = Body(default=VoiceCaptureStartRequest()),
        _=Depends(require_auth),
    ):
        try:
            await get_character(character_id)
            result = await asyncio.to_thread(
                loopback.start,
                request.device_index,
                device_id=request.device_id,
                character_id=character_id,
            )
            captures[result["capture_id"]] = character_id
            return JSONResponse(content={"success": True, "capture": result}, status_code=201)
        except CharacterNotFoundError as exc:
            raise _http_error(exc) from exc
        except WasapiLoopbackError as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("WASAPI録音開始エラー")
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def _finalize_capture(capture_id: str) -> Optional[dict[str, Any]]:
        character_id = captures.get(capture_id)
        if not character_id:
            raise CaptureNotFoundError(capture_id)
        if capture_id in finalized_captures:
            return finalized_captures[capture_id]
        async with finalize_lock:
            if capture_id in finalized_captures:
                return finalized_captures[capture_id]
            status = await asyncio.to_thread(loopback.get_status, capture_id)
            if status.get("status") != "ready":
                return None
            path = await asyncio.to_thread(loopback.take_output, capture_id)
            try:
                asset = await asset_service.add_asset_from_file(
                    character_id,
                    path,
                    display_name=f"PCスピーカー録音 {capture_id[:8]}",
                    source_kind="wasapi_loopback",
                )
            finally:
                loopback.cleanup_capture(capture_id)
            finalized_captures[capture_id] = asset
            return asset

    @router.post("/{character_id}/voice-assets/capture/{capture_id}/stop")
    async def stop_loopback_capture(character_id: str, capture_id: str, _=Depends(require_auth)):
        if captures.get(capture_id) != character_id:
            raise HTTPException(status_code=404, detail="録音が見つかりません")
        try:
            status = await asyncio.to_thread(loopback.stop, capture_id, wait=True)
            asset = await _finalize_capture(capture_id)
            return JSONResponse(content={"success": True, "capture": status, "asset": asset})
        except (WasapiLoopbackError, CharacterVoiceAssetError) as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("WASAPI録音停止エラー")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/{character_id}/voice-assets/capture/{capture_id}")
    async def get_loopback_capture_status(character_id: str, capture_id: str, _=Depends(require_auth)):
        if captures.get(capture_id) != character_id:
            raise HTTPException(status_code=404, detail="録音が見つかりません")
        try:
            status = await asyncio.to_thread(loopback.get_status, capture_id)
            asset = await _finalize_capture(capture_id)
            return JSONResponse(content={"success": True, "capture": status, "asset": asset})
        except (WasapiLoopbackError, CharacterVoiceAssetError) as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("WASAPI録音状態取得エラー")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/{character_id}/voice-assets/preview")
    async def preview_voice(
        character_id: str,
        request: VoicePreviewRequest,
        _=Depends(require_auth),
    ):
        text = str(request.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="試聴テキストを入力してください")
        try:
            character = await get_character(character_id)
            parameters = character.get("voice_parameters") or {}
            if not isinstance(parameters, dict):
                parameters = {}
            # Resolve the same character selector/checkpoint contract used by
            # normal TTS.  ``irodori_model`` is intentionally request-local so
            # an unsaved UI edit can be previewed without changing the DB.
            preview_settings = dict(parameters)
            if request.irodori_model is not None:
                preview_settings["irodori_model"] = request.irodori_model
            app_config = _tts_config_from_app(app_instance)
            global_irodori = {}
            if isinstance(app_config, Mapping):
                tts_settings = app_config.get("tts_settings", {})
                if isinstance(tts_settings, Mapping):
                    candidate = tts_settings.get("irodori_tts", {})
                    if isinstance(candidate, Mapping):
                        global_irodori = candidate
            has_explicit_checkpoint = any(
                key in preview_settings
                for key in ("irodori_model", "hf_checkpoint", "voice_design_checkpoint")
            ) or any(
                key in global_irodori
                for key in ("hf_checkpoint", "voice_design_checkpoint", "irodori_model")
            )
            checkpoint = resolve_irodori_checkpoint(
                preview_settings,
                fallback_settings=global_irodori,
            )
            records = parameters.get("irodori_reference_assets") or []
            ref_wavs: list[str] = []
            for record in records if isinstance(records, list) else []:
                asset_id = str(record.get("id") or "") if isinstance(record, dict) else ""
                if asset_id:
                    ref_wavs.append(str(await asset_service.resolve_asset_path(character_id, asset_id)))
            caption = request.caption if request.caption is not None else parameters.get("caption")
            caption = None if caption is None else str(caption)
            engine = await _resolve_irodori_engine(
                app_instance,
                checkpoint=checkpoint,
                checkpoint_is_explicit=has_explicit_checkpoint,
                preview_state=preview_state,
                init_lock=preview_init_lock,
            )
            # A live manager may be serving another engine.  It is safe to
            # reuse its Irodori engine above, but never call its generic
            # ``synthesize`` fallback (which could change/route normal
            # conversation audio) when preview initialization failed.  Only
            # the isolated, cached preview manager may be used here.
            manager = preview_state.get("manager")
            if engine is not None and callable(getattr(engine, "synthesize", None)):
                audio = await _call_nonblocking(
                    engine.synthesize,
                    text,
                    character_name=character.get("name") or character.get("slug"),
                    ref_wavs=ref_wavs or None,
                    # Old adapters accept ``ref_wav``; v4 adapters consume
                    # ``ref_wavs``.  Passing both keeps the transition smooth.
                    ref_wav=ref_wavs[0] if ref_wavs else None,
                    caption=caption,
                    no_ref=not bool(ref_wavs),
                    irodori_model=request.irodori_model,
                )
            elif manager is not None and callable(getattr(manager, "synthesize", None)):
                audio = await _call_nonblocking(
                    manager.synthesize,
                    text,
                    character_name=character.get("name") or character.get("slug"),
                    ref_wavs=ref_wavs or None,
                    ref_wav=ref_wavs[0] if ref_wavs else None,
                    caption=caption,
                    no_ref=not bool(ref_wavs),
                    irodori_model=request.irodori_model,
                )
            else:
                raise HTTPException(status_code=503, detail="Irodori-TTSエンジンが利用できません")
            if not audio:
                raise HTTPException(status_code=503, detail="Irodori-TTS試聴の生成に失敗しました")
            return Response(content=bytes(audio), media_type="audio/wav")
        except HTTPException:
            raise
        except (CharacterVoiceAssetError, CharacterNotFoundError) as exc:
            raise _http_error(exc) from exc
        except Exception as exc:
            logger.exception("Irodori試聴生成エラー")
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Starlette checks routes in declaration order.  Put fixed suffixes (order,
    # devices, preview, capture/start) before ``/{asset_id}`` so a GET for
    # ``devices`` is not interpreted as an asset id.
    router.routes.sort(key=lambda route: route.path.count("{"))
    return router


__all__ = ["build_character_voice_router"]
