"""推論モードのデータモデル定義"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum
import time


class ReasoningMode(Enum):
    """推論モードの動作レベル"""
    SILENT = "silent"  # 最終結果のみ表示
    PROGRESS = "progress"  # 進捗状況を表示（デフォルト）
    DETAILED = "detailed"  # 各ステップの詳細を表示
    DEBUG = "debug"  # すべての内部処理を表示


@dataclass
class ComplexityScore:
    """タスクの複雑度スコア"""
    score: float  # 0.0-1.0
    factors: Dict[str, float]  # 各評価要素のスコア
    reasoning: str  # 判定理由
    requires_reasoning: bool = field(init=False)
    
    def __post_init__(self):
        self.requires_reasoning = self.score > 0.6  # デフォルト閾値


@dataclass
class TaskStep:
    """実行計画の個別ステップ"""
    id: str
    description: str
    tool_requirements: List[str]
    dependencies: List[str] = field(default_factory=list)  # 他のステップIDのリスト
    expected_output_type: str = "text"
    optional: bool = False  # オプショナルなステップかどうか
    retry_count: int = 0  # 現在のリトライ回数
    max_retries: int = 3  # 最大リトライ回数


@dataclass
class ExecutionPlan:
    """タスクの実行計画"""
    steps: List[TaskStep]
    dependencies_graph: Dict[str, List[str]]
    estimated_duration: float = 0.0
    user_feedback_enabled: bool = True
    created_at: float = field(default_factory=time.time)
    
    def get_executable_steps(self, completed_steps: List[str]) -> List[TaskStep]:
        """実行可能なステップを取得"""
        executable = []
        for step in self.steps:
            if step.id in completed_steps:
                continue
            # 依存関係がすべて完了しているか確認
            if all(dep in completed_steps for dep in step.dependencies):
                executable.append(step)
        return executable


@dataclass
class StepResult:
    """ステップ実行結果"""
    step_id: str
    success: bool
    output: Any
    error: Optional[str] = None
    execution_time: float = 0.0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """辞書形式に変換"""
        return {
            "step_id": self.step_id,
            "success": self.success,
            "output": str(self.output) if self.output else None,
            "error": self.error,
            "execution_time": self.execution_time,
            "tool_calls": self.tool_calls,
            "metadata": self.metadata
        }


@dataclass
class ReasoningContext:
    """推論実行のコンテキスト"""
    user_input: str
    plan: ExecutionPlan
    completed_steps: List[str] = field(default_factory=list)
    step_results: Dict[str, StepResult] = field(default_factory=dict)
    shared_data: Dict[str, Any] = field(default_factory=dict)  # ステップ間で共有するデータ
    start_time: float = field(default_factory=time.time)
    
    def add_result(self, result: StepResult):
        """実行結果を追加"""
        self.step_results[result.step_id] = result
        if result.success:
            self.completed_steps.append(result.step_id)
    
    def get_elapsed_time(self) -> float:
        """経過時間を取得"""
        return time.time() - self.start_time
    
    def is_timeout(self, timeout: float) -> bool:
        """タイムアウトしているか確認"""
        return self.get_elapsed_time() > timeout