"""xAI Grok API 非同期クライアント（X検索用）"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import List, Optional

import httpx

logger = logging.getLogger("x-search-mcp")


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

        text = extract_text_from_response(response_payload)
        if not text:
            text = json.dumps(response_payload, ensure_ascii=False)

        return text
