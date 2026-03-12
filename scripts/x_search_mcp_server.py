#!/usr/bin/env python
"""X Search MCP サーバー起動スクリプト。

パッケージ階層 (src.tools.__init__) の重いインポートを回避するため、
中間パッケージをスタブ化してから server モジュールをインポートする。

Usage:
    python scripts/x_search_mcp_server.py
"""
import sys
import os
import types

# プロジェクトルートをパスに追加
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# src.tools.__init__.py の重いインポートチェーンを回避するため、
# 中間パッケージを空モジュールとして事前登録する
for mod_name, subdir in [
    ("src", "src"),
    ("src.tools", os.path.join("src", "tools")),
    ("src.tools.external", os.path.join("src", "tools", "external")),
]:
    if mod_name not in sys.modules:
        stub = types.ModuleType(mod_name)
        stub.__path__ = [os.path.join(project_root, subdir)]
        stub.__package__ = mod_name
        sys.modules[mod_name] = stub

from src.tools.external.x_search_mcp.server import main

main()
