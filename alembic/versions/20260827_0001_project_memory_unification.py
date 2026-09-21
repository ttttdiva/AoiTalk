"""Unify legacy Project memory into ContextMemory and add Project Overview.

Revision ID: 20260827_0001
Revises: 20260826_0001
Create Date: 2026-08-27
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.security.field_crypto import (
    decrypt_json_value_if_needed,
    decrypt_text_if_needed,
    encrypt_json_value,
    encrypt_text,
)


# revision identifiers, used by Alembic.
revision = "20260827_0001"
down_revision = "20260826_0001"
branch_labels = None
depends_on = None


MIGRATION_ID = "project_memory_unification_20260827"

AGENT_MEMORY_PREFIX = "agent_memory:"
AGENT_MEMORY_ROOT_SYSTEM_KEY = "agent_memory_root"
AGENT_MEMORY_SYSTEM_KEY = "agent_memory"
AGENT_MEMORY_EMPTY_PLACEHOLDER = "(まだ記憶はありません)"
MAX_AGENT_MEMORY_DEPTH = 8

CONTEXT_MEMORY_CONTENT_AAD = "context_memories.content"
CONTEXT_MEMORY_STRUCTURED_DATA_AAD = "context_memories.structured_data"
CONTEXT_MEMORY_EVIDENCE_REFS_AAD = "context_memories.evidence_refs"
CONTEXT_MEMORY_EVIDENCE_SPAN_AAD = "context_memories.evidence_span"
CONTEXT_MEMORY_PROJECTION_METADATA_AAD = (
    "context_memories.projection_metadata"
)

_AGENT_MEMORY_ROOT_LIKE = r"agent\_memory:%"

_SECRET_LIKE_RE = re.compile(
    r"""
    (?:
        -----BEGIN[ ]+(?:RSA[ ]+|EC[ ]+|OPENSSH[ ]+)?PRIVATE[ ]+KEY-----
      |
        \bbearer\s+[A-Za-z0-9._~+/=-]{8,}
      |
        \b
        (?:
            password
          | passwd
          | pwd
          | secret
          | client[_\-\s]?secret
          | api[_\-\s]?key
          | access[_\-\s]?token
          | refresh[_\-\s]?token
          | authorization
          | token
        )
        \b
        \s*(?:[:=]|is\b)\s*
        [^\s,;]+
      |
        (?:
            パスワード
          | 秘密鍵
          | シークレット
          | APIキー
          | アクセストークン
          | リフレッシュトークン
          | トークン
        )
        \s*(?:[:=：]|は)\s*
        \S+
      |
        \b(?:sk|pk)_[A-Za-z0-9_-]{16,}\b
      |
        \bghp_[A-Za-z0-9]{20,}\b
      |
        \bgithub_pat_[A-Za-z0-9_]{20,}\b
      |
        \bxox[baprs]-[A-Za-z0-9-]{16,}\b
      |
        \bAIza[0-9A-Za-z_-]{20,}\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class _MigrationFailClosed(RuntimeError):
    """Abort without destructive cleanup when rescue safety is uncertain."""


def _utcnow() -> datetime:
    return datetime.utcnow()


def _require_online_migration() -> None:
    context = op.get_context()
    if bool(getattr(context, "as_sql", False)):
        raise _MigrationFailClosed(
            "20260827_0001 cannot run in offline --sql mode because legacy "
            "plaintext must be encrypted using configured field crypto; "
            "no legacy data has been modified"
        )


def _require_tables(bind: sa.Connection, names: Iterable[str]) -> None:
    inspector = sa.inspect(bind)
    available = set(inspector.get_table_names())
    missing = sorted(set(names) - available)
    if missing:
        raise _MigrationFailClosed(
            "required pre-unification tables are missing: "
            + ", ".join(missing)
        )


def _require_absent_tables(
    bind: sa.Connection,
    names: Iterable[str],
) -> None:
    inspector = sa.inspect(bind)
    available = set(inspector.get_table_names())
    present = sorted(set(names) & available)
    if present:
        raise _MigrationFailClosed(
            "Project Overview tables unexpectedly already exist: "
            + ", ".join(present)
        )


def _reflect(
    bind: sa.Connection,
    metadata: sa.MetaData,
    table_name: str,
) -> sa.Table:
    return sa.Table(
        table_name,
        metadata,
        autoload_with=bind,
    )


def _require_columns(
    table: sa.Table,
    names: Iterable[str],
) -> None:
    missing = sorted(set(names) - set(table.c.keys()))
    if missing:
        raise _MigrationFailClosed(
            f"{table.name} is missing required columns: "
            + ", ".join(missing)
        )


def _as_uuid(value: Any, *, context: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise _MigrationFailClosed(
            f"{context} contains an invalid UUID"
        ) from None


def _row_is_archived(row: Mapping[str, Any]) -> bool:
    if "archived_at" in row and row["archived_at"] is not None:
        return True
    if "is_archived" in row and bool(row["is_archived"]):
        return True
    if "archived" in row and bool(row["archived"]):
        return True
    return False


def _is_skippable_agent_title(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False

    normalized = value.strip()
    return (
        not normalized
        or normalized == AGENT_MEMORY_EMPTY_PLACEHOLDER
    )


def _legacy_tree_requires_project_owner(
    descendants: Iterable[Mapping[str, Any]],
) -> bool:
    """Return whether a legacy tree contains content that will be rescued.

    Placeholder nodes and nodes archived either directly or through an
    archived ancestor are intentionally discarded during the migration.  An
    orphan Agent Memory root containing only those nodes therefore does not
    need a project owner lookup; requiring one would prevent the already
    validated destructive cleanup from running.  Any active, non-placeholder
    descendant still requires owner resolution so an orphan with real content
    remains fail-closed.
    """

    return any(
        not bool(node["_effectively_archived"])
        and not _is_skippable_agent_title(node["title"])
        for node in descendants
    )


def _reject_secret_like(
    plaintext: str,
    *,
    source_ref: str,
) -> None:
    if _SECRET_LIKE_RE.search(plaintext):
        raise _MigrationFailClosed(
            "secret-like legacy content detected; plaintext is intentionally "
            f"not logged (source_ref={source_ref})"
        )


def _encrypt_text_or_fail(
    plaintext: str,
    *,
    source_ref: str,
) -> str:
    try:
        encrypted = encrypt_text(
            plaintext,
            aad=CONTEXT_MEMORY_CONTENT_AAD,
        )
    except Exception:
        raise _MigrationFailClosed(
            "field encryption failed for legacy ContextMemory content "
            f"(source_ref={source_ref})"
        ) from None

    if (
        not isinstance(encrypted, str)
        or not encrypted.startswith("enc:v1:")
    ):
        raise _MigrationFailClosed(
            "field encryption did not produce encrypted ContextMemory "
            f"content (source_ref={source_ref})"
        )

    return encrypted


def _encrypt_json_or_fail(
    value: Any,
    *,
    aad: str,
    source_ref: str,
    require_ciphertext: bool,
) -> Any:
    try:
        encrypted = encrypt_json_value(
            value,
            aad=aad,
        )
    except Exception:
        raise _MigrationFailClosed(
            "JSON field encryption failed for legacy ContextMemory data "
            f"(source_ref={source_ref})"
        ) from None

    if require_ciphertext and (
        not isinstance(encrypted, str)
        or not encrypted.startswith("enc:v1:")
    ):
        raise _MigrationFailClosed(
            "JSON field encryption did not produce encrypted ContextMemory "
            f"data (source_ref={source_ref})"
        )

    return encrypted


def _decrypt_content_for_verification(
    value: Any,
    *,
    source_ref: str,
) -> str:
    try:
        plaintext = decrypt_text_if_needed(
            value,
            aad=CONTEXT_MEMORY_CONTENT_AAD,
        )
    except Exception:
        raise _MigrationFailClosed(
            "verification decrypt failed for migrated ContextMemory content "
            f"(source_ref={source_ref})"
        ) from None

    if not isinstance(plaintext, str):
        raise _MigrationFailClosed(
            "verification produced non-text ContextMemory content "
            f"(source_ref={source_ref})"
        )

    return plaintext


def _decrypt_evidence_for_verification(
    value: Any,
    *,
    source_ref: str,
) -> Any:
    try:
        return decrypt_json_value_if_needed(
            value,
            aad=CONTEXT_MEMORY_EVIDENCE_REFS_AAD,
        )
    except Exception:
        raise _MigrationFailClosed(
            "verification decrypt failed for migrated evidence_refs "
            f"(source_ref={source_ref})"
        ) from None


def _dedupe_key(source_ref: str) -> str:
    payload = f"{MIGRATION_ID}\0{source_ref}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolve_project_owner(
    bind: sa.Connection,
    projects: sa.Table,
    users: sa.Table,
    project_id: uuid.UUID,
    *,
    cache: dict[str, tuple[uuid.UUID, str]],
) -> tuple[uuid.UUID, str]:
    cache_key = str(project_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    project_row = (
        bind.execute(
            sa.select(
                projects.c.id,
                projects.c.owner_id,
            ).where(
                projects.c.id == project_id
            )
        )
        .mappings()
        .first()
    )

    if (
        project_row is None
        or project_row["owner_id"] is None
        or not str(project_row["owner_id"]).strip()
    ):
        raise _MigrationFailClosed(
            "project/owner resolution failed "
            f"(project_id={project_id})"
        )

    resolved_project_id = _as_uuid(
        project_row["id"],
        context=f"projects.id for project_id={project_id}",
    )
    owner_value = project_row["owner_id"]
    owner_user_id = str(owner_value).strip()

    owner_exists = bind.execute(
        sa.select(users.c.id)
        .where(users.c.id == owner_value)
        .limit(1)
    ).first()

    if owner_exists is None:
        owner_exists = bind.execute(
            sa.select(users.c.id)
            .where(sa.cast(users.c.id, sa.String()) == owner_user_id)
            .limit(1)
        ).first()

    if owner_exists is None:
        raise _MigrationFailClosed(
            "project/owner resolution failed "
            f"(project_id={project_id})"
        )

    resolved = (resolved_project_id, owner_user_id)
    cache[cache_key] = resolved
    return resolved


def _select_agent_memory_project_roots(
    bind: sa.Connection,
    knowledge_nodes: sa.Table,
) -> list[Mapping[str, Any]]:
    columns = [
        knowledge_nodes.c.id,
        knowledge_nodes.c.parent_id,
        knowledge_nodes.c.project_id,
        knowledge_nodes.c.system_key,
    ]

    for optional in (
        "archived_at",
        "is_archived",
        "archived",
    ):
        if optional in knowledge_nodes.c:
            columns.append(knowledge_nodes.c[optional])

    rows = (
        bind.execute(
            sa.select(*columns).where(
                knowledge_nodes.c.system_key.like(
                    _AGENT_MEMORY_ROOT_LIKE,
                    escape="\\",
                )
            )
        )
        .mappings()
        .all()
    )

    roots: list[Mapping[str, Any]] = []
    seen_project_ids: set[str] = set()

    for row in rows:
        system_key = row["system_key"]
        if (
            not isinstance(system_key, str)
            or not system_key.startswith(AGENT_MEMORY_PREFIX)
        ):
            raise _MigrationFailClosed(
                "malformed legacy Agent Memory project root system_key "
                f"(node_id={row['id']})"
            )

        suffix = system_key[len(AGENT_MEMORY_PREFIX) :]
        project_id = _as_uuid(
            suffix,
            context=f"legacy Agent Memory project root node_id={row['id']}",
        )

        project_key = str(project_id)
        if project_key in seen_project_ids:
            raise _MigrationFailClosed(
                "multiple legacy Agent Memory project roots exist for the "
                f"same project (project_id={project_id})"
            )
        seen_project_ids.add(project_key)

        if row["project_id"] is not None:
            row_project_id = _as_uuid(
                row["project_id"],
                context=(
                    "legacy Agent Memory project root project_id "
                    f"node_id={row['id']}"
                ),
            )
            if row_project_id != project_id:
                raise _MigrationFailClosed(
                    "legacy Agent Memory project root project mismatch "
                    f"(node_id={row['id']})"
                )

        roots.append(row)

    return roots


def _enumerate_agent_memory_tree(
    bind: sa.Connection,
    knowledge_nodes: sa.Table,
    root: Mapping[str, Any],
) -> list[dict[str, Any]]:
    root_id = root["id"]
    root_archived = _row_is_archived(root)

    select_columns = [
        knowledge_nodes.c.id,
        knowledge_nodes.c.parent_id,
        knowledge_nodes.c.project_id,
        knowledge_nodes.c.system_key,
        knowledge_nodes.c.title,
    ]

    for optional in (
        "archived_at",
        "is_archived",
        "archived",
        "created_at",
        "updated_at",
    ):
        if optional in knowledge_nodes.c:
            select_columns.append(knowledge_nodes.c[optional])

    seen: set[str] = {str(root_id)}
    descendants: list[dict[str, Any]] = []

    frontier: list[tuple[Any, bool]] = [
        (root_id, root_archived)
    ]

    for depth in range(1, MAX_AGENT_MEMORY_DEPTH + 1):
        if not frontier:
            break

        parent_ids = [
            parent_id
            for parent_id, _ in frontier
        ]
        inherited_archived_by_parent = {
            str(parent_id): archived
            for parent_id, archived in frontier
        }

        rows = (
            bind.execute(
                sa.select(*select_columns).where(
                    knowledge_nodes.c.parent_id.in_(parent_ids)
                )
            )
            .mappings()
            .all()
        )

        next_frontier: list[tuple[Any, bool]] = []

        for row in rows:
            node_key = str(row["id"])
            if node_key in seen:
                raise _MigrationFailClosed(
                    "cycle or duplicate node detected while enumerating "
                    "legacy Agent Memory "
                    f"(node_id={row['id']})"
                )
            seen.add(node_key)

            inherited_archived = inherited_archived_by_parent.get(
                str(row["parent_id"]),
                False,
            )
            effectively_archived = (
                inherited_archived
                or _row_is_archived(row)
            )

            materialized = dict(row)
            materialized["_depth"] = depth
            materialized["_effectively_archived"] = effectively_archived
            descendants.append(materialized)

            next_frontier.append(
                (
                    row["id"],
                    effectively_archived,
                )
            )

        frontier = next_frontier

    if frontier:
        depth_eight_parent_ids = [
            parent_id
            for parent_id, _ in frontier
        ]
        deeper_row = bind.execute(
            sa.select(knowledge_nodes.c.id)
            .where(
                knowledge_nodes.c.parent_id.in_(
                    depth_eight_parent_ids
                )
            )
            .limit(1)
        ).first()

        if deeper_row is not None:
            raise _MigrationFailClosed(
                "legacy Agent Memory tree exceeds maximum migration depth "
                f"{MAX_AGENT_MEMORY_DEPTH} (root_id={root_id})"
            )

    return descendants


def _validate_descendant_project_scope(
    node: Mapping[str, Any],
    *,
    expected_project_id: uuid.UUID,
) -> None:
    if node["project_id"] is None:
        return

    node_project_id = _as_uuid(
        node["project_id"],
        context=f"legacy Agent Memory child node_id={node['id']}",
    )
    if node_project_id != expected_project_id:
        raise _MigrationFailClosed(
            "legacy Agent Memory child crosses project scope; refusing "
            "destructive cleanup "
            f"(node_id={node['id']})"
        )


def _prepare_agent_system_node_deletion_order(
    bind: sa.Connection,
    knowledge_nodes: sa.Table,
    *,
    legacy_tree_ids: set[str],
) -> list[Any]:
    system_rows = (
        bind.execute(
            sa.select(
                knowledge_nodes.c.id,
                knowledge_nodes.c.parent_id,
                knowledge_nodes.c.system_key,
            ).where(
                knowledge_nodes.c.system_key.in_(
                    (
                        AGENT_MEMORY_ROOT_SYSTEM_KEY,
                        AGENT_MEMORY_SYSTEM_KEY,
                    )
                )
            )
        )
        .mappings()
        .all()
    )

    if not system_rows:
        return []

    system_by_id = {
        str(row["id"]): row
        for row in system_rows
    }
    system_ids = set(system_by_id)
    allowed_child_ids = legacy_tree_ids | system_ids

    direct_children = (
        bind.execute(
            sa.select(
                knowledge_nodes.c.id,
                knowledge_nodes.c.parent_id,
            ).where(
                knowledge_nodes.c.parent_id.in_(
                    [row["id"] for row in system_rows]
                )
            )
        )
        .mappings()
        .all()
    )

    for child in direct_children:
        if str(child["id"]) not in allowed_child_ids:
            raise _MigrationFailClosed(
                "legacy Agent Memory system/root node has a non-legacy Docs "
                "child; generic Docs will not be deleted "
                f"(node_id={child['id']})"
            )

    remaining = set(system_ids)
    deletion_order: list[Any] = []

    while remaining:
        leaf_ids: list[str] = []

        for node_id in remaining:
            has_remaining_child = any(
                candidate_id in remaining
                and str(candidate["parent_id"]) == node_id
                for candidate_id, candidate in system_by_id.items()
            )
            if not has_remaining_child:
                leaf_ids.append(node_id)

        if not leaf_ids:
            raise _MigrationFailClosed(
                "cycle detected among legacy Agent Memory system/root nodes"
            )

        for node_id in sorted(leaf_ids):
            deletion_order.append(
                system_by_id[node_id]["id"]
            )
            remaining.remove(node_id)

    return deletion_order


def _preflight_project_context_pack_foreign_keys(
    bind: sa.Connection,
) -> None:
    inspector = sa.inspect(bind)

    legacy_pack_tables = {
        "project_context_packs",
        "project_context_pack_revisions",
        "project_context_pack_rebuild_jobs",
    }

    for table_name in inspector.get_table_names():
        foreign_keys = inspector.get_foreign_keys(table_name)

        for foreign_key in foreign_keys:
            referred_table = foreign_key.get("referred_table")
            if referred_table not in legacy_pack_tables:
                continue

            if table_name in legacy_pack_tables:
                continue

            raise _MigrationFailClosed(
                "unexpected external foreign key references a legacy "
                "ProjectContextPack table "
                f"({table_name} -> {referred_table})"
            )


def _canonical_context_memory_values(
    context_memories: sa.Table,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    plaintext: str,
    title: str,
    source_type: str,
    source_ref: str,
    trust_level: str,
    confidence: float,
    importance: int,
    created_by_actor: str,
    evidence_refs: list[dict[str, Any]],
    created_at: Any,
    updated_at: Any,
) -> dict[str, Any]:
    encrypted_content = _encrypt_text_or_fail(
        plaintext,
        source_ref=source_ref,
    )

    encrypted_evidence_refs = _encrypt_json_or_fail(
        evidence_refs,
        aad=CONTEXT_MEMORY_EVIDENCE_REFS_AAD,
        source_ref=source_ref,
        require_ciphertext=True,
    )

    now = _utcnow()

    values: dict[str, Any] = {
        "user_id": owner_user_id,
        "project_id": project_id,
        "scope_type": "project",
        "scope_id": str(project_id),
        "memory_type": "note",
        "title": title,
        "content": encrypted_content,
        "source_type": source_type,
        "source_ref": source_ref,
        "confidence": confidence,
        "importance": importance,
        "trust_level": trust_level,
        "sensitivity": "normal",
        "dedupe_key": _dedupe_key(source_ref),
        "version": 1,
        "created_by_actor": created_by_actor,
        "migration_id": MIGRATION_ID,
        "status": "active",
        "is_pinned": False,
        "evidence_refs": encrypted_evidence_refs,
        "created_at": created_at or now,
        "updated_at": updated_at or created_at or now,
    }

    nullable_defaults = {
        "task_id": None,
        "session_id": None,
        "expires_at": None,
        "supersedes_id": None,
        "rejection_reason": None,
    }
    for column_name, default_value in nullable_defaults.items():
        if column_name in context_memories.c:
            values[column_name] = default_value

    if "structured_data" in context_memories.c:
        values["structured_data"] = _encrypt_json_or_fail(
            {},
            aad=CONTEXT_MEMORY_STRUCTURED_DATA_AAD,
            source_ref=source_ref,
            require_ciphertext=False,
        )

    if "evidence_span" in context_memories.c:
        values["evidence_span"] = _encrypt_json_or_fail(
            {},
            aad=CONTEXT_MEMORY_EVIDENCE_SPAN_AAD,
            source_ref=source_ref,
            require_ciphertext=False,
        )

    if "projection_metadata" in context_memories.c:
        values["projection_metadata"] = _encrypt_json_or_fail(
            {
                "migration_id": MIGRATION_ID,
                "source_ref": source_ref,
            },
            aad=CONTEXT_MEMORY_PROJECTION_METADATA_AAD,
            source_ref=source_ref,
            require_ciphertext=True,
        )

    return values


def _normalize_or_insert_context_memory(
    bind: sa.Connection,
    context_memories: sa.Table,
    *,
    canonical: Mapping[str, Any],
) -> Any:
    source_ref = str(canonical["source_ref"])

    existing_rows = (
        bind.execute(
            sa.select(context_memories).where(
                context_memories.c.source_ref == source_ref
            )
        )
        .mappings()
        .all()
    )

    if len(existing_rows) > 1:
        raise _MigrationFailClosed(
            "multiple ContextMemory rows already use the same legacy "
            f"source_ref (source_ref={source_ref})"
        )

    existing_id = (
        existing_rows[0]["id"]
        if existing_rows
        else None
    )

    dedupe_collision_conditions = [
        context_memories.c.user_id == canonical["user_id"],
        context_memories.c.scope_type == canonical["scope_type"],
        context_memories.c.scope_id == canonical["scope_id"],
        context_memories.c.dedupe_key == canonical["dedupe_key"],
        context_memories.c.status == "active",
    ]

    if existing_id is not None:
        dedupe_collision_conditions.append(
            context_memories.c.id != existing_id
        )

    dedupe_collision = bind.execute(
        sa.select(context_memories.c.id)
        .where(sa.and_(*dedupe_collision_conditions))
        .limit(1)
    ).first()

    if dedupe_collision is not None:
        raise _MigrationFailClosed(
            "dangerous ContextMemory dedupe collision detected "
            f"(source_ref={source_ref})"
        )

    if existing_id is not None:
        bind.execute(
            sa.update(context_memories)
            .where(context_memories.c.id == existing_id)
            .values(**dict(canonical))
        )
        return existing_id

    memory_id = uuid.uuid4()
    bind.execute(
        sa.insert(context_memories).values(
            id=memory_id,
            **dict(canonical),
        )
    )
    return memory_id


def _verify_context_memory(
    bind: sa.Connection,
    context_memories: sa.Table,
    *,
    source_ref: str,
    plaintext: str,
    evidence_refs: list[dict[str, Any]],
    canonical: Mapping[str, Any],
) -> None:
    rows = (
        bind.execute(
            sa.select(context_memories).where(
                context_memories.c.source_ref == source_ref
            )
        )
        .mappings()
        .all()
    )

    if len(rows) != 1:
        raise _MigrationFailClosed(
            "post-normalization source_ref verification failed "
            f"(source_ref={source_ref})"
        )

    row = rows[0]

    encrypted_content = row["content"]
    if (
        not isinstance(encrypted_content, str)
        or not encrypted_content.startswith("enc:v1:")
    ):
        raise _MigrationFailClosed(
            "migrated ContextMemory content is not encrypted "
            f"(source_ref={source_ref})"
        )

    encrypted_evidence_refs = row["evidence_refs"]
    if (
        not isinstance(encrypted_evidence_refs, str)
        or not encrypted_evidence_refs.startswith("enc:v1:")
    ):
        raise _MigrationFailClosed(
            "migrated ContextMemory evidence_refs is not encrypted "
            f"(source_ref={source_ref})"
        )

    decrypted_content = _decrypt_content_for_verification(
        encrypted_content,
        source_ref=source_ref,
    )
    if decrypted_content != plaintext:
        raise _MigrationFailClosed(
            "migrated ContextMemory content does not exactly match the "
            f"legacy source (source_ref={source_ref})"
        )

    decrypted_evidence_refs = _decrypt_evidence_for_verification(
        encrypted_evidence_refs,
        source_ref=source_ref,
    )
    if decrypted_evidence_refs != evidence_refs:
        raise _MigrationFailClosed(
            "migrated ContextMemory evidence_refs do not exactly match the "
            f"expected evidence (source_ref={source_ref})"
        )

    exact_fields = (
        "user_id",
        "project_id",
        "scope_type",
        "scope_id",
        "memory_type",
        "title",
        "source_type",
        "source_ref",
        "importance",
        "trust_level",
        "sensitivity",
        "dedupe_key",
        "version",
        "created_by_actor",
        "migration_id",
        "status",
        "is_pinned",
    )

    for field_name in exact_fields:
        actual = row[field_name]
        expected = canonical[field_name]

        if field_name == "project_id":
            matches = str(actual) == str(expected)
        else:
            matches = actual == expected

        if not matches:
            raise _MigrationFailClosed(
                "migrated ContextMemory field verification failed "
                f"(source_ref={source_ref}, field={field_name})"
            )

    actual_confidence = float(row["confidence"])
    expected_confidence = float(canonical["confidence"])
    if abs(actual_confidence - expected_confidence) > 1e-9:
        raise _MigrationFailClosed(
            "migrated ContextMemory confidence verification failed "
            f"(source_ref={source_ref})"
        )


def _create_project_overview_tables() -> None:
    op.create_table(
        "project_overviews",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "layout_json",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column(
            "source_digest",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "generation_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "error_message",
            sa.String(length=500),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'building', 'fresh', 'failed')",
            name="ck_project_overviews_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            name="uq_project_overviews_project_id",
        ),
    )

    op.create_index(
        "ix_project_overviews_project_id",
        "project_overviews",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_overviews_source_digest",
        "project_overviews",
        ["source_digest"],
        unique=False,
    )
    op.create_index(
        "ix_project_overviews_status",
        "project_overviews",
        ["status"],
        unique=False,
    )

    op.create_table(
        "project_overview_refresh_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "requested_by",
            sa.String(length=120),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "error_message",
            sa.String(length=500),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_project_overview_refresh_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_project_overview_refresh_jobs_project_id",
        "project_overview_refresh_jobs",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_overview_refresh_jobs_status",
        "project_overview_refresh_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_project_overview_refresh_jobs_project_status",
        "project_overview_refresh_jobs",
        ["project_id", "status"],
        unique=False,
    )


def _verify_agent_memory_cleanup(
    bind: sa.Connection,
    knowledge_nodes: sa.Table,
    knowledge_supertags: sa.Table,
) -> None:
    remaining_project_root = bind.execute(
        sa.select(knowledge_nodes.c.id)
        .where(
            knowledge_nodes.c.system_key.like(
                _AGENT_MEMORY_ROOT_LIKE,
                escape="\\",
            )
        )
        .limit(1)
    ).first()

    if remaining_project_root is not None:
        raise _MigrationFailClosed(
            "legacy Agent Memory project nodes remain after cleanup"
        )

    remaining_system_node = bind.execute(
        sa.select(knowledge_nodes.c.id)
        .where(
            knowledge_nodes.c.system_key.in_(
                (
                    AGENT_MEMORY_ROOT_SYSTEM_KEY,
                    AGENT_MEMORY_SYSTEM_KEY,
                )
            )
        )
        .limit(1)
    ).first()

    if remaining_system_node is not None:
        raise _MigrationFailClosed(
            "legacy Agent Memory root/system nodes remain after cleanup"
        )

    remaining_supertag = bind.execute(
        sa.select(knowledge_supertags.c.id)
        .where(
            knowledge_supertags.c.system_key
            == AGENT_MEMORY_SYSTEM_KEY
        )
        .limit(1)
    ).first()

    if remaining_supertag is not None:
        raise _MigrationFailClosed(
            "legacy Agent Memory supertag remains after cleanup"
        )


def upgrade() -> None:
    _require_online_migration()

    bind = op.get_bind()

    _require_tables(
        bind,
        (
            "projects",
            "users",
            "context_memories",
            "knowledge_nodes",
            "knowledge_supertags",
            "project_context_packs",
            "project_context_pack_revisions",
            "project_context_pack_rebuild_jobs",
        ),
    )

    _require_absent_tables(
        bind,
        (
            "project_overviews",
            "project_overview_refresh_jobs",
        ),
    )

    _preflight_project_context_pack_foreign_keys(bind)

    metadata = sa.MetaData()

    projects = _reflect(
        bind,
        metadata,
        "projects",
    )
    users = _reflect(
        bind,
        metadata,
        "users",
    )
    context_memories = _reflect(
        bind,
        metadata,
        "context_memories",
    )
    knowledge_nodes = _reflect(
        bind,
        metadata,
        "knowledge_nodes",
    )
    knowledge_supertags = _reflect(
        bind,
        metadata,
        "knowledge_supertags",
    )
    project_context_packs = _reflect(
        bind,
        metadata,
        "project_context_packs",
    )

    _require_columns(
        projects,
        (
            "id",
            "owner_id",
        ),
    )
    _require_columns(
        users,
        ("id",),
    )
    _require_columns(
        knowledge_nodes,
        (
            "id",
            "parent_id",
            "project_id",
            "system_key",
            "title",
        ),
    )
    _require_columns(
        knowledge_supertags,
        (
            "id",
            "system_key",
        ),
    )
    _require_columns(
        project_context_packs,
        (
            "id",
            "project_id",
            "manual_notes",
        ),
    )
    _require_columns(
        context_memories,
        (
            "id",
            "user_id",
            "project_id",
            "scope_type",
            "scope_id",
            "memory_type",
            "title",
            "content",
            "source_type",
            "source_ref",
            "confidence",
            "importance",
            "trust_level",
            "sensitivity",
            "dedupe_key",
            "version",
            "created_by_actor",
            "migration_id",
            "status",
            "is_pinned",
            "evidence_refs",
            "created_at",
            "updated_at",
        ),
    )

    owner_cache: dict[str, tuple[uuid.UUID, str]] = {}

    expected_memories: list[dict[str, Any]] = []

    project_roots = _select_agent_memory_project_roots(
        bind,
        knowledge_nodes,
    )

    all_legacy_tree_ids: set[str] = set()
    legacy_tree_delete_rows: list[tuple[int, Any]] = []

    for root in project_roots:
        root_id = root["id"]
        root_system_key = str(root["system_key"])
        project_id = _as_uuid(
            root_system_key[len(AGENT_MEMORY_PREFIX) :],
            context=(
                "legacy Agent Memory project root "
                f"node_id={root_id}"
            ),
        )

        descendants = _enumerate_agent_memory_tree(
            bind,
            knowledge_nodes,
            root,
        )

        local_tree_ids = {
            str(root_id),
            *(
                str(node["id"])
                for node in descendants
            ),
        }

        overlap = all_legacy_tree_ids.intersection(
            local_tree_ids
        )
        if overlap:
            raise _MigrationFailClosed(
                "legacy Agent Memory node belongs to multiple project trees "
                f"(node_id={sorted(overlap)[0]})"
            )

        all_legacy_tree_ids.update(local_tree_ids)

        legacy_tree_delete_rows.append(
            (0, root_id)
        )
        legacy_tree_delete_rows.extend(
            (
                int(node["_depth"]),
                node["id"],
            )
            for node in descendants
        )

        for node in descendants:
            _validate_descendant_project_scope(
                node,
                expected_project_id=project_id,
            )

        root_archived = _row_is_archived(root)

        resolved_project_id: uuid.UUID | None = None
        owner_user_id: str | None = None

        if (
            not root_archived
            and _legacy_tree_requires_project_owner(descendants)
        ):
            (
                resolved_project_id,
                owner_user_id,
            ) = _resolve_project_owner(
                bind,
                projects,
                users,
                project_id,
                cache=owner_cache,
            )

        for node in descendants:
            if bool(node["_effectively_archived"]):
                continue

            plaintext = node["title"]

            if _is_skippable_agent_title(plaintext):
                continue

            if not isinstance(plaintext, str):
                raise _MigrationFailClosed(
                    "legacy Agent Memory child title is not text "
                    f"(node_id={node['id']})"
                )

            if (
                resolved_project_id is None
                or owner_user_id is None
            ):
                raise _MigrationFailClosed(
                    "active legacy Agent Memory content has no resolvable "
                    f"project owner (project_id={project_id})"
                )

            source_ref = (
                f"knowledge_node:{node['id']}"
            )

            _reject_secret_like(
                plaintext,
                source_ref=source_ref,
            )

            evidence_refs = [
                {
                    "source_type": "knowledge_node",
                    "source_ref": source_ref,
                    "knowledge_node_id": str(node["id"]),
                    "legacy_agent_memory_depth": int(
                        node["_depth"]
                    ),
                }
            ]

            canonical = _canonical_context_memory_values(
                context_memories,
                project_id=resolved_project_id,
                owner_user_id=owner_user_id,
                plaintext=plaintext,
                title=plaintext,
                source_type="legacy_agent_memory_migration",
                source_ref=source_ref,
                trust_level="inferred",
                confidence=0.8,
                importance=5,
                created_by_actor=(
                    "legacy_agent_memory_migration"
                ),
                evidence_refs=evidence_refs,
                created_at=node.get("created_at"),
                updated_at=node.get("updated_at"),
            )

            expected_memories.append(
                {
                    "source_ref": source_ref,
                    "plaintext": plaintext,
                    "evidence_refs": evidence_refs,
                    "canonical": canonical,
                }
            )

    agent_system_node_deletion_order = (
        _prepare_agent_system_node_deletion_order(
            bind,
            knowledge_nodes,
            legacy_tree_ids=all_legacy_tree_ids,
        )
    )

    pack_select_columns = [
        project_context_packs.c.id,
        project_context_packs.c.project_id,
        project_context_packs.c.manual_notes,
    ]

    for optional in (
        "created_at",
        "updated_at",
    ):
        if optional in project_context_packs.c:
            pack_select_columns.append(
                project_context_packs.c[optional]
            )

    pack_rows = (
        bind.execute(
            sa.select(*pack_select_columns)
        )
        .mappings()
        .all()
    )

    for pack in pack_rows:
        plaintext = pack["manual_notes"]

        if plaintext is None:
            continue

        if not isinstance(plaintext, str):
            raise _MigrationFailClosed(
                "ProjectContextPack.manual_notes is not text "
                f"(pack_id={pack['id']})"
            )

        if not plaintext.strip():
            continue

        pack_id = pack["id"]
        project_id = _as_uuid(
            pack["project_id"],
            context=(
                "ProjectContextPack project_id "
                f"pack_id={pack_id}"
            ),
        )

        (
            resolved_project_id,
            owner_user_id,
        ) = _resolve_project_owner(
            bind,
            projects,
            users,
            project_id,
            cache=owner_cache,
        )

        source_ref = (
            f"project_context_pack:{pack_id}"
        )

        _reject_secret_like(
            plaintext,
            source_ref=source_ref,
        )

        evidence_refs = [
            {
                "source_type": "project_context_pack",
                "source_ref": source_ref,
                "project_context_pack_id": str(pack_id),
                "field": "manual_notes",
            }
        ]

        canonical = _canonical_context_memory_values(
            context_memories,
            project_id=resolved_project_id,
            owner_user_id=owner_user_id,
            plaintext=plaintext,
            title="Project context manual notes",
            source_type="project_context_pack_migration",
            source_ref=source_ref,
            trust_level="verified",
            confidence=1.0,
            importance=7,
            created_by_actor="project_context_pack_migration",
            evidence_refs=evidence_refs,
            created_at=pack.get("created_at"),
            updated_at=pack.get("updated_at"),
        )

        expected_memories.append(
            {
                "source_ref": source_ref,
                "plaintext": plaintext,
                "evidence_refs": evidence_refs,
                "canonical": canonical,
            }
        )

    expected_source_refs: set[str] = set()
    for expected in expected_memories:
        source_ref = str(expected["source_ref"])
        if source_ref in expected_source_refs:
            raise _MigrationFailClosed(
                "duplicate legacy migration source_ref was generated "
                f"(source_ref={source_ref})"
            )
        expected_source_refs.add(source_ref)

    _create_project_overview_tables()

    for expected in expected_memories:
        _normalize_or_insert_context_memory(
            bind,
            context_memories,
            canonical=expected["canonical"],
        )

    for expected in expected_memories:
        _verify_context_memory(
            bind,
            context_memories,
            source_ref=expected["source_ref"],
            plaintext=expected["plaintext"],
            evidence_refs=expected["evidence_refs"],
            canonical=expected["canonical"],
        )

    verified_count = 0
    for source_ref in expected_source_refs:
        count = bind.execute(
            sa.select(sa.func.count())
            .select_from(context_memories)
            .where(
                context_memories.c.source_ref == source_ref
            )
        ).scalar_one()

        if int(count) != 1:
            raise _MigrationFailClosed(
                "legacy rescue verification found an unexpected source_ref "
                f"count (source_ref={source_ref})"
            )

        verified_count += 1

    if verified_count != len(expected_memories):
        raise _MigrationFailClosed(
            "legacy rescue verification count mismatch"
        )

    # All required rescue rows have now been inserted or idempotently
    # normalized and round-trip verified. Destructive cleanup is forbidden
    # before this point.
    for _, node_id in sorted(
        legacy_tree_delete_rows,
        key=lambda item: item[0],
        reverse=True,
    ):
        bind.execute(
            sa.delete(knowledge_nodes).where(
                knowledge_nodes.c.id == node_id
            )
        )

    for node_id in agent_system_node_deletion_order:
        bind.execute(
            sa.delete(knowledge_nodes).where(
                knowledge_nodes.c.id == node_id
            )
        )

    bind.execute(
        sa.delete(knowledge_supertags).where(
            knowledge_supertags.c.system_key
            == AGENT_MEMORY_SYSTEM_KEY
        )
    )

    _verify_agent_memory_cleanup(
        bind,
        knowledge_nodes,
        knowledge_supertags,
    )

    op.drop_table(
        "project_context_pack_revisions"
    )
    op.drop_table(
        "project_context_pack_rebuild_jobs"
    )
    op.drop_table(
        "project_context_packs"
    )


def downgrade() -> None:
    raise _MigrationFailClosed(
        "20260827_0001 is intentionally irreversible. The upgrade "
        "losslessly rescues retained legacy content into ContextMemory and "
        "then deletes legacy Agent Memory structures and ProjectContextPack "
        "generated state. Reconstructing those deleted runtime structures "
        "during downgrade would not be lossless. Restore a pre-migration "
        "database backup instead."
    )
