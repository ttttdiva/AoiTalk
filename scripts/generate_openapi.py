#!/usr/bin/env python3
"""
FastAPI の OpenAPI スキーマを frontend/openapi.json へ出力するスクリプト。

サーバーを起動（uvicorn 等）せずに ``WebChatServer`` を実体化し、
``app.openapi()`` から得たスキーマを決定論的（キー順ソート）に整形して書き出す。
これにより backend の型変更を frontend の型生成（openapi-typescript）へ伝搬でき、
出力の差分検知（drift 検知）にも利用できる。

使い方（リポジトリルートで実行）:

    venv\\Scripts\\python.exe scripts/generate_openapi.py

生成後の TS 型再生成:

    cd frontend && npm run typegen
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# ── リポジトリルートを import パスへ追加 ──
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# FastAPI OpenAPI の共有正本。frontend/mobile の生成物はこのファイルから派生する。
CANONICAL_OUTPUT_PATH = REPO_ROOT / "contracts" / "openapi" / "fastapi.json"
# 既存 frontend パイプラインとの互換出力（内容は共有正本と同一）。
FRONTEND_OUTPUT_PATH = REPO_ROOT / "frontend" / "openapi.json"

# 実体化を軽くするため、DB 接続やバックグラウンド処理を伴う副作用は
# ライフスパン（起動時）に閉じ込められている。ここでは app を構築するだけで
# lifespan は起動しないため、ルート登録だけが行われる。


def build_app():
    """WebChatServer を実体化し、FastAPI app を返す。"""
    # うるさいログを抑制（スキーマ生成には不要）
    logging.disable(logging.WARNING)

    from src.config import Config
    from src.api.server import create_web_interface

    # Enterprise/DB必須プロファイルでは Config が PostgreSQL の正本設定を
    # 読むため、スキーマ生成プロセス自身も先にマイグレーションを完了させる。
    # 通常プロファイルでは従来どおりDBを必須にせず、seed設定だけで生成できる。
    require_database = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        from src.features import Features

        require_database = require_database or Features.is_enterprise()
    except Exception:
        # Feature判定自体が失敗する環境では、明示的な必須指定だけを尊重する。
        pass
    if require_database:
        from src.memory.database import get_database_manager

        db_manager = get_database_manager()
        if not asyncio.run(db_manager.initialize()):
            raise RuntimeError("Database initialization failed before OpenAPI generation")

    config = Config()
    # character_name はスキーマ生成に影響しない任意値。
    server = create_web_interface(config, character_name="kotonoha_aoi")
    return server.app


def sort_keys(value):
    """dict のキーを再帰的にソートして決定論的な出力にする。

    list は順序が意味を持つ（paths の並び等ではなく配列値）ため順序を保持する。
    ただし要素内の dict は再帰的にソートする。
    """
    if isinstance(value, dict):
        return {key: sort_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [sort_keys(item) for item in value]
    return value


def _write_schema(path: Path, schema: dict) -> None:
    """Write one deterministic JSON artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "FastAPI OpenAPI を共有契約へ出力する（frontend/mobile の型生成は "
            "contracts/openapi/fastapi.json を入力にする）。"
        )
    )
    parser.add_argument(
        "--canonical-only",
        action="store_true",
        help="共有正本 contracts/openapi/fastapi.json のみ更新する",
    )
    parser.add_argument(
        "--frontend-only",
        action="store_true",
        help="互換 frontend/openapi.json のみ更新する",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="指定した単一出力先へ書き出す（--canonical-only/--frontend-only と併用不可）",
    )
    args = parser.parse_args(argv)
    if args.canonical_only and args.frontend_only:
        parser.error("--canonical-only と --frontend-only は同時指定できません")
    if args.output is not None and (args.canonical_only or args.frontend_only):
        parser.error("--output は --canonical-only/--frontend-only と同時指定できません")

    app = build_app()

    schema = app.openapi()
    schema = sort_keys(schema)

    path_count = len(schema.get("paths", {}))
    if args.output is not None:
        outputs = [args.output if args.output.is_absolute() else REPO_ROOT / args.output]
    elif args.canonical_only:
        outputs = [CANONICAL_OUTPUT_PATH]
    elif args.frontend_only:
        outputs = [FRONTEND_OUTPUT_PATH]
    else:
        # Default keeps the historical frontend command working while making
        # the canonical artifact available to both native clients.
        outputs = [CANONICAL_OUTPUT_PATH, FRONTEND_OUTPUT_PATH]
    for output in outputs:
        _write_schema(output, schema)
        print(f"OpenAPI schema written to {output} ({path_count} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
