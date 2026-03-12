"""Grok (xAI) X search tool integration."""
import asyncio
import os
import json
from datetime import datetime
from typing import List, Optional

import requests

from ..external_llm_permission import check_permission

from ..core import tool as function_tool

XAI_API_BASE = os.getenv('XAI_API_BASE', 'https://api.x.ai/v1')
XAI_DEFAULT_MODEL = os.getenv('XAI_GROK_MODEL', 'grok-4-0709')
XAI_SYSTEM_PROMPT = (
    "あなたは速報性の高いニュースリサーチャーです。"
    "GrokのX検索ツールで取得した投稿の要点を2〜4個の箇条書きでまとめ、"
    "最終行に投稿へのURLまたはハンドルを提示してください。"
)


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


@function_tool
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
    print(f"[Tool] grok_x_search called: query='{query}' max_results={max_results}")
    
    # Check user permission for external LLM API call
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(
                    asyncio.run,
                    check_permission(
                        tool_name="grok_x_search",
                        tool_args={"query": query, "max_results": max_results},
                        description=f"X (Twitter) 検索: 「{query}」"
                    )
                )
                approved = future.result(timeout=60)
        else:
            approved = asyncio.run(
                check_permission(
                    tool_name="grok_x_search",
                    tool_args={"query": query, "max_results": max_results},
                    description=f"X (Twitter) 検索: 「{query}」"
                )
            )
    except RuntimeError:
        approved = True  # Fallback to allow if async context unavailable
    
    if not approved:
        return "ユーザーによってX検索がキャンセルされました。"

    api_key = os.getenv('XAI_API_KEY') or os.getenv('GROK_API_KEY')
    if not api_key:
        return "Grok X検索を使うにはXAI_API_KEY (またはGROK_API_KEY) を設定してください。"

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

    url = f"{XAI_API_BASE.rstrip('/')}/responses"
    headers = {
        'Authorization': f"Bearer {api_key}",
        'Content-Type': 'application/json'
    }

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

    text = _extract_text_from_response(response_payload)
    if not text:
        text = json.dumps(response_payload, ensure_ascii=False)

    return text


__all__ = ['grok_x_search']
