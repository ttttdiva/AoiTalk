from src.tools import get_registry, init_global_tools_registry
from src.tools.utils import (
    get_tool_registry,
    init_global_tools_registry as init_legacy_global_tools_registry,
)


def test_global_tools_registry_does_not_expose_use_mcp_tool():
    assert "use_mcp_tool" not in get_registry()
    assert "invoke_skill" not in get_registry()


def test_init_global_tools_registry_is_backward_compatible():
    assert init_global_tools_registry() is get_registry()
    assert "use_mcp_tool" not in init_global_tools_registry()
    assert "invoke_skill" not in init_global_tools_registry()


def test_legacy_utils_init_global_tools_registry_is_backward_compatible():
    assert init_legacy_global_tools_registry() is get_tool_registry()
