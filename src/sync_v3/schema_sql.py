"""Sync v3 変更ログのSQL生成（行トリガー・依存トリガー・install/uninstall）。

contract.py のDDLに、同期対象テーブルのトリガーを加えたSQLを組み立てる。
行トリガーは INSERT/UPDATE を AFTER、DELETE を BEFORE で記録する。DELETE を BEFORE にするのは、
親の削除記録（scope 付き）を ON DELETE CASCADE（RIトリガーとして親の削除後に実行される）より前に
sync_change_log に残し、子のscopeを過去記録から補うfallbackが、導入前から存在した親についても必ず解決できる
ようにするため。記録の op は目安で、S3 は行の現在状態から upsert/delete/hide を決めるため、別の BEFORE
トリガーが削除を取り消した場合の余分な D は無害。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import contract

UUID_RE = "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"

_NULL = "NULL::uuid"


@dataclass(frozen=True)
class _Spec:
    table: str
    entity_id: str  # {r} を OLD / NEW に置換する式
    project: str = _NULL
    library: str = _NULL
    space: str = _NULL
    owner: str = _NULL


# entity_id は v2 pull の key 形式（docs の junction は docsEntityKeyForStage に合わせる）。
_SPECS: tuple[_Spec, ...] = (
    _Spec("spaces", "{r}.id::text", space="{r}.id"),
    _Spec("projects", "{r}.id::text", project="{r}.id"),
    _Spec("tasks", "{r}.id::text", project="{r}.project_id"),
    _Spec("task_occurrences", "{r}.id::text", project="sync_v3_task_project({r}.task_id)"),
    _Spec("time_entries", "{r}.id::text", project="sync_v3_task_project({r}.task_id)"),
    _Spec("record_tables", "{r}.id::text", project="{r}.project_id"),
    _Spec("record_fields", "{r}.id::text", project="sync_v3_record_table_project({r}.table_id)"),
    _Spec("record_rows", "{r}.id::text", project="{r}.project_id"),
    _Spec("knowledge_nodes", "{r}.id::text", library="{r}.docs_library_id"),
    _Spec("knowledge_supertags", "{r}.id::text", library="{r}.docs_library_id"),
    _Spec(
        "knowledge_node_supertags",
        "{r}.node_id::text || ':' || {r}.supertag_id::text",
        library="sync_v3_node_library({r}.node_id)",
    ),
    _Spec(
        "knowledge_supertag_fields",
        "{r}.supertag_id::text || ':' || {r}.field_id::text",
        library="sync_v3_supertag_library({r}.supertag_id)",
    ),
    _Spec("knowledge_fields", "{r}.id::text", library="{r}.docs_library_id"),
    _Spec(
        "knowledge_field_values",
        "{r}.node_id::text || ':' || {r}.field_id::text",
        library="sync_v3_node_library({r}.node_id)",
    ),
    _Spec("knowledge_node_placements", "{r}.id::text", library="sync_v3_node_library({r}.node_id)"),
    _Spec("knowledge_edges", "{r}.id::text", library="sync_v3_node_library({r}.source_node_id)"),
)

# 会話2表は scope が「閲覧者の集合」なので専用関数で扱う。
_CONVERSATION_TABLES = ("conversation_sessions", "conversation_messages")

# 依存トリガー: (table, trigger suffix, 関数名, イベント種別)
_HELPERS_SQL = f"""
CREATE OR REPLACE FUNCTION sync_v3_emit(
  p_table text, p_id text, p_op char, p_owner uuid, p_project uuid, p_library uuid, p_space uuid
) RETURNS void LANGUAGE sql AS $fn$
  INSERT INTO sync_change_log (entity_table, entity_id, op, owner_user_id, project_id, docs_library_id, space_id)
  VALUES (p_table, p_id, p_op, p_owner, p_project, p_library, p_space)
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_task_project(p_id uuid) RETURNS uuid LANGUAGE sql AS $fn$
  SELECT coalesce(
    (SELECT project_id FROM tasks WHERE id = p_id),
    (SELECT project_id FROM sync_change_log
      WHERE entity_table = 'tasks' AND entity_id = p_id::text AND project_id IS NOT NULL
      ORDER BY seq DESC LIMIT 1))
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_record_table_project(p_id uuid) RETURNS uuid LANGUAGE sql AS $fn$
  SELECT coalesce(
    (SELECT project_id FROM record_tables WHERE id = p_id),
    (SELECT project_id FROM sync_change_log
      WHERE entity_table = 'record_tables' AND entity_id = p_id::text AND project_id IS NOT NULL
      ORDER BY seq DESC LIMIT 1))
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_node_library(p_id uuid) RETURNS uuid LANGUAGE sql AS $fn$
  SELECT coalesce(
    (SELECT docs_library_id FROM knowledge_nodes WHERE id = p_id),
    (SELECT docs_library_id FROM sync_change_log
      WHERE entity_table = 'knowledge_nodes' AND entity_id = p_id::text AND docs_library_id IS NOT NULL
      ORDER BY seq DESC LIMIT 1))
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_supertag_library(p_id uuid) RETURNS uuid LANGUAGE sql AS $fn$
  SELECT coalesce(
    (SELECT docs_library_id FROM knowledge_supertags WHERE id = p_id),
    (SELECT docs_library_id FROM sync_change_log
      WHERE entity_table = 'knowledge_supertags' AND entity_id = p_id::text AND docs_library_id IS NOT NULL
      ORDER BY seq DESC LIMIT 1))
