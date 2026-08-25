#!/usr/bin/env python3
"""AoiTalk の DDNS Now (f5.si) A レコード自動更新。

このスクリプトは、ルーターの WAN IPv4 が変わったときだけ DDNS Now の
``update.php`` を呼び出します。認証情報はコマンドライン引数に受け取らず、
プロジェクトルートの ``.env``（または環境変数）から読み込みます。

外部パッケージに依存しないよう、HTTP/DNS/IPv4 検証は標準ライブラリだけで
実装しています。タスクスケジューラから定期実行することを想定しています。
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_UPDATE_URL = "https://f5.si/update.php"

# hosts ファイルやローカル DNS キャッシュを経由せず、公開 DNS-over-HTTPS を利用する。
# 2 つのサービスが返す A 集合を比較し、片方の一時障害にも対応する。
DEFAULT_DNS_OVER_HTTPS_ENDPOINTS: tuple[str, ...] = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
ALLOWED_PUBLIC_IP_HOSTS = frozenset(
    {"api.ipify.org", "ipv4.icanhazip.com", "v4.ident.me"}
)
ALLOWED_DOH_HOSTS = frozenset({"cloudflare-dns.com", "dns.google"})

# 1 つのサービス障害で誤った A レコードを設定しないよう、独立した HTTPS
# エンドポイントを複数利用する。最低 2 つが同じ IPv4 を返したときに採用し、
# 片方しか応答しない場合はフォールバックとして 1 つの値を採用する。
DEFAULT_PUBLIC_IP_ENDPOINTS: tuple[str, ...] = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://v4.ident.me",
)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25


class ExitCode:
    """CLI の終了コード（タスクスケジューラのログから原因を判別しやすくする）。"""

    SUCCESS = 0
    CONFIGURATION_ERROR = 2
    PUBLIC_IP_ERROR = 3
    DNS_ERROR = 4
    API_ERROR = 5
    INTERNAL_ERROR = 6


class DdnsError(RuntimeError):
    """DDNS 更新処理で利用者に提示してよいエラー。"""


class ConfigurationError(DdnsError):
    """.env または引数の設定が不正。"""


class PublicIpError(DdnsError):
    """公開 IPv4 の取得に失敗。"""


class DnsLookupError(DdnsError):
    """現在の DNS A レコード取得に失敗。"""


class DdnsApiError(DdnsError):
    """DDNS Now API の更新に失敗。"""


@dataclasses.dataclass(frozen=True)
class DdnsConfig:
    """更新処理に必要な値。

    ``password`` は API リクエスト生成にのみ使い、ログや CLI には出さない。
    """

    domain: str
    password: str
    dns_name: str | None = None
    update_url: str = DEFAULT_UPDATE_URL
    public_ip_endpoints: tuple[str, ...] = DEFAULT_PUBLIC_IP_ENDPOINTS
    dns_over_https_endpoints: tuple[str, ...] = DEFAULT_DNS_OVER_HTTPS_ENDPOINTS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    retries: int = DEFAULT_RETRIES
    dry_run: bool = False

    def __post_init__(self) -> None:
        domain = normalize_domain(self.domain)
        if not domain:
            raise ConfigurationError("DDNS_NOW_DOMAIN が未設定です")
        if not self.password.strip():
            raise ConfigurationError("DDNS_NOW_PASSWORD が未設定です")
        if self.update_url != DEFAULT_UPDATE_URL:
            raise ConfigurationError("DDNS API URL は https://f5.si/update.php 固定です")
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.timeout > 10:
            raise ConfigurationError("timeout は 0 より大きく 10 秒以下で指定してください")
        if self.retries < 1 or self.retries > 2:
            raise ConfigurationError("retries は 1 または 2 で指定してください")

        # dataclass frozen のため、正規化した値を書き戻す。
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "password", self.password.strip())
        expected_dns_name = f"{domain}.f5.si"
        dns_name = normalize_dns_name(self.dns_name) if self.dns_name else expected_dns_name
        if dns_name != expected_dns_name:
            raise ConfigurationError("dns_name は DDNS_NOW_DOMAIN.f5.si と一致させてください")
        object.__setattr__(self, "dns_name", dns_name)
        object.__setattr__(
            self,
            "public_ip_endpoints",
            tuple(endpoint.strip() for endpoint in self.public_ip_endpoints if endpoint.strip()),
        )
        object.__setattr__(
            self,
            "dns_over_https_endpoints",
            tuple(endpoint.strip() for endpoint in self.dns_over_https_endpoints if endpoint.strip()),
        )


def _log(
    logger: logging.Logger,
    level: int,
    message: str,
    event: str,
    **fields: Any,
) -> None:
    """構造化された日本語ログを 1 行出力する。"""

    payload: dict[str, Any] = {
        "event": event,
        "message": message,
        **fields,
    }
    # logger のハンドラに JSON 化を任せると caplog 等でも扱いやすい。
    logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


class _JsonLogFormatter(logging.Formatter):
    """通常の logging レコードを JSON の 1 行に整形する。"""

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        # _log は既に JSON を作っているので二重エスケープしない。
        if text.startswith("{"):
            return text
        return json.dumps(
            {
                "event": "log",
                "message": text,
                "level": record.levelname,
            },
            ensure_ascii=False,
        )


def configure_logging(level: str = "INFO") -> logging.Logger:
    """CLI 用ロガーを設定して返す。既存ハンドラは追加しない。"""

    logger = logging.getLogger("aoitalk.update_ddns")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonLogFormatter())
        logger.addHandler(handler)
    return logger


def mask_secret(secret: str) -> str:
    """ログ用の秘密マスク（秘密の一部も出さない）。"""

    return "***" if secret else ""


def _safe_error(error: BaseException, secret: str = "") -> str:
    """例外文から認証情報と URL クエリを除去する。"""

    text = str(error)
    if secret:
        text = text.replace(secret, "***")
        text = text.replace(urllib.parse.quote(secret, safe=""), "***")
    # urllib の例外がクエリ全体を含む場合に備え、password 値を消す。
    text = re.sub(r"([?&]password=)[^&\s]+", r"\1***", text, flags=re.IGNORECASE)
    return text[:500]


def redact_url(url: str, secret: str = "") -> str:
    """ログに記録してよい URL に変換する。``password`` を常にマスク。"""

    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        redacted = []
        for key, value in query:
            if key.lower() in {"password", "pass", "token", "api_token"}:
                value = mask_secret(value)
            elif secret and value == secret:
                value = mask_secret(value)
            redacted.append((key, value))
        new_query = urllib.parse.urlencode(redacted)
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
        )
    except ValueError:
        return "<URL>"


def read_dotenv(path: Path) -> dict[str, str]:
    """依存を増やさずに ``.env`` の ``KEY=VALUE`` を読み取る。

    既存の ``python-dotenv`` と同様に、環境変数を上書きしない方針は呼び出し側で
    適用する。値に含まれる ``#`` はコメント扱いせず、トークンを壊さない。
    """

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise ConfigurationError(f".env を読み込めません: {exc}") from exc

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _env_value(name: str, file_values: Mapping[str, str], environ: Mapping[str, str]) -> str:
    value = environ.get(name)
    if value is not None:
        return value
    return file_values.get(name, "")


def normalize_domain(domain: str) -> str:
    """DDNS Now のユーザー名部分を正規化・検証する。"""

    raw = domain.strip().lower().rstrip(".")
    if "." in raw and not raw.endswith(".f5.si"):
        raise ConfigurationError("DDNS ドメインは *.f5.si のみ指定できます")
    value = raw[: -len(".f5.si")] if raw.endswith(".f5.si") else raw
    # サブドメイン方式（sub.user）も許可するが、URL で意味を持つ文字は拒否。
    if not value or len(value) > 253 or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
        value,
    ):
        raise ConfigurationError("DDNS_NOW_DOMAIN は f5.si の有効なドメイン名で指定してください")
    return value


def normalize_dns_name(name: str) -> str:
    value = name.strip().lower().rstrip(".")
    if not value:
        raise ConfigurationError("DNS 名が空です")
    if not value.endswith(".f5.si"):
        value = f"{value}.f5.si"
    # ネットワーク API に渡す前に制御文字などを拒否する。
    if len(value) > 253 or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
        value,
    ):
        raise ConfigurationError("DNS 名が不正です")
    return value


def load_config(
    env_file: Path = DEFAULT_ENV_FILE,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    dry_run: bool = False,
) -> DdnsConfig:
    """``.env`` と環境変数から DDNS 設定を作る。環境変数を優先する。"""

    env = os.environ if environ is None else environ
    file_values = read_dotenv(env_file)
    domain = _env_value("DDNS_NOW_DOMAIN", file_values, env)
    password = _env_value("DDNS_NOW_PASSWORD", file_values, env)
    if not domain or not password:
        raise ConfigurationError("DDNS_NOW_DOMAIN / DDNS_NOW_PASSWORD が未設定です")
    return DdnsConfig(
        domain=domain,
        password=password,
        timeout=timeout,
        retries=retries,
        dry_run=dry_run,
    )


def validate_ipv4(value: str) -> str:
    """公開 IPv4 だけを受け入れ、表記を正規化して返す。

    ループバック、プライベート、予約済み、ドキュメント用アドレスを DDNS に
    書き込むと誤設定になるため、``ipaddress.is_global`` を必須とする。
    """

    candidate = value.strip()
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError(f"不正な IPv4 アドレスです: {candidate!r}") from exc
    if parsed.version != 4:
        raise ValueError(f"IPv4 ではありません: {candidate!r}")
    if not parsed.is_global:
        raise ValueError(f"公開 IPv4 (global) ではありません: {candidate!r}")
    return str(parsed)


def _read_response(response: Any, *, max_bytes: int | None = None) -> bytes:
    """``urlopen`` の実レスポンスとテスト用簡易レスポンスの双方に対応。"""

    try:
        try:
            body = response.read(max_bytes + 1) if max_bytes is not None else response.read()
        except TypeError:
            # テスト用の簡易レスポンスなど、引数を受け取らない read() に対応。
            body = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if max_bytes is not None and len(body) > max_bytes:
        raise ValueError("HTTP 応答が許容サイズを超えています")
    return body


def _verify_final_url(
    response: Any,
    *,
    allowed_hosts: frozenset[str],
    error_type: type[DdnsError],
    label: str,
    expected_path: str | None = None,
) -> None:
    """リダイレクト後も HTTPS と許可済みホストを維持しているか検証する。"""

    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return
    final_url = geturl()
    parsed = urllib.parse.urlsplit(final_url)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in allowed_hosts
        or (expected_path is not None and parsed.path.rstrip("/") != expected_path)
    ):
        raise error_type(f"{label} が許可外 URL へリダイレクトしました")


def _request_with_retries(
    request: urllib.request.Request,
    *,
    timeout: float,
    retries: int,
    opener: Callable[..., Any] = urllib.request.urlopen,
    secret: str = "",
    response_validator: Callable[[Any], None] | None = None,
    max_bytes: int | None = None,
) -> bytes:
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            response = opener(request, timeout=timeout)
            if response_validator:
                response_validator(response)
            return _read_response(response, max_bytes=max_bytes)
        except urllib.error.HTTPError as exc:
            # 認証/パラメータ不正など 4xx は再試行しても成功しない。
            last_error = exc
            if 400 <= exc.code < 500:
                raise DdnsApiError(
                    f"DDNS API が HTTP {exc.code} を返しました: {_safe_error(exc, secret)}"
                ) from exc
            if attempt == retries:
                break
        except (urllib.error.URLError, TimeoutError, OSError, DdnsError) as exc:
            last_error = exc
            if attempt == retries:
                break
        if attempt < retries:
            time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    if isinstance(last_error, DdnsError):
        raise last_error
    raise DdnsApiError(
        f"DDNS API への接続に失敗しました: {_safe_error(last_error or RuntimeError('unknown'), secret)}"
    ) from last_error


def _fetch_endpoint(
    endpoint: str,
    *,
    timeout: float,
    retries: int,
    opener: Callable[..., Any],
) -> str:
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "text/plain", "User-Agent": "AoiTalk-DDNS/1.0"},
        method="GET",
    )
    # 公開 IP サービスは API 認証情報を含まないため、共通 HTTP ヘルパーを使っても
    # 秘密が漏れることはない。HTTP 4xx は DdnsApiError ではなく候補失敗として扱う。
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            response = opener(request, timeout=timeout)
            _verify_final_url(
                response,
                allowed_hosts=ALLOWED_PUBLIC_IP_HOSTS,
                error_type=PublicIpError,
                label="公開 IPv4 サービス",
            )
            body = _read_response(response, max_bytes=1024)
            return body.decode("utf-8", errors="replace").strip()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, PublicIpError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise PublicIpError(
        f"公開 IPv4 エンドポイントに接続できません ({redact_url(endpoint)}): "
        f"{_safe_error(last_error or RuntimeError('unknown'))}"
    ) from last_error


def fetch_public_ipv4(
    endpoints: Sequence[str] = DEFAULT_PUBLIC_IP_ENDPOINTS,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    opener: Callable[..., Any] = urllib.request.urlopen,
    logger: logging.Logger | None = None,
) -> str:
    """複数の信頼済み HTTPS サービスから公開 IPv4 を取得する。

    2 つ以上のサービスが応答した場合は同じ値の合意を要求し、サービスの一部が
    障害でも誤更新しない。1 つだけ応答した場合は、完全な停止を避けるためその値を
    フォールバック採用する。
    """

    log = logger or logging.getLogger("aoitalk.update_ddns")
    candidates: list[str] = []
    failures: list[str] = []
    seen_urls: set[str] = set()
    for endpoint in endpoints:
        parsed = urllib.parse.urlsplit(endpoint.strip())
        canonical_url = urllib.parse.urlunsplit(
            (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.path or "/", parsed.query, "")
        )
        if canonical_url in seen_urls:
            continue
        seen_urls.add(canonical_url)
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in ALLOWED_PUBLIC_IP_HOSTS:
            failures.append(f"{redact_url(endpoint)}: 許可されていない HTTPS エンドポイントです")
            continue
        try:
            raw = _fetch_endpoint(canonical_url, timeout=timeout, retries=retries, opener=opener)
            candidates.append(validate_ipv4(raw))
        except (PublicIpError, ValueError) as exc:
            failures.append(f"{redact_url(canonical_url)}: {_safe_error(exc)}")

    if len(candidates) < 2:
        detail = "; ".join(failures[:3])
        raise PublicIpError(f"公開 IPv4 の合意に必要な応答数が不足しています{': ' + detail if detail else ''}")

    counts = collections.Counter(candidates)
    selected, occurrences = counts.most_common(1)[0]
    # 2/3 の strict majority（2 エンドポイント構成なら 2/2 一致）を要求する。
    if occurrences <= len(candidates) // 2:
        raise PublicIpError("複数の公開 IPv4 エンドポイントの応答が一致しません")
    _log(log, logging.INFO, "公開 IPv4 を取得しました", "public_ip_detected", ip=selected, sources=len(candidates), agreement=occurrences)
    return selected


def _query_doh_a_records(
    endpoint: str,
    hostname: str,
    *,
    timeout: float,
    retries: int,
    opener: Callable[..., Any],
) -> set[str]:
    """DNS-over-HTTPS JSON API から A レコードだけを抽出する。"""

    separator = "&" if "?" in endpoint else "?"
    query = urllib.parse.urlencode({"name": hostname, "type": "A"})
    url = f"{endpoint}{separator}{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/dns-json",
            "User-Agent": "AoiTalk-DDNS/1.0",
        },
        method="GET",
    )
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            response = opener(request, timeout=timeout)
            _verify_final_url(
                response,
                allowed_hosts=ALLOWED_DOH_HOSTS,
                error_type=DnsLookupError,
                label="公開 DoH",
            )
            body = _read_response(response, max_bytes=64 * 1024)
            payload = json.loads(body.decode("utf-8", errors="strict"))
            if not isinstance(payload, Mapping):
                raise DnsLookupError("DoH 応答が JSON オブジェクトではありません")
            status = payload.get("Status")
            if not isinstance(status, int):
                raise DnsLookupError("DoH 応答の Status がありません")
            if status not in (0, 3):
                raise DnsLookupError(f"DoH が DNS Status={status} を返しました")
            # Status=3 (NXDOMAIN) や Answer 欠落は「A レコードなし」として扱う。
            answer = payload.get("Answer", [])
            if answer is None:
                answer = []
            if not isinstance(answer, list):
                raise DnsLookupError("DoH の Answer が配列ではありません")
            addresses: set[str] = set()
            for record in answer:
                if not isinstance(record, Mapping) or record.get("type") != 1:
                    continue
                data = record.get("data")
                if not isinstance(data, str):
                    continue
                try:
                    addresses.add(validate_ipv4(data))
                except ValueError:
                    continue
            return addresses
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, DnsLookupError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise DnsLookupError(
        f"公開 DoH に接続できません ({redact_url(endpoint)}): {_safe_error(last_error or RuntimeError('unknown'))}"
    ) from last_error


def resolve_dns_a(
    hostname: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    endpoints: Sequence[str] = DEFAULT_DNS_OVER_HTTPS_ENDPOINTS,
    opener: Callable[..., Any] = urllib.request.urlopen,
    logger: logging.Logger | None = None,
) -> set[str]:
    """hosts/ローカルリゾルバを使わず、公開 DNS-over-HTTPS で A を取得する。

    Cloudflare と Google の結果が異なる場合は、どちらかに一時的な古いキャッシュが
    あっても更新を抑制できるよう集合の和を返す。両方が空集合を返した場合は A レコード
    が無いと判断し、呼び出し側が新規作成を行えるよう空集合を返す。
    """

    log = logger or logging.getLogger("aoitalk.update_ddns")
    normalized_name = normalize_dns_name(hostname)
    if not endpoints:
        raise DnsLookupError("公開 DoH エンドポイントが未設定です")
    successful: list[set[str]] = []
    failures: list[str] = []
    seen_endpoints: set[str] = set()
    for endpoint in endpoints:
        parsed = urllib.parse.urlsplit(endpoint.strip())
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOH_HOSTS:
            failures.append(f"{endpoint}: HTTPS ではありません")
            continue
        canonical_endpoint = urllib.parse.urlunsplit(
            (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.path or "/", parsed.query, "")
        )
        if canonical_endpoint in seen_endpoints:
            continue
        seen_endpoints.add(canonical_endpoint)
        try:
            successful.append(
                _query_doh_a_records(
                    canonical_endpoint,
                    normalized_name,
                    timeout=timeout,
                    retries=retries,
                    opener=opener,
                )
            )
        except DnsLookupError as exc:
            failures.append(f"{redact_url(endpoint)}: {_safe_error(exc)}")
    if failures or len(successful) != len(seen_endpoints) or not successful:
        detail = "; ".join(failures[:3])
        raise DnsLookupError(f"公開 DNS A レコードを全リゾルバから取得できませんでした{': ' + detail if detail else ''}")

    if len(successful) >= 2 and any(item != successful[0] for item in successful[1:]):
        raise DnsLookupError("公開 DoH の DNS A レコードが一致しません")
    addresses: set[str] = set(successful[0])
    _log(
        log,
        logging.INFO,
        "公開 DNS A レコードを取得しました",
        "dns_a_detected",
        dns_name=normalized_name,
        addresses=sorted(addresses),
        resolvers=len(successful),
    )
    return addresses


MAX_API_RESPONSE_BYTES = 64 * 1024


def _parse_api_response(body: bytes, *, secret: str = "") -> Mapping[str, Any]:
    if len(body) > MAX_API_RESPONSE_BYTES:
        raise DdnsApiError("DDNS API の応答が大きすぎます")
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise DdnsApiError("DDNS API の応答が空です")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise DdnsApiError(f"DDNS API の応答を解釈できません: {_safe_error(RuntimeError(text[:200]), secret)}")
    if not isinstance(value, Mapping):
        raise DdnsApiError("DDNS API の JSON 応答がオブジェクトではありません")
    result = str(value.get("result", "")).upper()
    if result != "OK":
        error_code = value.get("errorcode", "unknown")
        error_message = value.get("errormsg", "")
        raise DdnsApiError(
            f"DDNS API が拒否しました ({_safe_error(RuntimeError(str(error_code)[:80]), secret)}): "
            f"{_safe_error(RuntimeError(str(error_message)[:200]), secret)}"
        )
    return value


def update_dns_record(
    config: DdnsConfig,
    ip: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    logger: logging.Logger | None = None,
) -> Mapping[str, Any]:
    """DDNS Now の A レコードを明示 IPv4 で更新する。"""

    log = logger or logging.getLogger("aoitalk.update_ddns")
    try:
        normalized_ip = validate_ipv4(ip)
    except ValueError as exc:
        raise DdnsApiError(str(exc)) from exc
    query = urllib.parse.urlencode(
        {
            "domain": config.domain,
            "password": config.password,
            "ip": normalized_ip,
            "format": "json",
        }
    )
    url = f"{config.update_url}?{query}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "AoiTalk-DDNS/1.0"},
        method="GET",
    )
    _log(log, logging.INFO, "DNS A レコード更新を要求します", "ddns_update_request", dns_name=config.dns_name, ip=normalized_ip, url=redact_url(url, config.password))
    try:
        body = _request_with_retries(
            request,
            timeout=config.timeout,
            retries=config.retries,
            opener=opener,
            secret=config.password,
            max_bytes=MAX_API_RESPONSE_BYTES,
            response_validator=lambda response: _verify_final_url(
                response,
                allowed_hosts=frozenset({"f5.si"}),
                error_type=DdnsApiError,
                label="DDNS API",
                expected_path="/update.php",
            ),
        )
        response = _parse_api_response(body, secret=config.password)
        remote_ip_raw = response.get("remote_ip")
        if not isinstance(remote_ip_raw, str):
            raise DdnsApiError("DDNS API 応答に remote_ip がありません")
        try:
            remote_ip = validate_ipv4(remote_ip_raw)
        except ValueError as exc:
            raise DdnsApiError("DDNS API 応答の remote_ip が不正です") from exc
        if remote_ip != normalized_ip:
            # API の remote_ip はプロキシ/NAT 経路の送信元を示す場合があり、明示した
            # 更新値と異なっても API 自体が result=OK なら更新成功として扱う。
            _log(
                log,
                logging.WARNING,
                "DDNS API の remote_ip が要求 IPv4 と異なりますが更新成功として扱います",
                "ddns_remote_ip_mismatch",
                requested_ip=normalized_ip,
                remote_ip=remote_ip,
            )
    except DdnsApiError:
        raise
    except Exception as exc:  # pragma: no cover - 防御的な最終境界
        raise DdnsApiError(f"DDNS API 更新に失敗しました: {_safe_error(exc, config.password)}") from exc
    _log(log, logging.INFO, "DNS A レコードを更新しました", "ddns_update_succeeded", dns_name=config.dns_name, ip=normalized_ip)
    return response


def run_update(
    config: DdnsConfig,
    *,
    logger: logging.Logger | None = None,
    public_ip_fetcher: Callable[..., str] = fetch_public_ipv4,
    dns_resolver: Callable[..., set[str]] = resolve_dns_a,
    updater: Callable[..., Mapping[str, Any]] = update_dns_record,
) -> int:
    """1 回分の判定・必要時更新を実行し、終了コードを返す。"""

    log = logger or logging.getLogger("aoitalk.update_ddns")
    try:
        public_ip = public_ip_fetcher(
            config.public_ip_endpoints,
            timeout=config.timeout,
            retries=config.retries,
            logger=log,
        )
        public_ip = validate_ipv4(public_ip)
    except (PublicIpError, ValueError) as exc:
        _log(log, logging.ERROR, "公開 IPv4 の取得または検証に失敗しました", "public_ip_failed", error=_safe_error(exc, config.password))
        return ExitCode.PUBLIC_IP_ERROR

    try:
        current_addresses = dns_resolver(
            config.dns_name or f"{config.domain}.f5.si",
            timeout=config.timeout,
            retries=config.retries,
            endpoints=config.dns_over_https_endpoints,
            logger=log,
        )
        normalized_addresses = {validate_ipv4(value) for value in current_addresses}
    except (DnsLookupError, ValueError, OSError) as exc:
        _log(log, logging.ERROR, "DNS A レコードの取得または検証に失敗しました", "dns_lookup_failed", error=_safe_error(exc, config.password))
        return ExitCode.DNS_ERROR

    if public_ip in normalized_addresses:
        _log(log, logging.INFO, "DNS A レコードは最新です。更新しません", "ddns_no_change", dns_name=config.dns_name, ip=public_ip)
        return ExitCode.SUCCESS

    _log(
        log,
        logging.INFO,
        "DNS A レコードと公開 IPv4 が異なります",
        "ddns_difference_detected",
        dns_name=config.dns_name,
        current=sorted(normalized_addresses),
        desired=public_ip,
    )
    if config.dry_run:
        _log(log, logging.INFO, "dry-run のため DDNS API 更新を省略しました", "ddns_dry_run", dns_name=config.dns_name, ip=public_ip)
        return ExitCode.SUCCESS

    try:
        updater(config, public_ip, logger=log)
    except Exception as exc:
        _log(log, logging.ERROR, "DDNS API 更新に失敗しました", "ddns_update_failed", error=_safe_error(exc, config.password))
        return ExitCode.API_ERROR
    return ExitCode.SUCCESS


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AoiTalk f5.si DDNS A レコード自動更新")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="DDNS_NOW_* を読む .env のパス")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP/DNS のタイムアウト秒")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="HTTP の最大試行回数")
    parser.add_argument("--dry-run", action="store_true", help="差分を検出しても API を呼ばない")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        config = load_config(
            args.env_file,
            timeout=args.timeout,
            retries=args.retries,
            dry_run=args.dry_run,
        )
    except (ConfigurationError, ValueError) as exc:
        _log(logger, logging.ERROR, "DDNS 設定が不正です", "configuration_failed", error=_safe_error(exc))
        return ExitCode.CONFIGURATION_ERROR

    _log(
        logger,
        logging.INFO,
        "DDNS 更新を開始します",
        "ddns_update_started",
        dns_name=config.dns_name,
        dry_run=config.dry_run,
        retries=config.retries,
        timeout=config.timeout,
    )
    try:
        return run_update(config, logger=logger)
    except Exception as exc:  # pragma: no cover - 予期しない障害でもタスクを再実行可能にする
        _log(logger, logging.ERROR, "DDNS 更新で予期しないエラーが発生しました", "ddns_unexpected_error", error=_safe_error(exc, config.password))
        return ExitCode.INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
