# -*- coding: utf-8 -*-
"""Alembic（実DB）を正本とした Drizzle スキーマドリフト検知スクリプト.

目的:
    同一 PostgreSQL を Python 側 Alembic と frontend/src/db/schema.ts（Drizzle）が
    二重定義している。正本は Alembic（=実DBスキーマ）と定め、schema.ts が実DBと
    食い違ったら検知して exit 1 で失敗させる。

使い方:
    venv\\Scripts\\python.exe scripts/check_schema_drift.py

動作:
    - .env の POSTGRES_* / DATABASE_URL から接続情報を取得（読み取り専用）。
    - frontend/src/db/schema.ts を正規表現ベースでパースし、pgTable 名・列名・
      SQL 型・nullable・default 有無を抽出。
    - information_schema.columns と比較。schema.ts に定義されたテーブルのみ対象。
    - 列の過不足・型不一致・nullable 不一致を報告。不一致があれば exit 1。

比較の除外項目（誤検知しやすいため pass/fail に含めない）:
    - default 式の内容・有無（$defaultFn はアプリ側デフォルトで DB default を作らない、
      Alembic 側の server_default 表現差、gen_random_uuid() 等が false positive の温床）。
      → default は情報として出力するが判定には使わない。
    - schema.ts に無いテーブル（Alembic 側にしかないテーブル）は対象外。
    - インデックス・ユニーク制約・外部キー・主キー構成そのものは比較対象外
      （列の nullable としては primaryKey/notNull を反映）。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_TS = REPO_ROOT / "frontend" / "src" / "db" / "schema.ts"


# ─── Drizzle 型 → PostgreSQL(information_schema.data_type) 正規化表 ───
# information_schema.columns.data_type の表現に合わせる。
def drizzle_to_pg(drizzle_type: str, *, with_timezone: bool) -> str:
    mapping = {
        "uuid": "uuid",
        "varchar": "character varying",
        "text": "text",
        "boolean": "boolean",
        "date": "date",
        "json": "json",
        "jsonb": "jsonb",
        "integer": "integer",
        "bigint": "bigint",
        "serial": "integer",
        "bigserial": "bigint",
        "doublePrecision": "double precision",
        "real": "real",
        "numeric": "numeric",
        "decimal": "numeric",
        "smallint": "smallint",
    }
    if drizzle_type == "timestamp":
        return "timestamp with time zone" if with_timezone else "timestamp without time zone"
    return mapping.get(drizzle_type, drizzle_type)


COLUMN_TYPES = (
    "uuid|varchar|text|boolean|timestamp|date|jsonb|json|integer|bigint|"
    "serial|bigserial|doublePrecision|real|numeric|decimal|smallint"
)


def _match_block(text: str, open_idx: int) -> tuple[int, int]:
    """open_idx（'{' の位置）から対応する '}' までの範囲を返す."""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
        i += 1
    raise ValueError("対応する '}' が見つかりません")


def parse_schema_ts(source: str) -> dict[str, dict[str, dict]]:
    """schema.ts をパースして {table_name: {column_name: {info}}} を返す."""
    tables: dict[str, dict[str, dict]] = {}
    # pgTable("name", { ... }) を探す。名前は最初の文字列リテラル。
    for m in re.finditer(r'pgTable\(\s*"([^"]+)"\s*,', source):
        table_name = m.group(1)
        # テーブル名の後、最初の '{' を探す（列定義オブジェクト）
        brace_start = source.find("{", m.end())
        if brace_start == -1:
            continue
        start, end = _match_block(source, brace_start)
        block = source[start + 1 : end]
        tables[table_name] = parse_columns(block)
    return tables


def parse_columns(block: str) -> dict[str, dict]:
    """列定義オブジェクトの中身から各列情報を抽出する."""
    columns: dict[str, dict] = {}
    col_re = re.compile(
        r'(\w+)\s*:\s*(' + COLUMN_TYPES + r')\(\s*"([^"]+)"',
    )
    matches = list(col_re.finditer(block))
    for idx, cm in enumerate(matches):
        js_name = cm.group(1)
        drizzle_type = cm.group(2)
        col_name = cm.group(3)
        # この列定義のチェーン範囲 = この match 開始〜次の match 開始
        seg_start = cm.start()
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(block)
        segment = block[seg_start:seg_end]

        with_timezone = "withTimezone: true" in segment
        is_primary = ".primaryKey(" in segment
        not_null = is_primary or ".notNull(" in segment
        # DB default: .default( / .defaultNow( のみ（$defaultFn はアプリ側なので除外）
        has_db_default = bool(re.search(r"\.default(Now)?\(", segment))

        columns[col_name] = {
            "js_name": js_name,
            "drizzle_type": drizzle_type,
            "pg_type": drizzle_to_pg(drizzle_type, with_timezone=with_timezone),
            "not_null": not_null,
            "has_default": has_db_default,
        }
    return columns


def get_conn():
    load_dotenv(REPO_ROOT / ".env")
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "aoitalk"),
        password=os.getenv("POSTGRES_PASSWORD"),
        dbname=os.getenv("POSTGRES_DB", "aoitalk_memory"),
    )


def fetch_db_columns(conn, table_names: list[str]) -> dict[str, dict[str, dict]]:
    """information_schema から対象テーブルの列情報を取得（読み取りのみ）."""
    result: dict[str, dict[str, dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(%s)
            ORDER BY table_name, ordinal_position
            """,
            (table_names,),
        )
        for table_name, column_name, data_type, is_nullable, column_default in cur.fetchall():
            result.setdefault(table_name, {})[column_name] = {
                "pg_type": data_type,
                "not_null": is_nullable == "NO",
                "has_default": column_default is not None,
            }
    return result


