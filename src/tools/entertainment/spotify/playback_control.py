"""
Spotify再生制御モジュール
"""

import spotipy
import logging
import time
import asyncio
from ...core import tool as function_tool

logger = logging.getLogger(__name__)

# Import logging functionality
from ..spotify_logger import get_spotify_logger


def play_spotify_track(spotify_uri: str = None) -> str:
    """Spotifyで楽曲を即座に再生します（現在の楽曲を停止）
    
    Args:
        spotify_uri: 再生するSpotify URI。指定しない場合は現在の再生を再開
        
    Returns:
        再生結果のメッセージ
        
    Note:
        この関数は現在再生中の楽曲を停止して新しい楽曲を再生します。
        キューに追加したい場合は add_to_queue を使用してください。
    """
    from .auth import get_spotify_manager
    from .monitoring import set_skip_in_progress, update_last_track_id, request_monitor_reset
    
    print(f"[Tool] play_spotify_track が呼び出されました: uri='{spotify_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    # 再生機能にはユーザー認証が必要
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生機能を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    success = False
    error_message = None
    track_info = None
    playback_info = None
    
    try:
        if spotify_uri:
            # Get track info before playing
            track_id = spotify_uri.split(':')[-1] if ':' in spotify_uri else spotify_uri
            track_info = spotify.track(track_id)
            
            # 監視システムを一時停止
            set_skip_in_progress(True)
            print(f"[AutoPlay] URI再生：監視システム一時停止")
            
            # 特定の楽曲を再生
            spotify.start_playback(uris=[spotify_uri])
            
            # Get playback info after starting
            time.sleep(0.2)  # Brief delay to ensure playback starts
            playback_info = spotify.current_playback()
            
            # デバッグログ: 再生開始した楽曲情報を出力
            if track_info:
                track_name = track_info.get('name', '不明')
                artist_names = ", ".join([artist['name'] for artist in track_info.get('artists', [])]) if track_info.get('artists') else "不明"
                print(f"[SPOTIFY_DEBUG] 🎵 再生開始: 「{track_name}」 by {artist_names}")
            
            # 監視システムの状態をリセット
            update_last_track_id(track_id)
            request_monitor_reset()
            
            # 少し待ってから監視を再開
            time.sleep(0.3)
            set_skip_in_progress(False)
            print(f"[AutoPlay] URI再生：楽曲ID更新({track_id}) + 監視再開")
            
            success = True
            result_message = f"🎵 楽曲の再生を開始しました: {spotify_uri}"
        else:
            # 現在の再生を再開
            spotify.start_playback()
            playback_info = spotify.current_playback()
            success = True
            result_message = "▶️ 再生を再開しました。"
            
    except spotipy.exceptions.SpotifyException as e:
        error_message = str(e)
        if e.http_status == 404:
            result_message = "アクティブなSpotifyデバイスが見つかりません。Spotifyアプリを開いてデバイスをアクティブにしてください。"
        elif e.http_status == 401:
            result_message = "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            result_message = f"Spotify再生エラー: {str(e)}"
    except Exception as e:
        error_message = str(e)
        result_message = f"再生エラー: {str(e)}"
    
    # Log the activity
    try:
        logger_instance = get_spotify_logger()
        asyncio.create_task(logger_instance.log_activity(
            user_id="default_user",  # TODO: Get from context
            character_name="assistant",  # TODO: Get from context
            action="play",
            track_info=track_info,
            playback_info=playback_info,
            request_text=f"play_spotify_track({spotify_uri})",
            success=success,
            error_message=error_message
        ))
    except Exception as log_error:
        print(f"[SpotifyLogger] Error logging play_spotify_track: {log_error}")
    
    return result_message


def play_song_now(song_name: str) -> str:
    """曲名で検索してSpotifyで即座に再生します（現在の楽曲を停止）
    
    Args:
        song_name: 今すぐ再生したい曲名
        
    Returns:
        再生結果のメッセージ
        
    Note:
        この関数は現在再生中の楽曲を停止して新しい楽曲を即座に再生します。
        「今すぐ流して」「この曲を流して」「すぐに再生」等のリクエストに使用してください。
        キューに追加したい場合は queue_song を使用してください。
    """
    from .auth import get_spotify_manager
    from .monitoring import set_skip_in_progress, update_last_track_id, request_monitor_reset
    
    print(f"[Tool] play_song_now が呼び出されました: song_name='{song_name}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    success = False
    error_message = None
    track_info = None
    playback_info = None
    
    # 検索は認証不要のクライアントを使用
    search_spotify = manager._get_spotify_client()
    if not search_spotify:
        return "Spotify検索機能が初期化されていません。設定を確認してください。"
    
    try:
        tracks = []
        
        # 「の」が含まれている場合、まずアーティスト名と曲名に分けて検索
        if 'の' in song_name:
            parts = song_name.split('の', 1)
            if len(parts) == 2:
                artist_name = parts[0].strip()
                track_name = parts[1].strip()
                # アーティスト名と曲名で検索
                search_query = f"{artist_name} {track_name}"
                print(f"[play_song_now] 「の」検出: アーティスト名と曲名で検索 - '{search_query}'")
                try:
                    results = search_spotify.search(q=search_query, type='track', limit=1)
                    tracks = [t for t in results['tracks']['items'] if t is not None]
                except Exception:
                    pass
        
        # 上記で見つからない場合、元のクエリで検索
        if not tracks:
            try:
                results = search_spotify.search(q=song_name, type='track', limit=1)
                tracks = [t for t in results['tracks']['items'] if t is not None]
                
                # 結果が見つからなかった場合のみ、追加の検索パターンを試行
                if not tracks:
                    # 限定的な追加パターンのみ試行
                    search_queries = [
                        f'"{song_name}"',  # 完全一致検索
                        song_name.lower(),  # 小文字変換
                    ]
                    
                    for search_query in search_queries:
                        try:
                            results = search_spotify.search(q=search_query, type='track', limit=1)
                            valid_tracks = [t for t in results['tracks']['items'] if t is not None]
                            if valid_tracks:
                                tracks = valid_tracks
                                break
                        except Exception:
                            continue
                
                if not tracks:
                    return f"'{song_name}'に該当する楽曲が見つかりませんでした。"
                    
            except Exception as e:
                return f"Spotify検索エラー: {str(e)}"
            
        track = tracks[0]
        artists = ", ".join([artist['name'] for artist in track['artists']])
        
        # 再生にはユーザー認証が必要
        play_spotify = manager._get_spotify_user_client()
        if not play_spotify:
            return f"🎵 楽曲を見つけました:\n楽曲: {track['name']}\nアーティスト: {artists}\n\n再生するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
        
        # 監視システムを一時停止
        set_skip_in_progress(True)
        print(f"[AutoPlay] 今すぐ再生：監視システム一時停止")
        
        # 楽曲を即座に再生
        play_spotify.start_playback(uris=[track['uri']])
        
        # Get playback info after starting
        time.sleep(0.2)  # Brief delay to ensure playback starts
        playback_info = play_spotify.current_playback()
        
        # デバッグログ: 再生開始した楽曲情報を出力
        track_name = track.get('name', '不明')
        artist_names = ", ".join([artist['name'] for artist in track.get('artists', [])]) if track.get('artists') else "不明"
        print(f"[SPOTIFY_DEBUG] 🎵 再生開始: 「{track_name}」 by {artist_names}")
        
        # 監視システムの状態をリセット
        update_last_track_id(track['id'])
        request_monitor_reset()
        
        # 少し待ってから監視を再開
        time.sleep(0.3)
        set_skip_in_progress(False)
        print(f"[AutoPlay] 今すぐ再生：楽曲ID更新({track['id']}) + 監視再開")
        
        success = True
        track_info = track
        result_message = f"🎵 再生開始:\n楽曲: {track['name']}\nアーティスト: {artists}\nアルバム: {track['album']['name']}"
        
    except spotipy.exceptions.SpotifyException as e:
        error_message = str(e)
        if e.http_status == 404:
            result_message = "アクティブなSpotifyデバイスが見つかりません。Spotifyアプリを開いてデバイスをアクティブにしてください。"
        elif e.http_status == 401:
            result_message = "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            result_message = f"Spotify検索・再生エラー: {str(e)}"
    except Exception as e:
        error_message = str(e)
        result_message = f"音楽検索・再生エラー: {str(e)}"
    
    # Log the activity
    try:
        logger_instance = get_spotify_logger()
        asyncio.create_task(logger_instance.log_activity(
            user_id="default_user",  # TODO: Get from context
            character_name="assistant",  # TODO: Get from context
            action="play",
            track_info=track_info,
            playback_info=playback_info,
            request_text=f"play_song_now({song_name})",
            success=success,
            error_message=error_message
        ))
    except Exception as log_error:
        print(f"[SpotifyLogger] Error logging play_song_now: {log_error}")
    
    return result_message


def pause_spotify() -> str:
    """Spotifyの再生を一時停止します
    
    Returns:
        一時停止結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] pause_spotify が呼び出されました")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    # 再生制御にはユーザー認証が必要
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生制御を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    success = False
    error_message = None
    playback_info = None
    
    try:
        # Get playback info before pausing
        playback_info = spotify.current_playback()
        
        spotify.pause_playback()
        success = True
        result_message = "⏸️ 再生を一時停止しました。"
        
    except spotipy.exceptions.SpotifyException as e:
        error_message = str(e)
        if e.http_status == 404:
            result_message = "アクティブなSpotifyデバイスが見つかりません。"
        elif e.http_status == 401:
            result_message = "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            result_message = f"Spotify一時停止エラー: {str(e)}"
    except Exception as e:
        error_message = str(e)
        result_message = f"一時停止エラー: {str(e)}"
    
    # Log the activity
    try:
        logger_instance = get_spotify_logger()
        asyncio.create_task(logger_instance.log_activity(
            user_id="default_user",  # TODO: Get from context
            character_name="assistant",  # TODO: Get from context
            action="pause",
            playback_info=playback_info,
            request_text="pause_spotify()",
            success=success,
            error_message=error_message
        ))
    except Exception as log_error:
        print(f"[SpotifyLogger] Error logging pause_spotify: {log_error}")
    
    return result_message


def skip_spotify_track() -> str:
    """内部キューを考慮してスマートにスキップします
    
    Returns:
        スキップ結果のメッセージ
    """
    from .auth import get_spotify_manager
    from .queue_system import get_internal_queue
    from .monitoring import set_skip_in_progress, update_last_track_id, request_monitor_reset
    
    print(f"[Tool] skip_spotify_track が呼び出されました")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生制御を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    # 結果変数を初期化
    result = ""
    success = False
    error_message = None
    track_info = None
    playback_info = None
    internal_queue = get_internal_queue()
    
    try:
        # Get current playback info before skipping
        try:
            playback_info = spotify.current_playback()
        except:
            pass  # Continue even if we can't get playback info
        
        # スキップ処理開始フラグを設定
        set_skip_in_progress(True)
        print("[AutoPlay] スキップ処理開始 - 自動再生を一時停止")
        
        # 内部キューに次の楽曲があるかチェック
        if internal_queue.has_next():
            # 内部キューから次の楽曲を取得
            next_track = internal_queue.get_next()
            
            if next_track:
                try:
                    # 次の楽曲を再生
                    spotify.start_playback(uris=[next_track['uri']])
                    
                    # デバッグログ: 内部キューからの手動スキップ再生
                    try:
                        track_name = next_track.get('name', '不明')
                        # 内部キューではアーティスト情報は'artist'フィールドに文字列として保存されている
                        artist_names = next_track.get('artist', '不明')
                        print(f"[SPOTIFY_DEBUG] ⏭️ 内部キューから手動スキップ再生: 「{track_name}」 by {artist_names}")
                    except Exception as e:
                        print(f"[SPOTIFY_DEBUG] 内部キューからの手動スキップ再生ログエラー: {e}")
                    
                    # 監視システムの完全リセットを要求と楽曲ID即座更新
                    track_id = next_track['uri'].split(':')[-1]
                    update_last_track_id(track_id)
                    request_monitor_reset()
                    print(f"[AutoPlay] 手動スキップ：楽曲ID即座更新({track_id}) + 監視リセット要求")
                    
                    result = f"⏭️ 内部キューから次の楽曲を再生：\n{next_track['name']} - {next_track.get('artist', '不明')}"
                    track_info = next_track
                    success = True
                    
                    # 自動キューが有効な場合、新しい曲を追加
                    from .auto_queue_manager import get_auto_queue_manager
                    auto_queue = get_auto_queue_manager()
                    if auto_queue.is_enabled():
                        if auto_queue.add_one_track():
                            result += "\n🎵 自動キューで新しい曲を追加しました"
                except spotipy.exceptions.SpotifyException as play_error:
                    if play_error.http_status == 404:
                        # デバイスが見つからない場合は、通常のスキップを試行
                        try:
                            spotify.next_track()
                            
                            # 監視システムの完全リセットを要求と楽曲ID即座更新
                            track_id = next_track['uri'].split(':')[-1]
                            update_last_track_id(track_id)
                            request_monitor_reset()
                            print(f"[AutoPlay] 手動スキップ（通常）：楽曲ID即座更新({track_id}) + 監視リセット要求")
                            
                            result = f"⏭️ 内部キューの次の楽曲にスキップ：\n{next_track['name']} - {next_track.get('artist', '不明')}"
                            track_info = next_track
                            success = True
                            
                            # 自動キューが有効な場合、新しい曲を追加
                            from .auto_queue_manager import get_auto_queue_manager
                            auto_queue = get_auto_queue_manager()
                            if auto_queue.is_enabled():
                                if auto_queue.add_one_track():
                                    result += "\n🎵 自動キューで新しい曲を追加しました"
                        except:
                            result = f"❌ 楽曲の再生とスキップの両方に失敗しました：\n{next_track['name']}"
                    else:
                        result = f"❌ 楽曲の再生に失敗：{str(play_error)}"
            else:
                # next_trackがNoneの場合（理論上は起こらない）
                result = "❌ 内部キューから楽曲を取得できませんでした。"
            
        else:
            # 内部キューが空の場合
            # 現在のSpotifyキューをチェック
            try:
                queue = spotify.queue()
                if queue and queue.get('queue') and len(queue['queue']) > 0:
                    # Spotifyキューに楽曲がある場合は通常のスキップ
                    spotify.next_track()
                    result = "⏭️ Spotifyキューの次の曲にスキップしました。"
                    success = True
                else:
                    # 両方のキューが空の場合は停止
                    spotify.pause_playback()
                    result = "⏹️ キューに次の楽曲がないため再生を停止しました。"
                    success = True
            except Exception as queue_error:
                # キューチェックに失敗した場合は停止
                try:
                    spotify.pause_playback()
                    result = "⏹️ 次の楽曲がないため再生を停止しました。"
                    success = True
                except:
                    result = "❌ 再生制御でエラーが発生しました。"
        
    except spotipy.exceptions.SpotifyException as e:
        error_message = str(e)
        if e.http_status == 404:
            result = "アクティブなSpotifyデバイスが見つかりません。"
        elif e.http_status == 401:
            result = "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            result = f"スキップエラー: {str(e)}"
    except Exception as e:
        error_message = str(e)
        result = f"スキップエラー: {str(e)}"
    finally:
        # スキップ処理完了
        set_skip_in_progress(False)
        print("[AutoPlay] スキップ処理完了")
    
    # Log the activity
    try:
        logger_instance = get_spotify_logger()
        asyncio.create_task(logger_instance.log_activity(
            user_id="default_user",  # TODO: Get from context
            character_name="assistant",  # TODO: Get from context
            action="skip",
            track_info=track_info,
            playback_info=playback_info,
            request_text="skip_spotify_track()",
            success=success,
            error_message=error_message
        ))
    except Exception as log_error:
        print(f"[SpotifyLogger] Error logging skip_spotify_track: {log_error}")
        
    return result


def previous_track() -> str:
    """前の曲に戻ります
    
    Returns:
        結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] previous_track が呼び出されました")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生制御を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    success = False
    error_message = None
    playback_info = None
    
    try:
        # Get playback info before action
        playback_info = spotify.current_playback()
        
        spotify.previous_track()
        success = True
        result_message = "⏮️ 前の曲に戻りました。"
        
    except spotipy.exceptions.SpotifyException as e:
        error_message = str(e)
        if e.http_status == 404:
            result_message = "アクティブなSpotifyデバイスが見つかりません。"
        else:
            result_message = f"エラー: {str(e)}"
    except Exception as e:
        error_message = str(e)
        result_message = f"エラー: {str(e)}"
    
    # Log the activity
    try:
        logger_instance = get_spotify_logger()
        asyncio.create_task(logger_instance.log_activity(
            user_id="default_user",  # TODO: Get from context
            character_name="assistant",  # TODO: Get from context
            action="previous",
            playback_info=playback_info,
            request_text="previous_track()",
            success=success,
            error_message=error_message
        ))
    except Exception as log_error:
        print(f"[SpotifyLogger] Error logging previous_track: {log_error}")
    
    return result_message


def add_to_queue(spotify_uri: str) -> str:
    """指定された楽曲を再生キューに追加します
    
    Args:
        spotify_uri: 追加する楽曲のSpotify URI
        
    Returns:
        追加結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] add_to_queue が呼び出されました: uri='{spotify_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生機能を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        # Spotifyのキューに追加
        spotify.add_to_queue(spotify_uri)
        
        # 追加した楽曲の情報を取得
        track_id = spotify_uri.split(":")[-1]
        track = spotify.track(track_id)
        artists = ", ".join([artist['name'] for artist in track['artists']])
        
        # 内部キューにも追加
        from .queue_system import get_internal_queue
        internal_queue = get_internal_queue()
        track_info = {
            'uri': spotify_uri,
            'name': track['name'],
            'artist': artists
        }
        internal_queue.add(track_info)
        
        return f"➕ キューに追加しました:\n楽曲: {track['name']}\nアーティスト: {artists}"
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 404:
            return "アクティブなSpotifyデバイスが見つかりません。"
        elif e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"キュー追加エラー: {str(e)}"
    except Exception as e:
        return f"エラー: {str(e)}"


def play_album(album_uri: str) -> str:
    """アルバム全体を再生します
    
    Args:
        album_uri: 再生するアルバムのSpotify URI
        
    Returns:
        再生結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] play_album が呼び出されました: uri='{album_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生機能を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        spotify.start_playback(context_uri=album_uri)
        
        # アルバム情報を取得
        album_id = album_uri.split(":")[-1]
        album = spotify.album(album_id)
        artists = ", ".join([artist['name'] for artist in album['artists']])
        
        return f"💿 アルバムの再生を開始しました:\nアルバム: {album['name']}\nアーティスト: {artists}"
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 404:
            return "アクティブなSpotifyデバイスが見つかりません。Spotifyアプリを開いてデバイスをアクティブにしてください。"
        elif e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"アルバム再生エラー: {str(e)}"
    except Exception as e:
        return f"再生エラー: {str(e)}"


