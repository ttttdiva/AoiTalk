"""既存の token_usage コストを料金カタログで再計算する管理者向けコマンド。

料金カタログを導入する前に記録された行は `total_cost = 0` / `pricing_status = 'unknown'`
のまま残る。このスクリプトは `created_at` 時点で有効な料金ルールを使ってそれらを
再計算する。自動では走らないので、必ず明示的に実行すること。

使い方::

    # まず dry-run で影響範囲を確認する（既定は dry-run）
    venv\\Scripts\\python.exe scripts/backfill_token_costs.py

    # 期間・プロバイダ・モデルで絞る
    venv\\Scripts\\python.exe scripts/backfill_token_costs.py --start 2026-01-01 --end 2026-07-29 --provider openai

    # 実際に書き込む
    venv\\Scripts\\python.exe scripts/backfill_token_costs.py --apply

注意:
    - `provider_reported_cost` を持つ行（OpenRouter など）は上書きしない。
    - 再実行しても同じ結果になる（冪等）。
    - 既定では `total_cost = 0` の行だけを対象にする。全行を対象にするなら --all。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows のコンソール既定コードページ(cp932)だと日本語が化けるため UTF-8 に固定する。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from src.services.pricing.backfill import BackfillFilter, backfill_token_usage_costs
from src.services.token_tracking_service import _to_datetime, _to_exclusive_end


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="token_usage の list cost を料金カタログで再計算する",
    )
    parser.add_argument("--start", help="開始日 YYYY-MM-DD（JST日界）")
    parser.add_argument("--end", help="終了日 YYYY-MM-DD（JST日界、当日を含む）")
    parser.add_argument("--provider", help="プロバイダで絞る（例: openai）")
    parser.add_argument("--model", help="モデル名で絞る（完全一致）")
    parser.add_argument(
        "--all",
        action="store_true",
        help="total_cost = 0 の行だけでなく全行を対象にする",
    )
    parser.add_argument("--limit", type=int, help="処理する最大行数")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際にDBを更新する（指定しない場合は dry-run）",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    filt = BackfillFilter(
        start=_to_datetime(args.start),
        end=_to_exclusive_end(args.end),
        provider=args.provider,
        model=args.model,
        only_zero_cost=not args.all,
    )

    dry_run = not args.apply
    mode = "DRY-RUN（DBは変更しません）" if dry_run else "APPLY（DBを更新します）"
    print(f"=== token_usage コスト再計算: {mode} ===")
    print(f"期間     : {filt.start or '指定なし'} 〜 {filt.end or '指定なし'} (UTC)")
    print(f"provider : {filt.provider or 'すべて'}")
    print(f"model    : {filt.model or 'すべて'}")
    print(f"対象     : {'total_cost = 0 の行のみ' if filt.only_zero_cost else '全行'}")
    print()

    result = await backfill_token_usage_costs(filt, dry_run=dry_run, limit=args.limit)

    print(f"走査件数                 : {result['scanned']}")
    print(f"更新件数                 : {result['updated']}")
    print(f"変更なし件数             : {result.get('unchanged', 0)}")
    print(f"未知モデルで据え置いた件数: {result['unknown_model_count']}")
    print(f"provider報告額で除外      : {result['skipped_provider_reported']}")
    print(f"合計コスト (変更前)      : ${result['total_cost_before']}")
    print(f"合計コスト (変更後)      : ${result['total_cost_after']}")

    unknown = result.get("unknown_models") or []
    if unknown:
        print()
        print("料金を確定できなかったモデル:")
        for name in unknown:
            print(f"  - {name}")
        print("→ config/pricing_catalog.json に料金ルールを追加してから再実行してください。")

    if dry_run:
        print()
        print("dry-run のため DB は変更していません。--apply を付けて再実行してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
