"""
ClickUp API 非同期クライアント

httpx.AsyncClient を使用した ClickUp API v2 クライアント。
日時変換ユーティリティも含む。
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("clickup-mcp.api")

# ClickUp API v2 ベースURL
BASE_URL = "https://api.clickup.com/api/v2"


def parse_datetime_to_ms(
    date_str: Optional[str] = None,
    time_str: Optional[str] = None,
) -> tuple[Optional[int], bool]:
    """日付・時刻文字列を ClickUp API 用ミリ秒タイムスタンプに変換する。

    Args:
        date_str: 日付文字列 (YYYY-MM-DD)
        time_str: 時刻文字列 (HH:MM) — 省略可

    Returns:
        (timestamp_ms, has_time) のタプル。
        パース失敗時は (None, False)。
    """
    if not date_str:
        return None, False

    date_str = date_str.strip()

    # 日付+時刻
    if time_str:
        time_str = time_str.strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", fmt)
                return int(dt.timestamp() * 1000), True
            except ValueError:
                continue

    # ISO形式 (T区切り)
    for fmt, has_time in (
        ("%Y-%m-%dT%H:%M:%S", True),
        ("%Y-%m-%dT%H:%M", True),
        ("%Y-%m-%d %H:%M:%S", True),
        ("%Y-%m-%d %H:%M", True),
        ("%Y-%m-%d", False),
        ("%Y/%m/%d %H:%M:%S", True),
        ("%Y/%m/%d %H:%M", True),
        ("%Y/%m/%d", False),
    ):
        try:
            dt = datetime.strptime(date_str, fmt)
            return int(dt.timestamp() * 1000), has_time
        except ValueError:
            continue

    logger.warning(f"日時パース失敗: date={date_str}, time={time_str}")
    return None, False


class ClickUpAPIClient:
    """ClickUp API v2 非同期クライアント。

    環境変数:
        CLICKUP_API_KEY: APIキー (必須)
        CLICKUP_TEAM_ID: チームID (必須)
        CLICKUP_DEFAULT_LIST_ID: デフォルトリストID (任意)
    """

    def __init__(self):
        self.api_key: str = os.getenv("CLICKUP_API_KEY", "")
        self.team_id: str = os.getenv("CLICKUP_TEAM_ID", "")
        self.default_list_id: str = os.getenv(
            "CLICKUP_DEFAULT_LIST_ID", ""
        )
        self._client: Optional[httpx.AsyncClient] = None

        if not self.api_key:
            logger.warning("CLICKUP_API_KEY が設定されていません")
        if not self.team_id:
            logger.warning("CLICKUP_TEAM_ID が設定されていません")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                headers=self.headers,
                timeout=30.0,
            )
        return self._client

    async def get(
        self, path: str, params: Optional[dict] = None
    ) -> httpx.Response:
        client = await self._get_client()
        return await client.get(path, params=params)

    async def post(
        self, path: str, json: Optional[dict] = None
    ) -> httpx.Response:
        client = await self._get_client()
        return await client.post(path, json=json)

    async def put(
        self, path: str, json: Optional[dict] = None
    ) -> httpx.Response:
        client = await self._get_client()
        return await client.put(path, json=json)

    async def delete(self, path: str) -> httpx.Response:
        client = await self._get_client()
        return await client.delete(path)

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
