"""Dry-run and apply command for the P2 Agent revision migration.

The command intentionally requires an explicit database URL.  It never reads
``DATABASE_URL``/``AOITALK_DATABASE_URL`` from the environment because a
maintenance command must not accidentally attach to an installation database.
SQLite is accepted for the local/test harness.  A non-SQLite URL requires the
explicit ``--allow-nonproduction-db`` gate, and writes additionally require
``--allow-write``.

The command is read-only by default.  ``--apply`` creates one immutable
revision per Agent and records the migration audit through the repository's
audit hook/model when available.  Conversation bindings are immutable
historical snapshots; they are deliberately not updated.  The next turn's
normal latest-revision pin selects the new revision while in-flight work keeps
its existing pin.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from src.services.agent_revision_migration import (  # noqa: E402
    AgentRevisionConversion,
    AgentRevisionMigrationError,
    content_hash_for_patch,
    convert_legacy_revision,
)


MIGRATION_ID = "agent-revisions-v2"
ENVIRONMENT_DATABASE_KEYS = ("DATABASE_URL", "AOITALK_DATABASE_URL")


class MigrationSafetyError(RuntimeError):
    """A command safety gate or required integration is missing."""


class MigrationMismatchError(RuntimeError):
    """The database changed after the read-only plan was built."""


AUDIT_FALLBACK = "immutable_agent_revision_creation_ledger"


@dataclass(frozen=True)
class MigrationCandidate:
    agent: Any
    revision: Any
    conversion: AgentRevisionConversion
    partial_fields: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.partial_fields) or self.conversion.blocked

    def to_dict(self) -> dict[str, Any]:
        result = self.conversion.to_dict()
        result.update(
            {
                "agent_state": str(_value(self.agent, "state", "") or ""),
                "partial_fields": list(self.partial_fields),
                "active_selection": "latest_revision_by_version",
                "conversation_binding_action": "preserve_immutable_history",
            }
        )
        return result


@dataclass
class MigrationReport:
    candidates: list[MigrationCandidate] = field(default_factory=list)
    skipped_retired: int = 0
    skipped_already_migrated: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(candidate.blocked for candidate in self.candidates)

    @property
    def mismatch_count(self) -> int:
        return sum(
            len(candidate.conversion.collisions)
            + len(candidate.conversion.unresolved)
            + len(candidate.partial_fields)
            for candidate in self.candidates
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": MIGRATION_ID,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "skipped_retired": self.skipped_retired,
            "skipped_already_migrated": self.skipped_already_migrated,
            "warnings": list(self.warnings),
            "blocked": self.blocked,
            "mismatch_count": self.mismatch_count,
            "active_revision_selection": "latest_revision_by_version",
            "conversation_bindings": {
                "updated": 0,
                "preserved": True,
                "next_turn_selects_latest": True,
                "in_flight_revision_pins_preserved": True,
            },
            "audit": {
                "event_type": "agent_revision.migrated_v2",
                "fallback": AUDIT_FALLBACK,
                "idempotency_key_carries_migration_id": True,
            },
        }


def _column_name(model: Any, *names: str) -> str | None:
    for name in names:
        if hasattr(model, name):
            return name
    return None


def revision_v2_columns(model: Any) -> dict[str, str]:
    """Find either the public or ``*_json`` SQLAlchemy attribute names."""

    result = {
        "model_overrides": _column_name(model, "model_overrides_json", "model_overrides"),
        "tool_ids": _column_name(model, "tool_ids_json", "tool_ids"),
        "delegate_policy": _column_name(model, "delegate_policy_json", "delegate_policy"),
    }
    missing = [key for key, name in result.items() if name is None]
    if missing:
        raise MigrationSafetyError(
            "agent_revisions is missing P2 columns: " + ", ".join(missing)
        )
    return {key: str(value) for key, value in result.items()}


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_id(value: Any) -> str:
    return str(value) if value is not None else ""


def _detach_agent(agent: Any) -> dict[str, Any]:
    """Capture Agent fields used after a read transaction closes."""

    return {
        "id": getattr(agent, "id", None),
        "state": getattr(agent, "state", None),
        "display_name": getattr(agent, "display_name", None),
    }


def _detach_revision(revision: Any) -> dict[str, Any]:
    """Detach source values before ending the read-only SQLAlchemy transaction."""

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
    )
    return {name: copy.deepcopy(getattr(revision, name, None)) for name in names}


def _safe_config_from_row(row: Any) -> dict[str, Any]:
    if row is None:
        try:
            from src.config_defaults import load_default_config

            return load_default_config()
        except Exception:
            return {}
    value = getattr(row, "value", {})
    value = copy.deepcopy(value) if isinstance(value, Mapping) else {}
    try:
        from src.security.field_crypto import decrypt_json_secret_leaves

        value = decrypt_json_secret_leaves(value, aad_prefix="app_config_settings.value")
    except Exception:
        # The app config row may already be plaintext in a test harness.  Do
        # not print or otherwise expose it; conversion only reads safe route
        # metadata and the caller's config object remains process-local.
        pass
    return value if isinstance(value, dict) else {}


async def load_app_config(session: AsyncSession) -> dict[str, Any]:
    from src.app_config_store import GLOBAL_CONFIG_KEY
    from src.memory.models import AppConfigSetting

    row = await session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
    return _safe_config_from_row(row)


async def collect_report(
    session: AsyncSession,
    config: Mapping[str, Any],
    *,
    agent_model: Any | None = None,
    revision_model: Any | None = None,
    route_resolver: Callable[[Any, Any, str], Any] | None = None,
    resolutions: Mapping[str, Any] | None = None,
) -> MigrationReport:
    """Read latest non-retired revisions and build pure conversion plans."""

    if agent_model is None or revision_model is None:
        from src.memory.models import Agent, AgentRevision

        agent_model = agent_model or Agent
        revision_model = revision_model or AgentRevision
    columns = revision_v2_columns(revision_model)
    report = MigrationReport()
    agents = (
        await session.execute(
            select(agent_model).where(agent_model.state != "retired").order_by(agent_model.id)
        )
    ).scalars().all()
    for agent in agents:
        latest = (
            await session.execute(
                select(revision_model)
                .where(revision_model.agent_id == agent.id)
                .order_by(revision_model.version.desc(), revision_model.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None:
            continue
        values = {key: getattr(latest, name, None) for key, name in columns.items()}
        non_null = tuple(key for key, value in values.items() if value is not None)
        if len(non_null) == len(columns):
            report.skipped_already_migrated += 1
            continue
        try:
            resolution_input: Mapping[str, Any] | None = None
            if isinstance(resolutions, Mapping):
                agent_key = _safe_id(getattr(agent, "id", ""))
                slug = str(getattr(agent, "slug", "") or "").strip()
                selected = resolutions.get(agent_key)
                if not isinstance(selected, Mapping) and slug:
                    selected = resolutions.get(slug)
                if isinstance(selected, Mapping):
                    resolution_input = {agent_key: selected}
                elif any(key in resolutions for key in ("chat", "code", "research", "review", "summarize")):
                    resolution_input = resolutions
            conversion = convert_legacy_revision(
                latest,
                config,
                route_resolver=route_resolver,
                resolutions=resolution_input,
            )
        except AgentRevisionMigrationError as exc:
            # A malformed latest row is still represented in the report so the
            # command exits 2 without attempting an apply.
            conversion = AgentRevisionConversion(
                agent_id=_safe_id(agent.id),
                source_revision_id=_safe_id(getattr(latest, "id", "")),
                source_version=int(getattr(latest, "version", 0) or 0),
                model_overrides={},
                tool_ids=[],
                delegate_policy={},
                before_routes={},
                after_routes={},
                route_matches={},
                unresolved=[{"reason": "conversion_input_invalid", "code": exc.__class__.__name__}],
            )
        report.candidates.append(
            MigrationCandidate(
                agent=_detach_agent(agent),
                revision=_detach_revision(latest),
                conversion=conversion,
                partial_fields=non_null if non_null else (),
            )
        )
    if not report.candidates:
        report.warnings.append("no latest non-retired null revisions matched the P2 migration")
    return report


def _markdown_route(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_markdown(report: MigrationReport, *, apply: bool = False) -> str:
    mode = "apply plan" if apply else "dry-run"
    lines = [
        f"# Agent revision P2 migration ({mode})",
        "",
        f"- migration id: `{MIGRATION_ID}`",
        f"- candidates: {len(report.candidates)}",
        f"- skipped already migrated: {report.skipped_already_migrated}",
        f"- blocked: `{str(report.blocked).lower()}`",
        "",
    ]
    for candidate in report.candidates:
        conversion = candidate.conversion
        lines.extend(
            [
                f"## Agent `{conversion.agent_id}` revision v{conversion.source_version}",
                "",
                "### Routes",
                "",
                "| legacy role | v2 role | before | after | match |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for legacy_role, before in sorted(conversion.before_routes.items()):
            target = ""
            for item in conversion.after_routes:
                if legacy_role in (conversion.collisions[0].get("legacy_roles", []) if conversion.collisions else []):
                    target = item
                    break
            if not target:
                # Mapping is stable and intentionally kept local to the pure
                # service; derive it from the after route key by the public
                # map in the conversion module.
                from src.services.agent_revision_migration import LEGACY_ROLE_TO_V2_ROLE

                target = LEGACY_ROLE_TO_V2_ROLE.get(legacy_role, "")
            after = conversion.after_routes.get(target, {})
            match = conversion.route_matches.get(legacy_role)
            if match is None:
                candidate_matches = [
                    value
                    for key, value in conversion.route_matches.items()
                    if key.startswith(f"{legacy_role}:")
                ]
                match = bool(candidate_matches) and all(candidate_matches)
            lines.append(
                f"| `{legacy_role}` | `{target}` | `{_markdown_route(before)}` | `{_markdown_route(after)}` | `{str(match).lower()}` |"
            )
        if conversion.unmapped_roles:
            lines.extend(["", "### Unmapped roles", ""])
            for item in conversion.unmapped_roles:
                lines.append(f"- `{item.get('role', '')}`: {item.get('reason', '')}")
        if conversion.collisions:
            lines.extend(["", "### Collisions", ""])
            for collision in conversion.collisions:
                lines.append(
                    f"- `{collision.get('role', '')}` from `{', '.join(collision.get('legacy_roles', []))}`: unresolved route collision"
                )
        if conversion.resolutions_applied:
            lines.extend(["", "### Applied resolutions", ""])
            lines.extend(f"- `{role}`" for role in conversion.resolutions_applied)
        if conversion.resolved_collisions:
            lines.extend(["", "### Resolved collisions", ""])
            for item in conversion.resolved_collisions:
                lines.append(
                    f"- `{item.get('role', '')}`: resolved by operator; source routes remain shown above"
                )
        if conversion.unresolved:
            lines.extend(["", "### Unresolved", ""])
            for item in conversion.unresolved:
                lines.append(f"- `{item.get('role', '')}`: {item.get('reason', '')}")
        lines.extend(
            [
                "",
                "### Tools and delegation policy",
                "",
                f"- tool_ids: `{_markdown_route(conversion.tool_ids)}`",
                f"- delegate_policy: `{_markdown_route(conversion.delegate_policy)}`",
                "",
            ]
        )
        if candidate.partial_fields:
            lines.append(
                "- partial P2 columns already populated: `" + ", ".join(candidate.partial_fields) + "`"
            )
            lines.append("")
    if report.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report.warnings)
        lines.append("")
    lines.extend(
        [
            "## Binding and pin behavior",
            "",
            "Existing conversation bindings remain immutable. Work and Run rows are not rebound or terminalized: unfinished work keeps its exact revision, status, and payload. New selections resolve the latest revision.",
            "",
        ]
    )
    return "\n".join(lines)


def render_resolutions_json(report: MigrationReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _revision_content_hash(revision_payload: Mapping[str, Any]) -> str:
    try:
        from src.services.agent_identity_service import AgentIdentityService

        return AgentIdentityService._revision_content_hash(revision_payload)
    except Exception:
        return content_hash_for_patch(revision_payload, revision_payload)


def _new_revision_hash_material(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project the created revision to the identity service hash contract."""

    return {
        "agent_id": payload.get("agent_id"),
        "display_name": payload.get("display_name"),
        "mission": payload.get("mission"),
        "responsibility_summary": payload.get("responsibility_summary"),
        "operational_instructions": payload.get("operational_instructions"),
        "agent_team_id": None,
        "execution_profile_id": None,
        "allowed_subagent_ids": None,
        "capability_ceiling": copy.deepcopy(payload.get("capability_ceiling") or []),
        "wake_policy": copy.deepcopy(payload.get("wake_policy") or {}),
        "budget_policy": copy.deepcopy(payload.get("budget_policy") or {}),
        "concurrency_policy": copy.deepcopy(payload.get("concurrency_policy") or {}),
        "model_overrides": copy.deepcopy(payload.get("model_overrides") or {}),
        "tool_ids": list(payload.get("tool_ids") or []),
        "delegate_policy": copy.deepcopy(payload.get("delegate_policy") or {}),
    }


