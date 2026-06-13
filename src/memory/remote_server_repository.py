"""外部AoiTalkサーバー接続プロファイルの永続化を担うリポジトリ。

認証トークンは保存時に既存フィールド暗号化基盤で暗号化し、aad を user_id に
バインドする。リモートから取得したデータ本体は永続化せず、接続テスト結果
（last_status / last_checked_at / last_capabilities）のみキャッシュ的に保持する。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..security.field_crypto import encrypt_text
from .models import RemoteServerProfile


def _normalize_base_url(base_url: str) -> str:
    """末尾スラッシュを除いた base_url を返す。"""
    return base_url.strip().rstrip("/")


class RemoteServerRepository:
    """外部AoiTalkサーバー接続プロファイルの CRUD。"""

    @staticmethod
    def _encrypt_token(user_id: UUID, token: Optional[str]) -> Optional[str]:
        if token is None or token == "":
            return None
        aad = f"remote_server_profiles.auth_token:{user_id}"
        return encrypt_text(token, aad=aad)

    @staticmethod
    async def create_profile(
        session: AsyncSession,
        user_id: UUID,
        name: str,
        base_url: str,
        auth_token: Optional[str] = None,
        display_color: Optional[str] = None,
        enabled: bool = True,
    ) -> RemoteServerProfile:
        """接続プロファイルを新規作成する。"""
        record = RemoteServerProfile(
            user_id=user_id,
            name=name,
            base_url=_normalize_base_url(base_url),
            auth_token=RemoteServerRepository._encrypt_token(user_id, auth_token),
            display_color=display_color,
            enabled=enabled,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    @staticmethod
    async def list_profiles(
        session: AsyncSession,
        user_id: UUID,
        enabled_only: bool = False,
    ) -> List[RemoteServerProfile]:
        """ユーザーの接続プロファイル一覧を新しい順で返す。"""
        query = select(RemoteServerProfile).where(
            RemoteServerProfile.user_id == user_id
        )
        if enabled_only:
            query = query.where(RemoteServerProfile.enabled == True)  # noqa: E712
        query = query.order_by(RemoteServerProfile.created_at.desc())
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_profile(
        session: AsyncSession,
        user_id: UUID,
        profile_id: UUID,
    ) -> Optional[RemoteServerProfile]:
        """所有者を確認した上でプロファイルを1件取得する。"""
        query = select(RemoteServerProfile).where(
            RemoteServerProfile.id == profile_id,
            RemoteServerProfile.user_id == user_id,
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def update_profile(
        session: AsyncSession,
        user_id: UUID,
        profile_id: UUID,
        updates: Dict[str, Any],
    ) -> Optional[RemoteServerProfile]:
        """プロファイルを部分更新する。

        ``auth_token`` が含まれる場合のみトークンを更新する。空文字なら未設定
        （None）に戻す。``updates`` に存在しないキーは変更しない。
        """
        record = await RemoteServerRepository.get_profile(
            session, user_id, profile_id
        )
        if record is None:
            return None
        if "name" in updates and updates["name"] is not None:
            record.name = updates["name"]
        if "base_url" in updates and updates["base_url"] is not None:
            record.base_url = _normalize_base_url(updates["base_url"])
        if "display_color" in updates:
            record.display_color = updates["display_color"]
        if "enabled" in updates and updates["enabled"] is not None:
            record.enabled = updates["enabled"]
        if "auth_token" in updates:
            record.auth_token = RemoteServerRepository._encrypt_token(
                user_id, updates["auth_token"]
            )
        await session.commit()
        await session.refresh(record)
        return record

    @staticmethod
    async def delete_profile(
        session: AsyncSession,
        user_id: UUID,
        profile_id: UUID,
    ) -> bool:
        """プロファイルを削除する。存在しなければ False。"""
        record = await RemoteServerRepository.get_profile(
            session, user_id, profile_id
        )
        if record is None:
            return False
        await session.delete(record)
        await session.commit()
        return True

    @staticmethod
    async def record_check_result(
        session: AsyncSession,
        user_id: UUID,
        profile_id: UUID,
        status: str,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> Optional[RemoteServerProfile]:
        """接続テスト結果を保存する。"""
        record = await RemoteServerRepository.get_profile(
            session, user_id, profile_id
        )
        if record is None:
            return None
        record.last_status = status
        record.last_checked_at = datetime.utcnow()
        if capabilities is not None:
            record.last_capabilities = capabilities
        await session.commit()
        await session.refresh(record)
        return record
