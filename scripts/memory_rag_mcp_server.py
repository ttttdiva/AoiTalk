#!/usr/bin/env python
"""Memory/RAG MCP サーバー起動スクリプト。

Usage:
    python scripts/memory_rag_mcp_server.py
"""
import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.tools.external.memory_rag_mcp.server import main

main()
