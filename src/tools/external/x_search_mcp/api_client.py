"""xAI Grok API 非同期クライアント（X検索用）"""

from __future__ import annotations

import json
import logging
import os
import asyncio
import time
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, List, Optional

import httpx

from ....services.outbound_privacy_service import OutboundPrivacyGateway
from ....services.turn_context import get_turn_context

logger = logging.getLogger("x-search-mcp")

XAI_PROVIDER = "xai"
XAI_REQUEST_TYPE = "search"


def normalize_usage(usage: Any, **kwargs: Any) -> dict[str, Any]:
    """Lazy proxy for the shared provider-usage normalizer.

    Keeping a module-level seam also lets MCP tests replace the normalizer
    without importing all LLM provider clients during process startup.
    """

    from ....llm.conversation_context import normalize_usage as _normalize_usage

    return _normalize_usage(usage, **kwargs)


def persist_usage_sync(*args: Any, **kwargs: Any) -> Any:
    """Lazy proxy for the shared synchronous usage persistence helper."""

    from ....llm.conversation_context import persist_usage_sync as _persist_usage_sync

    return _persist_usage_sync(*args, **kwargs)


@dataclass(frozen=True)
class _XAIUsageContext:
    """Best-effort turn identity for direct HTTP tool calls.

    Direct search tools are intentionally client-agnostic.  When they run
    inside a web turn, the request-scoped ContextVars still carry the durable
    identity; outside that scope all attribution fields remain ``None``.
    """

    user_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    character_name: Optional[str] = None

    @property
    def current_session_id(self) -> Optional[str]:
        return self.session_id

    @property
    def current_project_id(self) -> Optional[str]:
        return self.project_id

    def _get_session_user_id(self) -> Optional[str]:
        return self.user_id


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:
                continue
            if isinstance(dumped, Mapping):
                return dumped
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, Mapping) and raw:
        return raw
    return None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _canonical_tool_name(name: Any) -> str:
    """Map xAI's server-side usage labels to dashboard tool names."""

    value = str(name or "").strip().casefold().replace("-", "_")
    if not value:
        return ""
    if "x_search" in value or value in {
        "xsearch",
        "x_user_search",
        "x_keyword_search",
        "x_semantic_search",
        "x_thread_fetch",
    }:
        return "x_search"
    if "web_search" in value or value in {
        "websearch",
        "web_search_calls",
    }:
        return "web_search"
    if "image_search" in value:
        return "image_search"
    if "code_execution" in value or "code_interpreter" in value:
        return "code_execution"
    if "view_x_video" in value:
        return "view_x_video"
    if "view_image" in value:
        return "view_image"
    if "collection" in value:
        return "collections_search"
    # Unknown server-side categories are still useful to retain for pricing
    # catalog updates, but reject obvious non-tool metadata keys.
    if value.endswith("_calls") or value.startswith("server_side_tool_"):
        return value.removeprefix("server_side_tool_").removesuffix("_calls")
    return ""


def _tool_counts_from_mapping(value: Any) -> dict[str, int]:
    mapping = _mapping(value)
    if not mapping:
        return {}
    counts: dict[str, int] = {}
    for raw_name, raw_count in mapping.items():
        name = _canonical_tool_name(raw_name)
        count = _count(raw_count)
        if not name or not count:
            continue
        counts[name] = counts.get(name, 0) + count
    return counts


def _successful_x_search_calls(payload: Mapping[str, Any]) -> int:
    """Count completed Responses API ``x_search_call`` output items.

    If the API exposes ``server_side_tool_usage`` we prefer that billable
    counter.  This fallback is for direct HTTP responses that only expose the
    Responses output array.
    """

    output = payload.get("output")
    if not isinstance(output, list):
        return 0
    successful = {"", "completed", "succeeded", "success", "done"}
    total = 0
    for item in output:
        item_mapping = _mapping(item)
        if not item_mapping:
            continue
        if str(item_mapping.get("type") or "").casefold() != "x_search_call":
            continue
        status = str(item_mapping.get("status") or "").casefold()
        if status not in successful:
            continue
        total += 1
    return total


