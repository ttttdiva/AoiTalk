from __future__ import annotations

from src.agents.filesystem_agent import FilesystemAgent
from src.agents.media_agent import MediaAgent
from src.agents.search_agent import SearchAgent
from src.agents.skills_agent import SkillsAgent
from src.agents.spotify_agent import SpotifyAgent
from src.agents.utility_agent import UtilityAgent


def test_spotify_agent_exposes_playback_playlist_and_activity_tools():
    agent = SpotifyAgent(model="gpt-4o-mini").agent
    tool_names = {tool.name for tool in agent.tools}

    assert agent.model_settings.tool_choice == "required"
    assert {
        "setup_spotify_auth",
        "set_spotify_auth_code",
        "search_spotify_music",
        "play_song_now",
        "queue_song",
        "get_spotify_status",
        "create_playlist",
        "search_spotify_activity",
    }.issubset(tool_names)


def test_utility_agent_exposes_only_utility_tools():
    agent = UtilityAgent(model="gpt-4o-mini").agent
    tool_names = {tool.name for tool in agent.tools}

    assert agent.model_settings.tool_choice == "required"
    assert tool_names == {"get_current_time", "get_weather_info", "calculate"}


def test_filesystem_agent_exposes_workspace_user_file_and_os_tools():
    agent = FilesystemAgent(model="gpt-4o-mini").agent
    tool_names = {tool.name for tool in agent.tools}

    assert agent.model_settings.tool_choice == "auto"
    assert {
        "list_workspace_files",
        "find_workspace_items",
        "inspect_workspace_tree",
        "read_workspace_file",
        "execute_command",
        "view_file",
        "edit_file",
        "search_files",
        "get_repo_map",
        "upload_user_file",
        "list_user_files",
    }.issubset(tool_names)


def test_media_agent_exposes_image_and_video_tools():
    agent = MediaAgent(model="gpt-4o-mini").agent
    tool_names = {tool.name for tool in agent.tools}

    assert agent.model_settings.tool_choice == "required"
    assert {
        "generate_image",
        "generate_comfyui_image",
        "list_comfyui_workflows",
        "search_and_play_youtube",
        "play_youtube_audio",
        "search_and_play_niconico",
        "play_niconico_audio",
        "stop_video_audio",
        "get_video_playback_status",
        "play_bgm",
        "stop_bgm",
    }.issubset(tool_names)


def test_skills_agent_exposes_skill_invocation_tool():
    agent = SkillsAgent(model="gpt-4o-mini").agent
    tool_names = {tool.name for tool in agent.tools}

    assert agent.model_settings.tool_choice == "required"
    assert tool_names == {"invoke_skill"}


def test_search_agent_defaults_to_web_only_when_x_and_knowledge_are_off():
    agent = SearchAgent(
        model="gpt-4o-mini",
        config={},
    ).agent
    tool_names = {tool.name for tool in agent.tools}

    assert agent.model_settings.tool_choice == "auto"
    assert "web_search" in tool_names
    assert "grok_x_search" not in tool_names
    assert "knowledge_search" not in tool_names


def test_search_agent_can_enable_x_and_knowledge_by_config():
    agent = SearchAgent(
        model="gpt-4o-mini",
        config={
            "search": {"x_enabled": True, "knowledge_enabled": True},
            "memory": {"enabled": True, "enable_search": False},
        },
    ).agent
    tool_names = {tool.name for tool in agent.tools}

    assert {"web_search", "grok_x_search", "knowledge_search"}.issubset(tool_names)
