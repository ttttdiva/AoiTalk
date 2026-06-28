"""
Base class for all agents in the AoiTalk system.

Provides common functionality and structure for specialized agents
that handle domain-specific operations.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from ..llm.native_runtime import AgentDefinition as Agent, run_native_agent_once
from ..tools.core import ToolDefinition, ToolParam


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
        async def _delegate(request: str) -> str:
            print(f"[{self.__class__.__name__}] Tool '{self.get_tool_name()}' が呼び出されました")
            result = await run_native_agent_once(self.agent, request)
            return result.final_output

        _delegate.__name__ = self.get_tool_name()
        _delegate.__doc__ = self.get_tool_description()

        tool = ToolDefinition(
            name=self.get_tool_name(),
            description=self.get_tool_description(),
            function=_delegate,
            parameters=[
                ToolParam(
                    name="request",
                    type="string",
                    description="専門エージェントへの依頼内容",
                    required=True,
                )
            ],
            is_async=True,
        )

        print(f"[{self.__class__.__name__}] Tool '{self.get_tool_name()}' created")
        print(f"[{self.__class__.__name__}] Tool description: {self.get_tool_description()}")
        return tool

    @abstractmethod
    def get_tool_name(self) -> str:
        """Get the name for this agent when used as a tool."""
        pass

    @abstractmethod
    def get_tool_description(self) -> str:
        """Get the description for this agent when used as a tool."""
        pass
