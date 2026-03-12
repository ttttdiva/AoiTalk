"""
MCP (Model Context Protocol) integration tools
"""
import json
import asyncio
import traceback
import nest_asyncio

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()
from ..core import tool as _tool_decorator

# Global MCP plugin instance (will be set by AgentLLMClient)
_mcp_plugin = None

def set_mcp_plugin(plugin):
    """Set the global MCP plugin instance"""
    global _mcp_plugin
    _mcp_plugin = plugin


@_tool_decorator
def use_mcp_tool(server_name: str, tool_name: str, arguments_json: str = "{}") -> str:
    """MCPサーバーのツールを実行する
    
    Args:
        server_name: MCPサーバー名
        tool_name: ツール名
        arguments_json: ツールの引数（JSON文字列形式）
    """
    print(f"[Tool] use_mcp_tool が呼び出されました: {server_name}.{tool_name}({arguments_json})")
    
    global _mcp_plugin
    
    if _mcp_plugin is None:
        return "MCPプラグインが初期化されていません"
    
    if not _mcp_plugin.is_initialized():
        return "MCPプラグインが有効ではありません"
    
    try:
        # Parse JSON arguments
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError as e:
        return f"引数のJSON解析エラー: {arguments_json} - {str(e)}"
    
    # Build tool call format expected by MCP plugin
    tool_call = {
        'name': f'mcp_{server_name}_{tool_name}',
        'arguments': arguments
    }
    
    print(f"[Tool] MCPツール実行開始: {tool_call['name']}")
    
    try:
        # Create a new event loop for MCP operations to avoid conflicts
        import asyncio
        
        # Save current loop state
        try:
            current_loop = asyncio.get_running_loop()
            has_loop = True
        except RuntimeError:
            has_loop = False
        
        # Create new isolated loop for MCP
        new_loop = asyncio.new_event_loop()
        old_loop = None
        
        try:
            # Temporarily set the new loop
            if has_loop:
                old_loop = asyncio.get_event_loop()
            asyncio.set_event_loop(new_loop)
            
            # Execute MCP tool in new loop
            result = new_loop.run_until_complete(_mcp_plugin.execute_tool(tool_call))
            
            print(f"[Tool] MCPツール実行完了: {result}")
            return result
            
        finally:
            # Restore original loop state
            new_loop.close()
            if old_loop:
                asyncio.set_event_loop(old_loop)
            elif not has_loop:
                asyncio.set_event_loop(None)
        
    except Exception as e:
        error_msg = f"MCPツール実行エラー: {str(e)}"
        print(f"[Tool] {error_msg}")
        traceback.print_exc()
        return error_msg


def create_mcp_tool_wrapper(mcp_plugin):
    """Create a wrapper for MCP tools

    This function is used to integrate MCP with the agent.
    Returns ToolDefinition objects (use adapters to convert for specific backends).
    """
    set_mcp_plugin(mcp_plugin)
    return [use_mcp_tool]