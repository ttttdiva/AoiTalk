"""
Spotify自動再生監視モジュール
"""

import threading
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 自動再生モニタリング用の変数
_last_track_id: Optional[str] = None
_monitoring_active: bool = False
_skip_in_progress: bool = False  # スキップ処理中フラグ
_monitor_reset_requested: bool = False  # 監視システムリセット要求フラグ
_monitor_thread: Optional[threading.Thread] = None


def get_last_track_id() -> Optional[str]:
    """最後に再生された楽曲IDを取得"""
    return _last_track_id


def update_last_track_id(track_id: str):
    """最後に再生された楽曲IDを更新"""
    global _last_track_id
    _last_track_id = track_id


def is_monitoring_active() -> bool:
    """監視が有効かどうかを確認"""
    return _monitoring_active


def is_skip_in_progress() -> bool:
    """スキップ処理中かどうかを確認"""
    return _skip_in_progress


def set_skip_in_progress(value: bool):
    """スキップ処理中フラグを設定"""
    global _skip_in_progress
    _skip_in_progress = value


def request_monitor_reset():
    """監視システムのリセットを要求"""
    global _monitor_reset_requested
    _monitor_reset_requested = True


def is_monitor_reset_requested() -> bool:
    """監視システムのリセットが要求されているかを確認"""
    return _monitor_reset_requested


def clear_monitor_reset_request():
    """監視システムのリセット要求をクリア"""
    global _monitor_reset_requested
    _monitor_reset_requested = False


def start_auto_play_monitoring():
    """自動再生モニタリングを開始"""
    global _monitoring_active, _monitor_thread
    
    if _monitoring_active:
        return "自動再生監視は既に実行中です。"
    
    _monitoring_active = True
    _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
    _monitor_thread.start()
    
    logger.info("自動再生監視を開始しました")
    return "自動再生監視を開始しました。"


def stop_auto_play_monitoring():
    """自動再生モニタリングを停止"""
    global _monitoring_active
    
    if not _monitoring_active:
        return "自動再生監視は実行されていません。"
    
    _monitoring_active = False
    
    # スレッドの終了を待つ
    if _monitor_thread and _monitor_thread.is_alive():
        _monitor_thread.join(timeout=5.0)
    
    logger.info("自動再生監視を停止しました")
    return "自動再生監視を停止しました。"


def _monitor_loop():
    """監視のメインループ（リファクタリング版）"""
    from .auth import get_spotify_manager
    from .queue_system import get_internal_queue
    from ...keyword.spotify.track_change_detector import get_track_detector
    from ...keyword.spotify.auto_queue_trigger import get_auto_queue_trigger
    from ...keyword.spotify.auto_queue_manager import get_auto_queue_manager
    from .api_client import get_api_client
    
    global _last_track_id, _monitor_reset_requested, _skip_in_progress
    
    logger.info("[AutoPlay] 監視ループ開始（リファクタリング版）")
    
    # 専用モジュールを取得
    track_detector = get_track_detector()
    auto_trigger = get_auto_queue_trigger()
    api_client = get_api_client()
    
    while _monitoring_active:
        try:
            # スキップ処理中は監視をスキップ
            if _skip_in_progress:
                time.sleep(0.5)
                continue
            
            # Spotify クライアント取得
            manager = get_spotify_manager()
            if not manager:
                time.sleep(5.0)
                continue
            
            spotify = manager._get_spotify_user_client()
            if not spotify:
                time.sleep(5.0)
                continue
            
            # 現在の再生状態を安全に取得
            current_playback = api_client.get_current_playback_safe(spotify)
            if current_playback is None:
                continue  # エラーハンドリングはapi_clientで処理済み
            
            # 再生停止時の処理
            if not current_playback.get('is_playing'):
                _handle_playback_stopped(spotify, api_client)
                time.sleep(2.0)
                continue
            
            # 現在の楽曲情報を取得
            current_track = current_playback.get('item')
            if not current_track:
                time.sleep(2.0)
                continue
            
            current_track_id = current_track['id']
            progress_ms = current_playback.get('progress_ms', 0)
            duration_ms = current_track.get('duration_ms', 0)
            
            # 監視システムリセット処理
            if _monitor_reset_requested:
                track_detector.reset_tracking(current_track_id, progress_ms)
                _last_track_id = current_track_id
                _monitor_reset_requested = False
                time.sleep(2.0)
                continue
            
            # 楽曲変更検知と自動キュー判定
            change_info = track_detector.check_track_change(current_track_id, progress_ms, duration_ms)
            
            if change_info['changed']:
                # トリガーの状態をリセット
                auto_trigger.on_track_change(current_track_id)
                _handle_track_change(change_info, auto_trigger)
                _last_track_id = current_track_id
            
            # 事前読み込み判定
            if change_info['near_completion']:
                _handle_near_completion(auto_trigger, change_info['completion_ratio'])
            
            time.sleep(10.0)  # 10秒間隔でチェック（APIレート制限対策）
            
        except Exception as e:
            logger.error(f"[AutoPlay] 監視ループ予期しないエラー: {e}")
            time.sleep(5.0)
    
    logger.info("[AutoPlay] 監視ループ終了")


