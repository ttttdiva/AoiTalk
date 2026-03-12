"""
LLMベースSpotify自動キューキーワード検出器
"""

import json
from typing import Optional, Dict, Any
from ..base import LLMKeywordDetector, KeywordDetectionResult, KeywordAction


class SpotifyLLMKeywordDetector(LLMKeywordDetector):
    """LLMを使用するSpotify自動キュー検出器"""
    
    def __init__(self, enabled: bool = True, llm_client=None):
        """
        初期化
        
        Args:
            enabled: 有効/無効フラグ
            llm_client: LLMクライアント
        """
        super().__init__("spotify", enabled, llm_client)
        
        # 検出対象キーワード
        self.keywords = [
            "自動追加", "自動キュー", "自動で追加", "自動で流して",
            "自動でキュー", "自動でキューに", "オートキュー"
        ]
        
        # LLM抽出用プロンプト
        self.extraction_prompt = """
以下のテキストからSpotify音楽検索に必要な情報を抽出してください。

タスク:
1. 「自動キュー」「自動追加」などの自動再生機能に関する指示かどうかを判定
2. 停止指示（「停止」「やめて」）かどうかを判定  
3. アーティスト名、ジャンルなどの音楽検索クエリを抽出
4. 検索タイプ（アーティスト、ジャンル）を判定

出力形式（JSON）:
{
    "is_auto_queue": true/false,
    "is_stop": true/false,
    "search_query": "抽出された検索クエリ（アーティスト名、ジャンル名など）",
    "search_type": "artist/genre",
    "confidence": 0.0-1.0
}

ジャンル判定ルール:
- "ジャズ"、"ロック"、"ポップ"、"クラシック"、"電子音楽"などの音楽ジャンル名
- "〇〇系の音楽"、"〇〇な曲"のような表現
- アーティスト名が特定されていない場合

例:
- 入力: "usedcvntって人の曲を自動キューで追加して"
  出力: {"is_auto_queue": true, "is_stop": false, "search_query": "usedcvnt", "search_type": "artist", "confidence": 0.9}
  
- 入力: "ジャズを自動キューで追加して"
  出力: {"is_auto_queue": true, "is_stop": false, "search_query": "ジャズ", "search_type": "genre", "confidence": 0.95}

- 入力: "ロックな曲を自動で流して"
  出力: {"is_auto_queue": true, "is_stop": false, "search_query": "ロック", "search_type": "genre", "confidence": 0.9}

- 入力: "自動キューを停止して"
  出力: {"is_auto_queue": true, "is_stop": true, "search_query": "", "search_type": "", "confidence": 1.0}
"""
    
    def detect(self, text: str) -> KeywordDetectionResult:
        """
        テキストからSpotify自動キューのキーワードを検出
        
        Args:
            text: 入力テキスト
            
        Returns:
            検出結果
        """
        if not self.enabled:
            return KeywordDetectionResult()
        
        text_lower = text.lower()
        
        # 基本的なキーワード存在チェック
        has_auto_queue = any(keyword in text_lower for keyword in self.keywords)
        has_stop = "停止" in text_lower and ("自動キュー" in text_lower or "オートキュー" in text_lower)
        
        if not has_auto_queue and not has_stop:
            return KeywordDetectionResult()
        
        # LLMベース抽出を試行
        extraction_result = self._extract_with_llm_json(text)
        
        if extraction_result:
            # LLM抽出結果を使用
            if extraction_result.get("is_stop", False):
                return KeywordDetectionResult(
                    detected=True,
                    action=KeywordAction.STOP,
                    tool_name=self.tool_name,
                    parameters={"action": "stop"},
                    bypass_llm=True
                )
            elif extraction_result.get("is_auto_queue", False) and extraction_result.get("search_query"):
                return KeywordDetectionResult(
                    detected=True,
                    action=KeywordAction.PROCESS,
                    tool_name=self.tool_name,
                    parameters={
                        "action": "start",
                        "search_query": extraction_result["search_query"],
                        "search_type": extraction_result.get("search_type", "artist"),
                        "confidence": extraction_result.get("confidence", 0.8)
                    },
                    bypass_llm=True
                )
        
        # LLM抽出が失敗した場合、従来の正規表現ベースにフォールバック
        fallback_result = self._fallback_extraction(text, text_lower)
        if fallback_result:
            return fallback_result
        
        return KeywordDetectionResult()
    
    def process(self, result: KeywordDetectionResult) -> Optional[str]:
        """
        検出されたキーワードを処理
        
        Args:
            result: 検出結果
            
        Returns:
            処理結果メッセージ
        """
        if not result.detected:
            return None
        
        try:
            # Spotify自動キューマネージャーを取得
            from ..spotify.auto_queue_manager import get_auto_queue_manager
            manager = get_auto_queue_manager()
            
            if result.action == KeywordAction.STOP:
                manager.stop_auto_queue()
                return "Spotify自動キューを停止しました"
            elif result.action == KeywordAction.PROCESS:
                search_query = result.parameters.get("search_query")
                search_type = result.parameters.get("search_type", "artist")
                
                if search_query:
                    # 設定から除外ウィンドウパラメータを読み込む
                    kwargs = {}
                    try:
                        from ....config import Config
                        config = Config()
                        auto_queue_config = config.get('keyword_detection', {}).get('spotify', {}).get('auto_queue', {})
                        
                        if 'exclude_window_size' in auto_queue_config:
                            kwargs['exclude_window_size'] = auto_queue_config['exclude_window_size']
                        if 'exclude_hours' in auto_queue_config:
                            kwargs['exclude_hours'] = auto_queue_config['exclude_hours']
                    except Exception as e:
                        print(f"[Spotify検出器] 設定読み込みエラー: {e}")
                    
                    # ユーザーとキャラクター情報を追加（利用可能な場合）
                    kwargs['user_id'] = 'default'  # 実際の実装ではコンテキストから取得
                    kwargs['character_name'] = 'default'  # 実際の実装ではコンテキストから取得
                    kwargs['search_type'] = search_type  # 検索タイプを追加
                    
                    manager.start_auto_queue(search_query, **kwargs)
                    confidence = result.parameters.get("confidence", 0.8)
                    search_type_jp = {"artist": "アーティスト", "genre": "ジャンル"}.get(search_type, search_type)
                    return f"「{search_query}」（{search_type_jp}）で自動キューを開始しました（信頼度: {confidence:.2f}）"
            
        except Exception as e:
            print(f"[Spotify検出器] 処理エラー: {e}")
            return f"Spotify自動キューの処理中にエラーが発生しました: {e}"
        
        return None
    
    def _extract_with_llm_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        LLMを使ってJSON形式で情報を抽出
        
        Args:
            text: 入力テキスト
            
        Returns:
            抽出結果（辞書形式）
        """
        if not self.llm_client:
            return None
        
        try:
            # LLMに抽出を依頼
            response = self._extract_with_llm(text, self.extraction_prompt)
            if not response:
                return None
            
            # JSON解析を試行
            try:
                # レスポンスからJSON部分を抽出
                response = response.strip()
                if response.startswith('```json'):
                    response = response[7:]
                if response.endswith('```'):
                    response = response[:-3]
                response = response.strip()
                
                result = json.loads(response)
                
                # 必要なフィールドの検証
                if isinstance(result, dict) and "is_auto_queue" in result:
                    return result
                    
            except json.JSONDecodeError as e:
                print(f"[Spotify検出器] JSON解析エラー: {e}")
                # JSONが無効な場合、テキスト解析にフォールバック
                return self._parse_llm_text_response(response)
        
        except Exception as e:
            print(f"[Spotify検出器] LLM抽出エラー: {e}")
        
        return None
    
    def _parse_llm_text_response(self, response: str) -> Optional[Dict[str, Any]]:
        """
        LLMのテキストレスポンスを解析
        
        Args:
            response: LLMレスポンス
            
        Returns:
            抽出結果
        """
        response_lower = response.lower()
        
        # 基本的なパターンマッチング
        is_auto_queue = any(keyword in response_lower for keyword in ["自動キュー", "auto queue", "自動追加"])
        is_stop = "停止" in response_lower or "stop" in response_lower
        
        # 検索クエリの抽出を試行
        search_query = ""
        lines = response.split('\n')
        for line in lines:
            if 'search_query' in line.lower() or '検索クエリ' in line:
                # シンプルな抽出パターン
                parts = line.split(':')
                if len(parts) > 1:
                    search_query = parts[1].strip().strip('"').strip("'")
                    break
        
        if is_auto_queue or is_stop:
            return {
                "is_auto_queue": is_auto_queue,
                "is_stop": is_stop,
                "search_query": search_query,
                "confidence": 0.7  # テキスト解析の場合は低めの信頼度
            }
        
        return None
    
    def _fallback_extraction(self, text: str, text_lower: str) -> Optional[KeywordDetectionResult]:
        """
        正規表現ベースのフォールバック抽出
        
        Args:
            text: 入力テキスト
            text_lower: 小文字変換済みテキスト
            
        Returns:
            検出結果
        """
        # 停止キーワードチェック
        if "自動キュー" in text_lower and "停止" in text_lower:
            return KeywordDetectionResult(
                detected=True,
                action=KeywordAction.STOP,
                tool_name=self.tool_name,
                parameters={"action": "stop"},
                bypass_llm=True
            )
        
        # 自動キュー開始キーワードチェック
        if "自動" in text_lower and "キュー" in text_lower:
            # 簡単なクエリ抽出を試行
            import re
            patterns = [
                r'(.+?)を?自動キュー',
                r'(.+?)を?自動で追加',
                r'(.+?)を?自動でキューに'
            ]
            
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    query = match.group(1).strip()
                    if len(query) > 2:
                        return KeywordDetectionResult(
                            detected=True,
                            action=KeywordAction.PROCESS,
                            tool_name=self.tool_name,
                            parameters={
                                "action": "start",
                                "search_query": query,
                                "search_type": "artist",  # フォールバックではアーティストと仮定
                                "confidence": 0.6  # フォールバックの場合は低めの信頼度
                            },
                            bypass_llm=True
                        )
        
        return None