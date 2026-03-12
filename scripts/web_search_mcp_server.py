#!/usr/bin/env python
"""Web Search MCP サーバー起動スクリプト。

Usage:
    python scripts/web_search_mcp_server.py
"""
import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.tools.external.web_search_mcp.server import main

main()
