"""
Base class for all agents in the AoiTalk system.

Provides common functionality and structure for specialized agents
that handle domain-specific operations.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional
from agents import Agent


class BaseAgent(ABC):
    """Abstract base class for all specialized agents."""
    
    def __init__(self, model: str = "gpt-4o-mini"):
        """
        Initialize the base agent.
        
        Args:
            model: The model to use for the agent
        """
        self.model = model
        self._agent: Optional[Agent] = None
    
    @abstractmethod
    def _create_agent(self) -> Agent:
        """
        Create and configure the agent instance.
        
        Must be implemented by subclasses to define:
        - Agent name
        - Instructions
        - Tools
        
        Returns:
            Configured Agent instance
        """
        pass
    
    @property
    def agent(self) -> Agent:
        """Get the agent instance, creating it if necessary."""
        if self._agent is None:
            self._agent = self._create_agent()
        return self._agent
    
    def as_tool(self) -> Any:
        """
        Convert this agent to a tool for use by the main agent.
        
        Returns:
            Tool that can be used by the main agent
        """
        tool = self.agent.as_tool(
            tool_name=self.get_tool_name(),
            tool_description=self.get_tool_description()
        )
        
        # Log when tool is created
        print(f"[{self.__class__.__name__}] Tool '{self.get_tool_name()}' created")
        print(f"[{self.__class__.__name__}] Tool description: {self.get_tool_description()}")
        
        # Add logging wrapper to track when agent tool is called
        print(f"[{self.__class__.__name__}] Checking tool attributes for logging...")
        print(f"[{self.__class__.__name__}] Tool has __call__: {hasattr(tool, '__call__')}")
        print(f"[{self.__class__.__name__}] Tool has func: {hasattr(tool, 'func')}")
        print(f"[{self.__class__.__name__}] Tool has on_invoke_tool: {hasattr(tool, 'on_invoke_tool')}")
        
        # Try to wrap the on_invoke_tool callback if it exists
        if hasattr(tool, 'on_invoke_tool') and tool.on_invoke_tool:
            original_invoke = tool.on_invoke_tool
            def logged_invoke(*args, **kwargs):
                print(f"[{self.__class__.__name__}] Tool '{self.get_tool_name()}' が呼び出されました (on_invoke_tool)")
                return original_invoke(*args, **kwargs)
            tool.on_invoke_tool = logged_invoke
        elif hasattr(tool, '__call__'):
            original_call = tool.__call__
            def logged_call(*args, **kwargs):
                print(f"[{self.__class__.__name__}] Tool '{self.get_tool_name()}' が呼び出されました (__call__)")
                return original_call(*args, **kwargs)
            tool.__call__ = logged_call
        elif hasattr(tool, 'func'):
            original_func = tool.func
            def logged_func(*args, **kwargs):
                print(f"[{self.__class__.__name__}] Tool '{self.get_tool_name()}' が呼び出されました (func)")
                return original_func(*args, **kwargs)
            tool.func = logged_func
        
        return tool
    
    @abstractmethod
    def get_tool_name(self) -> str:
        """Get the name for this agent when used as a tool."""
        pass
    
    @abstractmethod
    def get_tool_description(self) -> str:
        """Get the description for this agent when used as a tool."""
        pass