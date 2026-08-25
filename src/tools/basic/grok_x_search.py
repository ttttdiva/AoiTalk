"""Grok (xAI) X search tool integration."""
import os
import json
import time
from datetime import datetime
from typing import List, Optional

import requests

from ..external_llm_permission import check_permission_sync
from ..external.x_search_mcp.api_client import (
    normalize_usage,
    normalize_xai_response_usage,
    persist_xai_usage_sync as _persist_xai_usage_sync,
)
from ...services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    get_privacy_policy_context,
)
from ...services.turn_context import get_turn_context

from ..core import tool
from .x_search import (
    format_yahoo_x_results,
    search_yahoo_realtime_sync,
    yahoo_posts,
    yahoo_result_has_results,
)


def persist_usage_sync(*args, **kwargs):
    """Lazy seam for tests and the shared TokenUsage persistence path."""

    from ...llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))


def persist_xai_usage_sync(usage, *, requested_model, latency_ms=0):
    """Persist xAI usage through this module's patchable sync seam."""

    return _persist_xai_usage_sync(
        usage,
        requested_model=requested_model,
        latency_ms=latency_ms,
        persistence_fn=persist_usage_sync,
    )

XAI_API_BASE = os.getenv('XAI_API_BASE', 'https://api.x.ai/v1')
XAI_DEFAULT_MODEL = os.getenv('XAI_GROK_MODEL', 'grok-4-0709')
XAI_SYSTEM_PROMPT = (
    "あなたは速報性の高いニュースリサーチャーです。"
    "GrokのX検索ツールで取得した投稿の要点を2〜4個の箇条書きでまとめ、"
    "最終行に投稿へのURLまたはハンドルを提示してください。"
)

_RECORDED_USAGE_RESPONSES: list[object] = []


def _grok_privacy_gateway() -> OutboundPrivacyGateway:
    try:
        from ...config import Config

        config = Config()
    except Exception as exc:
        raise RuntimeError("Grok検索のプライバシー設定を解決できません") from exc
    try:
        turn = get_turn_context()
    except Exception:
        turn = None
    inherited = get_privacy_policy_context()
    return OutboundPrivacyGateway(
        config,
        user_id=str(getattr(turn, "user_id", None) or ""),
        session_id=str(getattr(turn, "session_id", None) or ""),
        session_context=inherited.session_context,
        project_metadata=inherited.project_metadata,
    )


def _mark_usage_recorded(payload: object) -> bool:
    """Prevent accidental duplicate dashboard rows for one HTTP response."""

    if any(item is payload for item in _RECORDED_USAGE_RESPONSES):
        return True
    _RECORDED_USAGE_RESPONSES.append(payload)
    del _RECORDED_USAGE_RESPONSES[:-8]
    return False


def _parse_handles(raw_value: str) -> List[str]:
    """Normalize comma-separated handles into a clean list."""
    if not raw_value:
        return []
    handles = []
    for part in raw_value.replace('＠', '@').split(','):
        handle = part.strip()
        if not handle:
            continue
        if handle.startswith('@'):
            handle = handle[1:]
        if handle:
            handles.append(handle)
    return handles


def _validate_iso_date(value: str, label: str) -> Optional[str]:
    """Validate YYYY-MM-DD dates accepted by Grok."""
    if not value:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - simple validation
        raise ValueError(f"{label}はYYYY-MM-DD形式で指定してください: {value}") from exc
    return value


def _extract_text_from_response(payload: dict) -> str:
    """Extract assistant text from Grok response payload."""
    # 最優先: トップレベル output_text
    if payload.get('output_text'):
        if isinstance(payload['output_text'], list):
            return '\n'.join(str(t) for t in payload['output_text']).strip()
        return str(payload['output_text']).strip()
    # output 配列からメッセージを抽出
    outputs = payload.get('output', [])
    if not isinstance(outputs, list):
        outputs = []
    texts: List[str] = []
    for item in outputs:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        if item.get('type') != 'message':
            continue
        content = item.get('content', [])
        if isinstance(content, str):
            if content:
                texts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, str):
                if block:
                    texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = block.get('type', '')
            if block_type in ('output_text', 'text'):
                text_value = block.get('text') or block.get('output_text')
                if text_value:
                    texts.append(str(text_value))
    if texts:
        return '\n'.join(texts).strip()
    return json.dumps(payload, ensure_ascii=False)


