"""Compare and restore authority lost by an Agent revision P2 migration.

The command is read-only by default.  It requires an explicit
``--database-url`` and never discovers a URL from the process environment.
``--apply`` additionally requires ``--allow-write``.  The apply path creates
one immutable successor revision per affected Agent and records the restore
operation in the deterministic idempotency key; it never edits an existing
revision or rewrites historical bindings.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from src.services.agent_revision_authority import (  # noqa: E402
    AuthorityComparison,
    compare_revision_authority,
)
from src.services.agent_revision_migration import (  # noqa: E402
    content_hash_for_patch,
)


P2_MIGRATION_ID = "agent-revisions-v2"
RESTORE_MIGRATION_ID = "agent-revision-authority-restore-v2"
P2_IDEMPOTENCY_PREFIX = f"{P2_MIGRATION_ID}:"


class RestoreSafetyError(RuntimeError):
    """A command safety gate or required schema integration is missing."""


class RestoreMismatchError(RuntimeError):
    """The rows changed between comparison and an apply transaction."""


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_id(value: Any) -> str:
    return str(value) if value is not None else ""


def _p2_fields(row: Any) -> tuple[Any, Any, Any]:
    return (
        _value(row, "model_overrides", None),
        _value(row, "tool_ids", None),
        _value(row, "delegate_policy", None),
    )


def _is_p2_revision(row: Any) -> bool:
    key = str(_value(row, "idempotency_key", "") or "")
    return key.startswith(P2_IDEMPOTENCY_PREFIX) and all(
        value is not None for value in _p2_fields(row)
    )


def _is_legacy_revision(row: Any) -> bool:
    return all(value is None for value in _p2_fields(row))


def _detach(row: Any) -> dict[str, Any]:
    names = (
        "id",
        "agent_id",
        "version",
        "display_name",
        "mission",
        "responsibility_summary",
        "operational_instructions",
        "agent_team_id",
        "execution_profile_id",
        "allowed_subagent_ids",
        "capability_ceiling_json",
        "wake_policy_json",
        "budget_policy_json",
        "concurrency_policy_json",
        "model_overrides",
        "tool_ids",
        "delegate_policy",
        "content_hash",
        "idempotency_key",
        "created_by",
        "created_at",
    )
    return {name: copy.deepcopy(_value(row, name, None)) for name in names}


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class AuthorityRestoreCandidate:
    """A detached legacy/P2 pair and its pure authority comparison."""

    agent: dict[str, Any]
    legacy_revision: dict[str, Any]
    migrated_revision: dict[str, Any]
    comparison: AuthorityComparison

    @property
    def agent_id(self) -> str:
        return _safe_id(self.migrated_revision.get("agent_id"))

    @property
    def changed(self) -> bool:
        return self.comparison.changed

    @property
    def restorable(self) -> bool:
        return self.comparison.restorable

    @property
    def restore_patch(self) -> dict[str, list[str]]:
        return self.comparison.restore_patch()

    @property
    def restore_idempotency_key(self) -> str:
        return (
            f"{RESTORE_MIGRATION_ID}:{self.agent_id}:"
            f"from:{_safe_id(self.migrated_revision.get('id'))}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_state": str(self.agent.get("state") or ""),
            "legacy_revision_id": _safe_id(self.legacy_revision.get("id")),
            "legacy_version": int(self.legacy_revision.get("version") or 0),
            "migrated_revision_id": _safe_id(self.migrated_revision.get("id")),
            "migrated_version": int(self.migrated_revision.get("version") or 0),
            "migrated_idempotency_key": self.migrated_revision.get("idempotency_key"),
            "restore_idempotency_key": self.restore_idempotency_key,
            "changed": self.changed,
            "restorable": self.restorable,
            "comparison": self.comparison.to_dict(),
            "restore_patch": self.restore_patch,
        }


@dataclass
class AuthorityRestoreReport:
    candidates: list[AuthorityRestoreCandidate] = field(default_factory=list)
    skipped_retired: int = 0
    skipped_without_legacy_predecessor: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def affected(self) -> list[AuthorityRestoreCandidate]:
        return [candidate for candidate in self.candidates if candidate.restorable]

    @property
    def changed_count(self) -> int:
        return len(self.affected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "restore_migration_id": RESTORE_MIGRATION_ID,
            "p2_migration_id": P2_MIGRATION_ID,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "affected_agents": self.changed_count,
            "skipped_retired": self.skipped_retired,
            "skipped_without_legacy_predecessor": self.skipped_without_legacy_predecessor,
            "warnings": list(self.warnings),
            "dry_run_default": True,
            "immutable_revision_policy": {
                "one_successor_per_agent": True,
                "existing_revisions_unchanged": True,
                "bindings_rewritten": False,
            },
        }


def _revision_model_default() -> tuple[Any, Any]:
    from src.memory.models import Agent, AgentRevision

    return Agent, AgentRevision


async def collect_report(
    session: AsyncSession,
    config: Mapping[str, Any],
    *,
    agent_model: Any | None = None,
    revision_model: Any | None = None,
) -> AuthorityRestoreReport:
    """Read P2 rows and compare each to its immediately preceding legacy row."""

    if agent_model is None or revision_model is None:
        default_agent, default_revision = _revision_model_default()
        agent_model = agent_model or default_agent
        revision_model = revision_model or default_revision
    report = AuthorityRestoreReport()
    agents = (
        await session.execute(
            select(agent_model).order_by(agent_model.id)
        )
    ).scalars().all()
    for agent in agents:
        if str(_value(agent, "state", "") or "").casefold() == "retired":
            report.skipped_retired += 1
            continue
        rows = (
            await session.execute(
                select(revision_model)
                .where(revision_model.agent_id == agent.id)
                .order_by(revision_model.version.asc(), revision_model.id.asc())
            )
        ).scalars().all()
        for index, migrated in enumerate(rows):
            if not _is_p2_revision(migrated):
                continue
            previous = rows[index - 1] if index else None
            if previous is None or not _is_legacy_revision(previous):
                report.skipped_without_legacy_predecessor += 1
                continue
            legacy_detached = _detach(previous)
            migrated_detached = _detach(migrated)
            comparison = compare_revision_authority(
                legacy_detached,
                migrated_detached,
                config,
            )
            report.candidates.append(
                AuthorityRestoreCandidate(
                    agent={
                        "id": _value(agent, "id"),
                        "state": _value(agent, "state", ""),
                        "display_name": _value(agent, "display_name", ""),
                    },
                    legacy_revision=legacy_detached,
                    migrated_revision=migrated_detached,
                    comparison=comparison,
                )
            )
    if not report.candidates:
        report.warnings.append("no P2 revision with a legacy predecessor matched the authority comparison")
    return report


def _route_json(value: Any) -> str:
    return _safe_json(value or [])


def render_markdown(report: AuthorityRestoreReport, *, apply: bool = False) -> str:
    mode = "apply plan" if apply else "dry-run"
    lines = [
        f"# Agent revision authority restore ({mode})",
        "",
        f"- restore id: `{RESTORE_MIGRATION_ID}`",
        f"- P2 source id: `{P2_MIGRATION_ID}`",
        f"- comparisons: {len(report.candidates)}",
        f"- affected Agents: {report.changed_count}",
        "",
    ]
    for candidate in report.candidates:
        comparison = candidate.comparison
        before = comparison.before
        after = comparison.after
        lines.extend(
            [
                f"## Agent `{candidate.agent_id}` v{before.version} -> v{after.version}",
                "",
                f"- legacy revision: `{_safe_id(candidate.legacy_revision.get('id'))}`",
                f"- migrated revision: `{_safe_id(candidate.migrated_revision.get('id'))}`",
                f"- reduced: `{str(comparison.changed).lower()}`",
                f"- bounded restore available: `{str(comparison.restorable).lower()}`",
                "",
                "### Static scope",
                "",
                f"- Team `{before.team_id}` members: `{_route_json(before.team_member_ids)}`",
                f"- Agent allowed Subagents: `{_route_json(before.allowed_subagent_ids)}`",
                f"- before capability ceiling: `{_route_json(before.capability_ceiling)}`",
                f"- after capability ceiling: `{_route_json(after.capability_ceiling)}`",
                f"- before tool_ids: `{_safe_json(before.tool_ids)}`",
                f"- after tool_ids: `{_safe_json(after.tool_ids)}`",
                f"- before effective capabilities: `{_route_json(before.effective_capabilities)}`",
                f"- after effective capabilities: `{_route_json(after.effective_capabilities)}`",
                f"- before tool names: `{_route_json(before.tool_names)}`",
                f"- after tool names: `{_route_json(after.tool_names)}`",
                "",
                "### Reduction and restore patch",
                "",
                f"- reduced capability IDs: `{_route_json(comparison.reduced_capability_ids)}`",
                f"- reduced tool IDs: `{_route_json(comparison.reduced_tool_ids)}`",
                f"- reduced tool names: `{_route_json(comparison.reduced_tool_names)}`",
                f"- add to capability_ceiling: `{_route_json(comparison.restore_capability_ids)}`",
                f"- add to tool_ids: `{_route_json(comparison.restore_tool_ids)}`",
                f"- restore patch: `{_safe_json(comparison.restore_patch())}`",
                "",
            ]
        )
        if before.subagents:
            lines.extend(["### Subagent scope", "", "| id | member | enabled | declared | effective |", "| --- | --- | --- | --- | --- |"])
            for item in before.subagents:
                lines.append(
                    "| `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                        item.subagent_id,
                        str(item.team_member).lower(),
                        str(item.enabled).lower(),
                        _route_json(item.declared_capabilities),
                        _route_json(item.effective_capabilities),
                    )
                )
            lines.append("")
    if report.warnings:
        lines.extend(["## Warnings", "", *[f"- {warning}" for warning in report.warnings], ""])
    return "\n".join(lines)


def render_json(report: AuthorityRestoreReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _revision_payload(source: Any, *, version: int, actor_id: Any) -> dict[str, Any]:
    return {
        "agent_id": _value(source, "agent_id"),
        "display_name": _value(source, "display_name", "Agent") or "Agent",
        "mission": _value(source, "mission", "") or "",
        "responsibility_summary": _value(source, "responsibility_summary", "") or "",
        "operational_instructions": _value(source, "operational_instructions", "") or "",
        "agent_team_id": _value(source, "agent_team_id", _value(source, "team_id", "")) or "",
        "execution_profile_id": _value(source, "execution_profile_id", _value(source, "execution_profile", "")) or "",
        "allowed_subagent_ids": copy.deepcopy(
            _value(source, "allowed_subagent_ids", _value(source, "allowed_subagents", [])) or []
        ),
        "capability_ceiling": copy.deepcopy(
            _value(source, "capability_ceiling_json", _value(source, "capability_ceiling", [])) or []
        ),
        "wake_policy": copy.deepcopy(_value(source, "wake_policy_json", _value(source, "wake_policy", {})) or {}),
        "budget_policy": copy.deepcopy(_value(source, "budget_policy_json", _value(source, "budget_policy", {})) or {}),
        "concurrency_policy": copy.deepcopy(
            _value(source, "concurrency_policy_json", _value(source, "concurrency_policy", {})) or {}
        ),
        "model_overrides": copy.deepcopy(_value(source, "model_overrides", None)),
        "tool_ids": copy.deepcopy(_value(source, "tool_ids", None)),
        "delegate_policy": copy.deepcopy(_value(source, "delegate_policy", None)),
        "version": version,
        "created_by": actor_id,
    }


def _content_hash(source: Any, payload: Mapping[str, Any]) -> str:
    patch = {
        "model_overrides": copy.deepcopy(payload.get("model_overrides") or {}),
        "tool_ids": list(payload.get("tool_ids") or []),
        "delegate_policy": copy.deepcopy(payload.get("delegate_policy") or {}),
    }
    source_copy = dict(source) if isinstance(source, Mapping) else _detach(source)
    source_copy["capability_ceiling_json"] = copy.deepcopy(payload.get("capability_ceiling") or [])
    source_copy["capability_ceiling"] = copy.deepcopy(payload.get("capability_ceiling") or [])
    return content_hash_for_patch(source_copy, patch)


async def append_restore_audit(
    session: AsyncSession,
    *,
    agent_id: Any,
    source_revision_id: Any,
    new_revision_id: Any,
    source_version: int,
    new_version: int,
    audit_writer: Callable[..., Any] | None = None,
) -> None:
    event = {
        "event_type": "agent_revision.authority_restored_v2",
        "restore_id": RESTORE_MIGRATION_ID,
        "agent_id": _safe_id(agent_id),
        "source_revision_id": _safe_id(source_revision_id),
        "new_revision_id": _safe_id(new_revision_id),
        "source_version": int(source_version),
        "new_version": int(new_version),
    }
    if audit_writer is not None:
        result = audit_writer(session=session, event=event)
        if hasattr(result, "__await__"):
            await result
        return
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info.setdefault("agent_revision_authority_restore_audit", []).append(event)


async def apply_candidate(
    session: AsyncSession,
    candidate: AuthorityRestoreCandidate,
    *,
    actor_id: Any = None,
    audit_writer: Callable[..., Any] | None = None,
    agent_model: Any | None = None,
    revision_model: Any | None = None,
) -> dict[str, Any]:
    """Create one immutable successor revision for an affected Agent."""

    if not candidate.changed:
        return {"status": "unchanged", "agent_id": candidate.agent_id}
    if agent_model is None or revision_model is None:
        default_agent, default_revision = _revision_model_default()
        agent_model = agent_model or default_agent
        revision_model = revision_model or default_revision
    source = candidate.migrated_revision
    agent_id = _value(source, "agent_id")
    source_id = _value(source, "id")
    source_version = int(_value(source, "version", 0) or 0)
    idempotency_key = candidate.restore_idempotency_key
    try:
        async with session.begin():
            locked_agent = (
                await session.execute(
                    select(agent_model).where(agent_model.id == agent_id).with_for_update()
                )
            ).scalar_one_or_none()
            if locked_agent is None:
                raise RestoreMismatchError("Agent disappeared during authority restore")
            if str(_value(locked_agent, "state", "") or "").casefold() == "retired":
                return {"status": "skipped_retired", "agent_id": candidate.agent_id}
            existing_restore = (
                await session.execute(
                    select(revision_model)
                    .where(
                        revision_model.agent_id == agent_id,
                        revision_model.idempotency_key.like(
                            f"{RESTORE_MIGRATION_ID}:{candidate.agent_id}:%"
                        ),
                    )
                    .order_by(revision_model.version.desc(), revision_model.id.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing_restore is not None:
                return {
                    "status": "already_restored",
                    "agent_id": candidate.agent_id,
                    "new_revision_id": _safe_id(existing_restore.id),
                    "new_version": int(existing_restore.version or 0),
                }
            existing = (
                await session.execute(
                    select(revision_model)
                    .where(
                        revision_model.agent_id == agent_id,
                        revision_model.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {
                    "status": "already_restored",
                    "agent_id": candidate.agent_id,
                    "new_revision_id": _safe_id(existing.id),
                    "new_version": int(existing.version or 0),
                }
            latest = (
                await session.execute(
                    select(revision_model)
                    .where(revision_model.agent_id == agent_id)
                    .order_by(revision_model.version.desc(), revision_model.id.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if latest is None or _safe_id(latest.id) != _safe_id(source_id) or int(latest.version or 0) != source_version:
                raise RestoreMismatchError("latest AgentRevision changed after authority comparison")
            max_version = (
                await session.execute(
                    select(func.max(revision_model.version)).where(revision_model.agent_id == agent_id)
                )
            ).scalar_one()
            if int(max_version or 0) != source_version:
                raise RestoreMismatchError("AgentRevision version advanced during authority restore")
            patch = candidate.restore_patch
            payload = _revision_payload(
                latest,
                version=source_version + 1,
                actor_id=actor_id,
            )
            payload["capability_ceiling"] = list(patch["capability_ceiling"])
            payload["tool_ids"] = list(patch["tool_ids"])
            digest = _content_hash(_detach(latest), payload)
            row = revision_model(
                id=uuid4(),
                agent_id=agent_id,
                version=source_version + 1,
                display_name=payload["display_name"],
                mission=payload["mission"],
                responsibility_summary=payload["responsibility_summary"],
                operational_instructions=payload["operational_instructions"],
                agent_team_id=payload["agent_team_id"],
                execution_profile_id=payload["execution_profile_id"],
                allowed_subagent_ids=payload["allowed_subagent_ids"],
                capability_ceiling_json=payload["capability_ceiling"],
                wake_policy_json=payload["wake_policy"],
                budget_policy_json=payload["budget_policy"],
                concurrency_policy_json=payload["concurrency_policy"],
                model_overrides=payload["model_overrides"],
                tool_ids=payload["tool_ids"],
                delegate_policy=payload["delegate_policy"],
                content_hash=digest,
                idempotency_key=idempotency_key,
                created_by=actor_id,
            )
            session.add(row)
            await session.flush()
            await append_restore_audit(
                session,
                agent_id=agent_id,
                source_revision_id=source_id,
                new_revision_id=row.id,
                source_version=source_version,
                new_version=source_version + 1,
                audit_writer=audit_writer,
            )
            return {
                "status": "created",
                "agent_id": candidate.agent_id,
                "source_revision_id": _safe_id(source_id),
                "new_revision_id": _safe_id(row.id),
                "source_version": source_version,
                "new_version": source_version + 1,
                "content_hash": digest,
            }
    except IntegrityError as exc:
        await session.rollback()
        existing = (
            await session.execute(
                select(revision_model).where(
                    revision_model.agent_id == agent_id,
                    revision_model.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {
                "status": "already_restored",
                "agent_id": candidate.agent_id,
                "new_revision_id": _safe_id(existing.id),
                "new_version": int(existing.version or 0),
            }
        raise RestoreMismatchError("authority restore lost its idempotency race") from exc


def _validate_url(
    url: str,
    *,
    apply: bool,
    allow_write: bool,
    allow_nonproduction_db: bool = False,
) -> None:
    clean = str(url or "").strip()
    if not clean:
        raise RestoreSafetyError("--database-url is required; no database URL is read from the environment")
    if apply and not allow_write:
        raise RestoreSafetyError("--apply requires --allow-write")
    is_sqlite = clean.casefold().startswith(("sqlite:", "sqlite+aiosqlite:"))
    # Keep the P2 maintenance gate for read-only non-SQLite checks.  An
    # explicitly requested apply may target the production URL only after the
    # separate --allow-write gate; the URL is still never inferred from env.
    if not is_sqlite and not allow_nonproduction_db and not apply:
        raise RestoreSafetyError(
            "non-SQLite dry-runs require --allow-nonproduction-db; production apply requires an explicit URL and --allow-write"
        )


async def _set_read_only(session: AsyncSession, database_url: str) -> None:
    """Make the comparison transaction fail closed before its first SELECT."""

    connection = await session.connection()
    if str(database_url or "").casefold().startswith(("sqlite:", "sqlite+aiosqlite:")):
        await connection.exec_driver_sql("PRAGMA query_only=ON")
    else:
        await connection.exec_driver_sql("SET TRANSACTION READ ONLY")


async def run(
    *,
    database_url: str,
    apply: bool = False,
    allow_write: bool = False,
    allow_nonproduction_db: bool = False,
    audit_writer: Callable[..., Any] | None = None,
) -> tuple[int, AuthorityRestoreReport, list[dict[str, Any]]]:
    _validate_url(
        database_url,
        apply=apply,
        allow_write=allow_write,
        allow_nonproduction_db=allow_nonproduction_db,
    )
    engine = create_async_engine(database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    results: list[dict[str, Any]] = []
    try:
        async with factory() as session:
            from scripts.migrate_agent_revisions_v2 import load_app_config

            if not apply:
                await _set_read_only(session, database_url)
            config = await load_app_config(session)
            report = await collect_report(session, config)
            await session.rollback()
            if not apply:
                return 0, report, results
            applied_agents: set[str] = set()
            for candidate in report.affected:
                # The restore contract is one immutable successor per Agent,
                # even if a malformed/test database contains more than one
                # comparable P2 row.
                if candidate.agent_id in applied_agents:
                    continue
                applied_agents.add(candidate.agent_id)
                results.append(
                    await apply_candidate(
                        session,
                        candidate,
                        audit_writer=audit_writer,
                    )
                )
            return 0, report, results
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="explicit test/non-production database URL")
    parser.add_argument("--apply", action="store_true", help="create immutable authority-restore revisions")
    parser.add_argument("--allow-write", action="store_true", help="required safety gate for --apply")
    parser.add_argument(
        "--allow-nonproduction-db",
        action="store_true",
        help="required for an explicitly supplied non-SQLite dry-run URL; explicit --apply uses --allow-write",
    )
    parser.add_argument("--json-only", action="store_true", help="print JSON instead of Markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, report, results = asyncio.run(
            run(
                database_url=args.database_url,
                apply=args.apply,
                allow_write=args.allow_write,
                allow_nonproduction_db=args.allow_nonproduction_db,
            )
        )
    except (RestoreSafetyError, RestoreMismatchError) as exc:
        print(f"restore blocked: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"restore failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    if args.json_only:
        print(render_json(report), end="")
    else:
        print(render_markdown(report, apply=args.apply))
        print("## JSON\n\n```json")
        print(render_json(report), end="")
        print("```")
    for result in results:
        print(f"applied: {result.get('status')} agent={result.get('agent_id')}")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

