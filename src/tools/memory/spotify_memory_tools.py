"""
Spotify activity memory and search tools for conversation memory
"""

import asyncio
from typing import List, Dict, Any, Optional
from ..core import tool as function_tool

from ..entertainment.spotify_logger import get_spotify_logger


@function_tool
def search_spotify_activity(
    query: str,
    days: int = 7,
    limit: int = 10,
    user_id: str = "default_user"
) -> str:
    """過去のSpotifyアクティビティを検索します
    
    Args:
        query: 検索クエリ（楽曲名、アーティスト名、リクエスト内容など）
        days: 検索対象期間（日数、デフォルト7日）
        limit: 最大結果数（デフォルト10）
        user_id: ユーザーID（デフォルト"default_user"）
        
    Returns:
        検索結果の文字列
    """
    try:
        logger = get_spotify_logger()
        
        # Run async search in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Convert days to hours for search
            hours = days * 24
            results = loop.run_until_complete(
                logger.search_activity(
                    user_id=user_id,
                    query=query,
                    limit=limit
                )
            )
            
            if not results:
                return f"「{query}」に関するSpotifyアクティビティが見つかりませんでした。"
            
            output = f"🎵 Spotifyアクティビティ検索結果 (「{query}」で{len(results)}件):\n\n"
            
            for i, activity in enumerate(results, 1):
                action_map = {
                    'play': '🎵 再生',
                    'pause': '⏸️ 一時停止',
                    'skip': '⏭️ スキップ',
                    'queue': '➕ キュー追加',
                    'create_playlist': '📂 プレイリスト作成',
                    'previous': '⏮️ 前の曲'
                }
                
                action_str = action_map.get(activity['action'], activity['action'])
                
                output += f"{i}. {action_str}"
                
                if activity.get('track_name'):
                    output += f" - {activity['track_name']}"
                    if activity.get('artist_name'):
                        output += f" / {activity['artist_name']}"
                
                if activity.get('playlist_name'):
                    output += f" (プレイリスト: {activity['playlist_name']})"
                
                # 時間情報
                if activity.get('created_at'):
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(activity['created_at'].replace('Z', '+00:00'))
                        output += f" - {dt.strftime('%m/%d %H:%M')}"
                    except:
                        pass
                
                # 成功/失敗
                if not activity.get('success', True):
                    output += " ❌失敗"
                    if activity.get('error_message'):
                        output += f" ({activity['error_message']})"
                
                output += "\n"
            
            return output.strip()
            
        finally:
            loop.close()
            
    except Exception as e:
        return f"Spotifyアクティビティ検索エラー: {e}"


