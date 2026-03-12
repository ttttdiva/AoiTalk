#!/usr/bin/env python
"""Workspace MCP サーバー起動スクリプト。

Usage:
    python scripts/workspace_mcp_server.py
"""
import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.tools.external.workspace_mcp.server import main

main()
