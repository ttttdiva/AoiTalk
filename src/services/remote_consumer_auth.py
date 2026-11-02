"""Purpose-isolated verification for short-lived handoff consumer tickets."""
from __future__ import annotations

import hashlib
import hmac
import inspect
import ipaddress
import json
import re
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID

import jwt


class RemoteConsumerAuthUnavailable(Exception):
    pass


def _https(value: Any, *, origin: bool) -> bool:
    if not isinstance(value, str) or not value or len(value) > 255:
        return False
    if re.search(r"[\x00-\x20\x7f\\#]", value) or not value.startswith('https://'):
        return False
    try:
        url = urlsplit(value)
        host = url.hostname
        if not host or url.username is not None or url.password is not None or '*' in host or url.query:
            return False
        # WHATWG URL serialization lowercases and IDNA-encodes hosts and removes default ports.
        ascii_host = host.encode('idna').decode('ascii')
        if ':' in ascii_host:
            ascii_host = '[' + ipaddress.IPv6Address(ascii_host).compressed + ']'
        elif re.fullmatch(r'(?:[0-9]+|0[xX][0-9a-fA-F]*)', ascii_host.rstrip('.').split('.')[-1]):
            # WHATWG interprets domains ending in a numeric label as IPv4.
            # Only the canonical dotted-decimal representation survives serialization.
            ascii_host = str(ipaddress.IPv4Address(ascii_host))
        port = url.port
        authority = ascii_host + (f':{port}' if port is not None and port != 443 else '')
        path = url.path
        if any(segment.lower() in ('.', '..', '%2e', '.%2e', '%2e.', '%2e%2e') for segment in path.split('/')):
            return False
        if any(ord(ch) > 127 or ch in ' "<>`{}' for ch in path):
            return False
        canonical = 'https://' + authority + (path if path and path != '/' else '')
        return value == canonical and (not origin or not path)
    except (ValueError, UnicodeError):
        return False


def _callback(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512 or not value.startswith('/'):
        return False
    if '//' in value or re.search(r'[\\%?#\x00-\x1f\x7f]', value):
        return False
    if any(s in ('.', '..') for s in value.split('/')):
        return False
    return not any(ord(c) > 127 or c in ' "<>`{}' for c in value)


def _registry(raw: str) -> list[dict[str, str]]:
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > 100:
            return []
        ids = {'aoitalk-legacy'}
        pairs = {('https://nk-h01.f5.si', '/at/api/auth/handoff/callback')}
        audiences = {'https://nk-h01.f5.si'}
        apps: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'clientId', 'origin', 'callbackPath', 'audience', 'secretSha256', 'consumerApp'}:
                return []
            client_id, origin, callback, audience, digest = (row.get(k) for k in ('clientId', 'origin', 'callbackPath', 'audience', 'secretSha256'))
            app = row.get('consumerApp')
            if (not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', client_id)
                or not _https(origin, origin=True) or not _callback(callback)
                or not isinstance(audience, str) or len(audience) > 255 or not (re.fullmatch(r'urn:[A-Za-z0-9][A-Za-z0-9:._~-]{0,253}', audience) or _https(audience, origin=False))
                or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or digest == '0' * 64
                or ('consumerApp' in row and app not in ('73', '86'))
                or client_id in ids or (origin, callback) in pairs or audience in audiences or (app is not None and app in apps)):
                return []
            ids.add(client_id)
            pairs.add((origin, callback))
            audiences.add(audience)
            if app is not None:
                apps.add(app)
        return rows
    except (ValueError, TypeError):
        return []


class RemoteConsumerAuth:
    def __init__(self, *, get_user: Callable[[UUID], Any], get_secret: Callable[[], str | None],
                 get_registry: Callable[[], str | None], get_issuer: Callable[[], str | None]):
        self.get_user = get_user
        self.get_secret = get_secret
        self.get_registry = get_registry
        self.get_issuer = get_issuer

    async def verify(self, ticket: str, consumer_app: str) -> dict[str, str] | None:
        if not isinstance(ticket, str) or len(ticket) > 4096 or consumer_app not in ('73', '86'):
            return None
        try:
            secret = self.get_secret()
            raw = self.get_registry()
            issuer = self.get_issuer()
            if issuer is None or (isinstance(issuer, str) and not issuer.strip()):
                issuer = 'https://az-vnetspace.com'
            if not isinstance(secret, str) or not secret.strip() or not isinstance(raw, str):
                raise RemoteConsumerAuthUnavailable()
            if not _https(issuer, origin=True):
                return None
            key = hmac.new(secret.strip().encode(), b'aoitalk:remote-consumer:v1', hashlib.sha256).digest()
            claims = jwt.decode(ticket, key, algorithms=['HS256'], audience='41',
                options={'require': ['iss', 'sub', 'aud', 'purpose', 'consumer_app', 'client_id',
                                     'registration_hash', 'session_version', 'iat', 'exp', 'jti']})
            if (claims['iss'] != issuer or claims['purpose'] != 'remote-consumer' or
                claims['aud'] != '41' or claims['consumer_app'] != consumer_app or
                type(claims['iat']) is not int or type(claims['exp']) is not int or
                not 0 < claims['exp'] - claims['iat'] <= 60 or
                type(claims['session_version']) is not int or claims['session_version'] < 1 or
                not isinstance(claims['jti'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', claims['jti'])):
                return None
            owner = UUID(claims['sub'])
            clients = _registry(raw)
            client = next((c for c in clients if c['clientId'] == claims['client_id'] and c.get('consumerApp') == consumer_app), None)
            if client is None:
                return None
            parts = [client[k] for k in ('clientId', 'origin', 'callbackPath', 'audience', 'secretSha256', 'consumerApp')]
            digest = hashlib.sha256('\0'.join(parts).encode()).hexdigest()
            if not isinstance(claims['registration_hash'], str) or not hmac.compare_digest(digest, claims['registration_hash']):
                return None
        except (ValueError, TypeError, KeyError, AttributeError, jwt.PyJWTError):
            return None
        except RemoteConsumerAuthUnavailable:
            raise
        except Exception:
            raise RemoteConsumerAuthUnavailable() from None
        try:
            user = self.get_user(owner)
            if inspect.isawaitable(user):
                user = await user
            if (user is None or not user.is_active or user.is_password_reset_required or
                user.session_version != claims['session_version'] or not isinstance(user.role, str) or not user.role.strip()):
                return None
            return {'id': str(owner), 'role': user.role}
        except Exception:
            raise RemoteConsumerAuthUnavailable() from None
