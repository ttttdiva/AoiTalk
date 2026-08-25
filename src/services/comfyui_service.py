"""ComfyUI画像生成サービス

ローカルComfyUI (localhost:8188) のREST APIを経由して
ワークフローベースの画像生成を行う。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import socket
import stat
import time
import uuid
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)

# 出力ディレクトリ
OUTPUT_DIR = Path("temp/generated_images")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ワークフローディレクトリ
WORKFLOWS_DIR = Path("config/comfyui_workflows")
DEFAULT_AUTO_WORKFLOW = WORKFLOWS_DIR / "aoitalk_auto_sdxl.json"
DEFAULT_NEGATIVE_PROMPT = (
    "lowres, bad anatomy, bad hands, extra fingers, missing fingers, "
    "bad face, deformed, cropped, worst quality, low quality, jpeg artifacts, "
    "text, watermark, signature, username, blurry"
)


class ComfyUIError(Exception):
    """ComfyUI操作のエラー"""


_COMFYUI_ALLOWED_HOSTS_ENV = "AOITALK_COMFYUI_ALLOWED_HOSTS"
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_WORKFLOW_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}\.json\Z")
_UPLOAD_FILENAME_RE = re.compile(
    r"[0-9a-f]{64}\.(?:png|jpe?g|webp|gif|bmp|avif)\Z"
)
_UPLOAD_SUBFOLDER_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UPLOAD_MIME_TYPES = {
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_UPLOAD_SUFFIX_BY_MIME = {
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": ".png",
    "image/webp": ".webp",
}


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or (
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            and getattr(metadata, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _normalized_hostname(value: str) -> str:
    candidate = value.strip().rstrip(".").casefold()
    if not candidate or "%" in candidate:
        raise ValueError("ComfyUI URLのhostが不正です")
    try:
        normalized = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("ComfyUI URLのhostが不正です") from exc
    if not _HOSTNAME_RE.fullmatch(normalized):
        raise ValueError("ComfyUI URLのhostが不正です")
    return normalized


def _comfyui_host_is_allowed(host: str, allowlist: str | None) -> bool:
    """Allow loopback by default and exact hostname/IP/CIDR env entries."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        normalized = _normalized_hostname(host)
        if normalized == "localhost":
            return True
        for raw_entry in (allowlist or "").split(","):
            entry = raw_entry.strip()
            if not entry or "/" in entry:
                continue
            try:
                if _normalized_hostname(entry.strip("[]")) == normalized:
                    return True
            except ValueError:
                continue
        return False

    if address.is_loopback:
        return True
    for raw_entry in (allowlist or "").split(","):
        entry = raw_entry.strip().strip("[]")
        if not entry:
            continue
        try:
            if "/" in entry:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _comfyui_address_is_allowed(address: str, allowlist: str | None) -> bool:
    parsed = ipaddress.ip_address(address)
    if parsed.is_loopback:
        return True
    for raw_entry in (allowlist or "").split(","):
        entry = raw_entry.strip().strip("[]")
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if parsed in network:
            return True
    return False


class _PinnedPolicyResolver(aiohttp.abc.AbstractResolver):
    """Resolve once per request and reject if any answer escapes IP policy."""

    def __init__(self, allowlist: str, *, getaddrinfo: Any | None = None) -> None:
        self.allowlist = allowlist
        self._getaddrinfo = getaddrinfo

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[dict[str, Any]]:
        resolver = self._getaddrinfo or asyncio.get_running_loop().getaddrinfo
        infos = await resolver(
            host,
            port,
            type=socket.SOCK_STREAM,
            family=family,
        )
        results: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for resolved_family, _, proto, _, sockaddr in infos:
            address = str(sockaddr[0])
            identity = (int(resolved_family), address)
            if identity in seen:
                continue
            seen.add(identity)
            if not _comfyui_address_is_allowed(address, self.allowlist):
                raise OSError("ComfyUI DNS address is outside the allowed IP policy")
            results.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": resolved_family,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise OSError("ComfyUI DNS resolution returned no addresses")
        return results

    async def close(self) -> None:
        return None


