"""OpenAI SIP ingress verification and single-attempt Calls commands.

The caller owns deduplication, the durable attempt ledger, feature/employee
authority and the fixed destination_key -> target_uri mapping. Never expose
``refer(target_uri=...)`` as a model tool. Bind the current server-resolved
privacy policy context before each command, as for LiveVoice.

Evidence: https://developers.openai.com/api/docs/guides/realtime-sip
(inspected 2026-09-08) documents HTTP 200 acknowledgements. REFER 200 means
OpenAI relayed REFER to the SIP provider, NOT that the destination connected.
Accept/reject/hangup also recognize an empty HTTP 204 as a strong command ACK.
Success describes the command only, not media establishment or call state.

SDK 2.54 verifies the original webhook bytes. Calls use the same fixed-endpoint
httpx/OutboundPrivacyGateway boundary as LiveVoice: no SDK automatic retries,
SDK request-body debug logging, redirects, or provider response-body parsing.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from openai import OpenAI

from .outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
)

MAX_WEBHOOK_BYTES = 128 * 1024
MAX_COMMAND_BYTES = 128 * 1024
_BASE_URL = "https://api.openai.com"
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
# Optional diagnostic metadata is dropped unless it has the provider's opaque
# request-id shape. Never copy arbitrary error/header text into a safe result.
_REQUEST_ID = re.compile(r"req_[a-f0-9]{32}\Z")
_TEL_URI = re.compile(r"tel:\+[1-9][0-9]{1,14}\Z")
_SIP_URI = re.compile(
    r"sips?:[A-Za-z0-9_.!~*'()+%-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
    r"(?::[0-9]{1,5})?(?:;transport=(?:tls|tcp|udp))?\Z"
)


@dataclass(frozen=True)
class TelephonyProviderResult:
    """Safe command receipt; uncertain must be reconciled, never blindly retried."""

    status: Literal["succeeded", "failed", "uncertain"]
    request_id: str | None = None
    safe_error_code: str | None = None
    observed_at: datetime | None = field(default_factory=lambda: datetime.now(timezone.utc))


class TelephonyWebhookError(ValueError):
    safe_error_code = "telephony_webhook_invalid"

    def __init__(self) -> None:
        super().__init__(self.safe_error_code)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _valid_session(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "realtime"
        and isinstance(value.get("model"), str)
        and 0 < len(value["model"]) <= 128
    )


class OpenAITelephonyProvider:
    name = "openai_realtime_sip"

    def __init__(
        self,
        *,
        config: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        # The injected client is a trusted embedding/test seam. Its transport
        # and hooks must not retry, log bodies, or redirect requests themselves.
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("telephony_provider_timeout_invalid")
        self._config = config
        self._http_client = http_client
        self._timeout = timeout

    def verify_webhook(
        self, raw: bytes, headers: Mapping, *, webhook_secret: str
    ) -> dict:
        """Verify before parsing; return sensitive event only to trusted ingress.

        The HTTP route still owns streaming request-size/content-type limits,
        replay storage and safe projections. This method does no I/O or logging.
        """
        try:
            if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_WEBHOOK_BYTES:
                raise ValueError
            if not isinstance(webhook_secret, str) or not 0 < len(webhook_secret) <= 4096:
                raise ValueError
            selected: dict[str, str] = {}
            for name, value in headers.items():
                normalized = name.lower()
                if normalized not in {"webhook-id", "webhook-timestamp", "webhook-signature"}:
                    continue
                if normalized in selected or not isinstance(value, str) or not 0 < len(value) <= 4096:
                    raise ValueError
                selected[normalized] = value
            # Verification has no API-key dependency and cannot use an ambient
            # API base URL. Constructing this client does not make a request.
            with OpenAI(
                api_key="webhook-verification-only",
                base_url=f"{_BASE_URL}/v1",
                max_retries=0,
                http_client=httpx.Client(trust_env=False),
            ) as client:
                client.webhooks.verify_signature(raw, selected, secret=webhook_secret)
            event = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                not isinstance(event, dict)
                or event.get("type") != "realtime.call.incoming"
                or not isinstance(event.get("id"), str)
                or not 0 < len(event["id"]) <= 256
                or not isinstance(event.get("data"), dict)
                or not isinstance(event["data"].get("call_id"), str)
                or not _CALL_ID.fullmatch(event["data"]["call_id"])
            ):
                raise ValueError
            return event
        except Exception:
            pass
        # Raise outside the handler: no SDK exception context containing raw
        # inputs survives for a caller that logs the exception traceback.
        raise TelephonyWebhookError()

    async def accept(
        self, call_id: str, *, api_key: str, session_config: Mapping[str, Any]
    ) -> TelephonyProviderResult:
        return await self._command("accept", call_id, api_key, session_config)

    async def reject(
        self, call_id: str, *, api_key: str, status_code: int = 603
    ) -> TelephonyProviderResult:
        return await self._command("reject", call_id, api_key, {"status_code": status_code})

    async def refer(
        self, call_id: str, *, api_key: str, target_uri: str
    ) -> TelephonyProviderResult:
        """Submit a server-resolved fixed target; success is REFER acceptance only."""
        return await self._command("refer", call_id, api_key, {"target_uri": target_uri})

    async def hangup(self, call_id: str, *, api_key: str) -> TelephonyProviderResult:
        return await self._command("hangup", call_id, api_key, {})

    async def _command(
        self, operation: str, call_id: str, api_key: str, payload: Mapping[str, Any]
    ) -> TelephonyProviderResult:
        attempted = False
        receipt: TelephonyProviderResult | None = None
        try:
            from ..features import Features

            if Features.is_enterprise():
                return TelephonyProviderResult("failed", safe_error_code="provider_unavailable")
            if not isinstance(api_key, str) or not api_key or len(api_key) > 4096 or any(
                ord(char) <= 32 or ord(char) >= 127 for char in api_key
            ):
                return TelephonyProviderResult("failed", safe_error_code="provider_unavailable")
            if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
                raise ValueError
            if not isinstance(payload, Mapping):
                raise ValueError
            # Snapshot before awaiting policy/review; never mutate the caller's
            # pinned session config or permit it to change during review.
            encoded = json.dumps(dict(payload), allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_COMMAND_BYTES:
                raise ValueError
            body = json.loads(encoded)
            if operation == "accept" and not _valid_session(body):
                raise ValueError
            if operation == "reject" and (
                type(body["status_code"]) is not int or not 300 <= body["status_code"] <= 699
            ):
                raise ValueError
            if operation == "refer":
                target = body["target_uri"]
                if not isinstance(target, str) or len(target) > 512 or not (
                    _TEL_URI.fullmatch(target) or _SIP_URI.fullmatch(target)
                ):
                    raise ValueError
            endpoint = f"{_BASE_URL}/v1/realtime/calls/{call_id}/{operation}"
            gateway = OutboundPrivacyGateway(self._config)

            async def send(protected_payload: Any) -> TelephonyProviderResult:
                nonlocal attempted, receipt
                if not isinstance(protected_payload, dict):
                    raise PrivacyError("telephony_privacy_blocked")
                if operation == "accept":
                    if not _valid_session(protected_payload):
                        raise PrivacyError("telephony_privacy_blocked")
                elif protected_payload != body:
                    # Never bypass masking by restoring a raw phone number,
                    # or issue a command to a changed/redacted destination.
                    raise PrivacyError("telephony_privacy_blocked")
                wire = json.dumps(protected_payload, allow_nan=False).encode("utf-8")
                if len(wire) > MAX_COMMAND_BYTES:
                    raise PrivacyError("telephony_privacy_blocked")

                async def submit(client: httpx.AsyncClient) -> TelephonyProviderResult:
                    nonlocal attempted, receipt
                    attempted = True
                    async with client.stream(
                        "POST", endpoint,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        content=wire if operation != "hangup" else None,
                        timeout=self._timeout,
                        follow_redirects=False,
                    ) as response:
                        request_id = response.headers.get("x-request-id", "")
                        request_id = request_id if _REQUEST_ID.fullmatch(request_id) else None
                        status = response.status_code
                        if status == 200 or (status == 204 and operation != "refer"):
                            receipt = TelephonyProviderResult("succeeded", request_id=request_id)
                        elif 400 <= status < 500 and status not in {408, 409}:
                            receipt = TelephonyProviderResult(
                                "failed", request_id=request_id, safe_error_code="provider_request_rejected"
                            )
                        else:
                            receipt = TelephonyProviderResult(
                                "uncertain", request_id=request_id, safe_error_code="provider_result_uncertain"
                            )
                        # No body reads: error bodies can contain secrets/PII
                        # or be arbitrarily large. Headers suffice for ACKs.
                    return receipt

                if self._http_client is not None:
                    return await submit(self._http_client)
                async with httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=False, trust_env=False,
                    transport=httpx.AsyncHTTPTransport(retries=0),
                ) as client:
                    return await submit(client)

            return await gateway.execute(
                body,
                provider="openai_realtime",
                descriptor=EgressDescriptor(
                    action=f"telephony.{operation}", transport="httpx",
                    destination=endpoint, provider="openai_realtime", tool="telephony",
                    model=body.get("model", ""),
                ),
                sender=send,
                base_url=_BASE_URL,
                source_kind=f"telephony_{operation}",
            )
        except Exception as exc:
            # A close/cleanup error must not erase an already observed ACK.
            if receipt is not None:
                return receipt
            if attempted:
                return TelephonyProviderResult("uncertain", safe_error_code="provider_result_uncertain")
            code = "telephony_privacy_blocked" if isinstance(exc, PrivacyError) else "provider_request_invalid"
            return TelephonyProviderResult("failed", safe_error_code=code)
        # Cancellation propagates. The caller's prewritten attempt ledger must
        # treat interruption before a durable receipt as uncertain; do not retry.
