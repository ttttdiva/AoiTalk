"""Narrow Grok gateway API client with fixed errors and bounded responses."""

from __future__ import annotations

from typing import Mapping

from ..codes import AgentLinkFailure, LinkErrorCode
from .dpapi import DPAPIUnprotector
from .profile import ProfileReader
from .route import (
    GrokAgentStatus,
    GrokLinkFailure,
    GrokRouteProvider,
    HttpResponse,
    HttpTransport,
    _decode_json,
)
from .secrets import assert_account_pin

_ALLOWED_METHODS = frozenset(
    {"listAgents", "sendPrompt", "getAgentTranscriptTail", "getAgentThread"}
)


class GrokGatewayClient:
    """Expose only listAgents, sendPrompt and read-only inspection methods."""

    def __init__(
        self,
        profile_reader: ProfileReader,
        dpapi_unprotector: DPAPIUnprotector,
        transport: HttpTransport,
        *,
        expected_account_identity: str | None = None,
        expected_team_identity: str | None = None,
        timeout_s: float = 30.0,
        max_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._routes = GrokRouteProvider(
            profile_reader,
            dpapi_unprotector,
            transport,
            expected_account_identity=expected_account_identity,
            expected_team_identity=expected_team_identity,
            timeout_s=timeout_s,
            max_response_bytes=max_response_bytes,
        )
        self.last_failure: GrokLinkFailure | None = None

    @property
    def credentials(self):
        return self._routes.credentials

    @property
    def route(self):
        return self._routes.route

    async def ensure_route(self, *, force: bool = False):
        try:
            return await self._routes.ensure_route(force=force)
        except GrokLinkFailure as error:
            self.last_failure = error
            raise

    @staticmethod
    def _validate(method: str, body: Mapping[str, object]) -> str:
        if method == "listAgents":
            valid = not body
            stage = "gateway"
        elif method == "sendPrompt":
            valid = (
                isinstance(body.get("agentId"), str)
                and bool(body.get("agentId"))
                and isinstance(body.get("prompt"), str)
                and isinstance(body.get("clientNonce"), str)
                and bool(body.get("clientNonce"))
            )
            stage = "send"
        elif method == "getAgentTranscriptTail":
            valid = (
                isinstance(body.get("id"), str)
                and bool(body.get("id"))
                and type(body.get("limit")) is int
                and 1 <= body["limit"] <= 200
            )
            stage = "inspect"
        elif method == "getAgentThread":
            valid = all(
                isinstance(body.get(key), str) and bool(body.get(key))
                for key in ("id", "rootId")
            )
            stage = "inspect"
        else:
            raise GrokLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED, stage="gateway")
        if not valid:
            raise GrokLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED, stage=stage)
        return stage

    async def request(
        self, method: str, body: Mapping[str, object]
    ) -> tuple[object, HttpResponse]:
        if method not in _ALLOWED_METHODS or not isinstance(body, Mapping):
            raise GrokLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED, stage="gateway")
        stage = self._validate(method, body)
        route = await self.ensure_route()
        credentials = self.credentials
        assert credentials is not None
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {route.token}",
            **route.headers,
        }
        assert_account_pin(self._routes.profile_reader, credentials.pin)
        request_started = False
        try:
            request_started = True
            response = await self._routes.post_response(
                f"{route.url}/api/{method}", headers, body, stage=stage
            )
            if method != "sendPrompt" and response.status == 401:
                # The route token can expire independently of the desktop login.
                route = await self.ensure_route(force=True)
                credentials = self.credentials
                assert credentials is not None
                assert_account_pin(self._routes.profile_reader, credentials.pin)
                response = await self._routes.post_response(
                    f"{route.url}/api/{method}",
                    {
                        "content-type": "application/json",
                        "authorization": f"Bearer {route.token}",
                        **route.headers,
                    },
                    body,
                    stage=stage,
                )
            if method == "sendPrompt":
                if 400 <= response.status < 500:
                    raise GrokLinkFailure(
                        LinkErrorCode.GROK_SEND_REJECTED,
                        stage="send",
                        http_status=response.status,
                        request_id=response.request_id,
                        request_started=True,
                    )
                if not 200 <= response.status < 300:
                    raise GrokLinkFailure(
                        LinkErrorCode.GROK_SEND_UNCERTAIN,
                        stage="send",
                        http_status=response.status,
                        request_id=response.request_id,
                        request_started=True,
                    )
            elif not 200 <= response.status < 300:
                code = (
                    LinkErrorCode.GROK_INSPECT_SERVER_ERROR
                    if response.status >= 500
                    else LinkErrorCode.GROK_AUTH_EXPIRED
                    if response.status in {401, 403}
                    else LinkErrorCode.GROK_CONTRACT_CHANGED
                )
                raise GrokLinkFailure(
                    code,
                    stage="inspect" if method != "listAgents" else "gateway",
                    http_status=response.status,
                    request_id=response.request_id,
                    request_started=True,
                )
            assert self.credentials is not None
            assert_account_pin(self._routes.profile_reader, self.credentials.pin)
            self.last_failure = None
            return _decode_json(response, stage=stage), response
        except GrokLinkFailure as error:
            attempted = request_started or error.request_started
            if (
                method == "sendPrompt"
                and attempted
                and error.code is not LinkErrorCode.GROK_SEND_REJECTED
            ):
                failure = GrokLinkFailure(
                    LinkErrorCode.GROK_SEND_UNCERTAIN,
                    stage="send",
                    http_status=error.http_status,
                    request_id=error.request_id,
                    request_started=True,
                )
                self.last_failure = failure
                raise failure from None
            self.last_failure = error
            raise
        except AgentLinkFailure as error:
            failure = GrokLinkFailure(
                error.code,
                stage=stage,
                request_started=request_started,
            )
            self.last_failure = failure
            raise failure from None
        except Exception:
            # Profile/pin checks before the first POST are definite preflight failures.
            # Failures after post_response entered are ambiguous for sendPrompt.
            code = (
                LinkErrorCode.GROK_SEND_UNCERTAIN
                if method == "sendPrompt" and request_started
                else LinkErrorCode.GROK_CONTRACT_CHANGED
            )
            failure = GrokLinkFailure(
                code,
                stage=stage,
                request_started=request_started,
            )
            self.last_failure = failure
            raise failure from None

    async def list_agent_statuses(self, agent_ids: list[str]) -> tuple[GrokAgentStatus, ...]:
        return await self._routes.list_agent_statuses(agent_ids)

    async def get_selected_live_status(self, agent_id: str) -> GrokAgentStatus | None:
        return await self._routes.get_selected_live_status(agent_id)

    async def get_sand_usage_availability(self) -> bool | None:
        return await self._routes.get_sand_usage_availability()

    async def inspect_turn_failure(self, *, agent_id: str, turn_id: str,
                                   session_id: str | None = None):
        return await self._routes.inspect_turn_failure(
            agent_id=agent_id, turn_id=turn_id, session_id=session_id
        )

    async def list_agents(self) -> object:
        result, _ = await self.request("listAgents", {})
        return result

    async def send_prompt(
        self, *, agent_id: str, prompt: str, client_nonce: str
    ) -> tuple[object, HttpResponse]:
        return await self.request(
            "sendPrompt",
            {"agentId": agent_id, "prompt": prompt, "clientNonce": client_nonce},
        )

    async def get_agent_transcript_tail(
        self, *, agent_id: str, limit: int = 100
    ) -> tuple[object, HttpResponse]:
        return await self.request(
            "getAgentTranscriptTail", {"id": agent_id, "limit": limit}
        )

    async def get_agent_thread(
        self, *, agent_id: str, root_id: str
    ) -> tuple[object, HttpResponse]:
        return await self.request(
            "getAgentThread", {"id": agent_id, "rootId": root_id}
        )

    async def close(self) -> None:
        await self._routes.close()
