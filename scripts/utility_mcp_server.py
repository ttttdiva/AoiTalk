#!/usr/bin/env python
"""Utility MCP サーバー起動スクリプト。

パッケージ階層 (src.tools.__init__) の重いインポートを回避するため、
直接 server モジュールをインポートして起動する。

Usage:
    python scripts/utility_mcp_server.py
"""
import sys
import os

# プロジェクトルートをパスに追加
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.tools.external.utility_mcp.server import main

main()
