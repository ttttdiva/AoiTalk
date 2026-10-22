"""Sync v3 の有効判定（読み取り専用）。"""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..app_config_store import GLOBAL_CONFIG_KEY
from ..memory.models import AppConfigSetting
from . import contract

SYNC_V3_ENABLED_KEY = "sync_v3_enabled"


async def _config_flag(session: AsyncSession) -> bool:
    """app_config_settings の global 設定にある ``sync_v3_enabled``。未設定なら true。

    app_config_store と同じく global 行の JSON を読む。この値は秘密leafではないため
    暗号化されず、復号鍵なしで読める。行もtableも無ければ未設定として扱う。
    """
    present = (
        await session.execute(text("SELECT to_regclass('app_config_settings') IS NOT NULL"))
    ).scalar_one()
    if not present:
        return True
    value = (
        await session.execute(
            select(AppConfigSetting.value).where(AppConfigSetting.key == GLOBAL_CONFIG_KEY)
        )
    ).scalar_one_or_none()
    if not isinstance(value, dict) or SYNC_V3_ENABLED_KEY not in value:
        return True
    flag = value[SYNC_V3_ENABLED_KEY]
    if isinstance(flag, str):
        return flag.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(flag)


async def sync_v3_enabled(session: AsyncSession) -> bool:
    tables_present = bool(
        (await session.execute(text(contract.V3_TABLES_PRESENT_SQL))).scalar_one()
    )
    if not tables_present:
        return False
    return await _config_flag(session)
