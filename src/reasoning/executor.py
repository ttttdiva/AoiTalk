"""各ステップを実行するモジュール"""

import asyncio
import time
import logging
from typing import Dict, Any, Optional, List
from .models import TaskStep, StepResult, ReasoningContext
from .prompts import STEP_EXECUTION_PROMPT, ERROR_RECOVERY_PROMPT

logger = logging.getLogger(__name__)


class StepExecutor:
    """各ステップを実行し、結果を管理するクラス"""
    
    def __init__(self, llm_client, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            llm_client: LLMクライアント（必須）
            config: 実行設定
        """
        self.llm_client = llm_client
        self.config = config or {}
        self.step_timeout = self.config.get('step_timeout', 30)  # 各ステップのタイムアウト（秒）
        self.max_retries = self.config.get('max_retries', 3)
        self.retry_delay = self.config.get('retry_delay', 2)  # リトライ間隔（秒）
    
    async def execute_plan(self, context: ReasoningContext, progress_callback=None) -> List[StepResult]:
        """
        実行計画全体を実行
        
        Args:
            context: 推論実行コンテキスト
            progress_callback: 進捗通知用のコールバック関数
            
        Returns:
            List[StepResult]: 全ステップの実行結果
        """
        results = []
        
        # 全体のタイムアウトチェック
        overall_timeout = self.config.get('overall_timeout', 120)  # 2分
        
        while not self._all_steps_completed(context):
            # タイムアウトチェック
            if context.is_timeout(overall_timeout):
                logger.warning(f"Overall execution timeout after {overall_timeout}s")
                break
            
            # 実行可能なステップを取得
            executable_steps = context.plan.get_executable_steps(context.completed_steps)
            
            if not executable_steps:
                # 実行可能なステップがない（循環依存の可能性）
                logger.error("No executable steps found, possible circular dependency")
                break
            
            # 並列実行の可否を確認
            if self.config.get('parallel_execution', False) and len(executable_steps) > 1:
                # 並列実行
                step_results = await self._execute_parallel(executable_steps, context, progress_callback)
            else:
                # 順次実行
                step_results = await self._execute_sequential(executable_steps, context, progress_callback)
            
            # 結果をコンテキストに追加
            for result in step_results:
                context.add_result(result)
                results.append(result)
                
                # 失敗時のリカバリー
                if not result.success and result.step_id not in context.completed_steps:
                    recovery_result = await self._attempt_recovery(result, context)
                    if recovery_result:
                        context.add_result(recovery_result)
                        results.append(recovery_result)
        
        return results
    
    async def execute_step(self, step: TaskStep, context: ReasoningContext) -> StepResult:
        """
        単一ステップを実行
        
        Args:
            step: 実行するステップ
            context: 推論実行コンテキスト
            
        Returns:
            StepResult: ステップの実行結果
        """
        logger.info(f"Executing step: {step.id} - {step.description}")
        start_time = time.time()
        
        try:
            # プロンプトの構築
            prompt = self._build_step_prompt(step, context)
            
            # タイムアウト付きでLLMを実行
            result = await asyncio.wait_for(
                self._execute_with_llm(prompt, step),
                timeout=self.step_timeout
            )
            
            execution_time = time.time() - start_time
            
            # 成功結果を返す
            return StepResult(
                step_id=step.id,
                success=True,
                output=result.get('output'),
                execution_time=execution_time,
                tool_calls=result.get('tool_calls', []),
                metadata=result.get('metadata', {})
            )
            
        except asyncio.TimeoutError:
            logger.error(f"Step {step.id} timed out after {self.step_timeout}s")
            return StepResult(
                step_id=step.id,
                success=False,
                output=None,
                error=f"Timeout after {self.step_timeout} seconds",
                execution_time=time.time() - start_time
            )
        except Exception as e:
            logger.error(f"Step {step.id} failed with error: {e}")
            return StepResult(
                step_id=step.id,
                success=False,
                output=None,
                error=str(e),
                execution_time=time.time() - start_time
            )
    
    def _build_step_prompt(self, step: TaskStep, context: ReasoningContext) -> str:
        """ステップ実行用のプロンプトを構築"""
        # 前のステップの結果を整理
        previous_results = {}
        for dep_id in step.dependencies:
            if dep_id in context.step_results:
                result = context.step_results[dep_id]
                if result.success:
                    previous_results[dep_id] = {
                        'output': result.output,
                        'metadata': result.metadata
                    }
        
        # プロンプトを生成
        return STEP_EXECUTION_PROMPT.format(
            step_description=step.description,
            required_tools=', '.join(step.tool_requirements),
            previous_results=str(previous_results) if previous_results else "なし",
            shared_context=str(context.shared_data) if context.shared_data else "なし"
        )
    
    async def _execute_with_llm(self, prompt: str, step: TaskStep) -> Dict[str, Any]:
        """LLMを使用してステップを実行"""
        # 同期的なLLMクライアントを非同期で実行
        if hasattr(self.llm_client, 'generate_async'):
            # 非同期メソッドが利用可能
            response = await self.llm_client.generate_async(prompt)
        else:
            # 同期メソッドを非同期で実行
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.llm_client.generate, prompt)
        
        # レスポンスの解析
        # 実際の実装では、LLMのレスポンスからツール呼び出しと結果を抽出
        return {
            'output': response,
            'tool_calls': [],  # TODO: ツール呼び出しの抽出
            'metadata': {
                'prompt_length': len(prompt),
                'response_length': len(response)
            }
        }
    
    async def _execute_sequential(self, steps: List[TaskStep], context: ReasoningContext, 
                                  progress_callback=None) -> List[StepResult]:
        """ステップを順次実行"""
        results = []
        
        for step in steps:
            # 進捗通知
            if progress_callback:
                await progress_callback('executing', {
                    'current': len(context.completed_steps) + 1,
                    'total': len(context.plan.steps),
                    'step': step
                })
            
            # リトライ付きで実行
            result = None
            for attempt in range(self.max_retries):
                result = await self.execute_step(step, context)
                
                if result.success:
                    break
                
                if attempt < self.max_retries - 1:
                    logger.info(f"Retrying step {step.id} (attempt {attempt + 2}/{self.max_retries})")
                    await asyncio.sleep(self.retry_delay)
                    step.retry_count = attempt + 1
            
            results.append(result)
            
            # 進捗通知（完了）
            if progress_callback:
                status = 'step_complete' if result.success else 'step_failed'
                await progress_callback(status, {
                    'step': step,
                    'result': result
                })
            
            # 失敗時はオプショナルでない限り中断
            if not result.success and not step.optional:
                logger.warning(f"Required step {step.id} failed, stopping execution")
                break
        
        return results
    
    async def _execute_parallel(self, steps: List[TaskStep], context: ReasoningContext,
                                progress_callback=None) -> List[StepResult]:
        """ステップを並列実行"""
        # 並列実行タスクを作成
        tasks = []
        for step in steps:
            task = asyncio.create_task(self._execute_with_retry(step, context))
            tasks.append(task)
        
        # 全タスクの完了を待つ
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 例外を処理
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # 例外の場合はエラー結果を作成
                processed_results.append(StepResult(
                    step_id=steps[i].id,
                    success=False,
                    output=None,
                    error=str(result),
                    execution_time=0
                ))
            else:
                processed_results.append(result)
        
        return processed_results
    
    async def _execute_with_retry(self, step: TaskStep, context: ReasoningContext) -> StepResult:
        """リトライ付きでステップを実行"""
        for attempt in range(self.max_retries):
            result = await self.execute_step(step, context)
            
            if result.success:
                return result
            
            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_delay)
                step.retry_count = attempt + 1
        
        return result
    
    async def _attempt_recovery(self, failed_result: StepResult, context: ReasoningContext) -> Optional[StepResult]:
        """失敗したステップのリカバリーを試みる"""
        # 失敗したステップを取得
        failed_step = next((s for s in context.plan.steps if s.id == failed_result.step_id), None)
        if not failed_step:
            return None
        
        logger.info(f"Attempting recovery for failed step: {failed_step.id}")
        
        try:
            # リカバリープロンプトを構築
            prompt = ERROR_RECOVERY_PROMPT.format(
                failed_step=failed_step.description,
                error_message=failed_result.error,
                available_tools=', '.join(failed_step.tool_requirements)
            )
            
            # LLMにリカバリー方法を尋ねる
            recovery_plan = await self._execute_with_llm(prompt, failed_step)
            
            # リカバリーステップを作成して実行
            recovery_step = TaskStep(
                id=f"{failed_step.id}_recovery",
                description=f"Recovery: {recovery_plan.get('output', 'Alternative approach')}",
                tool_requirements=failed_step.tool_requirements,
                dependencies=failed_step.dependencies,
                expected_output_type=failed_step.expected_output_type
            )
            
            # リカバリーステップを実行
            recovery_result = await self.execute_step(recovery_step, context)
            
            # 成功した場合は元のステップIDで結果を返す
            if recovery_result.success:
                recovery_result.step_id = failed_step.id
                recovery_result.metadata['recovered'] = True
            
            return recovery_result
            
        except Exception as e:
            logger.error(f"Recovery attempt failed: {e}")
            return None
    
    def _all_steps_completed(self, context: ReasoningContext) -> bool:
        """すべてのステップが完了したかを確認"""
        required_steps = [s for s in context.plan.steps if not s.optional]
        completed_required = all(s.id in context.completed_steps for s in required_steps)
        
        # 必須ステップがすべて完了していればOK
        return completed_required
    
    def update_context(self, context: ReasoningContext, step_id: str, data: Dict[str, Any]):
        """実行コンテキストを更新"""
        # 共有データを更新
        context.shared_data[step_id] = data
        
        # 特定のデータタイプは自動的に共有
        if 'search_results' in data:
            context.shared_data['last_search_results'] = data['search_results']
        
        if 'created_item' in data:
            context.shared_data['last_created_item'] = data['created_item']