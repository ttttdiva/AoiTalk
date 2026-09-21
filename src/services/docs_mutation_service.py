"""Compile bounded, ID-anchored Agent changesets into canonical Docs writes."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, text

from ..memory.models import (
    ContextMemory,
    DocsLibrary,
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeNodePlacement,
    KnowledgeNodeShare,
    KnowledgeNodeSupertag,
    KnowledgeRevision,
    KnowledgeSearchIndex,
    KnowledgeSupertag,
    KnowledgeSupertagField,
    Project,
    ProjectKnowledgeRef,
    ProjectMember,
    Task,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskRecurrenceScheduleSegment,
    TimeEntry,
    User,
)
from ..memory.models.notifications import NotificationDelivery
from ..memory.models.docs_agent import (
    DocsAuthorityState,
    DocsLibraryRevision,
    DocsMutationReceipt,
    DocsReadLease,
)
from .docs_acl import docs_readable_node_predicate
from .docs_consistency import (
    DocsConflict,
    execute_nowait,
    is_postgres,
    lock_docs_writes,
    require_active_actor,
    revision,
)
from .docs_graph_service import SYSTEM_TASK_TAG, TASK_FIELD_TO_TASK_UPDATE, DocsGraphService
from .managed_docs_policy import assert_managed_docs_tree_mutation_allowed

OP_KEYS = {
    "update": {"op", "node_id", "title", "description", "content"},
    "create": {"op", "ref", "parent_id", "title", "description", "content", "block_type"},
    "set_fields": {"op", "node_id", "values"},
    "add_tag": {"op", "node_id", "tag_id"},
    "remove_tag": {"op", "node_id", "tag_id"},
    "move": {"op", "node_id", "parent_id", "leave_reference"},
    "archive": {"op", "node_id"},
}

_REFERENCE_TOKEN_RE = re.compile(
    r"\[\[node:([0-9a-fA-F-]{36})(?:\|[^\]]*)?\]\]|@docs:([0-9a-fA-F-]{36})"
)


def parse_changeset(payload):
    if (not isinstance(payload, dict) or set(payload) != {"intent", "operations"}
            or payload["intent"] not in {"revise_section", "upsert_record", "reorganize_subtree"}):
        raise ValueError("Specify intent (revise_section/upsert_record/reorganize_subtree) and operations")
    operations = payload["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("A Docs changeset must contain 1 to 100 operations")
    if len(json.dumps(payload, ensure_ascii=False)) > 1_000_000:
        raise ValueError("Docs changeset exceeds the one-million-character bound")
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") not in OP_KEYS:
            raise ValueError("Unsupported Docs operation")
        kind = operation["op"]
        if set(operation) - OP_KEYS[kind]:
            raise ValueError("Unknown Docs operation properties")
        required = {
            "create": {"parent_id", "ref", "title"}, "move": {"node_id", "parent_id"},
            "set_fields": {"node_id", "values"}, "add_tag": {"node_id", "tag_id"},
            "remove_tag": {"node_id", "tag_id"},
        }.get(kind, {"node_id"})
        if not required <= operation.keys():
            raise ValueError("Docs operation is missing required properties")
        for key in ("title", "description", "content", "node_id", "parent_id", "ref", "tag_id"):
            if key in operation and not isinstance(operation[key], str):
                raise ValueError(f"{key} must be a string")
        if "title" in operation and (len(operation["title"]) > 20_000 or "\n" in operation["title"] or "\r" in operation["title"]):
            raise ValueError("Docs titles must be one line of at most 20,000 characters")
        if "description" in operation and len(operation["description"]) > 200_000:
            raise ValueError("Docs description is too long")
        if kind == "set_fields":
            if not isinstance(operation["values"], dict):
                raise ValueError("values must map stable Field UUIDs to values")
            for key in operation["values"]:
                UUID(key)
        if "tag_id" in operation:
            UUID(operation["tag_id"])
        if "leave_reference" in operation and type(operation["leave_reference"]) is not bool:
            raise ValueError("leave_reference must be boolean")
        if kind == "create" and operation.get("block_type", "paragraph") not in {"paragraph", "markdown", "code"}:
            raise ValueError("Unsupported created block type")
        if kind == "create" and "content" in operation and operation.get("block_type") not in {"markdown", "code"}:
            raise ValueError("Independent content requires a markdown/code typed block")
    return operations


class DocsMutationService:
    def __init__(self, session):
        self.session = session
        self.docs = DocsGraphService(session)

    async def _authorized_node(self, node_id, actor_id, required="write"):
        node = (await self.session.execute(select(KnowledgeNode).where(
            KnowledgeNode.id == node_id, KnowledgeNode.archived_at.is_(None),
        ).execution_options(populate_existing=True))).scalar_one_or_none()
        if node is None:
            return None
        owner = await self.session.scalar(select(DocsLibrary.owner_user_id).where(DocsLibrary.id == node.docs_library_id))
        readable = await self.session.scalar(select(KnowledgeNode.id).where(
            KnowledgeNode.id == node.id,
            docs_readable_node_predicate(KnowledgeNode, docs_library_id=node.docs_library_id,
                user_id=actor_id, library_owner_id=owner, required=required),
        ))
        return node if readable is not None else None

    async def _table_available(self, table_name: str) -> bool:
        """Return whether an optional side-effect table exists in this DB.

        The Docs protocol tests intentionally build a small schema, while a
        production database has the full Task/Project/Memory surface.  The
        prelock boundary must work in both environments without turning an
        absent, unrelated table into a mutation failure.
        """
        if not is_postgres(self.session):
            return False
        return bool(await self.session.scalar(
            text("SELECT to_regclass(current_schema() || '.' || :table_name) IS NOT NULL"),
            {"table_name": table_name},
        ))

    async def _closure_ids(self, *, library_id: UUID, root_id: UUID) -> set[UUID]:
        result = await self.session.execute(text("""
            WITH RECURSIVE docs_mutation_closure AS (
                SELECT id, ARRAY[id]::uuid[] AS visited_path, 0 AS depth
                FROM knowledge_nodes
                WHERE id = :root_id AND docs_library_id = :library_id
                UNION ALL
                SELECT child.id, parent.visited_path || ARRAY[child.id]::uuid[], parent.depth + 1
                FROM knowledge_nodes child
                JOIN docs_mutation_closure parent ON parent.id = child.parent_id
                WHERE parent.depth < 512
                  AND child.docs_library_id = :library_id
                  AND NOT child.id = ANY(parent.visited_path)
            )
            SELECT DISTINCT id FROM docs_mutation_closure ORDER BY id
        """), {"root_id": root_id, "library_id": library_id})
        return {row[0] for row in result}

    async def _lock_side_effect_rows(
        self,
        *,
        model,
        whereclause,
        order_columns,
    ) -> list:
        """Lock an existing domain row set using Agent-only ``NOWAIT``."""
        statement = select(model).where(whereclause)
        if order_columns:
            statement = statement.order_by(*order_columns)
        statement = statement.with_for_update(nowait=True)
        result = await execute_nowait(self.session, statement)
        return list(result.scalars().all())

    async def _plan_ordered_schema_effects(
        self,
        *,
        actor_id: UUID,
        root,
        operations: list[dict],
        nodes_by_id: dict,
        field_ids: set[UUID],
        tag_ids: set[UUID],
        reference_ids: set[UUID],
        requested_project_ids: set[UUID],
        close_node_ids: set[UUID],
        task_bind_project_ids: dict[str, UUID],
        task_write_project_ids: set[UUID],
    ) -> None:
        """Dry-plan tag/Field/Task effects in changeset order without DML."""
        simulated: dict[str, dict] = {}
        inbox_project = None

        def canon(value) -> str:
            text = str(value)
            if text.startswith("local:"):
                return text
            return str(UUID(text))

        def seed_existing(node_key: str) -> None:
            if node_key in simulated or node_key.startswith("local:"):
                return
            node = nodes_by_id.get(UUID(node_key))
            if node is None:
                return
            simulated[node_key] = {
                "project_id": getattr(node, "project_id", None),
                "direct_tags": set(),
                "live_task": False,
                "task_project_id": None,
            }

        for operation in operations:
            for key in ("node_id", "parent_id"):
                value = operation.get(key)
                if value and not str(value).startswith("local:"):
                    seed_existing(canon(value))

        existing_keys = [UUID(key) for key in simulated]
        if existing_keys:
            tag_rows = await self.session.execute(
                select(
                    KnowledgeNodeSupertag.node_id,
                    KnowledgeNodeSupertag.supertag_id,
                ).where(
                    KnowledgeNodeSupertag.node_id.in_(sorted(existing_keys, key=str))
                )
            )
            for node_id, supertag_id in tag_rows.all():
                state = simulated.get(str(node_id))
                if state is not None:
                    state["direct_tags"].add(supertag_id)
            live_tasks = await self.session.execute(
                select(Task.knowledge_node_id, Task.project_id).where(
                    Task.knowledge_node_id.in_(sorted(existing_keys, key=str)),
                    Task.deleted_at.is_(None),
                )
            )
            for node_id, task_project_id in live_tasks.all():
                state = simulated.get(str(node_id))
                if state is not None:
                    state["live_task"] = True
                    state["task_project_id"] = task_project_id

        op_tag_ids = {
            UUID(str(operation["tag_id"]))
            for operation in operations
            if operation["op"] in {"add_tag", "remove_tag"}
        }
        tags_by_id: dict[UUID, KnowledgeSupertag] = {}
        if op_tag_ids:
            tag_result = await self.session.execute(
                select(KnowledgeSupertag).where(
                    KnowledgeSupertag.id.in_(sorted(op_tag_ids, key=str))
                )
            )
            tags_by_id = {tag.id: tag for tag in tag_result.scalars().all()}

        def resolve_tag(tag_id: UUID, raw: str) -> KnowledgeSupertag:
            tag = tags_by_id.get(tag_id)
            if tag is None or tag.docs_library_id != root.docs_library_id:
                raise ValueError(f"supertag not found: {raw}")
            return tag

        async def plan_task_create(state: dict) -> UUID:
            nonlocal inbox_project
            if state["project_id"] is not None:
                bind_id = UUID(str(state["project_id"]))
                requested_project_ids.add(bind_id)
                return bind_id
            if inbox_project is None:
                inbox_project = await self.docs.resolve_existing_inbox_project_for_task_bind(
                    actor_id
                )
            requested_project_ids.add(inbox_project.id)
            return inbox_project.id

        for operation in operations:
            kind = operation["op"]
            if kind == "create":
                parent_key = canon(operation["parent_id"])
                parent_state = simulated.get(parent_key)
                parent_project = None
                if parent_state is not None:
                    parent_project = parent_state["project_id"]
                elif not parent_key.startswith("local:"):
                    parent = nodes_by_id.get(UUID(parent_key))
                    if parent is not None:
                        parent_project = getattr(parent, "project_id", None)
                simulated[str(operation["ref"])] = {
                    "project_id": parent_project,
                    "direct_tags": set(),
                    "live_task": False,
                    "task_project_id": None,
                }
                continue

            node_key = canon(operation.get("node_id") or "")
            state = simulated.get(node_key)
            if kind in {"add_tag", "remove_tag"}:
                tag = resolve_tag(UUID(str(operation["tag_id"])), operation["tag_id"])
                tag_ids.add(tag.id)
                if state is None:
                    continue
                present = tag.id in state["direct_tags"]
                if kind == "add_tag":
                    if not present:
                        state["direct_tags"].add(tag.id)
                        if tag.system_key == SYSTEM_TASK_TAG and not state["live_task"]:
                            bind_id = await plan_task_create(state)
                            task_bind_project_ids[node_key] = bind_id
                            state["live_task"] = True
                            state["task_project_id"] = bind_id
                            task_write_project_ids.add(bind_id)
                elif present:
                    state["direct_tags"].discard(tag.id)
                    if tag.system_key == SYSTEM_TASK_TAG:
                        current = state.get("task_project_id")
                        if current is not None:
                            task_write_project_ids.add(UUID(str(current)))
                        state["live_task"] = False
                        state["task_project_id"] = None
                continue

            if kind != "set_fields" or state is None:
                continue
            effective_tags, definitions = await self.docs.resolve_schema_for_tag_ids(
                docs_library_id=root.docs_library_id,
                tag_ids=state["direct_tags"],
            )
            tag_ids.update(effective_tags)
            tag_ids.update(state["direct_tags"])
            seen_fields: dict[UUID, KnowledgeField] = {}
            for field in definitions.values():
                seen_fields[field.id] = field
            field_ids.update(seen_fields.keys())
            for field in seen_fields.values():
                if field.supertag_id is not None:
                    tag_ids.add(field.supertag_id)
            for field_ref, value in operation["values"].items():
                field = definitions.get(str(field_ref).casefold())
                if field is None:
                    field = definitions.get(str(field_ref))
                if field is None:
                    raise ValueError(f"field not found on node tags: {field_ref}")
                field_ids.add(field.id)
                if field.supertag_id is not None:
                    tag_ids.add(field.supertag_id)
                if field.system_key in TASK_FIELD_TO_TASK_UPDATE:
                    if not state["live_task"]:
                        raise ValueError("node is not bound to a task")
                    current = state.get("task_project_id")
                    if current is not None:
                        task_write_project_ids.add(UUID(str(current)))
                    if field.system_key == "task_project":
                        if value not in (None, ""):
                            target = UUID(str(value))
                            requested_project_ids.add(target)
                            task_write_project_ids.add(target)
                            state["task_project_id"] = target
                        continue
                if field.system_key == "task_status" and str(value or "").casefold() in {
                    "closed",
                    "done",
                }:
                    if not node_key.startswith("local:"):
                        close_node_ids.add(UUID(node_key))
                if field.field_type == "reference" and value not in (None, ""):
                    reference_ids.add(UUID(str(value)))

    async def _prelock_affected_rows(
        self,
        *,
        actor_id: UUID,
        root,
        operations: list[dict],
        allowed: set[UUID],
    ) -> dict[str, object]:
        """Prelock every existing row an Agent changes or causes to change.

        This is intentionally a planning pass: it performs no canonical DML.
        The lock order is stable (library, Project/User/Task, Docs rows,
        trigger-maintained revision/policy state), and every row lock is
        NOWAIT.
        If an ordinary writer owns one of these rows, only this Agent
        transaction receives ``DocsConflict``; the ordinary transaction is
        never routed through the old global trigger guard.
        """
        node_ids: set[UUID] = {UUID(str(root.id))}
        structural_roots: set[UUID] = set()
        field_ids: set[UUID] = set()
        tag_ids: set[UUID] = set()
        reference_ids: set[UUID] = set()
        requested_project_ids: set[UUID] = set()
        close_node_ids: set[UUID] = set()
        task_bind_project_ids: dict[str, UUID] = {}
        task_write_project_ids: set[UUID] = set()
        edge_source_ids: set[UUID] = set()
        mutation_ids: set[UUID] = set()

        # Existing UUID references are already constrained to the lease by the
        # caller.  Local references are created in this same transaction and
        # therefore have no pre-existing row to lock.
        for operation in operations:
            kind = operation["op"]
            for key in ("node_id", "parent_id"):
                value = operation.get(key)
                if not value or str(value).startswith("local:"):
                    continue
                identifier = UUID(str(value))
                node_ids.add(identifier)
                if key == "node_id":
                    mutation_ids.add(identifier)
                    if kind in {"update", "move", "archive"}:
                        edge_source_ids.add(identifier)
            if kind in {"move", "archive"} and operation.get("node_id"):
                value = str(operation["node_id"])
                if not value.startswith("local:"):
                    structural_roots.add(UUID(value))
            if kind in {"add_tag", "remove_tag"}:
                tag_ids.add(UUID(str(operation["tag_id"])))

        # Include the complete closure for every structural operation before
        # any graph mutation can move/archive it.  Parent/child FK key-share
        # locks then prevent a concurrent insert/reparent from escaping the
        # validated closure.
        for structural_root in sorted(structural_roots, key=str):
            closure_ids = await self._closure_ids(
                library_id=root.docs_library_id,
                root_id=structural_root,
            )
            if not closure_ids <= allowed:
                raise PermissionError("Structural mutation includes nodes outside the edit view")
            node_ids.update(closure_ids)
            edge_source_ids.update(closure_ids)

        node_result = await self.session.execute(
            select(KnowledgeNode)
            .where(KnowledgeNode.id.in_(sorted(node_ids, key=str)))
            .order_by(KnowledgeNode.id)
        ) if node_ids else None
        nodes = list(node_result.scalars().all()) if node_result is not None else []
        nodes_by_id = {node.id: node for node in nodes}

        # A move with ``leave_reference`` mutates the old parent/placement
        # boundary as well as the moved subtree.  The old parent is part of an
        # edit lease's authorized closure and must be fenced before DML.
        old_parent_ids = {
            node.parent_id
            for node in nodes
            if node.id in structural_roots and node.parent_id is not None
        }
        if old_parent_ids:
            if not old_parent_ids <= allowed:
                raise PermissionError("Move source parent is outside the edit view")
            node_ids.update(old_parent_ids)
            node_result = await self.session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id.in_(sorted(node_ids, key=str)))
                .order_by(KnowledgeNode.id)
            )
            nodes = list(node_result.scalars().all())
            nodes_by_id = {node.id: node for node in nodes}

        # Walk the ordered changeset against an in-memory tag/Task state so an
        # earlier add_tag/remove_tag cannot introduce a later Field or domain
        # row outside this lock set.  Local creates participate with empty
        # tags and inherited parent/project context.  No canonical DML.
        await self._plan_ordered_schema_effects(
            actor_id=actor_id,
            root=root,
            operations=operations,
            nodes_by_id=nodes_by_id,
            field_ids=field_ids,
            tag_ids=tag_ids,
            reference_ids=reference_ids,
            requested_project_ids=requested_project_ids,
            close_node_ids=close_node_ids,
            task_bind_project_ids=task_bind_project_ids,
            task_write_project_ids=task_write_project_ids,
        )
        # ``record_node_change`` rebuilds inline reference edges from the
        # node's title/body text.  Discover references for every operation
        # that can rebuild edges, including move/archive closures and updates
        # of a local node created earlier in this same changeset.
        for source_id in sorted(edge_source_ids, key=str):
            current = nodes_by_id.get(source_id)
            if current is not None:
                for text_value in (current.title or "", current.body_text or ""):
                    for match in _REFERENCE_TOKEN_RE.finditer(str(text_value)):
                        reference_ids.add(UUID(match.group(1) or match.group(2)))
        for operation in operations:
            text_values: list[str] = []
            if operation["op"] == "create":
                text_values.append(str(operation.get("title") or ""))
            elif operation["op"] == "update":
                node_ref = str(operation.get("node_id") or "")
                if node_ref.startswith("local:"):
                    if "title" in operation:
                        text_values.append(str(operation.get("title") or ""))
                else:
                    current = nodes_by_id.get(UUID(node_ref))
                    if current is not None:
                        text_values.extend((str(current.title or ""), str(current.body_text or "")))
                    if "title" in operation:
                        text_values.append(str(operation.get("title") or ""))
            for text_value in text_values:
                for match in _REFERENCE_TOKEN_RE.finditer(text_value):
                    reference_ids.add(UUID(match.group(1) or match.group(2)))

        # Inline edge targets are not required to be in the edit lease.  They
        # may be readable records outside this bounded edit view.  Authorize
        # and prelock same-library targets; the graph service ignores missing,
        # archived, or foreign-library targets when rebuilding edges.
        if reference_ids:
            target_result = await self.session.execute(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.id.in_(sorted(reference_ids, key=str)),
                    KnowledgeNode.archived_at.is_(None),
                )
                .order_by(KnowledgeNode.id)
            )
            for target in target_result.scalars().all():
                if target.docs_library_id != root.docs_library_id:
                    continue
                if await self._authorized_node(target.id, actor_id, required="read") is None:
                    raise PermissionError("Inline reference target is not readable")
                node_ids.add(target.id)
        if reference_ids:
            node_result = await self.session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id.in_(sorted(node_ids, key=str)))
                .order_by(KnowledgeNode.id)
            )
            nodes = list(node_result.scalars().all())
            nodes_by_id = {node.id: node for node in nodes}

        # Resolve tags used by the leased nodes as well as explicit tag
        # operations.  Field resolution follows inherited/shared tag links;
        # fencing these rows closes the ACL/schema race before revalidation.
        node_tags = await self.session.execute(
            select(KnowledgeNodeSupertag.supertag_id)
            .where(KnowledgeNodeSupertag.node_id.in_(sorted(node_ids, key=str)))
            .order_by(KnowledgeNodeSupertag.supertag_id)
        ) if node_ids else None
        if node_tags is not None:
            tag_ids.update(node_tags.scalars().all())

        # Existing field/tag/relation rows are all potential upsert/delete
        # targets for the changeset.  New rows are protected by the locked
        # parent node/field/tag and their uniqueness constraints.
        project_ids: set[UUID] = {
            node.project_id for node in nodes if getattr(node, "project_id", None) is not None
        }
        project_ids.update(requested_project_ids)
        project_ids.update(task_write_project_ids)
        project_rows = await self.session.execute(
            select(Project)
            .where(or_(
                Project.id.in_(sorted(project_ids, key=str)) if project_ids else text("FALSE"),
                Project.knowledge_node_id.in_(sorted(node_ids, key=str)) if node_ids else text("FALSE"),
            ))
            .order_by(Project.id)
        )
        known_projects = list(project_rows.scalars().all())
        project_ids.update(project.id for project in known_projects)

        task_rows = await self.session.execute(
            select(Task)
            .where(Task.knowledge_node_id.in_(sorted(node_ids, key=str)), Task.deleted_at.is_(None))
            .order_by(Task.id)
        ) if node_ids else None
        bound_tasks = list(task_rows.scalars().all()) if task_rows is not None else []
        task_ids = {task.id for task in bound_tasks}
        project_ids.update(task.project_id for task in bound_tasks if task.project_id is not None)

        # TaskManagementService inspects/locks direct children before a
        # ``task_status=closed`` update.  Discover those rows before acquiring
        # project advisory locks so the Agent takes the same Project → Task
        # order and NOWAIT-fences every row the close path can touch.
        close_task_ids = {
            task.id for task in bound_tasks
            if task.knowledge_node_id in close_node_ids
        }
        if close_task_ids:
            child_tasks_result = await self.session.execute(
                select(Task)
                .where(Task.parent_task_id.in_(sorted(close_task_ids, key=str)), Task.deleted_at.is_(None))
                .order_by(Task.id)
            )
            child_tasks = list(child_tasks_result.scalars().all())
            task_ids.update(task.id for task in child_tasks)
            project_ids.update(task.project_id for task in child_tasks if task.project_id is not None)

        # TaskManagementService takes these same project advisory locks before
        # Project/Task rows.  Keeping that order avoids Agent-vs-domain cycles.
        if is_postgres(self.session) and project_ids:
            from .task_project_invariants import lock_task_project_ids

            await lock_task_project_ids(self.session, sorted(project_ids, key=str))

        # The library row is a canonical parent of every node.  Lock it before
        # child/domain rows so an ordinary library/node writer cannot hold a
        # child row and then wait on the library while the Agent does the
        # opposite.
        await self._lock_side_effect_rows(
            model=DocsLibrary,
            whereclause=DocsLibrary.id == root.docs_library_id,
            order_columns=(DocsLibrary.id,),
        )

        locked_projects = await self._lock_side_effect_rows(
            model=Project,
            whereclause=or_(
                Project.id.in_(sorted(project_ids, key=str)) if project_ids else text("FALSE"),
                Project.knowledge_node_id.in_(sorted(node_ids, key=str)) if node_ids else text("FALSE"),
            ),
            order_columns=(Project.id,),
        )
        project_ids.update(project.id for project in locked_projects)

        # Project ACL and actor/session authority are rechecked by the owning
        # service after these deterministic locks.  The User lock also fences
        # concurrent role/is_active/session-version changes, while a
        # last_login-only update remains harmless to Docs revisions.
        await self._lock_side_effect_rows(
            model=User,
            whereclause=User.id == actor_id,
            order_columns=(User.id,),
        )
        if project_ids:
            await self._lock_side_effect_rows(
                model=ProjectMember,
                whereclause=and_(
                    ProjectMember.project_id.in_(sorted(project_ids, key=str)),
                    ProjectMember.user_id == actor_id,
                ),
                order_columns=(ProjectMember.project_id, ProjectMember.user_id),
            )

        # Lock existing bound Tasks after Projects.  Task service calls made by
        # DocsGraphService then observe these rows as already fenced.  The
        # service remains responsible for inserting activity/occurrence rows;
        # the task row/project lock serializes those inserts with other task
        # writers without broad table locks.
        if task_ids:
            await self._lock_side_effect_rows(
                model=Task,
                whereclause=Task.id.in_(sorted(task_ids, key=str)),
                order_columns=(Task.id,),
            )
            # Docs-bound Task updates can upsert/delete recurrence rules and
            # schedule segments and rematerialize existing occurrences.  Lock
            # those dependency rows in deterministic UUID order before the
            # first Docs/Task DML; ordinary occurrence writers then either run
            # before this Agent (causing a retryable NOWAIT conflict) or after
            # its commit, never racing an in-flight rematerialization.
            await self._lock_side_effect_rows(
                model=TaskRecurrenceRule,
                whereclause=TaskRecurrenceRule.task_id.in_(sorted(task_ids, key=str)),
                order_columns=(TaskRecurrenceRule.id,),
            )
            await self._lock_side_effect_rows(
                model=TaskRecurrenceScheduleSegment,
                whereclause=TaskRecurrenceScheduleSegment.task_id.in_(sorted(task_ids, key=str)),
                order_columns=(TaskRecurrenceScheduleSegment.id,),
            )
            locked_occurrences = await self._lock_side_effect_rows(
                model=TaskOccurrence,
                whereclause=TaskOccurrence.task_id.in_(sorted(task_ids, key=str)),
                order_columns=(TaskOccurrence.id,),
            )
            occurrence_ids = {occurrence.id for occurrence in locked_occurrences}
            for dependency_model in (NotificationDelivery, TimeEntry):
                if not await self._table_available(dependency_model.__tablename__):
                    continue
                await self._lock_side_effect_rows(
                    model=dependency_model,
                    whereclause=or_(
                        dependency_model.task_id.in_(sorted(task_ids, key=str)),
                        dependency_model.occurrence_id.in_(sorted(occurrence_ids, key=str))
                        if occurrence_ids else text("FALSE"),
                    ),
                    order_columns=(dependency_model.id,),
                )

        # Optional project-memory rows are a policy side effect of domain task
        # writes.  Lock only rows attached to the actual affected Project/Task
        # set; installations without the table simply skip this narrow fence.
        if await self._table_available(ContextMemory.__tablename__) and (project_ids or task_ids):
            await self._lock_side_effect_rows(
                model=ContextMemory,
                whereclause=or_(
                    ContextMemory.project_id.in_(sorted(project_ids, key=str)) if project_ids else text("FALSE"),
                    ContextMemory.task_id.in_(sorted(task_ids, key=str)) if task_ids else text("FALSE"),
                ),
                order_columns=(ContextMemory.id,),
            )

        # Finally fence the canonical Docs row set and all existing child rows
        # touched by updates, tags, Fields, references, moves and index/revision
        # maintenance.  Every statement uses NOWAIT and a stable UUID order.
        await self._lock_side_effect_rows(
            model=KnowledgeNode,
            whereclause=KnowledgeNode.id.in_(sorted(node_ids, key=str)),
            order_columns=(KnowledgeNode.id,),
        )
        if field_ids:
            await self._lock_side_effect_rows(
                model=KnowledgeField,
                whereclause=KnowledgeField.id.in_(sorted(field_ids, key=str)),
                order_columns=(KnowledgeField.id,),
            )
        if tag_ids:
            await self._lock_side_effect_rows(
                model=KnowledgeSupertag,
                whereclause=KnowledgeSupertag.id.in_(sorted(tag_ids, key=str)),
                order_columns=(KnowledgeSupertag.id,),
            )
        await self._lock_side_effect_rows(
            model=KnowledgeNodeSupertag,
            whereclause=KnowledgeNodeSupertag.node_id.in_(sorted(node_ids, key=str)),
            order_columns=(KnowledgeNodeSupertag.node_id, KnowledgeNodeSupertag.supertag_id),
        )
        await self._lock_side_effect_rows(
            model=KnowledgeSupertagField,
            whereclause=or_(
                KnowledgeSupertagField.supertag_id.in_(sorted(tag_ids, key=str)) if tag_ids else text("FALSE"),
                KnowledgeSupertagField.field_id.in_(sorted(field_ids, key=str)) if field_ids else text("FALSE"),
            ),
            order_columns=(KnowledgeSupertagField.supertag_id, KnowledgeSupertagField.field_id),
        )
        await self._lock_side_effect_rows(
            model=KnowledgeFieldValue,
            whereclause=KnowledgeFieldValue.node_id.in_(sorted(node_ids, key=str)),
            order_columns=(KnowledgeFieldValue.node_id, KnowledgeFieldValue.field_id),
        )
        await self._lock_side_effect_rows(
            model=KnowledgeNodePlacement,
            whereclause=or_(
                KnowledgeNodePlacement.node_id.in_(sorted(node_ids, key=str)),
                KnowledgeNodePlacement.parent_node_id.in_(sorted(node_ids, key=str)),
            ),
            order_columns=(KnowledgeNodePlacement.id,),
        )
        await self._lock_side_effect_rows(
            model=KnowledgeEdge,
            whereclause=or_(
                KnowledgeEdge.source_node_id.in_(sorted(node_ids, key=str)),
                KnowledgeEdge.target_node_id.in_(sorted(node_ids, key=str)),
            ),
            order_columns=(KnowledgeEdge.id,),
        )
        await self._lock_side_effect_rows(
            model=KnowledgeNodeShare,
            whereclause=KnowledgeNodeShare.node_id.in_(sorted(node_ids, key=str)),
            order_columns=(KnowledgeNodeShare.id,),
        )
        await self._lock_side_effect_rows(
            model=KnowledgeSearchIndex,
            whereclause=KnowledgeSearchIndex.node_id.in_(sorted(node_ids, key=str)),
            order_columns=(KnowledgeSearchIndex.node_id,),
        )
        await self._lock_side_effect_rows(
            model=KnowledgeRevision,
            whereclause=KnowledgeRevision.node_id.in_(sorted(node_ids, key=str)),
            order_columns=(KnowledgeRevision.id,),
        )
        if await self._table_available(ProjectKnowledgeRef.__tablename__):
            await self._lock_side_effect_rows(
                model=ProjectKnowledgeRef,
                whereclause=or_(
                    ProjectKnowledgeRef.knowledge_node_id.in_(sorted(node_ids, key=str)),
                    ProjectKnowledgeRef.project_id.in_(sorted(project_ids, key=str)) if project_ids else text("FALSE"),
                ),
                order_columns=(ProjectKnowledgeRef.id,),
            )

        # These trigger-maintained rows are deliberately locked last.  Every
        # ordinary writer acquires its canonical/domain row first and only then
        # reaches the revision/policy trigger; taking the state rows after the
        # corresponding row set avoids a reverse-order deadlock while still
        # making the final lease/revision recheck stable.
        await self._lock_side_effect_rows(
            model=DocsLibraryRevision,
            whereclause=DocsLibraryRevision.library_id == root.docs_library_id,
            order_columns=(DocsLibraryRevision.library_id,),
        )
        await self._lock_side_effect_rows(
            model=DocsAuthorityState,
            whereclause=DocsAuthorityState.id == 1,
            order_columns=(DocsAuthorityState.id,),
        )
        return {
            "node_ids": node_ids,
            "mutation_ids": mutation_ids,
            "structural_roots": structural_roots,
            "project_ids": project_ids,
            "task_ids": task_ids,
            "task_bind_project_ids": task_bind_project_ids,
            "task_write_project_ids": task_write_project_ids,
        }

    async def _recheck_after_prelock(
        self,
        *,
        actor_id: UUID,
        root_id: UUID,
        write_token: UUID,
        binding: str,
        root,
        allowed: set[UUID],
        plan: dict[str, object],
    ):
        """Recheck all optimistic authority immediately before first DML."""
        await require_active_actor(self.session, actor_id)
        current_root = await self._authorized_node(root_id, actor_id)
        if current_root is None or current_root.docs_library_id != root.docs_library_id:
            raise DocsConflict("Docs edit root changed authorization; read again")
        lease = await self.session.get(DocsReadLease, write_token)
        if (
            lease is None
            or lease.actor_id != actor_id
            or lease.root_id != root_id
            or lease.library_id != current_root.docs_library_id
            or lease.scope_binding != binding
            or lease.expires_at <= datetime.utcnow()
        ):
            raise DocsConflict("Docs edit lease expired or does not match this scope; read again")
        if await revision(self.session, current_root.docs_library_id) != (
            lease.revision,
            lease.policy_revision,
        ):
            raise DocsConflict("Docs or permissions changed before mutation; read again")

        from .task_management_service import TaskManagementService

        task_service = TaskManagementService()
        for project_id in sorted(set(plan.get("task_write_project_ids") or ()), key=str):
            await task_service.require_project_permission(
                self.session, project_id=project_id, user_id=actor_id, permission="write",
            )
            await task_service.require_project_permission(
                self.session, project_id=project_id, user_id=actor_id, permission="read",
            )

        for node_id in sorted(set(plan["mutation_ids"]), key=str):
            node = await self._authorized_node(node_id, actor_id)
            if node is None or node.docs_library_id != current_root.docs_library_id or node_id not in allowed:
                raise DocsConflict("A Docs mutation target changed authorization; read again")
            await assert_managed_docs_tree_mutation_allowed(self.session, node, tool_name="docs_mutate")
        return current_root, lease

    async def apply(self, *, actor_id: UUID, root_id: UUID, write_token: UUID,
                    operation_id: UUID, changeset: dict, binding: str, before_commit):
        operations = parse_changeset(changeset)
        request_hash = hashlib.sha256(json.dumps(
            [str(root_id), str(write_token), binding, changeset], ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        await lock_docs_writes(self.session)
        await require_active_actor(self.session, actor_id)
        root = await self._authorized_node(root_id, actor_id)
        if root is None:
            raise PermissionError("Docs edit root is not writable")
        receipt = await self.session.get(DocsMutationReceipt, operation_id)
        if receipt is not None:
            if (receipt.actor_id != actor_id or receipt.root_id != root_id
                    or receipt.request_hash != request_hash):
                raise DocsConflict("operation_id is already bound to a different request")
            before_commit()
            for node_id in receipt.result.get("affected_node_ids", []):
                if await self._authorized_node(UUID(node_id), actor_id, required="read") is None:
                    return {"success": True, "committed": True, "operation_id": str(operation_id),
                            "id": str(root_id), "replayed": True, "details_redacted": True}
            return {**receipt.result, "replayed": True}
        lease = await self.session.get(DocsReadLease, write_token)
        if (lease is None or lease.actor_id != actor_id or lease.root_id != root_id
                or lease.library_id != root.docs_library_id or lease.scope_binding != binding
                or lease.expires_at <= datetime.utcnow()):
            raise DocsConflict("Docs edit lease expired or does not match this scope; read again")
        if await revision(self.session, root.docs_library_id) != (lease.revision, lease.policy_revision):
            raise DocsConflict("Docs or permissions changed after reading; read again before editing")
        allowed = {UUID(value) for value in lease.node_ids}
        created, affected = {}, set()

        # Validate references and dependencies before the first mutation. New
        # local references must be declared before use, never guessed by title.
        known = set(map(str, allowed))
        for operation in operations:
            for key in ("node_id", "parent_id"):
                if key in operation and operation[key] not in known:
                    raise ValueError("Operation target is outside the edit view or precedes its creation")
            if operation["op"] == "create":
                ref = operation["ref"]
                if not ref.startswith("local:") or len(ref) > 80 or ref in known:
                    raise ValueError("Each created node requires a unique local: reference")
                known.add(ref)

        # Plan and fence the complete affected closure before any graph method
        # can flush canonical DML.  The lease/revision/authority checks are
        # repeated after these NOWAIT locks so a concurrent ordinary writer
        # cannot slip between validation and the first mutation statement.
        plan = await self._prelock_affected_rows(
            actor_id=actor_id,
            root=root,
            operations=operations,
            allowed=allowed,
        )
        root, lease = await self._recheck_after_prelock(
            actor_id=actor_id,
            root_id=root_id,
            write_token=write_token,
            binding=binding,
            root=root,
            allowed=allowed,
            plan=plan,
        )
        before_commit()

        async def target(value):
            node_id = created.get(value) or UUID(value)
            node = await self._authorized_node(node_id, actor_id)
            if (node is None or node.docs_library_id != root.docs_library_id or node_id not in allowed):
                raise PermissionError("Changeset target is not writable in this edit view")
            await assert_managed_docs_tree_mutation_allowed(self.session, node, tool_name="docs_mutate")
            return node

        async def whole_subtree(node):
            tree = select(KnowledgeNode.id).where(KnowledgeNode.id == node.id).cte("docs_mutation_subtree", recursive=True)
            tree = tree.union(select(KnowledgeNode.id).join(tree, KnowledgeNode.parent_id == tree.c.id).where(
                KnowledgeNode.docs_library_id == root.docs_library_id,
            ))
            ids = (await self.session.execute(select(KnowledgeNode.id).where(
                KnowledgeNode.id.in_(select(tree.c.id)), KnowledgeNode.archived_at.is_(None),
            ))).scalars().all()
            if not set(ids) <= allowed:
                raise PermissionError("Structural mutation includes nodes outside the edit view")
            for node_id in ids:
                await target(str(node_id))
            affected.update(ids)

        for operation in operations:
            before_commit()
            kind = operation["op"]
            if kind == "create":
                parent = await target(operation["parent_id"])
                block_type = operation.get("block_type", "paragraph")
                body = {"format": "doc_block", "block_type": block_type}
                if block_type in {"markdown", "code"}:
                    body["content"] = operation.get("content", "")
                elif operation["title"] == "":
                    body["blank"] = True
                node = await self.docs.create_node(
                    docs_library_id=root.docs_library_id, user_id=actor_id, parent=parent,
                    project_id=parent.project_id, title=operation["title"], body_json=body,
                )
                if "description" in operation:
                    await self.docs.update_node(node=node, user_id=actor_id, description=operation["description"])
                created[operation["ref"]] = node.id
                allowed.add(node.id)
            else:
                node = await target(operation["node_id"])
                if kind == "update":
                    arguments = {key: operation[key] for key in ("title", "description") if key in operation}
                    if "content" in operation:
                        body = dict(node.body_json or {})
                        if body.get("format") != "doc_block" or body.get("block_type") not in {"markdown", "code"}:
                            raise ValueError("content updates require an existing markdown/code block")
                        body["content"] = operation["content"]
                        arguments["body_json"] = body
                    await self.docs.update_node(node=node, user_id=actor_id, **arguments)
                elif kind == "set_fields":
                    definitions = await self.docs.resolve_node_fields(node)
                    for key, value in operation["values"].items():
                        definition = definitions.get(key)
                        if definition is not None and definition.field_type == "reference" and value not in (None, ""):
                            reference = await self._authorized_node(UUID(str(value)), actor_id, required="read")
                            if reference is None or not await self.docs._query_reference_visible(
                                reference.id, user_id=actor_id, turn_project_id=root.project_id,
                            ):
                                raise PermissionError("Reference target is not readable in this scope")
                    await self.docs.set_fields(node=node, user_id=actor_id, values=operation["values"])
                elif kind in {"add_tag", "remove_tag"}:
                    tag = await self.docs.resolve_supertag(docs_library_id=root.docs_library_id, tag=operation["tag_id"], create=False)
                    if kind == "add_tag":
                        node_key = str(operation["node_id"])
                        if not node_key.startswith("local:"):
                            node_key = str(UUID(node_key))
                        await self.docs.add_tag(
                            node=node,
                            tag=tag,
                            user_id=actor_id,
                            task_project_id=plan.get("task_bind_project_ids", {}).get(node_key),
                        )
                    else:
                        await self.docs.remove_tag(node=node, tag=tag, user_id=actor_id)
                else:
                    if node.id == root.id:
                        raise ValueError("Move/archive a child section, not the edit root")
                    await whole_subtree(node)
                    if kind == "move":
                        parent = await target(operation["parent_id"])
                        await self.docs.move_node(node=node, new_parent=parent, user_id=actor_id,
                                                  leave_reference=operation.get("leave_reference", False))
                    else:
                        await self.docs.archive_subtree(root=node, user_id=actor_id)
            affected.add(node.id)
        await self.session.flush()
        current = await revision(self.session, root.docs_library_id)
        result = {
            "success": True, "committed": False, "operation_id": str(operation_id),
            "id": str(root.id), "replayed": False, "revision": current[0],
            "affected_node_ids": sorted(map(str, affected)),
            "created_ids": {key: str(value) for key, value in created.items()},
        }
        self.session.add(DocsMutationReceipt(operation_id=operation_id, actor_id=actor_id,
                                             root_id=root_id, request_hash=request_hash, result={**result, "committed": True}))
        await self.session.flush()
        before_commit()
        # The caller commits this transaction exactly once, together with the
        # receipt and DB-triggered queue. Notifications are not commit authority.
        return result
