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


async def browser_profile_catalog(session, user_id: str) -> list[dict[str, str]]:
    """Local setup candidates only; the ordinary preview revalidates every pin.

    Reuse the same human/Project ACL, current Persona/account revision and
    provider-binding exclusions as manual Bridge connection, without secrets.
    """
    from sqlalchemy import select
    from ..memory.models import User, PlatformAccount, PlatformAccountRevision, Project
    from .operations_service import OperationsService, OperationsError
    from .media_bridge_connection import MediaBridgeConnectionService
    owner = UUID(user_id)
    user = await session.get(User, owner)
    if user is None or not user.is_active:
        raise PcBridgeError('bridge_owner_unavailable')
    actor = {'id': owner, 'role': user.role, 'actor_type': 'human'}
    operations = OperationsService()
    connection = MediaBridgeConnectionService(operations)
    accounts = (await session.scalars(select(PlatformAccount).where(
        PlatformAccount.owner_user_id == owner, PlatformAccount.platform.in_(['x', 'youtube']),
        PlatformAccount.account_type == 'profile').order_by(PlatformAccount.id).limit(256))).all()
    result = []
    def label(value):
        return ''.join(char for char in str(value or '') if ord(char) >= 32)[:200]
    for account in accounts:
        revision = await session.scalar(select(PlatformAccountRevision).where(
            PlatformAccountRevision.platform_account_id == account.id).order_by(PlatformAccountRevision.version.desc()).limit(1))
        if revision is None or account.persona_id is None:
            continue
        expected = {'owner_user_id': user_id, 'project_id': str(account.project_id) if account.project_id else None,
            'persona_id': str(account.persona_id), 'expected_account_revision_id': str(revision.id),
            'expected_account_revision_hash': revision.content_hash}
        try:
            _, _, _, _, snapshot = await connection._target(session, actor, account.id, expected)
        except OperationsError:
            continue
        project = await session.get(Project, account.project_id) if account.project_id else None
        result.append({'platform_account_id': str(account.id), 'service': account.platform,
            'display_name': label(revision.display_name), 'persona_name': label(snapshot['persona_display_name']),
            'project_name': label(project.name) if project else 'Personal'})
    return result


class PcBridgeStore:
    async def browser_profiles(self, user_id: str) -> list[dict[str, str]]:
        from ..memory.database import get_database_manager
        session = await get_database_manager().get_session()
        try:
            return await browser_profile_catalog(session, user_id)
        finally:
            await session.close()

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

    async def register(self, user_id: str, name: str, machine_id: str | None = None) -> dict[str, Any]:
        if machine_id is not None:
            from pc_bridge.action_protocol import MACHINE_PATTERN
            if not isinstance(machine_id, str) or not MACHINE_PATTERN.fullmatch(machine_id):
                raise PcBridgeError('bridge_machine_id_invalid')
        device = str(uuid4())
        secret = secrets.token_urlsafe(32)
        def change(value):
            devices = value.setdefault('devices', {})
            devices[device] = {
                'id': device, 'name': name,
                'token_hash': hashlib.sha256(secret.encode()).hexdigest(),
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
            if machine_id is not None:
                devices[device]['machine_id'] = machine_id
        await self.update(user_id, change)
        result = {'id': device, 'name': name, 'token': f'{user_id}.{device}.{secret}'}
        if machine_id is not None:
            result['machine_id'] = machine_id
        return result

    async def set_machine_id(self, user_id: str, device_id: str, machine_id: str):
        from pc_bridge.action_protocol import MACHINE_PATTERN
        if not isinstance(machine_id, str) or not MACHINE_PATTERN.fullmatch(machine_id):
            raise PcBridgeError('bridge_machine_id_invalid')
        def change(value):
            device = value.get('devices', {}).get(device_id)
            if device is None:
                raise PcBridgeError('bridge_device_not_found')
            current = device.get('machine_id')
            if current is not None and current != machine_id:
                raise PcBridgeError('bridge_machine_id_mismatch')
            device['machine_id'] = machine_id
        await self.update(user_id, change)

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
