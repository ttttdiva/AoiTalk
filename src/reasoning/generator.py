"""全ステップの結果を統合して最終応答を生成するモジュール"""

import logging
from typing import List, Dict, Any, Optional
from .models import ExecutionPlan, StepResult, ReasoningMode
from .prompts import RESPONSE_GENERATION_PROMPT, format_execution_results

logger = logging.getLogger(__name__)


class ResponseGenerator:
    """全ステップの結果を統合して最終応答を生成するクラス"""
    
    def __init__(self, llm_client=None, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            llm_client: LLMクライアント（高度な応答生成に使用）
            config: 生成設定
        """
        self.llm_client = llm_client
        self.config = config or {}
        self.mode = ReasoningMode(self.config.get('display_mode', 'progress'))
    
    def generate_response(self, user_input: str, plan: ExecutionPlan, 
                          results: List[StepResult]) -> str:
        """
        ステップ実行結果から自然な応答を生成
        
        Args:
            user_input: 元のユーザー入力
            plan: 実行計画
            results: ステップ実行結果のリスト
            
        Returns:
            str: 生成された応答
        """
        logger.info(f"Generating response from {len(results)} step results")
        
        # 結果の分析
        analysis = self._analyze_results(results)
        
        # 基本的な応答生成
        if analysis['all_success']:
            base_response = self._generate_success_response(user_input, plan, results, analysis)
        elif analysis['partial_success']:
            base_response = self._generate_partial_response(user_input, plan, results, analysis)
        else:
            base_response = self._generate_failure_response(user_input, plan, results, analysis)
        
        # LLMクライアントが利用可能な場合は、より自然な応答に変換
        if self.llm_client and self._should_use_llm_generation(analysis):
            try:
                enhanced_response = self._enhance_with_llm(user_input, plan, results, base_response)
                return enhanced_response
            except Exception as e:
                logger.warning(f"LLM enhancement failed, using base response: {e}")
                return base_response
        
        return base_response
    
    def _analyze_results(self, results: List[StepResult]) -> Dict[str, Any]:
        """実行結果を分析"""
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        
        # 主要な出力を抽出
        key_outputs = []
        for result in successful:
            if result.output:
                key_outputs.append({
                    'step_id': result.step_id,
                    'output': result.output,
                    'type': result.metadata.get('output_type', 'text')
                })
        
        # エラーをまとめる
        errors = []
        for result in failed:
            errors.append({
                'step_id': result.step_id,
                'error': result.error
            })
        
        return {
            'all_success': len(failed) == 0,
            'partial_success': len(successful) > 0 and len(failed) > 0,
            'total_steps': len(results),
            'successful_steps': len(successful),
            'failed_steps': len(failed),
            'key_outputs': key_outputs,
            'errors': errors,
            'total_execution_time': sum(r.execution_time for r in results)
        }
    
    def _generate_success_response(self, user_input: str, plan: ExecutionPlan, 
                                   results: List[StepResult], analysis: Dict[str, Any]) -> str:
        """成功時の応答を生成"""
        # 主要な結果を抽出
        main_results = []
        
        for output in analysis['key_outputs']:
            # 最終ステップや重要な中間結果を優先
            step = next((s for s in plan.steps if s.id == output['step_id']), None)
            if step:
                main_results.append(f"{step.description}: {output['output']}")
        
        # モードに応じた応答生成
        if self.mode == ReasoningMode.SILENT:
            # 最終結果のみ
            if main_results:
                return main_results[-1].split(': ', 1)[1] if ': ' in main_results[-1] else main_results[-1]
            return "タスクが完了しました。"
        
        elif self.mode == ReasoningMode.PROGRESS:
            # 進捗付き
            response_parts = []
            response_parts.append("✨ 完了しました。")
            
            if len(main_results) > 1:
                response_parts.append("\n実行結果:")
                for result in main_results[-2:]:  # 最後の2つの重要な結果
                    response_parts.append(f"• {result}")
            elif main_results:
                response_parts.append(main_results[0])
            
            return '\n'.join(response_parts)
        
        else:  # DETAILED or DEBUG
            # 詳細情報付き
            response_parts = []
            response_parts.append("✨ すべてのステップが正常に完了しました。")
            response_parts.append(f"\n実行時間: {analysis['total_execution_time']:.1f}秒")
            response_parts.append("\n実行結果:")
            
            for i, result in enumerate(results, 1):
                step = next((s for s in plan.steps if s.id == result.step_id), None)
                if step and result.success:
                    response_parts.append(f"{i}. {step.description}")
                    if result.output:
                        response_parts.append(f"   → {result.output}")
            
            return '\n'.join(response_parts)
    
    def _generate_partial_response(self, user_input: str, plan: ExecutionPlan,
                                   results: List[StepResult], analysis: Dict[str, Any]) -> str:
        """部分的成功時の応答を生成"""
        response_parts = []
        response_parts.append("⚠️ 一部のステップが失敗しましたが、可能な範囲で処理を完了しました。")
        
        # 成功した結果
        if analysis['key_outputs']:
            response_parts.append("\n成功した処理:")
            for output in analysis['key_outputs']:
                step = next((s for s in plan.steps if s.id == output['step_id']), None)
                if step:
                    response_parts.append(f"• {step.description}: {output['output']}")
        
        # エラー情報
        if self.mode != ReasoningMode.SILENT and analysis['errors']:
            response_parts.append("\n失敗した処理:")
            for error in analysis['errors'][:3]:  # 最初の3つのエラー
                step = next((s for s in plan.steps if s.id == error['step_id']), None)
                if step:
                    response_parts.append(f"• {step.description}: {error['error']}")
        
        return '\n'.join(response_parts)
    
    def _generate_failure_response(self, user_input: str, plan: ExecutionPlan,
                                   results: List[StepResult], analysis: Dict[str, Any]) -> str:
        """失敗時の応答を生成"""
        response_parts = []
        response_parts.append("❌ タスクの実行に失敗しました。")
        
        if self.mode != ReasoningMode.SILENT and analysis['errors']:
            response_parts.append("\nエラー内容:")
            for error in analysis['errors'][:3]:
                step = next((s for s in plan.steps if s.id == error['step_id']), None)
                if step:
                    response_parts.append(f"• {step.description}: {error['error']}")
        
        response_parts.append("\n再度お試しいただくか、別の方法でお試しください。")
        
        return '\n'.join(response_parts)
    
    def _should_use_llm_generation(self, analysis: Dict[str, Any]) -> bool:
        """LLM生成を使用すべきかどうかを判定"""
        # 複雑な結果や部分的失敗の場合はLLMで自然な応答を生成
        return (
            len(analysis['key_outputs']) > 3 or
            analysis['partial_success'] or
            self.config.get('always_use_llm', False)
        )
    
    def _enhance_with_llm(self, user_input: str, plan: ExecutionPlan,
                          results: List[StepResult], base_response: str) -> str:
        """LLMを使用して応答を強化"""
        if not self.llm_client:
            return base_response
        
        try:
            # 実行計画の要約
            plan_summary = []
            for step in plan.steps:
                plan_summary.append(f"- {step.description}")
            
            # 実行結果の要約
            results_summary = format_execution_results({r.step_id: r for r in results})
            
            # プロンプトの構築
            prompt = RESPONSE_GENERATION_PROMPT.format(
                user_input=user_input,
                execution_plan='\n'.join(plan_summary),
                execution_results=results_summary
            )
            
            # LLMに応答生成を依頼
            enhanced_response = self.llm_client.generate(prompt)
            
            # 応答の後処理
            enhanced_response = self._post_process_response(enhanced_response)
            
            return enhanced_response
            
        except Exception as e:
            logger.error(f"Failed to enhance response with LLM: {e}")
            return base_response
    
    def _post_process_response(self, response: str) -> str:
        """生成された応答の後処理"""
        # 改行の正規化
        response = response.strip()
        
        # 冗長な部分の削除
        redundant_phrases = [
            "以下が実行結果です。",
            "実行結果をお伝えします。",
            "処理が完了しました。以下が結果です。"
        ]
        
        for phrase in redundant_phrases:
            if response.startswith(phrase):
                response = response[len(phrase):].strip()
        
        # 絵文字の追加（設定による）
        if self.config.get('add_emoji', True):
            if not any(emoji in response for emoji in ['✨', '✅', '❌', '⚠️']):
                if '完了' in response or '成功' in response:
                    response = '✅ ' + response
                elif '失敗' in response or 'エラー' in response:
                    response = '❌ ' + response
        
        return response
    
    def generate_progress_message(self, stage: str, context: Dict[str, Any]) -> str:
        """進捗メッセージを生成"""
        from .prompts import PROGRESS_TEMPLATES, format_plan_steps
        
        template = PROGRESS_TEMPLATES.get(stage, "")
        
        if stage == 'plan_display':
            # 実行計画の表示
            return template.format(plan_steps=format_plan_steps(context['steps']))
        elif stage == 'executing':
            # 実行中
            return template.format(
                current=context['current'],
                total=context['total']
            )
        elif stage == 'step_complete':
            # ステップ完了
            step = context['step']
            return template.format(step_description=step.description)
        elif stage == 'step_failed':
            # ステップ失敗
            step = context['step']
            result = context['result']
            return template.format(
                step_description=step.description,
                error=result.error
            )
        elif stage == 'complete':
            # 全体完了
            return template.format(summary=context.get('summary', 'タスクが完了しました'))
        elif stage == 'partial_complete':
            # 部分完了
            return template.format(summary=context.get('summary', '一部のタスクが完了しました'))
        
        return template