$fn$;

-- 会話の閲覧者: 所有者（user_id は text。UUID形式のものだけ）と joined な user 参加者。
CREATE OR REPLACE FUNCTION sync_v3_session_viewers(p_session uuid, p_owner text) RETURNS SETOF uuid
LANGUAGE sql AS $fn$
  SELECT p_owner::uuid WHERE p_owner ~ {UUID_RE}
  UNION
  SELECT participant_id::uuid FROM conversation_participants
   WHERE session_id = p_session AND participant_type = 'user'
     AND status = 'joined' AND participant_id ~ {UUID_RE}
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_emit_session(p_session uuid, p_owner text, p_op char) RETURNS void
LANGUAGE plpgsql AS $fn$
DECLARE v uuid;
BEGIN
  FOR v IN SELECT sync_v3_session_viewers(p_session, p_owner) LOOP
    PERFORM sync_v3_emit('conversation_sessions', p_session::text, p_op, v, NULL, NULL, NULL);
  END LOOP;
END
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_emit_task(p_task uuid) RETURNS void LANGUAGE sql AS $fn$
  SELECT sync_v3_emit('tasks', p_task::text, 'U', NULL, sync_v3_task_project(p_task), NULL, NULL)
   WHERE sync_v3_task_project(p_task) IS NOT NULL
$fn$;
"""


def _row_function(spec: _Spec) -> str:
    def scope(rec: str) -> str:
        values = [spec.owner, spec.project, spec.library, spec.space]
        return ", ".join(value.format(r=rec) for value in values)

    eid_new = spec.entity_id.format(r="NEW")
    eid_old = spec.entity_id.format(r="OLD")
    return f"""
CREATE OR REPLACE FUNCTION sync_v3_log_{spec.table}() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM sync_v3_emit('{spec.table}', {eid_old}, 'D', {scope("OLD")});
    RETURN OLD;
  END IF;
  PERFORM sync_v3_emit('{spec.table}', {eid_new}, 'U', {scope("NEW")});
  IF TG_OP = 'UPDATE' THEN
    IF {eid_old} IS DISTINCT FROM {eid_new} THEN
      PERFORM sync_v3_emit('{spec.table}', {eid_old}, 'D', {scope("OLD")});
    ELSIF ROW({scope("OLD")}) IS DISTINCT FROM ROW({scope("NEW")}) THEN
      PERFORM sync_v3_emit('{spec.table}', {eid_old}, 'U', {scope("OLD")});
    END IF;
  END IF;
  RETURN NEW;
