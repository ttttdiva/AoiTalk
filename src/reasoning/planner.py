"""タスクを実行可能なステップに分解するモジュール"""

import json
import logging
import re
from typing import List, Dict, Any, Optional, Set, Tuple
from collections import defaultdict
from .models import TaskStep, ExecutionPlan
from .prompts import TASK_DECOMPOSITION_PROMPT

logger = logging.getLogger(__name__)


class ReasoningPlanner:
    """タスクを実行可能なステップに分解するクラス"""
    
    def __init__(self, llm_client=None, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            llm_client: LLMクライアント
            config: プランナー設定
        """
        self.llm_client = llm_client
        self.config = config or {}
        self.max_steps = self.config.get('max_steps', 5)
        
        # ステップパターンの定義
        self.step_patterns = {
            'search': {
                'keywords': ['検索', '調べ', '探す', '確認', '取得'],
                'tools': ['search_memory', 'search_rag', 'WebSearchTool']
            },
            'create': {
                'keywords': ['作成', '登録', '追加', '生成', '保存'],
                'tools': ['use_mcp_tool', 'spotify', 'execute_file_operation']
            },
            'analyze': {
                'keywords': ['分析', '解析', '評価', '判定', '比較'],
                'tools': ['calculate', 'analyze_data']
            },
            'fetch': {
                'keywords': ['取得', '読み込み', 'ダウンロード', 'アクセス'],
                'tools': ['execute_file_operation', 'web_fetch']
            },
            'transform': {
                'keywords': ['変換', '整形', 'フォーマット', '加工', '抽出'],
                'tools': ['data_transform']
            }
        }
    
    def create_plan(self, user_input: str, context: Dict[str, Any]) -> ExecutionPlan:
        """
        実行計画を生成
        
        Args:
            user_input: ユーザー入力
            context: 実行コンテキスト（利用可能なツールなど）
            
        Returns:
            ExecutionPlan: 実行計画
        """
        logger.info(f"Creating execution plan for: {user_input[:50]}...")
        
        # ヒューリスティックベースの分解
        heuristic_steps = self._heuristic_decomposition(user_input, context)
        
        # LLMクライアントが利用可能な場合は、より詳細な分解を実行
        if self.llm_client and len(heuristic_steps) > 1:
            try:
                llm_steps = self._llm_decomposition(user_input, context)
                steps = self._merge_steps(heuristic_steps, llm_steps)
            except Exception as e:
                logger.warning(f"LLM decomposition failed, using heuristic only: {e}")
                steps = heuristic_steps
        else:
            steps = heuristic_steps
        
        # ステップ数の制限
        if len(steps) > self.max_steps:
            logger.warning(f"Plan has {len(steps)} steps, limiting to {self.max_steps}")
            steps = self._prioritize_steps(steps)[:self.max_steps]
        
        # 依存関係の特定
        dependencies = self._identify_dependencies(steps)
        
        # 実行順序の最適化
        optimized_steps = self._optimize_execution_order(steps, dependencies)
        
        # 実行時間の推定
        estimated_duration = self._estimate_duration(optimized_steps)
        
        return ExecutionPlan(
            steps=optimized_steps,
            dependencies_graph=dependencies,
            estimated_duration=estimated_duration,
            user_feedback_enabled=self.config.get('show_planning', True)
        )
    
    def _heuristic_decomposition(self, user_input: str, context: Dict[str, Any]) -> List[TaskStep]:
        """ヒューリスティックベースのタスク分解"""
        steps = []
        step_id = 1
        
        # 文を句読点や接続詞で分割
        segments = self._split_input(user_input)
        
        for segment in segments:
            # 各セグメントからステップを抽出
            step_type = self._identify_step_type(segment)
            if step_type:
                tools = self._identify_required_tools(segment, step_type, context.get('available_tools', []))
                
                step = TaskStep(
                    id=f"step_{step_id}",
                    description=self._generate_step_description(segment, step_type),
                    tool_requirements=tools,
                    dependencies=[],  # 後で更新
                    expected_output_type=self._infer_output_type(step_type)
                )
                steps.append(step)
                step_id += 1
        
        # 基本的な依存関係を設定
        self._set_basic_dependencies(steps)
        
        return steps
    
    def _split_input(self, user_input: str) -> List[str]:
        """入力を意味のあるセグメントに分割"""
        # 接続詞や句読点で分割
        split_patterns = [
            r'、そして',
            r'、それから',
            r'、次に',
            r'、さらに',
            r'。',
            r'して、',
            r'してから',
        ]
        
        segments = [user_input]
        for pattern in split_patterns:
            new_segments = []
            for segment in segments:
                parts = re.split(pattern, segment)
                new_segments.extend([p.strip() for p in parts if p.strip()])
            segments = new_segments
        
        return segments
    
    def _identify_step_type(self, segment: str) -> Optional[str]:
        """セグメントからステップタイプを特定"""
        for step_type, info in self.step_patterns.items():
            for keyword in info['keywords']:
                if keyword in segment:
                    return step_type
        return None
    
    def _identify_required_tools(self, segment: str, step_type: str, available_tools: List[str]) -> List[str]:
        """必要なツールを特定（LLMベース）"""
        # LLMクライアントが利用可能な場合はAIによる動的選択
        if self.llm_client:
            try:
                return self._llm_select_tools(segment, step_type, available_tools)
            except Exception as e:
                logger.warning(f"LLM tool selection failed, falling back to heuristic: {e}")
        
        # フォールバック: シンプルなヒューリスティック
        return self._heuristic_tool_selection(segment, step_type, available_tools)
    
    def _llm_select_tools(self, segment: str, step_type: str, available_tools: List[str]) -> List[str]:
        """LLMを使用してツールを選択"""
        from ..prompts import TOOL_SELECTION_PROMPT
        
        # ツールの説明を含むリストを作成（実際の実装では各ツールの説明を取得）
        tools_with_desc = self._get_tools_with_descriptions(available_tools)
        
        prompt = TOOL_SELECTION_PROMPT.format(
            task_description=segment,
            task_type=step_type or "general",
            available_tools_with_descriptions=tools_with_desc
        )
        
        try:
            response = self.llm_client.generate(prompt)
            result = json.loads(response)
            selected_tools = result.get('selected_tools', [])
            
            # 利用可能なツールのみを返す
            return [tool for tool in selected_tools if tool in available_tools]
        except Exception as e:
            logger.error(f"Failed to parse LLM tool selection: {e}")
            raise
    
    def _get_tools_with_descriptions(self, available_tools: List[str]) -> str:
        """ツールの説明を含む文字列を生成"""
        # 実際の実装では各ツールの説明を動的に取得
        tool_descriptions = {
            'search_memory': '過去の会話履歴や記憶を検索',
            'search_rag': 'ドキュメントや資料をベクトル検索',
            'WebSearch': 'インターネットから最新情報を検索',
            'spotify_assistant': 'Spotify音楽の再生、検索、プレイリスト管理',
            'use_mcp_tool': 'ClickUpタスク管理（MCP経由: create_task, search_tasks等）',
            'execute_file_operation': 'ローカルファイルの編集・参照（OS操作ツール）',
            'get_weather': '天気予報と気象情報の取得',
        }
        
        descriptions = []
        for tool in available_tools:
            desc = tool_descriptions.get(tool, f'{tool}: 詳細な説明は利用不可')
            descriptions.append(f"- {tool}: {desc}")
        
        return "\n".join(descriptions)
    
    def _heuristic_tool_selection(self, segment: str, step_type: str, available_tools: List[str]) -> List[str]:
        """シンプルなヒューリスティックによるツール選択（フォールバック）"""
        required_tools = []
        
        # ステップタイプに基づく推奨ツール
        recommended = self.step_patterns.get(step_type, {}).get('tools', [])
        
        # 推奨ツールから利用可能なものを選択
        for tool in available_tools:
            for rec_tool in recommended:
                if rec_tool.lower() in tool.lower() or tool.lower() in rec_tool.lower():
                    if tool not in required_tools:
                        required_tools.append(tool)
                    break
        
        return required_tools
    
    def _generate_step_description(self, segment: str, step_type: str) -> str:
        """ステップの説明を生成"""
        # セグメントをクリーンアップ
        description = segment.strip()
        
        # ステップタイプに応じた説明の調整
        type_prefixes = {
            'search': '情報を検索: ',
            'create': 'データを作成/登録: ',
            'analyze': '分析を実行: ',
            'fetch': 'データを取得: ',
            'transform': 'データを変換: '
        }
        
        prefix = type_prefixes.get(step_type, '')
        if prefix and not any(description.startswith(p) for p in type_prefixes.values()):
            description = prefix + description
        
        return description
    
    def _infer_output_type(self, step_type: str) -> str:
        """ステップタイプから出力タイプを推測"""
        output_types = {
            'search': 'search_results',
            'create': 'created_item',
            'analyze': 'analysis_result',
            'fetch': 'fetched_data',
            'transform': 'transformed_data'
        }
        return output_types.get(step_type, 'text')
    
    def _set_basic_dependencies(self, steps: List[TaskStep]):
        """基本的な依存関係を設定"""
        # シンプルな順次依存（後のステップは前のステップに依存）
        for i in range(1, len(steps)):
            current_step = steps[i]
            
            # 「それを」「その結果」などの参照がある場合は依存関係を設定
            reference_keywords = ['それを', 'その結果', 'この情報', '取得した', '検索した']
            
            if any(keyword in current_step.description for keyword in reference_keywords):
                # 直前のステップに依存
                current_step.dependencies.append(steps[i-1].id)
            
            # データ変換や処理は通常、データ取得に依存
            if 'transform' in current_step.description or '変換' in current_step.description:
                for j in range(i):
                    if 'fetch' in steps[j].description or '取得' in steps[j].description:
                        if steps[j].id not in current_step.dependencies:
                            current_step.dependencies.append(steps[j].id)
    
    def _llm_decomposition(self, user_input: str, context: Dict[str, Any]) -> List[TaskStep]:
        """LLMを使用した詳細なタスク分解"""
        if not self.llm_client:
            return []
        
        try:
            # プロンプトの構築
            prompt = TASK_DECOMPOSITION_PROMPT.format(
                user_input=user_input,
                available_tools=', '.join(context.get('available_tools', [])),
                context=json.dumps(context, ensure_ascii=False, indent=2)
            )
            
            # LLMに分解を依頼
            response = self.llm_client.generate(prompt)
            
            # JSON形式の応答をパース
            result = json.loads(response)
            
            # TaskStepオブジェクトに変換
            steps = []
            for step_data in result.get('steps', []):
                step = TaskStep(
                    id=step_data['id'],
                    description=step_data['description'],
                    tool_requirements=step_data.get('tool_requirements', []),
                    dependencies=step_data.get('dependencies', []),
                    expected_output_type=step_data.get('expected_output', 'text')
                )
                steps.append(step)
            
            return steps
        except Exception as e:
            logger.error(f"Failed to parse LLM decomposition: {e}")
            return []
    
    def _merge_steps(self, heuristic: List[TaskStep], llm: List[TaskStep]) -> List[TaskStep]:
        """ヒューリスティックとLLMのステップをマージ"""
        if not llm:
            return heuristic
        
        # LLMの結果を優先するが、ヒューリスティックの情報で補完
        merged = []
        
        for llm_step in llm:
            # 対応するヒューリスティックステップを探す
            matching_heuristic = None
            for h_step in heuristic:
                if self._steps_are_similar(llm_step, h_step):
                    matching_heuristic = h_step
                    break
            
            if matching_heuristic:
                # ツール要件をマージ
                all_tools = set(llm_step.tool_requirements + matching_heuristic.tool_requirements)
                llm_step.tool_requirements = list(all_tools)
            
            merged.append(llm_step)
        
        return merged
    
    def _steps_are_similar(self, step1: TaskStep, step2: TaskStep) -> bool:
        """2つのステップが類似しているかを判定"""
        # 説明文の類似度をチェック（簡易版）
        desc1_words = set(step1.description.split())
        desc2_words = set(step2.description.split())
        
        # 共通単語の割合
        common = desc1_words.intersection(desc2_words)
        similarity = len(common) / max(len(desc1_words), len(desc2_words))
        
        return similarity > 0.5
    
    def _prioritize_steps(self, steps: List[TaskStep]) -> List[TaskStep]:
        """ステップの優先順位付け"""
        # 必須ステップを優先
        essential = [s for s in steps if not s.optional]
        optional = [s for s in steps if s.optional]
        
        return essential + optional
    
    def _identify_dependencies(self, steps: List[TaskStep]) -> Dict[str, List[str]]:
        """ステップ間の依存関係を特定"""
        dependencies = defaultdict(list)
        
        for step in steps:
            dependencies[step.id] = step.dependencies.copy()
        
        # 追加の依存関係分析
        for i, step in enumerate(steps):
            for j, other in enumerate(steps):
                if i >= j:
                    continue
                
                # 出力と入力の関係をチェック
                if self._output_feeds_input(step, other):
                    if step.id not in dependencies[other.id]:
                        dependencies[other.id].append(step.id)
        
        return dict(dependencies)
    
    def _output_feeds_input(self, producer: TaskStep, consumer: TaskStep) -> bool:
        """あるステップの出力が別のステップの入力になるかを判定"""
        # 出力タイプと必要なツールの対応
        output_to_tool = {
            'search_results': ['create', 'analyze', 'transform'],
            'fetched_data': ['analyze', 'transform', 'create'],
            'analysis_result': ['create', 'transform']
        }
        
        producer_type = producer.expected_output_type
        consumer_tools = consumer.tool_requirements
        
        # 出力タイプが消費者のツールタイプと一致するか
        if producer_type in output_to_tool:
            for tool_type in output_to_tool[producer_type]:
                if any(tool_type in tool for tool in consumer_tools):
                    return True
        
        return False
    
    def _optimize_execution_order(self, steps: List[TaskStep], dependencies: Dict[str, List[str]]) -> List[TaskStep]:
        """最適な実行順序を決定（トポロジカルソート）"""
        # 依存関係グラフの作成
        graph = defaultdict(set)
        in_degree = defaultdict(int)
        
        for step in steps:
            in_degree[step.id] = 0
        
        for step_id, deps in dependencies.items():
            for dep in deps:
                graph[dep].add(step_id)
                in_degree[step_id] += 1
        
        # トポロジカルソート
        queue = [step for step in steps if in_degree[step.id] == 0]
        sorted_steps = []
        
        while queue:
            # 同じレベルのステップは元の順序を保持
            queue.sort(key=lambda s: steps.index(s))
            current = queue.pop(0)
            sorted_steps.append(current)
            
            for neighbor in graph[current.id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    neighbor_step = next(s for s in steps if s.id == neighbor)
                    queue.append(neighbor_step)
        
        # 循環依存がある場合は元の順序を返す
        if len(sorted_steps) != len(steps):
            logger.warning("Circular dependency detected, using original order")
            return steps
        
        return sorted_steps
    
    def _estimate_duration(self, steps: List[TaskStep]) -> float:
        """実行時間を推定（秒）"""
        # ステップタイプごとの推定時間
        step_durations = {
            'search': 2.0,
            'create': 3.0,
            'analyze': 4.0,
            'fetch': 2.5,
            'transform': 3.5
        }
        
        total_duration = 0.0
        
        for step in steps:
            # ステップタイプを推測
            step_type = None
            for stype, info in self.step_patterns.items():
                if any(keyword in step.description for keyword in info['keywords']):
                    step_type = stype
                    break
            
            duration = step_durations.get(step_type, 3.0)  # デフォルト3秒
            
            # ツール数に応じて調整
            if len(step.tool_requirements) > 1:
                duration *= 1.5
            
            total_duration += duration
        
        # 並列実行可能な場合は短縮（簡易計算）
        if self.config.get('parallel_execution', False):
            total_duration *= 0.7
        
        return total_duration