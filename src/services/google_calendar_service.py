"""Google Calendar OAuth and event creation service."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import GoogleCalendarConnection, Task
from ..security.field_crypto import decrypt_text_if_needed, encrypt_text
from ..task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone


class GoogleCalendarServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class GoogleCalendarSettings:
    configured: bool
    connected: bool
    email: Optional[str]
    calendar_id: str
    default_action: str
    default_event_reminder_minutes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "connected": self.connected,
            "email": self.email,
            "calendar_id": self.calendar_id,
            "default_action": self.default_action,
            "default_event_reminder_minutes": self.default_event_reminder_minutes,
        }


@dataclass
class GoogleCalendarCallbackResult:
    platform: str
    success: bool
    email: Optional[str] = None
    error: Optional[str] = None
    mobile_redirect_uri: Optional[str] = None


class GoogleCalendarService:
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
    SCOPE = "https://www.googleapis.com/auth/calendar.events openid email"
    DEFAULT_ACTION = "open_template"
    DEFAULT_EVENT_REMINDER_MINUTES = 10
    DEFAULT_TIME_ZONE = DEFAULT_TASK_TIMEZONE
    STATE_MAX_AGE_SECONDS = 60 * 10
    AUTO_METADATA_KEY = "google_calendar"

    def __init__(self) -> None:
        self.client_id = os.getenv("GOOGLE_CALENDAR_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET", "").strip()
        self.redirect_uri = os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", "").strip()
        self.state_secret = (
            os.getenv("GOOGLE_CALENDAR_STATE_SECRET", "").strip()
            or os.getenv("INTERNAL_API_KEY", "").strip()
            or "aoitalk-google-calendar-state"
        )
        self.serializer = URLSafeTimedSerializer(self.state_secret)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    async def get_connection(
        self, session: AsyncSession, user_id: UUID
    ) -> Optional[GoogleCalendarConnection]:
        result = await session.execute(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def get_settings(
        self, session: AsyncSession, user_id: UUID
    ) -> GoogleCalendarSettings:
        connection = await self.get_connection(session, user_id)
        return GoogleCalendarSettings(
            configured=self.configured,
            connected=self._has_refresh_token(connection),
            email=connection.google_email if connection else None,
            calendar_id=(
                (connection.calendar_id or "primary") if connection else "primary"
            ),
            default_action=(
                connection.default_action or self.DEFAULT_ACTION
                if connection
                else self.DEFAULT_ACTION
            ),
            default_event_reminder_minutes=(
                int(
                    connection.default_event_reminder_minutes
                    if connection
                    and connection.default_event_reminder_minutes is not None
                    else self.DEFAULT_EVENT_REMINDER_MINUTES
                )
            ),
        )

    async def update_settings(
        self,
        session: AsyncSession,
        user_id: UUID,
        *,
        default_action: Optional[str] = None,
        default_event_reminder_minutes: Optional[int] = None,
    ) -> GoogleCalendarSettings:
        if default_action not in {None, "open_template", "create_event"}:
            raise GoogleCalendarServiceError("Invalid default_action", 400)
        if default_event_reminder_minutes is not None:
            try:
                default_event_reminder_minutes = int(default_event_reminder_minutes)
            except (TypeError, ValueError) as exc:
                raise GoogleCalendarServiceError(
                    "Invalid default_event_reminder_minutes", 400
                ) from exc
            if default_event_reminder_minutes < 0:
                raise GoogleCalendarServiceError(
                    "Invalid default_event_reminder_minutes", 400
                )

        connection = await self.get_connection(session, user_id)
        if connection is None:
            connection = GoogleCalendarConnection(
                user_id=user_id,
                calendar_id="primary",
                default_action=default_action or self.DEFAULT_ACTION,
                default_event_reminder_minutes=(
                    default_event_reminder_minutes
                    if default_event_reminder_minutes is not None
                    else self.DEFAULT_EVENT_REMINDER_MINUTES
                ),
            )
            session.add(connection)
        else:
            if default_action is not None:
                connection.default_action = default_action
            if default_event_reminder_minutes is not None:
                connection.default_event_reminder_minutes = (
                    default_event_reminder_minutes
                )
            if default_action is not None or default_event_reminder_minutes is not None:
                connection.updated_at = datetime.utcnow()

        await session.commit()
        await session.refresh(connection)
        return await self.get_settings(session, user_id)

    async def build_authorization_url(
        self,
        *,
        user_id: UUID,
        username: str,
        platform: str,
        mobile_redirect_uri: Optional[str] = None,
    ) -> str:
        if not self.configured:
            raise GoogleCalendarServiceError(
                "Google Calendar integration is not configured", 503
            )
        if platform not in {"web", "mobile"}:
            raise GoogleCalendarServiceError("Invalid platform", 400)

        state = self.serializer.dumps(
            {
                "user_id": str(user_id),
                "username": username,
                "platform": platform,
                "mobile_redirect_uri": mobile_redirect_uri,
            }
        )
        params = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": self.SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
            }
        )
        return f"{self.AUTH_URL}?{params}"

    async def handle_callback(
        self,
        session: AsyncSession,
        *,
        code: Optional[str],
        state: Optional[str],
        error: Optional[str],
    ) -> GoogleCalendarCallbackResult:
        payload = self._load_state(state)
        platform = str(payload.get("platform") or "web")
        mobile_redirect_uri = payload.get("mobile_redirect_uri")
        if error:
            return GoogleCalendarCallbackResult(
                platform=platform,
                success=False,
                error=error,
                mobile_redirect_uri=mobile_redirect_uri,
            )
        if not code:
            return GoogleCalendarCallbackResult(
                platform=platform,
                success=False,
                error="missing_code",
                mobile_redirect_uri=mobile_redirect_uri,
            )
        if not self.configured:
            return GoogleCalendarCallbackResult(
                platform=platform,
                success=False,
                error="integration_not_configured",
                mobile_redirect_uri=mobile_redirect_uri,
            )

        tokens = await self._exchange_code(code)
        email = await self._fetch_user_email(tokens["access_token"])
        user_id = UUID(str(payload["user_id"]))
        connection = await self.get_connection(session, user_id)
        if connection is None:
            connection = GoogleCalendarConnection(
                user_id=user_id,
                calendar_id="primary",
                default_action=(
                    "create_event" if platform == "mobile" else self.DEFAULT_ACTION
                ),
                default_event_reminder_minutes=self.DEFAULT_EVENT_REMINDER_MINUTES,
            )
            session.add(connection)

        connection.google_email = email
        self._set_token(connection, "access_token", tokens["access_token"])
        refresh_token = tokens.get("refresh_token") or self._get_token(
            connection,
            "refresh_token",
        )
        self._set_token(connection, "refresh_token", refresh_token)
        connection.token_type = tokens.get("token_type")
        connection.scope = tokens.get("scope")
        connection.expires_at = self._expires_at_from_tokens(tokens)
        connection.updated_at = datetime.utcnow()

        await session.commit()

        return GoogleCalendarCallbackResult(
            platform=platform,
            success=True,
            email=email,
            mobile_redirect_uri=mobile_redirect_uri,
        )

    async def disconnect(self, session: AsyncSession, user_id: UUID) -> None:
        connection = await self.get_connection(session, user_id)
        if connection is None:
            return
        connection.google_email = None
        connection.access_token = None
        connection.refresh_token = None
        connection.token_type = None
        connection.scope = None
        connection.expires_at = None
        connection.updated_at = datetime.utcnow()
        await session.commit()

    async def create_event_for_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.configured:
            raise GoogleCalendarServiceError(
                "Google Calendar integration is not configured", 503
            )
        connection = await self.get_connection(session, user_id)
        if not self._has_refresh_token(connection):
            raise GoogleCalendarServiceError(
                "Google Calendar is not connected for this user", 400
            )

        access_token = await self._ensure_access_token(session, connection)
        body = self._build_event_payload(
            task,
            default_event_reminder_minutes=connection.default_event_reminder_minutes,
        )

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                self.EVENTS_URL.format(calendar_id=connection.calendar_id or "primary"),
                params={"sendUpdates": "none"},
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )

            if response.status_code == 401:
                access_token = await self._refresh_tokens(session, connection)
                response = await client.post(
                    self.EVENTS_URL.format(
                        calendar_id=connection.calendar_id or "primary"
                    ),
                    params={"sendUpdates": "none"},
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=body,
                )

        if response.status_code >= 400:
            detail = response.text
            raise GoogleCalendarServiceError(
                f"Google Calendar event creation failed: {detail}", 502
            )

        event = response.json()
        return {
            "event_id": event.get("id"),
            "html_link": event.get("htmlLink"),
            "calendar_id": connection.calendar_id or "primary",
        }

    async def auto_sync_event_for_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(task.get("metadata") or task.get("task_metadata") or {})
        google_metadata = self._get_google_calendar_metadata(metadata)
        existing_event_id = google_metadata.get("auto_event_id")

        if not self.configured:
            return {"status": "skipped", "reason": "not_configured"}

        connection = await self.get_connection(session, user_id)
        if (
            not connection
            or not self._has_refresh_token(connection)
            or connection.default_action != "create_event"
        ):
            return {"status": "skipped", "reason": "not_enabled"}

        disabled_reason = None
        if str(task.get("status") or "").lower() == "closed":
            disabled_reason = "closed"
        elif task.get("notifications_enabled") is False:
            disabled_reason = "notifications_disabled"
        if disabled_reason:
            if existing_event_id:
                await self._delete_event(session, connection, str(existing_event_id))
                metadata.pop(self.AUTO_METADATA_KEY, None)
                saved_metadata = await self._save_task_metadata(
                    session, task_id=UUID(str(task["id"])), metadata=metadata
                )
                return {
                    "status": "deleted",
                    "reason": disabled_reason,
                    "metadata": saved_metadata,
                }
            return {"status": "skipped", "reason": disabled_reason}

        if not self._task_has_timed_start(task):
            if existing_event_id:
                await self._delete_event(session, connection, str(existing_event_id))
                metadata.pop(self.AUTO_METADATA_KEY, None)
                saved_metadata = await self._save_task_metadata(
                    session, task_id=UUID(str(task["id"])), metadata=metadata
                )
                return {
                    "status": "deleted",
                    "reason": "no_timed_start",
                    "metadata": saved_metadata,
                }
            return {"status": "skipped", "reason": "no_timed_start"}

        body = self._build_event_payload(
            task,
            default_event_reminder_minutes=connection.default_event_reminder_minutes,
        )
        access_token = await self._ensure_access_token(session, connection)
        calendar_id = connection.calendar_id or "primary"
        event: dict[str, Any]
        status = "updated" if existing_event_id else "created"

        async with httpx.AsyncClient(timeout=20.0) as client:
            if existing_event_id:
                response = await client.patch(
                    f"{self.EVENTS_URL.format(calendar_id=calendar_id)}/{existing_event_id}",
                    params={"sendUpdates": "none"},
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=body,
                )
                if response.status_code == 401:
                    access_token = await self._refresh_tokens(session, connection)
                    response = await client.patch(
                        f"{self.EVENTS_URL.format(calendar_id=calendar_id)}/{existing_event_id}",
                        params={"sendUpdates": "none"},
                        headers={"Authorization": f"Bearer {access_token}"},
                        json=body,
                    )
                if response.status_code == 404:
                    existing_event_id = None
                    status = "created"

            if not existing_event_id:
                response = await client.post(
                    self.EVENTS_URL.format(calendar_id=calendar_id),
                    params={"sendUpdates": "none"},
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=body,
                )
                if response.status_code == 401:
                    access_token = await self._refresh_tokens(session, connection)
                    response = await client.post(
                        self.EVENTS_URL.format(calendar_id=calendar_id),
                        params={"sendUpdates": "none"},
                        headers={"Authorization": f"Bearer {access_token}"},
                        json=body,
                    )

        if response.status_code >= 400:
            raise GoogleCalendarServiceError(
                f"Google Calendar auto sync failed: {response.text}", 502
            )

        event = response.json()
        metadata[self.AUTO_METADATA_KEY] = {
            "sync_mode": "auto",
            "auto_event_id": event.get("id"),
            "calendar_id": calendar_id,
            "html_link": event.get("htmlLink"),
            "synced_at": datetime.utcnow().isoformat(),
            "synced_start_at": task.get("start_at"),
            "synced_end_at": task.get("end_at"),
            "synced_reminder_offsets": task.get("reminder_offsets") or [],
        }
        saved_metadata = await self._save_task_metadata(
            session, task_id=UUID(str(task["id"])), metadata=metadata
        )
        return {
            "status": status,
            "event_id": event.get("id"),
            "html_link": event.get("htmlLink"),
            "calendar_id": calendar_id,
            "metadata": saved_metadata,
        }

    async def delete_auto_event_for_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(task.get("metadata") or task.get("task_metadata") or {})
        google_metadata = self._get_google_calendar_metadata(metadata)
        event_id = google_metadata.get("auto_event_id")
        if not event_id:
            return {"status": "skipped", "reason": "no_auto_event"}
        if not self.configured:
            return {"status": "skipped", "reason": "not_configured"}
        connection = await self.get_connection(session, user_id)
        if not self._has_refresh_token(connection):
            return {"status": "skipped", "reason": "not_connected"}
        await self._delete_event(session, connection, str(event_id))
        return {"status": "deleted", "event_id": str(event_id)}

    def build_mobile_redirect_uri(
        self, base_uri: Optional[str], status: str, message: Optional[str] = None
    ) -> str:
        target = base_uri or "aoitalk://settings/connection"
        parsed = urlparse(target)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["google_calendar"] = status
        if message:
            query["message"] = message
        return urlunparse(parsed._replace(query=urlencode(query)))

    def render_web_callback_html(
        self, *, success: bool, email: Optional[str] = None, error: Optional[str] = None
    ) -> str:
        payload = json.dumps(
            {
                "source": "aoitalk-google-calendar",
                "success": success,
                "email": email,
                "error": error,
            }
        )
        return f"""<!doctype html>
