"""
Spotify Recommendations Engine
ジャンルベースの楽曲推薦機能
"""

import os
import logging
import random
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class SpotifyRecommendationsEngine:
    """Spotify Recommendations APIを使用した楽曲推薦エンジン"""
    
    def __init__(self):
        self.default_params = {
            'limit': 50,
            # 'market': 'JP',  # Client Credentials Flowでは使用不可
            'min_popularity': 30,  # 最小人気度
            'max_popularity': 100   # 最大人気度
        }
    
    def get_tracks_by_genre(self, genre: str, limit: int = 50, **kwargs) -> List[Dict[str, Any]]:
        """
        ジャンルに基づいて楽曲を推薦
        Recommendations APIが利用できない場合は検索APIを使用
        
        Args:
            genre: ジャンル名（SpotifyのジャンルシードまたはLLMが変換したもの）
            limit: 取得する楽曲数（最大100）
            **kwargs: 追加のRecommendationsパラメータ
            
        Returns:
            推薦楽曲のリスト
        """
        try:
            from .auth import get_spotify_manager
            manager = get_spotify_manager()
            if not manager:
                logger.error("Spotify manager not available")
                return []
            
            spotify = manager._get_spotify_client()
            if not spotify:
                logger.error("Spotify client not available")
                return []
            
            # ジャンル名を正規化
            normalized_genre = self._normalize_genre(genre)
            if not normalized_genre:
                logger.warning(f"Could not normalize genre: {genre}")
                return []
            
            # Recommendations APIは非推奨(404エラー)のため、直接検索APIを使用
            # 2024年現在、Spotify Recommendations APIは利用不可
            logger.debug(f"Using search API for genre: {normalized_genre} (Recommendations API is deprecated)")
            
            # Recommendations APIが失敗した場合、検索APIを使用
            return self._get_tracks_by_genre_search(normalized_genre, limit)
            
        except Exception as e:
            logger.error(f"Failed to get tracks for genre {genre}: {e}")
            return []
    
    def _get_tracks_by_genre_search(self, genre: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        検索APIを使用したジャンルベースの楽曲取得
        
        Args:
            genre: 正規化されたジャンル名
            limit: 取得する楽曲数
            
        Returns:
            楽曲リスト
        """
        try:
            from .auth import get_spotify_manager
            manager = get_spotify_manager()
            spotify = manager._get_spotify_client()
            
            # ジャンル検索クエリを構築
            search_queries = [
                f'genre:"{genre}"',  # 正確なジャンル
                f'genre:{genre}',    # ジャンル名
                f'{genre}',          # 一般検索
            ]
            
            all_tracks = []
            tracks_per_query = max(1, limit // len(search_queries))
            
            for query in search_queries:
                try:
                    logger.debug(f"Searching with query: {query}")
                    results = spotify.search(q=query, type='track', limit=tracks_per_query)
                    
                    if results and 'tracks' in results:
                        tracks = results['tracks']['items']
                        
                        # 重複を避けるためURIでフィルタリング
                        existing_uris = {track['uri'] for track in all_tracks}
                        new_tracks = [track for track in tracks if track['uri'] not in existing_uris]
                        
                        all_tracks.extend(new_tracks)
                        logger.debug(f"Added {len(new_tracks)} new tracks from query: {query}")
                        
                        if len(all_tracks) >= limit:
                            break
                
                except Exception as search_error:
                    logger.debug(f"Search query failed: {query} - {search_error}")
                    continue
            
            # 人気度でソート（高い順）
            all_tracks.sort(key=lambda x: x.get('popularity', 0), reverse=True)
            
            # 指定された数に制限
            result_tracks = all_tracks[:limit]
            
            logger.info(f"Retrieved {len(result_tracks)} tracks for genre '{genre}' via search")
            return result_tracks
            
        except Exception as e:
            logger.error(f"Search-based genre discovery failed: {e}")
            return []
    
    def get_mixed_recommendations(self, 
                                  seed_genres: List[str] = None,
                                  seed_artists: List[str] = None,
                                  seed_tracks: List[str] = None,
                                  limit: int = 50,
                                  **kwargs) -> List[Dict[str, Any]]:
        """
        複数のシードを使用した混合推薦
        
        Args:
            seed_genres: ジャンルシードのリスト（最大5個）
            seed_artists: アーティストIDのリスト（最大5個）
            seed_tracks: トラックIDのリスト（最大5個）
            limit: 取得する楽曲数
            **kwargs: 追加のRecommendationsパラメータ
            
        Returns:
            推薦楽曲のリスト
        """
        try:
            from .auth import get_spotify_manager
            manager = get_spotify_manager()
            if not manager:
                return []
            
            spotify = manager._get_spotify_client()
            if not spotify:
                return []
            
            # シード合計数は最大5個まで
            total_seeds = len(seed_genres or []) + len(seed_artists or []) + len(seed_tracks or [])
            if total_seeds == 0:
                logger.warning("No seeds provided for recommendations")
                return []
            
            if total_seeds > 5:
                logger.warning(f"Too many seeds ({total_seeds}), Spotify API allows maximum 5")
                # 必要に応じてシードを調整
                seed_genres = (seed_genres or [])[:2]
                seed_artists = (seed_artists or [])[:2]
                seed_tracks = (seed_tracks or [])[:1]
            
            # パラメータを構築
            params = self.default_params.copy()
            params['limit'] = min(limit, 100)
            params.update(kwargs)
            
            if seed_genres:
                # ジャンルを正規化
                normalized_genres = []
                for genre in seed_genres:
                    normalized = self._normalize_genre(genre)
                    if normalized:
                        normalized_genres.append(normalized)
                
                if normalized_genres:
                    params['seed_genres'] = normalized_genres
            
            if seed_artists:
                params['seed_artists'] = seed_artists
            
            if seed_tracks:
                params['seed_tracks'] = seed_tracks
            
            logger.info(f"Getting mixed recommendations with seeds: genres={seed_genres}, artists={seed_artists}, tracks={seed_tracks}")
            
            recommendations = spotify.recommendations(**params)
            
            if recommendations and 'tracks' in recommendations:
                tracks = recommendations['tracks']
                logger.info(f"Retrieved {len(tracks)} mixed recommendations")
                return tracks
            
        except Exception as e:
            logger.error(f"Failed to get mixed recommendations: {e}")
        
        return []
    
    def _normalize_genre(self, genre_input: str) -> Optional[str]:
        """
        ジャンル名を正規化してSpotifyのジャンルシードにマッピング
        
        Args:
            genre_input: 入力ジャンル名（日本語or英語）
            
        Returns:
            正規化されたジャンルシード
        """
        try:
            from .genre_manager import get_genre_manager
            genre_manager = get_genre_manager()
            
            # まず直接マッチを試してみる
            genre_lower = genre_input.lower().strip()
            
            # Spotify ジャンルシードから正確なマッチを検索
            exact_match = genre_manager.find_genre_by_name(genre_lower)
            if exact_match:
                logger.debug(f"Found exact genre match: '{genre_input}' -> '{exact_match}'")
                return exact_match
            
            # 部分マッチを試行
            suggestions = genre_manager.get_genre_suggestions(genre_lower, limit=1)
            if suggestions:
                logger.debug(f"Found genre suggestion: '{genre_input}' -> '{suggestions[0]}'")
                return suggestions[0]
            
            # LLMを使用してジャンルマッピングを試行
            try:
                from src.llm.manager import AgentLLMClient
                from src.config import Config
                
                # 利用可能なジャンルリストを取得
                available_genres = genre_manager.get_available_genres()
                
                # LLMで適切なジャンルを選択
                prompt = f"""
以下の入力ジャンル名に最も近いSpotifyジャンルシードを選んでください。

入力: {genre_input}

利用可能なSpotifyジャンルシード:
{', '.join(available_genres)}

ルール:
- 上記のリストから必ず1つを選んでください
- 日本語入力の場合、意味に最も近い英語ジャンルを選択
- 例: "ジャズ" → "jazz", "フレンチ" → "french", "アニメ" → "anime"
- ジャンル名のみを返答し、余計な説明は不要

選択したジャンル:"""
                
                # APIキーを取得
                api_key = os.getenv('OPENAI_API_KEY')
                if not api_key:
                    logger.debug("OpenAI API key not found for genre mapping")
                    return None
                
                # 簡易的なconfigを作成
                config = Config()
                
                # LLMクライアントを作成
                llm_client = AgentLLMClient(api_key=api_key, model="gpt-4o-mini", config=config)
                response = llm_client.generate_response(prompt)
                
                if response:
                    # レスポンスからジャンル名を抽出
                    mapped_genre = response.strip().lower()
                    # 引用符や余計な文字を除去
                    mapped_genre = mapped_genre.strip('"\'「」')
                    
                    # 再度検証
                    if mapped_genre in available_genres:
                        logger.info(f"LLM mapped genre: '{genre_input}' -> '{mapped_genre}'")
                        return mapped_genre
                    
            except Exception as e:
                logger.debug(f"LLM genre mapping failed: {e}")
            
            logger.warning(f"Could not normalize genre: {genre_input}")
            return None
            
        except Exception as e:
            logger.error(f"Genre normalization failed for '{genre_input}': {e}")
            return None
    
    def get_audio_features_recommendations(self, 
                                           genre: str,
                                           energy: float = None,
                                           danceability: float = None,
                                           valence: float = None,
                                           tempo: int = None,
                                           limit: int = 50) -> List[Dict[str, Any]]:
        """
        オーディオ特徴量を指定したジャンル推薦
        
        Args:
            genre: ジャンル名
            energy: エネルギー（0.0-1.0）
            danceability: ダンサビリティ（0.0-1.0）
            valence: ポジティブさ（0.0-1.0）
            tempo: BPM
            limit: 取得する楽曲数
            
        Returns:
            推薦楽曲のリスト
        """
        kwargs = {}
        
        if energy is not None:
            kwargs['target_energy'] = energy
        if danceability is not None:
            kwargs['target_danceability'] = danceability
        if valence is not None:
            kwargs['target_valence'] = valence
        if tempo is not None:
            kwargs['target_tempo'] = tempo
        
        return self.get_tracks_by_genre(genre, limit=limit, **kwargs)


# グローバルインスタンス
_recommendations_engine = None


def get_recommendations_engine() -> SpotifyRecommendationsEngine:
    """SpotifyRecommendationsEngineのシングルトンインスタンス取得"""
    global _recommendations_engine
    if _recommendations_engine is None:
        _recommendations_engine = SpotifyRecommendationsEngine()
    return _recommendations_engine


def get_tracks_by_genre(genre: str, limit: int = 50) -> List[Dict[str, Any]]:
    """ジャンルベース楽曲推薦（簡易版）"""
    return get_recommendations_engine().get_tracks_by_genre(genre, limit=limit)