"""Fixed, fail-closed classification for CrowdWorks application requirements."""

from __future__ import annotations

import re
from typing import Any

from ..operations_text import model_text


EVALUATION_REQUIREMENT_CATEGORY_CODES = frozenset({
    "work_scope",
    "reference_information",
    "deliverable_format",
    "completion_criteria",
    "feasibility",
    "other",
})

_REQUIREMENT_SCOPE_MARKERS = (
    "調査対象", "実装対象", "対応範囲", "作業対象", "対象となる", "対象商品", "対象疾患", "対象部位", "作業範囲", "業務範囲",
    "担当範囲", "責任範囲", "担当するか", "作業内容", "仕事内容", "作業の範囲", "実施範囲",
    "納品物の内容", "業務内容", "依頼内容", "目的", "視聴者", "言語ペア",
    "動画本数", "投稿本数", "記事本数", "枚数", "通数", "文字数", "数量", "月間の", "完成尺",
    "素材尺", "1本当たり", "1話当たり", "1項目当たり",
)
_REQUIREMENT_REFERENCE_MARKERS = (
    "情報源", "参照資料", "参考資料", "参考動画", "一次資料", "入力データ", "支給範囲", "支給有無",
    "必要な素材", "必要な資料", "アクセス権", "アカウント", "原文", "編集元ファイル", "台本",
    "ナレーション", "画像", "映像", "音源", "素材",
)
_REQUIREMENT_INFEASIBLE_MARKERS = (
    "資格必須", "必須資格", "必須の資格", "免許必須", "有資格者", "常駐", "対面", "訪問",
    "出社", "違法", "規約違反", "実施できない", "対応できない", "実施不能", "対応不可", "不可能な要件",
    "極端に低い報酬", "低すぎる報酬", "報酬が低すぎ", "納期が短すぎ", "報酬・納期の不整合", "採算が合わない",
)
_REQUIREMENT_CLARIFIABLE_MARKERS = (
    "納品形式", "ファイル形式", "出力形式", "提出形式", "提出先", "納品方法", "ファイル名", "命名規則",
    "サイズ", "解像度", "縦横比", "カラープロファイル", "フォント", "レイヤー構成", "プラグイン",
    "完了条件", "検収", "品質基準", "判定基準", "合格基準", "KPI", "評価期間", "評価項目",
    "修正回数", "修正範囲", "追加修正", "初稿期限", "フィードバック期限", "納期", "スケジュール",
    "投稿頻度", "会議頻度", "会議回数", "連絡手段", "対応時間帯", "休日対応", "運用ルール",
    "報酬", "単価", "インセンティブ", "支払条件", "支給条件", "契約期間", "最低契約期間",
    "更新条件", "解約条件", "著作権", "利用許諾", "ライセンス", "生成AI", "AI利用", "利用可否",
    "確認事項", "追加確認", "書式", "文体", "配置",
)


_ITEM_MAX_CHARS = 1000
_EVIDENCE_MAX_CHARS = 200
_INVALID_REQUIREMENT = "不足要件の重大度・根拠を確認できません"
_EVIDENCE_LOOSE_REMOVE_RE = re.compile(r'[\s「」『』【】"“”‘’：:]+')
_EVIDENCE_FRAGMENT_EDGE_PUNCTUATION = "。．.、，,・"


def _item_text(value: Any) -> str:
    return (model_text(value) or _INVALID_REQUIREMENT)[:_ITEM_MAX_CHARS]


def _loose(text: str) -> str:
    return _EVIDENCE_LOOSE_REMOVE_RE.sub("", text)


def _structured_valid(value: dict[str, Any]) -> bool:
    item = model_text(value.get("item"))
    evidence = model_text(value.get("evidence"))
    return bool(
        item and len(item) <= _ITEM_MAX_CHARS
        and isinstance(value.get("severity"), str)
        and value.get("severity") in {"blocking", "clarifiable"}
        and isinstance(value.get("category"), str)
        and value.get("category") in EVALUATION_REQUIREMENT_CATEGORY_CODES
        and evidence and len(evidence) <= _EVIDENCE_MAX_CHARS
    )


def _evidence_matches(value: dict[str, Any], brief_text: Any) -> bool:
    quote = " ".join((model_text(value.get("evidence")) or "").split())
    brief = " ".join((model_text(brief_text) or "").split())
    if quote == "記載なし":
        return True
    if quote and brief and quote in brief:
        return True
    if not quote or not brief:
        return False

    loose_brief = _loose(brief)
    fragments = [
        _loose(fragment).strip(_EVIDENCE_FRAGMENT_EDGE_PUNCTUATION)
        for fragment in re.split(r"／|\s+/\s+", quote)
    ]
    fragments = [fragment for fragment in fragments if fragment]
    return bool(fragments) and all(
        len(fragment) >= 4 and fragment in loose_brief
        for fragment in fragments
    )


