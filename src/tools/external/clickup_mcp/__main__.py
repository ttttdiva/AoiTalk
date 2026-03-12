"""python -m src.tools.external.clickup_mcp で起動するエントリポイント。

注意: この方法は src.tools.__init__ の全インポートが走るため非推奨。
推奨: python scripts/clickup_mcp_server.py
"""

from .server import main

main()
