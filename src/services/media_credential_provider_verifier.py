"""Fail-closed verification adapters for supported MediaOps providers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping

try:  # pragma: no cover - import is exercised in normal runtime
    import httpx
except Exception:  # pragma: no cover - allows package import in minimal envs
    httpx = None  # type: ignore[assignment]

from .media_credential_package import CredentialPackage
from .media_provider_capability_registry import bound_credential_capabilities


VERIFY_TIMEOUT_SECONDS = 8.0
MAX_PROVIDER_RESPONSE_BYTES = 1 * 1024 * 1024


# Identity verification is a trust boundary.  The vault must be able to
# distinguish an evidence-bearing result produced by this implementation from
# a result-shaped object returned by an injected/test adapter.  Object identity
# is intentionally kept private and is never serialized in the safe result.
_PROVIDER_IDENTITY_EVIDENCE = object()


@dataclass(frozen=True)
class CredentialVerificationResult:
    status: str
    capabilities: dict[str, str]
    remote_account_ref: str | None = None
    provider_code: str | None = None
    provider_evidence: object | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "capabilities": dict(self.capabilities),
            "remote_account_ref": self.remote_account_ref,
            "provider_code": self.provider_code,
        }


def has_trusted_identity_evidence(result: CredentialVerificationResult) -> bool:
    """Return whether a provider verifier supplied real identity evidence.

    The marker is intentionally not part of ``safe_dict``.  Callers can use
    this predicate without being able to manufacture a trusted result merely
    by copying the public status/capability fields.
    """

    return isinstance(result, CredentialVerificationResult) and result.provider_evidence is _PROVIDER_IDENTITY_EVIDENCE


def _unknown() -> dict[str, str]:
    return {key: "unknown" for key in ("identity", "publish", "media", "analytics")}


def _safe_ref(value: Any) -> str | None:
    # ``bool`` is an ``int`` subclass in Python, but it is never a valid
    # provider identity.  Reject it explicitly so a malformed response such
    # as ``{"id": true}`` cannot verify an account named ``true``.
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)) and str(value).strip() and len(str(value)) <= 255:
        return str(value).strip()
    return None


def _matches(account_ref: str | None, values: list[Any]) -> bool:
    expected = str(account_ref or "").strip().lstrip("@").casefold()
    if not expected:
        return False
    for value in values:
        candidate = _safe_ref(value)
        if candidate is not None and candidate.lstrip("@").casefold() == expected:
            return True
    return False


class MediaCredentialProviderVerifier:
    """Only calls documented identity endpoints; no synthetic success path."""

    def __init__(self, *, timeout: float = VERIFY_TIMEOUT_SECONDS, client_factory: Any = None) -> None:
        self.timeout = max(1.0, min(float(timeout), 30.0))
        self.client_factory = client_factory

    async def _get(self, url: str, *, headers: Mapping[str, str], params: Mapping[str, Any] | None = None) -> tuple[int, Any] | None:
        if httpx is None:
            return None
        factory = self.client_factory or httpx.AsyncClient
        try:
            client = factory(timeout=self.timeout, follow_redirects=False, trust_env=False)
        except TypeError:
            try:
                client = factory(timeout=self.timeout, follow_redirects=False)
            except TypeError:
                client = factory(timeout=self.timeout)
        try:
            async def fetch(session: Any) -> tuple[int, Any] | None:
                # Prefer streaming on real httpx clients so a hostile provider
                # response cannot be fully buffered before the size guard.
                stream = getattr(session, "stream", None)
                if callable(stream):
                    try:
                        async with stream(
                            "GET", url, headers=dict(headers), params=dict(params or {})
                        ) as response:
                            status = int(getattr(response, "status_code", 0) or 0)
                            headers_obj = getattr(response, "headers", {})
                            try:
                                declared_length = int(headers_obj.get("content-length", "0") or "0")
                            except (TypeError, ValueError):
                                declared_length = 0
                            if declared_length > MAX_PROVIDER_RESPONSE_BYTES:
                                return status, None
                            iterator = getattr(response, "aiter_bytes", None)
                            if callable(iterator):
                                chunks: list[bytes] = []
                                total = 0
                                async for chunk in iterator():
                                    total += len(chunk)
                                    if total > MAX_PROVIDER_RESPONSE_BYTES:
                                        return status, None
                                    chunks.append(bytes(chunk))
                                try:
                                    return status, json.loads(b"".join(chunks).decode("utf-8"))
                                except Exception:
                                    return status, None
                    except TypeError:
                        # A narrow test/dummy client may expose a different
                        # stream signature; use its bounded ``get`` fallback.
                        pass
                response = await session.get(url, headers=dict(headers), params=dict(params or {}))
                status = int(getattr(response, "status_code", 0) or 0)
                headers_obj = getattr(response, "headers", {})
                try:
                    declared_length = int(headers_obj.get("content-length", "0") or "0")
                except (TypeError, ValueError):
                    declared_length = 0
                body_bytes = getattr(response, "content", b"")
                if declared_length > MAX_PROVIDER_RESPONSE_BYTES or (
                    isinstance(body_bytes, (bytes, bytearray)) and len(body_bytes) > MAX_PROVIDER_RESPONSE_BYTES
                ):
                    return status, None
                try:
                    body = response.json()
                except Exception:
                    body = None
                return status, body

            if callable(getattr(client, "__aenter__", None)):
                async with client as session:
                    return await fetch(session)
            return await fetch(client)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        except Exception:
            # The provider exception may contain request headers/body; never
            # propagate it across the credential boundary.
            return None

    async def verify(
        self,
        package: CredentialPackage,
        *,
        account_ref: str | None,
    ) -> CredentialVerificationResult:
        platform = package.platform
        if package.connection_type != "oauth":
            return CredentialVerificationResult("unsupported", _unknown(), provider_code="credential_type_unsupported")
        access_token = package.payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            return CredentialVerificationResult("invalid", _unknown(), provider_code="missing_access_token")
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        if platform == "x":
            result = await self._get("https://api.x.com/2/users/me", headers=headers)
        elif platform == "youtube":
            result = await self._get(
                "https://www.googleapis.com/youtube/v3/channels",
                headers=headers,
                params={"part": "id,snippet", "mine": "true"},
            )
        elif platform == "patreon":
            result = await self._get("https://www.patreon.com/api/oauth2/v2/identity", headers=headers)
        else:
            return CredentialVerificationResult("unsupported", _unknown(), provider_code="provider_unsupported")
        if result is None:
            return CredentialVerificationResult("verification_pending", _unknown(), provider_code="provider_unavailable")
        status_code, body = result
        if status_code == 401:
            return CredentialVerificationResult("invalid", _unknown(), provider_code="unauthorized")
        if status_code in {403, 429} or status_code >= 500:
            return CredentialVerificationResult("verification_pending", _unknown(), provider_code="provider_retryable")
        if status_code < 200 or status_code >= 300 or not isinstance(body, Mapping):
            return CredentialVerificationResult("verification_pending", _unknown(), provider_code="provider_response_unknown")

        values: list[Any] = []
        remote_ref: str | None = None
        if platform == "x":
            data = body.get("data")
            if not isinstance(data, Mapping):
                return CredentialVerificationResult("verification_pending", _unknown(), provider_code="provider_response_unknown")
            remote_ref = _safe_ref(data.get("id")) or _safe_ref(data.get("username"))
            # Only provider-stable identifiers/handles are identity evidence;
            # display names are not unique and must never establish readiness.
            values.extend([data.get("id"), data.get("username")])
        elif platform == "youtube":
            items = body.get("items")
            if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
                return CredentialVerificationResult("verification_pending", _unknown(), provider_code="provider_response_unknown")
            first = items[0]
            snippet = first.get("snippet") if isinstance(first.get("snippet"), Mapping) else {}
            remote_ref = _safe_ref(first.get("id"))
            # Channel ids are the strongest evidence, while ``customUrl``
            # covers the normal @handle form users enter in the Character
            # account reference field.  Display titles are intentionally not
            # considered identity evidence.
            values.extend([first.get("id"), snippet.get("customUrl")])
        else:
            data = body.get("data")
            if not isinstance(data, Mapping):
                return CredentialVerificationResult("verification_pending", _unknown(), provider_code="provider_response_unknown")
            attrs = data.get("attributes") if isinstance(data.get("attributes"), Mapping) else {}
            remote_ref = _safe_ref(data.get("id"))
            values.extend([data.get("id"), attrs.get("vanity")])
        normalized_values = [
            candidate
            for value in values
            if (candidate := _safe_ref(value)) is not None
        ]
        if not normalized_values:
            return CredentialVerificationResult("verification_pending", _unknown(), provider_code="provider_response_unknown")
        if not _matches(account_ref, normalized_values):
            return CredentialVerificationResult("invalid", _unknown(), remote_ref, "identity_mismatch")
        return CredentialVerificationResult(
            "verified",
            {**_unknown(), "identity": "available"},
            remote_ref,
            "identity_match",
            _PROVIDER_IDENTITY_EVIDENCE,
        )


async def verify_media_credential(package: CredentialPackage, *, account_ref: str | None, verifier: MediaCredentialProviderVerifier | None = None) -> CredentialVerificationResult:
    try:
        result = await (verifier or MediaCredentialProviderVerifier()).verify(
            package,
            account_ref=account_ref,
        )
    except Exception:
        return CredentialVerificationResult(
            "verification_pending",
            _unknown(),
            provider_code="provider_unavailable",
        )
    if not isinstance(result, CredentialVerificationResult):
        return CredentialVerificationResult(
            "verification_pending",
            _unknown(),
            provider_code="provider_response_unknown",
        )
    if result.status == "verified" and not has_trusted_identity_evidence(result):
        return CredentialVerificationResult(
            "verification_pending",
            _unknown(),
            provider_code="provider_response_unknown",
        )
    bounded = bound_credential_capabilities(
        package.platform,
        result.capabilities,
        credential_status=result.status,
    )
    if bounded != result.capabilities:
        return CredentialVerificationResult(
            result.status,
            bounded,
            result.remote_account_ref,
            result.provider_code,
            result.provider_evidence,
        )
    return result


ProviderCredentialVerifier = MediaCredentialProviderVerifier
MediaCredentialVerificationResult = CredentialVerificationResult


__all__ = [
    "CredentialVerificationResult",
    "MediaCredentialVerificationResult",
    "MediaCredentialProviderVerifier",
    "ProviderCredentialVerifier",
    "VERIFY_TIMEOUT_SECONDS",
    "MAX_PROVIDER_RESPONSE_BYTES",
    "has_trusted_identity_evidence",
    "verify_media_credential",
]