def play_playlist(playlist_uri: str) -> str:
    """プレイリストを再生します
    
    Args:
        playlist_uri: 再生するプレイリストのSpotify URI
        
    Returns:
        再生結果のメッセージ
    """
    from .auth import get_spotify_manager
    
    print(f"[Tool] play_playlist が呼び出されました: uri='{playlist_uri}'")
    
    manager = get_spotify_manager()
    if not manager:
        return "Spotify管理クラスが初期化されていません。"
    
    spotify = manager._get_spotify_user_client()
    if not spotify:
        return "Spotify再生機能を使用するには、まず 'spotify認証設定' と話しかけて認証を完了してください。"
    
    try:
        spotify.start_playback(context_uri=playlist_uri)
        
        # プレイリスト情報を取得
        playlist_id = playlist_uri.split(":")[-1]
        playlist = spotify.playlist(playlist_id, fields="name,owner")
        
        # デバッグログ: プレイリスト再生開始を出力
        print(f"[SPOTIFY_DEBUG] 📂 プレイリスト再生開始: 「{playlist['name']}」 by {playlist['owner']['display_name']}")
        
        # 少し待ってから現在再生中の楽曲情報も取得
        time.sleep(0.3)
        playback_info = spotify.current_playback()
        if playback_info and playback_info.get('item'):
            track_item = playback_info['item']
            track_name = track_item.get('name', '不明')
            artist_names = ", ".join([artist['name'] for artist in track_item.get('artists', [])]) if track_item.get('artists') else "不明"
            print(f"[SPOTIFY_DEBUG] 🎵 現在再生中: 「{track_name}」 by {artist_names}")
        
        return f"📂 プレイリストの再生を開始しました:\nプレイリスト: {playlist['name']}\n作成者: {playlist['owner']['display_name']}"
        
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 404:
            return "アクティブなSpotifyデバイスが見つかりません。Spotifyアプリを開いてデバイスをアクティブにしてください。"
        elif e.http_status == 401:
            return "Spotify認証が必要です。'spotify認証設定' と話しかけて認証を完了してください。"
        else:
            return f"プレイリスト再生エラー: {str(e)}"
    except Exception as e:
        return f"再生エラー: {str(e)}"