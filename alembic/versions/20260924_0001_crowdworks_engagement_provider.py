"""Enable durable CrowdWorks engagement actions and repair legacy evidence drift.

Some installations applied an older 20260908_0002 body before the repository
added the legacy_evidence_incomplete markers/guards.  This revision is the
first one after that drift was discovered, so it repairs both shapes before
widening engagement.submit_application to the registry-backed provider lane.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260924_0001"
down_revision = "20260923_0002"
branch_labels = None
depends_on = None

_ACTION_NEW = (
    "legacy_evidence_incomplete OR execution_mode <> 'provider' OR "
    "(adapter_key IS NOT NULL AND length(trim(adapter_key)) > 0 "
    "AND adapter_version IS NOT NULL AND length(trim(adapter_version)) > 0 "
    "AND credential_state_hash IS NOT NULL AND length(credential_state_hash) = 64 "
    "AND execution_key IS NOT NULL AND length(trim(execution_key)) > 0 "
    "AND ((action_type NOT LIKE 'media.%' "
    "AND registry_schema_version IS NOT NULL AND length(trim(registry_schema_version)) > 0) "
    "OR (capability_snapshot_id IS NOT NULL AND capability_snapshot_hash IS NOT NULL "
    "AND length(capability_snapshot_hash) = 64)))"
)

_ACTION_OLD = (
    "legacy_evidence_incomplete OR execution_mode <> 'provider' OR "
    "(adapter_key IS NOT NULL AND length(trim(adapter_key)) > 0 "
    "AND adapter_version IS NOT NULL AND length(trim(adapter_version)) > 0 "
    "AND credential_state_hash IS NOT NULL AND length(credential_state_hash) = 64 "
    "AND execution_key IS NOT NULL AND length(trim(execution_key)) > 0 "
    "AND ((action_type NOT LIKE 'media.%' AND action_type <> 'engagement.submit_application' "
    "AND registry_schema_version IS NOT NULL AND length(trim(registry_schema_version)) > 0) "
    "OR (capability_snapshot_id IS NOT NULL AND capability_snapshot_hash IS NOT NULL "
    "AND length(capability_snapshot_hash) = 64)))"
)

_ATTEMPT_NEW = (
    "legacy_evidence_incomplete OR execution_mode <> 'provider' OR "
    "(provider_key IS NOT NULL AND length(trim(provider_key)) > 0 "
    "AND provider_adapter_key IS NOT NULL AND length(trim(provider_adapter_key)) > 0 "
    "AND provider_adapter_version IS NOT NULL AND length(trim(provider_adapter_version)) > 0 "
    "AND credential_state_hash IS NOT NULL AND length(credential_state_hash) = 64 "
    "AND execution_key IS NOT NULL AND length(trim(execution_key)) > 0 "
    "AND ((registry_schema_version IS NOT NULL AND length(trim(registry_schema_version)) > 0) "
    "OR (capability_snapshot_id IS NOT NULL AND capability_snapshot_hash IS NOT NULL "
    "AND length(capability_snapshot_hash) = 64)))"
)


def _column_names(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}


def _check_names(table: str) -> set[str]:
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_check_constraints(table)
        if item.get("name")
    }


def _ensure_legacy_markers() -> None:
    for table in ("external_actions", "external_action_attempts"):
        if "legacy_evidence_incomplete" not in _column_names(table):
            op.add_column(
                table,
                sa.Column(
                    "legacy_evidence_incomplete",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )

    action_complete = (
        "adapter_key IS NOT NULL AND length(trim(adapter_key)) > 0 "
        "AND adapter_version IS NOT NULL AND length(trim(adapter_version)) > 0 "
        "AND credential_state_hash IS NOT NULL AND length(credential_state_hash) = 64 "
        "AND execution_key IS NOT NULL AND length(trim(execution_key)) > 0 "
        "AND ((action_type NOT LIKE 'media.%' "
        "AND action_type <> 'engagement.submit_application' "
        "AND registry_schema_version IS NOT NULL AND length(trim(registry_schema_version)) > 0) "
        "OR (capability_snapshot_id IS NOT NULL AND capability_snapshot_hash IS NOT NULL "
        "AND length(capability_snapshot_hash) = 64))"
    )
    attempt_complete = (
        "provider_key IS NOT NULL AND length(trim(provider_key)) > 0 "
        "AND provider_adapter_key IS NOT NULL AND length(trim(provider_adapter_key)) > 0 "
        "AND provider_adapter_version IS NOT NULL AND length(trim(provider_adapter_version)) > 0 "
        "AND credential_state_hash IS NOT NULL AND length(credential_state_hash) = 64 "
        "AND execution_key IS NOT NULL AND length(trim(execution_key)) > 0 "
        "AND ((registry_schema_version IS NOT NULL AND length(trim(registry_schema_version)) > 0) "
        "OR (capability_snapshot_id IS NOT NULL AND capability_snapshot_hash IS NOT NULL "
        "AND length(capability_snapshot_hash) = 64))"
    )
    op.execute(
        sa.text(
            "UPDATE external_actions SET legacy_evidence_incomplete = true "
            "WHERE execution_mode = 'provider' "
            f"AND NOT ({action_complete})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE external_action_attempts SET legacy_evidence_incomplete = true "
            "WHERE execution_mode = 'provider' "
            f"AND NOT ({attempt_complete})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE external_actions SET legacy_evidence_incomplete = true "
            "WHERE id IN (SELECT action_id FROM external_action_attempts "
            "WHERE legacy_evidence_incomplete)"
        )
    )


def _drop_obsolete_attempt_guard() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS ai_employee_attempt_legacy "
            "ON external_action_attempts"
        )
        op.execute("DROP FUNCTION IF EXISTS ai_employee_legacy_attempt()")
    elif bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS ai_employee_attempt_legacy_insert")
        op.execute("DROP TRIGGER IF EXISTS ai_employee_attempt_legacy_update")


def _repair_postgres_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        "DROP TRIGGER IF EXISTS external_action_origin_immutable ON external_actions"
    )
    op.execute("DROP FUNCTION IF EXISTS external_action_origin_guard()")
    op.execute(
        """
        CREATE FUNCTION external_action_origin_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.origin_agent_id IS DISTINCT FROM OLD.origin_agent_id
               OR NEW.origin_agent_run_id IS DISTINCT FROM OLD.origin_agent_run_id
               OR NEW.origin_work_item_id IS DISTINCT FROM OLD.origin_work_item_id
            THEN
                RAISE EXCEPTION 'action origin evidence is immutable';
            END IF;
            RETURN NEW;
        END; $$;
        """
    )
    op.execute(
        "CREATE TRIGGER external_action_origin_immutable BEFORE UPDATE "
        "ON external_actions FOR EACH ROW EXECUTE FUNCTION external_action_origin_guard()"
    )

    for table in ("external_actions", "external_action_attempts"):
        trigger = f"{table}_legacy_guard"
        function = f"{table}_legacy_guard"
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
        parent = (
            " OR EXISTS (SELECT 1 FROM external_actions a "
            "WHERE a.id = NEW.action_id AND a.legacy_evidence_incomplete)"
            if table == "external_action_attempts"
            else ""
        )
        op.execute(
            f"""
            CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.legacy_evidence_incomplete{parent} THEN
                        RAISE EXCEPTION 'legacy provider evidence is nonexecutable';
                    END IF;
                ELSE
                    IF NEW.legacy_evidence_incomplete IS DISTINCT FROM OLD.legacy_evidence_incomplete
                       OR ((OLD.legacy_evidence_incomplete{parent})
                           AND (to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD)))
                    THEN
                        RAISE EXCEPTION 'legacy provider evidence is nonexecutable';
                    END IF;
                END IF;
                RETURN NEW;
            END; $$;
            """
        )
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )


def _replace_check(table: str, name: str, expression: str) -> None:
    if name in _check_names(table):
        op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expression)


def upgrade():
    _ensure_legacy_markers()
    _drop_obsolete_attempt_guard()
    _replace_check(
        "external_actions",
        "ck_external_actions_provider_evidence",
        _ACTION_NEW,
    )
    _replace_check(
        "external_action_attempts",
        "ck_external_action_attempts_provider_evidence",
        _ATTEMPT_NEW,
    )
    _repair_postgres_guards()


def downgrade():
    _replace_check(
        "external_actions",
        "ck_external_actions_provider_evidence",
        _ACTION_OLD,
    )
