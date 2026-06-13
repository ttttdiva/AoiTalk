"""Skills specialist agent."""

from __future__ import annotations

from agents import Agent, ModelSettings

from ..skills.executor import invoke_skill
from ..tools.adapters import OpenAIAgentAdapter
from .base import BaseAgent


class SkillsAgent(BaseAgent):
    """Specialized agent for skill routing."""

    def _create_agent(self) -> Agent:
        tools = OpenAIAgentAdapter.convert_all([invoke_skill])

        instructions = """
You are a skills specialist.

Use the available skill invocation tool when the user asks for a task that
matches an installed skill. Route the request through the skill instead of
answering manually.
""".strip()

        return Agent(
            name="SkillsAssistant",
            model=self.model,
            instructions=instructions,
            model_settings=ModelSettings(tool_choice="required"),
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "skills_assistant"

    def get_tool_description(self) -> str:
        return "Skills assistant - route requests through installed skills"
