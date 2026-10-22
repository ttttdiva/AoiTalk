#!/usr/bin/env python3
"""Preview/apply a rerunnable employee manifest through canonical services.

No model, provider, account, action policy or ACL provisioning occurs here.
Work execution is a separate explicit assignment and shared coordinator tick.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def crowdworks_manifest(graph, *, config, worker_team_id, worker_profile_id, reviewer_team_id, reviewer_profile_id):
    """Reuse CW/Web/App identities; seed only the missing internal positions."""
    from src.services.agent_identity_service import AgentIdentityValidationError
    from src.services.agent_model_routing import OPERATIONS_WORK_ROLES, known_agent_teams

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
    teams = known_agent_teams(config)
    for key, title, role, responsibility in jobs:
        slug = "crowdworks-" + {"data": "data-worker", "review": "quality-reviewer"}.get(key, key)
        entry = {"key": key, "slug": slug, "display_name": title, "manager_key": "crowdworks", "job_title": title,
                 "responsibility_summary": responsibility}
        if slug in indexed:
            agent = indexed[slug]["agent"]
            entry.update(agent_id=agent["id"], display_name=agent["display_name"])
        if slug not in indexed:
            team_id, profile_id = (reviewer_team_id, reviewer_profile_id) if key == "review" else (worker_team_id, worker_profile_id)
            team = teams.get(team_id)
            if team is None:
                raise AgentIdentityValidationError("referenced employee Team is unavailable")
            allowed = [sid for sid in OPERATIONS_WORK_ROLES[role] if sid in team["subagent_ids"]]
            if not allowed:
                raise AgentIdentityValidationError("referenced Team cannot execute the employee role")
            entry.update(activate=True, revision={"display_name": title, "mission": responsibility,
                "responsibility_summary": responsibility, "operational_instructions": responsibility,
                "agent_team_id": team_id, "execution_profile_id": profile_id,
                "allowed_subagent_ids": allowed, "capability_ceiling": [], "wake_policy": {"mode": "manual"}})
        entries.append(entry)
    return {"employees": entries}


def sns_manifest(graph, portfolio, *, actor_user_id, director_agent_id, config,
                 brand_team_id, brand_profile_id, worker_team_id, worker_profile_id,
                 reviewer_team_id, reviewer_profile_id):
    """Build reporting roles from the canonical owner-bound brand readback.

    These bindings are assignment inputs, not new scope grants. The existing
    MediaDirector is reused; its lifecycle/revision is left to an explicit
    manifest command. No Project/Persona/content/channel is created here.
    """
    from scripts.media_brand_portfolio_setup import verify_readback
    from src.services.agent_identity_service import AgentIdentityValidationError
    from src.services.agent_model_routing import OPERATIONS_WORK_ROLES, known_agent_teams

    verify_readback(portfolio, str(actor_user_id))
    indexed = {node["agent"]["slug"]: node for node in graph["nodes"] if node["agent"].get("slug")}
    director = next((node["agent"] for node in graph["nodes"] if node["agent"]["id"] == str(director_agent_id)), None)
    if director is None:
        raise AgentIdentityValidationError("existing MediaDirector employee is required")
    teams = known_agent_teams(config)
    entries = [{"key": "sns-director", "agent_id": director["id"], "display_name": director["display_name"],
                "job_title": "SNS事業統括"}]
    bindings = []

    def position(key, slug, title, role, instruction, team_id, profile_id):
        entry = {"key": key, "slug": slug, "display_name": title, "manager_key": "sns-director",
                 "job_title": title, "responsibility_summary": instruction}
        if slug in indexed:
            agent = indexed[slug]["agent"]
            entry.update(agent_id=agent["id"], display_name=agent["display_name"])
        if slug not in indexed:
            team = teams.get(team_id)
            allowed = [sid for sid in OPERATIONS_WORK_ROLES[role] if team and sid in team["subagent_ids"]]
            if not allowed:
                raise AgentIdentityValidationError("referenced Team cannot execute the employee role")
            entry.update(activate=True, revision={"display_name": title, "mission": instruction,
                "responsibility_summary": instruction, "operational_instructions": instruction,
                "agent_team_id": team_id, "execution_profile_id": profile_id,
                "allowed_subagent_ids": allowed, "capability_ceiling": [], "wake_policy": {"mode": "manual"}})
        entries.append(entry)

    for brand in portfolio["brands"]:
        definition = brand["definition"]
        key = "sns-brand-" + definition["brand_key"]
        position(key, key, definition["display_name"] + " ブランド担当", "planner",
            "担当ブランドの採用済み設計と提供された根拠に基づき、共有専門社員へ調査・制作・運用・分析を委任し、review結果をSNS事業統括へ返す。公開や外部送信は別の明示承認経路へ引き渡す。",
            brand_team_id, brand_profile_id)
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
        team_id, profile_id = (reviewer_team_id, reviewer_profile_id) if key == "review" else (worker_team_id, worker_profile_id)
        position("sns-" + key, "sns-shared-" + key, title, role, instruction, team_id, profile_id)
    return {"manifest": {"employees": entries}, "bindings": bindings,
            "headquarters_project_id": portfolio["headquarters_project_id"],
            "source_sha256": portfolio["source"]["sha256"]}


async def run(args):
    from src.api.agent_delegation_routes import OrganizationSetupRequest
    from src.app_config_store import load_app_config_snapshot_sync
    from src.memory.database import get_database_manager
    from src.services.agent_delegation_service import AgentDelegationService
    from src.services.agent_organization_service import AgentOrganizationService

    AgentDelegationService.require_enabled()
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
            values = (args.worker_team_id, args.worker_profile_id, args.reviewer_team_id, args.reviewer_profile_id)
            if not all(values):
                raise ValueError("CrowdWorks recipe requires explicit existing Team/Profile IDs for worker and reviewer")
            manifest = crowdworks_manifest(graph, config=config, worker_team_id=values[0], worker_profile_id=values[1],
                reviewer_team_id=values[2], reviewer_profile_id=values[3])
        elif args.sns:
            values = (args.director_agent_id, args.brand_team_id, args.brand_profile_id,
                args.worker_team_id, args.worker_profile_id, args.reviewer_team_id, args.reviewer_profile_id)
            if not all(values):
                raise ValueError("SNS recipe requires an existing MediaDirector and explicit existing Team/Profile IDs")
            from src.services.media_brand_portfolio_service import MediaBrandPortfolioService
            session = await service.identity._session()
            try:
                portfolio = await MediaBrandPortfolioService().readback(session,
                    {"id": args.actor_user_id, "role": "admin", "actor_type": "human"})
            finally:
                await session.close()
            plan = sns_manifest(graph, portfolio, actor_user_id=args.actor_user_id, director_agent_id=values[0],
                config=config, brand_team_id=values[1], brand_profile_id=values[2], worker_team_id=values[3],
                worker_profile_id=values[4], reviewer_team_id=values[5], reviewer_profile_id=values[6])
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
    """Export only inside this repository's scratch area, including symlink checks."""
    destination = Path(path).resolve()
    scratch = ROOT / ".local"
    if not scratch.resolve().is_relative_to(ROOT) or not destination.is_relative_to(scratch.resolve()):
        raise ValueError("preview --output must be inside this repository's .local directory")
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--crowdworks", action="store_true")
    source.add_argument("--sns", action="store_true")
    parser.add_argument("--actor-user-id", required=True, help="Existing authenticated administrator User UUID")
    parser.add_argument("--director-agent-id")
    parser.add_argument("--brand-team-id")
    parser.add_argument("--brand-profile-id")
    parser.add_argument("--worker-team-id")
    parser.add_argument("--worker-profile-id")
    parser.add_argument("--reviewer-team-id")
    parser.add_argument("--reviewer-profile-id")
    parser.add_argument("--apply", action="store_true", help="Apply explicit manifest; default only previews it")
    parser.add_argument("--output", type=Path, help="Write preview JSON in UTF-8 inside this repository's .local directory")
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
