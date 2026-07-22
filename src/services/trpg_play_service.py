"""TRPG マルチプレイヤープレイサービス（後方互換 re-export シム）

実装は関心ごとに `src/services/trpg_play/` パッケージへ分割されている。
既存の import パス（`from ..services.trpg_play_service import ...` や
`import src.services.trpg_play_service as trpg_play_service` からの属性参照）を
維持するため、公開シンボルと他サービス／テストが参照する内部ヘルパーを
ここから再公開する。

ココフォリア風のルーム／参加者／ログ管理と、AI GM 連携の入口を提供する。
既存の scenario_service.py はシングルプレイ用のAPIを維持する。
"""

from __future__ import annotations

from .trpg_play import *  # noqa: F401,F403
from .trpg_play import __all__  # noqa: F401
