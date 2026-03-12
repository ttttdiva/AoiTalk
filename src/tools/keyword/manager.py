"""
キーワード検出器管理システム
"""

from typing import List, Optional, Dict, Any
from .base import KeywordDetectorBase, KeywordDetectionResult


class KeywordDetectorManager:
    """キーワード検出器管理クラス"""
    
    def __init__(self):
        """初期化"""
        self.detectors: List[KeywordDetectorBase] = []
        self._detector_map: Dict[str, KeywordDetectorBase] = {}
    
    def register_detector(self, detector: KeywordDetectorBase) -> None:
        """
        キーワード検出器を登録
        
        Args:
            detector: 検出器インスタンス
        """
        if detector.tool_name in self._detector_map:
            existing_detector = self._detector_map[detector.tool_name]
            print(f"[キーワード管理] 警告: {detector.tool_name} は既に登録されています")
            print(f"[キーワード管理] 既存のインスタンスID: {id(existing_detector)}, 新しいインスタンスID: {id(detector)}")
            return
        
        self.detectors.append(detector)
        self._detector_map[detector.tool_name] = detector
        print(f"[キーワード管理] {detector.tool_name} 検出器を登録しました (インスタンスID: {id(detector)})")
    
    def unregister_detector(self, tool_name: str) -> bool:
        """
        キーワード検出器を登録解除
        
        Args:
            tool_name: ツール名
            
        Returns:
            成功したかどうか
        """
        if tool_name not in self._detector_map:
            return False
        
        detector = self._detector_map[tool_name]
        self.detectors.remove(detector)
        del self._detector_map[tool_name]
        print(f"[キーワード管理] {tool_name} 検出器を登録解除しました")
        return True
    
    def get_detector(self, tool_name: str) -> Optional[KeywordDetectorBase]:
        """
        指定されたツール名の検出器を取得
        
        Args:
            tool_name: ツール名
            
        Returns:
            検出器インスタンス（存在しない場合はNone）
        """
        return self._detector_map.get(tool_name)
    
    def list_detectors(self) -> List[str]:
        """登録されている検出器の一覧を取得"""
        return list(self._detector_map.keys())
    
    def process_text(self, text: str) -> Optional[KeywordDetectionResult]:
        """
        テキストを全ての有効な検出器で処理
        
        Args:
            text: 入力テキスト
            
        Returns:
            最初に検出された結果（検出されなかった場合はNone）
        """
        for detector in self.detectors:
            if not detector.is_enabled():
                continue
            
            try:
                result = detector.detect(text)
                if result and result.detected:
                    # 検出された場合、実際の処理を実行
                    process_message = detector.process(result)
                    if process_message:
                        result.message = process_message
                    
                    print(f"[キーワード管理] {detector.tool_name} でキーワード検出: {result.action}")
                    return result
            except Exception as e:
                print(f"[キーワード管理] {detector.tool_name} 処理エラー: {e}")
                continue
        
        return None
    
    def set_detector_enabled(self, tool_name: str, enabled: bool) -> bool:
        """
        指定された検出器の有効/無効を設定
        
        Args:
            tool_name: ツール名
            enabled: 有効/無効フラグ
            
        Returns:
            成功したかどうか
        """
        detector = self.get_detector(tool_name)
        if not detector:
            return False
        
        detector.set_enabled(enabled)
        status = "有効" if enabled else "無効"
        print(f"[キーワード管理] {tool_name} を{status}に設定しました")
        return True
    
    def get_status(self) -> Dict[str, Any]:
        """
        管理システムの状態を取得
        
        Returns:
            状態情報
        """
        return {
            "total_detectors": len(self.detectors),
            "enabled_detectors": len([d for d in self.detectors if d.is_enabled()]),
            "detectors": {
                detector.tool_name: {
                    "enabled": detector.is_enabled(),
                    "keywords": detector.get_keywords()
                }
                for detector in self.detectors
            }
        }


# グローバルマネージャーインスタンス
_keyword_manager = None


def get_keyword_manager() -> KeywordDetectorManager:
    """キーワード検出器マネージャーのシングルトンインスタンスを取得"""
    global _keyword_manager
    if _keyword_manager is None:
        _keyword_manager = KeywordDetectorManager()
        print(f"[キーワード管理] マネージャーインスタンスを作成しました (ID: {id(_keyword_manager)})")
    else:
        print(f"[キーワード管理] 既存のマネージャーインスタンスを返します (ID: {id(_keyword_manager)})")
    return _keyword_manager


def process_keywords(text: str) -> Optional[KeywordDetectionResult]:
    """
    テキストからキーワードを検出して処理
    
    Args:
        text: 入力テキスト
        
    Returns:
        検出結果（検出されなかった場合はNone）
    """
    manager = get_keyword_manager()
    return manager.process_text(text)