def _new_revision_payload(source: Any, patch: Mapping[str, Any], *, version: int, actor_id: Any) -> dict[str, Any]:
    payload = {
        "agent_id": _value(source, "agent_id"),
        "display_name": _value(source, "display_name", "Agent"),
        "mission": _value(source, "mission", "") or "",
        "responsibility_summary": _value(source, "responsibility_summary", "") or "",
        "operational_instructions": _value(source, "operational_instructions", "") or "",
        # Team/Profile/Subagent pins are accepted above only as historical
        # migration input. New immutable revisions must not carry them.
        "agent_team_id": None,
        "execution_profile_id": None,
        "allowed_subagent_ids": None,
        "capability_ceiling": list(_value(source, "capability_ceiling_json", _value(source, "capability_ceiling", [])) or []),
        "wake_policy": copy.deepcopy(_value(source, "wake_policy_json", _value(source, "wake_policy", {})) or {}),
        "budget_policy": copy.deepcopy(_value(source, "budget_policy_json", _value(source, "budget_policy", {})) or {}),
        "concurrency_policy": copy.deepcopy(_value(source, "concurrency_policy_json", _value(source, "concurrency_policy", {})) or {}),
        "version": version,
        "created_by": actor_id,
    }
    payload.update(copy.deepcopy(dict(patch)))
    return payload