END
$fn$;
"""


_CONVERSATION_SQL = """
-- 会話の可視性。
-- I-C: ユーザーUにとって会話の可視性が変わり得る変更では、U宛てに session と、その session の全
--      conversation_messages の記録を同じトランザクションで残す。
-- I-J: 並行する変更どうしでも、上の記録に欠落を生じない。
-- ロックの対応: message 側は session 行を FOR SHARE、閲覧者が変わる側（参加者の変更）は session 行を
--      FOR NO KEY UPDATE、session 自体の UPDATE / DELETE は自身の行ロックで直列化する。
--      ロックを取った後の読取は別の文（READ COMMITTED の新しいスナップショット）で行う。
CREATE OR REPLACE FUNCTION sync_v3_require_read_committed() RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'sync_v3: conversation writes require READ COMMITTED';
  END IF;
END
$fn$;

-- U宛てに session の全 message を1文で記録する。
CREATE OR REPLACE FUNCTION sync_v3_emit_session_messages(p_session uuid, p_user uuid, p_op char)
RETURNS void LANGUAGE sql AS $fn$
  INSERT INTO sync_change_log (entity_table, entity_id, op, owner_user_id)
  SELECT 'conversation_messages', m.id::text, p_op, p_user
    FROM conversation_messages m WHERE m.session_id = p_session
$fn$;

-- 参加者行の変更 1件分（user 参加者のみ）。session が無ければ何もしない。
CREATE OR REPLACE FUNCTION sync_v3_emit_participant(p_session uuid, p_type text, p_id text)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE u uuid;
BEGIN
  IF p_type IS DISTINCT FROM 'user' OR p_id IS NULL OR p_id !~ __UUID_RE__ THEN
    RETURN;
  END IF;
  u := p_id::uuid;
  PERFORM 1 FROM conversation_sessions WHERE id = p_session FOR NO KEY UPDATE;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  PERFORM sync_v3_emit('conversation_sessions', p_session::text, 'U', u, NULL, NULL, NULL);
  PERFORM sync_v3_emit_session_messages(p_session, u, 'U');
END
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_log_conversation_sessions() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE v uuid;
BEGIN
  PERFORM sync_v3_require_read_committed();
  IF TG_OP = 'DELETE' THEN
    FOR v IN SELECT sync_v3_session_viewers(OLD.id, OLD.user_id) LOOP
      PERFORM sync_v3_emit('conversation_sessions', OLD.id::text, 'D', v, NULL, NULL, NULL);
      PERFORM sync_v3_emit_session_messages(OLD.id, v, 'D');
    END LOOP;
    RETURN OLD;
  END IF;
  PERFORM sync_v3_emit_session(NEW.id, NEW.user_id, 'U');
  IF TG_OP = 'UPDATE' AND OLD.user_id IS DISTINCT FROM NEW.user_id THEN
    PERFORM sync_v3_emit_session(NEW.id, OLD.user_id, 'U');
  END IF;
  IF TG_OP = 'UPDATE'
     AND (OLD.user_id IS DISTINCT FROM NEW.user_id OR OLD.project_id IS DISTINCT FROM NEW.project_id) THEN
    FOR v IN
      SELECT sync_v3_session_viewers(NEW.id, NEW.user_id)
      UNION
      SELECT OLD.user_id::uuid WHERE OLD.user_id ~ __UUID_RE__
    LOOP
      PERFORM sync_v3_emit_session_messages(NEW.id, v, 'U');
    END LOOP;
  END IF;
  RETURN NEW;
END
$fn$;

CREATE OR REPLACE FUNCTION sync_v3_log_conversation_messages() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
  rec record;
  v uuid;
  sess_owner text;
  op char;
BEGIN
  PERFORM sync_v3_require_read_committed();
  IF TG_OP = 'DELETE' THEN
    rec := OLD; op := 'D';
  ELSE
    rec := NEW; op := 'U';
  END IF;
  PERFORM 1 FROM conversation_sessions WHERE id = rec.session_id FOR SHARE;
  IF NOT FOUND THEN
    RETURN rec;
  END IF;
  SELECT user_id INTO sess_owner FROM conversation_sessions WHERE id = rec.session_id;
  FOR v IN SELECT sync_v3_session_viewers(rec.session_id, sess_owner) LOOP
    PERFORM sync_v3_emit('conversation_messages', rec.id::text, op, v, NULL, NULL, NULL);
  END LOOP;
  RETURN rec;
