"""Official MediaOps provider execution adapters.

The router is invoked only after ``ExternalAction`` has persisted an immutable
intent/attempt fence and re-validated policy, approval, capability snapshot and
credential authority.  It deliberately receives ORM/session state through an
internal-only method so credentials and asset locations never enter an action
payload, log record or public DTO.

Supported write contracts:

* X API v2 create Post and media upload (including chunked upload processing)
* YouTube Data API v3 resumable video upload and custom thumbnail upload

Transport failures after a remote write begins are allowed to escape.  The
calling ``OperationsService`` converts them to ``uncertain`` and never retries
blindly.  Bounded 4xx/provider rejections return a deterministic failed result.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import re
import socket
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import select, update, exists

from ..memory.models import GenerationOutput, ExternalAction, ExternalActionAttempt, ContentVariantRevision
from ..security.media_credential_crypto import decrypt_media_credential, encrypt_media_credential
from ..security.http_log_redaction import install_http_log_redaction
from .media_credential_vault_service import MediaCredentialVaultService
from .media_asset_transport import public_asset_client


PROVIDER_ADAPTER_VERSION = "3"
install_http_log_redaction()
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024  # one bounded 1 GiB asset
X_SIMPLE_UPLOAD_BYTES = 15 * 1024 * 1024
X_CHUNK_BYTES = 4 * 1024 * 1024
X_MAX_PROCESSING_POLLS = 60
YOUTUBE_CHUNK_BYTES = 8 * 1024 * 1024  # multiple of the required 256 KiB
YOUTUBE_MAX_RECOVERY_PROBES = 3
YOUTUBE_MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024

_X_API = "https://api.x.com"
_YOUTUBE_API = "https://www.googleapis.com"


class MediaProviderExecutionError(RuntimeError):
    """Base class whose message is safe and contains no provider payload."""


class MediaProviderRejected(MediaProviderExecutionError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class MediaProviderPartial(MediaProviderExecutionError):
    def __init__(self, message: str, *, remote_ids: Sequence[str]):
        self.remote_ids = tuple(str(item) for item in remote_ids if item)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolvedMediaAsset:
    generation_output_id: UUID
    path: Path
    sha256: str
    mime_type: str
    size_bytes: int
    source_url: str


class _DefaultHttpClientFactory:
    def __call__(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=180.0, write=180.0, pool=15.0),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "AoiTalk-MediaOps/3"},
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_response_json(response: Any) -> Mapping[str, Any]:
    try:
        value = response.json()
    except Exception as exc:  # malformed 2xx is an ambiguous provider outcome
        raise MediaProviderExecutionError("provider returned an invalid response") from exc
    if not isinstance(value, Mapping):
        raise MediaProviderExecutionError("provider returned an invalid response")
    return value


def _response_status(response: Any) -> int:
    try:
        return int(response.status_code)
    except (TypeError, ValueError, AttributeError) as exc:
        raise MediaProviderExecutionError("provider response status is unavailable") from exc


def _check_provider_response(response: Any, *, operation: str) -> None:
    status = _response_status(response)
    if 200 <= status < 300:
        return
    if status == 429:
        raise MediaProviderRejected("rate_limited", f"{operation} was rate limited")
    if 400 <= status < 500:
        raise MediaProviderRejected(f"http_{status}", f"{operation} was rejected by the provider")
    # A remote 5xx can occur after the provider accepted a write.  Escaping
    # routes the attempt to uncertain rather than fabricating a safe retry.
    raise MediaProviderExecutionError(f"{operation} outcome is ambiguous")


async def _file_chunks(path: Path, *, chunk_size: int = 1024 * 1024, offset: int = 0, length: int | None = None):
    """Stream a bounded local asset without retaining the whole video in RAM."""

    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            if length == 0:
                return
            chunk = await asyncio.to_thread(handle.read, min(chunk_size, length) if length is not None else chunk_size)
            if not chunk:
                return
            if length is not None:
                length -= len(chunk)
            yield chunk


def _youtube_session_url(value: Any) -> str:
    """Pin bearer-bearing requests to Google's documented upload endpoint."""
    try:
        parsed = urlparse(value) if isinstance(value, str) else None
        valid = (parsed is not None and len(value) <= 8192 and parsed.scheme == "https"
                 and parsed.hostname == "www.googleapis.com" and parsed.port in (None, 443)
                 and parsed.path == "/upload/youtube/v3/videos" and not parsed.username
                 and not parsed.password and not parsed.fragment and bool(parsed.query)
                 and not any(ord(c) < 33 for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise MediaProviderExecutionError("YouTube upload session receipt is invalid")
    return value


def _remote_id(value: Any, platform: str) -> str:
    pattern = r"[0-9]{1,19}" if platform == "x" else r"[A-Za-z0-9_-]{1,128}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise MediaProviderExecutionError("provider resource receipt is invalid")
    return value


class MediaProviderCheckpoint:
    """A committed encrypted journal on the existing publication attempt.

    The AAD uses a distinct namespace and account binding. The plaintext also
    binds all immutable authority, so edited content/account cannot resume it.
    Never store raw OAuth tokens or serialize upload URLs in safe projections.
    """

    def __init__(self, *, session: Any, action: Any, attempt: Any):
        self.session, self.action, self.attempt = session, action, attempt
        self.action_version = action.version
        self.binding = {key: str(getattr(action, key, None)) for key in (
            "id", "owner_user_id", "project_id", "platform", "platform_account_id",
            "platform_account_revision_id", "persona_revision_id", "content_variant_id",
            "payload_hash", "action_version", "credential_state_hash", "execution_key",
        )}

    def load(self) -> dict[str, Any]:
        ciphertext = getattr(self.attempt, "provider_state_ciphertext", None)
        if not ciphertext:
            return {}
        value = decrypt_media_credential(ciphertext,
            credential_id=f"publication-attempt:{self.attempt.id}",
            platform_account_id=self.action.platform_account_id)
        if not isinstance(value, dict) or value.get("binding") != self.binding or not isinstance(value.get("state"), dict):
            raise MediaProviderExecutionError("provider checkpoint authority does not match")
        return value["state"]

    async def save(self, state: Mapping[str, Any]) -> None:
        await self.check()
        encrypted = encrypt_media_credential({"binding": self.binding, "state": dict(state)},
            credential_id=f"publication-attempt:{self.attempt.id}",
            platform_account_id=self.action.platform_account_id)
        self.attempt.provider_state_ciphertext = encrypted.ciphertext
        self.attempt.provider_lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=15)
        await self.session.flush()
        await self.session.commit()

    async def check(self) -> None:
        # Lock order matches recovery admission: action, then attempt. The
        # target predicate is re-evaluated after a PostgreSQL lock wait.
        claimed = await self.session.execute(update(ExternalAction).where(
            ExternalAction.id == self.action.id, ExternalAction.version == self.action_version,
            ExternalAction.status == "attempting",
        ).values(version=ExternalAction.version).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise MediaProviderExecutionError("provider execution fence changed")
        result = await self.session.execute(select(ExternalAction.version, ExternalAction.status, ExternalActionAttempt.status)
            .join(ExternalActionAttempt, ExternalActionAttempt.action_id == ExternalAction.id)
            .where(ExternalAction.id == self.action.id, ExternalActionAttempt.id == self.attempt.id))
        row = result.first()
        if row is None or tuple(row) != (self.action_version, "attempting", "running"):
            raise MediaProviderExecutionError("provider execution fence changed")
        renewed = await self.session.execute(update(ExternalActionAttempt).where(
            ExternalActionAttempt.id == self.attempt.id, ExternalActionAttempt.status == "running",
            exists(select(ExternalAction.id).where(ExternalAction.id == self.action.id,
                ExternalAction.version == self.action_version, ExternalAction.status == "attempting")),
        ).values(provider_lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=15))
          .execution_options(synchronize_session=False))
        if renewed.rowcount != 1:
            raise MediaProviderExecutionError("provider execution fence changed")


class _GuardedProviderClient:
    """Re-check the durable claim and credential before each provider call."""

    def __init__(self, client: Any, journal: Any, authorize: Callable[..., Any]):
        self.client, self.journal, self.authorize = client, journal, authorize

    def __getattr__(self, key: str) -> Any:
        return getattr(self.client, key)

    async def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        if "Authorization" in kwargs.get("headers", {}):
            await self.journal.check()
            await self.authorize()
        return await getattr(self.client, method)(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self._call("post", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Any:
        return await self._call("put", url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self._call("get", url, **kwargs)


def _token(payload: Mapping[str, Any]) -> str:
    value = payload.get("access_token")
    if not isinstance(value, str) or not value.strip():
        raise MediaProviderRejected("credential_missing", "provider access token is unavailable")
    return value.strip()


def _collect_generation_output_ids(payload: Mapping[str, Any]) -> list[UUID]:
    ordered: list[UUID] = []

    def add(value: Any) -> None:
        try:
            item = UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise MediaProviderRejected("asset_reference_invalid", "media asset reference is invalid") from None
        if item not in ordered:
            ordered.append(item)

    for key in ("media", "media_asset", "thumbnail", "cover"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                add(item)
    posts = payload.get("posts")
    if isinstance(posts, Sequence) and not isinstance(posts, (str, bytes, bytearray)):
        for post in posts:
            if not isinstance(post, Mapping):
                continue
            media = post.get("media")
            if isinstance(media, Sequence) and not isinstance(media, (str, bytes, bytearray)):
                for item in media:
                    add(item)
    return ordered


async def _assert_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MediaProviderRejected("asset_url_invalid", "media asset URL must be public HTTPS")
    if parsed.port not in (None, 443):
        raise MediaProviderRejected("asset_url_invalid", "media asset URL uses an unsupported port")
    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise MediaProviderRejected("asset_url_private", "media asset URL is not public")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            rows = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise MediaProviderRejected("asset_dns_failed", "media asset host could not be resolved") from exc
        addresses = []
        for row in rows:
            try:
                address = ipaddress.ip_address(row[4][0].split("%", 1)[0])
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
    if not addresses or any(not address.is_global for address in addresses):
        raise MediaProviderRejected("asset_url_private", "media asset URL is not public")


async def _download_asset(
    client: Any,
    *,
    row: GenerationOutput,
    target: Path,
    expected_sha256: str | None = None,
) -> ResolvedMediaAsset:
    source_url = str(row.deep_link or "").strip()
    await _assert_public_https_url(source_url)
    digest = hashlib.sha256()
    total = 0
    try:
        stream_context = client.stream("GET", source_url, headers={"Accept": "*/*", "Accept-Encoding": "identity"})
        async with stream_context as response:
            status = _response_status(response)
            if 300 <= status < 400:
                raise MediaProviderRejected("asset_redirect_blocked", "media asset redirected unexpectedly")
            if status != 200:
                raise MediaProviderRejected("asset_download_failed", "media asset could not be downloaded")
            if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
                raise MediaProviderRejected("asset_encoding_unsupported", "compressed media assets are unsupported")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_DOWNLOAD_BYTES:
                        raise MediaProviderRejected("asset_too_large", "media asset exceeds the upload limit")
                except ValueError:
                    raise MediaProviderRejected("asset_download_failed", "media asset length is invalid") from None
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise MediaProviderRejected("asset_too_large", "media asset exceeds the upload limit")
                    digest.update(chunk)
                    handle.write(chunk)
    except MediaProviderRejected:
        raise
    except (httpx.HTTPError, OSError) as exc:
        # Asset GETs have no remote write side effect, but the publication
        # attempt has already been fenced.  Return a deterministic local failure.
        raise MediaProviderRejected("asset_download_failed", "media asset could not be downloaded") from exc
    if total <= 0:
        raise MediaProviderRejected("asset_empty", "media asset is empty")
    expected = str(expected_sha256 or row.sha256 or "").strip().lower()
    observed = digest.hexdigest()
    if not expected or not hmac.compare_digest(expected, observed):
        raise MediaProviderRejected("asset_hash_mismatch", "media asset integrity check failed")
    return ResolvedMediaAsset(
        generation_output_id=row.id,
        path=target,
        sha256=observed,
        mime_type=str(row.mime_type or "application/octet-stream").strip().lower(),
        size_bytes=total,
        source_url=source_url,
    )


class MediaProviderExecutionRouter:
    """Internal router for reviewed official provider write contracts."""

    adapter_version = PROVIDER_ADAPTER_VERSION

    def __init__(
        self,
        *,
        vault: MediaCredentialVaultService | None = None,
        client_factory: Callable[[], Any] | None = None,
        asset_loader: Callable[..., Any] | None = None,
        asset_service: Any | None = None,
        asset_client_factory: Callable[[], Any] = public_asset_client,
        sleep: Callable[[float], Any] = asyncio.sleep,
        checkpoint_factory: Callable[..., Any] = MediaProviderCheckpoint,
    ) -> None:
        self._vault = vault or MediaCredentialVaultService()
        self._client_factory = client_factory or _DefaultHttpClientFactory()
        self._asset_loader = asset_loader
        self._asset_service = asset_service
        self._asset_client_factory = asset_client_factory
        self._sleep = sleep
        self._checkpoint_factory = checkpoint_factory

    async def execute_internal(self, *, session: Any, action: Any, attempt: Any) -> dict[str, Any]:
        return await self._run(session=session, action=action, attempt=attempt, recovery=False, resume=False)

    async def recover_internal(self, *, session: Any, action: Any, attempt: Any, resume: bool = False) -> dict[str, Any]:
        return await self._run(session=session, action=action, attempt=attempt, recovery=True, resume=resume)

    async def _run(self, *, session: Any, action: Any, attempt: Any, recovery: bool, resume: bool) -> dict[str, Any]:
        platform = str(getattr(action, "platform", "") or "").strip().lower()
        raw = action.payload_json if isinstance(action.payload_json, Mapping) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else {}
        account_id = getattr(action, "platform_account_id", None)
        state_hash = str(getattr(action, "credential_state_hash", "") or "")
        if platform not in {"x", "youtube"}:
            return {
                "status": "failed",
                "remote_status": "unsupported_provider",
                "error_message": "provider write adapter is not implemented",
                "provider_attempt_ref": str(attempt.id),
            }
        from .media_publication_contract import official_write_blockers
        blockers = official_write_blockers(platform, payload)
        if raw.get("schedule"):
            blockers.append("provider_schedule_metadata_unsupported")
        if blockers:
            return {"status": "failed", "remote_status": blockers[0],
                "error_message": "provider publication body is unsupported",
                "provider_attempt_ref": str(attempt.id)}
        if (getattr(action, "adapter_key", None) != f"media-provider/{platform}"
                or getattr(action, "adapter_version", None) != PROVIDER_ADAPTER_VERSION):
            return {"status": "failed", "remote_status": "provider_route_mismatch",
                "error_message": "provider route does not match the pinned official API adapter",
                "provider_attempt_ref": str(attempt.id)}
        if account_id is None or len(state_hash) != 64:
            return {
                "status": "failed",
                "remote_status": "authority_missing",
                "error_message": "provider execution authority is incomplete",
                "provider_attempt_ref": str(attempt.id),
            }
        try:
            journal = self._checkpoint_factory(session=session, action=action, attempt=attempt)
            state = journal.load()
            if state and not recovery:
                raise MediaProviderExecutionError("provider checkpoint already exists; recovery is required")
            if recovery and not state:
                return self._unresolved(str(attempt.id), "checkpoint_missing")
            async with self._vault.execution_handle(
                session,
                platform_account_id=account_id,
                expected_state_hash=state_hash,
                expected_platform=platform,
            ) as credential:
                scratch = Path(__file__).resolve().parents[2] / ".local" / "media-provider"
                scratch.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="attempt-", dir=scratch) as temporary:
                    async with self._client_factory() as raw_client:
                        async def authorize() -> None:
                            async with self._vault.execution_handle(session, platform_account_id=account_id,
                                expected_state_hash=state_hash, expected_platform=platform):
                                pass
                            await session.commit()
                        client = _GuardedProviderClient(raw_client, journal, authorize)
                        if recovery and platform == "x":
                            return await self._recover_x(client=client, state=state,
                                access_token=_token(credential.payload), attempt_id=str(attempt.id))
                        if recovery and platform == "youtube" and not resume:
                            return await self._recover_youtube(client=client, state=state, journal=journal,
                                access_token=_token(credential.payload), attempt_id=str(attempt.id))
                        assets = await self._resolve_assets(
                            session=session,
                            action=action,
                            payload=payload,
                            directory=Path(temporary),
                            client=client,
                        )
                        if platform == "x":
                            return await self._execute_x(
                                client=client,
                                payload=payload,
                                assets=assets,
                                access_token=_token(credential.payload),
                                attempt_id=str(attempt.id),
                                journal=journal,
                            )
                        return await self._execute_youtube(
                            client=client,
                            payload=payload,
                            assets=assets,
                            access_token=_token(credential.payload),
                            attempt_id=str(attempt.id),
                            journal=journal,
                            state=state,
                        )
        except MediaProviderPartial as exc:
            return {
                "status": "uncertain",
                "provider_attempt_ref": str(attempt.id),
                "remote_status": "partial_remote_write",
                "remote_resource_id": exc.remote_ids[-1] if exc.remote_ids else None,
                "result_summary": str(exc),
                "evidence_note": "remote write was only partially confirmed; reconcile before retry",
                "provider_observed_at": _utc_now(),
            }
        except MediaProviderRejected as exc:
            return {
                "status": "uncertain" if recovery or (locals().get("state", {}).get("session_url")) else "failed",
                "provider_attempt_ref": str(attempt.id),
                "remote_status": exc.code,
                "error_message": str(exc),
                "provider_observed_at": _utc_now(),
            }

    @staticmethod
    def _unresolved(attempt_id: str, code: str) -> dict[str, Any]:
        return {"status": "uncertain", "provider_attempt_ref": attempt_id,
                "remote_status": code, "provider_observed_at": _utc_now(),
                "evidence_note": f"provider outcome remains unresolved ({code}); no new publication was created"}

    @staticmethod
    def _receipt(platform: str, resource_id: str, attempt_id: str, remote_status: str) -> dict[str, Any]:
        return {"status": "succeeded", "provider_attempt_ref": attempt_id,
                "provider_receipt_ref": f"{platform}:{'post' if platform == 'x' else 'video'}:{resource_id}",
                "remote_resource_id": resource_id,
                "remote_url": f"https://x.com/i/web/status/{resource_id}" if platform == "x" else f"https://www.youtube.com/watch?v={resource_id}",
                "remote_status": remote_status, "confirmation_level": "provider_confirmed",
                "provider_observed_at": _utc_now(), "evidence_note": "official provider returned a resource receipt"}

    async def _recover_x(self, *, client: Any, state: Mapping[str, Any], access_token: str, attempt_id: str) -> dict[str, Any]:
        posts = state.get("post_ids", [])
        # A missing create receipt cannot be disproved by timeline/search lookup.
        # The platform has no documented client idempotency key for create Post.
        if not posts:
            return self._unresolved(attempt_id, "receipt_unknown")
        for post_id in posts:
            post_id = _remote_id(post_id, "x")
            response = await client.get(f"{_X_API}/2/tweets/{post_id}",
                headers={"Authorization": f"Bearer {access_token}"})
            _check_provider_response(response, operation="X post lookup")
            data = _safe_response_json(response).get("data")
            if not isinstance(data, Mapping) or data.get("id") != post_id:
                return self._unresolved(attempt_id, "receipt_unknown")
        if state.get("phase") != "complete":
            return self._unresolved(attempt_id, "partial_remote_write")
        return self._receipt("x", posts[-1], attempt_id, "published")

    async def _resolve_assets(
        self,
        *,
        session: Any,
        action: Any,
        payload: Mapping[str, Any],
        directory: Path,
        client: Any,
    ) -> dict[UUID, ResolvedMediaAsset]:
        ids = _collect_generation_output_ids(payload)
        if not ids:
            return {}
        if self._asset_loader is not None:
            value = self._asset_loader(
                session=session,
                action=action,
                generation_output_ids=ids,
                directory=directory,
                client=client,
            )
            loaded = await value if hasattr(value, "__await__") else value
            if not isinstance(loaded, Mapping):
                raise MediaProviderRejected("asset_loader_invalid", "media asset loader returned invalid data")
            return dict(loaded)
        # The reviewed revision, rather than mutable output metadata, owns the
        # expected digest. Refresh both rows even if this session cached them.
        from .operations_service import _media_revision_content_hash, canonical_json
        from .media_operations_content_service import canonical_publication_payload
        revision_result = await session.execute(select(ContentVariantRevision).where(
            ContentVariantRevision.id == getattr(action, "content_variant_revision_id", None)
        ).execution_options(populate_existing=True))
        revision = revision_result.scalar_one_or_none()
        raw = action.payload_json if isinstance(action.payload_json, Mapping) else {}
        if (revision is None or revision.owner_user_id != action.owner_user_id
                or revision.project_id != action.project_id
                or revision.content_hash != raw.get("content_variant_revision_hash")
                or _media_revision_content_hash(revision) != revision.content_hash
                or canonical_json(canonical_publication_payload(revision.payload_json, action.platform))
                != canonical_json(canonical_publication_payload(payload, action.platform))):
            raise MediaProviderRejected("asset_review_binding_invalid", "media asset reviewed revision is invalid")
        pins = {UUID(ref["generation_output_id"]): ref["sha256"] for ref in revision.generation_output_refs_json}
        if any(output_id not in pins for output_id in ids):
            raise MediaProviderRejected("asset_review_binding_invalid", "media asset is absent from the reviewed manifest")
        result = await session.execute(select(GenerationOutput).where(GenerationOutput.id.in_(ids))
            .execution_options(populate_existing=True))
        rows = list(result.scalars().all())
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(ids):
            raise MediaProviderRejected("asset_not_found", "media asset is unavailable")
        resolved: dict[UUID, ResolvedMediaAsset] = {}
        from .media_asset_service import MediaAssetService
        from .media_operations_service import MediaOperationsError
        from .storage_io import StorageError
        asset_service = self._asset_service or MediaAssetService()
        # Only an unregistered output may use the public HTTPS reader. A
        # registered asset failure must never send its mutable deep_link.
        async with AsyncExitStack() as asset_context:
            asset_client = None
            for index, output_id in enumerate(ids):
                row = by_id[output_id]
                if (
                    row.owner_user_id != getattr(action, "owner_user_id", None)
                    or row.project_id != getattr(action, "project_id", None)
                ):
                    raise MediaProviderRejected("asset_scope_mismatch", "media asset scope is invalid")
                if row.sha256 != pins[output_id]:
                    raise MediaProviderRejected("asset_hash_mismatch", "media asset differs from the reviewed manifest")
                target = directory / f"asset-{index:02d}"
                try:
                    managed = await asset_service.materialize_for_provider(
                        session, output_id, owner_user_id=revision.owner_user_id,
                        project_id=revision.project_id, expected_sha256=pins[output_id], target=target)
                except (MediaOperationsError, StorageError, OSError) as exc:
                    raise MediaProviderRejected("managed_asset_invalid", "registered media asset could not be verified") from exc
                if managed is not None:
                    resolved[output_id] = ResolvedMediaAsset(
                        generation_output_id=managed.generation_output_id, path=managed.path,
                        sha256=managed.sha256, mime_type=managed.mime_type,
                        size_bytes=managed.size_bytes, source_url="")
                    continue
                # This client pins public IPs at connect and never shares the
                # authenticated provider client's DNS/proxy/headers route.
                if asset_client is None:
                    asset_client = await asset_context.enter_async_context(self._asset_client_factory())
                resolved[output_id] = await _download_asset(asset_client, row=row, target=target,
                    expected_sha256=pins[output_id])
        return resolved

    @staticmethod
    def _asset(assets: Mapping[UUID, ResolvedMediaAsset], value: Any) -> ResolvedMediaAsset:
        try:
            key = UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise MediaProviderRejected("asset_reference_invalid", "media asset reference is invalid") from None
        item = assets.get(key)
        if item is None:
            raise MediaProviderRejected("asset_not_found", "media asset is unavailable")
        return item

    @staticmethod
    def _x_text(payload: Mapping[str, Any]) -> str:
        text = str(payload.get("text") or "").strip()
        hashtags = payload.get("hashtags")
        if isinstance(hashtags, Sequence) and not isinstance(hashtags, (str, bytes, bytearray)):
            tags = [f"#{str(item).strip().lstrip('#')}" for item in hashtags if str(item).strip()]
            if tags:
                text = " ".join([text, *tags]).strip()
        links = payload.get("links")
        if isinstance(links, Sequence) and not isinstance(links, (str, bytes, bytearray)):
            values = [str(item).strip() for item in links if str(item).strip()]
            if values:
                text = " ".join([text, *values]).strip()
        if not text:
            raise MediaProviderRejected("payload_invalid", "X post text is empty")
        return text

    @staticmethod
    def _reply_id(value: Any) -> str | None:
        rendered = str(value or "").strip()
        if not rendered:
            return None
        match = re.search(r"(\d{5,})/?$", rendered)
        if match:
            return match.group(1)
        if re.fullmatch(r"\d{5,}", rendered):
            return rendered
        raise MediaProviderRejected("reply_reference_invalid", "X reply reference is invalid")

    async def _x_media_id(self, client: Any, access_token: str, asset: ResolvedMediaAsset) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        category = (
            "tweet_gif"
            if asset.mime_type == "image/gif"
            else "tweet_video"
            if asset.mime_type.startswith("video/")
            else "tweet_image"
        )
        if category == "tweet_image" and asset.size_bytes <= X_SIMPLE_UPLOAD_BYTES:
            encoded = base64.b64encode(await asyncio.to_thread(asset.path.read_bytes)).decode("ascii")
            response = await client.post(
                f"{_X_API}/2/media/upload",
                headers=headers,
                json={"media": encoded, "media_category": category},
            )
            _check_provider_response(response, operation="X media upload")
            data = _safe_response_json(response).get("data")
            media_id = data.get("id") if isinstance(data, Mapping) else None
            return _remote_id(media_id, "x")

        init = await client.post(
            f"{_X_API}/2/media/upload/initialize",
            headers=headers,
            json={
                "media_type": asset.mime_type,
                "total_bytes": asset.size_bytes,
                "media_category": category,
            },
        )
        _check_provider_response(init, operation="X media upload initialization")
        init_data = _safe_response_json(init).get("data")
        media_id = init_data.get("id") if isinstance(init_data, Mapping) else None
        media_id = _remote_id(media_id, "x")
        try:
            with asset.path.open("rb") as handle:
                segment = 0
                while True:
                    chunk = handle.read(X_CHUNK_BYTES)
                    if not chunk:
                        break
                    append = await client.post(
                        f"{_X_API}/2/media/upload/{media_id}/append",
                        headers=headers,
                        json={"segment_index": segment, "media": base64.b64encode(chunk).decode("ascii")},
                    )
                    _check_provider_response(append, operation="X media chunk upload")
                    segment += 1
            finalize = await client.post(
                f"{_X_API}/2/media/upload/{media_id}/finalize",
                headers=headers,
            )
            _check_provider_response(finalize, operation="X media upload finalization")
            body = _safe_response_json(finalize)
            data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
            processing = data.get("processing_info") if isinstance(data, Mapping) else None
            for _ in range(X_MAX_PROCESSING_POLLS):
                if not isinstance(processing, Mapping):
                    return media_id
                state = str(processing.get("state") or "").strip().lower()
                if state == "succeeded":
                    return media_id
                if state == "failed":
                    raise MediaProviderRejected("media_processing_failed", "X media processing failed")
                delay = processing.get("check_after_secs", 1)
                try:
                    seconds = max(0.25, min(float(delay), 10.0))
                except (TypeError, ValueError):
                    seconds = 1.0
                await self._sleep(seconds)
                status = await client.get(
                    f"{_X_API}/2/media/upload",
                    headers=headers,
                    params={"command": "STATUS", "media_id": media_id},
                )
                _check_provider_response(status, operation="X media processing status")
                status_data = _safe_response_json(status).get("data")
                processing = status_data.get("processing_info") if isinstance(status_data, Mapping) else None
            raise MediaProviderPartial("X media processing did not complete", remote_ids=[media_id])
        except MediaProviderRejected:
            raise
        except MediaProviderPartial:
            raise
        except Exception as exc:
            raise MediaProviderPartial(
                "X media upload outcome is ambiguous",
                remote_ids=[media_id],
            ) from exc

    async def _x_create_post(
        self,
        client: Any,
        *,
        access_token: str,
        text: str,
        media_ids: Sequence[str],
        reply_to: str | None,
    ) -> str:
        body: dict[str, Any] = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": list(media_ids)}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        response = await client.post(
            f"{_X_API}/2/tweets",
            headers={"Authorization": f"Bearer {access_token}"},
            json=body,
        )
        _check_provider_response(response, operation="X post creation")
        data = _safe_response_json(response).get("data")
        post_id = data.get("id") if isinstance(data, Mapping) else None
        return _remote_id(post_id, "x")

    async def _execute_x(
        self,
        *,
        client: Any,
        payload: Mapping[str, Any],
        assets: Mapping[UUID, ResolvedMediaAsset],
        access_token: str,
        attempt_id: str,
        journal: Any,
    ) -> dict[str, Any]:
        kind = str(payload.get("type") or "").strip().lower()
        posted: list[str] = []
        state: dict[str, Any] = {"phase": "preparing", "post_ids": []}
        await journal.save(state)
        try:
            if kind == "x_post":
                media_ids = [
                    await self._x_media_id(client, access_token, self._asset(assets, item))
                    for item in payload.get("media", [])
                ]
                state["phase"] = "create_pending"
                await journal.save(state)
                post_id = await self._x_create_post(
                    client,
                    access_token=access_token,
                    text=self._x_text(payload),
                    media_ids=media_ids,
                    reply_to=self._reply_id(payload.get("reply_to")),
                )
                posted.append(post_id)
                state.update(phase="complete", post_ids=list(posted))
                await journal.save(state)
            elif kind == "x_thread":
                posts = payload.get("posts")
                if not isinstance(posts, Sequence) or isinstance(posts, (str, bytes, bytearray)) or not posts:
                    raise MediaProviderRejected("payload_invalid", "X thread payload is invalid")
                reply_to = self._reply_id(payload.get("reply_to"))
                for item in posts:
                    if not isinstance(item, Mapping):
                        raise MediaProviderRejected("payload_invalid", "X thread payload is invalid")
                    media_ids = [
                        await self._x_media_id(client, access_token, self._asset(assets, ref))
                        for ref in item.get("media", [])
                    ]
                    state["phase"] = "create_pending"
                    await journal.save(state)
                    post_id = await self._x_create_post(
                        client,
                        access_token=access_token,
                        text=str(item.get("text") or "").strip(),
                        media_ids=media_ids,
                        reply_to=reply_to,
                    )
                    posted.append(post_id)
                    state.update(phase="complete" if len(posted) == len(posts) else "partial", post_ids=list(posted))
                    await journal.save(state)
                    reply_to = post_id
            else:
                raise MediaProviderRejected("payload_invalid", "X payload type is unsupported")
        except MediaProviderRejected as exc:
            if posted:
                raise MediaProviderPartial(
                    "X thread was only partially published",
                    remote_ids=posted,
                ) from exc
            raise
        except MediaProviderPartial:
            raise
        except Exception as exc:
            if posted:
                raise MediaProviderPartial(
                    "X publication outcome is ambiguous",
                    remote_ids=posted,
                ) from exc
            raise
        final_id = posted[-1]
        return {
            "status": "succeeded",
            "provider_attempt_ref": attempt_id,
            "provider_receipt_ref": f"x:post:{final_id}",
            "remote_resource_id": final_id,
            "remote_url": f"https://x.com/i/web/status/{final_id}",
            "remote_status": "published",
            "result_summary": f"published {len(posted)} X post(s)",
            "confirmation_level": "provider_confirmed",
            "provider_observed_at": _utc_now(),
            "evidence_note": "X API v2 returned post identifiers",
        }

    async def _execute_youtube(
        self,
        *,
        client: Any,
        payload: Mapping[str, Any],
        assets: Mapping[UUID, ResolvedMediaAsset],
        access_token: str,
        attempt_id: str,
        journal: Any,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        kind = str(payload.get("type") or "").strip().lower()
        if kind not in {"youtube_video", "youtube_short"}:
            raise MediaProviderRejected("payload_invalid", "YouTube payload type is unsupported")
        if payload.get("captions"):
            raise MediaProviderRejected(
                "captions_not_implemented",
                "YouTube caption upload is not implemented by this adapter",
            )
        media_refs = payload.get("media_asset")
        if not isinstance(media_refs, Sequence) or isinstance(media_refs, (str, bytes, bytearray)) or len(media_refs) != 1:
            raise MediaProviderRejected("payload_invalid", "YouTube video asset is invalid")
        video = self._asset(assets, media_refs[0])
        if not video.mime_type.startswith("video/"):
            raise MediaProviderRejected("asset_type_invalid", "YouTube media asset must be a video")
        visibility = str(payload.get("visibility") or "private").strip().lower()
        if visibility not in {"private", "unlisted", "public"}:
            raise MediaProviderRejected("payload_invalid", "YouTube visibility is invalid")
        scheduled_at = str(payload.get("scheduled_at") or "").strip() or None
        status: dict[str, Any] = {"privacyStatus": "private" if scheduled_at else visibility}
        if scheduled_at:
            status["publishAt"] = scheduled_at
        audience = str(payload.get("audience") or "").strip().lower()
        if audience in {"made_for_kids", "children", "kids"}:
            status["selfDeclaredMadeForKids"] = True
        elif audience in {"not_made_for_kids", "general", "adult"}:
            status["selfDeclaredMadeForKids"] = False
        disclosure = str(payload.get("disclosure") or "").strip().lower()
        if disclosure in {"synthetic", "altered", "ai", "true", "yes"}:
            status["containsSyntheticMedia"] = True
        metadata = {
            "snippet": {
                "title": str(payload.get("title") or "").strip(),
                "description": str(payload.get("description") or ""),
                "tags": list(payload.get("tags") or []),
                "categoryId": "22",
            },
            "status": status,
        }
        if not metadata["snippet"]["title"]:
            raise MediaProviderRejected("payload_invalid", "YouTube title is empty")
        thumbnail_refs = payload.get("thumbnail") or []
        thumbnail = self._asset(assets, thumbnail_refs[0]) if thumbnail_refs else None
        if thumbnail and (thumbnail.mime_type not in {"image/jpeg", "image/png"}
                          or thumbnail.size_bytes > YOUTUBE_MAX_THUMBNAIL_BYTES):
            raise MediaProviderRejected("thumbnail_invalid", "YouTube thumbnail must be JPEG/PNG and at most 2 MiB")
        fresh_session = False
        if state:
            if state.get("asset_sha256") != video.sha256 or state.get("size_bytes") != video.size_bytes:
                raise MediaProviderExecutionError("YouTube upload asset changed")
            if not state.get("session_url") and not state.get("video_id"):
                return self._unresolved(attempt_id, "session_receipt_unknown")
        else:
            state.update(phase="initializing", asset_sha256=video.sha256, size_bytes=video.size_bytes,
                         offset=0, thumbnail_required=bool(thumbnail), thumbnail_done=False)
            await journal.save(state)
            initiation = await client.post(
                f"{_YOUTUBE_API}/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"Authorization": f"Bearer {access_token}",
                         "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Length": str(video.size_bytes),
                         "X-Upload-Content-Type": video.mime_type}, json=metadata)
            _check_provider_response(initiation, operation="YouTube resumable upload initialization")
            state.update(session_url=_youtube_session_url(initiation.headers.get("Location") or initiation.headers.get("location")),
                         phase="uploading")
            await journal.save(state)
            # Only the fresh, committed session may skip the initial status probe.
            fresh_session = True
        if not state.get("video_id"):
            result = await self._upload_youtube(client=client, access_token=access_token,
                video=video, state=state, journal=journal, probe_first=not fresh_session)
            if not result:
                return self._unresolved(attempt_id, "upload_incomplete")
        if state.get("upload_status") in {"failed", "rejected", "deleted"}:
            return self._unresolved(attempt_id, "video_upload_failed")
        if thumbnail and not state.get("thumbnail_done"):
            state["phase"] = "thumbnail_pending"
            await journal.save(state)
            video_id = _remote_id(state["video_id"], "youtube")
            thumb_response = await client.post(f"{_YOUTUBE_API}/upload/youtube/v3/thumbnails/set",
                params={"videoId": video_id, "uploadType": "media"},
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": thumbnail.mime_type,
                         "Content-Length": str(thumbnail.size_bytes)},
                content=await asyncio.to_thread(thumbnail.path.read_bytes))
            _check_provider_response(thumb_response, operation="YouTube thumbnail upload")
            state["thumbnail_done"] = True
            await journal.save(state)
        if not state.get("privacy_status"):
            return await self._recover_youtube(client=client, state=state, journal=journal,
                access_token=access_token, attempt_id=attempt_id)
        state["phase"] = "complete"
        await journal.save(state)
        return self._receipt("youtube", state["video_id"], attempt_id,
                             "scheduled" if state.get("privacy_status") == "private" and state.get("publish_at") else state["privacy_status"])

    @staticmethod
    def _upload_offset(response: Any, total: int) -> int:
        value = response.headers.get("Range") or response.headers.get("range")
        if value is None:
            return 0
        match = re.fullmatch(r"bytes=0-([0-9]+)", str(value))
        if not match or not 0 <= int(match[1]) < total:
            raise MediaProviderExecutionError("YouTube upload range is invalid")
        return int(match[1]) + 1

    async def _save_video_receipt(self, body: Mapping[str, Any], state: dict[str, Any], journal: Any) -> None:
        video_id = _remote_id(body.get("id"), "youtube")
        if state.get("video_id") and state["video_id"] != video_id:
            raise MediaProviderExecutionError("YouTube video receipt changed")
        status = body.get("status") if isinstance(body.get("status"), Mapping) else {}
        privacy = status.get("privacyStatus")
        if privacy is not None and privacy not in {"private", "unlisted", "public"}:
            raise MediaProviderExecutionError("YouTube privacy receipt is invalid")
        state.update(video_id=video_id, privacy_status=privacy, publish_at=status.get("publishAt"),
                     upload_status=status.get("uploadStatus"), phase="uploaded")
        await journal.save(state)

    async def _youtube_probe(self, *, client: Any, access_token: str, state: dict[str, Any], journal: Any) -> bool:
        response = await client.put(_youtube_session_url(state["session_url"]),
            headers={"Authorization": f"Bearer {access_token}", "Content-Length": "0",
                     "Content-Range": f"bytes */{state['size_bytes']}"}, content=b"")
        if _response_status(response) == 308:
            state["offset"] = self._upload_offset(response, state["size_bytes"])
            await journal.save(state)
            return False
        _check_provider_response(response, operation="YouTube upload status")
        await self._save_video_receipt(_safe_response_json(response), state, journal)
        return True

    async def _upload_youtube(self, *, client: Any, access_token: str, video: ResolvedMediaAsset,
                              state: dict[str, Any], journal: Any, probe_first: bool) -> bool:
        if probe_first and await self._youtube_probe(client=client, access_token=access_token, state=state, journal=journal):
            return True
        recoveries = 0
        while state["offset"] < video.size_bytes:
            offset = state["offset"]
            length = min(YOUTUBE_CHUNK_BYTES, video.size_bytes - offset)
            try:
                response = await client.put(_youtube_session_url(state["session_url"]),
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": video.mime_type,
                             "Content-Length": str(length),
                             "Content-Range": f"bytes {offset}-{offset + length - 1}/{video.size_bytes}"},
                    content=_file_chunks(video.path, offset=offset, length=length))
                if _response_status(response) == 308:
                    received = self._upload_offset(response, video.size_bytes)
                    if received <= offset or received > offset + length:
                        raise MediaProviderExecutionError("YouTube upload made no valid progress")
                    state["offset"] = received
                    await journal.save(state)
                    continue
                _check_provider_response(response, operation="YouTube video upload")
                await self._save_video_receipt(_safe_response_json(response), state, journal)
                return True
            except (httpx.HTTPError, MediaProviderExecutionError) as exc:
                if isinstance(exc, MediaProviderRejected) or recoveries >= YOUTUBE_MAX_RECOVERY_PROBES:
                    raise
                recoveries += 1
                await self._sleep(min(2 ** (recoveries - 1), 4))
                # Never assume how many bytes arrived: ask the same session.
                if await self._youtube_probe(client=client, access_token=access_token, state=state, journal=journal):
                    return True
        return await self._youtube_probe(client=client, access_token=access_token, state=state, journal=journal)

    async def _recover_youtube(self, *, client: Any, state: dict[str, Any], journal: Any,
                               access_token: str, attempt_id: str) -> dict[str, Any]:
        if not state.get("video_id"):
            if not state.get("session_url"):
                return self._unresolved(attempt_id, "session_receipt_unknown")
            if not await self._youtube_probe(client=client, access_token=access_token, state=state, journal=journal):
                return self._unresolved(attempt_id, "upload_incomplete")
        video_id = _remote_id(state["video_id"], "youtube")
        response = await client.get(f"{_YOUTUBE_API}/youtube/v3/videos",
            params={"part": "status", "id": video_id}, headers={"Authorization": f"Bearer {access_token}"})
        _check_provider_response(response, operation="YouTube video lookup")
        items = _safe_response_json(response).get("items")
        if not isinstance(items, list) or len(items) != 1 or items[0].get("id") != video_id:
            return self._unresolved(attempt_id, "receipt_unknown")
        await self._save_video_receipt(items[0], state, journal)
        if state.get("upload_status") in {"failed", "rejected", "deleted"}:
            return self._unresolved(attempt_id, "video_upload_failed")
        if state.get("thumbnail_required") and not state.get("thumbnail_done"):
            return self._unresolved(attempt_id, "thumbnail_pending")
        if not state.get("privacy_status"):
            return self._unresolved(attempt_id, "visibility_unknown")
        state["phase"] = "complete"
        await journal.save(state)
        return self._receipt("youtube", video_id, attempt_id,
                             "scheduled" if state.get("privacy_status") == "private" and state.get("publish_at") else state["privacy_status"])



__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "MediaProviderExecutionError",
    "MediaProviderExecutionRouter",
    "MediaProviderPartial",
    "MediaProviderRejected",
    "PROVIDER_ADAPTER_VERSION",
    "ResolvedMediaAsset",
]
