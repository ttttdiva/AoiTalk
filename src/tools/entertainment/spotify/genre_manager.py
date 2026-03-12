"""
Spotify Genre Manager - ジャンルシードの管理
Spotify Recommendations APIで使用可能なジャンルを取得・キャッシュ
"""

import json
import os
import time
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class SpotifyGenreManager:
    """Spotifyジャンル管理クラス"""
    
    def __init__(self):
        self.cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
        self.cache_file = os.path.join(self.cache_dir, 'genre_seeds.json')
        self.seed_genres_file = os.path.join(os.path.dirname(__file__), 'seed_genres.json')
        self.cache_duration = timedelta(days=7)  # 1週間キャッシュ
        self._genres = None
        self._last_update = None
        
        # キャッシュディレクトリを作成
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def get_available_genres(self, force_refresh: bool = False) -> List[str]:
        """
        利用可能なジャンルリストを取得
        
        Args:
            force_refresh: 強制更新フラグ
            
        Returns:
            ジャンルリスト
        """
        if self._should_refresh_cache(force_refresh):
            self._refresh_genre_cache()
        
        if self._genres is None:
            self._load_from_cache()
        
        return self._genres or []
    
    def find_genre_by_name(self, genre_name: str) -> Optional[str]:
        """
        ジャンル名で正確なジャンルシードを検索
        
        Args:
            genre_name: 検索するジャンル名
            
        Returns:
            マッチしたジャンルシード（完全一致優先、部分一致で補完）
        """
        genres = self.get_available_genres()
        if not genres:
            return None
        
        genre_lower = genre_name.lower()
        
        # 完全一致を優先
        for genre in genres:
            if genre.lower() == genre_lower:
                return genre
        
        # 部分一致をチェック
        partial_matches = []
        for genre in genres:
            if genre_lower in genre.lower() or genre.lower() in genre_lower:
                partial_matches.append(genre)
        
        if partial_matches:
            # 最も短いマッチを返す（最も具体的なものと仮定）
            return min(partial_matches, key=len)
        
        return None
    
    def get_genre_suggestions(self, query: str, limit: int = 5) -> List[str]:
        """
        ジャンル名の候補を取得
        
        Args:
            query: 検索クエリ
            limit: 返す候補数
            
        Returns:
            候補ジャンルリスト
        """
        genres = self.get_available_genres()
        if not genres:
            return []
        
        query_lower = query.lower()
        suggestions = []
        
        for genre in genres:
            if query_lower in genre.lower():
                suggestions.append(genre)
        
        # 完全一致を優先してソート
        suggestions.sort(key=lambda x: (
            x.lower() != query_lower,  # 完全一致を最初に
            len(x) - len(query),       # 長さの差を考慮
            x.lower()                  # アルファベット順
        ))
        
        return suggestions[:limit]
    
    def _should_refresh_cache(self, force_refresh: bool) -> bool:
        """キャッシュを更新すべきかチェック"""
        if force_refresh:
            return True
        
        if not os.path.exists(self.cache_file):
            return True
        
        # ファイルの更新時刻をチェック
        try:
            file_mtime = datetime.fromtimestamp(os.path.getmtime(self.cache_file))
            return datetime.now() - file_mtime > self.cache_duration
        except Exception as e:
            logger.warning(f"Failed to check cache file time: {e}")
            return True
    
    def _refresh_genre_cache(self):
        """ジャンルキャッシュを更新"""
        try:
            # seed_genres.jsonファイルから読み込む
            if os.path.exists(self.seed_genres_file):
                logger.info(f"Loading genre seeds from {self.seed_genres_file}")
                with open(self.seed_genres_file, 'r', encoding='utf-8') as f:
                    seed_data = json.load(f)
                    genres = seed_data.get('genres', [])
                    logger.info(f"Loaded {len(genres)} genres from seed file")
            else:
                # フォールバック：既知のジャンルリストを使用
                logger.warning(f"Seed genres file not found at {self.seed_genres_file}, using fallback list")
                genres = [
                    "acoustic", "afrobeat", "alt-rock", "alternative", "ambient", "anime", 
                    "black-metal", "bluegrass", "blues", "bossanova", "brazil", "breakbeat", 
                    "british", "cantopop", "chicago-house", "children", "chill", "classical", 
                    "club", "comedy", "country", "dance", "dancehall", "death-metal", 
                    "deep-house", "detroit-techno", "disco", "disney", "drum-and-bass", 
                    "dub", "dubstep", "edm", "electro", "electronic", "emo", "folk", 
                    "forro", "french", "funk", "garage", "german", "gospel", "goth", 
                    "grindcore", "groove", "grunge", "guitar", "happy", "hard-rock", 
                    "hardcore", "hardstyle", "heavy-metal", "hip-hop", "holidays", 
                    "honky-tonk", "house", "idm", "indian", "indie", "indie-pop", 
                    "industrial", "iranian", "j-dance", "j-idol", "j-pop", "j-rock", 
                    "jazz", "k-pop", "kids", "latin", "latino", "malay", "mandopop", 
                    "metal", "metal-misc", "metalcore", "minimal-techno", "movies", "mpb", 
                    "new-age", "new-release", "opera", "pagode", "party", "philippines-opm", 
                    "piano", "pop", "pop-film", "post-dubstep", "power-pop", 
                    "progressive-house", "psych-rock", "punk", "punk-rock", "r-n-b", 
                    "rainy-day", "reggae", "reggaeton", "road-trip", "rock", "rock-n-roll", 
                    "rockabilly", "romance", "sad", "salsa", "samba", "sertanejo", 
                    "show-tunes", "singer-songwriter", "ska", "sleep", "songwriter", "soul", 
                    "soundtrack", "spanish", "study", "summer", "swedish", "synth-pop", 
                    "tango", "techno", "trance", "trip-hop", "turkish", "work-out", "world-music"
                ]
            
            # 可能であればSpotify APIで一部検証
            try:
                from .auth import get_spotify_manager
                manager = get_spotify_manager()
                if manager:
                    spotify = manager._get_spotify_client()
                    if spotify:
                        # テスト用にいくつかのジャンルで検索してみる
                        test_genres = ["jazz", "rock", "pop", "electronic"]
                        verified_genres = []
                        
                        for genre in test_genres:
                            try:
                                # ジャンルで検索してみて有効性を確認
                                results = spotify.search(q=f"genre:{genre}", type='track', limit=1)
                                if results and results.get('tracks', {}).get('items'):
                                    verified_genres.append(genre)
                            except:
                                pass
                        
                        if verified_genres:
                            logger.info(f"Verified {len(verified_genres)} genres through API testing")
            except Exception as e:
                logger.debug(f"API verification failed (not critical): {e}")
            
            # キャッシュファイルに保存
            cache_data = {
                'genres': genres,
                'last_update': datetime.now().isoformat(),
                'source': 'predefined_extended_list',
                'note': 'Using predefined genres due to deprecated Spotify API'
            }
            
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            
            self._genres = genres
            self._last_update = datetime.now()
            
            logger.info(f"Genre cache updated with {len(genres)} predefined genres")
                
        except Exception as e:
            logger.error(f"Failed to refresh genre cache: {e}")
            # フォールバック：最小限のジャンルリスト
            fallback_genres = ["jazz", "rock", "pop", "classical", "electronic", "hip-hop", "country", "blues"]
            self._genres = fallback_genres
            logger.warning(f"Using fallback genre list with {len(fallback_genres)} genres")
    
    def _load_from_cache(self):
        """キャッシュファイルから読み込み"""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                
                self._genres = cache_data.get('genres', [])
                last_update_str = cache_data.get('last_update')
                
                if last_update_str:
                    self._last_update = datetime.fromisoformat(last_update_str)
                
                logger.info(f"Loaded {len(self._genres)} genres from cache")
            else:
                logger.warning("No genre cache file found")
                self._genres = []
                
        except Exception as e:
            logger.error(f"Failed to load genre cache: {e}")
            self._genres = []
    
    def get_cache_info(self) -> Dict[str, Any]:
        """キャッシュ情報を取得"""
        return {
            'cache_file': self.cache_file,
            'cache_exists': os.path.exists(self.cache_file),
            'genre_count': len(self._genres) if self._genres else 0,
            'last_update': self._last_update.isoformat() if self._last_update else None,
            'cache_duration_days': self.cache_duration.days
        }


# グローバルインスタンス
_genre_manager = None


def get_genre_manager() -> SpotifyGenreManager:
    """SpotifyGenreManagerのシングルトンインスタンスを取得"""
    global _genre_manager
    if _genre_manager is None:
        _genre_manager = SpotifyGenreManager()
    return _genre_manager


def get_available_genres() -> List[str]:
    """利用可能なジャンルリストを取得（簡易版）"""
    return get_genre_manager().get_available_genres()


def find_genre_by_name(genre_name: str) -> Optional[str]:
    """ジャンル名で正確なジャンルシードを検索（簡易版）"""
    return get_genre_manager().find_genre_by_name(genre_name)