END
$fn$;

-- 依存: 参加者の変更（参加・状態変更・脱退・削除）は、その参加者宛てに session と全 message を記録する。
CREATE OR REPLACE FUNCTION sync_v3_dep_participants() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  PERFORM sync_v3_require_read_committed();
  IF TG_OP <> 'INSERT' THEN
    PERFORM sync_v3_emit_participant(OLD.session_id, OLD.participant_type, OLD.participant_id);
  END IF;
  IF TG_OP <> 'DELETE' AND NOT (TG_OP = 'UPDATE'
     AND OLD.session_id = NEW.session_id AND OLD.participant_type = NEW.participant_type
     AND OLD.participant_id = NEW.participant_id) THEN
    PERFORM sync_v3_emit_participant(NEW.session_id, NEW.participant_type, NEW.participant_id);
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$fn$;
""".replace("__UUID_RE__", UUID_RE)

_INFO_NODE_PREDICATE = "n.system_key LIKE 'project_information:%'"

_DEPENDENCY_SQL = f"""
-- 依存: Project名/状態/所有者 → 案件情報node（serialize_docs_node_for_sync の canonical_title・lifecycle）
CREATE OR REPLACE FUNCTION sync_v3_dep_projects() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  PERFORM sync_v3_emit('knowledge_nodes', n.id::text, 'U', NULL, NULL, n.docs_library_id, NULL)
    FROM knowledge_nodes n
   WHERE n.id IN (NEW.knowledge_node_id, OLD.knowledge_node_id)
      OR (n.project_id = NEW.id AND {_INFO_NODE_PREDICATE});
  RETURN NEW;
END
$fn$;

-- 依存: 案件情報nodeのタグ検証（project_info タグの付け外し）
CREATE OR REPLACE FUNCTION sync_v3_dep_node_supertags() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE rec record;
BEGIN
  IF TG_OP = 'DELETE' THEN rec := OLD; ELSE rec := NEW; END IF;
  PERFORM sync_v3_emit('knowledge_nodes', n.id::text, 'U', NULL, NULL, n.docs_library_id, NULL)
    FROM knowledge_nodes n WHERE n.id = rec.node_id AND {_INFO_NODE_PREDICATE};
  RETURN rec;
END
$fn$;

-- 依存: ハブnode（project_information_root）の変更 → 配下の案件情報node
CREATE OR REPLACE FUNCTION sync_v3_dep_hub_nodes() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE rec record;
BEGIN
  IF TG_OP = 'DELETE' THEN rec := OLD; ELSE rec := NEW; END IF;
  IF rec.system_key IS DISTINCT FROM 'project_information_root'
     AND (TG_OP <> 'UPDATE' OR OLD.system_key IS DISTINCT FROM 'project_information_root') THEN
    RETURN rec;
  END IF;
  PERFORM sync_v3_emit('knowledge_nodes', n.id::text, 'U', NULL, NULL, n.docs_library_id, NULL)
    FROM knowledge_nodes n WHERE n.parent_id = rec.id AND {_INFO_NODE_PREDICATE};
  RETURN rec;
END
$fn$;

-- 依存: project_info supertag の変更/削除 → そのタグが付いた案件情報node
CREATE OR REPLACE FUNCTION sync_v3_dep_supertags() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE rec record;
BEGIN
  IF TG_OP = 'DELETE' THEN rec := OLD; ELSE rec := NEW; END IF;
  IF rec.system_key IS DISTINCT FROM 'project_info'
     AND (TG_OP <> 'UPDATE' OR OLD.system_key IS DISTINCT FROM 'project_info') THEN
    RETURN rec;
  END IF;
  PERFORM sync_v3_emit('knowledge_nodes', n.id::text, 'U', NULL, NULL, n.docs_library_id, NULL)
    FROM knowledge_nodes n
    JOIN knowledge_node_supertags nst ON nst.node_id = n.id
   WHERE nst.supertag_id = rec.id AND {_INFO_NODE_PREDICATE};
  RETURN rec;
