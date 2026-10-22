#!/usr/bin/env python3
"""Re-runnable owner-bound SNS v6 setup through authenticated public APIs.

Default commands are offline plan and readback. Apply must be explicit, uses
the existing transport, verifies the authenticated owner, and performs exactly
one atomic setup command. It never creates remote accounts or submits media.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.mediaops_bootstrap import BootstrapError, HttpTransport, canonical, digest, read_json, save_json
from src.services.media_brand_portfolio_service import SCHEMA, SOURCE_SHA256, portfolio_definition

ENDPOINT = "/api/operations/media/brand-portfolio"
PLAN_SCHEMA = "mediaops-brand-setup/v1"


def make_plan(owner: str, project_bindings: dict | None = None) -> dict:
    definition = portfolio_definition()
    owner = str(UUID(owner))
    bindings = {k: str(UUID(v)) for k, v in (project_bindings or {}).items()}
    keys = {"headquarters", *(b["brand_key"] for b in definition["brands"])}
    if set(bindings) - keys or len(set(bindings.values())) != len(bindings):
        raise BootstrapError("each known brand requires a distinct Project")
    plan = {"schema": PLAN_SCHEMA, "endpoint": ENDPOINT + "/setup", "mode": "setup_only",
            "payload": {"expected_owner_user_id": owner, "expected_source_sha256": SOURCE_SHA256,
                        "project_bindings": bindings},
            "idempotency_key": f"sns-v6:{UUID(owner).hex}:{SOURCE_SHA256[:12]}",
            "source": definition["source"],
            "projects": [{"brand_key": "headquarters", "project_name": definition["headquarters"]["project_name"],
                          "public_persona": False}, *[{"brand_key": b["brand_key"], "project_name": b["project_name"],
                                                      "public_persona": True} for b in definition["brands"]]],
            "remote_account_creation": False, "publication": False, "generation_submission": False,
            "automatic_probe_adoption": False}
    return {**plan, "plan_hash": digest(plan)}


def validate_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("plan_hash") != digest({k: v for k, v in plan.items() if k != "plan_hash"}):
        raise BootstrapError("setup plan checksum mismatch")
    payload = plan.get("payload", {})
    expected = make_plan(payload.get("expected_owner_user_id"), payload.get("project_bindings"))
    if plan != expected:
        raise BootstrapError("setup plan differs from the adopted v6 contract")


def verify_readback(result: dict, owner: str) -> dict:
    definition = portfolio_definition()
    if (result.get("schema") != SCHEMA or result.get("owner_user_id") != owner
            or result.get("source") != definition["source"] or result.get("status") != "ready"
            or result.get("headquarters_public_persona") is not False):
        raise BootstrapError("portfolio readback owner/source/state mismatch")
    brands = result.get("brands", [])
    if len(brands) != 8:
        raise BootstrapError("portfolio must read back eight public brands")
    project_ids = {result.get("headquarters_project_id")}
    persona_ids = set()
    account_ids = set()
    for row, original in zip(brands, definition["brands"], strict=True):
        persona = row.get("persona") or {}
        revision = persona.get("current_revision") or {}
        if (row.get("definition") != original or row.get("project_id") in project_ids
                or persona.get("owner_user_id") != owner or persona.get("project_id") != row.get("project_id")
                or not row.get("editorial_program_id") or not row.get("content_item_id")
                or revision.get("research_policy", {}).get("source_sha256") != SOURCE_SHA256
                or persona.get("id") in persona_ids):
            raise BootstrapError("portfolio Persona/Project/production mapping mismatch")
        project_ids.add(row["project_id"])
        persona_ids.add(persona["id"])
        for channel in row["channels"]:
            account = channel.get("account")
            if account:
                if (account.get("id") in account_ids or account.get("owner_user_id") != owner
                        or account.get("project_id") != row["project_id"] or account.get("persona_id") != persona["id"]):
                    raise BootstrapError("portfolio account readback crosses owner/brand/Project")
                account_ids.add(account["id"])
    if None in project_ids or len(project_ids) != 9:
        raise BootstrapError("portfolio must contain nine distinct Projects")
    return {"projects": len(project_ids), "personas": len(persona_ids), "internal_channels": len(account_ids),
            "source_sha256": SOURCE_SHA256, "readback_hash": digest(result)}


def execute(transport, plan: dict, *, apply: bool = False) -> dict:
    validate_plan(plan)
    owner = plan["payload"]["expected_owner_user_id"]
    auth = transport.request("GET", "/api/auth/status")
    if not auth.get("authenticated") or (auth.get("user") or {}).get("id") != owner:
        raise BootstrapError("authenticated owner differs; no setup mutation performed")
    if apply:
        transport.request("POST", plan["endpoint"], plan["payload"], plan["idempotency_key"])
    result = transport.request("GET", ENDPOINT)
    verified = verify_readback(result, owner)
    actual = {"headquarters": result["headquarters_project_id"], **{
        b["definition"]["brand_key"]: b["project_id"] for b in result["brands"]}}
    if any(actual[k] != v for k, v in plan["payload"]["project_bindings"].items()):
        raise BootstrapError("readback differs from the explicitly planned Project mapping")
    return {"result": result, "verified": verified}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("plan")
    offline.add_argument("--owner", required=True)
    offline.add_argument("--project-bindings", type=Path)
    offline.add_argument("--output", type=Path, required=True)
    for command in ("apply", "readback"):
        online = sub.add_parser(command)
        online.add_argument("--plan", type=Path, required=True)
        online.add_argument("--base-url", required=True)
        online.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = make_plan(args.owner, read_json(args.project_bindings) if args.project_bindings else None)
            save_json(args.output, result)
            summary = {"plan_hash": result["plan_hash"], "projects": 9, "personas": 8}
        else:
            result = execute(HttpTransport(args.base_url), read_json(args.plan), apply=args.command == "apply")
            if args.output:
                save_json(args.output, result)
            summary = result["verified"]
        print(canonical({"ok": True, "command": args.command, **summary}))
        return 0
    except (BootstrapError, ValueError, TypeError, KeyError, OSError) as exc:
        message = str(exc) if isinstance(exc, BootstrapError) else "invalid input or unavailable file"
        print(canonical({"ok": False, "message": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
