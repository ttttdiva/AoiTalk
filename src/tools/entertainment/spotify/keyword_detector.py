"""
Spotify Auto Queue Keyword Detector
特定のキーワードを機械的に検知して自動キューを開始する
"""

import re
from typing import Optional, Dict, Any, Tuple
from .auto_queue_manager import get_auto_queue_manager


class SpotifyKeywordDetector:
    """Spotify自動キュー用のキーワード検出器"""
    
    def __init__(self):
        # 検索クエリ抽出用のキーワードパターン
        self.auto_queue_keywords = [
            "自動追加",
            "自動キュー", 
            "自動で追加",
            "自動で流して",
            "自動でキュー",
            "自動でキューに"
        ]
    
    def detect_auto_queue_request(self, text: str) -> Optional[Dict[str, Any]]:
        """
        テキストから自動キュー開始の指示を検出
        
        Args:
            text: 入力テキスト
            
        Returns:
            検出された場合は設定辞書、そうでなければNone
        """
        text_lower = text.lower()
        
        # 停止キーワードをチェック（「自動キュー」と「停止」の両方が含まれる場合）
        if "自動キュー" in text_lower and "停止" in text_lower:
            return {'action': 'stop'}
        
        # 開始キーワードをチェック（「自動」と「キュー」の両方が含まれる場合、または特定のフレーズが含まれる場合）
        has_auto_queue_intent = (
            ("自動" in text_lower and "キュー" in text_lower) or
            any(keyword in text_lower for keyword in self.auto_queue_keywords)
        )
        
        if has_auto_queue_intent:
            # キーワード周辺から検索クエリを抽出
            search_query = self._extract_search_query(text, "自動キュー")
            if not search_query:
                # 他のパターンも試す
                for keyword in self.auto_queue_keywords:
                    if keyword in text_lower:
                        search_query = self._extract_search_query(text, keyword)
                        if search_query:
                            break
            
            if search_query:
                return {
                    'action': 'start',
                    'search_query': search_query
                }
        
        return None
    
    def _extract_search_query(self, text: str, keyword: str) -> Optional[str]:
        """キーワード周辺から検索クエリを抽出"""
        # パターン1: "○○を自動追加"
        pattern1 = r'(.+?)を?' + re.escape(keyword)
        match = re.search(pattern1, text)
        if match:
            query = match.group(1).strip()
            if len(query) > 2:
                return query
        
        # パターン2: "自動追加して○○"
        pattern2 = re.escape(keyword) + r'して?(.+)'
        match = re.search(pattern2, text)
        if match:
            query = match.group(1).strip()
            if len(query) > 2:
                return query
        
        # パターン3: 前後の文脈から推測
        sentences = text.split('。')
        for sentence in sentences:
            if keyword in sentence:
                # 音楽に関連するワードを含む部分を抽出
                music_keywords = ['曲', '音楽', '歌', 'アーティスト', 'アルバム', 'ジャンル']
                if any(mk in sentence for mk in music_keywords):
                    # キーワードを除いた部分を返す
                    clean_sentence = sentence.replace(keyword, '').strip()
                    if len(clean_sentence) > 2:
                        return clean_sentence
        
        return None
    
    def _extract_interval(self, text: str) -> int:
        """間隔を抽出（デフォルト5分）"""
        # "5分間隔"、"10分おき"などのパターンを検索
        patterns = [
            r'(\d+)分間隔',
            r'(\d+)分おき',
            r'(\d+)分ごと',
            r'(\d+)分間'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                minutes = int(match.group(1))
                if 1 <= minutes <= 60:  # 1-60分の範囲
                    return minutes
        
        return 5  # デフォルト
    
    def _extract_track_count(self, text: str) -> int:
        """一度に追加する曲数を抽出（デフォルト3曲）"""
        # "5曲ずつ"、"3曲づつ"などのパターンを検索
        patterns = [
            r'(\d+)曲ずつ',
            r'(\d+)曲づつ',
            r'(\d+)曲まとめて',
            r'一度に(\d+)曲'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                count = int(match.group(1))
                if 1 <= count <= 10:  # 1-10曲の範囲
                    return count
        
        return 3  # デフォルト
    
    def _extract_variety_mode(self, text: str) -> str:
        """選曲モードを抽出（デフォルトmixed）"""
        if any(word in text for word in ['ランダム', 'バラバラ', 'シャッフル']):
            return 'random'
        elif any(word in text for word in ['人気', '有名', 'ヒット']):
            return 'popular'
        elif any(word in text for word in ['新しい', '発見', '知らない', 'マイナー']):
            return 'discovery'
        else:
            return 'mixed'  # デフォルト
    
    def process_message(self, text: str) -> Optional[str]:
        """
        メッセージを処理して自動キューの操作を実行
        
        Args:
            text: 入力テキスト
            
        Returns:
            実行結果のメッセージ（操作がなかった場合はNone）
        """
        detection = self.detect_auto_queue_request(text)
        if not detection:
            return None
        
        manager = get_auto_queue_manager()
        
        if detection['action'] == 'stop':
            result = manager.stop_auto_queue()
            return True  # Indicate keyword was processed, but let AI generate response
        
        elif detection['action'] == 'start':
            result = manager.start_auto_queue(detection['search_query'])
            return True  # Indicate keyword was processed, but let AI generate response
        
        return None


# グローバルインスタンス
_keyword_detector = None


def get_keyword_detector() -> SpotifyKeywordDetector:
    """キーワード検出器のシングルトンインスタンスを取得"""
    global _keyword_detector
    if _keyword_detector is None:
        _keyword_detector = SpotifyKeywordDetector()
    return _keyword_detector


def process_spotify_keywords(text: str) -> Optional[str]:
    """
    テキストからSpotify自動キューのキーワードを検出して処理
    
    Args:
        text: 入力テキスト
        
    Returns:
        処理結果のメッセージ（検出されなかった場合はNone）
    """
    detector = get_keyword_detector()
    return detector.process_message(text)