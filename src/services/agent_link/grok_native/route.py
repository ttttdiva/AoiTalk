"""Safe, in-process HTTP routing for the Grok Bot desktop gateway."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..codes import AgentLinkFailure, LinkErrorCode
from .dpapi import DPAPIUnprotector
from .profile import ProfileReader
from .secrets import GrokCredentials, assert_account_pin, read_credentials

ENSURE_SANDBOX_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox"
LIST_STATUSES_URL = (
    "https://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotAgentStatuses"
)
SAND_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"
LIST_TRANSCRIPT_ENTRIES_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotTranscriptEntries"
WATCH_TRANSCRIPTS_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/WatchGrokBotTranscripts"
_WATCH_STOP = object()
MAX_STATUS_AGENT_IDS = 100
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_S = 30.0
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "connect-protocol-version",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "x-cursor-client-type",
        "x-cursor-client-version",
        "x-cursor-team-id",
        "x-sand-box-namespace",
    }
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9a-z]+$")
_SAFE_REQUEST_ID = re.compile(
    r"^(?:[a-f0-9]{16,128}|[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)$", re.IGNORECASE
)


def _safe_request_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (
        not value
        or len(value) > 128
        or not _SAFE_REQUEST_ID.fullmatch(value)
        or re.search(
            r"(?:authorization|bearer|cookie|credential|key|password|secret|token)",
            value,
            re.I,
        )
    ):
        return None
    return value


class GrokLinkFailure(AgentLinkFailure):
    """An error with only fixed-code text and bounded, safe diagnostics."""

    def __init__(
        self,
        code: object,
        *,
        stage: str = "route",
        http_status: int | None = None,
        request_id: str | None = None,
        request_started: bool = False,
    ) -> None:
        super().__init__(code)
        self.stage = (
            stage
            if stage in {"profile", "dpapi", "ensure", "gateway", "send", "inspect"}
            else "route"
        )
        self.http_status = (
            http_status
            if type(http_status) is int and 100 <= http_status <= 599
            else None
        )
        self.request_id = _safe_request_id(request_id)
        self.request_started = bool(request_started)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(default=b"", repr=False)
    request_id: str | None = None
    redirected: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("HTTP status is invalid")
        if not isinstance(self.body, bytes):
            raise ValueError("HTTP body is invalid")
        object.__setattr__(self, "request_id", _safe_request_id(self.request_id))
        object.__setattr__(self, "redirected", bool(self.redirected))


class HttpTransport(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout_s: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class HttpxTransport:
    """Streaming httpx adapter that refuses redirects and bounds response reads."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False)

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout_s: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        try:
            async with self._client.stream(
                "POST",
                url,
                headers=dict(headers),
                json=dict(json_body),
                timeout=timeout_s,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                request_id = None
                for name in (
                    "x-request-id",
                    "x-correlation-id",
                    "x-cursor-request-id",
                    "x-cursor-correlation-id",
                ):
                    request_id = _safe_request_id(response.headers.get(name))
                    if request_id:
                        break
                redirected = (
                    300 <= status < 400
                    or bool(response.history)
                    or str(response.url) != url
                )
                if not 200 <= status < 300 or redirected:
                    # Error response bodies can contain arbitrary user or credential data.
                    return HttpResponse(
                        status, request_id=request_id, redirected=redirected
                    )
                declared = response.headers.get("content-length")
                if (
                    declared
                    and declared.isdecimal()
                    and int(declared) > max_response_bytes
                ):
                    raise GrokLinkFailure(
                        LinkErrorCode.GROK_CONTRACT_CHANGED,
                        stage="gateway",
                        http_status=status,
                        request_id=request_id,
                        request_started=True,
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_response_bytes:
                        raise GrokLinkFailure(
                            LinkErrorCode.GROK_CONTRACT_CHANGED,
                            stage="gateway",
                            http_status=status,
                            request_id=request_id,
                            request_started=True,
                        )
                    chunks.append(chunk)
                return HttpResponse(
                    status,
                    body=b"".join(chunks),
                    request_id=request_id,
                    redirected=redirected,
                )
        except GrokLinkFailure:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
            raise GrokLinkFailure(
                LinkErrorCode.GROK_GATEWAY_REJECTED,
                stage="gateway",
                request_started=True,
            ) from None
        except Exception:
            raise GrokLinkFailure(
                LinkErrorCode.GROK_GATEWAY_REJECTED,
                stage="gateway",
                request_started=True,
            ) from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def watch_frames(self, url, *, headers, body, timeout_s, max_response_bytes, inspect):
        """Read a bounded Connect stream until explicit evidence is observed."""
        size = 0
        pending = bytearray()
        ended = False
        try:
            async with asyncio.timeout(timeout_s):
                async with self._client.stream(
                    "POST", url, headers=dict(headers), content=body,
                    timeout=timeout_s, follow_redirects=False,
                ) as response:
                    if (response.status_code != 200 or response.history
                            or str(response.url) != url):
                        return None
                    async for chunk in response.aiter_bytes():
                        remaining = max_response_bytes - size
                        pending.extend(chunk[:remaining])
                        size += min(len(chunk), remaining)
                        while len(pending) >= 5:
                            flags = pending[0]
                            length = int.from_bytes(pending[1:5], "big")
                            if flags not in (0, 2) or length > max_response_bytes - 5:
                                return None
                            if len(pending) < 5 + length:
                                break
                            data = bytes(pending[5:5 + length])
                            del pending[:5 + length]
                            if ended:
                                return None
                            try:
                                frame = json.loads(data.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                return None
                            if flags == 2:
                                if not isinstance(frame, dict) or "error" in frame:
                                    return None
                                ended = True
                                continue
                            candidate = inspect(frame)
                            if candidate is _WATCH_STOP:
                                return None
                            if candidate is not None:
                                return candidate
                        if len(chunk) > remaining:
                            return None
                    return None
        except (TimeoutError, httpx.HTTPError, OSError, ValueError):
            return None


@dataclass(frozen=True)
class GatewayRoute:
    url: str = field(repr=False)
    token: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


def validate_gateway_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or any(ord(char) < 0x21 or ord(char) == 0x7F for char in value)
    ):
        raise GrokLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED, stage="ensure")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError):
        raise GrokLinkFailure(
            LinkErrorCode.GROK_GATEWAY_REJECTED, stage="ensure"
        ) from None
    allowed_host = any(
        hostname == suffix.removeprefix(".") or hostname.endswith(suffix)
        for suffix in (".cursor.sh", ".cursor.com", ".cursorvm.com")
    )
    if (
        parsed.scheme.lower() != "https"
        or not allowed_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise GrokLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED, stage="ensure")
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", hostname, path, "", ""))


