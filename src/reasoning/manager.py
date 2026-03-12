"""推論モード全体の制御を行うマネージャー"""

import asyncio
import logging
from typing import Dict, Any, Optional, Callable, List
from .evaluator import ComplexityEvaluator
from .planner import ReasoningPlanner
from .executor import StepExecutor
from .generator import ResponseGenerator
from .models import ReasoningContext, ReasoningMode
from .prompts import PROGRESS_TEMPLATES

logger = logging.getLogger(__name__)


class ReasoningManager:
    """推論モード全体を管理するクラス"""
    
    def __init__(self, llm_client, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            llm_client: LLMクライアント（必須）
            config: 推論モード設定
        """
        self.llm_client = llm_client
        self.config = config or {}
        
        # 各コンポーネントの初期化
        self.evaluator = ComplexityEvaluator(llm_client, self.config)
        self.planner = ReasoningPlanner(llm_client, self.config)
        self.executor = StepExecutor(llm_client, self.config)
        self.generator = ResponseGenerator(llm_client, self.config)
        
        # 設定の読み込み
        self.enabled = self.config.get('enabled', True)
        self.complexity_threshold = self.config.get('complexity_threshold', 0.6)
        self.show_planning = self.config.get('show_planning', True)
        self.mode = ReasoningMode(self.config.get('display_mode', 'progress'))
    
    def evaluate_complexity(self, user_input: str, available_tools: Optional[List[str]] = None) -> float:
        """
        タスクの複雑度を評価
        
        Args:
            user_input: ユーザー入力
            available_tools: 利用可能なツールのリスト
            
        Returns:
            float: 複雑度スコア（0.0-1.0）
        """
        if not self.enabled:
            return 0.0
        
        tools = available_tools or []
        complexity = self.evaluator.evaluate(user_input, tools)
        
        logger.info(f"Complexity score: {complexity.score:.2f} - {complexity.reasoning}")
        
        return complexity.score
    
    async def execute_reasoning_mode(self, user_input: str, context: Dict[str, Any],
                                     progress_callback: Optional[Callable] = None) -> str:
        """
        推論モードでタスクを実行
        
        Args:
            user_input: ユーザー入力
            context: 実行コンテキスト（利用可能なツールなど）
            progress_callback: 進捗通知用のコールバック関数
            
        Returns:
            str: 最終的な応答
        """
        logger.info(f"Starting reasoning mode execution for: {user_input[:50]}...")
        
        try:
            # 1. タスクを分析
            if progress_callback and self.mode != ReasoningMode.SILENT:
                await self._notify_progress(progress_callback, 'analyzing', {})
            
            # 2. 実行計画を作成
            if progress_callback and self.mode != ReasoningMode.SILENT:
                await self._notify_progress(progress_callback, 'planning', {})
            
            plan = self.planner.create_plan(user_input, context)
            
            # 3. 計画を表示（設定による）
            if self.show_planning and progress_callback and self.mode != ReasoningMode.SILENT:
                await self._notify_progress(progress_callback, 'plan_display', {
                    'steps': plan.steps
                })
                # ユーザーが計画を確認できるよう少し待機
                await asyncio.sleep(1.0)
            
            # 4. 推論コンテキストを作成
            reasoning_context = ReasoningContext(
                user_input=user_input,
                plan=plan
            )
            
            # 5. ステップを実行
            results = await self.executor.execute_plan(
                reasoning_context, 
                self._create_progress_wrapper(progress_callback)
            )
            
            # 6. 最終応答を生成
            response = self.generator.generate_response(user_input, plan, results)
            
            # 7. 完了通知
            if progress_callback and self.mode != ReasoningMode.SILENT:
                status = 'complete' if all(r.success for r in results) else 'partial_complete'
                await self._notify_progress(progress_callback, status, {
                    'summary': self._extract_summary(response)
                })
            
            return response
            
        except asyncio.TimeoutError:
            logger.error("Reasoning mode execution timed out")
            return "申し訳ございません。処理がタイムアウトしました。タスクを簡略化してお試しください。"
        except Exception as e:
            logger.error(f"Reasoning mode execution failed: {e}")
            return f"申し訳ございません。推論モードの実行中にエラーが発生しました: {str(e)}"
    
    def _create_progress_wrapper(self, original_callback: Optional[Callable]) -> Optional[Callable]:
        """元のコールバックをラップして、推論モード固有の進捗情報を追加"""
        if not original_callback or self.mode == ReasoningMode.SILENT:
            return None
        
        async def wrapped_callback(stage: str, context: Dict[str, Any]):
            # 進捗メッセージを生成
            message = self.generator.generate_progress_message(stage, context)
            
            # 元のコールバックに渡す
            await original_callback('reasoning_progress', {
                'stage': stage,
                'message': message,
                'context': context
            })
        
        return wrapped_callback
    
    async def _notify_progress(self, callback: Callable, stage: str, context: Dict[str, Any]):
        """進捗を通知"""
        if callback:
            message = self.generator.generate_progress_message(stage, context)
            await callback('reasoning_progress', {
                'stage': stage,
                'message': message,
                'context': context
            })
    
    def _extract_summary(self, response: str) -> str:
        """応答から要約を抽出"""
        # 最初の1-2文を要約として使用
        lines = response.split('\n')
        summary_lines = []
        
        for line in lines:
            if line.strip():
                summary_lines.append(line.strip())
                if len(summary_lines) >= 2:
                    break
        
        return ' '.join(summary_lines) if summary_lines else response[:100]
    
    def update_config(self, config: Dict[str, Any]):
        """設定を更新"""
        self.config.update(config)
        
        # 設定の再読み込み
        self.enabled = self.config.get('enabled', True)
        self.complexity_threshold = self.config.get('complexity_threshold', 0.6)
        self.show_planning = self.config.get('show_planning', True)
        self.mode = ReasoningMode(self.config.get('display_mode', 'progress'))
        
        # 各コンポーネントの設定も更新
        self.evaluator.config = self.config
        self.planner.config = self.config
        self.executor.config = self.config
        self.generator.config = self.config
        self.generator.mode = self.mode
    
    def is_reasoning_required(self, user_input: str, available_tools: Optional[List[str]] = None) -> bool:
        """
        推論モードが必要かどうかを判定
        
        Args:
            user_input: ユーザー入力
            available_tools: 利用可能なツールのリスト
            
        Returns:
            bool: 推論モードが必要な場合True
        """
        if not self.enabled:
            return False
        
        complexity_score = self.evaluate_complexity(user_input, available_tools)
        return complexity_score > self.complexity_threshold