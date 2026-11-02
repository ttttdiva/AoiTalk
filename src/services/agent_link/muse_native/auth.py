"""Cookie parsing and the Muse auth/check → session → hatch-token chain."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Mapping, Protocol

import httpx

from ..codes import AgentLinkFailure, LinkErrorCode

MUSE_ORIGIN = "https://muse.ai"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_SAFE_VM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CookieJar:
    """Parsed Cookie header with a deliberately redacted representation."""

    __slots__ = ("_pairs",)

    def __init__(self, pairs: Mapping[str, str]) -> None:
        safe: dict[str, str] = {}
        for name, value in pairs.items():
            if not isinstance(name, str) or not name or any(c in name for c in "=;\r\n"):
                raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
            if not isinstance(value, str) or any(c in value for c in ";\r\n"):
                raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
            safe[name] = value
        if not safe:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        self._pairs = safe

    def __repr__(self) -> str:
        return "CookieJar(<redacted>)"

    @property
    def header_value(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self._pairs.items())

    @classmethod
    def parse(cls, source: str | bytes | Mapping[str, object] | list[object]) -> "CookieJar":
        if isinstance(source, bytes):
            try:
                source = source.decode("utf-8")
            except UnicodeDecodeError:
                raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED) from None
        if isinstance(source, Mapping) or isinstance(source, list):
            return cls._from_json_object(source)
        if not isinstance(source, str):
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        stripped = source.strip()
        if not stripped:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return cls._from_json_object(json.loads(stripped))
            except (json.JSONDecodeError, TypeError, ValueError):
                raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED) from None
        if "\t" in stripped or "\n" in stripped:
            pairs: dict[str, str] = {}
            for line in stripped.splitlines():
                if not line:
                    continue
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_") :]
                elif line.lstrip().startswith("#"):
                    continue
                columns = line.split("\t")
                if len(columns) >= 7:
                    name, value = columns[5], columns[6]
                    if name:
                        pairs[name] = value
            if pairs:
                return cls(pairs)
        cookie = SimpleCookie()
        try:
            cookie.load(stripped)
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED) from None
        pairs = {name: morsel.value for name, morsel in cookie.items()}
        if not pairs:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        return cls(pairs)

    @classmethod
    def from_file(cls, path: Path) -> "CookieJar":
        try:
            return cls.parse(path.read_text(encoding="utf-8"))
        except AgentLinkFailure:
            raise
        except (OSError, UnicodeError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED) from None

    @classmethod
    def _from_json_object(
        cls, value: Mapping[str, object] | list[object]
    ) -> "CookieJar":
        if isinstance(value, Mapping):
            for key in ("cookies", "cookie", "Cookie"):
                nested = value.get(key)
                if isinstance(nested, (Mapping, list)):
                    return cls._from_json_object(nested)
            if all(isinstance(key, str) and isinstance(item, (str, int, float)) for key, item in value.items()):
                return cls({str(key): str(item) for key, item in value.items()})
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        pairs: dict[str, str] = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            name, cookie_value = item.get("name"), item.get("value")
            if isinstance(name, str) and isinstance(cookie_value, str):
                pairs[name] = cookie_value
        if not pairs:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        return cls(pairs)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    content: bytes = field(default=b"", repr=False)

    def json(self) -> object:
        try:
            return json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None


class HttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...

    async def close(self) -> None: ...


class HttpxTransport:
    """Default HTTPS transport; TLS fingerprint impersonation is not assumed."""

    def __init__(self, *, timeout: float = 20.0, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None

    def __repr__(self) -> str:
        return "HttpxTransport(<session redacted>)"

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        try:
            response = await self._client.request(
                method, url, headers=dict(headers), content=content, timeout=timeout
            )
            return HttpResponse(
                status_code=response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONNECT_FAILED) from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True)
class AuthSession:
    vm_id: str = field(repr=False)
    access_token: str = field(repr=False)
    hatch_token: str = field(repr=False)

    def __repr__(self) -> str:
        return "AuthSession(<credentials redacted>)"


class AuthClient:
    def __init__(
        self,
        cookie: CookieJar | str | bytes | Mapping[str, object] | list[object],
        *,
        transport: HttpTransport | None = None,
        vm_attempts: int = 3,
        retry_delay_s: float = 0.25,
        sleep=asyncio.sleep,
    ) -> None:
        if not 1 <= vm_attempts <= 5:
            raise ValueError("VM retry limit is invalid")
        if retry_delay_s < 0:
            raise ValueError("VM retry delay is invalid")
        self._cookie = cookie if isinstance(cookie, CookieJar) else CookieJar.parse(cookie)
        self.transport = transport or HttpxTransport()
        self.vm_attempts = vm_attempts
        self.retry_delay_s = retry_delay_s
        self._sleep = sleep

    def __repr__(self) -> str:
        return "AuthClient(<credentials redacted>)"

    def _headers(self, *, post: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Cookie": self._cookie.header_value,
            "Origin": MUSE_ORIGIN,
            "Referer": f"{MUSE_ORIGIN}/",
            "User-Agent": _USER_AGENT,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if post:
            headers["Content-Type"] = "application/json"
        return headers

    async def _request(self, method: str, path: str, *, content: bytes | None = None) -> HttpResponse:
        try:
            return await self.transport.request(
                method,
                MUSE_ORIGIN + path,
                headers=self._headers(post=method.upper() == "POST"),
                content=content,
            )
        except AgentLinkFailure:
            raise
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONNECT_FAILED) from None

    @staticmethod
    def _check_status(response: HttpResponse, *, hatch: bool = False) -> None:
        if response.status_code == 401:
            raise AgentLinkFailure(LinkErrorCode.MUSE_AUTH_EXPIRED)
        if response.status_code == 403:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HTTP_FORBIDDEN)
        if not 200 <= response.status_code < 300:
            code = (
                LinkErrorCode.MUSE_HATCH_TOKEN_FAILED
                if hatch
                else LinkErrorCode.MUSE_CONNECT_FAILED
            )
            raise AgentLinkFailure(code)

    async def authenticate(self) -> AuthSession:
        check = await self._request("POST", "/api/auth/check", content=b"")
        self._check_status(check)
        check_data = check.json()
        if not isinstance(check_data, Mapping):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        access_token = check_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)

        session_data: Mapping[str, object] | None = None
        for attempt in range(self.vm_attempts):
            session = await self._request("GET", "/api/session")
            self._check_status(session)
            decoded = session.json()
            if not isinstance(decoded, Mapping):
                raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
            if decoded.get("status") == "unavailable" or decoded.get("vm_id") is None:
                if attempt + 1 == self.vm_attempts:
                    raise AgentLinkFailure(LinkErrorCode.MUSE_VM_UNAVAILABLE)
                await self._sleep(self.retry_delay_s)
                continue
            session_data = decoded
            break
        if session_data is None:
            raise AgentLinkFailure(LinkErrorCode.MUSE_VM_UNAVAILABLE)
        vm_id = session_data.get("vm_id")
        if not isinstance(vm_id, str) or not _SAFE_VM_ID.fullmatch(vm_id):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)

        body = json.dumps(
            {"vmAddress": f"wss://{vm_id}.metaaivm.com/", "vmName": vm_id},
            separators=(",", ":"),
        ).encode("utf-8")
        headers = self._headers(post=True)
        headers["Authorization"] = f"Bearer {access_token}"
        try:
            token_response = await self.transport.request(
                "POST",
                MUSE_ORIGIN + "/api/hatch/token",
                headers=headers,
                content=body,
            )
        except AgentLinkFailure:
            raise
        except Exception:
            raise AgentLinkFailure(LinkErrorCode.MUSE_HATCH_TOKEN_FAILED) from None
        self._check_status(token_response, hatch=True)
        token_data = token_response.json()
        if not isinstance(token_data, Mapping):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        hatch_token = next(
            (
                token_data.get(key)
                for key in ("auth_token", "hatch_token", "token")
                if isinstance(token_data.get(key), str) and token_data.get(key)
            ),
            None,
        )
        if not isinstance(hatch_token, str):
            raise AgentLinkFailure(LinkErrorCode.MUSE_HATCH_TOKEN_FAILED)
        return AuthSession(vm_id, access_token, hatch_token)

    async def close(self) -> None:
        await self.transport.close()
