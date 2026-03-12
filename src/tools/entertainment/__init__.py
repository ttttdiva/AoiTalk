"""
Entertainment tools for voice assistant

This module provides entertainment features (Spotify, YouTube, NicoNico).
Feature Flag: FEATURE_ENTERTAINMENT controls whether these components are loaded.
"""
from src.features import Features

# Conditional imports based on Feature Flags
if Features.entertainment():
    # Spotify tools
    from .spotify_tools import (
        search_spotify_music,
        play_spotify_track,
        pause_spotify,
        skip_spotify_track,
        get_spotify_status,
        show_queue,
        clear_spotify_queue,
        skip_all_queue,
        play_song_now,
        queue_song,
        add_to_queue,
        add_playlist_to_queue,
        get_spotify_user_playlists,
        setup_spotify_auth,
        set_spotify_auth_code,
        init_spotify_manager,
        # プレイリスト管理機能
        create_playlist_from_queue,
        add_queue_to_playlist,
        remove_tracks_from_playlist,
        add_tracks_to_playlist,
        create_playlist,
        play_playlist,
        # キュー管理機能
        remove_from_queue,
        # 互換性のため古い名前も維持
        add_song_to_queue,
        get_spotify_queue,
        find_and_play_spotify_music,
        find_and_queue_spotify_music,
        queue_playlist
    )

    # Video streaming tools
    from .video_streaming import (
        search_and_play_youtube,
        play_youtube_audio,
        search_and_play_niconico,
        play_niconico_audio,
        stop_video_audio,
        get_video_playback_status
    )

    __all__ = [
        'search_spotify_music',
        'play_spotify_track', 
        'pause_spotify',
        'skip_spotify_track',
        'get_spotify_status',
        'show_queue',
        'clear_spotify_queue',
        'skip_all_queue',
        'play_song_now',
        'queue_song',
        'add_to_queue',
        'add_playlist_to_queue',
        'get_spotify_user_playlists',
        'setup_spotify_auth',
        'set_spotify_auth_code',
        'init_spotify_manager',
        'create_playlist_from_queue',
        'add_queue_to_playlist',
        'remove_tracks_from_playlist',
        'add_tracks_to_playlist',
        'create_playlist',
        'play_playlist',
        'remove_from_queue',
        'add_song_to_queue',
        'get_spotify_queue',
        'find_and_play_spotify_music',
        'find_and_queue_spotify_music',
        'queue_playlist',
        'search_and_play_youtube',
        'play_youtube_audio',
        'search_and_play_niconico',
        'play_niconico_audio',
        'stop_video_audio',
        'get_video_playback_status'
    ]
else:
    # Feature disabled - provide None stubs
    search_spotify_music = None
    play_spotify_track = None
    pause_spotify = None
    skip_spotify_track = None
    get_spotify_status = None
    show_queue = None
    clear_spotify_queue = None
    skip_all_queue = None
    play_song_now = None
    queue_song = None
    add_to_queue = None
    add_playlist_to_queue = None
    get_spotify_user_playlists = None
    setup_spotify_auth = None
    set_spotify_auth_code = None
    init_spotify_manager = None
    create_playlist_from_queue = None
    add_queue_to_playlist = None
    remove_tracks_from_playlist = None
    add_tracks_to_playlist = None
    create_playlist = None
    play_playlist = None
    remove_from_queue = None
    add_song_to_queue = None
    get_spotify_queue = None
    find_and_play_spotify_music = None
    find_and_queue_spotify_music = None
    queue_playlist = None
    search_and_play_youtube = None
    play_youtube_audio = None
    search_and_play_niconico = None
    play_niconico_audio = None
    stop_video_audio = None
    get_video_playback_status = None

    __all__ = [
        'search_spotify_music',
        'play_spotify_track', 
        'pause_spotify',
        'skip_spotify_track',
        'get_spotify_status',
        'show_queue',
        'clear_spotify_queue',
        'skip_all_queue',
        'play_song_now',
        'queue_song',
        'add_to_queue',
        'add_playlist_to_queue',
        'get_spotify_user_playlists',
        'setup_spotify_auth',
        'set_spotify_auth_code',
        'init_spotify_manager',
        'create_playlist_from_queue',
        'add_queue_to_playlist',
        'remove_tracks_from_playlist',
        'add_tracks_to_playlist',
        'create_playlist',
        'play_playlist',
        'remove_from_queue',
        'add_song_to_queue',
        'get_spotify_queue',
        'find_and_play_spotify_music',
        'find_and_queue_spotify_music',
        'queue_playlist',
        'search_and_play_youtube',
        'play_youtube_audio',
        'search_and_play_niconico',
        'play_niconico_audio',
        'stop_video_audio',
        'get_video_playback_status'
    ]