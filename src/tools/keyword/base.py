"""
汎用キーワード検出システム - ベースクラス
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum


class KeywordAction(Enum):
    """キーワード検出アクション"""
    PROCESS = "process"  # 処理を実行
    STOP = "stop"       # 処理を停止
    TOGGLE = "toggle"   # 状態を切り替え


@dataclass
class KeywordDetectionResult:
    """キーワード検出結果"""
    detected: bool = False
    action: Optional[KeywordAction] = None
    tool_name: str = ""
    parameters: Dict[str, Any] = None
    message: str = ""
    bypass_llm: bool = True  # LLM処理をバイパスするか
    
    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}


class KeywordDetectorBase(ABC):
    """キーワード検出器の基底クラス"""
    
    def __init__(self, tool_name: str, enabled: bool = True):
        """
        初期化
        
        Args:
            tool_name: ツール名
            enabled: 有効/無効フラグ
        """
        self.tool_name = tool_name
        self.enabled = enabled
        self.keywords = []
    
    @abstractmethod
    def detect(self, text: str) -> KeywordDetectionResult:
        """
        テキストからキーワードを検出
        
        Args:
            text: 入力テキスト
            
        Returns:
            検出結果
        """
        pass
    
    @abstractmethod
    def process(self, result: KeywordDetectionResult) -> Optional[str]:
        """
        検出されたキーワードを処理
        
        Args:
            result: 検出結果
            
        Returns:
            処理結果メッセージ（処理しなかった場合はNone）
        """
        pass
    
    def get_keywords(self) -> List[str]:
        """対応キーワード一覧を取得"""
        return self.keywords.copy()
    
    def is_enabled(self) -> bool:
        """有効状態を確認"""
        return self.enabled
    
    def set_enabled(self, enabled: bool):
        """有効/無効を設定"""
        self.enabled = enabled


class LLMKeywordDetector(KeywordDetectorBase):
    """LLMを使用するキーワード検出器の基底クラス"""
    
    def __init__(self, tool_name: str, enabled: bool = True, llm_client=None):
        """
        初期化
        
        Args:
            tool_name: ツール名
            enabled: 有効/無効フラグ
            llm_client: LLMクライアント
        """
        super().__init__(tool_name, enabled)
        self.llm_client = llm_client
    
    def _extract_with_llm(self, text: str, prompt: str) -> Optional[str]:
        """
        LLMを使ってテキストから情報を抽出
        
        Args:
            text: 入力テキスト
            prompt: 抽出用プロンプト
            
        Returns:
            抽出結果
        """
        if not self.llm_client:
            return None
            
        try:
            full_prompt = f"{prompt}\n\n入力テキスト: {text}"
            response = self.llm_client.generate_response(full_prompt)
            return response.strip() if response else None
        except Exception as e:
            print(f"[{self.tool_name}] LLM抽出エラー: {e}")
            return None