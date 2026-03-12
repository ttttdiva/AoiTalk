"""
ClickUp タスク PostgreSQL 読み取りクライアント

clickup_tasks テーブルからキャッシュされたタスク情報を取得する。
DB接続に失敗してもサーバーは動作を継続する (graceful degradation)。
"""

import os
import logging
from typing import Optional

logger = logging.getLogger("clickup-mcp.db")


class ClickUpDBClient:
    """PostgreSQL 読み取り専用クライアント。

    psycopg2 を直接使用し、SQLAlchemy に依存しない軽量実装。
    clickup_tasks テーブルのスキーマは src/memory/models.py:ClickUpTask に準拠。
    """

    def __init__(self):
        self.enabled = False
        self._dsn: Optional[str] = None
        self._setup()

    def _setup(self):
        host = os.getenv("POSTGRES_HOST", "127.0.0.1")
        port = os.getenv("POSTGRES_PORT", "5432")
        db = os.getenv("POSTGRES_DB", "aoitalk_memory")
        user = os.getenv("POSTGRES_USER", "aoitalk")
        password = os.getenv("POSTGRES_PASSWORD", "")

        if not password:
            logger.info("POSTGRES_PASSWORD 未設定 — DB機能は無効")
            return

        self._dsn = (
            f"host={host} port={port} dbname={db} user={user} password={password}"
        )

        try:
            import psycopg2

            conn = psycopg2.connect(self._dsn)
            conn.close()
            self.enabled = True
            logger.info("PostgreSQL 接続確認OK")
        except Exception as e:
            logger.warning(f"PostgreSQL 接続失敗、DB機能無効: {e}")
            self._dsn = None

    def _connect(self):
        """新しい接続を返す。呼び出し側で close すること。"""
        import psycopg2

        return psycopg2.connect(self._dsn)

    def get_task_summary(self) -> Optional[str]:
        """clickup_tasks テーブルからタスクサマリーを取得する。

        Returns:
            ステータス別タスク数の日本語文字列。DB無効時は None。
        """
        if not self.enabled:
            return None

        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, COUNT(*) FROM clickup_tasks "
                        "GROUP BY status ORDER BY COUNT(*) DESC"
                    )
                    rows = cur.fetchall()

                    if not rows:
                        return None

                    total = sum(count for _, count in rows)
                    lines = [f"現在のタスク状況 (合計: {total}件)"]
                    for status, count in rows:
                        lines.append(f"- {status}: {count}件")

                    # 最終同期時刻
                    cur.execute(
                        "SELECT MAX(last_synced) FROM clickup_tasks"
                    )
                    last_synced = cur.fetchone()[0]
                    if last_synced:
                        lines.append(f"\n最終同期: {last_synced.strftime('%Y-%m-%d %H:%M')}")

                    return "\n".join(lines)
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"タスクサマリー取得失敗: {e}")
            return None

    def get_weekly_tasks(self) -> Optional[list[dict]]:
        """直近1週間のタスクをDBから取得する。

        Returns:
            タスク辞書のリスト。DB無効時は None。
        """
        if not self.enabled:
            return None

        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, name, status, priority,
                               start_date, due_date, list_name,
                               folder_name, space_name,
                               assignee_names, tags
                        FROM clickup_tasks
                        ORDER BY due_date ASC NULLS LAST, date_updated DESC
                        """
                    )
                    columns = [desc[0] for desc in cur.description]
                    return [dict(zip(columns, row)) for row in cur.fetchall()]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"週次タスク取得失敗: {e}")
            return None

    def search_tasks_by_name(self, query: str) -> Optional[list[dict]]:
        """タスク名でローカルDBを部分一致検索する。

        Args:
            query: 検索クエリ文字列

        Returns:
            マッチしたタスク辞書のリスト。DB無効時は None。
        """
        if not self.enabled:
            return None

        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, name, status, priority,
                               due_date, list_name, space_name
                        FROM clickup_tasks
                        WHERE name ILIKE %s
                        ORDER BY date_updated DESC
                        LIMIT 20
                        """,
                        (f"%{query}%",),
                    )
                    columns = [desc[0] for desc in cur.description]
                    return [dict(zip(columns, row)) for row in cur.fetchall()]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"タスク名検索失敗: {e}")
            return None