def main() -> int:
    if not SCHEMA_TS.exists():
        print(f"schema.ts が見つかりません: {SCHEMA_TS}", file=sys.stderr)
        return 2

    source = SCHEMA_TS.read_text(encoding="utf-8")
    ts_tables = parse_schema_ts(source)
    table_names = sorted(ts_tables.keys())
    print(f"schema.ts から {len(table_names)} テーブルを検出")

    conn = get_conn()
    try:
        db_tables = fetch_db_columns(conn, table_names)
    finally:
        conn.close()

    drift_count = 0
    missing_tables: list[str] = []

    for table in table_names:
        ts_cols = ts_tables[table]
        db_cols = db_tables.get(table)
        if db_cols is None:
            missing_tables.append(table)
            print(f"[テーブル欠落] {table}: 実DBに存在しません")
            drift_count += 1
            continue

        ts_col_names = set(ts_cols.keys())
        db_col_names = set(db_cols.keys())

        # schema.ts にあって DB に無い列（列欠落）
        for col in sorted(ts_col_names - db_col_names):
            print(f"[列欠落] {table}.{col}: schema.ts で定義されているが実DBに存在しない")
            drift_count += 1

        # DB にあって schema.ts に無い列（列余剰）
        for col in sorted(db_col_names - ts_col_names):
            db_info = db_cols[col]
            print(
                f"[列余剰] {table}.{col}: 実DBに存在するが schema.ts 未定義 "
                f"(type={db_info['pg_type']}, nullable={not db_info['not_null']})"
            )
            drift_count += 1

        # 共通列の型・nullable 比較
        for col in sorted(ts_col_names & db_col_names):
            ts_info = ts_cols[col]
            db_info = db_cols[col]
            if ts_info["pg_type"] != db_info["pg_type"]:
                print(
                    f"[型不一致] {table}.{col}: "
                    f"schema.ts={ts_info['pg_type']}（{ts_info['drizzle_type']}）"
                    f" vs 実DB={db_info['pg_type']}"
                )
                drift_count += 1
            if ts_info["not_null"] != db_info["not_null"]:
                print(
                    f"[nullable不一致] {table}.{col}: "
                    f"schema.ts notNull={ts_info['not_null']}"
                    f" vs 実DB notNull={db_info['not_null']}"
                )
                drift_count += 1

    print("")
    if drift_count == 0:
        print("ドリフトなし: schema.ts は実DB（Alembic適用結果）と整合しています")
        return 0
    print(f"ドリフト検出: {drift_count} 件（正本=Alembic、schema.ts 側を修正してください）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