@function_tool
def get_spotify_activity_stats(
    days: int = 7,
    user_id: str = "default_user"
) -> str:
    """Spotifyアクティビティ統計を取得します
    
    Args:
        days: 統計対象期間（日数、デフォルト7日）
        user_id: ユーザーID（デフォルト"default_user"）
        
    Returns:
        統計情報の文字列
    """
    try:
        logger = get_spotify_logger()
        
        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            stats = loop.run_until_complete(
                logger.get_activity_stats(
                    user_id=user_id,
                    days=days
                )
            )
            
            if not stats or stats.get('total_activities', 0) == 0:
                return f"過去{days}日間のSpotifyアクティビティがありません。"
            
            output = f"📊 Spotifyアクティビティ統計 (過去{days}日間):\n\n"
            
            # 基本統計
            output += f"総アクティビティ数: {stats.get('total_activities', 0)}回\n"
            
            # アクション内訳
            action_breakdown = stats.get('action_breakdown', {})
            if action_breakdown:
                output += "\n📈 アクション内訳:\n"
                
                action_map = {
                    'play': '🎵 再生',
                    'pause': '⏸️ 一時停止',
                    'skip': '⏭️ スキップ',
                    'queue': '➕ キュー追加',
                    'create_playlist': '📂 プレイリスト作成',
                    'previous': '⏮️ 前の曲'
                }
                
                for action, count in sorted(action_breakdown.items(), key=lambda x: x[1], reverse=True):
                    action_str = action_map.get(action, action)
                    output += f"  {action_str}: {count}回\n"
            
            # 最多アクション
            most_common = stats.get('most_common_action')
            if most_common:
                action_str = action_map.get(most_common, most_common)
                output += f"\n最も多いアクション: {action_str}\n"
            
            # トップ楽曲
            top_tracks = stats.get('top_tracks', [])
            if top_tracks:
                output += "\n🎵 よく聞いた楽曲 (Top 5):\n"
                for i, track in enumerate(top_tracks[:5], 1):
                    output += f"  {i}. {track['track_name']}"
                    if track.get('artist_name'):
                        output += f" - {track['artist_name']}"
                    output += f" ({track['play_count']}回)\n"
            
            # トップアーティスト
            top_artists = stats.get('top_artists', [])
            if top_artists:
                output += "\n🎤 よく聞いたアーティスト (Top 5):\n"
                for i, artist in enumerate(top_artists[:5], 1):
                    output += f"  {i}. {artist['artist_name']} ({artist['play_count']}回)\n"
            
            return output.strip()
            
        finally:
            loop.close()
            
    except Exception as e:
        return f"Spotifyアクティビティ統計取得エラー: {e}"


@function_tool
def get_recent_spotify_activity(
    hours: int = 24,
    limit: int = 20,
    user_id: str = "default_user"
) -> str:
    """最近のSpotifyアクティビティを取得します
    
    Args:
        hours: 取得対象期間（時間、デフォルト24時間）
        limit: 最大取得数（デフォルト20）
        user_id: ユーザーID（デフォルト"default_user"）
        
    Returns:
        最近のアクティビティの文字列
    """
    try:
        logger = get_spotify_logger()
        
        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            activities = loop.run_until_complete(
                logger.get_recent_activity(
                    user_id=user_id,
                    hours=hours,
                    limit=limit
                )
            )
            
            if not activities:
                return f"過去{hours}時間のSpotifyアクティビティがありません。"
            
            output = f"🕒 最近のSpotifyアクティビティ (過去{hours}時間、{len(activities)}件):\n\n"
            
            action_map = {
                'play': '🎵 再生',
                'pause': '⏸️ 一時停止',
                'skip': '⏭️ スキップ',
                'queue': '➕ キュー追加',
                'create_playlist': '📂 プレイリスト作成',
                'previous': '⏮️ 前の曲'
            }
            
            for i, activity in enumerate(activities, 1):
                action_str = action_map.get(activity['action'], activity['action'])
                
                output += f"{i}. {action_str}"
                
                if activity.get('track_name'):
                    output += f" - {activity['track_name']}"
                    if activity.get('artist_name'):
                        output += f" / {activity['artist_name']}"
                
                # 時間情報
                if activity.get('created_at'):
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(activity['created_at'].replace('Z', '+00:00'))
                        output += f" ({dt.strftime('%m/%d %H:%M')})"
                    except:
                        pass
                
                # エラー情報
                if not activity.get('success', True):
                    output += " ❌失敗"
                
                output += "\n"
            
            return output.strip()
            
        finally:
            loop.close()
            
    except Exception as e:
        return f"最近のSpotifyアクティビティ取得エラー: {e}"


