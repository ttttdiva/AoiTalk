"""話速調整キーワード検出器

「話速」「速度」「スピード」などのキーワードを検出し、
LLMを使って適切な話速調整係数を決定する。
"""

from typing import Optional, Dict, Any
import re
import json
import logging
from ..base import LLMKeywordDetector, KeywordDetectionResult, KeywordAction

logger = logging.getLogger(__name__)


class SpeechRateDetector(LLMKeywordDetector):
    """話速調整のキーワード検出器"""
    
    def __init__(
        self,
        llm_client: Any = None,
        enabled: bool = True,
        use_llm_extraction: bool = True,
        confidence_threshold: float = 0.7,
        fallback_to_regex: bool = True,
        config: Dict[str, Any] = None
    ):
        super().__init__("speech_rate", llm_client, enabled)
        self.use_llm_extraction = use_llm_extraction
        self.confidence_threshold = confidence_threshold
        self.fallback_to_regex = fallback_to_regex
        self.config = config or {}
        
        # 話速調整に関連するキーワード
        self.speed_keywords = [
            "話速", "話す速度", "話すスピード",
            "速度", "スピード", "speed",
            "早く", "速く", "ゆっくり", "遅く",
            "早め", "速め", "遅め", "ゆったり",
            "もっと早く", "もっと速く", "もっとゆっくり",
            "普通に", "通常に", "標準に", "デフォルト"
        ]
        
        # キーワードパターンの正規表現
        self.keyword_pattern = re.compile(
            r'(' + '|'.join(re.escape(kw) for kw in self.speed_keywords) + r')',
            re.IGNORECASE
        )
        
        # 現在の話速調整係数（グローバル状態として保持）
        self._current_speed_adjustment = 1.0
    
    def detect(self, text: str) -> KeywordDetectionResult:
        """テキストから話速調整キーワードを検出"""
        if not self.enabled:
            return KeywordDetectionResult(detected=False)
        
        # キーワードの存在をチェック
        if not self.keyword_pattern.search(text):
            return KeywordDetectionResult(detected=False)
        
        # LLMを使用して意図を抽出
        extraction_result = None
        if self.use_llm_extraction and self.llm_client:
            try:
                extraction_result = self._extract_with_llm(text)
            except Exception as e:
                logger.error(f"LLM抽出エラー: {e}")
                if not self.fallback_to_regex:
                    return KeywordDetectionResult(detected=False)
        
        # LLMが使用できない場合は正規表現で基本的な抽出
        if not extraction_result and self.fallback_to_regex:
            extraction_result = self._extract_with_regex(text)
        
        if extraction_result:
            return KeywordDetectionResult(
                detected=True,
                action=KeywordAction.PROCESS,
                data=extraction_result,
                bypass_llm=True  # LLM処理をバイパス（キーワード処理で完結）
            )
        
        return KeywordDetectionResult(detected=False)
    
    def _extract_with_llm(self, text: str) -> Optional[Dict[str, Any]]:
        """LLMを使って話速調整の意図を抽出"""
        prompt = f"""ユーザーのテキストから話速調整の意図を抽出してください。

入力テキスト: "{text}"

現在の話速調整係数: {self._current_speed_adjustment}

以下のJSON形式で回答してください：
{{
    "confidence": 0.0-1.0の数値（話速調整の意図の確信度）,
    "action": "increase" | "decrease" | "reset" | "set",
    "target_speed": 推奨される話速調整係数（0.5-2.0の範囲）,
    "description": "どのような調整か（例：「もっと速く」「ゆっくり」「普通に戻す」）"
}}

話速調整の目安：
- 「もっと速く」「早く」→ 現在の1.2-1.5倍
- 「速め」→ 現在の1.1-1.2倍
- 「ゆっくり」「遅く」→ 現在の0.7-0.8倍
- 「もっとゆっくり」→ 現在の0.5-0.7倍
- 「普通に」「標準に」→ 1.0にリセット

JSON以外の説明は不要です。"""
        
        try:
            # AgentLLMClientのインターフェースに合わせて呼び出し
            response = None
            if hasattr(self.llm_client, 'generate_simple'):
                response = self.llm_client.generate_simple(prompt)
            elif hasattr(self.llm_client, 'generate'):
                response = self.llm_client.generate(prompt)
            elif hasattr(self.llm_client, 'get_response'):
                response = self.llm_client.get_response([{"role": "user", "content": prompt}])
            else:
                # 直接APIを呼び出す
                import openai
                client = openai.OpenAI()
                completion = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}]
                )
                response = completion.choices[0].message.content
            
            if not response:
                return None
                
            # JSON部分を抽出
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                result = json.loads(json_match.group())
                
                # 確信度チェック
                if result.get('confidence', 0) >= self.confidence_threshold:
                    return result
        except Exception as e:
            logger.error(f"LLM話速抽出エラー: {e}")
        
        return None
    
    def _extract_with_regex(self, text: str) -> Optional[Dict[str, Any]]:
        """正規表現を使った基本的な話速調整抽出"""
        text_lower = text.lower()
        
        # 増速パターン
        if any(kw in text_lower for kw in ["もっと速く", "もっと早く", "すごく速く", "すごく早く"]):
            return {
                "action": "increase",
                "target_speed": min(self._current_speed_adjustment * 1.3, 2.0),
                "description": "もっと速く"
            }
        elif any(kw in text_lower for kw in ["速く", "早く", "速め", "早め"]):
            return {
                "action": "increase", 
                "target_speed": min(self._current_speed_adjustment * 1.15, 2.0),
                "description": "速く"
            }
        
        # 減速パターン
        elif any(kw in text_lower for kw in ["もっとゆっくり", "すごくゆっくり", "もっと遅く"]):
            return {
                "action": "decrease",
                "target_speed": max(self._current_speed_adjustment * 0.6, 0.5),
                "description": "もっとゆっくり"
            }
        elif any(kw in text_lower for kw in ["ゆっくり", "遅く", "遅め", "ゆったり"]):
            return {
                "action": "decrease",
                "target_speed": max(self._current_speed_adjustment * 0.8, 0.5),
                "description": "ゆっくり"
            }
        
        # リセットパターン
        elif any(kw in text_lower for kw in ["普通", "通常", "標準", "デフォルト", "元に"]):
            return {
                "action": "reset",
                "target_speed": 1.0,
                "description": "標準速度に戻す"
            }
        
        return None
    
    def process(self, result: KeywordDetectionResult) -> Optional[str]:
        """話速調整を実行"""
        if not result.data:
            return None
        
        data = result.data
        new_speed = data.get('target_speed', 1.0)
        description = data.get('description', '話速調整')
        
        # 話速調整を適用（設定ファイルに保存）
        try:
            # 現在の速度を保存
            old_speed = self._current_speed_adjustment
            
            # 新しい速度を適用
            self._apply_speed_adjustment(new_speed)
            self._current_speed_adjustment = new_speed
            
            # フィードバックメッセージ
            if new_speed == 1.0:
                return f"話速を標準に戻しました"
            elif new_speed > old_speed:
                return f"話速を速くしました（{new_speed:.1f}倍）"
            elif new_speed < old_speed:
                return f"話速をゆっくりにしました（{new_speed:.1f}倍）"
            else:
                return f"話速を{new_speed:.1f}倍に調整しました"
                
        except Exception as e:
            logger.error(f"話速調整エラー: {e}")
            return "話速の調整に失敗しました"
    
    def _apply_speed_adjustment(self, speed_adjustment: float):
        """設定ファイルに話速調整を保存"""
        import yaml
        import os
        
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../../../../config/config.yaml'
        )
        
        # 設定ファイルを読み込み
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # TTSセクションに話速調整を追加
        if 'tts' not in config:
            config['tts'] = {}
        
        config['tts']['speed_adjustment'] = float(speed_adjustment)
        
        # 設定ファイルに書き戻し
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)