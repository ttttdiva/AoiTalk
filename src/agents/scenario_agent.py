"""Scenario specialist agent."""

from __future__ import annotations

from ..llm.native_runtime import AgentDefinition as Agent, NativeModelSettings as ModelSettings

from ..tools.core import ensure_tool_definitions
from ..tools.trpg_creation_tools import create_trpg_scenario
from ..tools.scenario_tools import roll_dice, get_scenario_state, update_scenario_state
from ..tools.entertainment.music_tools import play_bgm, stop_bgm
from .base import BaseAgent


class ScenarioAgent(BaseAgent):
    """Specialized agent for TRPG scenario management and automation."""

    def _create_agent(self) -> Agent:
        tools = ensure_tool_definitions(
            [
                roll_dice,
                create_trpg_scenario,
                get_scenario_state,
                update_scenario_state,
                play_bgm,
                stop_bgm,
            ]
        )

        instructions = """
You are a TRPG scenario specialist (Game Master assistant).

Your job is to manage the state of a TRPG scenario play session by observing the conversation and extracting key updates.
Perform the following actions silently using the provided tools:

1. **State Extraction**:
   - Observe the player's dialogue and the narrator's descriptions.
   - If the player gains an item, use `update_scenario_state` with `add_items`.
   - If a significant event happens (e.g., meeting someone, discovering a secret), add a flag using `add_flags`.
   - If the player's health (HP) changes, update it using `hp`.

2. **Scene Transitions & BGM**:
   - Check the `current_scene` and its `transitions` using `get_scenario_state`.
   - If the story progress matches a transition condition (e.g., "reached the castle", "defeated the boss"), use `update_scenario_state` to move to the `current_scene_id` of the target scene.
   - Automatically change the BGM using `play_bgm` when the scene changes or the atmosphere shifts significantly (e.g., "mysterious", "battle", "sad", "heroic").

3. **Randomness**:
   - Use `roll_dice` when the player performs an action that requires a random outcome or check (e.g., "1d100", "2d6").

**Technical Guidelines**:
- Use `get_scenario_state` to understand the current situation, scene transitions, and player state.
- Combine multiple state updates (HP, flags, items) into a single `update_scenario_state` call whenever possible.
- If multiple technical actions are required (e.g., changing the scene and the BGM), emit all necessary tool calls in a single response to ensure they are processed together.
- Perform updates silently. If you are also acting as the GM, you can incorporate the results naturally into your narrative response, but ensure the technical state is updated first.
- Be proactive. Don't wait for explicit instructions to update the inventory, flags, or BGM if the narrative clearly implies a change.
""".strip()

        return Agent(
            name="ScenarioAssistant",
            model=self.model,
            instructions=instructions,
            model_settings=ModelSettings(tool_choice="auto"),
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "scenario_assistant"

    def get_tool_description(self) -> str:
        return (
            "Scenario assistant - manage TRPG play session state, roll dice, and track progress"
        )
