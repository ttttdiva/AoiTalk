"""Audit unified TRPG reference documents and structured rule data."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List

from sqlalchemy import func, select

from src.memory.database import get_db_session
from src.models.ecc_models import TRPGCreatureEntry, TRPGReferenceDocument, TRPGRuleItem


async def audit_reference_documents(ruleset_key: str | None = None) -> Dict[str, Any]:
    async with await get_db_session() as session:
        doc_stmt = select(TRPGReferenceDocument)
        rule_stmt = select(TRPGRuleItem)
        creature_stmt = select(TRPGCreatureEntry)
        if ruleset_key:
            doc_stmt = doc_stmt.where(TRPGReferenceDocument.ruleset_key == ruleset_key)
            rule_stmt = rule_stmt.where(TRPGRuleItem.ruleset_key == ruleset_key)
            creature_stmt = creature_stmt.where(TRPGCreatureEntry.ruleset_key == ruleset_key)

        documents = list((await session.execute(doc_stmt.order_by(TRPGReferenceDocument.ruleset_key, TRPGReferenceDocument.title))).scalars())
        rule_count = (await session.execute(select(func.count()).select_from(rule_stmt.subquery()))).scalar_one()
        creature_count = (await session.execute(select(func.count()).select_from(creature_stmt.subquery()))).scalar_one()

        missing_rule_refs_stmt = select(TRPGRuleItem).where(TRPGRuleItem.reference_document_id.is_(None))
        missing_creature_refs_stmt = select(TRPGCreatureEntry).where(TRPGCreatureEntry.reference_document_id.is_(None))
        if ruleset_key:
            missing_rule_refs_stmt = missing_rule_refs_stmt.where(TRPGRuleItem.ruleset_key == ruleset_key)
            missing_creature_refs_stmt = missing_creature_refs_stmt.where(TRPGCreatureEntry.ruleset_key == ruleset_key)
        missing_rule_refs = list((await session.execute(missing_rule_refs_stmt.limit(20))).scalars())
        missing_creature_refs = list((await session.execute(missing_creature_refs_stmt.limit(20))).scalars())

        doc_type_rows = (
            await session.execute(
                select(TRPGReferenceDocument.ruleset_key, TRPGReferenceDocument.document_type, func.count())
                .group_by(TRPGReferenceDocument.ruleset_key, TRPGReferenceDocument.document_type)
                .order_by(TRPGReferenceDocument.ruleset_key, TRPGReferenceDocument.document_type)
            )
        ).all()

    issues: List[Dict[str, Any]] = []
    if not documents:
        issues.append({"kind": "missing_reference_documents", "message": "TRPGReferenceDocument がありません"})
    if missing_rule_refs:
        issues.append(
            {
                "kind": "rule_items_without_reference_document",
                "message": "TRPGRuleItem に親資料がありません",
                "count_sampled": len(missing_rule_refs),
            }
        )
    if missing_creature_refs:
        issues.append(
            {
                "kind": "creature_entries_without_reference_document",
                "message": "TRPGCreatureEntry に親資料がありません",
                "count_sampled": len(missing_creature_refs),
            }
        )

    return {
        "ruleset_key": ruleset_key or "all",
        "document_count": len(documents),
        "documents": [
            {
                "id": str(document.id),
                "ruleset_key": document.ruleset_key,
                "title": document.title,
                "document_type": document.document_type,
                "supplement_kind": document.supplement_kind,
                "is_active": bool(document.is_active),
            }
            for document in documents
        ],
        "document_types": [
            {"ruleset_key": ruleset, "document_type": kind, "count": int(count)}
            for ruleset, kind, count in doc_type_rows
            if ruleset_key is None or ruleset == ruleset_key
        ],
        "rule_item_count": int(rule_count or 0),
        "creature_entry_count": int(creature_count or 0),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ruleset", default="", help="Optional ruleset key, e.g. coc6")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    args = parser.parse_args()
    report = asyncio.run(audit_reference_documents(args.ruleset.strip() or None))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"ruleset: {report['ruleset_key']}")
        print(f"reference documents: {report['document_count']}")
        print(f"rule items: {report['rule_item_count']}")
        print(f"creature entries: {report['creature_entry_count']}")
        print(f"issues: {len(report['issues'])}")
        for issue in report["issues"]:
            print(f"- [{issue['kind']}] {issue['message']}")
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