END
$fn$;

-- 依存: Library種別/所有者の変更 → Library内の案件情報node（library_is_owner_personal）
CREATE OR REPLACE FUNCTION sync_v3_dep_libraries() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  PERFORM sync_v3_emit('knowledge_nodes', n.id::text, 'U', NULL, NULL, n.docs_library_id, NULL)
    FROM knowledge_nodes n WHERE n.docs_library_id = NEW.id AND {_INFO_NODE_PREDICATE};
  RETURN NEW;
END
$fn$;

-- 依存: task の担当者・繰り返しルール・タグ付け → task
CREATE OR REPLACE FUNCTION sync_v3_dep_task_child() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE rec record;
BEGIN
  IF TG_OP = 'DELETE' THEN rec := OLD; ELSE rec := NEW; END IF;
  PERFORM sync_v3_emit_task(rec.task_id);
  IF TG_OP = 'UPDATE' AND OLD.task_id IS DISTINCT FROM NEW.task_id THEN
    PERFORM sync_v3_emit_task(OLD.task_id);
  END IF;
  RETURN rec;
END
$fn$;

-- 依存: タグ名/色の変更 → そのタグが付いたtask
CREATE OR REPLACE FUNCTION sync_v3_dep_tags() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE rec record;
BEGIN
  IF TG_OP = 'DELETE' THEN rec := OLD; ELSE rec := NEW; END IF;
  PERFORM sync_v3_emit_task(tt.task_id) FROM task_tags tt WHERE tt.tag_id = rec.id;
  RETURN rec;
