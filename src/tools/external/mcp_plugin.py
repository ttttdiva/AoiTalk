"""
MCP (Model Context Protocol) plugin for integrating external tools and data sources
"""
import asyncio
import logging
from typing import Dict, List, Optional, Any
from contextlib import AsyncExitStack
import json
import os

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logging.warning("MCP SDK not available. Install with: pip install mcp[cli]")

logger = logging.getLogger(__name__)


class MCPClient:
    """
    Model Context Protocol client for connecting to MCP servers
    """
    
    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self.exit_stack: Optional[AsyncExitStack] = None
        self.servers: Dict[str, Dict[str, Any]] = {}
    
    async def start(self):
        """Initialize the MCP client"""
        if not MCP_AVAILABLE:
            logger.error("MCP SDK not available")
            return False
            
        self.exit_stack = AsyncExitStack()
        return True
    
    async def stop(self):
        """Clean up MCP client resources"""
        if self.exit_stack:
            try:
                await self.exit_stack.aclose()
            except Exception as e:
                # Ignore errors during cleanup, especially GeneratorExit
                if "GeneratorExit" not in str(type(e).__name__):
                    logger.debug(f"Error during exit stack cleanup: {e}")
            finally:
                self.exit_stack = None
        self.sessions.clear()
        self.servers.clear()
    
    async def add_server(self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """
        Add an MCP server
        
        Args:
            name: Server identifier
            command: Path to server executable
            args: Command line arguments
            env: Environment variables
        """
        if not MCP_AVAILABLE or not self.exit_stack:
            logger.error("MCP client not initialized")
            return False
            
        if args is None:
            args = []
        if env is None:
            env = {}
            
        try:
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=env
            )
            
            # Add timeout to prevent hanging during server startup
            stdio_client_result = await asyncio.wait_for(
                self.exit_stack.enter_async_context(stdio_client(server_params)),
                timeout=45.0  # 45 second timeout for server startup
            )
            read_stream, write_stream = stdio_client_result
            
            session = await self.exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            
            # Add timeout to prevent hanging during initialization
            await asyncio.wait_for(session.initialize(), timeout=30.0)
            
            self.sessions[name] = session
            self.servers[name] = {
                'command': command,
                'args': args,
                'env': env
            }
            
            logger.info(f"MCP server '{name}' connected successfully")
            return True
            
        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to MCP server '{name}' - server took too long to start")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to MCP server '{name}': {e}")
            return False
    
    async def remove_server(self, name: str):
        """Remove an MCP server"""
        if name in self.sessions:
            del self.sessions[name]
            del self.servers[name]
            logger.info(f"MCP server '{name}' removed")
    
    async def list_tools(self, server_name: str = None) -> Dict[str, List[Dict]]:
        """
        List available tools from all servers or a specific server
        
        Args:
            server_name: Optional server name to filter tools
            
        Returns:
            Dictionary mapping server names to their tool lists
        """
        tools = {}
        
        servers_to_check = [server_name] if server_name else list(self.sessions.keys())
        
        for name in servers_to_check:
            if name not in self.sessions:
                continue
                
            try:
                session = self.sessions[name]
                response = await session.list_tools()
                tools[name] = [
                    {
                        'name': tool.name,
                        'description': tool.description,
                        'inputSchema': tool.inputSchema
                    }
                    for tool in response.tools
                ]
            except Exception as e:
                logger.error(f"Failed to list tools from server '{name}': {e}")
                tools[name] = []
        
        return tools
    
    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict]:
        """
        Call a tool on a specific MCP server
        
        Args:
            server_name: Name of the server
            tool_name: Name of the tool to call
            arguments: Tool arguments
            
        Returns:
            Tool execution result or None if failed
        """
        if server_name not in self.sessions:
            logger.error(f"Server '{server_name}' not found")
            return None
            
        try:
            session = self.sessions[server_name]
            # Add timeout to prevent hanging
            response = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=30.0  # 30 second timeout for API calls
            )
            
            return {
                'content': response.content,
                'isError': response.isError
            }
            
        except asyncio.TimeoutError:
            logger.error(f"Tool '{tool_name}' on server '{server_name}' timed out after 30 seconds")
            return {'content': ['Tool execution timed out'], 'isError': True}
        except Exception as e:
            logger.error(f"Failed to call tool '{tool_name}' on server '{server_name}': {e}")
            return None
    
    async def list_resources(self, server_name: str = None) -> Dict[str, List[Dict]]:
        """
        List available resources from all servers or a specific server
        
        Args:
            server_name: Optional server name to filter resources
            
        Returns:
            Dictionary mapping server names to their resource lists
        """
        resources = {}
        
        servers_to_check = [server_name] if server_name else list(self.sessions.keys())
        
        for name in servers_to_check:
            if name not in self.sessions:
                continue
                
            try:
                session = self.sessions[name]
                response = await session.list_resources()
                resources[name] = [
                    {
                        'uri': resource.uri,
                        'name': resource.name,
                        'description': resource.description,
                        'mimeType': resource.mimeType
                    }
                    for resource in response.resources
                ]
            except Exception as e:
                logger.error(f"Failed to list resources from server '{name}': {e}")
                resources[name] = []
        
        return resources
    
    async def read_resource(self, server_name: str, uri: str) -> Optional[Dict]:
        """
        Read a resource from a specific MCP server
        
        Args:
            server_name: Name of the server
            uri: Resource URI
            
        Returns:
            Resource content or None if failed
        """
        if server_name not in self.sessions:
            logger.error(f"Server '{server_name}' not found")
            return None
            
        try:
            session = self.sessions[server_name]
            response = await session.read_resource(uri)
            
            return {
                'contents': response.contents
            }
            
        except Exception as e:
            logger.error(f"Failed to read resource '{uri}' from server '{server_name}': {e}")
            return None
    
    def get_server_info(self) -> Dict[str, Dict]:
        """Get information about connected servers"""
        return self.servers.copy()
    
    def is_available(self) -> bool:
        """Check if MCP is available"""
        return MCP_AVAILABLE


