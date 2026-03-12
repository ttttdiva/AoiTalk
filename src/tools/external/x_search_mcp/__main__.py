"""python -m src.tools.external.x_search_mcp で起動するエントリポイント。

注意: この方法は src.tools.__init__ の全インポートが走るため非推奨。
推奨: python scripts/x_search_mcp_server.py
"""

from .server import main

main()