def _gateway_headers(body: Mapping[str, object]) -> dict[str, str]:
    raw = body.get("gatewayHeaders", body.get("gateway_headers"))
    if raw is not None and not isinstance(raw, Mapping):
        raise GrokLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED, stage="ensure")
    result: dict[str, str] = {}
    normalized_names: set[str] = set()
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            name = key.lower() if isinstance(key, str) else ""
            if (
                not _HEADER_NAME.fullmatch(name)
                or name in _RESERVED_HEADERS
                or name in normalized_names
                or not isinstance(value, str)
                or len(value) > 8192
                or any(char in value for char in "\r\n\0")
            ):
                raise GrokLinkFailure(
                    LinkErrorCode.GROK_GATEWAY_REJECTED, stage="ensure"
                )
            normalized_names.add(name)
            if value:
                result[name] = value
    if result:
        return result
    network_token = body.get(
        "anyrunNetworkToken",
        body.get("anyrun_network_token", body.get("networkToken")),
    )
    if network_token in (None, ""):
        return {}
    if (
        not isinstance(network_token, str)
        or len(network_token) > 8192
        or any(char in network_token for char in "\r\n\0")
    ):
        raise GrokLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED, stage="ensure")
    return {"x-anyrun-network-token": network_token}


def route_from_ensure_response(body: object) -> GatewayRoute:
    if not isinstance(body, Mapping):
        raise GrokLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED, stage="ensure")
    url = validate_gateway_url(body.get("gatewayUrl", body.get("gateway_url")))
    token = body.get("gatewayToken", body.get("gateway_token"))
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192
        or token.strip() != token
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in token)
    ):
        raise GrokLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED, stage="ensure")
    return GatewayRoute(url=url, token=token, headers=_gateway_headers(body))


