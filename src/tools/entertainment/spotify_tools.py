"""
Spotify音楽再生関連のツール - リファクタリング後の統合インターフェース

このファイルは後方互換性のために残されており、
実際の実装は src/tools/entertainment/spotify/ モジュールに移動されました。
"""

# 新しいモジュール構造からすべての機能をインポート
from .spotify import *

# 互換性維持のための関数エイリアス
def init_spotify():
    """Spotifyを初期化（レガシー関数名）"""
    return initialize_spotify()

# 自動初期化（元の動作を維持）
import os
if os.getenv('SPOTIFY_CLIENT_ID') and os.getenv('SPOTIFY_CLIENT_SECRET'):
    try:
        initialize_spotify()
    except Exception as e:
        print(f"[Spotify] 自動初期化警告: {e}")

from ..core import tool as function_tool

# 自動キュー開始はキーワード検知でのみ処理（function_toolから削除）
def start_auto_queue(search_query: str) -> str:
    """
    指定されたアーティストや曲の楽曲を自動的にキューに追加し続ける
    キューが1曲以下になると自動的に新しい曲を追加
    
    注意：この関数はキーワード検知システムからのみ呼び出される。
    LLMのfunction callingからは使用不可。
    
    Args:
        search_query: 検索クエリ（アーティスト名や曲名）
    
    Returns:
        開始結果のメッセージ
    """
    from .spotify.auto_queue_manager import get_auto_queue_manager
    manager = get_auto_queue_manager()
    
    result = manager.start_auto_queue(search_query)
    
    if result.get('status') == 'started':
        if result.get('initial_track_added'):
            return f"🎵 自動キュー機能を開始しました！\n「{search_query}」の曲を1曲追加しました。\n今後も自動的に追加し続けます。"
        else:
            return f"⚠️ 自動キュー機能は開始しましたが、「{search_query}」の曲が見つかりませんでした。\n別の検索キーワードをお試しください。"
    else:
        return f"自動キュー機能の開始に失敗しました: {result}"

# 自動キュー停止はキーワード検知でのみ処理（function_toolから削除）
def stop_auto_queue() -> str:
    """
    自動キュー追加機能を停止する
    
    注意：この関数はキーワード検知システムからのみ呼び出される。
    LLMのfunction callingからは使用不可。
    
    Returns:
        停止結果のメッセージ
    """
    from .spotify.auto_queue_manager import get_auto_queue_manager
    manager = get_auto_queue_manager()
    result = manager.stop_auto_queue()
    
    if result.get('status') == 'stopped':
        return "⏹️ 自動キュー機能を停止しました。"
    else:
        return f"自動キュー機能の停止に失敗しました: {result}"

@function_tool
def check_queue_sync_status() -> str:
    """
    キューの同期状態を確認し、失敗した同期があれば報告する
    
    Returns:
        同期状態の詳細レポート
    """
    from .spotify.auto_queue_manager import get_auto_queue_manager
    from .spotify.queue_system import get_internal_queue
    
    # 自動キューマネージャーから同期失敗情報を取得
    auto_queue = get_auto_queue_manager()
    sync_failures = auto_queue.get_sync_failures()
    
    # 内部キューの状態を取得
    internal_queue = get_internal_queue()
    queue_size = internal_queue.size()
    
    # レポート生成
    report = f"🔍 キュー同期状態レポート:\n\n"
    report += f"📊 内部キューサイズ: {queue_size}曲\n"
    report += f"🤖 自動キュー状態: {'有効' if auto_queue.is_enabled() else '無効'}\n"
    
    if sync_failures:
        report += f"\n⚠️ 同期失敗: {len(sync_failures)}件\n"
        for i, failure in enumerate(sync_failures[-3:], 1):  # 最新3件表示
            report += f"  {i}. {failure['track_name']} - {failure['error'][:50]}...\n"
        
        if len(sync_failures) > 3:
            report += f"  (他 {len(sync_failures) - 3}件)\n"
    else:
        report += f"\n✅ 同期状態: 正常（失敗なし）\n"
    
    return report