def normalize_xai_response_usage(
    payload: Any,
    *,
    normalizer: Any = None,
) -> dict[str, Any]:
    """Normalize a successful xAI Responses JSON envelope for TokenUsage.

    The shared normalizer handles token/cache/reasoning fields.  xAI places
    billable server-side tool counts at the response envelope level, so those
    are merged from ``server_side_tool_usage`` or successful ``x_search_call``
    output items without guessing from failed tool attempts.
    """

    body = _mapping(payload)
    if not body:
        return {}
    raw_usage = body.get("usage")
    if raw_usage is None:
        return {}
    resolved_model = body.get("model")
    normalized = (normalizer or normalize_usage)(
        raw_usage,
        provider=XAI_PROVIDER,
        resolved_model=(str(resolved_model).strip() if resolved_model else None),
    )
    if not normalized:
        return {}
    if normalized.get("input_tokens") is None and normalized.get("output_tokens") is None:
        return {}

    tool_counts: dict[str, int] = {}
    # Prefer a single provider-reported map.  Some proxies mirror the same map
    # both under ``usage`` and at the response top level; summing both would
    # over-count a single request.
    provider_tool_usage = _field(raw_usage, "server_side_tool_usage")
    if not _mapping(provider_tool_usage):
        provider_tool_usage = body.get("server_side_tool_usage")
    if not _mapping(provider_tool_usage):
        provider_tool_usage = body.get("tool_invocations")
    if _mapping(provider_tool_usage):
        # Provider-reported successful counts are authoritative.  Do not add a
        # mirrored generic ``usage.tool_invocations`` map on top of them.
        tool_counts.update(_tool_counts_from_mapping(provider_tool_usage))
    else:
        tool_counts.update(
            _tool_counts_from_mapping(normalized.get("tool_invocations"))
        )
    if not tool_counts:
        fallback_count = _successful_x_search_calls(body)
        if fallback_count:
            tool_counts["x_search"] = fallback_count
    if tool_counts:
        normalized["tool_invocations"] = tool_counts
    return normalized


def _usage_context(context: Any = None) -> Any:
    """Capture only identities explicitly present in current runtime context."""

    if context is not None and (
        hasattr(context, "current_session_id")
        or hasattr(context, "current_project_id")
        or callable(getattr(context, "_get_session_user_id", None))
    ):
        return context

    user_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    try:
        from ....services.turn_context import get_turn_context

        turn = get_turn_context()
        user_id = str(turn.user_id).strip() if turn.user_id else None
        session_id = str(turn.session_id).strip() if turn.session_id else None
        project_id = str(turn.project_id).strip() if turn.project_id else None
    except Exception:
        logger.debug("turn context unavailable for xAI usage", exc_info=True)

    try:
        from ...os_operations.tools import get_current_user_context

        current = get_current_user_context() or {}
        if not user_id and current.get("user_id"):
            user_id = str(current["user_id"]).strip() or None
    except Exception:
        logger.debug("user context unavailable for xAI usage", exc_info=True)

    if not project_id:
        try:
            from ....services.project_context import get_runtime_project_context

            runtime_project = get_runtime_project_context() or {}
            if isinstance(runtime_project, Mapping) and runtime_project.get("id"):
                project_id = str(runtime_project["id"]).strip() or None
        except Exception:
            logger.debug("project context unavailable for xAI usage", exc_info=True)

    return _XAIUsageContext(
        user_id=user_id,
        session_id=session_id,
        project_id=project_id,
    )


def persist_xai_usage_sync(
    usage: Mapping[str, Any],
    *,
    requested_model: str,
    latency_ms: int = 0,
    usage_context: Any = None,
    persistence_fn: Any = None,
) -> bool:
    """Persist one normalized xAI usage payload using the existing sync path."""

    if not usage:
        return False
    try:
        return bool(
            (persistence_fn or persist_usage_sync)(
                _usage_context(usage_context),
                provider=XAI_PROVIDER,
                model=str(requested_model or ""),
                usage=usage,
                request_type=XAI_REQUEST_TYPE,
                latency_ms=max(0, int(latency_ms or 0)),
                is_streaming=False,
            )
        )
    except Exception:
        logger.debug("xAI usage persistence failed", exc_info=True)
        return False


