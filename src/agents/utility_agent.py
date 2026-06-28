"""Utility specialist agent."""

from __future__ import annotations

from ..llm.native_runtime import AgentDefinition as Agent, NativeModelSettings as ModelSettings

from ..tools.basic import calculate, get_current_time, get_weather_info
from ..tools.core import ensure_tool_definitions
from .base import BaseAgent


class UtilityAgent(BaseAgent):
    """Specialized agent for utility operations."""

    def _create_agent(self) -> Agent:
        tools = ensure_tool_definitions(
            [get_current_time, get_weather_info, calculate]
        )

        instructions = """
You are a utility specialist.

Handle time lookup, weather lookup, and calculation requests directly with
tools whenever the user asks for a utility-style action.
""".strip()

        return Agent(
            name="UtilityAssistant",
            model=self.model,
            instructions=instructions,
            model_settings=ModelSettings(tool_choice="required"),
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "utility_assistant"

    def get_tool_description(self) -> str:
        return (
            "Utility assistant - get the current time, weather, and calculation "
            "results"
        )