def validate_comfyui_base_url(
    value: str,
    *,
    allowed_hosts: str | None = None,
) -> str:
    """Return a canonical SSRF-safe ComfyUI origin.

    Non-loopback hosts must be explicitly listed in
    ``AOITALK_COMFYUI_ALLOWED_HOSTS`` as an exact hostname, IP address, or CIDR.
    Only a bare HTTP(S) origin is accepted because API paths are server-owned.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("ComfyUI URLが不正です")
    if "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("ComfyUI URLが不正です")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError("ComfyUI URLが不正です") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("ComfyUI URLはhttp/httpsのみ利用できます")
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("ComfyUI URLにuserinfoは指定できません")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("ComfyUI URLにはpath/query/fragmentを指定できません")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("ComfyUI URLのportが不正です")
    configured_allowlist = (
        os.environ.get(_COMFYUI_ALLOWED_HOSTS_ENV, "")
        if allowed_hosts is None
        else allowed_hosts
    )
    if not _comfyui_host_is_allowed(host, configured_allowlist):
        raise ValueError("ComfyUI URLのhostは許可されていません")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        normalized_host = _normalized_hostname(host)
    else:
        normalized_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    authority = normalized_host if port is None else f"{normalized_host}:{port}"
    return f"{parsed.scheme.casefold()}://{authority}"


async def validate_comfyui_connection_url(
    value: str,
    *,
    allowed_hosts: str | None = None,
    getaddrinfo: Any | None = None,
) -> str:
    """Validate an origin and every DNS answer using the connection policy."""
    normalized = validate_comfyui_base_url(value, allowed_hosts=allowed_hosts)
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        configured_allowlist = (
            os.environ.get(_COMFYUI_ALLOWED_HOSTS_ENV, "")
            if allowed_hosts is None
            else allowed_hosts
        )
        resolver = _PinnedPolicyResolver(
            configured_allowlist,
            getaddrinfo=getaddrinfo,
        )
        try:
            await resolver.resolve(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                family=socket.AF_UNSPEC,
            )
        except OSError as exc:
            raise ValueError("ComfyUI hostnameのDNS addressが許可されていません") from exc
    return normalized


class ComfyUIService:
    """ComfyUI REST APIクライアント"""

    def __init__(
        self,
        enabled: bool = True,
        base_url: str = "http://127.0.0.1:8188",
        default_workflow_path: Optional[str] = None,
        workflows_dir: Optional[str] = None,
        timeout_seconds: int = 180,
        max_download_bytes: int = 64 * 1024 * 1024,
        max_upload_bytes: int = 32 * 1024 * 1024,
        max_upload_response_bytes: int = 64 * 1024,
    ):
        self.enabled = bool(enabled)
        self.base_url = validate_comfyui_base_url(base_url)
        if workflows_dir:
            self.workflows_dir = Path(workflows_dir)
        elif default_workflow_path:
            self.workflows_dir = Path(default_workflow_path).parent
        else:
            self.workflows_dir = WORKFLOWS_DIR
        self.default_workflow_path = default_workflow_path or str(
            self.workflows_dir / DEFAULT_AUTO_WORKFLOW.name
        )
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max(1, int(max_download_bytes))
        self.max_upload_bytes = max(1, int(max_upload_bytes))
        self.max_upload_response_bytes = max(1, int(max_upload_response_bytes))
        self.client_id = str(uuid.uuid4())
        
        # ワークフローディレクトリの確保
        self.workflows_dir.mkdir(parents=True, exist_ok=True)

    def _http_session(self, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
        # Revalidate policy at request time, then pin the connector to the exact
        # validated DNS answer set so a second lookup cannot redirect the socket.
        validate_comfyui_base_url(self.base_url)
        allowlist = os.environ.get(_COMFYUI_ALLOWED_HOSTS_ENV, "")
        resolver = _PinnedPolicyResolver(allowlist)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=True,
            ttl_dns_cache=None,
        )
        return aiohttp.ClientSession(timeout=timeout, connector=connector)

    @classmethod
    def from_config(cls, config) -> "ComfyUIService":
        """config.yaml の comfyui セクションから初期化する。"""
        comfyui_conf = config.get("comfyui", {})
        if not comfyui_conf:
            comfyui_conf = {}
        return cls(
            enabled=comfyui_conf.get("enabled", True),
            base_url=comfyui_conf.get("url", "http://127.0.0.1:8188"),
            default_workflow_path=comfyui_conf.get("default_workflow"),
            workflows_dir=comfyui_conf.get("workflows_dir"),
            timeout_seconds=comfyui_conf.get("timeout_seconds", 120),
        )

    async def is_available(self) -> bool:
        """ComfyUIサーバーに接続可能か確認する。"""
        if not self.enabled:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with self._http_session(timeout) as session:
                async with session.get(
                    f"{self.base_url}/system_stats",
                    allow_redirects=False,
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(
            total=float(timeout_seconds or min(self.timeout_seconds, 30))
        )
        try:
            async with self._http_session(timeout) as session:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=dict(payload) if payload is not None else None,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400:
                        raise ComfyUIError(
                            f"ComfyUI {path} redirectは許可されていません"
                        )
                    if response.status < 200 or response.status >= 300:
                        raise ComfyUIError(
                            f"ComfyUI {path} 失敗: HTTP {response.status}"
                        )
                    body = await response.text()
                    if not body.strip():
                        value = {}
                    else:
                        try:
                            value = json.loads(body)
                        except json.JSONDecodeError as exc:
                            raise ComfyUIError(
                                f"ComfyUI {path} の応答がJSONではありません"
                            ) from exc
        except ComfyUIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ComfyUIError(f"ComfyUI {path} 通信失敗: {exc}") from exc
        if not isinstance(value, Mapping):
            raise ComfyUIError(f"ComfyUI {path} の応答がJSON objectではありません")
        return dict(value)

    async def system_stats(self) -> dict[str, Any]:
        return await self._request_json("GET", "/system_stats", timeout_seconds=5)

    async def object_info(self, node_type: str | None = None) -> dict[str, Any]:
        suffix = f"/{node_type}" if node_type else ""
        return await self._request_json("GET", f"/object_info{suffix}")

    async def submit_prompt(
        self,
        prompt: Mapping[str, Any],
        *,
        extra_data: Mapping[str, Any] | None = None,
        client_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        if not isinstance(prompt, Mapping) or not prompt:
            raise ValueError("ComfyUI API promptは空でないJSON objectが必要です")
        payload: dict[str, Any] = {
            "prompt": json.loads(json.dumps(prompt, ensure_ascii=False)),
            "client_id": str(client_id or self.client_id),
        }
        if extra_data:
            payload["extra_data"] = json.loads(json.dumps(extra_data, ensure_ascii=False))
        result = await self._request_json(
            "POST", "/prompt", payload=payload, timeout_seconds=timeout_seconds
        )
        if result.get("error"):
            raise ComfyUIError(f"ComfyUI prompt validation失敗: {result['error']}")
        prompt_id = str(result.get("prompt_id") or "").strip()
        if not prompt_id:
            raise ComfyUIError("ComfyUI /prompt 応答にprompt_idがありません")
        return prompt_id

    @staticmethod
    def _validate_upload_subfolder(value: str) -> str:
        subfolder = str(value or "").strip()
        if (
            not subfolder
            or len(subfolder) > 512
            or "\\" in subfolder
            or subfolder.startswith("/")
            or subfolder.endswith("/")
        ):
            raise ValueError("ComfyUI upload subfolderが不正です")
        parts = subfolder.split("/")
        if any(
            part in {"", ".", ".."}
            or not _UPLOAD_SUBFOLDER_COMPONENT_RE.fullmatch(part)
            for part in parts
        ):
            raise ValueError("ComfyUI upload subfolderが不正です")
        return "/".join(parts)

    async def upload_image(
        self,
        content: bytes,
        *,
        filename: str,
        subfolder: str,
        mime_type: str,
        expected_checksum_sha256: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, str]:
        """Upload deterministic verified input bytes without following redirects."""
        if not isinstance(content, bytes) or not content:
            raise ValueError("ComfyUI upload contentは空でないbytesが必要です")
        if len(content) > self.max_upload_bytes:
            raise ValueError("ComfyUI upload sizeが上限を超えています")
        safe_filename = str(filename or "").strip().casefold()
        if not _UPLOAD_FILENAME_RE.fullmatch(safe_filename):
            raise ValueError("ComfyUI upload filenameが不正です")
        safe_subfolder = self._validate_upload_subfolder(subfolder)
        safe_mime = str(mime_type or "").strip().casefold()
        if safe_mime not in _UPLOAD_MIME_TYPES:
            raise ValueError("ComfyUI upload MIMEが不正です")
        allowed_suffix = _UPLOAD_SUFFIX_BY_MIME[safe_mime]
        if not safe_filename.endswith(allowed_suffix):
            raise ValueError("ComfyUI upload filenameとMIMEが一致しません")
        checksum = str(expected_checksum_sha256 or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("ComfyUI upload checksumが不正です")
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError("ComfyUI upload checksumがbytesと一致しません")
        form = aiohttp.FormData()
        form.add_field(
            "image",
            content,
            filename=safe_filename,
            content_type=safe_mime,
        )
        form.add_field("type", "input")
        form.add_field("subfolder", safe_subfolder)
        form.add_field("overwrite", "true")
        timeout = aiohttp.ClientTimeout(
            total=float(timeout_seconds or min(self.timeout_seconds, 30))
        )
        try:
            async with self._http_session(timeout) as session:
                async with session.request(
                    "POST",
                    f"{self.base_url}/upload/image",
                    data=form,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400:
                        raise ComfyUIError(
                            "ComfyUI /upload/image redirectは許可されていません"
                        )
                    if response.status < 200 or response.status >= 300:
                        raise ComfyUIError(
                            f"ComfyUI /upload/image 失敗: HTTP {response.status}"
                        )
                    chunks: list[bytes] = []
                    response_size = 0
                    while True:
                        chunk = await response.content.read(16 * 1024)
                        if not chunk:
                            break
                        response_size += len(chunk)
                        if response_size > self.max_upload_response_bytes:
                            raise ComfyUIError(
                                "ComfyUI /upload/image 応答sizeが上限を超えています"
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
        except ComfyUIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ComfyUIError(f"ComfyUI /upload/image 通信失敗: {exc}") from exc
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyUIError(
                "ComfyUI /upload/image 応答がJSONではありません"
            ) from exc
        if not isinstance(value, Mapping):
            raise ComfyUIError(
                "ComfyUI /upload/image 応答がJSON objectではありません"
            )
        returned_name = str(value.get("name") or "").strip()
        returned_subfolder = str(value.get("subfolder") or "").strip()
        returned_type = str(value.get("type") or "").strip().casefold()
        try:
            validated_subfolder = self._validate_upload_subfolder(returned_subfolder)
        except ValueError as exc:
            raise ComfyUIError(
                "ComfyUI /upload/image 応答subfolderが不正です"
            ) from exc
        if (
            returned_name != safe_filename
            or not _UPLOAD_FILENAME_RE.fullmatch(returned_name)
            or validated_subfolder != safe_subfolder
            or returned_type != "input"
        ):
            raise ComfyUIError(
                "ComfyUI /upload/image 応答locatorがrequestと一致しません"
            )
        return {
            "filename": returned_name,
            "subfolder": validated_subfolder,
            "type": returned_type,
        }

    async def queue(self) -> dict[str, Any]:
        return await self._request_json("GET", "/queue")

    async def history(self, prompt_id: str | None = None) -> dict[str, Any]:
        suffix = f"/{prompt_id}" if prompt_id else ""
        return await self._request_json("GET", f"/history{suffix}")

    @staticmethod
    def output_locators(history_entry: Mapping[str, Any]) -> list[dict[str, Any]]:
        locators: list[dict[str, Any]] = []
        outputs = history_entry.get("outputs")
        if not isinstance(outputs, Mapping):
            return locators
        for node_id in sorted(outputs, key=str):
            node_output = outputs[node_id]
            if not isinstance(node_output, Mapping):
                continue
            images = node_output.get("images")
            if not isinstance(images, list):
                continue
            for index, image in enumerate(images):
                if not isinstance(image, Mapping) or not image.get("filename"):
                    continue
                filename = str(image["filename"])
                subfolder = str(image.get("subfolder") or "")
                type_ = str(image.get("type") or "output")
                locators.append(
                    {
                        "engine_output_key": f"{node_id}:{index}:{subfolder}:{filename}",
                        "node_id": str(node_id),
                        "index": index,
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": type_,
                    }
                )
        return locators

    @staticmethod
    def _queue_contains(entries: Any, prompt_id: str) -> bool:
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if isinstance(entry, Mapping) and str(entry.get("prompt_id") or "") == prompt_id:
                return True
            if isinstance(entry, (list, tuple)) and any(
                str(value) == prompt_id for value in entry[:3]
            ):
                return True
        return False

    async def recover(self, prompt_id: str) -> dict[str, Any]:
        normalized = str(prompt_id or "").strip()
        if not normalized:
            raise ValueError("prompt_idは必須です")
        history = await self.history(normalized)
        entry = history.get(normalized)
        if isinstance(entry, Mapping):
            status = entry.get("status") if isinstance(entry.get("status"), Mapping) else {}
            status_str = str(status.get("status_str") or "").lower()
            if status_str in {"error", "failed", "execution_error"}:
                return {
                    "state": "error",
                    "prompt_id": normalized,
                    "history": dict(entry),
                    "outputs": self.output_locators(entry),
                    "evidence": {"status": dict(status)},
                }
            if status.get("completed"):
                state = "completed" if status_str == "success" else "error"
                return {
                    "state": state,
                    "prompt_id": normalized,
                    "history": dict(entry),
                    "outputs": self.output_locators(entry),
                    "evidence": {"status": dict(status)},
                }
        queue = await self.queue()
        if self._queue_contains(queue.get("queue_running"), normalized):
            state = "running"
        elif self._queue_contains(queue.get("queue_pending"), normalized):
            state = "queued"
        else:
            state = "unknown"
        return {
            "state": state,
            "prompt_id": normalized,
            "outputs": [],
            "evidence": {"queue": queue},
        }

    async def delete_queued(self, prompt_id: str) -> bool:
        await self._request_json(
            "POST", "/queue", payload={"delete": [str(prompt_id)]}
        )
        return True

    async def interrupt(self, prompt_id: str | None = None) -> bool:
        payload = {"prompt_id": str(prompt_id)} if prompt_id else {}
        await self._request_json("POST", "/interrupt", payload=payload)
        return True

    async def cancel_prompt(self, prompt_id: str) -> dict[str, Any]:
        recovered = await self.recover(prompt_id)
        state = recovered["state"]
        if state == "completed":
            return {**recovered, "action": "too_late"}
        if state == "queued":
            await self.delete_queued(prompt_id)
            after = await self.recover(prompt_id)
            if after["state"] == "completed":
                return {**after, "action": "too_late"}
            return {
                **recovered,
                "state": "cancel_requested",
                "action": "queue_delete",
                "command_acknowledged": True,
            }
        if state == "running":
            await self.interrupt(prompt_id)
            after = await self.recover(prompt_id)
            if after["state"] == "completed":
                return {**after, "action": "too_late"}
            return {
                **recovered,
                "state": "cancel_requested",
                "action": "interrupt",
                "command_acknowledged": True,
            }
        return {**recovered, "action": "not_found"}

    async def wait_for_terminal(
        self,
        prompt_id: str,
        *,
        timeout_seconds: float | None = None,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        timeout = float(timeout_seconds or self.timeout_seconds)
        deadline = asyncio.get_running_loop().time() + max(0.01, timeout)
        last: dict[str, Any] = {"state": "unknown", "prompt_id": prompt_id}
        while asyncio.get_running_loop().time() < deadline:
            last = await self.recover(prompt_id)
            if last["state"] in {"completed", "error"}:
                return last
            await asyncio.sleep(max(0.001, poll_interval))
        raise ComfyUIError(
            f"ComfyUI prompt {prompt_id} の待機が{timeout:g}秒でtimeoutしました"
        )

    async def view_bytes(self, locator: Mapping[str, Any]) -> bytes:
        params = {
            "filename": str(locator.get("filename") or ""),
            "subfolder": str(locator.get("subfolder") or ""),
            "type": str(locator.get("type") or "output"),
        }
        if not params["filename"]:
            raise ValueError("output locatorにfilenameが必要です")
        timeout = aiohttp.ClientTimeout(total=min(self.timeout_seconds, 30))
        try:
            async with self._http_session(timeout) as session:
                async with session.get(
                    f"{self.base_url}/view",
                    params=params,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400:
                        raise ComfyUIError("ComfyUI /view redirectは許可されていません")
                    if response.status != 200:
                        raise ComfyUIError(
                            f"ComfyUI /view 失敗: HTTP {response.status}"
                        )
                    content_type = str(response.headers.get("Content-Type") or "")
                    if not content_type.lower().startswith("image/"):
                        raise ComfyUIError("ComfyUI /view がimage content-typeではありません")
                    declared = response.content_length
                    if declared is not None and declared > self.max_download_bytes:
                        raise ComfyUIError("ComfyUI /view の画像が上限を超えています")
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        received += len(chunk)
                        if received > self.max_download_bytes:
                            raise ComfyUIError("ComfyUI /view の画像が上限を超えています")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except ComfyUIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ComfyUIError(f"ComfyUI /view 通信失敗: {exc}") from exc

    async def list_workflows(self) -> list[dict[str, Any]]:
        """利用可能なワークフローJSONの一覧を取得する。"""
        workflows = []
        root = self._workflow_root()
        default_name = (
            Path(self.default_workflow_path).name if self.default_workflow_path else None
        )
        for path in root.glob("*.json"):
            try:
                target = self._workflow_target(path.name, require_existing=True)
                metadata = target.stat()
            except (OSError, ValueError):
                continue
            workflows.append(
                {
                    "name": target.name,
                    "is_default": target.name == default_name,
                    "mtime": metadata.st_mtime,
                }
            )
        return sorted(workflows, key=lambda x: x["mtime"], reverse=True)

    def _workflow_root(self) -> Path:
        root = self.workflows_dir.absolute()
        if not root.exists() or not root.is_dir() or _is_link_or_reparse(root):
            raise ValueError("workflow rootが安全な通常directoryではありません")
        return root.resolve(strict=True)

    def _workflow_target(self, name: str, *, require_existing: bool = False) -> Path:
        if (
            not isinstance(name, str)
            or name != Path(name).name
            or "/" in name
            or "\\" in name
            or ":" in name
            or ".." in name
            or not _WORKFLOW_NAME_RE.fullmatch(name)
        ):
            raise ValueError("workflow nameが不正です")
        root = self._workflow_root()
        target = root / name
        resolved = target.resolve(strict=require_existing)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("workflow pathがroot外です") from exc
        if target.exists() and (
            not target.is_file() or _is_link_or_reparse(target) or resolved != target
        ):
            raise ValueError("workflow fileが安全な通常fileではありません")
        return target

    async def save_workflow(self, name: str, content: str | dict) -> dict[str, Any]:
        """ワークフローJSONを保存する。"""
        target_path = self._workflow_target(name)
        if isinstance(content, dict):
            content_str = json.dumps(content, indent=2, ensure_ascii=False)
        else:
            content_str = content
            # JSONバリデーション
            json.loads(content_str)

        target_path.write_text(content_str, encoding="utf-8")
        return {"name": target_path.name, "is_default": False}

    async def delete_workflow(self, name: str) -> bool:
        """ワークフローJSONを削除する。"""
        target_path = self._workflow_target(name)
        if target_path.exists():
            self._workflow_target(name, require_existing=True)
            target_path.unlink()
            return True
        return False

    async def get_workflow_content(self, name: str) -> dict:
        """ワークフローJSONの内容を取得する。"""
        target_path = self._workflow_target(name)
        if not target_path.exists():
            raise FileNotFoundError(f"Workflow not found: {name}")
        self._workflow_target(name, require_existing=True)
        return self._load_workflow(str(target_path))

    def update_config(
        self,
        enabled: bool = None,
        base_url: str = None,
        default_workflow_path: str = None,
    ):
        """設定を更新する。"""
        if enabled is not None:
            self.enabled = bool(enabled)
        if base_url is not None:
            self.base_url = validate_comfyui_base_url(base_url)
        if default_workflow_path is not None:
            self.default_workflow_path = default_workflow_path

    async def generate_image(
        self,
        positive_prompt: str,
        negative_prompt: str = "",
        workflow_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> str:
        """画像を生成し、ローカルファイルパスを返す。

        Args:
            positive_prompt: ポジティブプロンプト（Danbooruタグ形式推奨）
            negative_prompt: ネガティブプロンプト
            workflow_path: ワークフローJSONのパス（Noneならデフォルト使用）
            overrides: 上書き設定 {checkpoint, lora, lora_strength,
                       width, height, steps, cfg, sampler, scheduler, seed}

        Returns:
            生成画像の絶対パス

        Raises:
            ComfyUIError: 生成失敗時
        """
        overrides = overrides or {}

        # ワークフロー読み込み
        wf_path = workflow_path or self.default_workflow_path
        if not wf_path or not Path(wf_path).exists():
            wf_path = await self.ensure_default_workflow(overrides)
        if not wf_path or not Path(wf_path).exists():
            raise ComfyUIError(f"ワークフローファイルが見つかりません: {wf_path}")

        workflow = self._load_workflow(wf_path)

        # APIプロンプト形式に変換
        prompt = self._workflow_to_api_prompt(
            workflow, positive_prompt, negative_prompt, overrides
        )

        # ジョブ投入
        prompt_id = await self._queue_prompt(prompt)
        logger.info("ComfyUI ジョブを投入しました: %s", prompt_id)

        # 完了待ち
        result = await self._wait_for_completion(prompt_id)

        # 画像ダウンロード
        image_path = await self._download_result_image(result)
        logger.info("ComfyUI 画像生成完了: %s", image_path)

        return str(image_path)

    async def ensure_default_workflow(self, overrides: Optional[Dict[str, Any]] = None) -> str:
        """利用可能な実モデルから標準 txt2img ワークフローを作成して返す。"""
        overrides = overrides or {}
        target = Path(self.default_workflow_path) if self.default_workflow_path else self.workflows_dir / DEFAULT_AUTO_WORKFLOW.name
        if target.exists():
            self.default_workflow_path = str(target)
            return str(target)

        checkpoint = str(overrides.get("checkpoint") or "").strip()
        if not checkpoint:
            checkpoint = await self._select_available_checkpoint()
        sampler = str(overrides.get("sampler") or "euler_ancestral")
        scheduler = str(overrides.get("scheduler") or "normal")
        workflow = self._build_default_txt2img_workflow(
            checkpoint=checkpoint,
            sampler=sampler,
            scheduler=scheduler,
            width=int(overrides.get("width") or 1024),
            height=int(overrides.get("height") or 1024),
            steps=int(overrides.get("steps") or 24),
            cfg=float(overrides.get("cfg") or 6.5),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        self.default_workflow_path = str(target)
        logger.info("ComfyUI 標準ワークフローを作成しました: %s", target)
        return str(target)

    async def _select_available_checkpoint(self) -> str:
        """ComfyUI に登録されている checkpoint から実在するものを選ぶ。"""
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._http_session(timeout) as session:
                async with session.get(
                    f"{self.base_url}/object_info/CheckpointLoaderSimple",
                    allow_redirects=False,
                ) as resp:
                    if 300 <= resp.status < 400:
                        raise ComfyUIError("checkpoint 一覧のredirectは許可されていません")
                    if resp.status != 200:
                        raise ComfyUIError(f"checkpoint 一覧取得失敗: HTTP {resp.status}")
                    info = await resp.json()
        except ComfyUIError:
            raise
        except Exception as e:
            raise ComfyUIError(f"checkpoint 一覧取得失敗: {e}") from e

        choices = (
            info.get("CheckpointLoaderSimple", {})
            .get("input", {})
            .get("required", {})
            .get("ckpt_name", [[]])[0]
        )
        if not choices:
            raise ComfyUIError("ComfyUI に利用可能な checkpoint がありません")

        preferred = [
            "waiNSFWIllustrious_v150.safetensors",
            "pornmaster_proSDXLV8.safetensors",
        ]
        for name in preferred:
            if name in choices:
                return name
        return str(choices[0])

    def _build_default_txt2img_workflow(
        self,
        *,
        checkpoint: str,
        sampler: str,
        scheduler: str,
        width: int,
        height: int,
        steps: int,
        cfg: float,
    ) -> dict:
        """ComfyUI UI JSON 形式の標準 SDXL txt2img ワークフローを作る。"""
        return {
            "last_node_id": 7,
            "last_link_id": 9,
            "nodes": [
                {"id": 1, "type": "CheckpointLoaderSimple", "mode": 0, "inputs": [], "widgets_values": [checkpoint]},
                {"id": 2, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "link": 2}], "widgets_values": ["positive prompt"]},
                {"id": 3, "type": "CLIPTextEncode", "mode": 0, "inputs": [{"name": "clip", "link": 3}], "widgets_values": [DEFAULT_NEGATIVE_PROMPT]},
                {"id": 4, "type": "EmptyLatentImage", "mode": 0, "inputs": [], "widgets_values": [width, height, 1]},
                {
                    "id": 5,
                    "type": "KSampler",
                    "mode": 0,
                    "inputs": [
                        {"name": "model", "link": 1},
                        {"name": "positive", "link": 4},
                        {"name": "negative", "link": 5},
                        {"name": "latent_image", "link": 6},
                    ],
                    "widgets_values": [0, "randomize", steps, cfg, sampler, scheduler, 1.0],
                },
                {
                    "id": 6,
                    "type": "VAEDecode",
                    "mode": 0,
                    "inputs": [
                        {"name": "samples", "link": 7},
                        {"name": "vae", "link": 8},
                    ],
                    "widgets_values": [],
                },
                {"id": 7, "type": "SaveImage", "mode": 0, "inputs": [{"name": "images", "link": 9}], "widgets_values": ["aoitalk"]},
            ],
            "links": [
                [1, 1, 0, 5, 0, "MODEL"],
                [2, 1, 1, 2, 0, "CLIP"],
                [3, 1, 1, 3, 0, "CLIP"],
                [4, 2, 0, 5, 1, "CONDITIONING"],
                [5, 3, 0, 5, 2, "CONDITIONING"],
                [6, 4, 0, 5, 3, "LATENT"],
                [7, 5, 0, 6, 0, "LATENT"],
                [8, 1, 2, 6, 1, "VAE"],
                [9, 6, 0, 7, 0, "IMAGE"],
            ],
        }

    def _load_workflow(self, path: str) -> dict:
        """ワークフローJSONを読み込む。"""
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _workflow_to_api_prompt(
        self,
        workflow: dict,
        positive_prompt: str,
        negative_prompt: str,
        overrides: dict,
    ) -> dict:
        """ワークフローJSONをComfyUI APIのprompt dictに変換する。

        ワークフローの nodes 配列からノードID→class_type→inputs を構築し、
        プロンプトやパラメータを動的に差し替える。
        """
        nodes = workflow.get("nodes", [])
        links = workflow.get("links", [])

        # リンクテーブル構築: link_id → (from_node_id, from_slot)
        link_map = {}
        for link in links:
            # link format: [link_id, from_node_id, from_slot, to_node_id, to_slot, type]
            link_id, from_node, from_slot = link[0], link[1], link[2]
            link_map[link_id] = (str(from_node), from_slot)

        # ノードマップ構築
        prompt = {}
        node_info = {}  # node_id → {class_type, widgets_values, inputs, mode}

        for node in nodes:
            node_id = str(node["id"])
            class_type = node["type"]
            widgets = node.get("widgets_values", [])
            inputs_list = node.get("inputs", [])
            mode = node.get("mode", 0)

            node_info[node_id] = {
                "class_type": class_type,
                "widgets_values": widgets,
                "inputs": inputs_list,
                "mode": mode,
            }

        # 各ノードをAPIプロンプト形式に変換
        for node_id, info in node_info.items():
            class_type = info["class_type"]
            widgets = info["widgets_values"]
            inputs_raw = info["inputs"]
            mode = info["mode"]

            # mode=4 はバイパス (パススルー)
            # バイパスノードはAPIに含めるが、接続を直結にする必要あり
            # 今回はLoRAのバイパスを考慮して処理

            api_inputs = {}

            # ── ノード種別ごとのinputs構築 ──

            if class_type == "CheckpointLoaderSimple":
                ckpt = overrides.get("checkpoint", widgets[0] if widgets else "")
                api_inputs["ckpt_name"] = ckpt

            elif class_type == "LoraLoader":
                lora_name = overrides.get("lora", widgets[0] if len(widgets) > 0 else "")
                strength_model = overrides.get("lora_strength", widgets[1] if len(widgets) > 1 else 1.0)
                strength_clip = overrides.get("lora_strength", widgets[2] if len(widgets) > 2 else 1.0)
                api_inputs["lora_name"] = lora_name
                api_inputs["strength_model"] = strength_model
                api_inputs["strength_clip"] = strength_clip
                # model/clip 入力はリンクから
                for inp in inputs_raw:
                    if inp["name"] == "model" and inp.get("link") is not None:
                        api_inputs["model"] = list(link_map[inp["link"]])
                    elif inp["name"] == "clip" and inp.get("link") is not None:
                        api_inputs["clip"] = list(link_map[inp["link"]])

            elif class_type == "EmptyLatentImage":
                api_inputs["width"] = overrides.get("width", widgets[0] if len(widgets) > 0 else 1280)
                api_inputs["height"] = overrides.get("height", widgets[1] if len(widgets) > 1 else 1536)
                api_inputs["batch_size"] = widgets[2] if len(widgets) > 2 else 1

            elif class_type == "CLIPTextEncode":
                # positive (ノード6) vs negative (ノード7) の判別
                # outputsのリンク先を確認してKSamplerのpositive/negativeどちらに繋がるか判定
                # 簡易判定: node_id == "6" → positive, "7" → negative
                if self._is_positive_prompt_node(node_id, links):
                    api_inputs["text"] = positive_prompt
                else:
                    api_inputs["text"] = negative_prompt or (widgets[0] if widgets else "")
                # clip入力
                for inp in inputs_raw:
                    if inp["name"] == "clip" and inp.get("link") is not None:
                        api_inputs["clip"] = list(link_map[inp["link"]])

            elif class_type == "KSampler":
                seed = overrides.get("seed", random.randint(0, 2**32 - 1))
                api_inputs["seed"] = seed
                api_inputs["steps"] = overrides.get("steps", widgets[2] if len(widgets) > 2 else 25)
                api_inputs["cfg"] = overrides.get("cfg", widgets[3] if len(widgets) > 3 else 8.0)
                api_inputs["sampler_name"] = overrides.get("sampler", widgets[4] if len(widgets) > 4 else "euler_ancestral")
                api_inputs["scheduler"] = overrides.get("scheduler", widgets[5] if len(widgets) > 5 else "normal")
                api_inputs["denoise"] = widgets[6] if len(widgets) > 6 else 1.0
                # model/positive/negative/latent_image 入力はリンクから
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            elif class_type == "VAEDecode":
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            elif class_type == "SaveImage":
                api_inputs["filename_prefix"] = overrides.get("filename_prefix", "aoitalk")
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            else:
                # 未知のノードタイプ: widgets_values とリンクをそのまま使用
                for inp in inputs_raw:
                    link = inp.get("link")
                    if link is not None and link in link_map:
                        api_inputs[inp["name"]] = list(link_map[link])

            # バイパスモード (mode=4) のノードはスキップ
            # ただしリンク構造を維持するために出力先を直結する必要がある
            if mode == 4:
                continue

            prompt[node_id] = {
                "inputs": api_inputs,
                "class_type": class_type,
            }

        # バイパスされたノードのリンクを修正
        # LoRA がバイパスの場合、CheckpointLoader→KSampler/CLIPTextEncode に直結
        self._fix_bypassed_links(prompt, node_info, link_map, links)

        return prompt

    def _is_positive_prompt_node(self, node_id: str, links: list) -> bool:
        """ノードがKSamplerのpositive入力に繋がっているか判定する。"""
        for link in links:
            # link: [link_id, from_node, from_slot, to_node, to_slot, type]
            if str(link[1]) == node_id and link[4] == 1:  # slot 1 = positive
                return True
        return False

    def _fix_bypassed_links(self, prompt: dict, node_info: dict, link_map: dict, links: list):
        """バイパスされたノードを経由するリンクを直結に修正する。"""
        bypassed_ids = {nid for nid, info in node_info.items() if info["mode"] == 4}
        if not bypassed_ids:
            return

        # バイパスされたノードの入力→出力の対応を構築
        # LoraLoaderの場合: input model(slot0)→output MODEL(slot0), input clip(slot1)→output CLIP(slot1)
        for bp_id in bypassed_ids:
            bp_info = node_info[bp_id]
            bp_inputs = bp_info["inputs"]

            # 入力スロット → ソースノード のマッピング
            input_sources = {}
            for inp in bp_inputs:
                link = inp.get("link")
                if link is not None and link in link_map:
                    # LoraLoader: model(0番目input) → 0番目output, clip(1番目input) → 1番目output
                    input_sources[inp["name"]] = link_map[link]

            # このバイパスノードを参照しているpromptノードのリンクを修正
            for nid, node_data in prompt.items():
                inputs = node_data.get("inputs", {})
                for key, val in list(inputs.items()):
                    if isinstance(val, list) and len(val) == 2 and str(val[0]) == bp_id:
                        output_slot = val[1]
                        # LoraLoader: output slot 0 = model → 入力の model
                        # output slot 1 = clip → 入力の clip
                        if bp_info["class_type"] == "LoraLoader":
                            if output_slot == 0 and "model" in input_sources:
                                inputs[key] = list(input_sources["model"])
                            elif output_slot == 1 and "clip" in input_sources:
                                inputs[key] = list(input_sources["clip"])

    async def _queue_prompt(self, prompt: dict) -> str:
        """ComfyUI にプロンプトをキュー投入する。"""
        return await self.submit_prompt(prompt)

    async def _wait_for_completion(self, prompt_id: str) -> dict:
        """ジョブ完了を待機してhistoryデータを返す。"""
        recovered = await self.wait_for_terminal(prompt_id)
        if recovered["state"] == "error":
            status = recovered.get("evidence", {}).get("status", {})
            raise ComfyUIError(f"生成失敗: {status.get('messages', [])}")
        return dict(recovered.get("history") or {})

    async def _download_result_image(self, history_entry: dict) -> Path:
        """生成結果の画像をダウンロードしてローカルに保存する。"""
        outputs = history_entry.get("outputs", {})

        # SaveImageノードの出力を探す
        for node_id, output in outputs.items():
            images = output.get("images", [])
            if images:
                img_info = images[0]
                filename = img_info["filename"]
                subfolder = img_info.get("subfolder", "")

                content = await self.view_bytes(
                    {"filename": filename, "subfolder": subfolder, "type": "output"}
                )
                ext = Path(filename).suffix or ".png"
                output_name = f"comfyui_{int(time.time())}_{random.randint(1000, 9999)}{ext}"
                output_path = OUTPUT_DIR / output_name
                output_path.write_bytes(content)
                return output_path.resolve()

        raise ComfyUIError("生成結果に画像が含まれていません")


# ────────────────────────────────────────────
# シングルトンインスタンス管理
# ────────────────────────────────────────────

_instance: Optional[ComfyUIService] = None


def get_comfyui_service(config=None) -> ComfyUIService:
    """ComfyUIServiceのシングルトンを取得する。"""
    global _instance
    if _instance is None:
        if config:
            _instance = ComfyUIService.from_config(config)
        else:
            try:
                from ..config import Config

                _instance = ComfyUIService.from_config(Config())
            except Exception:
                _instance = ComfyUIService()
    elif config:
        comfyui_conf = config.get("comfyui", {}) if hasattr(config, "get") else {}
        _instance.update_config(
            enabled=comfyui_conf.get("enabled", True),
            base_url=comfyui_conf.get("url"),
            default_workflow_path=comfyui_conf.get("default_workflow"),
        )
        if comfyui_conf.get("timeout_seconds"):
            _instance.timeout_seconds = int(comfyui_conf["timeout_seconds"])
    return _instance


async def generate_image(
    prompt: str,
    negative_prompt: str = "",
    workflow_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """ComfyUIで画像生成し、呼び出し元が扱いやすいdictで返す。"""
    service = get_comfyui_service()
    if not service.enabled:
        raise ComfyUIError("ComfyUI連携は設定で無効化されています")
    if not await service.is_available():
        raise ComfyUIError(f"ComfyUIサーバーに接続できません: {service.base_url}")

    image_path = await service.generate_image(
        positive_prompt=prompt,
        negative_prompt=negative_prompt,
        workflow_path=workflow_path,
        overrides=overrides or {},
    )
    filename = Path(image_path).name
    return {
        "success": True,
        "image_path": image_path,
        "image_url": f"/api/generated-images/{filename}",
        "filename": filename,
    }