def _decode_json(response: HttpResponse, *, stage: str) -> object:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GrokLinkFailure(
            LinkErrorCode.GROK_CONTRACT_CHANGED,
            stage=stage,
            http_status=response.status,
            request_id=response.request_id,
            request_started=True,
        ) from None


@dataclass(frozen=True)
class GrokTurnSettlement:
    turn_id: str = field(repr=False)
    outcome: str = field(repr=False)
    settled_at_ms: int


@dataclass(frozen=True)
class GrokLiveStatus:
    last_turn_settlement: GrokTurnSettlement | None


@dataclass(frozen=True)
class GrokAgentStatus:
    agent_id: str = field(repr=False)
    is_running: bool
    live: GrokLiveStatus | None
    raw: Mapping[str, object] = field(repr=False)


def _normal_headers(credentials: GrokCredentials) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "connect-protocol-version": "1",
        "authorization": f"Bearer {credentials.access_token}",
        "x-cursor-client-type": "sand",
        "x-cursor-client-version": credentials.app_version,
        "x-sand-box-namespace": "prod",
    }
    if credentials.team_id is not None:
        headers["x-cursor-team-id"] = credentials.team_id
    return headers


def _valid_agent_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value.strip() == value
        and not any(ord(c) < 33 or ord(c) == 127 for c in value)
    )


def _parse_statuses(body: object, requested: set[str]) -> tuple[GrokAgentStatus, ...]:
    def invalid() -> None:
        raise GrokLinkFailure(
            LinkErrorCode.GROK_CONTRACT_CHANGED, stage="inspect", request_started=True
        )

    if not isinstance(body, dict) or not isinstance(body.get("statuses"), list):
        invalid()
    result = []
    seen: set[str] = set()
    for item in body["statuses"]:
        if not isinstance(item, dict):
            invalid()
        agent_id = item.get("agentId")
        running = item.get("isRunning", False)
        if (
            not _valid_agent_id(agent_id)
            or agent_id not in requested
            or agent_id in seen
            or type(running) is not bool
        ):
            invalid()
        seen.add(agent_id)
        client_data = item.get("client")
        if client_data is not None and (
            not isinstance(client_data, dict)
            or client_data.get("agentId", agent_id) != agent_id
        ):
            invalid()
        live_data = item.get("live")
        live = None
        if live_data is not None:
            if (
                not isinstance(live_data, dict)
                or live_data.get("agentId", agent_id) != agent_id
            ):
                invalid()
            if any(
                name in live_data and type(live_data[name]) is not bool
                for name in (
                    "isRunning",
                    "isComposingMessage",
                    "isRetrying",
                    "hasRunningSubagents",
                )
            ):
                invalid()
            settlement_data = live_data.get("lastTurnSettlement")
            settlement = None
            if settlement_data is not None:
                if not isinstance(settlement_data, dict):
                    invalid()
                turn_id = settlement_data.get("turnId")
                outcome = settlement_data.get("outcome")
                timestamp = settlement_data.get("settledAtMs")
                if (
                    not _valid_agent_id(turn_id)
                    or not isinstance(outcome, str)
                    or not outcome
                    or len(outcome) > 256
                    or isinstance(timestamp, bool)
                    or not isinstance(timestamp, (str, int))
                    or not str(timestamp).isdecimal()
                    or len(str(timestamp)) > 19
                    or int(timestamp) > 9223372036854775807
                ):
                    invalid()
                settlement = GrokTurnSettlement(turn_id, outcome, int(timestamp))
            live = GrokLiveStatus(settlement)
        result.append(GrokAgentStatus(agent_id, running, live, MappingProxyType(item)))
    return tuple(result)


