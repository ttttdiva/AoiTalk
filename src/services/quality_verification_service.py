"""
レスポンス品質検証サービス

AI応答の品質を複数の観点からヒューリスティックに検証する。
追加のLLM呼び出しは行わず、コスト効率を維持する。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────
# 品質レポート
# ────────────────────────────────────────────


@dataclass
class QualityReport:
    """品質検証結果"""

    score: float  # 0.0〜1.0（1.0が最高品質）
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    is_acceptable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "issues": self.issues,
            "suggestions": self.suggestions,
            "is_acceptable": self.is_acceptable,
        }


# ────────────────────────────────────────────
# ヘッジワード / 不確実性マーカー
# ────────────────────────────────────────────

_HEDGE_WORDS_JA = [
    "かもしれません",
    "おそらく",
    "たぶん",
    "可能性があります",
    "と思います",
    "と思われます",
    "推測ですが",
    "確かではありませんが",
    "わかりませんが",
    "不明ですが",
    "はっきりとは",
    "正確には",
    "一般的には",
    "必ずしも",
]

_HEDGE_WORDS_EN = [
    "maybe",
    "perhaps",
    "possibly",
    "might",
    "could be",
    "i think",
    "i believe",
    "i'm not sure",
    "not certain",
    "it seems",
    "it appears",
    "arguably",
    "generally",
    "typically",
    "in most cases",
    "it depends",
]

# 日本語検出パターン
_JAPANESE_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]")

# 英語検出パターン（ASCII英字が主）
_ENGLISH_RE = re.compile(r"[a-zA-Z]")

# 空・拒否応答パターン
_REFUSAL_PATTERNS_JA = [
    re.compile(r"(お答え|回答).*できません", re.IGNORECASE),
    re.compile(r"(情報|データ)が(不足|ありません)", re.IGNORECASE),
    re.compile(r"(わかりません|存じません)", re.IGNORECASE),
    re.compile(r"(申し訳|すみません).*(?:できません|わかりません)", re.IGNORECASE),
]

_REFUSAL_PATTERNS_EN = [
    re.compile(r"i\s+(can'?t|cannot|don'?t)\s+(answer|help|provide)", re.IGNORECASE),
    re.compile(r"i'?m\s+(not\s+able|unable)\s+to", re.IGNORECASE),
    re.compile(r"i\s+don'?t\s+(have|know)", re.IGNORECASE),
    re.compile(r"(sorry|apologies).*(can'?t|cannot|unable)", re.IGNORECASE),
]


class QualityVerificationService:
    """
    レスポンス品質検証サービス

    以下の観点でAI応答を検証する:
    1. 応答の関連性（質問に対して答えているか）
    2. 応答の完全性（短すぎないか）
    3. RAG整合性（コンテキストとの矛盾チェック）
    4. 信頼度推定（ヘッジワード・不確実性マーカー）
    5. 言語整合性（入力と応答の言語一致）

    追加のLLM呼び出しは行わず、ヒューリスティクスのみで検証する。
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        logger.info(
            "[QualityVerification] 品質検証サービスを初期化しました (有効=%s)",
            enabled,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        logger.info("[QualityVerification] 有効状態を変更しました: %s", value)

    # ────────────────────────────────────────
    # メイン検証
    # ────────────────────────────────────────

    async def verify_response(
        self,
        user_input: str,
        response: str,
        context: Optional[str] = None,
    ) -> QualityReport:
        """
        AI応答の品質を検証する。

        Args:
            user_input: ユーザーの入力テキスト
            response: AI応答テキスト
            context: RAGコンテキスト（オプション）

        Returns:
            QualityReport: 品質検証レポート
        """
        if not self._enabled:
            return QualityReport(score=1.0, is_acceptable=True)

        issues: List[str] = []
        suggestions: List[str] = []
        scores: List[float] = []

        # 1. 応答の関連性チェック
        relevance_score = self._check_relevance(user_input, response)
        scores.append(relevance_score)
        if relevance_score < 0.5:
            issues.append("応答が質問に対して関連性が低い可能性があります")
            suggestions.append("質問のキーワードに対応する内容を含めてください")

        # 2. 完全性チェック
        completeness_score = self._check_completeness(user_input, response)
        scores.append(completeness_score)
        if completeness_score < 0.5:
            issues.append("応答が短すぎる可能性があります")
            suggestions.append("より詳細な説明を追加することを検討してください")

        # 3. RAG整合性チェック（コンテキストがある場合）
        if context:
            rag_score = self._check_rag_consistency(response, context)
            scores.append(rag_score)
            if rag_score < 0.5:
                issues.append("応答がコンテキスト情報と矛盾している可能性があります")
                suggestions.append(
                    "提供されたコンテキストに基づいて回答を調整してください"
                )
        else:
            scores.append(0.8)  # コンテキストなしの場合はやや高めのデフォルト

        # 4. 信頼度推定
        confidence_score = self.get_confidence_score(response)
        scores.append(confidence_score)
        if confidence_score < 0.4:
            issues.append("応答に不確実性が多く含まれています")
            suggestions.append(
                "より確信を持った表現を使用するか、不明点を明示してください"
            )

        # 5. 言語整合性チェック
        lang_score = self._check_language_consistency(user_input, response)
        scores.append(lang_score)
        if lang_score < 0.5:
            issues.append("応答の言語が入力言語と異なります")
            suggestions.append("入力と同じ言語で応答してください")

        # 総合スコア（加重平均）
        weights = [0.30, 0.20, 0.15, 0.20, 0.15]
        total_score = sum(s * w for s, w in zip(scores, weights))

        # 許容判定（0.4 未満は不合格）
        is_acceptable = total_score >= 0.4

        report = QualityReport(
            score=total_score,
            issues=issues,
            suggestions=suggestions,
            is_acceptable=is_acceptable,
        )

        logger.info(
            "[QualityVerification] 検証完了: score=%.3f, issues=%d, acceptable=%s",
            total_score,
            len(issues),
            is_acceptable,
        )
        return report

    # ────────────────────────────────────────
    # 信頼度スコア（公開API）
    # ────────────────────────────────────────

    def get_confidence_score(self, response: str) -> float:
        """
        応答の信頼度スコアを算出する。

        ヘッジワードや不確実性マーカーの出現頻度から推定。

        Args:
            response: AI応答テキスト

        Returns:
            0.0〜1.0 の信頼度スコア（1.0 = 高信頼）
        """
        if not response or not response.strip():
            return 0.0

        text_lower = response.lower()
        hedge_count = 0

        # 日本語ヘッジワード
        for word in _HEDGE_WORDS_JA:
            hedge_count += text_lower.count(word)

        # 英語ヘッジワード
        for word in _HEDGE_WORDS_EN:
            hedge_count += text_lower.count(word)

        # 拒否応答チェック
        is_refusal = False
        for pattern in _REFUSAL_PATTERNS_JA + _REFUSAL_PATTERNS_EN:
            if pattern.search(response):
                is_refusal = True
                break

        if is_refusal:
            return 0.2

        # 文数で正規化
        sentence_count = max(1, len(re.split(r"[。.!！?？\n]", response.strip())))
        hedge_ratio = hedge_count / sentence_count

        # ヘッジ比率からスコアに変換（0 → 1.0, 0.5+ → 0.3）
        score = max(0.3, 1.0 - hedge_ratio * 1.4)
        return min(1.0, score)

    # ────────────────────────────────────────
    # 個別チェック（内部メソッド）
    # ────────────────────────────────────────

    def _check_relevance(self, user_input: str, response: str) -> float:
        """
        応答の関連性スコアを算出する。

        入力のキーワードが応答にどれだけ含まれているかで判定。
        """
        if not user_input.strip() or not response.strip():
            return 0.5

        # 入力からキーワードを抽出（ストップワード除外）
        input_keywords = self._extract_keywords(user_input)
        if not input_keywords:
            return 0.7  # キーワードなし → やや高めのデフォルト

        response_lower = response.lower()
        matched = sum(1 for kw in input_keywords if kw.lower() in response_lower)
        ratio = matched / len(input_keywords)

        # 0〜1に正規化（50%一致で0.7、100%で1.0）
        return min(1.0, 0.4 + ratio * 0.6)

    def _check_completeness(self, user_input: str, response: str) -> float:
        """
        応答の完全性スコアを算出する。

        入力の複雑度に対して応答が十分な長さかを判定。
        """
        if not response.strip():
            return 0.0

        input_len = len(user_input.strip())
        response_len = len(response.strip())

        # 非常に短い入力（挨拶等）に対する短い応答はOK
        if input_len < 20:
            return 1.0 if response_len > 0 else 0.0

        # 入力に対する応答の比率
        ratio = response_len / max(1, input_len)

        # 質問記号がある場合はより長い応答を期待
        has_question = bool(re.search(r"[?？]", user_input))
        min_ratio = 0.5 if has_question else 0.3

        if ratio < min_ratio:
            # 短すぎる場合のペナルティ
            return max(0.2, ratio / min_ratio)

        # 十分な長さ
        return min(1.0, 0.6 + ratio * 0.1)

    def _check_rag_consistency(self, response: str, context: str) -> float:
        """
        RAGコンテキストとの整合性スコアを算出する。

        応答がコンテキストの情報と矛盾していないかを判定。
        コンテキストのキーファクトが応答に反映されているかを確認。
        """
        if not context.strip():
            return 0.8  # コンテキストが空の場合はデフォルトスコア

        # コンテキストからキーワードを抽出
        context_keywords = self._extract_keywords(context)
        if not context_keywords:
            return 0.8

        response_lower = response.lower()
        # コンテキストキーワードのうち、応答に含まれるものの割合
        matched = sum(1 for kw in context_keywords if kw.lower() in response_lower)
        usage_ratio = matched / len(context_keywords)

        # 矛盾チェック: 否定表現+コンテキストキーワード
        contradiction_count = 0
        negation_patterns = [
            re.compile(
                r"(ではありません|ではない|しません|しない|ません)", re.IGNORECASE
            ),
            re.compile(r"(is\s+not|isn'?t|doesn'?t|don'?t|never|no\s)", re.IGNORECASE),
        ]
        for kw in context_keywords[:10]:  # 上位10キーワードのみ
            for neg in negation_patterns:
                # キーワード付近の否定表現を検出
                pattern = re.compile(
                    rf"{re.escape(kw.lower())}.{{0,30}}{'|'.join(p.pattern for p in negation_patterns)}",
                    re.IGNORECASE,
                )
                if pattern.search(response_lower):
                    contradiction_count += 1

        # 矛盾ペナルティ
        contradiction_penalty = min(0.3, contradiction_count * 0.1)

        # スコア算出: コンテキスト活用率 - 矛盾ペナルティ
        score = min(1.0, 0.5 + usage_ratio * 0.5) - contradiction_penalty
        return max(0.0, score)

    def _check_language_consistency(self, user_input: str, response: str) -> float:
        """
        入力と応答の言語整合性スコアを算出する。

        日本語入力に対して日本語応答、英語入力に対して英語応答を期待する。
        """
        if not user_input.strip() or not response.strip():
            return 0.8

        input_lang = self._detect_language(user_input)
        response_lang = self._detect_language(response)

        if input_lang == response_lang:
            return 1.0
        elif input_lang == "mixed" or response_lang == "mixed":
            return 0.7  # 混在は許容
        else:
            return 0.3  # 明確な言語不一致

    # ────────────────────────────────────────
    # ユーティリティ
    # ────────────────────────────────────────

    @staticmethod
    def _detect_language(text: str) -> str:
        """テキストの主要言語を検出する（ja/en/mixed）。"""
        jp_count = len(_JAPANESE_RE.findall(text))
        en_count = len(_ENGLISH_RE.findall(text))
        total = jp_count + en_count

        if total == 0:
            return "mixed"

        jp_ratio = jp_count / total
        if jp_ratio > 0.3:
            return "ja"
        elif jp_ratio < 0.05:
            return "en"
        else:
            return "mixed"

    @staticmethod
    def _extract_keywords(text: str) -> List[str]:
        """
        テキストからキーワードを抽出する（ストップワード除外）。
        """
        # 日本語ストップワード
        ja_stopwords = {
            "の",
            "に",
            "は",
            "を",
            "た",
            "が",
            "で",
            "て",
            "と",
            "し",
            "れ",
            "さ",
            "ある",
            "いる",
            "も",
            "する",
            "から",
            "な",
            "こと",
            "として",
            "い",
            "や",
            "れる",
            "など",
            "なっ",
            "ない",
            "この",
            "ため",
            "その",
            "あっ",
            "よう",
            "また",
            "もの",
            "という",
            "あり",
            "まで",
            "られ",
            "なる",
            "へ",
            "か",
            "だ",
            "これ",
            "によって",
            "により",
            "おり",
            "より",
            "による",
            "ず",
            "なり",
            "られる",
            "において",
            "に対して",
            "ほか",
            "ながら",
            "うち",
            "そして",
            "ただし",
            "ただ",
            "なお",
            "です",
            "ます",
        }

        # 英語ストップワード
        en_stopwords = {
            "the",
            "a",
            "an",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "can",
            "shall",
            "to",
            "of",
            "in",
            "for",
            "on",
            "with",
            "at",
            "by",
            "from",
            "as",
            "into",
            "about",
            "not",
            "or",
            "and",
            "but",
            "if",
            "then",
            "that",
            "this",
            "it",
            "its",
            "i",
            "you",
            "he",
            "she",
            "they",
            "we",
            "my",
            "your",
            "his",
            "her",
            "their",
            "what",
            "which",
            "who",
            "when",
            "where",
            "how",
            "why",
            "all",
            "each",
            "every",
            "both",
            "few",
            "more",
            "most",
            "some",
            "any",
            "no",
        }

        # トークン分割（日本語はN-gram的に、英語はスペース区切り）
        # 簡易的に連続する日本語文字列と英単語を抽出
        tokens: List[str] = []

        # 英単語
        for word in re.findall(r"[a-zA-Z]+", text):
            if word.lower() not in en_stopwords and len(word) > 2:
                tokens.append(word)

        # 日本語: 2〜5文字の連続漢字/カタカナを抽出（名詞相当）
        for chunk in re.findall(r"[\u4e00-\u9fff\u30a0-\u30ff]{2,5}", text):
            if chunk not in ja_stopwords:
                tokens.append(chunk)

        # 重複排除して返却
        seen = set()
        unique: List[str] = []
        for t in tokens:
            t_lower = t.lower()
            if t_lower not in seen:
                seen.add(t_lower)
                unique.append(t)

        return unique[:30]  # 最大30キーワード