@function_tool
def retry_queue_sync() -> str:
    """
    失敗したキュー同期を再試行する
    
    Returns:
        再試行結果のレポート
    """
    from .spotify.auto_queue_manager import get_auto_queue_manager
    
    auto_queue = get_auto_queue_manager()
    result = auto_queue.retry_failed_syncs()
    
    if 'error' in result:
        return f"❌ 同期再試行エラー: {result['error']}"
    
    if result['retried'] == 0:
        return "ℹ️ 再試行する同期失敗はありませんでした。"
    
    report = f"🔄 同期再試行結果:\n"
    report += f"  • 再試行: {result['retried']}件\n"
    report += f"  • 成功: {result['succeeded']}件\n"
    report += f"  • 失敗: {result['failed']}件\n"
    
    if result['succeeded'] > 0:
        report += f"\n✅ {result['succeeded']}件の同期が正常に復旧しました。"
    
    if result['failed'] > 0:
        report += f"\n⚠️ {result['failed']}件はまだ同期に失敗しています。"
    
    return report

@function_tool
def spotify_system_health_check() -> str:
    """
    Spotifyシステム全体の健全性をチェックする
    
    Returns:
        システム健全性の詳細レポート
    """
    import time
    from .spotify.auto_queue_manager import get_auto_queue_manager
    from .spotify.queue_system import get_internal_queue
    from .spotify.monitoring import is_monitoring_active
    from .spotify.api_client import get_api_client
    from .spotify.auth import get_spotify_manager
    
    report = "🔧 Spotify システム健全性チェック\n\n"
    
    # 1. 認証状態チェック
    manager = get_spotify_manager()
    if manager:
        spotify_client = manager._get_spotify_client()
        user_client = manager._get_spotify_user_client()
        
        report += "🔐 認証状態:\n"
        report += f"  • 検索クライアント: {'✅ 正常' if spotify_client else '❌ エラー'}\n"
        report += f"  • ユーザークライアント: {'✅ 正常' if user_client else '❌ エラー'}\n"
    else:
        report += "🔐 認証状態: ❌ Spotifyマネージャー未初期化\n"
    
    # 2. 監視システム状態
    monitoring_active = is_monitoring_active()
    report += f"\n👁️ 監視システム: {'✅ アクティブ' if monitoring_active else '❌ 非アクティブ'}\n"
    
    # 3. API健全性
    api_client = get_api_client()
    health = api_client.get_health_status()
    health_emoji = {'healthy': '✅', 'degraded': '⚠️', 'unhealthy': '❌'}
    
    report += f"\n🌐 API健全性: {health_emoji.get(health['health_status'], '❓')} {health['health_status']}\n"
    report += f"  • 連続タイムアウト: {health['consecutive_timeouts']}回\n"
    report += f"  • 最終成功: {health['time_since_last_success']:.1f}秒前\n"
    
    # 4. キュー状態
    internal_queue = get_internal_queue()
    auto_queue = get_auto_queue_manager()
    sync_failures = auto_queue.get_sync_failures()
    
    report += f"\n📊 キュー状態:\n"
    report += f"  • 内部キューサイズ: {internal_queue.size()}曲\n"
    report += f"  • 自動キュー: {'✅ 有効' if auto_queue.is_enabled() else '⏹️ 無効'}\n"
    report += f"  • 同期失敗: {len(sync_failures)}件\n"
    
    if auto_queue.is_enabled():
        report += f"  • 検索クエリ: '{auto_queue.search_query}'\n"
    
    # 5. 総合判定
    issues = []
    if not manager: issues.append("認証未初期化")
    if not monitoring_active: issues.append("監視停止")
    if health['health_status'] == 'unhealthy': issues.append("API不安定")
    if len(sync_failures) > 3: issues.append("同期失敗多数")
    
    if issues:
        report += f"\n⚠️ 検出された問題: {', '.join(issues)}\n"
        report += "💡 推奨アクション: システム再起動または個別修復を検討してください。"
    else:
        report += "\n✅ システム状態: 正常動作中"
    
    return report

@function_tool
def reset_spotify_monitoring() -> str:
    """
    Spotify監視システムをリセットする
    
    Returns:
        リセット結果のメッセージ
    """
    import time
    from .spotify.monitoring import request_monitor_reset, is_monitoring_active
    from .spotify.track_change_detector import reset_track_detection
    from .spotify.api_client import get_api_client
    
    if not is_monitoring_active():
        return "❌ 監視システムが非アクティブのためリセットできません。"
    
    try:
        # 監視システムのリセット要求
        request_monitor_reset()
        
        # 楽曲追跡のリセット
        reset_track_detection("unknown")
        
        # API健全性のリセット
        api_client = get_api_client()
        api_client.consecutive_timeouts = 0
        api_client.last_success_time = time.time()
        
        return "✅ Spotify監視システムをリセットしました。次回のループで新しい状態から開始されます。"
        
    except Exception as e:
        return f"❌ 監視システムリセットエラー: {e}"

