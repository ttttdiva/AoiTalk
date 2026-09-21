"""
グループチャット API ルート

複数キャラクターが参加するグループチャットセッションの
作成と応答生成を提供する。
"""

import logging
import random
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update as sa_update

logger = logging.getLogger(__name__)


def _parse_project_id(value: str | None) -> UUID | None:
    """Normalize a project query/body value without raising an existence oracle."""

    raw = str(value or "").strip()
    if not raw or raw.casefold() in {"none", "all"}:
        return None
    try:
        return UUID(raw)
    except (TypeError, ValueError, AttributeError):
        return None


def _looks_like_builtin_masking_token(value: object) -> bool:
    """Detect a literal token for fail-closed partial-worker behavior."""

    if not isinstance(value, str):
        return False
    parts = value.strip().split(None, 1)
    return bool(parts and parts[0].casefold() == "/masking")


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


def create_group_chat_router(
    require_auth,
    get_current_user,
    config=None,
    masking_handler=None,
) -> APIRouter:
    """グループチャットルーターを作成する。

    Args:
        require_auth: 認証依存関数
        get_current_user: リクエストからユーザー情報を取得する関数
        config: アプリケーション設定
        masking_handler: Optional trusted server-owned ``/masking`` handler.
            The main WebChatServer supplies its request-bound helper here so
            this legacy group endpoint cannot route a masking turn through a
            GroupChatManager/provider.  Keeping it optional preserves import
            compatibility for small test/embedding routers.

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
            from ..memory.models import Project, User
            from ..memory.project_repository import ProjectRepository
            from ..services.project_permissions import normalize_project_member_permissions

            current = await _current_user(request)
            project_id = _parse_project_id(
                request.query_params.get("project_id") if request else None
            )
            if project_id is None:
                return JSONResponse({"users": []})
            try:
                actor_id = UUID(str(current.get("id")))
            except (TypeError, ValueError, AttributeError):
                return JSONResponse({"users": []})
            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            try:
                project = await db_session.get(Project, project_id)
                if project is None or project.deleted_at is not None:
                    return JSONResponse({"users": []})
                if not await ProjectRepository.has_permission(
                    db_session,
                    project_id=project_id,
                    user_id=actor_id,
                    permission="read",
                ):
                    return JSONResponse({"users": []})
                user_result = await db_session.execute(
                    select(User)
                    .where(User.is_active.is_(True))
                    .order_by(User.username)
                    .limit(200)
                )
                users = list(user_result.scalars().all())
                allowed_users = []
                for user in users:
                    if user.id == actor_id:
                        continue
                    if user.id == project.owner_id:
                        allowed_users.append(user)
                        continue
                    member = await ProjectRepository.get_member(
                        db_session,
                        project_id,
                        user.id,
                    )
                    permissions = normalize_project_member_permissions(
                        getattr(member, "permissions", None)
                        if member is not None
                        else None
                    )
                    if permissions.get("read") is True:
                        allowed_users.append(user)
            finally:
                await db_session.close()
            return JSONResponse(
                {
                    "users": [
                        user.to_dict(include_sensitive=False)
                        for user in allowed_users
                    ]
                }
            )
        except Exception as e:
            logger.warning("ユーザー候補取得を拒否: %s", e)
            return JSONResponse({"users": []})

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
            try:
                actor_uuid = UUID(user_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise HTTPException(status_code=403, detail="アクセス拒否") from exc

            # Validate the Project scope and every invited user before any
            # conversation row is created.  Projectless sessions may still
            # use characters/agents, but cannot invite arbitrary users.
            normalized_project_uuid = _parse_project_id(payload.project_id)
            normalized_project_id = (
                str(normalized_project_uuid)
                if normalized_project_uuid is not None
                else None
            )
            from ..memory.database import get_database_manager
            from ..memory.models import Project, User
            from ..memory.project_repository import ProjectRepository
            from ..services.project_permissions import normalize_project_member_permissions

            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            repo = ConversationRepository(db_session)
            if normalized_project_uuid is None and any(
                str(invited).strip() and str(invited) != user_id
                for invited in payload.user_ids
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Project scope is required for user invitations",
                )
            project = None
            if normalized_project_uuid is not None:
                project = await db_session.get(Project, normalized_project_uuid)
                if project is None or project.deleted_at is not None:
                    raise HTTPException(status_code=404, detail="アクセス拒否")
                if not await ProjectRepository.has_permission(
                    db_session,
                    project_id=normalized_project_uuid,
                    user_id=actor_uuid,
                    permission="write",
                ):
                    raise HTTPException(status_code=404, detail="アクセス拒否")

                for invited_user_id in dict.fromkeys(payload.user_ids):
                    if not invited_user_id or str(invited_user_id) == user_id:
                        continue
                    try:
                        invited_uuid = UUID(str(invited_user_id))
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise HTTPException(status_code=404, detail="アクセス拒否") from exc
                    invited_user = await db_session.get(User, invited_uuid)
                    if invited_user is None or not bool(getattr(invited_user, "is_active", False)):
                        raise HTTPException(status_code=404, detail="アクセス拒否")
                    if invited_uuid == project.owner_id:
                        continue
                    member = await ProjectRepository.get_member(
                        db_session,
                        normalized_project_uuid,
                        invited_uuid,
                    )
                    permissions = normalize_project_member_permissions(
                        getattr(member, "permissions", None)
                        if member is not None
                        else None
                    )
                    if member is None or permissions.get("read") is not True:
                        raise HTTPException(status_code=404, detail="アクセス拒否")

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

            from ..memory.models import ConversationSession
            # グループチャットフラグとキャラクター一覧を更新
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
        finally:
            if "db_session" in locals() and db_session is not None:
                try:
                    await db_session.close()
                except Exception:
                    pass

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
            try:
                actor_uuid = UUID(user_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise HTTPException(status_code=403, detail="アクセス拒否") from exc

            from ..memory.database import get_database_manager
            from ..memory.project_repository import ProjectRepository

            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            repo = ConversationRepository(db_session)
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(
                    status_code=404, detail="セッションが見つかりません"
                )
            if not await repo.user_has_session_write_access(session_id, user_id):
                raise HTTPException(status_code=403, detail="アクセス拒否")
            if getattr(session, "project_id", None) is not None:
                try:
                    project_uuid = UUID(str(session.project_id))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise HTTPException(status_code=403, detail="アクセス拒否") from exc
                if not await ProjectRepository.has_permission(
                    db_session,
                    project_id=project_uuid,
                    user_id=actor_uuid,
                    permission="write",
                ):
                    raise HTTPException(status_code=403, detail="アクセス拒否")
            if not session.is_group_chat:
                raise HTTPException(
                    status_code=400,
                    detail="このセッションはグループチャットではありません",
                )

            # This endpoint predates the shared WebSocket/REST dispatch path.
            # Keep the same trusted built-in boundary here: a literal
            # ``/masking`` request must never be persisted as an ordinary
            # group turn or sent to GroupChatManager.  Auth/session/project
            # ACL checks above deliberately run first.
            try:
                from ..services.masking_service import parse_masking_command

                masking_command = parse_masking_command(payload.message)
            except Exception:
                masking_command = None
            if masking_command is None and _looks_like_builtin_masking_token(
                payload.message
            ):
                # A literal server-owned command must never degrade into a
                # GroupChatManager/provider turn when an older worker lacks
                # the canonical parser.
                raise HTTPException(
                    status_code=503,
                    detail="Masking operation is not ready",
                )
            if masking_command is not None:
                if not callable(masking_handler):
                    raise HTTPException(
                        status_code=503,
                        detail="Masking operation is not ready",
                    )
                # ``db_session`` is held only for the ACL read above.  Close
                # it before the request-bound helper opens its own persistence
                # unit; this also prevents a response write from observing a
                # stale transaction snapshot on SQLite.
                try:
                    await db_session.close()
                except Exception:
                    pass
                try:
                    result = await masking_handler(
                        {
                            "message": payload.message,
                            "session_id": session_id,
                            "project_id": (
                                str(getattr(session, "project_id", None))
                                if getattr(session, "project_id", None)
                                else None
                            ),
                            "_sender_user_id": user_id,
                            "_sender_display_name": _display_name(user_info),
                            "attachments": [],
                        },
                        masking_command,
                    )
                except PermissionError as exc:
                    logger.warning("Group masking authorization failed")
                    raise HTTPException(status_code=403, detail="アクセス拒否") from exc
                except ValueError as exc:
                    logger.warning("Group masking validation failed")
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid masking request",
                    ) from exc
                except Exception as exc:
                    # Masking errors may include source-path details from the
                    # local transformer.  Never expose those through this
                    # legacy endpoint's generic exception handler.
                    logger.warning("Group masking operation failed")
                    raise HTTPException(
                        status_code=500,
                        detail="Masking operation failed",
                    ) from exc
                safe_result = result if isinstance(result, dict) else {}
                return JSONResponse(
                    {
                        # The helper's success flag is a protocol boolean;
                        # truthy strings from a legacy/fake response must not
                        # turn an unverified projection into a success.
                        "success": safe_result.get("success") is True,
                        "responses": [
                            {
                                # Keep the legacy response envelope usable by
                                # clients that render only ``responses``.
                                # ``message``/``attachments`` come from the
                                # trusted helper and contain the masked
                                # projection only.
                                "content": str(safe_result.get("message") or ""),
                                "character_slug": "masking",
                                "character_name": "Masking / マスキング",
                                "attachments": list(
                                    safe_result.get("attachments") or []
                                ),
                            }
                        ],
                        "masking": safe_result,
                    },
                    status_code=200,
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
                try:
                    from ..services.privacy_masking_projection import (
                        is_privacy_masking_source,
                    )

                    if is_privacy_masking_source(msg):
                        continue
                except Exception:
                    # If the structural marker helper is unavailable, fail
                    # closed for group provider history rather than risk
                    # replaying a raw masking source.
                    continue
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
            manager = GroupChatManager(
                config=config,
                character_slugs=character_slugs,
                # Usage persistence must use the authenticated API principal
                # and the conversation's durable scope, not the manager's
                # legacy ``default_user``/NULL fallback.
                user_id=user_id,
                session_id=str(session.id),
                project_id=(
                    str(getattr(session, "project_id", None))
                    if getattr(session, "project_id", None)
                    else None
                ),
            )
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
        finally:
            if "db_session" in locals() and db_session is not None:
                try:
                    await db_session.close()
                except Exception:
                    pass

    return router
