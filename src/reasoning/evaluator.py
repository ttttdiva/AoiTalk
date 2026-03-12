"""タスクの複雑度を評価するモジュール"""

import re
import json
import logging
from typing import List, Dict, Any, Optional
from .models import ComplexityScore
from .prompts import COMPLEXITY_EVALUATION_PROMPT

logger = logging.getLogger(__name__)


class ComplexityEvaluator:
    """タスクの複雑度を評価するクラス"""
    
    def __init__(self, llm_client=None, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            llm_client: LLMクライアント（複雑な評価に使用）
            config: 評価設定
        """
        self.llm_client = llm_client
        self.config = config or {}
        
        # デフォルトの重み設定
        self.weights = self.config.get('evaluation_weights', {
            'multi_tool': 0.3,
            'dependencies': 0.3,
            'conditional': 0.2,
            'data_transformation': 0.2
        })
        
        # 複雑なタスクを示すキーワード
        self.complex_keywords = {
            'multi_step': ['そして', 'それから', 'その後', '次に', 'さらに'],
            'dependency': ['結果を', '基に', '使って', 'それを', 'その情報で'],
            'conditional': ['もし', '場合', 'なら', 'ときは', 'によって'],
            'data_transform': ['変換', '整形', 'フォーマット', '抽出', '計算']
        }
        
        # ツール組み合わせパターン
        self.tool_combinations = {
            'search_and_action': ['検索', '調べ', '探し'],
            'analyze_and_create': ['分析', '確認', '作成', '登録'],
            'fetch_and_process': ['取得', '処理', '加工']
        }
    
    def evaluate(self, user_input: str, available_tools: List[str]) -> ComplexityScore:
        """
        タスクの複雑度を評価
        
        Args:
            user_input: ユーザー入力
            available_tools: 利用可能なツールのリスト
            
        Returns:
            ComplexityScore: 複雑度スコアと詳細
        """
        logger.info(f"Evaluating complexity for: {user_input[:50]}...")
        
        # ヒューリスティックベースの評価
        heuristic_scores = self._heuristic_evaluation(user_input, available_tools)
        
        # LLMクライアントが利用可能な場合は、より詳細な評価を実行
        if self.llm_client and self._should_use_llm_evaluation(heuristic_scores):
            try:
                llm_scores = self._llm_evaluation(user_input, available_tools)
                # ヒューリスティックとLLMの結果を組み合わせる
                factors = self._merge_scores(heuristic_scores, llm_scores)
            except Exception as e:
                logger.warning(f"LLM evaluation failed, using heuristic only: {e}")
                factors = heuristic_scores
        else:
            factors = heuristic_scores
        
        # 重み付き平均でスコアを計算
        total_score = sum(
            factors.get(key, 0) * self.weights.get(key, 0.25)
            for key in self.weights
        )
        
        # 理由の生成
        reasoning = self._generate_reasoning(factors, total_score)
        
        return ComplexityScore(
            score=min(total_score, 1.0),
            factors=factors,
            reasoning=reasoning
        )
    
    def _heuristic_evaluation(self, user_input: str, available_tools: List[str]) -> Dict[str, float]:
        """ヒューリスティックベースの評価"""
        scores = {}
        
        # 複数ツールの必要性
        scores['multi_tool'] = self._detect_multi_tool_requirement(user_input, available_tools)
        
        # タスク間の依存関係
        scores['dependencies'] = self._detect_dependencies(user_input)
        
        # 条件分岐の必要性
        scores['conditional'] = self._detect_conditional_logic(user_input)
        
        # データ変換の必要性
        scores['data_transformation'] = self._detect_data_transformation(user_input)
        
        return scores
    
    def _detect_multi_tool_requirement(self, user_input: str, available_tools: List[str]) -> float:
        """複数ツールが必要かを判定"""
        score = 0.0
        
        # ツール関連のキーワードを数える
        tool_indicators = 0
        for pattern_type, keywords in self.tool_combinations.items():
            for keyword in keywords:
                if keyword in user_input:
                    tool_indicators += 1
        
        # 複数の動作を示す接続詞
        connectors = ['そして', 'それから', 'さらに', 'また', '加えて']
        connector_count = sum(1 for conn in connectors if conn in user_input)
        
        # スコア計算
        if tool_indicators >= 2:
            score += 0.6
        elif tool_indicators == 1:
            score += 0.3
        
        if connector_count >= 2:
            score += 0.4
        elif connector_count == 1:
            score += 0.2
        
        return min(score, 1.0)
    
    def _detect_dependencies(self, user_input: str) -> float:
        """タスク間の依存関係を検出"""
        score = 0.0
        
        # 依存関係を示すキーワード
        dependency_keywords = self.complex_keywords['dependency']
        found_dependencies = sum(1 for keyword in dependency_keywords if keyword in user_input)
        
        # 順序を示す表現
        sequence_patterns = [
            r'まず.*次に',
            r'最初に.*それから',
            r'(.+)を(.+)してから',
            r'(.+)の結果を(.+)',
            r'(.+)した後に(.+)'
        ]
        
        sequence_count = sum(1 for pattern in sequence_patterns if re.search(pattern, user_input))
        
        # スコア計算
        if found_dependencies >= 2:
            score += 0.6
        elif found_dependencies == 1:
            score += 0.3
        
        if sequence_count >= 1:
            score += 0.4
        
        return min(score, 1.0)
    
    def _detect_conditional_logic(self, user_input: str) -> float:
        """条件分岐の必要性を検出"""
        score = 0.0
        
        # 条件分岐を示すキーワード
        conditional_keywords = self.complex_keywords['conditional']
        found_conditionals = sum(1 for keyword in conditional_keywords if keyword in user_input)
        
        # 条件分岐パターン
        conditional_patterns = [
            r'もし.*なら',
            r'(.+)の場合は',
            r'(.+)によって(.+)を変える',
            r'(.+)かどうか'
        ]
        
        pattern_count = sum(1 for pattern in conditional_patterns if re.search(pattern, user_input))
        
        # スコア計算
        if found_conditionals >= 2 or pattern_count >= 2:
            score = 0.8
        elif found_conditionals >= 1 or pattern_count >= 1:
            score = 0.4
        
        return score
    
    def _detect_data_transformation(self, user_input: str) -> float:
        """データ変換・加工の必要性を検出"""
        score = 0.0
        
        # データ変換を示すキーワード
        transform_keywords = self.complex_keywords['data_transform']
        found_transforms = sum(1 for keyword in transform_keywords if keyword in user_input)
        
        # データ処理パターン
        transform_patterns = [
            r'(.+)を(.+)形式に',
            r'(.+)から(.+)を抽出',
            r'(.+)を基に(.+)を生成',
            r'(.+)を(.+)として保存'
        ]
        
        pattern_count = sum(1 for pattern in transform_patterns if re.search(pattern, user_input))
        
        # スコア計算
        if found_transforms >= 2 or pattern_count >= 2:
            score = 0.7
        elif found_transforms >= 1 or pattern_count >= 1:
            score = 0.4
        
        return score
    
    def _should_use_llm_evaluation(self, heuristic_scores: Dict[str, float]) -> bool:
        """LLM評価を使用すべきかどうかを判定"""
        # ヒューリスティックスコアの平均が中程度の場合、LLM評価を使用
        avg_score = sum(heuristic_scores.values()) / len(heuristic_scores)
        return 0.3 <= avg_score <= 0.7
    
    def _llm_evaluation(self, user_input: str, available_tools: List[str]) -> Dict[str, float]:
        """LLMを使用した詳細な評価"""
        if not self.llm_client:
            return {}
        
        try:
            # プロンプトの構築
            prompt = COMPLEXITY_EVALUATION_PROMPT.format(
                user_input=user_input,
                available_tools=', '.join(available_tools)
            )
            
            # LLMに評価を依頼
            response = self.llm_client.generate(prompt)
            
            # JSON形式の応答をパース
            result = json.loads(response)
            
            return {
                'multi_tool': result.get('multi_tool_score', 0),
                'dependencies': result.get('dependency_score', 0),
                'conditional': result.get('conditional_score', 0),
                'data_transformation': result.get('transformation_score', 0)
            }
        except Exception as e:
            logger.error(f"Failed to parse LLM evaluation: {e}")
            return {}
    
    def _merge_scores(self, heuristic: Dict[str, float], llm: Dict[str, float]) -> Dict[str, float]:
        """ヒューリスティックとLLMのスコアをマージ"""
        merged = {}
        for key in self.weights:
            h_score = heuristic.get(key, 0)
            l_score = llm.get(key, 0)
            # ヒューリスティック60%、LLM40%の重み付け
            merged[key] = h_score * 0.6 + l_score * 0.4
        return merged
    
    def _generate_reasoning(self, factors: Dict[str, float], total_score: float) -> str:
        """評価理由を生成"""
        reasons = []
        
        if factors.get('multi_tool', 0) > 0.5:
            reasons.append("複数のツールを組み合わせる必要がある")
        
        if factors.get('dependencies', 0) > 0.5:
            reasons.append("タスク間に依存関係が存在する")
        
        if factors.get('conditional', 0) > 0.5:
            reasons.append("条件分岐を含む処理が必要")
        
        if factors.get('data_transformation', 0) > 0.5:
            reasons.append("データの変換や加工が必要")
        
        if not reasons:
            if total_score > 0.3:
                reasons.append("中程度の複雑性を持つタスク")
            else:
                reasons.append("単純なタスク")
        
        return "、".join(reasons)