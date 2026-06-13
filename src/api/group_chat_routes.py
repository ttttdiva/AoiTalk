"""
グループチャット API ルート

複数キャラクターが参加するグループチャットセッションの
作成と応答生成を提供する。
"""

import logging
import random
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── リクエストモデル ──


class CreateGroupSessionRequest(BaseModel):
    """グループセッション作成リクエスト"""

    character_names: List[str]
    user_ids: List[str] = []
    agent_ids: List[str] = []
    project_id: Optional[str] = None


class GroupRespondRequest(BaseModel):
    """グループ応答生成リクエスト"""

    message: str
    strategy: str = "round_robin"


# ── ファクトリ関数 ──


def create_group_chat_router(require_auth, get_current_user, config=None) -> APIRouter:
    """グループチャットルーターを作成する。

    Args:
        require_auth: 認証依存関数
        get_current_user: リクエストからユーザー情報を取得する関数
        config: アプリケーション設定

    Returns:
        APIRouter
    """
    router = APIRouter(prefix="/api/conversations", tags=["group-chat"])

    # リポジトリの利用可否
    try:
        from ..memory.conversation_repository import ConversationRepository

        REPO_AVAILABLE = True
    except ImportError:
        REPO_AVAILABLE = False
        logger.warning("ConversationRepository が利用できません")

    async def _current_user(request: Request) -> dict:
        user_info = get_current_user(request)
        if hasattr(user_info, "__await__"):
            user_info = await user_info
        return user_info or {"id": "default_user", "username": "default_user"}

    def _display_name(user_info: dict) -> str:
        return str(
            user_info.get("display_name")
            or user_info.get("username")
            or user_info.get("id")
            or "default_user"
        )

    # ─── POST /api/conversations/group ─── グループセッション作成 ───

    @router.get("/participants/users")
    async def list_group_user_candidates(
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Return active users that can be invited to a shared chat."""
        try:
            from ..memory.database import get_database_manager
            from ..memory.user_repository import UserRepository

            current = await _current_user(request)
            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            try:
                users, _ = await UserRepository.list_users(
                    db_session,
                    limit=200,
                    include_inactive=False,
                )
            finally:
                await db_session.close()
            return JSONResponse(
                {
                    "users": [
                        user.to_dict(include_sensitive=False)
                        for user in users
                        if str(user.id) != str(current.get("id"))
                    ]
                }
            )
        except Exception as e:
            logger.error("ユーザー候補取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/group")
    async def create_group_session(
        payload: CreateGroupSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """グループチャットセッションを作成する"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="データベースが利用できません")

        total_participants = (
            len(set(payload.character_names))
            + len(set(payload.user_ids))
            + len(set(payload.agent_ids))
            + 1
        )
        if total_participants < 2:
            raise HTTPException(
                status_code=400,
                detail="グループチャットには2名以上の参加者が必要です",
            )

        try:
            user_info = await _current_user(request)
            user_id = str(user_info.get("id") or "default_user")

            repo = ConversationRepository()

            # project_id の正規化
            normalized_project_id = payload.project_id
            if payload.project_id and payload.project_id.lower() in [
                "none",
                "all",
                "",
            ]:
                normalized_project_id = None

            # セッションの作成（character_name は先頭キャラを代表値として使用）
            primary_character = payload.character_names[0] if payload.character_names else "group"
            session = await repo.create_session(
                user_id=user_id,
                character_name=primary_character,
                title="",
                project_id=normalized_project_id,
            )
            await repo.ensure_participant(
                str(session.id),
                "user",
                user_id,
                display_name=_display_name(user_info),
                role="owner",
                status="joined",
            )

            # グループチャットフラグとキャラクター一覧を更新
            from sqlalchemy import update as sa_update
            from ..memory.models import ConversationSession
            from ..memory.database import get_database_manager
            import uuid

            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            try:
                stmt = (
                    sa_update(ConversationSession)
                    .where(ConversationSession.id == session.id)
                    .values(
                        is_group_chat=True,
                        group_character_names=payload.character_names,
                    )
                )
                await db_session.execute(stmt)
                await db_session.commit()
            finally:
                await db_session.close()

            for invited_user_id in dict.fromkeys(payload.user_ids):
                if invited_user_id and invited_user_id != user_id:
                    await repo.ensure_participant(
                        str(session.id),
                        "user",
                        invited_user_id,
                        display_name=invited_user_id,
                        role="member",
                        status="joined",
                    )

            for agent_id in dict.fromkeys(payload.agent_ids):
                if agent_id:
                    await repo.ensure_participant(
                        str(session.id),
                        "agent",
                        agent_id,
                        display_name=agent_id,
                        role="member",
                        status="joined",
                        auto_respond=True,
                    )

            # 各キャラクターの first_message を挿入
            first_messages = []
            try:
                from ..services.character_service import get_character_for_prompt

                for char_slug in payload.character_names:
                    await repo.ensure_participant(
                        str(session.id),
                        "character",
                        char_slug,
                        display_name=char_slug,
                        role="member",
                        status="joined",
                        auto_respond=True,
                    )
                    char_data = await get_character_for_prompt(char_slug)
                    if not char_data:
                        continue

                    first_msg_content = char_data.get("first_message", "")
                    if not first_msg_content and char_data.get("alternate_greetings"):
                        greetings = char_data["alternate_greetings"]
                        if greetings:
                            first_msg_content = random.choice(greetings)

                    if first_msg_content:
                        msg = await repo.add_message(
                            session_id=str(session.id),
                            role="assistant",
                            content=first_msg_content,
                            metadata={"character_name": char_slug},
                            sender_type="character",
                            sender_id=char_slug,
                            sender_display_name=char_data.get("name", char_slug),
                        )
                        first_messages.append(
                            {
                                "character_slug": char_slug,
                                "character_name": char_data.get("name", char_slug),
                                "content": first_msg_content,
                            }
                        )
            except Exception as e:
                logger.warning(f"first_message の取得に失敗: {e}")

            # セッション情報を再取得
            updated_session = await repo.get_session_by_id(str(session.id))

            return JSONResponse(
                {
                    "success": True,
                    "session": (
                        updated_session.to_dict()
                        if updated_session
                        else session.to_dict()
                    ),
                    "first_messages": first_messages,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"グループセッション作成エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ─── POST /api/conversations/{session_id}/group-respond ─── グループ応答生成 ───

    @router.post("/{session_id}/group-respond")
    async def group_respond(
        session_id: str,
        payload: GroupRespondRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """グループチャットの応答を生成する"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="データベースが利用できません")

        try:
            user_info = await _current_user(request)
            user_id = str(user_info.get("id") or "default_user")

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(
                    status_code=404, detail="セッションが見つかりません"
                )
            if not await repo.user_has_session_access(session_id, user_id):
                raise HTTPException(status_code=403, detail="アクセス拒否")
            if not session.is_group_chat:
                raise HTTPException(
                    status_code=400,
                    detail="このセッションはグループチャットではありません",
                )

            # ユーザーメッセージを保存
            await repo.add_message(
                session_id=session_id,
                role="user",
                content=payload.message,
                sender_type="user",
                sender_id=user_id,
                sender_display_name=_display_name(user_info),
            )

            # 既存メッセージを履歴として取得
            messages = await repo.get_session_messages(session_id, limit=50)
            history = []
            for msg in messages:
                meta = msg.message_metadata or {}
                char_name = meta.get("character_name", "")
                if msg.role == "assistant" and char_name:
                    history.append(
                        {
                            "role": "assistant",
                            "content": f"[{char_name}]: {msg.content}",
                        }
                    )
                else:
                    history.append({"role": msg.role, "content": msg.content})

            # GroupChatManager で応答生成
            from ..llm.group_chat_manager import GroupChatManager

            character_slugs = session.group_character_names or []
            manager = GroupChatManager(config=config, character_slugs=character_slugs)
            responses = await manager.generate_responses(
                user_message=payload.message,
                history=history,
                strategy=payload.strategy,
            )

            # 各応答をDBに保存
            for resp in responses:
                await repo.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=resp["content"],
                    metadata={"character_name": resp["character_slug"]},
                    sender_type="character",
                    sender_id=resp["character_slug"],
                    sender_display_name=resp.get("character_name"),
                )

            return JSONResponse(
                {
                    "success": True,
                    "responses": responses,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"グループ応答生成エラー: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
