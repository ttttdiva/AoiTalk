"""Audit TRPG scenario DB rows for runtime-ready structure.

Read-only by default. This intentionally reports archived source_text length but
does not print scenario body text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.memory.database import get_db_session
from src.models.ecc_models import (
    Scenario,
    ScenarioCharacter,
    TRPGScenarioDocument,
)
from src.services.scenario_service import (
    ScenarioError,
    SCENARIO_KIND_TRPG,
    _validate_trpg_character_nodes,
    _validate_trpg_source_label,
    _validate_trpg_structure_runtime_ready,
)


def _issue(kind: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"kind": kind, "message": message, **extra}


def _character_has_owner_user(character: ScenarioCharacter) -> bool:
    relationships = character.relationships or []
    if not isinstance(relationships, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "owner_user"
        for item in relationships
    )


async def audit_trpg_scenarios() -> List[Dict[str, Any]]:
    async with await get_db_session() as session:
        result = await session.execute(
            select(Scenario)
            .options(
                selectinload(Scenario.characters),
                selectinload(Scenario.trpg_documents),
            )
            .where(Scenario.scenario_kind == SCENARIO_KIND_TRPG)
            .order_by(Scenario.updated_at.desc())
        )
        scenarios = result.scalars().all()

    reports: List[Dict[str, Any]] = []
    for scenario in scenarios:
        report: Dict[str, Any] = {
            "scenario_id": str(scenario.id),
            "title": scenario.title,
            "ruleset": scenario.ruleset or "",
            "document_count": len(scenario.trpg_documents or []),
            "character_count": len(scenario.characters or []),
            "issues": [],
        }
        if not scenario.trpg_documents:
            report["issues"].append(_issue("missing_document", "TRPGシナリオ文書がありません"))
        for character in scenario.characters or []:
            if _character_has_owner_user(character):
                report["issues"].append(
                    _issue(
                        "player_sheet_mixed_into_scenario_character",
                        "ユーザー所有PCシートが ScenarioCharacter に混入しています",
                        character_id=str(character.id),
                        character_name=character.name,
                    )
                )
        for document in scenario.trpg_documents or []:
            structure = document.structure or {}
            node_count = len(structure.get("nodes") or []) if isinstance(structure, dict) else 0
            try:
                _validate_trpg_source_label(document.source_label or "")
            except ScenarioError as exc:
                report["issues"].append(
                    _issue(
                        "external_source_label",
                        exc.message,
                        document_id=str(document.id),
                        source_label=document.source_label or "",
                    )
                )
            try:
                _validate_trpg_structure_runtime_ready(structure)
            except ScenarioError as exc:
                report["issues"].append(
                    _issue(
                        "structure_not_runtime_ready",
                        exc.message,
                        document_id=str(document.id),
                        node_count=node_count,
                    )
                )
            try:
                _validate_trpg_character_nodes(structure, scenario.characters or [])
            except ScenarioError as exc:
                report["issues"].append(
                    _issue(
                        "character_nodes_not_expanded",
                        exc.message,
                        document_id=str(document.id),
                        node_count=node_count,
                    )
                )
            report.setdefault("documents", []).append(
                {
                    "document_id": str(document.id),
                    "source_text_chars": len(document.source_text or ""),
                    "node_count": node_count,
                }
            )
        reports.append(report)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit TRPG scenario DB structure")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    args = parser.parse_args()

    reports = asyncio.run(audit_trpg_scenarios())
    failing = [report for report in reports if report["issues"]]
    if args.json:
        print(json.dumps({"scenarios": reports}, ensure_ascii=False, indent=2))
    else:
        print(f"TRPG scenarios: {len(reports)}")
        print(f"Scenarios with issues: {len(failing)}")
        for report in reports:
            print(f"- {report['title']} ({report['scenario_id']}): {len(report['issues'])} issue(s)")
            for issue in report["issues"]:
                print(f"  [{issue['kind']}] {issue['message']}")
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
