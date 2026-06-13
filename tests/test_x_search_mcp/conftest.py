"""X Search MCP テスト用 conftest。

src.tools.__init__ の重いインポートチェーンを回避するため、
中間パッケージをスタブ化する。
"""

import os
import sys
import types
from importlib import import_module

project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

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

        if mod_name == "src.tools":

            def _tools_getattr(name):
                if name in {"get_registry", "init_global_tools_registry", "register_tool"}:
                    return getattr(import_module("src.tools.registry"), name)
                if name in {"ToolDefinition", "tool"}:
                    return getattr(import_module("src.tools.core"), name)
                if name == "init_spotify_manager":
                    return getattr(
                        import_module("src.tools.entertainment.spotify.auth"),
                        name,
                    )
                raise AttributeError(name)

            stub.__getattr__ = _tools_getattr

        if mod_name == "src.tools.external":

            def _external_getattr(name):
                if name == "MCPPlugin":
                    return getattr(import_module("src.tools.external.mcp_plugin"), name)
                if name == "set_mcp_plugin":
                    return getattr(import_module("src.tools.external.mcp_tools"), name)
                raise AttributeError(name)

            stub.__getattr__ = _external_getattr

if "src" in sys.modules and "src.tools" in sys.modules:
    setattr(sys.modules["src"], "tools", sys.modules["src.tools"])
if "src.tools" in sys.modules and "src.tools.external" in sys.modules:
    setattr(sys.modules["src.tools"], "external", sys.modules["src.tools.external"])