def _audit_model() -> Any | None:
    try:
        import src.memory.models as models

        for name in ("AgentRevisionMigrationEvent", "AgentAuditEvent", "AuditEvent"):
            value = getattr(models, name, None)
            if value is not None:
                return value
    except Exception:
        return None
    return None


async def append_migration_audit(
    session: AsyncSession,
    *,
    agent_id: Any,
    source_revision_id: Any,
    new_revision_id: Any,
    source_version: int,
    new_version: int,
    migration_id: str,
    actor_id: Any = None,
    audit_writer: Callable[..., Any] | None = None,
) -> None:
    """Append one safe migration audit event in the caller's transaction.

    The project currently has several domain-specific audit ledgers, but no
    generic Agent audit table.  A future P2 schema can expose a generic
    ``AgentRevisionMigrationEvent`` (or inject ``audit_writer``) without
    changing the migration algorithm.  Until then, the immutable revision
    itself is the durable audit ledger: its deterministic idempotency key
    carries the migration id/source version and its ``created_by``/timestamp
    fields record provenance.  ``session.info`` receives the event as a
    diagnostic projection only.
    """

    event = {
        "event_type": "agent_revision.migrated_v2",
        "migration_id": migration_id,
        "agent_id": _safe_id(agent_id),
        "source_revision_id": _safe_id(source_revision_id),
        "new_revision_id": _safe_id(new_revision_id),
        "source_version": int(source_version),
        "new_version": int(new_version),
    }
    if audit_writer is not None:
        result = audit_writer(session=session, event=event, actor_id=actor_id)
        if hasattr(result, "__await__"):
            await result
        return
    model = _audit_model()
    if model is None:
        info = getattr(session, "info", None)
        if isinstance(info, dict):
            info.setdefault("agent_revision_migration_audit", []).append(event)
        return
    columns = getattr(model, "__table__", None)
    column_names = {column.name for column in columns.columns} if columns is not None else set()
    values: dict[str, Any] = {}
    for key, value in event.items():
        if key in column_names:
            values[key] = value
    if "actor_user_id" in column_names:
        values["actor_user_id"] = actor_id
    if "metadata" in column_names:
        values["metadata"] = event
    elif "payload" in column_names:
        values["payload"] = event
    if not values:
        raise MigrationSafetyError("P2 audit model has no compatible columns")
    session.add(model(**values))
    await session.flush()