def _build_tool_config(
    allowed_handles: List[str],
    excluded_handles: List[str],
    from_date: Optional[str],
    to_date: Optional[str],
    enable_image_understanding: bool,
    enable_video_understanding: bool,
    max_results: int,
    freshness: str,
    search_mode: str,
    language: str,
) -> dict:
    tool_config = {
        'max_results': max_results,
        'search_mode': search_mode,
        'freshness': freshness,
        'language': language,
    }
    if allowed_handles:
        tool_config['allowed_x_handles'] = allowed_handles
    if excluded_handles:
        tool_config['excluded_x_handles'] = excluded_handles
    if from_date:
        tool_config['from_date'] = from_date
    if to_date:
        tool_config['to_date'] = to_date
    if enable_image_understanding:
        tool_config['enable_image_understanding'] = True
    if enable_video_understanding:
        tool_config['enable_video_understanding'] = True
    return tool_config


def _try_yahoo_x_search(
    query: str,
    *,
    max_results: int,
    timeout_seconds: int,
    privacy_gateway=None,
):
    """Fetch canonical Yahoo posts before spending an xAI request.

    This helper is deliberately patchable for tests and keeps Grok's existing
    transport untouched.  A Yahoo error is a soft failure: the legacy Grok
    path remains available when its key is configured.
    """

    # ``grok_x_search`` is itself an explicit X-search operation.  It must
    # never bypass the canonical Yahoo first hop, even for terse keywords such
    # as ``xAI``; the caller has already selected the X-specific tool.
    try:
        gateway = privacy_gateway
        if gateway is None:
            try:
                gateway = _grok_privacy_gateway()
            except Exception as exc:  # privacy failure is a soft Yahoo miss
                print(f"[Tool] Yahoo X検索のプライバシー設定を解決できません: {exc}")
                return None
        return search_yahoo_realtime_sync(
            query,
            max_results=max_results,
            timeout_seconds=max(1, min(int(timeout_seconds), 45)),
            privacy_gateway=gateway,
        )
    except Exception as exc:  # noqa: BLE001 - supplement/fallback boundary
        print(f"[Tool] Yahoo X検索をスキップしました: {exc}")
        return None


def _yahoo_is_sufficient(result, *, max_results: int) -> bool:
    posts = yahoo_posts(result)
    if not posts or not yahoo_result_has_results(result):
        return False
    return len(posts) >= max(1, min(int(max_results), 25))