def normalize_evaluation_requirement_findings(values: Any, brief_text: Any) -> list[dict[str, str]]:
    """Validate new model findings; malformed output never enters legacy fallback.

    Evidence is an exact quote modulo whitespace, or the explicit absence marker.
    Invalid findings retain bounded item text but an invalid severity so every
    downstream gate rejects them, including after persistence/readback.
    """
    if not isinstance(values, list):
        values = [None]
    normalized = []
    for value in values:
        valid = isinstance(value, dict) and _structured_valid(value)
        finding = value if isinstance(value, dict) else {}
        evidence = model_text(finding.get("evidence")) or "記載なし"
        valid = valid and _evidence_matches(finding, brief_text)
        normalized.append({
            "item": _item_text(finding.get("item") if isinstance(value, dict) else value),
            "severity": finding["severity"] if valid else "invalid",
            "category": finding.get("category") if isinstance(finding.get("category"), str) and finding.get("category") in EVALUATION_REQUIREMENT_CATEGORY_CODES else "other",
            "evidence": evidence[:_EVIDENCE_MAX_CHARS],
        })
    return normalized


def missing_requirements(values: Any) -> list[str]:
    """Return bounded item text only, never serialized evidence or metadata."""
    if not isinstance(values, (list, tuple)):
        return [_INVALID_REQUIREMENT]
    return [_item_text(value.get("item") if isinstance(value, dict) else value) for value in values]


def missing_requirement_categories(values: Any) -> set[str]:
    """Return a bounded, content-free category set for scan summaries."""
    categories: set[str] = set()
    texts = values if isinstance(values, (list, tuple)) else [values]
    for value in texts:
        if isinstance(value, dict):
            category = value.get("category")
            categories.add(category if isinstance(category, str) and category in EVALUATION_REQUIREMENT_CATEGORY_CODES else "other")
            continue
        text = model_text(value)
        if not text:
            categories.add("other")
            continue
        matched: set[str] = set()
        if any(marker in text for marker in _REQUIREMENT_SCOPE_MARKERS):
            matched.add("work_scope")
        if any(marker in text for marker in _REQUIREMENT_REFERENCE_MARKERS):
            matched.add("reference_information")
        if any(marker in text for marker in (
            "納品", "成果物", "提出", "形式", "ファイル", "サイズ", "解像度", "画像寸法",
            "レイヤー", "カラープロファイル", "命名規則",
        )):
            matched.add("deliverable_format")
        if any(marker in text for marker in (
            "完了", "検収", "品質", "判定", "合格", "KPI", "正答率", "評価基準",
        )):
            matched.add("completion_criteria")
        if any(marker in text for marker in _REQUIREMENT_INFEASIBLE_MARKERS):
            matched.add("feasibility")
        categories.update(matched or {"other"})
    return categories or {"other"}


def classify_missing_requirements(values: Any, *, brief_text: Any = None) -> dict[str, Any]:
    """Honor evidence-bound structured severity and conservative legacy decisions.

    Persisted quotes must still match the current Brief. Without Brief context,
    only the explicit absence marker can be verified; any quote fails closed.
    """
    result: dict[str, Any] = {
        "blocking": [], "clarifiable": [], "invalid_count": 0,
        "severity_counts": {"blocking": 0, "clarifiable": 0},
        "severity_category_counts": {
            severity: {category: 0 for category in sorted(EVALUATION_REQUIREMENT_CATEGORY_CODES)}
            for severity in ("blocking", "clarifiable")
        },
    }
    entries = values if isinstance(values, (list, tuple)) else [None]
    for value in entries:
        text = _item_text(value.get("item") if isinstance(value, dict) else value)
        categories = missing_requirement_categories([value])
        if isinstance(value, dict):
            valid = _structured_valid(value) and _evidence_matches(value, brief_text)
            severity = value["severity"] if valid else "blocking"
            if not valid:
                result["invalid_count"] += 1
        elif model_text(value):
            # Legacy persisted string lists keep their previous, stricter rule.
            if (categories & {"work_scope", "reference_information", "feasibility"}
                    or any(marker in text for marker in _REQUIREMENT_SCOPE_MARKERS)
                    or any(marker in text for marker in _REQUIREMENT_REFERENCE_MARKERS)
                    or any(marker in text for marker in _REQUIREMENT_INFEASIBLE_MARKERS)):
                severity = "blocking"
            elif (any(marker in text for marker in _REQUIREMENT_CLARIFIABLE_MARKERS)
                  or categories & {"deliverable_format", "completion_criteria"}):
                severity = "clarifiable"
            else:
                severity = "blocking"
        else:
            severity = "blocking"
            result["invalid_count"] += 1
        result[severity].append(text)
        result["severity_counts"][severity] += 1
        for category in categories:
            result["severity_category_counts"][severity][category] += 1
    return result


__all__ = [
    "EVALUATION_REQUIREMENT_CATEGORY_CODES",
    "classify_missing_requirements",
    "missing_requirement_categories",
    "missing_requirements",
    "normalize_evaluation_requirement_findings",
]
