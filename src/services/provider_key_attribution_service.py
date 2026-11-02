"""Body-free attribution and health receipts for OpenAI provider keys.

This ledger deliberately stores no API key, authorization header, request or
response body, or provider exception message. Runtime call hooks enqueue only
masked aliases, peppered local digests, consumer ids, timestamps and outcome
classes. Ledger writes run on a bounded daemon queue and are best effort.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock, Timeout as FileLockTimeout

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_LEDGER_BYTES = 256 * 1024
MAX_KEYS = 128
MAX_CONSUMER_ROWS = 512
LOCAL_DIGEST_HEX = 24
LEGACY_RECENT_DAYS = 30
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_LOCAL_DIGEST = re.compile(rf"[0-9a-f]{{{LOCAL_DIGEST_HEX}}}\Z")
_CONSUMER_ID = re.compile(r"[a-z0-9][a-z0-9:_-]{0,95}\Z")
_FAILURE_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_KEY_PREFIXES = (
    ("sk-proj-", "project"),
    ("sk-svcacct-", "project"),
    ("sk-admin-", "project"),
    ("sk-org-", "project"),
    ("sk-", "legacy"),
)


class AttributionStoreError(RuntimeError):
    """Safe, fixed-code error raised when the local ledger cannot be used."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderKeyIdentity:
    local_digest: str
    masked_alias: str
    kind: str

    def as_dict(self) -> dict[str, str]:
        return {
            "local_digest": self.local_digest,
            "masked_alias": self.masked_alias,
            "kind": self.kind,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mask_and_kind(api_key: str) -> tuple[str, str]:
    prefix, kind = "unknown-", "unknown"
    for candidate, candidate_kind in _KEY_PREFIXES:
        if api_key.startswith(candidate):
            prefix, kind = candidate, candidate_kind
            break
    # Do not reveal a short test or malformed value in full.
    suffix = api_key[-4:] if len(api_key) >= len(prefix) + 4 else "****"
    return f"{prefix}…{suffix}", kind


def _default_ledger_path() -> Path:
    from ..runtime_release_contract import resolve_runtime_paths

    project_root = Path(__file__).resolve().parents[2]
    paths = resolve_runtime_paths(os.environ, project_root)
    return Path(str(paths["data_root"])) / "provider_key_attribution.json"


class ProviderKeyAttributionStore:
    """Small bounded JSON ledger using AoiTalk's canonical data root."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else _default_ledger_path()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._pepper: bytes | None = None
        self._pepper_lock = threading.Lock()

    def _assert_safe_path(self, *, create_parent: bool) -> None:
        from .storage_roots import StorageError, assert_no_links

        try:
            assert_no_links(self.path.parent, allow_missing=True)
            if create_parent:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_no_links(self.path.parent)
            assert_no_links(self.lock_path, allow_missing=True)
            assert_no_links(self.path, allow_missing=True)
        except (OSError, StorageError) as exc:
            raise AttributionStoreError("ledger_path_unavailable") from exc

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "local_pepper": secrets.token_hex(32),
            "keys": [],
        }

    def _load_locked(self, *, create: bool) -> dict[str, Any]:
        self._assert_safe_path(create_parent=create)
        if not self.path.exists():
            if not create:
                return self._empty_document()
            document = self._empty_document()
            self._write_locked(document)
            return document
        try:
            size = self.path.stat().st_size
            if size > MAX_LEDGER_BYTES:
                raise AttributionStoreError("ledger_too_large")
            raw = self.path.read_bytes()
            if len(raw) > MAX_LEDGER_BYTES:
                raise AttributionStoreError("ledger_too_large")
            document = json.loads(raw.decode("utf-8"))
        except AttributionStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AttributionStoreError("ledger_unreadable") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != SCHEMA_VERSION
            or not isinstance(document.get("keys"), list)
            or not isinstance(document.get("local_pepper"), str)
            or not _HEX_64.fullmatch(document["local_pepper"])
            or len(document["keys"]) > MAX_KEYS
        ):
            raise AttributionStoreError("ledger_schema_invalid")
        return document

    def _write_locked(self, document: dict[str, Any]) -> None:
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_LEDGER_BYTES:
            raise AttributionStoreError("ledger_too_large")
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{self.path.name}.", suffix=".tmp",
                dir=self.path.parent, delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except AttributionStoreError:
            raise
        except OSError as exc:
            raise AttributionStoreError("ledger_write_failed") from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    @contextmanager
    def _locked(self, *, create: bool, timeout: float = 0.25):
        self._assert_safe_path(create_parent=create)
        try:
            lock = FileLock(str(self.lock_path), timeout=timeout, thread_local=False)
            with lock:
                yield self._load_locked(create=create)
        except FileLockTimeout as exc:
            raise AttributionStoreError("ledger_busy") from exc

    def identity_for_key(self, api_key: Any) -> ProviderKeyIdentity | None:
        """Compute a peppered digest and mask; the input is never persisted."""
        if not isinstance(api_key, str) or not api_key or len(api_key) > 4096:
            return None
        with self._pepper_lock:
            if self._pepper is None:
                with self._locked(create=True) as document:
                    self._pepper = bytes.fromhex(document["local_pepper"])
            pepper = self._pepper
        if pepper is None:
            return None
        digest = hmac.new(pepper, api_key.encode("utf-8"), hashlib.sha256).hexdigest()[:LOCAL_DIGEST_HEX]
        masked_alias, kind = _mask_and_kind(api_key)
        return ProviderKeyIdentity(digest, masked_alias, kind)

    def record_event(
        self,
        identity: ProviderKeyIdentity | dict[str, str],
        consumer_id: str,
        *,
        success: bool,
        recorded_at: str,
        status_code: int | None = None,
        failure_class: str | None = None,
    ) -> None:
        data = identity.as_dict() if isinstance(identity, ProviderKeyIdentity) else dict(identity)
        if (
            not _LOCAL_DIGEST.fullmatch(str(data.get("local_digest", "")))
            or data.get("kind") not in {"legacy", "project", "unknown"}
            or not isinstance(data.get("masked_alias"), str)
            or len(data["masked_alias"]) > 80
            or not isinstance(consumer_id, str)
            or not _CONSUMER_ID.fullmatch(consumer_id)
        ):
            return
        event_at = _timestamp(_parse_timestamp(recorded_at))
        with self._locked(create=True, timeout=0.25) as document:
            key = next((item for item in document["keys"] if item.get("local_digest") == data["local_digest"]), None)
            if key is None:
                if len(document["keys"]) >= MAX_KEYS:
                    return
                key = {
                    **data,
                    "provider_key_id_digest": None,
                    "state": "unknown",
                    "state_source": "unobserved",
                    "state_attested_at": None,
                    "state_conflict": False,
                    "health": [],
                }
                document["keys"].append(key)
            health = next((item for item in key["health"] if item.get("consumer_id") == consumer_id), None)
            if health is None:
                if sum(len(item.get("health", [])) for item in document["keys"]) >= MAX_CONSUMER_ROWS:
                    return
                health = {
                    "consumer_id": consumer_id,
                    "last_success_at": None,
                    "last_failure_at": None,
                    "last_failure_class": None,
                    "success_count": 0,
                    "failure_count": 0,
                }
                key["health"].append(health)
            if success:
                health["last_success_at"] = event_at
                health["success_count"] = min(int(health["success_count"]) + 1, 2**31 - 1)
                attested_at = _parse_timestamp(key.get("state_attested_at"))
                observed_at = _parse_timestamp(event_at)
                if (
                    key.get("state_source") == "owner_attested"
                    and key.get("state") == "disabled"
                    and attested_at is not None
                    and observed_at is not None
                    and observed_at > attested_at
                ):
                    key["state_conflict"] = True
                else:
                    if key.get("state_source") != "owner_attested":
                        key["state"] = "active"
                        key["state_source"] = "observed_success"
            else:
                health["last_failure_at"] = event_at
                health["failure_count"] = min(int(health["failure_count"]) + 1, 2**31 - 1)
                if status_code is not None and 100 <= status_code <= 599:
                    health["last_failure_class"] = f"http_{status_code // 100}xx"
                elif isinstance(failure_class, str) and _FAILURE_LABEL.fullmatch(failure_class):
                    health["last_failure_class"] = failure_class
                else:
                    health["last_failure_class"] = "provider_error"
            self._write_locked(document)

    def attest(
        self,
        local_digest: str,
        provider_key_id_digest: str,
        state: str,
        *,
        attested_at: str | None = None,
    ) -> None:
        if not _LOCAL_DIGEST.fullmatch(local_digest):
            raise ValueError("local_digest_invalid")
        if not _HEX_64.fullmatch(provider_key_id_digest):
            raise ValueError("provider_key_id_digest_invalid")
        if state not in {"active", "disabled", "unknown"}:
            raise ValueError("state_invalid")
        with self._locked(create=False) as document:
            key = next((item for item in document["keys"] if item.get("local_digest") == local_digest), None)
            if key is None:
                raise KeyError("provider_key_not_found")
            key["provider_key_id_digest"] = provider_key_id_digest
            key["state"] = state
            key["state_source"] = "owner_attested"
            key["state_attested_at"] = _timestamp(_parse_timestamp(attested_at)) if attested_at else _timestamp()
            key["state_conflict"] = False
            self._write_locked(document)

    def read_projection(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or _utc_now()
        if not self.path.exists():
            return {"keys": []}
        with self._locked(create=False, timeout=1.0) as document:
            duplicates = Counter(
                item.get("provider_key_id_digest")
                for item in document["keys"]
                if isinstance(item.get("provider_key_id_digest"), str)
            )
            projected = []
            for item in document["keys"]:
                health_rows = []
                for health in item.get("health", []):
                    last_success = health.get("last_success_at")
                    last_failure = health.get("last_failure_at")
                    duplicate_attestation = bool(
                        item.get("provider_key_id_digest")
                        and duplicates[item["provider_key_id_digest"]] > 1
                    )
                    conflict = bool(item.get("state_conflict") or duplicate_attestation)
                    attribution = "conflict" if conflict else "confirmed" if int(health.get("success_count", 0)) > 0 else "unknown"
                    last_success_dt = _parse_timestamp(last_success)
                    legacy_recent = bool(
                        item.get("kind") == "legacy"
                        and last_success_dt is not None
                        and last_success_dt >= now.astimezone(timezone.utc) - timedelta(days=LEGACY_RECENT_DAYS)
                    )
                    health_rows.append({
                        "consumer_id": health.get("consumer_id", "unknown_consumer"),
                        "last_success_at": last_success,
                        "last_failure_at": last_failure,
                        "last_failure_class": health.get("last_failure_class"),
                        "success_count": int(health.get("success_count", 0)),
                        "failure_count": int(health.get("failure_count", 0)),
                        "attribution": attribution,
                        "legacy_still_in_use": legacy_recent,
                    })
                projected.append({
                    "masked_alias": item["masked_alias"],
                    "local_digest": item["local_digest"],
                    "kind": item["kind"],
                    "state": item["state"],
                    "state_source": item.get("state_source", "unknown"),
                    "provider_key_id_digest": item.get("provider_key_id_digest"),
                    "health": sorted(health_rows, key=lambda row: row["consumer_id"]),
                })
            return {"keys": sorted(projected, key=lambda row: row["local_digest"])}


_DEFAULT_STORE: ProviderKeyAttributionStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()
_EVENT_QUEUE: queue.Queue[tuple[ProviderKeyAttributionStore, ProviderKeyIdentity, str, bool, str, int | None, str | None] | None] = queue.Queue(maxsize=256)
_WRITER_STARTED = False
_WRITER_LOCK = threading.Lock()


def get_provider_key_attribution_store() -> ProviderKeyAttributionStore:
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = ProviderKeyAttributionStore()
        return _DEFAULT_STORE


def prepare_provider_key_identity(api_key: Any) -> dict[str, str] | None:
    """Return only non-secret key metadata for attachment to an SDK client."""
    try:
        identity = get_provider_key_attribution_store().identity_for_key(api_key)
        return identity.as_dict() if identity else None
    except Exception:
        return None


def _failure_class(exc: BaseException) -> tuple[int | None, str | None]:
    status = getattr(exc, "status_code", None)
    if type(status) is not int:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        return status, None
    name = type(exc).__name__
    return None, name if _FAILURE_LABEL.fullmatch(name) else "provider_error"


def _start_writer() -> None:
    global _WRITER_STARTED
    with _WRITER_LOCK:
        if _WRITER_STARTED:
            return
        thread = threading.Thread(target=_writer_loop, name="provider-key-attribution", daemon=True)
        thread.start()
        _WRITER_STARTED = True


def _writer_loop() -> None:
    while True:
        item = _EVENT_QUEUE.get()
        try:
            if item is None:
                return
            store, identity, consumer_id, success, recorded_at, status_code, failure_class = item
            try:
                store.record_event(
                    identity, consumer_id, success=success, recorded_at=recorded_at,
                    status_code=status_code, failure_class=failure_class,
                )
            except Exception:
                # Intentionally omit exception text and traceback from logs.
                logger.debug("provider key attribution write skipped")
        finally:
            _EVENT_QUEUE.task_done()


def enqueue_provider_key_event(
    identity: Any,
    consumer_id: str,
    *,
    success: bool,
    exc: BaseException | None = None,
    status_code: int | None = None,
    failure_class: str | None = None,
    store: ProviderKeyAttributionStore | None = None,
) -> bool:
    """Queue an outcome using a precomputed, non-secret key identity."""
    try:
        if isinstance(identity, dict):
            identity = ProviderKeyIdentity(
                str(identity["local_digest"]), str(identity["masked_alias"]), str(identity["kind"])
            )
        if not isinstance(identity, ProviderKeyIdentity) or not _CONSUMER_ID.fullmatch(consumer_id):
            return False
        if exc is not None:
            status_code, failure_class = _failure_class(exc)
            success = False
        _start_writer()
        _EVENT_QUEUE.put_nowait((
            store or get_provider_key_attribution_store(), identity, consumer_id,
            bool(success), _timestamp(), status_code, failure_class,
        ))
        return True
    except Exception:
        return False


async def call_with_provider_key_attribution(
    call: Callable[[], Any],
    *,
    identity: Any,
    consumer_id: str,
    store: ProviderKeyAttributionStore | None = None,
    result_is_success: Callable[[Any], bool] | None = None,
    result_failure_class: Callable[[Any], str | None] | None = None,
) -> Any:
    """Run an async/sync sender and record its outcome without changing it."""
    try:
        result = call()
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            result = await result
    except Exception as exc:
        try:
            enqueue_provider_key_event(identity, consumer_id, success=False, exc=exc, store=store)
        except Exception:
            pass
        raise
    try:
        success = result_is_success(result) if result_is_success else True
        failure_class = result_failure_class(result) if not success and result_failure_class else None
        enqueue_provider_key_event(
            identity, consumer_id, success=bool(success), failure_class=failure_class, store=store,
        )
    except Exception:
        pass
    return result


def call_with_provider_key_attribution_sync(
    call: Callable[[], Any],
    *,
    identity: Any,
    consumer_id: str,
    store: ProviderKeyAttributionStore | None = None,
    result_is_success: Callable[[Any], bool] | None = None,
    result_failure_class: Callable[[Any], str | None] | None = None,
) -> Any:
    """Synchronous companion for direct OpenAI SDK sender functions."""
    try:
        result = call()
    except Exception as exc:
        try:
            enqueue_provider_key_event(identity, consumer_id, success=False, exc=exc, store=store)
        except Exception:
            pass
        raise
    try:
        success = result_is_success(result) if result_is_success else True
        failure_class = result_failure_class(result) if not success and result_failure_class else None
        enqueue_provider_key_event(
            identity, consumer_id, success=bool(success), failure_class=failure_class, store=store,
        )
    except Exception:
        pass
    return result


def flush_provider_key_events(timeout: float = 2.0) -> bool:
    """Test/maintenance seam; production request paths never wait on the queue."""
    deadline = time.monotonic() + max(0.0, timeout)
    while _EVENT_QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.005)
    return _EVENT_QUEUE.unfinished_tasks == 0


def consumer_model_suffix(model: str) -> str:
    """Stable short model labels for fixed consumer ids, never user payloads."""
    normalized = str(model or "").lower()
    for suffix in ("sol", "luna", "terra"):
        if normalized.endswith("-" + suffix):
            return suffix
    return "other"
