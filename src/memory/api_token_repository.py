"""長期APIトークンの発行・一覧・失効・検証を担うリポジトリ。

平文トークンは発行時に一度だけ返す。DBにはSHA-256ハッシュのみを保存し、
検証時は提示トークンを同方式でハッシュして突き合わせる。
"""

import hashlib
import secrets
from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .models import LongLivedApiToken

# 発行トークンの接頭辞。表示・識別用で、秘匿値ではない。
TOKEN_PREFIX = "aoitpat_"
# secrets.token_urlsafe に渡すバイト数。
_TOKEN_BYTES = 32
# 表示用に保持する接頭辞の長さ（接頭辞 + 乱数先頭数文字）。
_DISPLAY_PREFIX_LEN = 14


class ApiTokenRepository:
    """長期APIトークンの永続化と検証。"""

    @staticmethod
    def generate_token() -> str:
        """新しい平文トークンを生成する。"""
        return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)

    @staticmethod
    def hash_token(token: str) -> str:
        """平文トークンをSHA-256でハッシュ化する。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    async def create_token(
        session: AsyncSession,
        user_id: UUID,
        name: str,
        expires_at: Optional[datetime] = None,
    ) -> Tuple[LongLivedApiToken, str]:
        """トークンを発行する。

        Returns:
            (保存されたトークンレコード, 平文トークン) のタプル。
            平文はこの戻り値でしか取得できない。
        """
        plaintext = ApiTokenRepository.generate_token()
        record = LongLivedApiToken(
            user_id=user_id,
            name=name,
            token_hash=ApiTokenRepository.hash_token(plaintext),
            token_prefix=plaintext[:_DISPLAY_PREFIX_LEN],
            expires_at=expires_at,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record, plaintext

    @staticmethod
    async def list_tokens(
        session: AsyncSession,
        user_id: UUID,
        include_revoked: bool = False,
    ) -> List[LongLivedApiToken]:
        """ユーザーのトークン一覧を新しい順で返す。"""
        query = select(LongLivedApiToken).where(
            LongLivedApiToken.user_id == user_id
        )
        if not include_revoked:
            query = query.where(LongLivedApiToken.revoked == False)  # noqa: E712
        query = query.order_by(LongLivedApiToken.created_at.desc())
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_token(
        session: AsyncSession,
        user_id: UUID,
        token_id: UUID,
    ) -> Optional[LongLivedApiToken]:
        """所有者を確認した上でトークンを1件取得する。"""
        query = select(LongLivedApiToken).where(
            LongLivedApiToken.id == token_id,
            LongLivedApiToken.user_id == user_id,
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def revoke_token(
        session: AsyncSession,
        user_id: UUID,
        token_id: UUID,
    ) -> bool:
        """トークンを失効させる。失効済みでも冪等にTrueを返す。"""
        record = await ApiTokenRepository.get_token(session, user_id, token_id)
        if record is None:
            return False
        if not record.revoked:
            record.revoked = True
            await session.commit()
        return True

    @staticmethod
    async def verify_token(
        session: AsyncSession,
        token: str,
        touch: bool = True,
    ) -> Optional[LongLivedApiToken]:
        """平文トークンを検証し、有効なら対応レコードを返す。

        失効済み・期限切れは None を返す。touch=True のとき last_used_at を更新する。
        """
        if not token:
            return None
        token_hash = ApiTokenRepository.hash_token(token)
        query = select(LongLivedApiToken).where(
            LongLivedApiToken.token_hash == token_hash
        )
        result = await session.execute(query)
        record = result.scalar_one_or_none()
        if record is None or record.revoked:
            return None
        now = datetime.utcnow()
        if record.expires_at is not None and record.expires_at <= now:
            return None
        if touch:
            record.last_used_at = now
            await session.commit()
        return record

    @staticmethod
    def verify_token_sync(
        session: Session,
        token: str,
        touch: bool = True,
    ) -> Optional[LongLivedApiToken]:
        """verify_token の同期版。同期セッションで動く認証経路から使う。

        FastAPI の同期依存（スレッドプール実行）から呼ぶことを想定。
        """
        if not token:
            return None
        token_hash = ApiTokenRepository.hash_token(token)
        record = session.execute(
            select(LongLivedApiToken).where(
                LongLivedApiToken.token_hash == token_hash
            )
        ).scalar_one_or_none()
        if record is None or record.revoked:
            return None
        now = datetime.utcnow()
        if record.expires_at is not None and record.expires_at <= now:
            return None
        if touch:
            record.last_used_at = now
            session.commit()
        return record