async def persist_xai_usage_async(
    usage: Mapping[str, Any],
    *,
    requested_model: str,
    latency_ms: int = 0,
    usage_context: Any = None,
    persistence_fn: Any = None,
) -> bool:
    """Async bridge for the same existing usage normalization/storage path."""

    if not usage:
        return False
    # persist_usage_sync already owns the project's safe async/sync bridge. Run
    # it off the MCP event loop so a slow DB write never blocks other requests.
    return bool(
        await asyncio.to_thread(
            persist_xai_usage_sync,
            usage,
            requested_model=requested_model,
            latency_ms=latency_ms,
            usage_context=usage_context,
            persistence_fn=persistence_fn,
        )
    )


def parse_handles(raw_value: str) -> List[str]:
    """カンマ区切りのXハンドル文字列をリストに変換する。

    '@'プレフィックスと全角'＠'を正規化して除去する。
    """
    if not raw_value:
        return []
    handles = []
    for part in raw_value.replace("＠", "@").split(","):
        handle = part.strip()
        if not handle:
            continue
        if handle.startswith("@"):
            handle = handle[1:]
        if handle:
            handles.append(handle)
    return handles


def validate_iso_date(value: str, label: str) -> Optional[str]:
    """YYYY-MM-DD形式の日付文字列を検証する。"""
    if not value:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{label}はYYYY-MM-DD形式で指定してください: {value}"
        ) from exc
    return value


def extract_text_from_response(payload: dict) -> str:
    """Grok API レスポンスからテキストを抽出する。

    payload.output_text → output[].content[].text → JSON全体 の順に試行する。
    """
    # 最優先: トップレベル output_text（Responses API の標準フィールド）
    if payload.get("output_text"):
        if isinstance(payload["output_text"], list):
            return "\n".join(str(t) for t in payload["output_text"]).strip()
        return str(payload["output_text"]).strip()

    # output 配列からメッセージを抽出
    outputs = payload.get("output", [])
    if not isinstance(outputs, list):
        outputs = []

    texts: List[str] = []
    for item in outputs:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        content = item.get("content", [])
        # content が文字列の場合
        if isinstance(content, str):
            if content:
                texts.append(content)
            continue
        # content が配列の場合
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, str):
                if block:
                    texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type in ("output_text", "text"):
                text_value = block.get("text") or block.get("output_text")
                if text_value:
                    texts.append(str(text_value))
    if texts:
        return "\n".join(texts).strip()

    return json.dumps(payload, ensure_ascii=False)


