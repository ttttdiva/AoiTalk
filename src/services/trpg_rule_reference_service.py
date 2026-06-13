"""Structured TRPG rule/supplement lookup for AI and CoC mechanics.

This layer reads concise structured excerpts from the DB. It intentionally does
not treat a whole OCR file as canonical prompt context.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, or_, select

from ..memory.database import get_db_session
from ..models.ecc_models import (
    TRPGCreatureEntry,
    TRPGMechanicRuleLink,
    TRPGReferenceDocument,
    TRPGRuleItem,
)
from .trpg_rules import list_coc_mechanic_keys, normalize_ruleset_key


RULE_DOMAINS = {
    "checks",
    "skills",
    "combat",
    "sanity",
    "insanity",
    "resistance",
    "resources",
    "spells",
    "equipment",
    "character_creation",
    "keeper",
    "growth",
    "tables",
    "creatures",
    "mythos_tomes",
    "occult_tomes",
    "general",
}

_DOMAIN_HINTS = [
    ("spells", ("spell", "magic", "呪文", "魔術")),
    ("sanity", ("san", "sanity", "正気", "正気度")),
    ("insanity", ("insanity", "madness", "temporary", "indefinite", "狂気", "一時", "不定")),
    ("combat", ("combat", "attack", "damage", "weapon", "armor", "戦闘", "攻撃", "ダメージ", "武器", "装甲")),
    ("resistance", ("resistance", "opposed", "抵抗", "対抗")),
    ("skills", ("skill", "development", "experience", "技能", "成長")),
    ("resources", ("hp", "mp", "resource", "耐久力", "マジックポイント")),
    ("character_creation", ("character", "investigator", "ability", "探索者", "能力値", "キャラ")),
    ("equipment", ("equipment", "price", "weapon", "装備", "価格")),
    ("keeper", ("keeper", "gm", "キーパー")),
]

_D100_RE = re.compile(r"\b(?:1d100|d100)\b|判定|ロール|roll", re.I)
_SPELL_RULE_RE = re.compile(
    r"呪文を(?:かける|学ぶ)|spell|magic|マジック[・ ]?ポイント|magic point|\bMP\b|\bPOW\b|"
    r"正気度(?:ポイント)?(?:を)?(?:失|喪失)|コスト|詠唱には|ラウンド",
    re.I,
)
_SANITY_RULE_RE = re.compile(r"\bSAN\b|正気度", re.I)
_SANITY_PROCEDURE_RE = re.compile(r"喪失|失う|チェック|ロール|判定|減少|回復|[0-9]+(?:D[0-9]+)?/[0-9]+(?:D[0-9]+)?", re.I)
_INSANITY_RULE_RE = re.compile(r"狂気|madness|insanity|一時|不定|症状|発作|恐怖症|マニア", re.I)
_COMBAT_RULE_RE = re.compile(r"攻撃|ダメージ|武器|装甲|回避|受け流し|戦闘|attack|damage|weapon|armor|dodge", re.I)
_RESISTANCE_RULE_RE = re.compile(r"抵抗表|抵抗ロール|対抗|能動|受動|POT|CON", re.I)
_SKILL_RULE_RE = re.compile(r"技能|skill|成長|経験|成功|失敗|判定|ロール", re.I)
_RESOURCE_RULE_RE = re.compile(r"\bHP\b|\bMP\b|耐久力|マジック[・ ]?ポイント|resource", re.I)


def normalize_reference_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def infer_rule_domain_and_mechanic(text: str) -> Dict[str, Any]:
    haystack = normalize_reference_name(text)
    domain = "general"
    for candidate, hints in _DOMAIN_HINTS:
        if any(hint.lower() in haystack for hint in hints):
            domain = candidate
            break

    mechanic_key = ""
    if _D100_RE.search(haystack):
        mechanic_key = "evaluate_coc6_d100"
    if domain == "skills" and _SKILL_RULE_RE.search(haystack):
        mechanic_key = "coc_skill_check"
    elif domain == "combat" and _COMBAT_RULE_RE.search(haystack):
        mechanic_key = "coc_attack_action"
    elif domain == "resistance" and _RESISTANCE_RULE_RE.search(haystack):
        mechanic_key = "coc_resistance_check"
    elif domain == "sanity" and _SANITY_RULE_RE.search(haystack) and _SANITY_PROCEDURE_RE.search(haystack):
        mechanic_key = "coc_apply_resource"
    elif domain == "resources" and _RESOURCE_RULE_RE.search(haystack):
        mechanic_key = "coc_apply_resource"
    elif domain == "insanity" and _INSANITY_RULE_RE.search(haystack):
        mechanic_key = "coc_insanity_action"
    elif domain == "spells" and _SPELL_RULE_RE.search(haystack):
        mechanic_key = "coc_spell_cost_action"

    confidence = 0.82 if mechanic_key else (0.55 if domain != "general" else 0.35)
    return {"rule_domain": domain, "mechanic_key": mechanic_key, "confidence": confidence}


def _score_text(query_terms: Sequence[str], values: Iterable[Any]) -> int:
    text = normalize_reference_name(" ".join(str(value or "") for value in values))
    score = 0
    for term in query_terms:
        if term and term in text:
            score += 4 if len(term) > 2 else 1
    return score


def _query_terms(query: str) -> List[str]:
    normalized = normalize_reference_name(query)
    return [part for part in re.split(r"[\s,./:;()（）「」『』\[\]]+", normalized) if part]


def sort_reference_matches(items: Iterable[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    terms = _query_terms(query)

    def key(item: Dict[str, Any]) -> tuple[int, float, int]:
        score = _score_text(
            terms,
            [
                item.get("title"),
                item.get("name"),
                item.get("normalized_name"),
                item.get("rule_domain"),
                item.get("mechanic_key"),
                item.get("raw_excerpt"),
                item.get("summary"),
                item.get("san_loss"),
            ],
        )
        names = [
            normalize_reference_name(item.get("title")),
            normalize_reference_name(item.get("name")),
            normalize_reference_name(item.get("normalized_name")),
        ]
        for term in terms:
            for name in names:
                if not term or not name:
                    continue
                if term == name:
                    score += 120
                elif name.startswith(term):
                    score += 70
                elif term in name:
                    score += 35
        confidence = item.get("confidence")
        if isinstance(confidence, str):
            confidence_value = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(confidence.lower(), 0.0)
        else:
            try:
                confidence_value = float(confidence or 0)
            except (TypeError, ValueError):
                confidence_value = 0.0
        return (score, confidence_value, int(item.get("priority") or 0))

    return sorted(items, key=key, reverse=True)


async def search_rule_references(
    *,
    ruleset_key: str = "coc6",
    query: str = "",
    mechanic_keys: Optional[Sequence[str]] = None,
    rule_domains: Optional[Sequence[str]] = None,
    excluded_rule_domains: Optional[Sequence[str]] = None,
    creature_types: Optional[Sequence[str]] = None,
    include_creatures: bool = True,
    limit: int = 8,
) -> Dict[str, Any]:
    key = normalize_ruleset_key(ruleset_key)
    mechanic_values = [str(item).strip() for item in mechanic_keys or [] if str(item).strip()]
    domain_values = [str(item).strip() for item in rule_domains or [] if str(item).strip()]
    excluded_domain_values = [str(item).strip() for item in excluded_rule_domains or [] if str(item).strip()]
    creature_type_values = [str(item).strip() for item in creature_types or [] if str(item).strip()]
    terms = _query_terms(query)
    like_terms = [f"%{term}%" for term in terms[:6]]
    normalized_query = normalize_reference_name(query)
    mechanic_query_only = bool(mechanic_values) and normalized_query in {
        normalize_reference_name(value) for value in mechanic_values
    }

    async with await get_db_session() as session:
        rule_stmt = select(TRPGRuleItem).where(
            TRPGRuleItem.ruleset_key == key,
            TRPGRuleItem.is_active.is_(True),
        )
        if mechanic_values:
            rule_stmt = rule_stmt.where(TRPGRuleItem.mechanic_key.in_(mechanic_values))
        if domain_values:
            rule_stmt = rule_stmt.where(TRPGRuleItem.rule_domain.in_(domain_values))
        if excluded_domain_values:
            rule_stmt = rule_stmt.where(TRPGRuleItem.rule_domain.notin_(excluded_domain_values))
        if like_terms and not mechanic_query_only:
            rule_stmt = rule_stmt.where(
                or_(
                    *[
                        or_(
                            TRPGRuleItem.title.ilike(term),
                            TRPGRuleItem.normalized_name.ilike(term),
                            TRPGRuleItem.raw_excerpt.ilike(term),
                        )
                        for term in like_terms
                    ]
                )
            )
        rule_stmt = rule_stmt.order_by(
            TRPGRuleItem.priority.desc(),
            TRPGRuleItem.confidence.desc(),
            TRPGRuleItem.updated_at.desc(),
        ).limit(max(1, limit * 3))
        rule_rows = (await session.execute(rule_stmt)).scalars().all()
        rules = sort_reference_matches([row.to_dict() for row in rule_rows], query)[:limit]

        mechanic_links: List[Dict[str, Any]] = []
        if mechanic_values:
            link_stmt = (
                select(TRPGMechanicRuleLink)
                .where(
                    TRPGMechanicRuleLink.ruleset_key == key,
                    TRPGMechanicRuleLink.mechanic_key.in_(mechanic_values),
                )
                .order_by(TRPGMechanicRuleLink.priority.desc(), TRPGMechanicRuleLink.updated_at.desc())
                .limit(limit)
            )
            mechanic_links = [row.to_dict() for row in (await session.execute(link_stmt)).scalars().all()]

        creatures: List[Dict[str, Any]] = []
        if include_creatures and not mechanic_values:
            creature_stmt = select(TRPGCreatureEntry).where(TRPGCreatureEntry.ruleset_key == key)
            if creature_type_values:
                creature_stmt = creature_stmt.where(TRPGCreatureEntry.entry_type.in_(creature_type_values))
            if like_terms:
                creature_stmt = creature_stmt.where(
                    or_(
                        *[
                            or_(
                                TRPGCreatureEntry.name.ilike(term),
                                TRPGCreatureEntry.normalized_name.ilike(term),
                                TRPGCreatureEntry.summary.ilike(term),
                                TRPGCreatureEntry.source_excerpt.ilike(term),
                            )
                            for term in like_terms
                        ]
                    )
                )
            creature_stmt = creature_stmt.order_by(TRPGCreatureEntry.updated_at.desc()).limit(max(80, limit * 8))
            creature_rows = (await session.execute(creature_stmt)).scalars().all()
            creatures = sort_reference_matches([row.to_dict() for row in creature_rows], query)[:limit]

    return {
        "ruleset_key": key,
        "query": query,
        "mechanic_keys": mechanic_values,
        "rule_domains": domain_values,
        "creature_types": creature_type_values,
        "rules": rules,
        "creatures": creatures,
        "mechanic_links": mechanic_links,
    }


async def get_rule_reference_stats(ruleset_key: str = "coc6") -> Dict[str, Any]:
    key = normalize_ruleset_key(ruleset_key)
    async with await get_db_session() as session:
        rule_count = (
            await session.execute(
                select(func.count()).select_from(TRPGRuleItem).where(TRPGRuleItem.ruleset_key == key)
            )
        ).scalar_one()
        reference_count = (
            await session.execute(
                select(func.count()).select_from(TRPGReferenceDocument).where(TRPGReferenceDocument.ruleset_key == key)
            )
        ).scalar_one()
        creature_count = (
            await session.execute(
                select(func.count()).select_from(TRPGCreatureEntry).where(TRPGCreatureEntry.ruleset_key == key)
            )
        ).scalar_one()
        creature_type_rows = (
            await session.execute(
                select(TRPGCreatureEntry.entry_type, func.count())
                .where(TRPGCreatureEntry.ruleset_key == key)
                .group_by(TRPGCreatureEntry.entry_type)
            )
        ).all()
        mechanic_rows = (
            await session.execute(
                select(TRPGRuleItem.mechanic_key, func.count())
                .where(TRPGRuleItem.ruleset_key == key, TRPGRuleItem.mechanic_key != "")
                .group_by(TRPGRuleItem.mechanic_key)
                .order_by(func.count().desc())
            )
        ).all()
        rule_domain_rows = (
            await session.execute(
                select(TRPGRuleItem.rule_domain, func.count())
                .where(TRPGRuleItem.ruleset_key == key, TRPGRuleItem.is_active.is_(True))
                .group_by(TRPGRuleItem.rule_domain)
                .order_by(func.count().desc())
            )
        ).all()
        document_type_rows = (
            await session.execute(
                select(TRPGReferenceDocument.document_type, func.count())
                .where(TRPGReferenceDocument.ruleset_key == key, TRPGReferenceDocument.is_active.is_(True))
                .group_by(TRPGReferenceDocument.document_type)
                .order_by(func.count().desc())
            )
        ).all()
    return {
        "ruleset_key": key,
        "reference_documents": int(reference_count or 0),
        "rule_items": int(rule_count or 0),
        "creature_entries": int(creature_count or 0),
        "document_types": {str(name or "unknown"): int(count or 0) for name, count in document_type_rows},
        "creature_types": {str(name or "unknown"): int(count or 0) for name, count in creature_type_rows},
        "rule_domains": {str(name or "general"): int(count or 0) for name, count in rule_domain_rows},
        "mechanics": {str(name or ""): int(count or 0) for name, count in mechanic_rows},
    }


async def get_mechanic_rule_context(
    ruleset_key: str,
    mechanic_key: str,
    *,
    query: str = "",
    limit: int = 4,
) -> Dict[str, Any]:
    mechanic = list_coc_mechanic_keys().get(mechanic_key, {})
    domain = str(mechanic.get("rule_domain") or "")
    return await search_rule_references(
        ruleset_key=ruleset_key,
        query=query or mechanic_key,
        mechanic_keys=[mechanic_key],
        rule_domains=[domain] if domain else None,
        include_creatures=False,
        limit=limit,
    )


def format_rule_reference_context(reference_bundle: Dict[str, Any], *, max_excerpt_chars: int = 900) -> str:
    lines: List[str] = []
    rules = reference_bundle.get("rules") if isinstance(reference_bundle, dict) else []
    creatures = reference_bundle.get("creatures") if isinstance(reference_bundle, dict) else []
    if rules:
        lines.append("## Related Rules")
        for item in rules[:8]:
            title = item.get("title") or item.get("normalized_name") or "rule"
            domain = item.get("rule_domain") or "general"
            mechanic = item.get("mechanic_key") or ""
            source = item.get("source_title") or item.get("source_kind") or ""
            excerpt = str(item.get("raw_excerpt") or "").strip()
            if len(excerpt) > max_excerpt_chars:
                excerpt = excerpt[: max_excerpt_chars - 1].rstrip() + "..."
            marker = f"{domain}"
            if mechanic:
                marker += f" / {mechanic}"
            if source:
                marker += f" / {source}"
            lines.append(f"- {title} ({marker})")
            if excerpt:
                lines.append(f"  {excerpt}")
    if creatures:
        lines.append("## Related Creatures")
        for item in creatures[:6]:
            name = item.get("name") or "creature"
            kind = item.get("entry_type") or item.get("classification") or "creature"
            san = item.get("san_loss") or ""
            summary = str(item.get("summary") or item.get("raw_excerpt") or "").strip()
            if len(summary) > max_excerpt_chars:
                summary = summary[: max_excerpt_chars - 1].rstrip() + "..."
            suffix = f" / SAN {san}" if san else ""
            lines.append(f"- {name} ({kind}{suffix})")
            if summary:
                lines.append(f"  {summary}")
    return "\n".join(lines).strip()


async def build_ai_rule_context(
    *,
    ruleset_key: str,
    query: str,
    mechanic_keys: Optional[Sequence[str]] = None,
    limit: int = 8,
) -> str:
    bundle = await search_rule_references(
        ruleset_key=ruleset_key,
        query=query,
        mechanic_keys=mechanic_keys,
        limit=limit,
    )
    return format_rule_reference_context(bundle)


async def build_scenario_creation_rule_context(
    *,
    ruleset_key: str = "coc6",
    premise: str = "",
    limit: int = 8,
) -> str:
    query = " ".join(
        part
        for part in [
            premise,
            "scenario keeper sanity combat creature spell investigation",
        ]
        if part
    )
    return await build_ai_rule_context(ruleset_key=ruleset_key, query=query, limit=limit)
