"""Private per-owner PC registrations in PostgreSQL, separate from global config.

Tokens are shown once and only their SHA-256 digests are persisted. This uses
namespaced app_config_settings rows; the ordinary settings API reads 'global'.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid4
from typing import Any, Callable


class PcBridgeError(RuntimeError):
    pass


def owner_key(user_id: str) -> str:
    return 'pc_bridge:' + str(UUID(str(user_id)))


def token_parts(token: str) -> tuple[str, str, str]:
    try:
        owner, device, secret = token.split('.')
        UUID(owner)
        UUID(device)
        if len(secret) < 32 or len(secret) > 128:
            raise ValueError()
        return owner, device, secret
    except (ValueError, TypeError, AttributeError):
        raise PcBridgeError('bridge_invalid_token') from None


class PcBridgeStore:
    async def read(self, user_id: str) -> dict[str, Any]:
        from sqlalchemy import select
        from ..memory.database import get_database_manager
        from ..memory.models import AppConfigSetting
        session = await get_database_manager().get_session()
        try:
            row = (await session.execute(select(AppConfigSetting).where(
                AppConfigSetting.key == owner_key(user_id)
            ))).scalar_one_or_none()
            return copy.deepcopy(row.value) if row else {}
        finally:
            await session.close()

    async def update(self, user_id: str, change: Callable) -> Any:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert
        from ..memory.database import get_database_manager
        from ..memory.models import AppConfigSetting
        session = await get_database_manager().get_session()
        try:
            key = owner_key(user_id)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.execute(insert(AppConfigSetting).values(
                key=key, value={}, created_at=now, updated_at=now
            ).on_conflict_do_nothing(index_elements=['key']))
            row = (await session.execute(select(AppConfigSetting).where(
                AppConfigSetting.key == key
            ).with_for_update())).scalar_one()
            value = copy.deepcopy(row.value)
            result = change(value)
            row.value = value
            row.updated_at = now
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def register(self, user_id: str, name: str) -> dict[str, Any]:
        device = str(uuid4())
        secret = secrets.token_urlsafe(32)
        def change(value):
            devices = value.setdefault('devices', {})
            devices[device] = {
                'id': device, 'name': name,
                'token_hash': hashlib.sha256(secret.encode()).hexdigest(),
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
        await self.update(user_id, change)
        return {'id': device, 'name': name, 'token': f'{user_id}.{device}.{secret}'}

    async def authenticate(self, token: str) -> tuple[str, dict[str, Any]]:
        owner, device, secret = token_parts(token)
        data = await self.read(owner)
        item = data.get('devices', {}).get(device)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        if not item or not hmac.compare_digest(item.get('token_hash', ''), digest):
            raise PcBridgeError('bridge_invalid_token')
        return owner, {k: v for k, v in item.items() if k != 'token_hash'}

    async def list(self, user_id: str) -> dict[str, Any]:
        value = await self.read(user_id)
        return {
            'selected_device_id': value.get('selected_device_id'),
            'devices': [{k: v for k, v in item.items() if k != 'token_hash'}
                        for item in value.get('devices', {}).values()],
        }

    async def select(self, user_id: str, device_id: str | None, session_id: str | None = None):
        def change(value):
            if device_id and device_id not in value.get('devices', {}):
                raise PcBridgeError('bridge_device_not_found')
            if session_id:
                value.setdefault('sessions', {})[session_id] = device_id
            else:
                value['selected_device_id'] = device_id
        await self.update(user_id, change)

    async def resolve(self, user_id: str, device_id: str | None = None, session_id: str | None = None) -> str:
        data = await self.read(user_id)
        selected = device_id or data.get('sessions', {}).get(session_id, data.get('selected_device_id'))
        if not selected:
            raise PcBridgeError('bridge_select_a_pc_first')
        if selected not in data.get('devices', {}):
            raise PcBridgeError('bridge_device_not_found')
        return selected

    async def revoke(self, user_id: str, device_id: str):
        def change(value):
            if device_id not in value.get('devices', {}):
                raise PcBridgeError('bridge_device_not_found')
            value['devices'].pop(device_id)
            if value.get('selected_device_id') == device_id:
                value['selected_device_id'] = None
            for session, selected in value.get('sessions', {}).items():
                if selected == device_id:
                    value['sessions'][session] = None
        await self.update(user_id, change)
