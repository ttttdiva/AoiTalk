"""Scenario-specific chat context isolation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional


SCENARIO_WRITING_TOOLS = frozenset({"writing_assistant", "import_assistant"})
SCENARIO_ROLEPLAY_TOOLS = frozenset({"scenario_assistant"})
SCENARIO_TRPG_PLAY_TOOLS = frozenset({"scenario_assistant"})

SCENARIO_WORKFLOW_ALLOWED_TOOLS = {
    "writing": SCENARIO_WRITING_TOOLS,
    "roleplay": SCENARIO_ROLEPLAY_TOOLS,
    "trpg_play": SCENARIO_TRPG_PLAY_TOOLS,
}

DISABLED_SCENARIO_WORKFLOW_TOOLS = {
    "project_management_assistant",
}


@dataclass(frozen=True)
class ScenarioChatContext:
    mode: str
    prompt: str
    agent_name: str
    allowed_tools: frozenset[str]


def is_scenario_workflow_tool_disabled(tool_name: str) -> bool:
    return tool_name in DISABLED_SCENARIO_WORKFLOW_TOOLS


def get_scenario_workflow_allowed_tools(mode: str) -> frozenset[str]:
    return SCENARIO_WORKFLOW_ALLOWED_TOOLS.get(mode, frozenset())


def is_scenario_workflow_tool_allowed(
    tool_name: str,
    context: ScenarioChatContext,
) -> bool:
    return tool_name in context.allowed_tools


def _compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _format_common_rules(mode_label: str) -> list[str]:
    return [
        f"## Scenario Workflow: {mode_label}",
        "You are running inside a dedicated scenario workflow session.",
        "This is the primary system instruction for the session.",
        "The app-header assistant/character selection is not part of this session.",
        "Selected project/case context is not part of this session.",
        "Do not use project-management, task-management, scheduling, or case-management behavior.",
        "Only use tools explicitly allowed for this scenario workflow mode.",
    ]


def _format_writing_prompt(
    *,
    conversation_id: str,
    writing_session: dict[str, Any],
    writing_context: str,
) -> str:
    parts = _format_common_rules("Writing")
    parts.extend(
        [
            "Act as the scenario writing assistant for this session.",
            "Use the scenario, episode, scene, canon, and character context below as the source of truth.",
            "For drafting, continuation, rewriting, setting proposals, and canon work, stay inside this scenario context.",
            "Allowed tools: `writing_assistant`, `import_assistant`.",
            (
                "If a tool is needed, include this exact "
                f"`conversation_id`: {conversation_id}"
            ),
            "When writing prose, produce actual scene/story text instead of project-management advice.",
            "",
            "### Writing Session",
            _compact_json(writing_session),
            "",
            "### Writing Context",
            writing_context,
        ]
    )
    return "\n".join(parts)


def _format_roleplay_prompt(
    *,
    conversation_id: str,
    scenario: dict[str, Any],
    character: dict[str, Any],
) -> str:
    parts = _format_common_rules("Scenario Roleplay")
    parts.extend(
        [
            f"Act only as the scenario character `{character.get('name') or 'unknown'}`.",
            "Stay in-character unless the user explicitly asks for out-of-character discussion.",
            "Use the scenario and character data below as the source of truth.",
            "Allowed tools: `scenario_assistant` only when scenario state must be checked or updated.",
            f"conversation_id: {conversation_id}",
            "",
            "### Scenario",
            _compact_json(
                {
                    "id": scenario.get("id"),
                    "title": scenario.get("title"),
                    "description": scenario.get("description"),
                    "setting": scenario.get("setting"),
                    "genre": scenario.get("genre"),
                }
            ),
            "",
            "### Roleplay Character",
            _compact_json(character),
        ]
    )
    return "\n".join(parts)


def _format_play_prompt(
    *,
    conversation_id: str,
    play_session: dict[str, Any],
) -> str:
    parts = _format_common_rules("TRPG Play")
    parts.extend(
        [
            "Act as the scenario/TRPG narrator for this play session.",
            "Handle GM narration, NPC portrayal, scene framing, and consequences within the active scenario state.",
            "Do not replace scenario play with general assistant advice.",
            "Use `scenario_assistant` for scenario state updates, dice, BGM, and progress tracking when needed.",
            "Allowed tools: `scenario_assistant`.",
            f"If using scenario tools, include this exact `conversation_id`: {conversation_id}",
            "",
            "### Active TRPG Scenario State",
            _compact_json(
                {
                    "scenario_title": play_session.get("scenario", {}).get("title"),
                    "current_scene": play_session.get("current_scene", {}).get("title"),
                    "player_state": play_session.get("player_state", {}),
                    "status": play_session.get("status"),
                }
            ),
        ]
    )
    return "\n".join(parts)


async def build_scenario_chat_context(
    conversation_id: Optional[str],
) -> Optional[ScenarioChatContext]:
    """Return scenario-specific prompt context for a conversation session."""

    if not conversation_id:
        return None

    try:
        from ..services.scenario_service import (
            get_play_session_by_conversation_id,
            get_scenario,
            get_writing_session_by_conversation,
        )
        from ..tools.writing_tools import get_writing_context
    except Exception:
        return None

    writing_session = await get_writing_session_by_conversation(conversation_id)
    if writing_session:
        writing_context = await get_writing_context.execute_async(
            conversation_id=conversation_id
        )
        return ScenarioChatContext(
            mode="writing",
            agent_name="ScenarioWritingAssistant",
            allowed_tools=get_scenario_workflow_allowed_tools("writing"),
            prompt=_format_writing_prompt(
                conversation_id=conversation_id,
                writing_session=writing_session,
                writing_context=str(writing_context),
            ),
        )

    play_session = await get_play_session_by_conversation_id(conversation_id)
    if play_session:
        return ScenarioChatContext(
            mode="trpg_play",
            agent_name="ScenarioTRPGNarrator",
            allowed_tools=get_scenario_workflow_allowed_tools("trpg_play"),
            prompt=_format_play_prompt(
                conversation_id=conversation_id,
                play_session=play_session,
            ),
        )

    try:
        from ..memory.conversation_repository import ConversationRepository

        repo = ConversationRepository()
        session = await repo.get_session_by_id(conversation_id)
    except Exception:
        session = None

    character_name = getattr(session, "character_name", "") if session else ""
    roleplay_match = re.fullmatch(r"scenario_roleplay:([^:]+):([^:]+)", character_name)
    if not roleplay_match:
        return None

    scenario_id, character_id = roleplay_match.groups()
    try:
        scenario = await get_scenario(scenario_id, include_children=True)
    except Exception:
        return None

    character = next(
        (
            item
            for item in scenario.get("characters", [])
            if str(item.get("id")) == character_id
        ),
        None,
    )
    if not character:
        return None

    return ScenarioChatContext(
        mode="roleplay",
        agent_name=str(character.get("name") or "ScenarioRoleplayCharacter"),
        allowed_tools=get_scenario_workflow_allowed_tools("roleplay"),
        prompt=_format_roleplay_prompt(
            conversation_id=conversation_id,
            scenario=scenario,
            character=character,
        ),
    )