def _handle_playback_stopped(spotify, api_client):
    """再生停止時の処理"""
    from .queue_system import get_internal_queue
    
    internal_queue = get_internal_queue()
    if internal_queue.has_next():
        next_track = internal_queue.get_next()
        if next_track:
            logger.info(f"[AutoPlay] 自動再生：{next_track['name']}")
            
            success = api_client.start_playback_safe(spotify, [next_track['uri']])
            if success:
                global _last_track_id
                _last_track_id = next_track['uri'].split(':')[-1]
                logger.info(f"[AutoPlay] 楽曲ID更新: {_last_track_id}")
                
                # デバッグログ: 自動再生開始した楽曲情報を出力
                try:
                    track_name = next_track.get('name', '不明')
                    # 内部キューではアーティスト情報は'artist'フィールドに文字列として保存されている
                    artist_names = next_track.get('artist', '不明')
                    print(f"[SPOTIFY_DEBUG] 🎵 自動再生開始: 「{track_name}」 by {artist_names}")
                except Exception as e:
                    print(f"[SPOTIFY_DEBUG] 🎵 自動再生開始: {next_track.get('name', '不明')} (アーティスト情報取得エラー: {e})")
                    # デバッグ用: next_trackの構造を出力
                    print(f"[SPOTIFY_DEBUG] next_track構造: {list(next_track.keys()) if isinstance(next_track, dict) else type(next_track)}")
                
                # 追加: 実際の再生情報もSpotify APIから取得して表示
                try:
                    import time
                    time.sleep(0.3)  # 再生開始まで少し待つ
                    current_playback = spotify.current_playback()
                    if current_playback and current_playback.get('item'):
                        current_item = current_playback['item']
                        current_track_name = current_item.get('name', '不明')
                        current_artists = ", ".join([artist['name'] for artist in current_item.get('artists', [])]) if current_item.get('artists') else "不明"
                        print(f"[SPOTIFY_DEBUG] 🎵 自動再生確認: 「{current_track_name}」 by {current_artists}")
                except Exception as e:
                    print(f"[SPOTIFY_DEBUG] 自動再生確認エラー: {e}")


def _handle_track_change(change_info, auto_trigger):
    """楽曲変更時の処理"""
    from .queue_system import get_internal_queue
    from ...keyword.spotify.auto_queue_manager import get_auto_queue_manager
    
    internal_queue = get_internal_queue()
    auto_queue = get_auto_queue_manager()
    
    # 自動キュー追加判定
    trigger_decision = auto_trigger.should_add_track(
        change_info['change_type'],
        internal_queue.size(),
        auto_queue.is_enabled()
    )
    
    # 自動キューが有効な場合のみログ出力
    if auto_queue.is_enabled():
        print(f"[DEBUG] 楽曲変更後チェック: {trigger_decision['reason']}")
    
    if trigger_decision['should_add']:
        print(f"[DEBUG] 自動キュー追加条件を満たしています（{trigger_decision['trigger_type']}）")
        auto_trigger.add_track_and_log(trigger_decision['trigger_type'])
    elif auto_queue.is_enabled():
        print(f"[DEBUG] 自動キュー追加条件不満足: {trigger_decision['reason']}")


def _handle_near_completion(auto_trigger, completion_ratio):
    """楽曲完了接近時の処理"""
    from .queue_system import get_internal_queue
    from ...keyword.spotify.auto_queue_manager import get_auto_queue_manager
    
    internal_queue = get_internal_queue()
    auto_queue = get_auto_queue_manager()
    
    # 事前読み込み判定
    preload_decision = auto_trigger.should_preload_track(
        completion_ratio,
        internal_queue.size(),
        auto_queue.is_enabled()
    )
    
    # 自動キューが有効な場合のみログ出力
    if auto_queue.is_enabled():
        print(f"[DEBUG] 完了前準備チェック: {preload_decision['reason']}")
    
    if preload_decision['should_preload']:
        print("[DEBUG] 自動キュー追加条件を満たしています（完了前準備）")
        auto_trigger.add_track_and_log('preload')