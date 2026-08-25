"""Webex Messaging の本人 OAuth・スペース選択・読み取り専用検索。"""

from __future__ import annotations

import json
import os
import re
import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin, urlparse
from uuid import UUID

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import WebexConnection, WebexSpaceSelection
from ..security.field_crypto import decrypt_text_if_needed, encrypt_text


def _utcnow() -> datetime:
    """Return UTC as a naive datetime to match the existing DB columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


class WebexServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class WebexCallbackResult:
    success: bool
    email: Optional[str] = None
    display_name: Optional[str] = None
    error: Optional[str] = None
    return_origin: Optional[str] = None


class WebexService:
    AUTH_URL = "https://webexapis.com/v1/authorize"
    TOKEN_URL = "https://webexapis.com/v1/access_token"
    API_BASE_URL = "https://webexapis.com/v1/"
    SCOPE = "spark:rooms_read spark:messages_read spark:people_read"
    STATE_MAX_AGE_SECONDS = 60 * 10
    MAX_SELECTED_SPACES = 20
    MAX_SEARCH_DAYS = 90
    MAX_SEARCH_RESULTS = 50
    MAX_MESSAGES_PER_SPACE = 200
    MAX_ROOM_PAGES = 10
    _NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.client_id = os.getenv("WEBEX_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("WEBEX_CLIENT_SECRET", "").strip()
        self.redirect_uri = os.getenv("WEBEX_REDIRECT_URI", "").strip()
        self.state_secret = (
            os.getenv("WEBEX_STATE_SECRET", "").strip()
            or os.getenv("INTERNAL_API_KEY", "").strip()
            or self.client_secret
            or "aoitalk-webex-state"
        )
        self.serializer = URLSafeTimedSerializer(
            self.state_secret,
            salt="aoitalk-webex-oauth-v1",
        )
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=20.0, follow_redirects=False)
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    async def get_connection(
        self,
        session: AsyncSession,
        user_id: UUID,
    ) -> Optional[WebexConnection]:
        result = await session.execute(
            select(WebexConnection).where(WebexConnection.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_settings(
        self,
        session: AsyncSession,
        user_id: UUID,
    ) -> dict[str, Any]:
        connection = await self.get_connection(session, user_id)
        selected = (
            await self._selected_spaces(session, connection)
            if connection is not None
            else []
        )
        return {
            "configured": self.configured,
            "connected": self._has_connection_tokens(connection),
            "callback_origin": (
                self._normalize_return_origin(self.redirect_uri)
                if self.configured
                else None
            ),
            "email": connection.webex_email if connection else None,
            "display_name": connection.webex_display_name if connection else None,
            "scope": connection.scope if connection else None,
            "selected_space_count": len(selected),
            "selected_room_ids": [space.room_id for space in selected],
            "max_selected_spaces": self.MAX_SELECTED_SPACES,
        }

    def build_authorization_url(
        self,
        *,
        user_id: UUID,
        username: str,
        return_origin: Optional[str] = None,
    ) -> str:
        if not self.configured:
            raise WebexServiceError(
                "Webex OAuth の環境変数が設定されていません",
                503,
            )
        nonce = secrets.token_urlsafe(18)
        code_verifier = secrets.token_urlsafe(64)
        encrypted_verifier = encrypt_text(
            code_verifier,
            aad=f"webex_oauth_pkce:{nonce}",
        )
        state = self.serializer.dumps(
            {
                "user_id": str(user_id),
                "username": username,
                "return_origin": self._normalize_return_origin(return_origin),
                "pkce_nonce": nonce,
                "pkce_verifier": encrypted_verifier,
            }
        )
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        query = urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": self.SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.AUTH_URL}?{query}"

    async def handle_callback(
        self,
        session: AsyncSession,
        *,
        code: Optional[str],
        state: Optional[str],
        error: Optional[str],
        expected_user_id: UUID,
    ) -> WebexCallbackResult:
        payload = self._load_state(state)
        state_user_id = UUID(str(payload["user_id"]))
        if state_user_id != expected_user_id:
            raise WebexServiceError(
                "OAuthを開始したAoiTalkユーザーと現在のセッションが一致しません",
                403,
            )
        return_origin = self._normalize_return_origin(payload.get("return_origin"))
        if error:
            return WebexCallbackResult(
                success=False,
                error=str(error),
                return_origin=return_origin,
            )
        if not code:
            return WebexCallbackResult(
                success=False,
                error="認可コードがありません",
                return_origin=return_origin,
            )
        if not self.configured:
            return WebexCallbackResult(
                success=False,
                error="Webex OAuth が設定されていません",
                return_origin=return_origin,
            )

        code_verifier = self._load_code_verifier(payload)
        tokens = await self._exchange_code(code, code_verifier=code_verifier)
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise WebexServiceError(
                "Webex OAuth 応答にアクセストークンがありません",
                502,
            )
        person = await self._fetch_current_person(access_token)
        connection = await self.get_connection(session, state_user_id)
        if connection is None:
            connection = WebexConnection(user_id=state_user_id)
            session.add(connection)

        next_person_id = str(person.get("id") or "") or None
        if (
            connection.id is not None
            and connection.webex_person_id
            and next_person_id
            and connection.webex_person_id != next_person_id
        ):
            await session.execute(
                delete(WebexSpaceSelection).where(
                    WebexSpaceSelection.connection_id == connection.id
                )
            )

        emails = person.get("emails")
        email = (
            str(emails[0])
            if isinstance(emails, list) and emails
            else str(person.get("email") or "")
        )
        display_name = str(person.get("displayName") or "")
        connection.webex_person_id = next_person_id
        connection.webex_org_id = str(person.get("orgId") or "") or None
        connection.webex_email = email or None
        connection.webex_display_name = display_name or None
        self._set_token(connection, "access_token", access_token)
        refresh_token = str(tokens.get("refresh_token") or "") or self._get_token(
            connection,
            "refresh_token",
        )
        self._set_token(connection, "refresh_token", refresh_token)
        connection.token_type = str(tokens.get("token_type") or "Bearer")
        connection.scope = str(tokens.get("scope") or self.SCOPE)
        connection.expires_at = self._expires_at_from_tokens(tokens)
        connection.updated_at = _utcnow()
        await session.commit()

        return WebexCallbackResult(
            success=True,
            email=connection.webex_email,
            display_name=connection.webex_display_name,
            return_origin=return_origin,
        )

    async def disconnect(self, session: AsyncSession, user_id: UUID) -> None:
        connection = await self.get_connection(session, user_id)
        if connection is None:
            return
        await session.delete(connection)
        await session.commit()

    async def list_spaces(
        self,
        session: AsyncSession,
        user_id: UUID,
        *,
        selected_only: bool = False,
    ) -> list[dict[str, Any]]:
        connection = await self._require_connection(session, user_id)
        selected = await self._selected_spaces(session, connection)
        selected_ids = {space.room_id for space in selected}
        if selected_only:
            return [
                {
                    "id": space.room_id,
                    "title": space.title,
                    "type": space.room_type,
                    "selected": True,
                }
                for space in selected
            ]

        rooms: list[dict[str, Any]] = []
        url: Optional[str] = "rooms"
        params: Optional[dict[str, Any]] = {
            "max": 100,
            "sortBy": "lastactivity",
        }
        page_count = 0
        while url and page_count < self.MAX_ROOM_PAGES:
            data, next_url = await self._api_get_page(
                session,
                connection,
                url,
                params=params,
            )
            page_count += 1
            params = None
            items = data.get("items")
            if isinstance(items, list):
                for room in items:
                    if not isinstance(room, dict) or not room.get("id"):
                        continue
                    room_id = str(room["id"])
                    rooms.append(
                        {
                            "id": room_id,
                            "title": str(room.get("title") or "名称なし"),
                            "type": str(room.get("type") or ""),
                            "last_activity": room.get("lastActivity"),
                            "created": room.get("created"),
                            "selected": room_id in selected_ids,
                        }
                    )
            url = next_url
        return rooms

    async def save_selected_spaces(
        self,
        session: AsyncSession,
        user_id: UUID,
        room_ids: list[str],
    ) -> list[dict[str, Any]]:
        connection = await self._require_connection(session, user_id)
        normalized_ids = list(
            dict.fromkeys(str(room_id).strip() for room_id in room_ids if room_id)
        )
        if len(normalized_ids) > self.MAX_SELECTED_SPACES:
            raise WebexServiceError(
                f"選択できるWebexスペースは最大{self.MAX_SELECTED_SPACES}件です",
                400,
            )
        rooms = await self.list_spaces(session, user_id)
        room_map = {str(room["id"]): room for room in rooms}
        unknown = [room_id for room_id in normalized_ids if room_id not in room_map]
        if unknown:
            raise WebexServiceError(
                "参加していないWebexスペースが含まれています",
                403,
            )

        await session.execute(
            delete(WebexSpaceSelection).where(
                WebexSpaceSelection.connection_id == connection.id
            )
        )
        for room_id in normalized_ids:
            room = room_map[room_id]
            session.add(
                WebexSpaceSelection(
                    connection_id=connection.id,
                    room_id=room_id,
                    title=str(room.get("title") or "名称なし"),
                    room_type=str(room.get("type") or "") or None,
                )
            )
        await session.commit()
        return await self.list_spaces(session, user_id, selected_only=True)

    async def search_messages(
        self,
        session: AsyncSession,
        user_id: UUID,
        *,
        query: str,
        room_ids: Optional[list[str]] = None,
        days: int = 30,
        max_results: int = 20,
    ) -> dict[str, Any]:
        connection = await self._require_connection(session, user_id)
        selected = await self._selected_spaces(session, connection)
        selected_map = {space.room_id: space for space in selected}
        if not selected_map:
            raise WebexServiceError(
                "検索対象のWebexスペースが選択されていません",
                400,
            )

        requested_ids = list(
            dict.fromkeys(str(room_id).strip() for room_id in (room_ids or []) if room_id)
        )
        if requested_ids:
            denied = [room_id for room_id in requested_ids if room_id not in selected_map]
            if denied:
                raise WebexServiceError(
                    "許可されていないWebexスペースは検索できません",
                    403,
                )
            targets = [selected_map[room_id] for room_id in requested_ids]
        else:
            targets = list(selected_map.values())

        days = max(1, min(int(days), self.MAX_SEARCH_DAYS))
        max_results = max(1, min(int(max_results), self.MAX_SEARCH_RESULTS))
        since = datetime.now(timezone.utc) - timedelta(days=days)
        matches: list[dict[str, Any]] = []
        scanned_messages = 0
        skipped_spaces: list[str] = []

        for space in targets:
            try:
                messages = await self._list_messages_for_room(
                    session,
                    connection,
                    room_id=space.room_id,
                    since=since,
                    max_messages=self.MAX_MESSAGES_PER_SPACE,
                )
            except WebexServiceError as exc:
                if exc.status_code not in {403, 404}:
                    raise
                skipped_spaces.append(space.room_id)
                continue
            scanned_messages += len(messages)
            for message in messages:
                text = self._message_text(message)
                if not text or not self._message_matches(text, query):
                    continue
                matches.append(
                    self._serialize_message(
                        message,
                        room_id=space.room_id,
                        room_title=space.title,
                    )
                )

        matches.sort(
            key=lambda item: self._parse_datetime(item.get("created"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return {
            "query": query,
            "days": days,
            "scanned_space_count": len(targets),
            "scanned_message_count": scanned_messages,
            "skipped_room_ids": skipped_spaces,
            "messages": matches[:max_results],
        }

    async def get_thread(
        self,
        session: AsyncSession,
        user_id: UUID,
        *,
        room_id: str,
        parent_id: str,
        max_messages: int = 50,
    ) -> dict[str, Any]:
        connection = await self._require_connection(session, user_id)
        selected_map = {
            space.room_id: space
            for space in await self._selected_spaces(session, connection)
        }
        if room_id not in selected_map:
            raise WebexServiceError(
                "許可されていないWebexスペースは参照できません",
                403,
            )
        max_messages = max(1, min(int(max_messages), self.MAX_SEARCH_RESULTS))
        since = datetime.now(timezone.utc) - timedelta(days=self.MAX_SEARCH_DAYS)
        data, _ = await self._api_get_page(
            session,
            connection,
            "messages",
            params={
                "roomId": room_id,
                "parentId": parent_id,
                "max": max_messages,
            },
        )
        items = data.get("items")
        messages = [
            self._serialize_message(
                item,
                room_id=room_id,
                room_title=selected_map[room_id].title,
            )
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
            and (
                (created_at := self._parse_datetime(item.get("created")))
                is not None
                and created_at >= since
            )
        ]
        messages.sort(
            key=lambda item: self._parse_datetime(item.get("created"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return {
            "room_id": room_id,
            "room_title": selected_map[room_id].title,
            "parent_id": parent_id,
            "messages": messages,
        }

    def render_web_callback_html(
        self,
        *,
        success: bool,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        error: Optional[str] = None,
        target_origin: Optional[str] = None,
    ) -> str:
        payload = json.dumps(
            {
                "source": "aoitalk-webex",
                "success": success,
                "email": email,
                "displayName": display_name,
                "error": error,
            },
            ensure_ascii=False,
        )
        payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
        safe_target_origin = self._normalize_return_origin(target_origin)
        message = (
            "Webex connected. You can close this window."
            if success
            else "Webex connection failed."
        )
        return f"""<!doctype html>