@function_tool
def get_spotify_listening_patterns(
    days: int = 30,
    user_id: str = "default_user"
) -> str:
    """Spotify聴取パターンの分析を行います
    
    Args:
        days: 分析対象期間（日数、デフォルト30日）
        user_id: ユーザーID（デフォルト"default_user"）
        
    Returns:
        聴取パターン分析結果の文字列
    """
    try:
        logger = get_spotify_logger()
        
        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # 基本統計を取得
            stats = loop.run_until_complete(
                logger.get_activity_stats(
                    user_id=user_id,
                    days=days
                )
            )
            
            # 詳細なアクティビティを取得
            recent_activities = loop.run_until_complete(
                logger.get_recent_activity(
                    user_id=user_id,
                    hours=days * 24,
                    limit=1000  # 大量のデータを取得
                )
            )
            
            if not stats or stats.get('total_activities', 0) == 0:
                return f"過去{days}日間のSpotifyアクティビティがないため、パターン分析できません。"
            
            output = f"🎯 Spotify聴取パターン分析 (過去{days}日間):\n\n"
            
            # 基本統計
            total_activities = stats.get('total_activities', 0)
            output += f"📊 基本統計:\n"
            output += f"  総アクティビティ: {total_activities}回\n"
            output += f"  1日平均: {total_activities / days:.1f}回\n"
            
            # 行動パターン分析
            action_breakdown = stats.get('action_breakdown', {})
            if action_breakdown:
                play_count = action_breakdown.get('play', 0)
                skip_count = action_breakdown.get('skip', 0)
                queue_count = action_breakdown.get('queue', 0)
                
                output += f"\n🎵 聴取行動分析:\n"
                if play_count > 0:
                    output += f"  再生数: {play_count}回\n"
                    if skip_count > 0:
                        skip_rate = (skip_count / play_count) * 100
                        output += f"  スキップ率: {skip_rate:.1f}%\n"
                        
                        if skip_rate > 50:
                            output += f"  → スキップが多め。新しい楽曲を探している可能性\n"
                        elif skip_rate < 20:
                            output += f"  → 安定した聴取パターン。好みの楽曲が多い\n"
                    
                    if queue_count > 0:
                        queue_ratio = (queue_count / play_count) * 100
                        output += f"  キュー利用率: {queue_ratio:.1f}%\n"
                        
                        if queue_ratio > 30:
                            output += f"  → 計画的な音楽聴取スタイル\n"
            
            # 楽曲・アーティスト嗜好
            top_tracks = stats.get('top_tracks', [])
            top_artists = stats.get('top_artists', [])
            
            if top_tracks or top_artists:
                output += f"\n🎼 音楽嗜好分析:\n"
                
                if top_tracks:
                    favorite_track = top_tracks[0]
                    output += f"  最もよく聞いた楽曲: {favorite_track['track_name']}"
                    if favorite_track.get('artist_name'):
                        output += f" - {favorite_track['artist_name']}"
                    output += f" ({favorite_track['play_count']}回)\n"
                
                if top_artists:
                    favorite_artist = top_artists[0]
                    output += f"  最もよく聞いたアーティスト: {favorite_artist['artist_name']} ({favorite_artist['play_count']}回)\n"
                
                # 多様性分析
                if len(top_artists) >= 3:
                    total_artist_plays = sum(artist['play_count'] for artist in top_artists[:3])
                    top3_ratio = (total_artist_plays / action_breakdown.get('play', 1)) * 100
                    
                    if top3_ratio > 60:
                        output += f"  → 特定のアーティストを集中的に聴く傾向\n"
                    else:
                        output += f"  → 多様なアーティストを楽しむ傾向\n"
            
            # おすすめ
            output += f"\n💡 聴取パターンに基づくおすすめ:\n"
            
            if action_breakdown.get('play', 0) > action_breakdown.get('queue', 0) * 2:
                output += f"  • キュー機能を活用すると、より快適に音楽を楽しめそうです\n"
            
            if action_breakdown.get('skip', 0) > action_breakdown.get('play', 0) * 0.3:
                output += f"  • 新しいアーティストや楽曲の発見に興味がありそうです\n"
            
            if len(top_artists) < 5 and total_activities > 50:
                output += f"  • より多様なアーティストを試してみると新しい発見があるかもしれません\n"
            
            return output.strip()
            
        finally:
            loop.close()
            
    except Exception as e:
        return f"Spotify聴取パターン分析エラー: {e}"