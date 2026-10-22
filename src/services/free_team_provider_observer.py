"""OpenRouter無料モデル日次リクエスト枠のProvider実測観測。

``GET /api/v1/key`` の ``data.free_model_daily_requests{used,limit,remaining}``
（UTC日次）を明示的なrefreshでのみ取得する。観測値は表示専用で、cap・DB・
routing・課金経路には一切影響させない。``is_free_tier`` は実Tier判定に使わない。

状態は ``ObservationStore`` が登録App単位で保持し、現在のcredential binding
（profile・有効状態・キーのprocess内限定fingerprint）と一致する場合だけ返す。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import math
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any, Callable, Mapping

KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
STALE_AFTER_SECONDS = 15 * 60
MAX_SAFE_INTEGER = 2**53 - 1

FetchJson = Callable[[str, str, float], Any]

_KNOWN_FAILURES = frozenset(
    {"missing_key", "unsupported_endpoint", "timeout", "network_error", "invalid_response", "redirect_rejected"}
)
_HTTP_FAILURE = re.compile(r"^http_[1-5][0-9]{2}$")


class ObservationError(RuntimeError):
    """``str(exc)`` は固定コードのみ。秘密・応答本文を含めない。"""


def safe_failure_code(exc: BaseException) -> str:
    """未知の文言は外へ出さず ``network_error`` に正規化する。"""

    text = str(exc)
    if isinstance(exc, ObservationError) and (text in _KNOWN_FAILURES or _HTTP_FAILURE.match(text)):
        return text
    return "network_error"


def is_official_endpoint(base_url: str | None) -> bool:
    """正規の公式OpenRouter origin/base path（https://openrouter.ai/api/v1）だけを許可する。

    空・別host・別path・別port・userinfo・query・fragmentは非対応。互換endpoint用の
    キーを公式OpenRouterへ送らないための境界で、空値も安全側（非対応）に倒す。
    """

    try:
        parsed = urlsplit(str(base_url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "") == "openrouter.ai"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/") == "/api/v1"
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Authorizationを別originへ転送しないよう、全redirectを拒否する。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get_json(url: str, api_key: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ObservationError("redirect_rejected") from None
        raise ObservationError(f"http_{exc.code}") from None
    except (socket.timeout, TimeoutError):
        raise ObservationError("timeout") from None
    except urllib.error.URLError as exc:
        raise ObservationError(
            "timeout" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "network_error"
        ) from None
    except (ValueError, http.client.HTTPException):
        raise ObservationError("invalid_response") from None


def default_fetch(url: str, api_key: str, timeout: float) -> Any:
    if url != KEY_ENDPOINT:
        raise ObservationError("network_error")
    return _get_json(url, api_key, timeout)


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationError("invalid_response")
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > MAX_SAFE_INTEGER or value != int(value):
            raise ObservationError("invalid_response")
    if value < 0 or value > MAX_SAFE_INTEGER:
        raise ObservationError("invalid_response")
    return int(value)


def parse_key_payload(payload: Any) -> dict[str, int]:
    """used/limit/remaining を検証する。

    remaining は ``max(0, limit - used)`` と一致しなければ不正。枠を使い切った状態
    （used >= limit）は remaining == 0 の場合のみ有効とし、不一致は資格や残量を
    推測せず不正応答として扱う。
    """

    data = payload.get("data") if isinstance(payload, Mapping) else None
    block = data.get("free_model_daily_requests") if isinstance(data, Mapping) else None
    if not isinstance(block, Mapping):
        raise ObservationError("invalid_response")
    used, limit, remaining = (_count(block.get(key)) for key in ("used", "limit", "remaining"))
    if limit < 1 or remaining != max(0, limit - used):
        raise ObservationError("invalid_response")
    return {"used": used, "limit": limit, "remaining": remaining}


def fetch_observation(
    api_key: str, *, fetch: FetchJson | None = None, timeout: float = 10.0
) -> tuple[dict[str, int] | None, str]:
    """(values, failure)。失敗時は数値を持たず、failureは固定コードのみ。"""

    if not str(api_key or "").strip():
        return None, "missing_key"
    try:
        return parse_key_payload((fetch or default_fetch)(KEY_ENDPOINT, api_key, timeout)), ""
    except Exception as exc:  # noqa: BLE001 - 例外文字列に秘密が混ざり得るため分類のみ使う
        return None, safe_failure_code(exc)


def _empty_status() -> dict[str, Any]:
    return {
        "source": "openrouter_key_api",
        "status": "unverified",
        "used": None,
        "limit": None,
        "remaining": None,
        "observed_at": None,
        "failure": "",
    }


@dataclass
class _Entry:
    binding: str
    observed_at: datetime
    values: dict[str, int] | None
    failure: str


class ObservationStore:
    """登録App単位の観測保持。bindingが変われば古い観測は返さない。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._salt = os.urandom(16)
        self._issued = 0
        self._committed = 0
        self._entry: _Entry | None = None

    @staticmethod
    def unusable_reason(enabled: bool, api_key: str, base_url: str | None) -> str:
        """送信不可の理由（固定コード）。送信可能なら空文字。"""

        if not enabled or not str(api_key or "").strip():
            return "missing_key"
        return "" if is_official_endpoint(base_url) else "unsupported_endpoint"

    def binding(
        self, profile_id: str, enabled: bool, api_key: str, base_url: str | None
    ) -> str | None:
        """現在のcredential revision（endpoint identity込み）。送信不可はNone。値は外部へ出さない。"""

        if self.unusable_reason(enabled, api_key, base_url):
            return None
        key = str(api_key).strip()
        endpoint = str(base_url).strip().rstrip("/")
        digest = hmac.new(
            self._salt, f"{profile_id}\0{endpoint}\0{key}".encode(), hashlib.sha256
        )
        return digest.hexdigest()

    def begin(self) -> int:
        with self._lock:
            self._issued += 1
            return self._issued

    def commit(
        self,
        ticket: int,
        binding: str | None,
        values: dict[str, int] | None,
        failure: str,
        observed_at: datetime,
    ) -> bool:
        """より新しいticketが確定済みなら捨てる。bindingなしは観測を破棄する。"""

        with self._lock:
            if ticket <= self._committed:
                return False
            self._committed = ticket
            self._entry = (
                None if binding is None else _Entry(binding, observed_at, values, failure)
            )
            return True

    def status(self, binding: str | None, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or _utcnow()
        with self._lock:
            entry = self._entry
        base = _empty_status()
        if entry is None or binding is None or not hmac.compare_digest(entry.binding, binding):
            return base
        base["observed_at"] = entry.observed_at.isoformat()
        if entry.values is None:
            return {**base, "status": "failed", "failure": entry.failure}
        observed = entry.observed_at.astimezone(timezone.utc)
        stale = (
            observed.date() != current.astimezone(timezone.utc).date()
            or (current - entry.observed_at).total_seconds() > STALE_AFTER_SECONDS
        )
        return {**base, **entry.values, "status": "stale" if stale else "observed"}

    def refresh(
        self,
        binding: str | None,
        api_key: str,
        *,
        fetch: FetchJson | None = None,
        now: datetime | None = None,
        timeout: float = 10.0,
        reason: str = "",
    ) -> dict[str, Any]:
        ticket = self.begin()
        # 無効・未設定・非対応endpointのcredentialではProviderへ送信しない。
        if binding is None or reason:
            reason = reason or "missing_key"
            values, failure = None, reason
        else:
            values, failure = fetch_observation(api_key, fetch=fetch, timeout=timeout)
        observed_at = now or _utcnow()
        if self.commit(ticket, binding, values, failure, observed_at):
            if binding is None:
                return {**_empty_status(), "status": "failed", "failure": failure or "missing_key",
                        "observed_at": observed_at.isoformat()}
            return self.status(binding, now=observed_at)
        # 新しい取得が先に確定済み。古い結果は適用せず最新の状態を返す。
        return self.status(binding, now=now)