class XSearchAPIClient:
    """xAI Grok API を使用した X 検索クライアント"""

    def __init__(self) -> None:
        self.api_key: str = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY", "")
        self.api_base: str = os.getenv("XAI_API_BASE", "https://api.x.ai/v1")
        self.model: str = os.getenv("XAI_GROK_MODEL", "grok-4-0709")
        self._client: Optional[httpx.AsyncClient] = None
        # Keep strong references so object-id reuse cannot turn a later
        # response into a false duplicate.  The list is bounded because a
        # long-lived MCP process may serve many requests.
        self._recorded_usage_responses: list[Any] = []

    def _privacy_gateway(self) -> OutboundPrivacyGateway:
        try:
            from ....config import Config

            config = Config()
        except Exception as exc:
            raise RuntimeError("Grok検索のプライバシー設定を解決できません") from exc
        try:
            turn = get_turn_context()
        except Exception:
            turn = None
        from ....services.outbound_privacy_service import get_privacy_policy_context

        inherited = get_privacy_policy_context()
        return OutboundPrivacyGateway(
            config,
            user_id=str(getattr(turn, "user_id", None) or ""),
            session_id=str(getattr(turn, "session_id", None) or ""),
            session_context=inherited.session_context,
            project_metadata=inherited.project_metadata,
        )

    def _mark_usage_recorded(self, payload: Any) -> bool:
        """Return True when this exact response payload was already persisted."""

        if any(item is payload for item in self._recorded_usage_responses):
            return True
        self._recorded_usage_responses.append(payload)
        del self._recorded_usage_responses[:-8]
        return False

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self.headers,
                timeout=60.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        system_prompt: str,
        max_results: int = 8,
        allowed_handles: Optional[List[str]] = None,
        excluded_handles: Optional[List[str]] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        freshness: str = "auto",
        search_mode: str = "auto",
        language: str = "ja",
        enable_image_understanding: bool = False,
        enable_video_understanding: bool = False,
        temperature: float = 0.2,
        max_output_tokens: int = 900,
        timeout_seconds: int = 45,
    ) -> str:
        """Grok API の x_search ツールを使用して X を検索する。

        Returns:
            検索結果のテキスト、またはエラーメッセージ。
        """
        if not self.api_key:
            return "XAI_API_KEY (またはGROK_API_KEY) が設定されていません。"

        tool_config: dict = {
            "max_results": max_results,
            "search_mode": search_mode,
            "freshness": freshness,
            "language": language,
        }
        if allowed_handles:
            tool_config["allowed_x_handles"] = allowed_handles
        if excluded_handles:
            tool_config["excluded_x_handles"] = excluded_handles
        if from_date:
            tool_config["from_date"] = from_date
        if to_date:
            tool_config["to_date"] = to_date
        if enable_image_understanding:
            tool_config["enable_image_understanding"] = True
        if enable_video_understanding:
            tool_config["enable_video_understanding"] = True

        tool_entry = {"type": "x_search", "x_search": tool_config}

        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query.strip()},
            ],
            "tools": [tool_entry],
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }

        url = f"{self.api_base.rstrip('/')}/responses"

        # This MCP client is itself an external egress boundary.  The shared
        # contextvar carries session/project policy when invoked from an
        # Agent Team turn; direct mode remains a no-op for compatibility.
        # Effective session/project policy is inherited from the privacy
        # context; do not bypass its review bridge with an auto-approve hook.
        try:
            gateway = self._privacy_gateway()
        except Exception as exc:
            return f"Grok検索はプライバシー設定を解決できないため停止しました: {exc}"
        try:
            protected = await gateway.protect(
                payload,
                provider="grok",
                base_url=self.api_base,
                source_kind="grok_x_search_mcp",
                model=self.model,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Grok検索はプライバシーポリシーにより停止しました: {exc}"
        payload = protected.payload

        started_at = time.monotonic()
        try:
            client = await self._get_client()
            response = await client.post(
                url, json=payload, timeout=timeout_seconds
            )
        except httpx.RequestError as exc:
            return f"Grok APIへの接続に失敗しました: {exc}"

        if response.status_code >= 300:
            try:
                error_payload = response.json()
                err = error_payload.get("error", "")
                if isinstance(err, dict):
                    error_message = err.get("message", "") or json.dumps(err, ensure_ascii=False)
                elif isinstance(err, str) and err:
                    error_message = err
                else:
                    error_message = json.dumps(error_payload, ensure_ascii=False)
            except Exception:
                error_message = response.text
            return f"Grok APIエラー({response.status_code}): {error_message}"

        try:
            response_payload = response.json()
        except ValueError:
            return f"Grok APIの応答を解析できませんでした: {response.text}"

        # Record only successful Responses envelopes with provider-reported
        # usage.  No synthetic zero row is created when ``usage`` is absent.
        usage = normalize_xai_response_usage(response_payload)
        if usage and not self._mark_usage_recorded(response_payload):
            latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
            try:
                await persist_xai_usage_async(
                    usage,
                    requested_model=self.model,
                    latency_ms=latency_ms,
                )
            except Exception:
                logger.debug("xAI usage persistence failed", exc_info=True)

        text = extract_text_from_response(response_payload)
        if not text:
            text = json.dumps(response_payload, ensure_ascii=False)
        return str(gateway.restore_aliases(text))