<html>
  <body>
    <script>
      const payload = {payload};
      if (window.opener) {{
        window.opener.postMessage(payload, "*");
      }}
      window.close();
      document.body.textContent = payload.success
        ? "Google Calendar connected. You can close this window."
        : "Google Calendar connection failed.";
    </script>
  </body>
</html>"""

    def _load_state(self, state: Optional[str]) -> dict[str, Any]:
        if not state:
            raise GoogleCalendarServiceError("Missing OAuth state", 400)
        try:
            payload = self.serializer.loads(state, max_age=self.STATE_MAX_AGE_SECONDS)
        except SignatureExpired as exc:
            raise GoogleCalendarServiceError("OAuth state expired", 400) from exc
        except BadSignature as exc:
            raise GoogleCalendarServiceError("Invalid OAuth state", 400) from exc
        if "user_id" not in payload:
            raise GoogleCalendarServiceError("Invalid OAuth state payload", 400)
        return payload

    async def _exchange_code(self, code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            raise GoogleCalendarServiceError(
                f"OAuth token exchange failed: {response.text}", 502
            )
        return response.json()

    async def _fetch_user_email(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                self.USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            raise GoogleCalendarServiceError(
                f"Google user info fetch failed: {response.text}", 502
            )
        data = response.json()
        email = data.get("email")
        if not email:
            raise GoogleCalendarServiceError(
                "Google account email was not returned", 502
            )
        return str(email)

    def _expires_at_from_tokens(self, tokens: dict[str, Any]) -> Optional[datetime]:
        expires_in = tokens.get("expires_in")
        if not isinstance(expires_in, int):
            return None
        return datetime.utcnow() + timedelta(seconds=max(expires_in - 60, 0))

    def _token_aad(self, connection: GoogleCalendarConnection, field: str) -> str:
        return f"google_calendar_connections.{field}:{connection.user_id}"

    def _get_token(
        self, connection: Optional[GoogleCalendarConnection], field: str
    ) -> Optional[str]:
        if connection is None:
            return None
        value = getattr(connection, field)
        return decrypt_text_if_needed(value, aad=self._token_aad(connection, field))

    def _set_token(
        self,
        connection: GoogleCalendarConnection,
        field: str,
        value: Optional[str],
    ) -> None:
        setattr(
            connection,
            field,
            encrypt_text(value, aad=self._token_aad(connection, field)),
        )

    def _has_refresh_token(
        self, connection: Optional[GoogleCalendarConnection]
    ) -> bool:
        return bool(self._get_token(connection, "refresh_token"))

    async def _ensure_access_token(
        self, session: AsyncSession, connection: GoogleCalendarConnection
    ) -> str:
        access_token = self._get_token(connection, "access_token")
        if (
            access_token
            and connection.expires_at
            and connection.expires_at > datetime.utcnow()
        ):
            return access_token
        return await self._refresh_tokens(session, connection)

    async def _refresh_tokens(
        self, session: AsyncSession, connection: GoogleCalendarConnection
    ) -> str:
        refresh_token = self._get_token(connection, "refresh_token")
        if not refresh_token:
            raise GoogleCalendarServiceError(
                "Google Calendar refresh token is missing", 400
            )
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            raise GoogleCalendarServiceError(
                f"OAuth token refresh failed: {response.text}", 502
            )
        data = response.json()
        self._set_token(connection, "access_token", data["access_token"])
        connection.token_type = data.get("token_type") or connection.token_type
        connection.scope = data.get("scope") or connection.scope
        connection.expires_at = self._expires_at_from_tokens(data)
        connection.updated_at = datetime.utcnow()
        await session.commit()
        return self._get_token(connection, "access_token") or data["access_token"]

    async def _delete_event(
        self,
        session: AsyncSession,
        connection: GoogleCalendarConnection,
        event_id: str,
    ) -> None:
        access_token = await self._ensure_access_token(session, connection)
        calendar_id = connection.calendar_id or "primary"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.delete(
                f"{self.EVENTS_URL.format(calendar_id=calendar_id)}/{event_id}",
                params={"sendUpdates": "none"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 401:
                access_token = await self._refresh_tokens(session, connection)
                response = await client.delete(
                    f"{self.EVENTS_URL.format(calendar_id=calendar_id)}/{event_id}",
                    params={"sendUpdates": "none"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        if response.status_code not in {204, 404} and response.status_code >= 400:
            raise GoogleCalendarServiceError(
                f"Google Calendar event delete failed: {response.text}", 502
            )

    def _get_google_calendar_metadata(
        self, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        value = metadata.get(self.AUTO_METADATA_KEY)
        return value if isinstance(value, dict) else {}

    async def _save_task_metadata(
        self, session: AsyncSession, *, task_id: UUID, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task is None:
            raise GoogleCalendarServiceError("Task not found", 404)
        task.task_metadata = dict(metadata)
        task.updated_at = datetime.utcnow()
        await session.commit()
        return task.task_metadata or {}

    def _build_event_payload(
        self,
        task: dict[str, Any],
        *,
        default_event_reminder_minutes: Optional[int] = None,
    ) -> dict[str, Any]:
        title = str(task.get("title") or "").strip()
        start_at = task.get("start_at")
        end_at = task.get("end_at")
        all_day = bool(task.get("all_day"))
        if not title:
            raise GoogleCalendarServiceError("Task title is required", 400)
        if not start_at and not end_at:
            raise GoogleCalendarServiceError(
                "Task must have a start or due date to create a calendar event", 400
            )

        reminder_minutes: list[int] = []
        raw_reminders = task.get("reminder_offsets")
        if isinstance(raw_reminders, list):
            for raw_offset in raw_reminders:
                try:
                    minutes = max(0, int(raw_offset))
                except (TypeError, ValueError):
                    continue
                if minutes not in reminder_minutes:
                    reminder_minutes.append(minutes)
        if not reminder_minutes:
            default_minutes = (
                self.DEFAULT_EVENT_REMINDER_MINUTES
                if default_event_reminder_minutes is None
                else max(0, int(default_event_reminder_minutes))
            )
            if default_minutes > 0:
                reminder_minutes.append(default_minutes)

        payload: dict[str, Any] = {
            "summary": title,
            "extendedProperties": {
                "private": {"aoitalkTaskId": str(task.get("id") or "")}
            },
        }

        start_dt = self._parse_task_datetime(start_at) or self._parse_task_datetime(
            end_at
        )
        if start_dt is None:
            raise GoogleCalendarServiceError("Task date is invalid", 400)
        end_dt = self._parse_task_datetime(end_at)
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + timedelta(days=1 if all_day else 1 / 24)

        if all_day:
            payload["start"] = {"date": start_dt.date().isoformat()}
            payload["end"] = {"date": end_dt.date().isoformat()}
        else:
            time_zone = self._resolve_task_time_zone(task)
            payload["start"] = {
                "dateTime": start_dt.isoformat(),
                "timeZone": time_zone,
            }
            payload["end"] = {
                "dateTime": end_dt.isoformat(),
                "timeZone": time_zone,
            }

        if reminder_minutes:
            payload["reminders"] = {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": minutes}
                    for minutes in reminder_minutes
                ],
            }

        return {key: value for key, value in payload.items() if value is not None}

    def _resolve_task_time_zone(self, task: dict[str, Any]) -> str:
        return normalize_task_timezone(
            task.get("timezone") or task.get("recurrence_timezone")
        )

    def _task_has_timed_start(self, task: dict[str, Any]) -> bool:
        if bool(task.get("all_day")):
            return False
        start_dt = self._parse_task_datetime(task.get("start_at"))
        if start_dt is None:
            return False
        return any(
            [start_dt.hour, start_dt.minute, start_dt.second, start_dt.microsecond]
        )

    def _parse_task_datetime(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1]
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
