"""StoryCharacter の aliases / keywords 破損修復スクリプト。

デフォルトは dry-run。``--apply`` で DB 更新、``--verify`` で修復後確認。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.story_character_fields import (  # noqa: E402
    dedupe_keywords,
    is_corrupted_char_split_array,
    normalize_character_aliases_keywords,
    repair_char_split_array,
)

load_dotenv(ROOT / ".env")


def _summarize(values: Any) -> str:
    if values is None:
        return "null"
    try:
        text = json.dumps(values, ensure_ascii=False)
    except TypeError:
        text = repr(values)
    return text if len(text) <= 120 else f"{text[:117]}..."


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        user=os.getenv("POSTGRES_USER", "aoitalk"),
        password=os.getenv("POSTGRES_PASSWORD"),
        database=os.getenv("POSTGRES_DB", "aoitalk_memory"),
    )


async def _load_characters(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, name, aliases, keywords
        FROM story_characters
        WHERE archived_at IS NULL
        ORDER BY name
        """
    )


def _coerce_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _needs_repair(name: str, aliases: Any, keywords: Any) -> tuple[bool, list[str], list[str], list[str]]:
    reasons: list[str] = []
    next_aliases = _coerce_json_list(aliases)
    next_keywords = _coerce_json_list(keywords)

    if is_corrupted_char_split_array(next_aliases):
        repaired = repair_char_split_array(next_aliases)
        if repaired is not None:
            reasons.append("aliases 破損形状")
            next_aliases = repaired

    if is_corrupted_char_split_array(next_keywords):
        repaired = repair_char_split_array(next_keywords)
        if repaired is not None:
            reasons.append("keywords 破損形状")
            next_keywords = repaired

    normalized_aliases, normalized_keywords = normalize_character_aliases_keywords(
        name,
        next_aliases,
        next_keywords,
        strict=False,
    )
    if normalized_aliases != next_aliases:
        reasons.append("aliases 正規化")
    if normalized_keywords != next_keywords:
        reasons.append("keywords 正規化")

    deduped_keywords = dedupe_keywords(name, normalized_aliases, normalized_keywords)
    if deduped_keywords != normalized_keywords:
        reasons.append("keywords 冗長削除")
        normalized_keywords = deduped_keywords

    return bool(reasons), normalized_aliases, normalized_keywords, reasons


async def run(*, apply: bool, verify: bool) -> int:
    conn = await _connect()
    try:
        rows = await _load_characters(conn)
        candidates: list[tuple[str, str, Any, Any, list[str], list[str], list[str]]] = []
        for row in rows:
            name = str(row["name"] or "")
            aliases = row["aliases"]
            keywords = row["keywords"]
            needs, next_aliases, next_keywords, reasons = _needs_repair(name, aliases, keywords)
            if needs:
                candidates.append(
                    (
                        str(row["id"]),
                        name,
                        aliases,
                        keywords,
                        next_aliases,
                        next_keywords,
                        reasons,
                    )
                )

        print(f"対象件数: {len(candidates)} / 全 {len(rows)} 件")
        for character_id, name, before_aliases, before_keywords, next_aliases, next_keywords, reasons in candidates:
            print(f"- ID={character_id} name={name!r} ({', '.join(reasons)})")
            print(f"  aliases: {_summarize(before_aliases)} -> {_summarize(next_aliases)}")
            print(f"  keywords: {_summarize(before_keywords)} -> {_summarize(next_keywords)}")

        if apply and candidates:
            async with conn.transaction():
                for character_id, _name, _ba, _bk, next_aliases, next_keywords, _reasons in candidates:
                    await conn.execute(
                        """
                        UPDATE story_characters
                        SET aliases = $2::jsonb, keywords = $3::jsonb, updated_at = NOW()
                        WHERE id = $1::uuid
                        """,
                        character_id,
                        next_aliases,
                        next_keywords,
                    )
            print(f"apply 件数: {len(candidates)}")
        elif apply:
            print("apply 件数: 0")

        if verify:
            remaining = 0
            for row in await _load_characters(conn):
                name = str(row["name"] or "")
                aliases = _coerce_json_list(row["aliases"])
                keywords = _coerce_json_list(row["keywords"])
                if is_corrupted_char_split_array(aliases) or is_corrupted_char_split_array(keywords):
                    remaining += 1
                    print(f"verify NG: ID={row['id']} name={name!r}")
            if remaining == 0:
                print("verify OK: 破損形状は残っていません")
            else:
                print(f"verify NG: 残件 {remaining}")
                return 1
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="StoryCharacter aliases/keywords 修復")
    parser.add_argument("--apply", action="store_true", help="DB を更新する")
    parser.add_argument("--verify", action="store_true", help="修復後に破損形状が残っていないか確認する")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply, verify=args.verify))


if __name__ == "__main__":
    raise SystemExit(main())
