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

import json
import logging
import os
import sys
from pathlib import Path

# ── リポジトリルートを import パスへ追加 ──
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 出力先（frontend 配下。openapi-typescript の入力にする）
OUTPUT_PATH = REPO_ROOT / "frontend" / "openapi.json"

# 実体化を軽くするため、DB 接続やバックグラウンド処理を伴う副作用は
# ライフスパン（起動時）に閉じ込められている。ここでは app を構築するだけで
# lifespan は起動しないため、ルート登録だけが行われる。


def build_app():
    """WebChatServer を実体化し、FastAPI app を返す。"""
    # うるさいログを抑制（スキーマ生成には不要）
    logging.disable(logging.WARNING)

    from src.config import Config
    from src.api.server import create_web_interface

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


def main() -> int:
    app = build_app()

    schema = app.openapi()
    schema = sort_keys(schema)

    text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)
    # 末尾改行を付与（POSIX テキストファイル慣習・差分安定化）
    text = text + "\n"

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(text, encoding="utf-8", newline="\n")

    path_count = len(schema.get("paths", {}))
    print(f"OpenAPI schema written to {OUTPUT_PATH} ({path_count} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
