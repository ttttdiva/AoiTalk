"""Skill Recording API Routes

画面録画 + 音声説明から Skill ドラフトを自動生成する機能のエンドポイント。
録画のアップロード / 解析開始 / 状態取得 / ドラフト取得 / 保存 / 削除を提供する。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.skill_recording_service import (
    MAX_UPLOAD_BYTES,
    RecordingNotFoundError,
    STATUS_ANALYZING,
    SkillRecordingError,
    SkillRecordingService,
)

logger = logging.getLogger(__name__)

# チャンク読み込みサイズ（1MB）
_CHUNK_SIZE = 1024 * 1024


class SaveSkillRecordingRequest(BaseModel):
    """録画ドラフトの保存リクエスト。"""

    name: str
    description: str = ""
    markdown: str
    trigger_mode: str = "both"
    target: str  # "global" | "project"
    project_id: Optional[str] = None
    delete_recording: bool = True


def create_skill_recording_router(
    require_auth,
    get_current_user=None,
    config=None,
    service: Optional[SkillRecordingService] = None,
) -> APIRouter:
    """Skill Recording API ルーターを作成する。

    Args:
        require_auth: 認証依存関数。
        get_current_user: リクエストからユーザー情報を得る関数。
        config: アプリ設定（MediaRecognitionService / LLM 用）。
        service: 差し替え用サービス（主にテスト）。省略時は config から生成。
    """
    router = APIRouter(prefix="/api/skill-recordings", tags=["skill-recordings"])
    _service = service or SkillRecordingService(config)

    async def _current_user_id(request: Request) -> Optional[str]:
        """リクエストから現在のユーザー ID を取り出す。"""
        user = None
        if get_current_user is not None:
            user = await get_current_user(request)
        if not user:
            state_user = getattr(request.state, "user", None)
            user = state_user if isinstance(state_user, dict) else None
        user_id = (user or {}).get("user_id") or (user or {}).get("id")
        return str(user_id) if user_id else None

    def _request_session_id(request: Request, user_id: Optional[str]) -> Optional[str]:
        """Return a request-scoped conversation ID when one is available.

        Only middleware state that names the same authenticated principal, or
        a task-local turn context for that principal, is trusted.  Caller-owned
        headers/query parameters are deliberately ignored: accepting an
        arbitrary ID would poison usage aggregation even if it did not grant
        recording access.
        """

        state = getattr(request, "state", None)
        state_user = getattr(state, "user", None)
        state_user_id = None
        if isinstance(state_user, dict):
            state_user_id = state_user.get("user_id") or state_user.get("id")
        trusted_state = bool(
            user_id
            and state_user_id
            and str(state_user_id) == str(user_id)
        )
        if trusted_state:
            for name in ("session_id", "conversation_session_id"):
                value = getattr(state, name, None)
                if value:
                    return str(value).strip() or None

        try:
            from ..services.turn_context import get_turn_context

            turn = get_turn_context()
            if turn is not None and user_id and str(
                getattr(turn, "user_id", "") or ""
            ) == str(user_id):
                value = getattr(turn, "session_id", None)
                if value:
                    return str(value).strip() or None
        except Exception:  # pragma: no cover - import/runtime compatibility
            pass
        return None

    async def authorize_project(request: Request, project_id: Optional[str]) -> None:
        """プロジェクトを扱う場合はメンバーシップを検証する。"""
        if not project_id:
            return
        try:
            UUID(project_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        user_id = await _current_user_id(request)
        if not user_id:
            raise HTTPException(status_code=403, detail="プロジェクトへのアクセス権がありません")
        from ..services.project_context import ProjectContextResolver

        context = await ProjectContextResolver().get_project_context(
            project_id, user_id=user_id
        )
        if context is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")

    async def authorize_recording(request: Request, metadata: dict) -> None:
        """録画の所有者を検証する（所有者不明の旧データは従来どおり許可）。"""
        owner_id = metadata.get("user_id")
        if not owner_id:
            return
        user_id = await _current_user_id(request)
        if user_id != str(owner_id):
            # 存在を秘匿するため 404 を返す。
            raise HTTPException(status_code=404, detail="録画が見つかりません")

    def _run_analysis(recording_id: str) -> None:
        """解析を非同期タスクとして起動する。"""
        try:
            asyncio.create_task(_service.analyze(recording_id))
        except RuntimeError:
            # 実行中のイベントループが無い場合は同期的に実行する。
            asyncio.run(_service.analyze(recording_id))

    @router.post("")
    async def upload_recording(
        request: Request,
        file: UploadFile = File(...),
        project_id: Optional[str] = Form(None),
        title: Optional[str] = Form(None),
        _=Depends(require_auth),
    ):
        """録画（webm）をアップロードして保存する。"""
        await authorize_project(request, project_id)
        recording_id = _service.new_recording_id()
        video_path = _service.video_path(recording_id)
        video_path.parent.mkdir(parents=True, exist_ok=True)

        total = 0
        try:
            with open(video_path, "wb") as out:
                while True:
                    chunk = await file.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="録画サイズが上限（500MB）を超えています",
                        )
                    out.write(chunk)
        except HTTPException:
            _service.delete_recording(recording_id)
            raise
        except Exception as exc:  # noqa: BLE001
            _service.delete_recording(recording_id)
            logger.exception("録画アップロードに失敗")
            raise HTTPException(status_code=500, detail="録画の保存に失敗しました") from exc
        finally:
            await file.close()

        if total == 0:
            _service.delete_recording(recording_id)
            raise HTTPException(status_code=400, detail="空の録画ファイルです")

        current_user_id = await _current_user_id(request)
        _service.create_uploaded(
            recording_id,
            title=title or "",
            project_id=project_id,
            size_bytes=total,
            user_id=current_user_id,
            session_id=_request_session_id(request, current_user_id),
        )
        return JSONResponse(
            content={"id": recording_id, "status": "uploaded"},
            status_code=201,
        )

    @router.post("/{recording_id}/analyze")
    async def analyze_recording(
        recording_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """録画の解析を非同期で開始する（二重起動は冪等）。"""
        try:
            metadata = _service.read_metadata(recording_id)
        except RecordingNotFoundError:
            raise HTTPException(status_code=404, detail="録画が見つかりません")
        await authorize_recording(request, metadata)
        await authorize_project(request, metadata.get("project_id"))

        started = _service.begin_analysis(recording_id)
        if started:
            _run_analysis(recording_id)
        return JSONResponse(content={"id": recording_id, "status": STATUS_ANALYZING})

    @router.get("/{recording_id}")
    async def get_recording(
        recording_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """録画の状態を取得する。"""
        try:
            metadata = _service.read_metadata(recording_id)
        except RecordingNotFoundError:
            raise HTTPException(status_code=404, detail="録画が見つかりません")
        await authorize_recording(request, metadata)
        await authorize_project(request, metadata.get("project_id"))
        return JSONResponse(content=_service.status_view(recording_id))

    @router.get("/{recording_id}/draft")
    async def get_recording_draft(
        recording_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """draft_ready のときドラフトを取得する。"""
        try:
            metadata = _service.read_metadata(recording_id)
        except RecordingNotFoundError:
            raise HTTPException(status_code=404, detail="録画が見つかりません")
        await authorize_recording(request, metadata)
        await authorize_project(request, metadata.get("project_id"))
        draft = _service.get_draft(recording_id)
        if draft is None:
            raise HTTPException(status_code=409, detail="ドラフトはまだ準備できていません")
        return JSONResponse(content=draft)

    @router.post("/{recording_id}/save")
    async def save_recording(
        recording_id: str,
        req: SaveSkillRecordingRequest,
        request: Request,
        _=Depends(require_auth),
    ):
        """ドラフトをグローバル / プロジェクトのスキルとして保存する。"""
        try:
            metadata = _service.read_metadata(recording_id)
        except RecordingNotFoundError:
            raise HTTPException(status_code=404, detail="録画が見つかりません")
        # 録画自身のプロジェクトと、保存先プロジェクトの両方を検証する。
        await authorize_recording(request, metadata)
        await authorize_project(request, metadata.get("project_id"))
        if req.target == "project":
            await authorize_project(request, req.project_id)
        try:
            saved = _service.save_skill(
                recording_id,
                name=req.name,
                description=req.description,
                markdown=req.markdown,
                trigger_mode=req.trigger_mode,
                target=req.target,
                project_id=req.project_id,
                delete_recording=req.delete_recording,
            )
        except SkillRecordingError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse(
            content={"saved": True, "location": saved.get("path", ""), "skill": saved}
        )

    @router.delete("/{recording_id}")
    async def delete_recording(
        recording_id: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """録画と成果物を削除する。"""
        try:
            metadata = _service.read_metadata(recording_id)
        except RecordingNotFoundError:
            raise HTTPException(status_code=404, detail="録画が見つかりません")
        await authorize_recording(request, metadata)
        await authorize_project(request, metadata.get("project_id"))
        _service.delete_recording(recording_id)
        return JSONResponse(content={"success": True})

    return router
