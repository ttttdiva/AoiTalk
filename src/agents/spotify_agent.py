"""Spotify specialist agent."""

from __future__ import annotations

from ..llm.native_runtime import AgentDefinition as Agent, NativeModelSettings as ModelSettings

from ..tools.core import ensure_tool_definitions, tool
from ..tools.entertainment.spotify import (
    add_playlist_to_queue,
    add_queue_to_playlist,
    add_tracks_to_playlist,
    clear_spotify_queue,
    create_playlist,
    create_playlist_from_queue,
    get_spotify_status,
    get_spotify_user_playlists,
    pause_spotify,
    play_playlist,
    play_song_now,
    play_spotify_track,
    previous_track,
    queue_song,
    remove_from_queue,
    remove_tracks_from_playlist,
    search_spotify_music,
    set_spotify_auth_code,
    setup_spotify_auth,
    show_queue,
    skip_spotify_track,
)
from ..tools.memory.spotify_memory_tools import (
    get_recent_spotify_activity,
    get_spotify_activity_stats,
    get_spotify_listening_patterns,
    search_spotify_activity,
)
from .base import BaseAgent


class SpotifyAgent(BaseAgent):
    """Specialized agent for Spotify playback and playlist work."""

    def _create_agent(self) -> Agent:
        tools = [
            *ensure_tool_definitions(
                [
                    setup_spotify_auth,
                    set_spotify_auth_code,
                    search_spotify_activity,
                    get_spotify_activity_stats,
                    get_recent_spotify_activity,
                    get_spotify_listening_patterns,
                ]
            ),
            tool(search_spotify_music),
            tool(play_spotify_track),
            tool(play_song_now),
            tool(queue_song),
            tool(pause_spotify),
            tool(skip_spotify_track),
            tool(previous_track),
            tool(get_spotify_status),
            tool(show_queue),
            tool(clear_spotify_queue),
            tool(remove_from_queue),
            tool(get_spotify_user_playlists),
            tool(create_playlist),
            tool(create_playlist_from_queue),
            tool(add_tracks_to_playlist),
            tool(add_queue_to_playlist),
            tool(add_playlist_to_queue),
            tool(remove_tracks_from_playlist),
            tool(play_playlist),
        ]

        instructions = """
You are a Spotify specialist.

Handle Spotify authentication, search, playback control, queue management,
playlist management, and Spotify activity history. Use the available tools
directly instead of describing what the user should click in Spotify.
""".strip()

        return Agent(
            name="SpotifyAssistant",
            model=self.model,
            instructions=instructions,
            model_settings=ModelSettings(tool_choice="required"),
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "spotify_assistant"

    def get_tool_description(self) -> str:
        return (
            "Spotify assistant - authenticate Spotify, search music, control "
            "playback, manage queues and playlists, and inspect Spotify "
            "activity history"
        )