@tool
def grok_x_search(
    query: str,
    max_results: int = 8,
    allowed_x_handles: str = '',
    excluded_x_handles: str = '',
    from_date: str = '',
    to_date: str = '',
    freshness: str = 'auto',
    search_mode: str = 'auto',
    language: str = 'ja',
    enable_image_understanding: bool = False,
    enable_video_understanding: bool = False,
    temperature: float = 0.2,
    max_output_tokens: int = 900,
    timeout_seconds: int = 45,
) -> str:
    """Grok 4.1のX検索ツールで最新ポストを調査します"""
    print(f"[Tool] grok_x_search called: query_chars={len(str(query or ''))} max_results={max_results}")

    if not query or not query.strip():
        return "検索クエリを指定してください。"

    if max_results < 1 or max_results > 25:
        return "max_resultsは1〜25の範囲で指定してください。"

    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        return "temperatureは数値で指定してください。"
    temperature = max(0.0, min(1.0, temperature))

    allowed_handles = _parse_handles(allowed_x_handles)
    excluded_handles = _parse_handles(excluded_x_handles)
    if allowed_handles and excluded_handles:
        return "allowed_x_handlesとexcluded_x_handlesは同時に指定できません。"

    try:
        parsed_from_date = _validate_iso_date(from_date, 'from_date')
        parsed_to_date = _validate_iso_date(to_date, 'to_date')
    except ValueError as exc:
        return str(exc)

    freshness = freshness.lower() if freshness else 'auto'
    search_mode = search_mode.lower() if search_mode else 'auto'

    # Yahoo is the canonical, keyless backend.  Return it directly when it
    # satisfies the requested count; otherwise retain it as context while
    # optionally asking Grok for a supplement.  This call intentionally occurs
    # before checking XAI_API_KEY so a Yahoo-only result never requires xAI.
    yahoo_result = _try_yahoo_x_search(
        query.strip(),
        max_results=max_results,
        timeout_seconds=timeout_seconds,
    )
    yahoo_text = (
        format_yahoo_x_results(query, yahoo_result, max_results=max_results)
        if yahoo_result_has_results(yahoo_result)
        else ""
    )
    if _yahoo_is_sufficient(yahoo_result, max_results=max_results):
        return yahoo_text

    api_key = os.getenv('XAI_API_KEY') or os.getenv('GROK_API_KEY')
    if not api_key:
        # A partial Yahoo response is still useful and should not be replaced
        # by a missing-key error.  Preserve the old error for a true miss.
        if yahoo_text:
            return yahoo_text
        return "Grok X検索を使うにはXAI_API_KEY (またはGROK_API_KEY) を設定してください。"

    tool_entry = {
        'type': 'x_search',
        'x_search': _build_tool_config(
            allowed_handles,
            excluded_handles,
            parsed_from_date,
            parsed_to_date,
            enable_image_understanding,
            enable_video_understanding,
            max_results,
            freshness,
            search_mode,
            language,
        )
    }

    payload = {
        'model': XAI_DEFAULT_MODEL,
        'input': [
            {'role': 'system', 'content': XAI_SYSTEM_PROMPT},
            {'role': 'user', 'content': query.strip()},
        ],
        'tools': [tool_entry],
        'temperature': temperature,
        'max_output_tokens': max_output_tokens,
    }

    # X search is an external egress boundary.  Keep aliases in the query
    # until this boundary and apply the shared gateway immediately before the
    # HTTP request; local_only therefore fails closed without contacting xAI.
    # Never install an auto-approve callback here.  The gateway inherits the
    # effective request/session policy through its context and must invoke the
    # configured review bridge (or fail closed) for protected high-risk work.
    try:
        gateway = _grok_privacy_gateway()
    except Exception as exc:
        return f"Grok検索はプライバシー設定を解決できないため停止しました: {exc}"
    try:
        protected = gateway.protect_sync(
            payload,
            provider="grok",
            base_url=XAI_API_BASE,
            source_kind="grok_x_search",
            model=XAI_DEFAULT_MODEL,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Grok検索はプライバシーポリシーにより停止しました: {exc}"
    payload = protected.payload

    # Permission UI receives only the gateway output.  Raw search terms must
    # never be exposed to the tool-confirmation surface before redaction.
    try:
        permission_query = payload["input"][-1].get("content", "")
    except (KeyError, IndexError, AttributeError, TypeError):
        permission_query = ""
    approved = check_permission_sync(
        tool_name="grok_x_search",
        tool_args={"query": permission_query, "max_results": max_results},
        description=f"X (Twitter) 検索: 「{permission_query}」",
    )
    if not approved:
        return "ユーザーによってX検索がキャンセルされました。"

    url = f"{XAI_API_BASE.rstrip('/')}/responses"
    headers = {
        'Authorization': f"Bearer {api_key}",
        'Content-Type': 'application/json'
    }

    started_at = time.monotonic()
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
    except requests.RequestException as exc:
        return f"Grok APIへの接続に失敗しました: {exc}"

    if response.status_code >= 300:
        try:
            error_payload = response.json()
            err = error_payload.get('error', '')
            if isinstance(err, dict):
                error_message = err.get('message', '') or json.dumps(err, ensure_ascii=False)
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

    # xAI Responses returns usage only on successful provider responses.  Keep
    # the direct tool on the same normalization/storage path as native LLM
    # clients, while treating persistence as best-effort for tool UX.
    usage = normalize_xai_response_usage(
        response_payload,
        normalizer=normalize_usage,
    )
    if usage and not _mark_usage_recorded(response_payload):
        try:
            persist_xai_usage_sync(
                usage,
                requested_model=XAI_DEFAULT_MODEL,
                latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            )
        except Exception:
            # A dashboard/database outage must not hide a successful search.
            pass

    text = _extract_text_from_response(response_payload)
    text = str(gateway.restore_aliases(text))
    if not text:
        text = json.dumps(response_payload, ensure_ascii=False)

    if yahoo_text:
        return f"{yahoo_text}\n\nGrok補足:\n{text}"

    return text


__all__ = ['grok_x_search']