async def apply_candidate(
    session: AsyncSession,
    candidate: MigrationCandidate,
    *,
    config: Mapping[str, Any],
    columns: Mapping[str, str],
    migration_id: str = MIGRATION_ID,
    actor_id: Any = None,
    audit_writer: Callable[..., Any] | None = None,
    agent_model: Any | None = None,
    revision_model: Any | None = None,
) -> dict[str, Any]:
    """Create one immutable v2 revision in one transaction."""

    if candidate.blocked:
        raise MigrationMismatchError("candidate has unresolved conversion findings")
    if agent_model is None or revision_model is None:
        from src.memory.models import Agent, AgentRevision

        agent_model = agent_model or Agent
        revision_model = revision_model or AgentRevision
    source = candidate.revision
    agent_id = _value(source, "agent_id")
    source_id = _value(source, "id")
    source_version = int(_value(source, "version", 0) or 0)
    idempotency_key = f"{migration_id}:{_safe_id(agent_id)}:v{source_version}"
    async with session.begin():
        locked_agent = (
            await session.execute(
                select(agent_model).where(agent_model.id == agent_id).with_for_update()
            )
        ).scalar_one_or_none()
        if locked_agent is None:
            raise MigrationMismatchError("agent disappeared during apply")
        if str(getattr(locked_agent, "state", "") or "") == "retired":
            return {"status": "skipped_retired", "agent_id": _safe_id(agent_id)}
        latest = (
            await session.execute(
                select(revision_model)
                .where(revision_model.agent_id == agent_id)
                .order_by(revision_model.version.desc(), revision_model.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if latest is None or latest.id != source_id or latest.version != source_version:
            raise MigrationMismatchError("latest AgentRevision changed after dry-run")
        current_values = {key: getattr(latest, name, None) for key, name in columns.items()}
        if all(value is not None for value in current_values.values()):
            return {"status": "already_migrated", "agent_id": _safe_id(agent_id)}
        if any(value is not None for value in current_values.values()):
            raise MigrationMismatchError("latest AgentRevision has partial P2 columns")
        next_version = source_version + 1
        max_version = (
            await session.execute(
                select(func.max(revision_model.version)).where(revision_model.agent_id == agent_id)
            )
        ).scalar_one()
        if int(max_version or 0) != source_version:
            raise MigrationMismatchError("AgentRevision version advanced during apply")
        patch = candidate.conversion.patch()
        payload = _new_revision_payload(source, patch, version=next_version, actor_id=actor_id)
        # The established identity service hash is authoritative.  Its input
        # mapping is kept separate from SQLAlchemy attribute names so this
        # remains compatible with either public or *_json columns.
        digest = _revision_content_hash(_new_revision_hash_material(payload))
        kwargs = {
            "id": uuid4(),
            "agent_id": agent_id,
            "version": next_version,
            "display_name": payload["display_name"],
            "mission": payload["mission"],
            "responsibility_summary": payload["responsibility_summary"],
            "operational_instructions": payload["operational_instructions"],
            "agent_team_id": payload["agent_team_id"],
            "execution_profile_id": payload["execution_profile_id"],
            "allowed_subagent_ids": payload["allowed_subagent_ids"],
            "capability_ceiling_json": payload["capability_ceiling"],
            "wake_policy_json": payload["wake_policy"],
            "budget_policy_json": payload["budget_policy"],
            "concurrency_policy_json": payload["concurrency_policy"],
            "content_hash": digest,
            "idempotency_key": idempotency_key,
            "created_by": actor_id,
        }
        row = revision_model(**kwargs)
        for key, name in columns.items():
            setattr(row, name, copy.deepcopy(patch[key]))
        session.add(row)
        await session.flush()
        await append_migration_audit(
            session,
            agent_id=agent_id,
            source_revision_id=source_id,
            new_revision_id=row.id,
            source_version=source_version,
            new_version=next_version,
            migration_id=migration_id,
            actor_id=actor_id,
            audit_writer=audit_writer,
        )
        return {
            "status": "created",
            "agent_id": _safe_id(agent_id),
            "source_revision_id": _safe_id(source_id),
            "new_revision_id": _safe_id(row.id),
            "source_version": source_version,
            "new_version": next_version,
            "content_hash": digest,
            "conversation_bindings_updated": 0,
        }


def _validate_url(url: str, *, apply: bool, allow_write: bool, allow_nonproduction_db: bool) -> None:
    clean = str(url or "").strip()
    if not clean:
        raise MigrationSafetyError("--database-url is required; environment DATABASE_URL is ignored")
    for key in ENVIRONMENT_DATABASE_KEYS:
        if os.getenv(key) and os.getenv(key) == clean:
            raise MigrationSafetyError(f"refusing URL copied from {key}; pass a dedicated test database URL")
    if apply and not allow_write:
        raise MigrationSafetyError("--apply requires --allow-write")
    is_sqlite = clean.lower().startswith(("sqlite:", "sqlite+aiosqlite:"))
    if not is_sqlite and not allow_nonproduction_db:
        raise MigrationSafetyError(
            "non-SQLite databases require --allow-nonproduction-db; production DATABASE_URL is never accepted"
        )


async def run(
    *,
    database_url: str,
    apply: bool = False,
    allow_write: bool = False,
    allow_nonproduction_db: bool = False,
    resolutions_path: str | None = None,
    resolutions: Mapping[str, Any] | None = None,
    audit_writer: Callable[..., Any] | None = None,
) -> tuple[int, MigrationReport, list[dict[str, Any]]]:
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
            config = await load_app_config(session)
            report = await collect_report(session, config, resolutions=resolutions)
            # ``collect_report`` performs SELECTs and therefore leaves an
            # autobegun read transaction open on SQLAlchemy sessions.  End it
            # before each per-Agent write transaction so the apply boundary is
            # explicit and never becomes a nested transaction by accident.
            await session.rollback()
            if resolutions_path:
                Path(resolutions_path).write_text(render_resolutions_json(report), encoding="utf-8")
            if report.blocked:
                return 2, report, results
            if not apply:
                return 0, report, results
            from src.memory.models import AgentRevision

            columns = revision_v2_columns(AgentRevision)
            for candidate in report.candidates:
                # Each invocation has its own transaction.  The transaction
                # closes before the next agent, keeping row locks bounded.
                results.append(
                    await apply_candidate(
                        session,
                        candidate,
                        config=config,
                        columns=columns,
                        audit_writer=audit_writer,
                    )
                )
            return 0, report, results
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="explicit test/non-production database URL")
    parser.add_argument("--apply", action="store_true", help="create immutable v2 revisions")
    parser.add_argument("--allow-write", action="store_true", help="required safety gate for --apply")
    parser.add_argument(
        "--allow-nonproduction-db",
        action="store_true",
        help="required for an explicitly supplied non-SQLite test/staging URL",
    )
    parser.add_argument(
        "--resolutions",
        dest="resolutions_input",
        help="JSON file containing agent_id/slug -> role -> resolved Route",
    )
    parser.add_argument(
        "--resolutions-json",
        dest="resolutions_path",
        help="write the redacted resolutions JSON to this path",
    )
    parser.add_argument("--json-only", action="store_true", help="print resolutions JSON instead of Markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    resolutions: Mapping[str, Any] | None = None
    if args.resolutions_input:
        try:
            resolution_source = Path(args.resolutions_input)
            if resolution_source.exists():
                resolution_text = resolution_source.read_text(encoding="utf-8")
            else:
                resolution_text = args.resolutions_input
            value = json.loads(resolution_text)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"migration blocked: invalid --resolutions JSON ({exc.__class__.__name__})", file=sys.stderr)
            return 2
        if not isinstance(value, Mapping):
            print("migration blocked: --resolutions must contain a JSON object", file=sys.stderr)
            return 2
        resolutions = value
    try:
        code, report, results = asyncio.run(
            run(
                database_url=args.database_url,
                apply=args.apply,
                allow_write=args.allow_write,
                allow_nonproduction_db=args.allow_nonproduction_db,
                resolutions_path=args.resolutions_path,
                resolutions=resolutions,
            )
        )
    except (MigrationSafetyError, MigrationMismatchError) as exc:
        print(f"migration blocked: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Keep operational failures bounded and avoid dumping config/URLs.
        print(f"migration failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    if args.json_only:
        print(render_resolutions_json(report), end="")
    else:
        print(render_markdown(report, apply=args.apply))
        print("## Resolutions JSON\n\n```json")
        print(render_resolutions_json(report), end="")
        print("```")
    for result in results:
        print(f"applied: {result.get('status')} agent={result.get('agent_id')}")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