END
$fn$;
"""

# (table, trigger name, function, timing/event clause)。UPDATE は内容が変わった時だけ発火する。
_CHANGED = "WHEN (to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW))"
_DEPENDENCY_TRIGGERS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("conversation_participants", "sync_v3_dep_participants", "sync_v3_dep_participants",
     ("INSERT", "UPDATE", "DELETE")),
    ("projects", "sync_v3_dep_projects", "sync_v3_dep_projects", ("UPDATE",)),
    ("knowledge_node_supertags", "sync_v3_dep_node_supertags", "sync_v3_dep_node_supertags",
     ("INSERT", "UPDATE", "DELETE")),
    ("knowledge_nodes", "sync_v3_dep_hub_nodes", "sync_v3_dep_hub_nodes", ("UPDATE", "DELETE")),
    ("knowledge_supertags", "sync_v3_dep_supertags", "sync_v3_dep_supertags",
     ("INSERT", "UPDATE", "DELETE")),
    ("docs_libraries", "sync_v3_dep_libraries", "sync_v3_dep_libraries", ("UPDATE",)),
    ("task_assignees", "sync_v3_dep_task_child", "sync_v3_dep_task_child",
     ("INSERT", "UPDATE", "DELETE")),
    ("task_recurrence_rules", "sync_v3_dep_task_child", "sync_v3_dep_task_child",
     ("INSERT", "UPDATE", "DELETE")),
    ("task_tags", "sync_v3_dep_task_child", "sync_v3_dep_task_child",
     ("INSERT", "UPDATE", "DELETE")),
    ("tags", "sync_v3_dep_tags", "sync_v3_dep_tags", ("UPDATE", "DELETE")),
)

_EXTRA_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_sync_change_log_entity "
    "ON sync_change_log (entity_table, entity_id, seq DESC);\n"
)

_ROW_TRIGGER_TABLES: tuple[str, ...] = tuple(spec.table for spec in _SPECS) + _CONVERSATION_TABLES


_PARTICIPANTS_CHANGED = (
    "WHEN (OLD.session_id IS DISTINCT FROM NEW.session_id "
    "OR OLD.participant_type IS DISTINCT FROM NEW.participant_type "
    "OR OLD.participant_id IS DISTINCT FROM NEW.participant_id "
    "OR OLD.status IS DISTINCT FROM NEW.status)"
)


def _create_triggers(table: str, name: str, function: str, events: tuple[str, ...]) -> str:
    changed = _PARTICIPANTS_CHANGED if table == "conversation_participants" else _CHANGED
    out = []
    for event in events:
        if event == "INSERT":
            out.append(f"DROP TRIGGER IF EXISTS {name}_ins ON {table};")
            out.append(
                f"CREATE TRIGGER {name}_ins AFTER INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {function}();"
            )
        elif event == "UPDATE":
            out.append(f"DROP TRIGGER IF EXISTS {name}_upd ON {table};")
            out.append(
                f"CREATE TRIGGER {name}_upd AFTER UPDATE ON {table} "
                f"FOR EACH ROW {changed} EXECUTE FUNCTION {function}();"
            )
        else:
            out.append(f"DROP TRIGGER IF EXISTS {name}_del ON {table};")
            out.append(
                f"CREATE TRIGGER {name}_del BEFORE DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {function}();"
            )
    return "\n".join(out) + "\n"


def _trigger_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for table in _ROW_TRIGGER_TABLES:
        pairs.extend((table, f"sync_v3_log_{table}_{suffix}") for suffix in ("ins", "upd", "del"))
    for table, name, _function, events in _DEPENDENCY_TRIGGERS:
        suffixes = {"INSERT": "ins", "UPDATE": "upd", "DELETE": "del"}
        pairs.extend((table, f"{name}_{suffixes[event]}") for event in events)
    return pairs


def _function_names() -> list[str]:
    names = [f"sync_v3_log_{table}" for table in _ROW_TRIGGER_TABLES]
    names += sorted({function for _t, _n, function, _e in _DEPENDENCY_TRIGGERS})
    return names


_HELPER_SIGNATURES: tuple[str, ...] = (
    "sync_v3_emit_participant(uuid, text, text)",
    "sync_v3_emit_session_messages(uuid, uuid, char)",
    "sync_v3_require_read_committed()",
    "sync_v3_emit_task(uuid)",
    "sync_v3_emit_session(uuid, text, char)",
    "sync_v3_session_viewers(uuid, text)",
    "sync_v3_supertag_library(uuid)",
    "sync_v3_node_library(uuid)",
    "sync_v3_record_table_project(uuid)",
    "sync_v3_task_project(uuid)",
    "sync_v3_emit(text, text, char, uuid, uuid, uuid, uuid)",
)


def build_install_sql() -> str:
    parts = [
        contract.SYNC_CHANGE_LOG_DDL,
        _EXTRA_INDEX_SQL,
        contract.SYNC_COMMIT_ORDER_SQL,
        _HELPERS_SQL,
    ]
    parts.extend(_row_function(spec) for spec in _SPECS)
    parts.append(_CONVERSATION_SQL)
    parts.append(_DEPENDENCY_SQL)
    for table in _ROW_TRIGGER_TABLES:
        parts.append(
            _create_triggers(table, f"sync_v3_log_{table}", f"sync_v3_log_{table}",
                             ("INSERT", "UPDATE", "DELETE"))
        )
    for table, name, function, events in _DEPENDENCY_TRIGGERS:
        parts.append(_create_triggers(table, name, function, events))
    return "\n".join(parts)


def build_uninstall_sql() -> str:
    lines = ["DROP TRIGGER IF EXISTS zz_sync_commit_order ON sync_change_log;"]
    for table, trigger in reversed(_trigger_pairs()):
        lines.append(f"DROP TRIGGER IF EXISTS {trigger} ON {table};")
    lines.extend(f"DROP FUNCTION IF EXISTS {name}();" for name in reversed(_function_names()))
    lines.extend(f"DROP FUNCTION IF EXISTS {sig};" for sig in _HELPER_SIGNATURES)
    lines.extend(
        [
            "DROP FUNCTION IF EXISTS sync_assign_commit_seq();",
            "DROP TABLE IF EXISTS sync_change_log_state;",
            "DROP TABLE IF EXISTS sync_entity_versions;",
            "DROP TABLE IF EXISTS sync_change_log;",
            "DROP SEQUENCE IF EXISTS sync_commit_seq;",
        ]
    )
    return "\n".join(lines) + "\n"
