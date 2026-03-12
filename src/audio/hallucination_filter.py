"""
統合されたハルシネーション・エコー検出フィルター
各音声認識エンジンから共通で使用可能
"""
from typing import Optional, Dict, Any


class HallucinationFilter:
    """統合されたハルシネーション・エコー検出フィルター"""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize hallucination filter
        
        Args:
            config: Configuration dictionary
        """
        self.config = config or {}
        
        # 繰り返し検出設定
        self.max_repetitions = self.config.get('max_repetitions', 3)  # 最大繰り返し回数
        self.min_repeat_length = self.config.get('min_repeat_length', 2)  # 最小繰り返し単位長
        
        # Whisper特有のハルシネーションパターン
        self.whisper_patterns = {
            'ご視聴', '視聴', 'チャンネル登録', '高評価', 'コメント欄',
            '次回の動画', '次の動画', '最後まで見て', '動画をご覧', 'ご覧いただき',
            'お聞きいただき', '配信', 'ライブ', '今回は以上',
            'それでは、また', 'さようなら', 'それでは、お待ちしております',
            'お会いしましょう', 'お楽しみに', '楽しみにして',
            'ありがとうございました',  # 追加
            'see you', 'bye bye',
            'thank you for watching', 'thanks for watching',
            '[音楽]', '[拍手]', '[無音]', '[♪', '♪',
            '(音楽)', '(拍手)', 'subtitle', 'transcribe', 'amara.org'
        }
        
        
    def is_hallucination(self, 
                        text: str, 
                        engine: Optional[str] = None) -> bool:
        """
        統合されたハルシネーション判定
        
        Args:
            text: 検査するテキスト
            engine: 音声認識エンジン名 ('whisper', 'google', etc.)
        
        Returns:
            True if hallucination detected
        """
        if not text or not text.strip():
            return True
            
        text = text.strip()
        
        # 繰り返しパターンをチェック
        if self._is_repetitive_hallucination(text):
            return True
            
        # エンジン固有のチェック
        if engine == 'whisper' and self._is_whisper_hallucination(text):
            return True
            
        return False
        
        
    def _is_repetitive_hallucination(self, text: str) -> bool:
        """繰り返しパターンによるハルシネーションをチェック"""
        import re
        
        # まず、完全に同じ文の繰り返しをチェック（「スイカの作り方を紹介します。」の例）
        # 文を句点で分割
        sentences = [s.strip() for s in text.split('。') if s.strip()]
        if len(sentences) >= 2:
            # 各文の出現回数をカウント
            sentence_counts = {}
            for sentence in sentences:
                if len(sentence) >= self.min_repeat_length:
                    sentence_counts[sentence] = sentence_counts.get(sentence, 0) + 1
            
            # 最大繰り返し回数を超えているかチェック
            if sentence_counts:
                max_sent_count = max(sentence_counts.values())
                if max_sent_count >= self.max_repetitions:
                    repeated_sentence = [s for s, c in sentence_counts.items() if c == max_sent_count][0]
                    print(f"[HallucinationFilter] 文の繰り返しハルシネーション検出: '{repeated_sentence}' が {max_sent_count} 回繰り返されています")
                    return True
        
        # 次に、フレーズレベルの繰り返しをチェック
        # 句読点や空白で分割してフレーズを取得
        phrases = re.split(r'[。、.,\s]+', text.strip())
        phrases = [p.strip() for p in phrases if p.strip() and len(p.strip()) >= self.min_repeat_length]
        
        if len(phrases) < self.max_repetitions:
            return False
            
        # 同じフレーズの出現回数をカウント
        phrase_counts = {}
        for phrase in phrases:
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1
            
        # 最大繰り返し回数を超えているかチェック
        max_count = max(phrase_counts.values()) if phrase_counts else 0
        if max_count >= self.max_repetitions:
            repeated_phrase = [phrase for phrase, count in phrase_counts.items() if count == max_count][0]
            print(f"[HallucinationFilter] フレーズの繰り返しハルシネーション検出: '{repeated_phrase}' が {max_count} 回繰り返されています")
            return True
            
        # 連続する同じフレーズをチェック
        consecutive_count = 1
        for i in range(1, len(phrases)):
            if phrases[i] == phrases[i-1]:
                consecutive_count += 1
                if consecutive_count >= self.max_repetitions:
                    print(f"[HallucinationFilter] 連続繰り返しハルシネーション検出: '{phrases[i]}' が {consecutive_count} 回連続しています")
                    return True
            else:
                consecutive_count = 1
        
        # 繰り返しパターンの比率をチェック（全体の50%以上が同じフレーズの場合）
        if phrases and max_count > 1:
            repetition_ratio = max_count / len(phrases)
            if repetition_ratio >= 0.5:  # 50%以上が同じフレーズ
                repeated_phrase = [phrase for phrase, count in phrase_counts.items() if count == max_count][0]
                print(f"[HallucinationFilter] 高比率繰り返しハルシネーション検出: '{repeated_phrase}' が全体の {repetition_ratio:.1%} を占めています")
                return True
                
        return False
        
    def _is_whisper_hallucination(self, text: str) -> bool:
        """Whisper特有のハルシネーションをチェック"""
        text_lower = text.lower()
        
        # Whisperパターンをチェック
        if any(pattern in text_lower for pattern in self.whisper_patterns):
            return True
        
        # 「おやすみなさい」は除外するが、「おやすみ」だけの場合は許可
        if 'おやすみなさい' in text_lower and text_lower.strip() != 'おやすみ':
            return True
                
        return False
        
    def add_output_context(self, text: str) -> None:
        """Add assistant's output for echo detection
        
        Args:
            text: Assistant's output text
        """
        # This method is currently a placeholder
        # Could be extended to store recent outputs for echo detection
        pass