class MCPPlugin:
    """
    Plugin wrapper for MCP client functionality
    """
    
    def __init__(self):
        self.name = "MCP Plugin"  # Add name attribute for OpenAI Agents SDK compatibility
        self.client = MCPClient()
        self._initialized = False
        self._init_loop_id = None
        self._tool_server_map: Dict[str, str] = {}  # tool_name -> server_name cache
    
    async def initialize(self, config: Dict[str, Any] = None):
        """Initialize the MCP plugin"""
        if config is None:
            config = {}
            
        # Track which event loop this was initialized in
        import asyncio
        import platform
        try:
            current_loop = asyncio.get_running_loop()
            self._init_loop_id = id(current_loop)
        except RuntimeError:
            self._init_loop_id = None
            
        success = await self.client.start()
        if success:
            self._initialized = True
            
            # Add servers from config if provided
            servers = config.get('servers', {})
            if servers:  # Check if servers is not None and not empty
                for name, server_config in servers.items():
                    # Handle platform-specific configuration
                    if isinstance(server_config, dict) and ('windows' in server_config or 'linux' in server_config):
                        platform_name = 'windows' if platform.system() == 'Windows' else 'linux'
                        if platform_name in server_config:
                            actual_config = server_config[platform_name]
                        else:
                            logger.warning(f"No configuration for platform '{platform_name}' found for server '{name}'")
                            continue
                    else:
                        actual_config = server_config
                    
                    # Start with current process environment (PATH, etc.)
                    # then overlay config-specified env vars
                    env = actual_config.get('env', {})
                    expanded_env = dict(os.environ)
                    expanded_env['PYTHONIOENCODING'] = 'utf-8'
                    for key, value in env.items():
                        if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                            # Extract env var name and expand it
                            env_var_name = value[2:-1]
                            expanded_value = os.getenv(env_var_name, '')
                            expanded_env[key] = expanded_value
                        else:
                            expanded_env[key] = value
                    
                    await self.add_server(
                        name=name,
                        command=actual_config.get('command'),
                        args=actual_config.get('args', []),
                        env=expanded_env
                    )
        
        return success
    
    async def cleanup(self):
        """Clean up plugin resources"""
        if self._initialized:
            await self.client.stop()
            self._initialized = False
    
    async def add_server(self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """Add an MCP server"""
        if not self._initialized:
            logger.error("MCP plugin not initialized")
            return False
        return await self.client.add_server(name, command, args, env)
    
    def is_initialized(self) -> bool:
        """Check if plugin is initialized"""
        return self._initialized
    
    def is_initialized_in_current_loop(self) -> bool:
        """Check if plugin is initialized in the current event loop"""
        try:
            import asyncio
            current_loop = asyncio.get_running_loop()
            current_loop_id = id(current_loop)
            return self._initialized and self._init_loop_id == current_loop_id
        except RuntimeError:
            # No running loop
            return self._initialized and self._init_loop_id is None
    
    async def get_tools_for_agent(self) -> List[Dict]:
        """
        Get all available tools formatted for agent use
        
        Returns:
            List of tool definitions
        """
        if not self._initialized:
            return []
            
        all_tools = await self.client.list_tools()
        agent_tools = []
        
        for server_name, tools in all_tools.items():
            for tool in tools:
                agent_tools.append({
                    'type': 'function',
                    'function': {
                        'name': f"mcp_{server_name}_{tool['name']}",
                        'description': f"[MCP:{server_name}] {tool['description']}",
                        'parameters': tool['inputSchema']
                    },
                    'server_name': server_name,
                    'tool_name': tool['name']
                })
        
        return agent_tools
    
    async def execute_tool(self, tool_call) -> str:
        """
        Execute an MCP tool call
        
        Args:
            tool_call: Tool call object with name and arguments
            
        Returns:
            Tool execution result as string
        """
        if not self._initialized:
            return "MCP plugin not initialized"
            
        tool_name = tool_call.get('name', '')
        if not tool_name.startswith('mcp_'):
            return "Not an MCP tool"
        
        # Parse server and tool name from function name
        remaining = tool_name[4:]  # Remove 'mcp_' prefix
        
        # Try to match with known servers
        server_name = None
        actual_tool_name = None
        
        for known_server in self.client.sessions.keys():
            if remaining.startswith(known_server + '_'):
                server_name = known_server
                actual_tool_name = remaining[len(known_server) + 1:]
                break
        
        if server_name is None or actual_tool_name is None:
            return f"Invalid MCP tool name format: {tool_name}"
        arguments = tool_call.get('arguments', {})
        
        # Handle string arguments (from JSON)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return "Invalid tool arguments JSON"
        
        result = await self.client.call_tool(server_name, actual_tool_name, arguments)
        if result is None:
            return f"Failed to execute tool {actual_tool_name} on server {server_name}"
        
        if result.get('isError', False):
            return f"Tool execution error: {result.get('content', 'Unknown error')}"
        
        # Format content for return
        content = result.get('content', [])
        if isinstance(content, list):
            texts = []
            for item in content:
                if hasattr(item, 'text'):
                    # TextContent object
                    texts.append(str(item.text))
                elif isinstance(item, dict) and 'text' in item:
                    # Dictionary with 'text' key
                    texts.append(str(item['text']))
                else:
                    # Other types
                    texts.append(str(item))
            return '\n'.join(texts)
        else:
            return str(content)
    
    def is_available(self) -> bool:
        """Check if MCP is available"""
        return self.client.is_available()
    
    async def list_tools(self):
        """List tools (required by OpenAI Agents SDK)"""
        if not self._initialized:
            return []

        # OpenAI Agents SDK expects a list of tool objects with .name attribute
        all_tools = await self.client.list_tools()
        tools_list = []

        # Rebuild tool-to-server mapping cache
        self._tool_server_map.clear()

        for server_name, tools in all_tools.items():
            for tool in tools:
                # Cache tool -> server mapping
                self._tool_server_map[tool['name']] = server_name

                # Create a simple object with name attribute
                class ToolObject:
                    def __init__(self, name, description, inputSchema):
                        self.name = name
                        self.description = description
                        self.inputSchema = inputSchema

                tool_obj = ToolObject(
                    name=tool['name'],
                    description=tool['description'],
                    inputSchema=tool['inputSchema']
                )
                tools_list.append(tool_obj)

        return tools_list
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]):
        """Call tool (required by OpenAI Agents SDK)

        The SDK calls list_tools() to discover tools, then call_tool(name, args)
        to invoke them. Tool names come directly from the MCP server (e.g.,
        'get_teams', 'create_task') so we need to find the correct server.
        """
        class TextContent:
            def __init__(self, text):
                self.text = text
            def model_dump_json(self):
                return json.dumps({"text": self.text})

        def _make_result(text):
            return type('MockResult', (), {'content': [TextContent(text)]})()

        if not self._initialized:
            return _make_result('MCP plugin not initialized')

        # Use cached tool-to-server mapping (built by list_tools)
        server_name = self._tool_server_map.get(tool_name)

        # Fallback: rebuild mapping if cache miss
        if server_name is None:
            for sname in self.client.sessions:
                try:
                    session = self.client.sessions[sname]
                    response = await session.list_tools()
                    for t in response.tools:
                        self._tool_server_map[t.name] = sname
                except Exception:
                    continue
            server_name = self._tool_server_map.get(tool_name)

        if server_name is None:
            return _make_result(f'Tool "{tool_name}" not found on any MCP server')

        # Call the tool through MCP client
        result = await self.client.call_tool(server_name, tool_name, arguments)

        if result is None:
            return _make_result(f'Failed to execute tool {tool_name}')

        if result.get('isError', False):
            return _make_result(f'Tool execution error: {result.get("content", "Unknown error")}')

        # Format content for OpenAI Agents SDK
        content = result.get('content', [])
        if isinstance(content, list):
            texts = []
            for item in content:
                if hasattr(item, 'text'):
                    texts.append(str(item.text))
                elif isinstance(item, dict) and 'text' in item:
                    texts.append(str(item['text']))
                else:
                    texts.append(str(item))
            content_text = '\n'.join(texts)
        else:
            content_text = str(content)

        return _make_result(content_text) 