class GrokRouteProvider:
    """Read profile credentials and obtain a validated EnsureSandBox route."""

    def __init__(
        self,
        profile_reader: ProfileReader,
        dpapi_unprotector: DPAPIUnprotector,
        transport: HttpTransport,
        *,
        expected_account_identity: str | None = None,
        expected_team_identity: str | None = None,
        timeout_s: float = REQUEST_TIMEOUT_S,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.profile_reader = profile_reader
        self.dpapi_unprotector = dpapi_unprotector
        self.transport = transport
        self.expected_account_identity = expected_account_identity
        self.expected_team_identity = expected_team_identity
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes
        self.credentials: GrokCredentials | None = None
        self.route: GatewayRoute | None = None
        self.last_failure: GrokLinkFailure | None = None

    async def post_response(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        *,
        stage: str,
    ) -> HttpResponse:
        try:
            response = await self.transport.post(
                url,
                headers=headers,
                json_body=body,
                timeout_s=self.timeout_s,
                max_response_bytes=self.max_response_bytes,
            )
        except GrokLinkFailure:
            raise
        except (TimeoutError, OSError, httpx.TimeoutException, httpx.NetworkError):
            raise GrokLinkFailure(
                LinkErrorCode.GROK_GATEWAY_REJECTED,
                stage=stage,
                request_started=True,
            ) from None
        except Exception:
            raise GrokLinkFailure(
                LinkErrorCode.GROK_GATEWAY_REJECTED,
                stage=stage,
                request_started=True,
            ) from None
        if not isinstance(response, HttpResponse) or type(response.status) is not int:
            raise GrokLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED, stage=stage)
        if len(response.body) > self.max_response_bytes:
            raise GrokLinkFailure(
                LinkErrorCode.GROK_CONTRACT_CHANGED,
                stage=stage,
                http_status=response.status,
                request_id=response.request_id,
                request_started=True,
            )
        if response.redirected or 300 <= response.status < 400:
            raise GrokLinkFailure(
                LinkErrorCode.GROK_GATEWAY_REJECTED,
                stage=stage,
                http_status=response.status,
                request_id=response.request_id,
                request_started=True,
            )
        return response

    async def ensure_route(self, *, force: bool = False) -> GatewayRoute:
        if self.route is not None and self.credentials is not None and not force:
            assert_account_pin(self.profile_reader, self.credentials.pin)
            return self.route
        credentials = read_credentials(
            self.profile_reader,
            self.dpapi_unprotector,
            expected_account_identity=self.expected_account_identity,
            expected_team_identity=self.expected_team_identity,
        )
        assert_account_pin(self.profile_reader, credentials.pin)
        headers = _normal_headers(credentials)
        response = await self.post_response(
            ENSURE_SANDBOX_URL, headers, {}, stage="ensure"
        )
        assert_account_pin(self.profile_reader, credentials.pin)
        if response.status in {401, 403}:
            failure = GrokLinkFailure(
                LinkErrorCode.GROK_AUTH_EXPIRED,
                stage="ensure",
                http_status=response.status,
                request_id=response.request_id,
                request_started=True,
            )
            self.last_failure = failure
            raise failure
        if not 200 <= response.status < 300:
            failure = GrokLinkFailure(
                LinkErrorCode.GROK_GATEWAY_REJECTED,
                stage="ensure",
                http_status=response.status,
                request_id=response.request_id,
                request_started=True,
            )
            self.last_failure = failure
            raise failure
        route = route_from_ensure_response(_decode_json(response, stage="ensure"))
        assert_account_pin(self.profile_reader, credentials.pin)
        self.credentials = credentials
        self.route = route
        self.last_failure = None
        return route

    async def list_agent_statuses(
        self, agent_ids: list[str]
    ) -> tuple[GrokAgentStatus, ...]:
        """Read statuses for explicitly selected agents using current normal credentials."""
        if (
            not isinstance(agent_ids, list)
            or not 1 <= len(agent_ids) <= MAX_STATUS_AGENT_IDS
            or any(not _valid_agent_id(value) for value in agent_ids)
            or len(set(agent_ids)) != len(agent_ids)
        ):
            raise GrokLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED, stage="inspect")
        credentials = self.credentials or read_credentials(
            self.profile_reader,
            self.dpapi_unprotector,
            expected_account_identity=self.expected_account_identity,
            expected_team_identity=self.expected_team_identity,
        )
        assert_account_pin(self.profile_reader, credentials.pin)
        try:
            response = await self.post_response(
                LIST_STATUSES_URL,
                _normal_headers(credentials),
                {"agentIds": list(agent_ids)},
                stage="inspect",
            )
        finally:
            assert_account_pin(self.profile_reader, credentials.pin)
        if response.status in {401, 403}:
            raise GrokLinkFailure(
                LinkErrorCode.GROK_AUTH_EXPIRED,
                stage="inspect",
                http_status=response.status,
                request_id=response.request_id,
                request_started=True,
            )
        if not 200 <= response.status < 300:
            raise GrokLinkFailure(
                LinkErrorCode.GROK_GATEWAY_REJECTED,
                stage="inspect",
                http_status=response.status,
                request_id=response.request_id,
                request_started=True,
            )
        result = _parse_statuses(
            _decode_json(response, stage="inspect"), set(agent_ids)
        )
        assert_account_pin(self.profile_reader, credentials.pin)
        return result

    async def get_selected_live_status(self, agent_id: str) -> GrokAgentStatus | None:
        """Read the selected owner's live state using a current transcript cursor."""
        if not _valid_agent_id(agent_id):
            return None
        watch = getattr(self.transport, "watch_frames", None)
        if not callable(watch):
            return None
        credentials = self.credentials or read_credentials(
            self.profile_reader, self.dpapi_unprotector,
            expected_account_identity=self.expected_account_identity,
            expected_team_identity=self.expected_team_identity,
        )
        assert_account_pin(self.profile_reader, credentials.pin)
        try:
            response = await self.post_response(
                LIST_TRANSCRIPT_ENTRIES_URL, _normal_headers(credentials),
                {"agentId": agent_id, "limit": 1}, stage="inspect",
            )
            assert_account_pin(self.profile_reader, credentials.pin)
            if response.status in {401, 403}:
                raise GrokLinkFailure(LinkErrorCode.GROK_AUTH_EXPIRED, stage="inspect",
                                      http_status=response.status, request_started=True)
            if not 200 <= response.status < 300:
                raise GrokLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED, stage="inspect",
                                      http_status=response.status, request_started=True)
            body = _decode_json(response, stage="inspect")
            generation = body.get("generation") if isinstance(body, dict) else None
            if type(generation) is not int or not 0 < generation <= 4294967295:
                return None
            request = json.dumps({"cursors": [{"agentId": agent_id, "generation": generation,
                                               "afterUpdatedSeq": "0"}],
                                  "includeUnlistedAgents": False, "inlineBodyMaxBytes": 0},
                                 separators=(",", ":")).encode()
            headers = _normal_headers(credentials)
            headers["content-type"] = "application/connect+json"

            def inspect(frame):
                if not isinstance(frame, dict):
                    return _WATCH_STOP
                variants = ("connected", "rows", "cleared", "cursorTooOld", "heartbeat",
                            "agentState", "computerActions", "agentStateChanged", "turnFailed",
                            "rosterChanged", "boxState", "pageTitleChanged")
                if sum(name in frame for name in variants) != 1:
                    return _WATCH_STOP
                if "cleared" in frame or "cursorTooOld" in frame:
                    event = frame.get("cleared", frame.get("cursorTooOld"))
                    if not isinstance(event, dict) or not _valid_agent_id(event.get("agentId")):
                        return _WATCH_STOP
                    return _WATCH_STOP if event["agentId"] == agent_id else None
                if "agentState" not in frame:
                    return None
                state = frame["agentState"]
                if not isinstance(state, dict) or not isinstance(state.get("live", []), list):
                    return _WATCH_STOP
                if type(state.get("snapshot", False)) is not bool:
                    return _WATCH_STOP
                selected = []
                for live in state.get("live", []):
                    if not isinstance(live, dict) or not _valid_agent_id(live.get("agentId")):
                        return _WATCH_STOP
                    session = live.get("sessionId", "")
                    if not isinstance(session, str):
                        return _WATCH_STOP
                    if live["agentId"] == agent_id and session == "":
                        selected.append(live)
                if len(selected) > 1:
                    return _WATCH_STOP
                if selected:
                    try:
                        return _parse_statuses({"statuses": [{"agentId": agent_id,
                            "live": selected[0]}]}, {agent_id})[0]
                    except GrokLinkFailure:
                        return _WATCH_STOP
                return _WATCH_STOP if state.get("snapshot", False) else None

            try:
                async with asyncio.timeout(self.timeout_s):
                    return await watch(WATCH_TRANSCRIPTS_URL, headers=headers,
                        body=b"\x00" + len(request).to_bytes(4, "big") + request,
                        timeout_s=self.timeout_s, max_response_bytes=self.max_response_bytes,
                        inspect=inspect)
            except TimeoutError:
                return None
        finally:
            assert_account_pin(self.profile_reader, credentials.pin)

    async def get_sand_usage_availability(self) -> bool | None:
        """Read only the explicit availability field; absent or invalid is unknown."""
        credentials = self.credentials or read_credentials(
            self.profile_reader, self.dpapi_unprotector,
            expected_account_identity=self.expected_account_identity,
            expected_team_identity=self.expected_team_identity,
        )
        assert_account_pin(self.profile_reader, credentials.pin)
        try:
            response = await self.post_response(
                SAND_USAGE_URL, _normal_headers(credentials), {}, stage="inspect"
            )
        finally:
            assert_account_pin(self.profile_reader, credentials.pin)
        if not 200 <= response.status < 300:
            return None
        try:
            body = _decode_json(response, stage="inspect")
        except AgentLinkFailure:
            return None
        value = body.get("hasAvailableUsage") if isinstance(body, dict) else None
        return value if type(value) is bool else None

    async def inspect_turn_failure(self, *, agent_id: str, turn_id: str,
                                   session_id: str | None = None):
        """Inspect only a selected agent's typed watch; return fixed evidence only."""
        from .turn_failure import parse_correlated_turn_failure

        if not _valid_agent_id(agent_id) or not _valid_agent_id(turn_id):
            return None
        if session_id is not None and not _valid_agent_id(session_id):
            return None
        watch = getattr(self.transport, "watch_frames", None)
        if not callable(watch):
            return None
        credentials = self.credentials or read_credentials(
            self.profile_reader, self.dpapi_unprotector,
            expected_account_identity=self.expected_account_identity,
            expected_team_identity=self.expected_team_identity,
        )
        assert_account_pin(self.profile_reader, credentials.pin)
        request = json.dumps({"cursors": [{"agentId": agent_id}],
                              "includeUnlistedAgents": False,
                              "inlineBodyMaxBytes": 0}, separators=(",", ":")).encode()
        headers = _normal_headers(credentials)
        headers["content-type"] = "application/connect+json"
        def inspect(frame):
            if not isinstance(frame, dict):
                return _WATCH_STOP
            variants = ("connected", "rows", "cleared", "cursorTooOld", "heartbeat",
                        "agentState", "computerActions", "agentStateChanged", "turnFailed",
                        "rosterChanged", "boxState", "pageTitleChanged")
            if sum(name in frame for name in variants) != 1:
                return _WATCH_STOP
            if "cleared" in frame or "cursorTooOld" in frame:
                payload = frame.get("cleared", frame.get("cursorTooOld"))
                if isinstance(payload, dict) and _valid_agent_id(payload.get("agentId")):
                    if payload["agentId"] != agent_id:
                        return None
                return _WATCH_STOP
            failure = frame.get("turnFailed")
            code = parse_correlated_turn_failure(
                failure, expected_agent_id=agent_id, expected_turn_id=turn_id,
                expected_session_id=session_id,
            )
            if code is None:
                return None
            timestamp = failure.get("failedAtMs")
            if (isinstance(timestamp, bool) or not isinstance(timestamp, (str, int))
                    or not str(timestamp).isdecimal() or len(str(timestamp)) > 19
                    or not 0 < int(timestamp) <= 9223372036854775807):
                return None
            return code, int(timestamp)

        try:
            async with asyncio.timeout(self.timeout_s):
                result = await watch(
                    WATCH_TRANSCRIPTS_URL, headers=headers,
                    body=b"\x00" + len(request).to_bytes(4, "big") + request,
                    timeout_s=self.timeout_s, max_response_bytes=self.max_response_bytes,
                    inspect=inspect,
                )
        except TimeoutError:
            result = None
        finally:
            assert_account_pin(self.profile_reader, credentials.pin)
        return result

    async def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result
