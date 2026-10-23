"""Sync v3 の有効判定（読み取り専用）。"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .schema_readiness import inspect_readiness, read_sync_v3_flag

SYNC_V3_ENABLED_KEY = "sync_v3_enabled"


async def _config_flag(session: AsyncSession) -> bool:
    """Read only the global ``sync_v3_enabled`` leaf; absent means disabled."""

    return await session.run_sync(read_sync_v3_flag)


async def sync_v3_enabled(session: AsyncSession) -> bool:
    """Return true only for a complete, key-ready schema with an explicit flag.

    Any catalog/key/provider error is a closed gate.  The v2 sync router has its
    own readiness path and remains available when this function returns false.
    """

    try:
        report = await inspect_readiness(
            session, check_key=True, short_circuit_disabled=True
        )
    except Exception:
        return False
    return report.enabled
