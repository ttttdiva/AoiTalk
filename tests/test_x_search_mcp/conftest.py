"""X Search MCP テスト用 conftest。

src.tools.__init__ の重いインポートチェーンを回避するため、
中間パッケージをスタブ化する。
"""
import sys
import os
import types

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