<html lang="ja">
  <head><meta charset="utf-8"><title>Webex OAuth</title></head>
  <body>
    <script>
      const payload = {payload};
      const targetOrigin = {json.dumps(safe_target_origin)};
      if (window.opener && targetOrigin) {{
        window.opener.postMessage(payload, targetOrigin);
      }}
      window.close();
      document.body.textContent = {json.dumps(message)};
    </script>
  </body>
</html>"""

    async def _selected_spaces(
        self,
        session: AsyncSession,
        connection: Optional[WebexConnection],
    ) -> list[WebexSpaceSelection]:
        if connection is None or connection.id is None:
            return []
        result = await session.execute(
            select(WebexSpaceSelection)
            .where(WebexSpaceSelection.connection_id == connection.id)
            .order_by(WebexSpaceSelection.title.asc())
        )
        return list(result.scalars().all())

    async def _require_connection(
        self,
        session: AsyncSession,
        user_id: UUID,
    ) -> WebexConnection:
        if not self.configured:
            raise WebexServiceError(
                "Webex OAuth の環境変数が設定されていません",
                503,
            )
        connection = await self.get_connection(session, user_id)
        if not self._has_connection_tokens(connection):
            raise WebexServiceError(
                "Webexが接続されていません",
                400,
            )
        return connection

    async def _list_messages_for_room(
        self,
        session: AsyncSession,
        connection: WebexConnection,
        *,
        room_id: str,
        since: datetime,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        url: Optional[str] = "messages"
        params: Optional[dict[str, Any]] = {"roomId": room_id, "max": 100}
        while url and len(messages) < max_messages:
            data, next_url = await self._api_get_page(
                session,
                connection,
                url,
                params=params,
            )
            params = None
            page_items = [
                item
                for item in (data.get("items") or [])
                if isinstance(item, dict)
            ]
            messages.extend(page_items)
            oldest = min(
                (
                    parsed
                    for parsed in (
                        self._parse_datetime(item.get("created"))
                        for item in page_items
                    )
                    if parsed is not None
                ),
                default=None,
            )
            if oldest is not None and oldest < since:
                break
            url = next_url
        return [
            item
            for item in messages[:max_messages]
            if (
                (created_at := self._parse_datetime(item.get("created")))
                is not None
                and created_at >= since
            )
        ]

    async def _api_get_page(
        self,
        session: AsyncSession,
        connection: WebexConnection,
        path_or_url: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], Optional[str]]:
        url = self._normalize_api_url(path_or_url)
        access_token = await self._ensure_access_token(session, connection)
        response = await self._send_api_request(
            url,
            access_token=access_token,
            params=params,
        )
        if response.status_code == 401:
            access_token = await self._refresh_tokens(session, connection)
            response = await self._send_api_request(
                url,
                access_token=access_token,
                params=params,
            )
        if response.status_code >= 400:
            status_code = 429 if response.status_code == 429 else 502
            if response.status_code in {400, 403, 404}:
                status_code = response.status_code
            retry_after = response.headers.get("Retry-After")
            detail = self._response_error_detail(response)
            if retry_after and response.status_code == 429:
                detail = f"{detail}（Retry-After: {retry_after}秒）"
            raise WebexServiceError(f"Webex APIエラー: {detail}", status_code)
        data = response.json()
        if not isinstance(data, dict):
            raise WebexServiceError("Webex APIの応答形式が不正です", 502)
        return data, self._extract_next_link(response.headers.get("Link"))

    async def _send_api_request(
        self,
        url: str,
        *,
        access_token: str,
        params: Optional[dict[str, Any]],
    ) -> httpx.Response:
        async with self._client_factory() as client:
            return await client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )

    async def _exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
    ) -> dict[str, Any]:
        async with self._client_factory() as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            raise WebexServiceError(
                f"Webex OAuthのトークン交換に失敗しました: "
                f"{self._response_error_detail(response)}",
                502,
            )
        data = response.json()
        if not isinstance(data, dict):
            raise WebexServiceError("Webex OAuthの応答形式が不正です", 502)
        return data

    async def _fetch_current_person(self, access_token: str) -> dict[str, Any]:
        async with self._client_factory() as client:
            response = await client.get(
                f"{self.API_BASE_URL}people/me",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
        if response.status_code >= 400:
            raise WebexServiceError(
                f"Webexアカウント情報の取得に失敗しました: "
                f"{self._response_error_detail(response)}",
                502,
            )
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            raise WebexServiceError("Webexアカウント情報が不正です", 502)
        return data

    async def _ensure_access_token(
        self,
        session: AsyncSession,
        connection: WebexConnection,
    ) -> str:
        access_token = self._get_token(connection, "access_token")
        if access_token and (
            connection.expires_at is None
            or connection.expires_at > _utcnow()
        ):
            return access_token
        return await self._refresh_tokens(session, connection)

    async def _refresh_tokens(
        self,
        session: AsyncSession,
        connection: WebexConnection,
    ) -> str:
        refresh_token = self._get_token(connection, "refresh_token")
        if not refresh_token:
            raise WebexServiceError(
                "Webexの再認証が必要です",
                401,
            )
        async with self._client_factory() as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            raise WebexServiceError(
                "Webexトークンを更新できません。再接続してください",
                401,
            )
        data = response.json()
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise WebexServiceError("Webexトークン更新の応答が不正です", 502)
        self._set_token(connection, "access_token", access_token)
        next_refresh_token = str(data.get("refresh_token") or "") or refresh_token
        self._set_token(connection, "refresh_token", next_refresh_token)
        connection.token_type = str(data.get("token_type") or connection.token_type or "Bearer")
        connection.scope = str(data.get("scope") or connection.scope or self.SCOPE)
        connection.expires_at = self._expires_at_from_tokens(data)
        connection.updated_at = _utcnow()
        await session.commit()
        return access_token

    def _load_state(self, state: Optional[str]) -> dict[str, Any]:
        if not state:
            raise WebexServiceError("OAuth stateがありません", 400)
        try:
            payload = self.serializer.loads(
                state,
                max_age=self.STATE_MAX_AGE_SECONDS,
            )
        except SignatureExpired as exc:
            raise WebexServiceError("OAuth stateの有効期限が切れています", 400) from exc
        except BadSignature as exc:
            raise WebexServiceError("OAuth stateが不正です", 400) from exc
        if not isinstance(payload, dict) or "user_id" not in payload:
            raise WebexServiceError("OAuth stateの内容が不正です", 400)
        try:
            UUID(str(payload["user_id"]))
        except (TypeError, ValueError) as exc:
            raise WebexServiceError("OAuth stateのユーザーIDが不正です", 400) from exc
        return payload

    def _load_code_verifier(self, payload: dict[str, Any]) -> str:
        nonce = str(payload.get("pkce_nonce") or "")
        encrypted = payload.get("pkce_verifier")
        if not nonce or not isinstance(encrypted, str):
            raise WebexServiceError("OAuth PKCE stateが不正です", 400)
        verifier = decrypt_text_if_needed(
            encrypted,
            aad=f"webex_oauth_pkce:{nonce}",
        )
        if not verifier:
            raise WebexServiceError("OAuth PKCE verifierがありません", 400)
        return verifier

    @staticmethod
    def _normalize_return_origin(value: Any) -> Optional[str]:
        if not value:
            return None
        try:
            parsed = urlparse(str(value))
            hostname = parsed.hostname
            parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    def _normalize_api_url(self, path_or_url: str) -> str:
        url = urljoin(self.API_BASE_URL, path_or_url)
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise WebexServiceError(
                "Webex APIのページングURLが不正です",
                502,
            ) from exc
        if (
            parsed.scheme != "https"
            or hostname != "webexapis.com"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise WebexServiceError("Webex APIのページングURLが不正です", 502)
        return url

    def _extract_next_link(self, link_header: Optional[str]) -> Optional[str]:
        if not link_header:
            return None
        match = self._NEXT_LINK_RE.search(link_header)
        if not match:
            return None
        return self._normalize_api_url(match.group(1))

    def _token_aad(self, connection: WebexConnection, field: str) -> str:
        return f"webex_connections.{field}:{connection.user_id}"

    def _get_token(
        self,
        connection: Optional[WebexConnection],
        field: str,
    ) -> Optional[str]:
        if connection is None:
            return None
        return decrypt_text_if_needed(
            getattr(connection, field),
            aad=self._token_aad(connection, field),
        )

    def _set_token(
        self,
        connection: WebexConnection,
        field: str,
        value: Optional[str],
    ) -> None:
        setattr(
            connection,
            field,
            encrypt_text(value, aad=self._token_aad(connection, field)),
        )

    def _has_connection_tokens(
        self,
        connection: Optional[WebexConnection],
    ) -> bool:
        return bool(
            self._get_token(connection, "refresh_token")
            or self._get_token(connection, "access_token")
        )

    @staticmethod
    def _expires_at_from_tokens(tokens: dict[str, Any]) -> Optional[datetime]:
        try:
            expires_in = int(tokens.get("expires_in"))
        except (TypeError, ValueError):
            return None
        return _utcnow() + timedelta(seconds=max(expires_in - 60, 0))

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        return str(message.get("text") or message.get("markdown") or "").strip()

    @classmethod
    def _message_matches(cls, text: str, query: str) -> bool:
        normalized_query = " ".join(str(query or "").casefold().split())
        if not normalized_query:
            return True
        haystack = " ".join(text.casefold().split())
        return normalized_query in haystack or all(
            token in haystack for token in normalized_query.split()
        )

    @classmethod
    def _serialize_message(
        cls,
        message: dict[str, Any],
        *,
        room_id: str,
        room_title: str,
    ) -> dict[str, Any]:
        text = cls._message_text(message)
        if len(text) > 2000:
            text = f"{text[:2000]}…"
        return {
            "id": str(message.get("id") or ""),
            "room_id": room_id,
            "room_title": room_title,
            "room_type": message.get("roomType"),
            "parent_id": message.get("parentId"),
            "person_id": message.get("personId"),
            "person_email": message.get("personEmail"),
            "created": message.get("created"),
            "updated": message.get("updated"),
            "text": text,
            "has_files": bool(message.get("files")),
        }

    @staticmethod
    def _response_error_detail(response: httpx.Response) -> str:
        try:
            data = response.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            value = data.get("message") or data.get("error") or data.get("detail")
            if value:
                return str(value)[:500]
        return str(response.text or response.reason_phrase or "unknown error")[:500]
