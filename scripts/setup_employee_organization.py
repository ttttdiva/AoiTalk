#!/usr/bin/env python3
"""Preview/apply a rerunnable employee manifest through canonical services.

No model, provider, account, action policy or ACL provisioning occurs here.
Work execution is a separate explicit assignment and shared coordinator tick.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import os
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _v2_revision(title, responsibility):
    """Return a new Agent revision payload with only P2/v2 fields."""

    return {
        "display_name": title,
        "mission": responsibility,
        "responsibility_summary": responsibility,
        "operational_instructions": responsibility,
        "model_overrides": {},
        "tool_ids": [],
        "delegate_policy": {"agents": [], "workers": []},
        "capability_ceiling": [],
        "wake_policy": {"mode": "manual"},
    }


def crowdworks_manifest(graph):
    """Reuse CW/Web/App identities; seed only the missing internal positions."""
    from src.services.agent_identity_service import AgentIdentityValidationError

    indexed = {node["agent"]["slug"]: node for node in graph["nodes"] if node["agent"].get("slug")}
    positions = (
        ("crowdworks", "crowdworks-marketplace", "CrowdWorks全体統括", None),
        ("web", "crowdworks-web-developer", "Web制作担当", "crowdworks"),
        ("app", "crowdworks-app-developer", "アプリ開発担当", "crowdworks"),
    )
    entries = []
    for key, slug, title, manager in positions:
        if slug not in indexed:
            raise AgentIdentityValidationError("existing CrowdWorks/Web/App employee is required: " + slug)
        agent = indexed[slug]["agent"]
        entry = {"key": key, "agent_id": agent["id"], "display_name": agent["display_name"], "job_title": title}
        if manager:
            entry["manager_key"] = manager
        entries.append(entry)
    jobs = (
        ("research", "案件調査・評価担当", "researcher", "提供された案件資料を調査・評価し、根拠、不足要件、適法な作業範囲を統括へ返す。"),
        ("writer", "ライター担当", "writer", "許可済み資料から原稿を制作し、根拠と成果物を品質reviewへ提出する。"),
        ("data", "軽作業担当", "data_worker", "権利と許可を確認した資料整理・データ整形を行う。個人経験を偽る回答や規約違反の仕事は引き受けない。"),
        ("review", "品質review担当", "reviewer", "他社員の成果物と記録された検証を独立にreviewし、合否と修正要件を全体統括へ返す。"),
    )
    for key, title, _role, responsibility in jobs:
        slug = "crowdworks-" + {"data": "data-worker", "review": "quality-reviewer"}.get(key, key)
        entry = {"key": key, "slug": slug, "display_name": title, "manager_key": "crowdworks", "job_title": title,
                 "responsibility_summary": responsibility}
        if slug in indexed:
            agent = indexed[slug]["agent"]
            entry.update(agent_id=agent["id"], display_name=agent["display_name"])
        if slug not in indexed:
            entry.update(activate=True, revision=_v2_revision(title, responsibility))
        entries.append(entry)
    return {"employees": entries}


def sns_manifest(graph, portfolio, *, actor_user_id, lead_agent_id):
    """Build reporting roles from the canonical owner-bound brand readback.

    These bindings are assignment inputs, not new scope grants. The existing
    Existing SNS lead identity is reused; its lifecycle/revision is left to an explicit
    manifest command. No Project/Persona/content/channel is created here.
    """
    from scripts.media_brand_portfolio_setup import verify_readback
    from src.services.agent_identity_service import AgentIdentityValidationError

    verify_readback(portfolio, str(actor_user_id))
    indexed = {node["agent"]["slug"]: node for node in graph["nodes"] if node["agent"].get("slug")}
    lead = next((node["agent"] for node in graph["nodes"] if node["agent"]["id"] == str(lead_agent_id)), None)
    if lead is None:
        raise AgentIdentityValidationError("existing SNS lead employee is required")
    entries = [{"key": "sns-lead", "agent_id": lead["id"], "display_name": lead["display_name"],
                "job_title": "SNS事業統括"}]
    bindings = []

    def position(key, slug, title, _role, instruction):
        entry = {"key": key, "slug": slug, "display_name": title, "manager_key": "sns-lead",
                 "job_title": title, "responsibility_summary": instruction}
        if slug in indexed:
            agent = indexed[slug]["agent"]
            entry.update(agent_id=agent["id"], display_name=agent["display_name"])
        if slug not in indexed:
            entry.update(activate=True, revision=_v2_revision(title, instruction))
        entries.append(entry)

    for brand in portfolio["brands"]:
        definition = brand["definition"]
        key = "sns-brand-" + definition["brand_key"]
        position(key, key, definition["display_name"] + " ブランド担当", "planner",
            "担当ブランドの採用済み設計と提供された根拠に基づき、共有専門社員へ調査・制作・運用・分析を委任し、review結果をSNS事業統括へ返す。公開や外部送信は別の明示承認経路へ引き渡す。")
        bindings.append({"employee_key": key, "brand_key": definition["brand_key"],
            "project_id": brand["project_id"], "persona_id": brand["persona"]["id"],
            "persona_revision_id": brand["persona"]["current_revision"]["id"],
            "editorial_program_id": brand["editorial_program_id"], "content_item_id": brand["content_item_id"]})
    for key, title, role, instruction in (
        ("research", "SNS共有調査担当", "researcher", "提供された原典と調査記録を評価し、不足情報・根拠・制作仮説をブランド担当へ返す。"),
        ("creation", "SNS共有制作担当", "writer", "ブランド担当の許可済み資料と採用済み設計から制作案を作り、成果物をreviewへ返す。"),
        ("operations", "SNS共有運用担当", "general_worker", "許可済みの制作進行・資料整理を担当し、公開要求は既存承認経路へ引き渡す。"),
        ("analysis", "SNS共有分析担当", "researcher", "提供された実測記録を分析し、未実測と実測を区別してブランド担当へ返す。"),
        ("review", "SNS共有品質review担当", "reviewer", "他社員の成果物、原典、QAと記録済み検証を独立reviewし合否を返す。"),
    ):
        position("sns-" + key, "sns-shared-" + key, title, role, instruction)
    return {"manifest": {"employees": entries}, "bindings": bindings,
            "headquarters_project_id": portfolio["headquarters_project_id"],
            "source_sha256": portfolio["source"]["sha256"]}


async def run(args):
    from src.api.agent_delegation_routes import OrganizationSetupRequest
    from src.app_config_store import load_app_config_snapshot_sync
    from src.memory.database import get_database_manager
    from src.services.agent_organization_service import AgentOrganizationService

    manager = get_database_manager()
    try:
        # A preview must not upgrade/stamp/seed the database or write config defaults.
        # Reuse the canonical CLI's revision check before allowing settings reads.
        await asyncio.to_thread(verify_existing_database, manager, read_only=not args.apply)
        config = await asyncio.to_thread(load_app_config_snapshot_sync)
        service = AgentOrganizationService(manager, config=config)
        graph = await service.graph(actor_user_id=args.actor_user_id)
        bindings = None
        if args.crowdworks:
            manifest = crowdworks_manifest(graph, config=config)
        elif args.sns:
            if not args.lead_agent_id:
                raise ValueError("SNS recipe requires an existing lead employee")
            from src.services.media_brand_portfolio_service import MediaBrandPortfolioService
            session = await service.identity._session()
            try:
                portfolio = await MediaBrandPortfolioService().readback(session,
                    {"id": args.actor_user_id, "role": "admin", "actor_type": "human"})
            finally:
                await session.close()
            plan = sns_manifest(graph, portfolio, actor_user_id=args.actor_user_id, lead_agent_id=args.lead_agent_id,
                config=config)
            manifest, bindings = plan["manifest"], plan["bindings"]
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        request = OrganizationSetupRequest.model_validate(manifest)
        employees = [employee.model_dump(mode="json", exclude_unset=True) for employee in request.employees]
        if args.apply:
            result = await service.setup(actor_user_id=args.actor_user_id, employees=employees)
        else:
            result = {"applied": False, "manifest": {"employees": employees}}
        if bindings is not None:
            result["brand_bindings"] = [{**binding, **({"agent_id": result["employees"][binding["employee_key"]]["id"]}
                if args.apply else {})} for binding in bindings]
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            destination = preview_output_path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(encoded, encoding="utf-8")
            print(destination, file=sys.stderr)
        else:
            print(encoded, end="")
    finally:
        await manager.close()
        manager.sync_engine.dispose()


def verify_existing_database(manager, *, read_only):
    from sqlalchemy import event
    from sqlalchemy.engine import make_url
    from scripts.init_db_schema import _alembic_config, _verify_head

    def read_only_transaction(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    if read_only:
        # Enforce the same read-only boundary for settings and every service SELECT.
        event.listen(manager.sync_engine, "begin", read_only_transaction)
        event.listen(manager.engine.sync_engine, "begin", read_only_transaction)
    _verify_head(_alembic_config(make_url(manager.sync_database_url)), manager.sync_engine)
    # This marker is set only after the existing schema passed the exact-head check.
    # get_session()/the snapshot reader can now open sessions without running upgrades.
    manager._initialized = True


def preview_output_path(path):
    """Resolve preview exports within this checkout's external temporary scope."""
    root = ROOT.resolve()
    temp = Path(os.path.abspath(tempfile.gettempdir()))
    resolved_temp = temp.resolve()
    if temp.is_relative_to(root) or resolved_temp.is_relative_to(root):
        raise ValueError("preview --output requires an OS temp directory outside the repository")
    checkout_key = os.path.normcase(str(root)).encode("utf-8")
    scope = temp / ("aoitalk-employee-preview-" + hashlib.sha256(checkout_key).hexdigest()[:16])
    resolved_scope = scope.resolve()
    if resolved_scope.is_relative_to(root) or resolved_scope != resolved_temp / scope.name:
        raise ValueError("preview --output requires an external OS temp scope")
    requested = Path(path)
    if not requested.is_absolute() and requested.parts and requested.parts[0].lower() == ".local":
        raise ValueError("preview --output no longer accepts .local; use a filename")
    destination = Path(os.path.abspath(requested if requested.is_absolute() else scope / requested))
    resolved = destination.resolve()
    if (destination == scope or not destination.is_relative_to(scope)
            or resolved == resolved_scope or not resolved.is_relative_to(resolved_scope)
            or resolved.is_relative_to(root)):
        raise ValueError("preview --output must be inside this checkout's OS temp scope")
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--crowdworks", action="store_true")
    source.add_argument("--sns", action="store_true")
    parser.add_argument("--actor-user-id", required=True, help="Existing authenticated administrator User UUID")
    parser.add_argument("--lead-agent-id")
    parser.add_argument("--apply", action="store_true", help="Apply explicit manifest; default only previews it")
    parser.add_argument("--output", type=Path, help="Write preview JSON in UTF-8 in this checkout's OS temp scope; use a filename")
    args = parser.parse_args(argv)
    if args.output:
        if args.apply:
            parser.error("--output is preview-only; do not combine it with --apply")
        try:
            preview_output_path(args.output)
        except ValueError as error:
            parser.error(str(error))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
