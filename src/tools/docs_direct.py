"""Direct runtime tools for AoiTalk Docs.

Tool surface for chat agents, intentionally kept to a small, non-overlapping
set (Anthropic "writing effective tools for agents" guidance):

- Read: ``docs_search`` (find), ``docs_read`` (fetch one node in full),
  ``docs_query`` (structured tag/field query).
- Write: ``docs_create_nodes``, ``docs_update_node`` (title / description /
  fields / tags in one call), ``docs_move_node``, ``docs_archive_node``.

Field and tag mutations are folded into ``docs_update_node`` because tagging a
node and setting its fields is almost always a single logical edit.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select

from .core import tool


DOCS_READ_TOOL_NAMES = {
    "docs_search",
    "docs_read",
    "docs_query",
}

DOCS_MUTATION_TOOL_NAMES = {
    "docs_ensure_inbox",
    "docs_create_nodes",
    "docs_update_node",
    "docs_move_node",
    "docs_archive_node",
}


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    context = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, coro).result()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _parse_json_object(value: str, *, field_name: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return parsed


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


async def _resolve_operator_user_id(session, *, require_context: bool = False) -> UUID:
    from ..memory.models import User
    from ..services.turn_context import get_turn_context

    turn_user_id = get_turn_context().user_id
    if turn_user_id:
        try:
            user = await session.get(User, UUID(turn_user_id))
        except (TypeError, ValueError):
            user = None
        if user is None:
            raise ValueError("Authenticated Docs user was not found.")
        return user.id
    if require_context:
        raise PermissionError("Authenticated Docs user context is required for mutation.")

    admin_result = await session.execute(select(User).where(User.role == "admin").limit(1))
    admin_user = admin_result.scalar_one_or_none()
    if admin_user:
        return admin_user.id
    first_user_result = await session.execute(select(User).limit(1))
    first_user = first_user_result.scalar_one_or_none()
    if first_user:
        return first_user.id
    raise ValueError("No local user exists to act as the Docs operator.")


async def _resolve_authorized_project(session, service, project_ref: str, user_id: UUID):
    from ..memory.models import User
    from ..memory.project_repository import ProjectRepository
    from ..services.turn_context import get_turn_context

    ref = str(project_ref or "").strip() or str(get_turn_context().project_id or "").strip()
    if ref == "*":
        return None
    if not ref:
        return None
    project = await service.resolve_project(ref)
    if project is None:
        raise ValueError(f"project not found: {ref}")
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError("Docs user was not found.")
    if user.role != "admin":
        member = await ProjectRepository.get_member(session, project.id, user_id)
        if member is None:
            raise PermissionError("project access denied")
    return project


async def _email_node_allowed_in_turn(session, node) -> bool:
    """Keep generic cross-project Docs access while isolating archived email trees."""
    from ..memory.models import KnowledgeNode, KnowledgeNodeSupertag, KnowledgeSupertag
    from ..services.turn_context import get_turn_context

    turn_project_ref = str(get_turn_context().project_id or "").strip()
    if not turn_project_ref or node.project_id is None:
        return True
    try:
        turn_project_id = UUID(turn_project_ref)
    except (TypeError, ValueError):
        return True
    if node.project_id == turn_project_id:
        return True

    ancestor_ids = []
    current = node
    seen = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        ancestor_ids.append(current.id)
        if str(current.system_key or "").startswith("project_mail"):
            return False
        current = await session.get(KnowledgeNode, current.parent_id) if current.parent_id else None

    tagged = await session.scalar(
        select(KnowledgeNodeSupertag.node_id)
        .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
        .where(
            KnowledgeNodeSupertag.node_id.in_(ancestor_ids),
            KnowledgeSupertag.system_key == "email",
        )
        .limit(1)
    )
    return tagged is None


async def _semantic_docs_hits(workspace_id, query: str, project_id, limit: int) -> list[UUID]:
    """Return node ids from the Docs RAG index, or [] when it is unavailable.

    Fully guarded: a disabled index, a missing embedding model, or any runtime
    error degrades gracefully to lexical-only search rather than failing the
    tool call.
    """
    if not str(query or "").strip():
        return []
    try:
        from ..rag.docs_index import search_docs_index

        return await search_docs_index(
            workspace_id=workspace_id,
            query=query,
            project_id=project_id,
            limit=limit,
        )
    except Exception:
        return []


def build_docs_direct_tools() -> list:
    """Build root-level Docs tools used by chat agents."""

    @tool
    def docs_search(query: str, project: str = "", tag: str = "", limit: int = 20) -> str:
        """Search AoiTalk Docs (the internal outliner of notes, project info, and tasks).

        Hybrid keyword + semantic search over Docs nodes. Returns compact lines:
        `short_id | title | #tags | project | ⤷ parent`. Use `docs_read` to open a
        hit in full. This searches Docs only; use `search_memory` for past
        conversations.
        """
        from ..memory.database import get_database_manager
        from ..memory.models import KnowledgeNode
        from ..services.docs_graph_service import DocsGraphService

        async def _search():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await _resolve_authorized_project(
                    session, service, project, user_id
                )
                project_id = project_obj.id if project_obj else None
                lexical = await service.search(
                    workspace_id=workspace.id,
                    query=query,
                    project_id=project_id,
                    tag=tag,
                    limit=limit,
                )
                # Semantic hits (RAG). Merge before lexical, de-duplicated, so
                # paraphrase matches surface even when keywords differ.
                semantic_ids = await _semantic_docs_hits(
                    workspace.id, query, project_id, limit
                )
                merged = list(lexical)
                if semantic_ids:
                    seen = {node.id for node in merged}
                    extra_ids = [nid for nid in semantic_ids if nid not in seen]
                    if extra_ids:
                        extra_result = await session.execute(
                            select(KnowledgeNode).where(
                                KnowledgeNode.id.in_(extra_ids),
                                KnowledgeNode.workspace_id == workspace.id,
                                *([KnowledgeNode.project_id == project_id] if project_id is not None else []),
                                KnowledgeNode.archived_at.is_(None),
                            )
                        )
                        by_id = {n.id: n for n in extra_result.scalars().all()}
                        ordered_extra = [by_id[nid] for nid in extra_ids if nid in by_id]
                        merged = ordered_extra + merged
                merged = [
                    node
                    for node in merged
                    if await _email_node_allowed_in_turn(session, node)
                ]
                merged = merged[: max(1, min(int(limit or 20), 100))]
                return await service.format_search_results(merged)
            finally:
                await session.close()

        try:
            return _run_async(_search())
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_read(target: str, project: str = "", depth: int = 3) -> str:
        """Read a Docs node in full: header, description, fields, backlinks, and outline.

        `target` is a node id/prefix/title, 'today', or a project name. This is the
        detail view for a `docs_search` hit — it returns the node's description,
        current field values, nodes that reference it (backlinks), and its child
        outline down to `depth`.
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _read():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await _resolve_authorized_project(
                    session, service, project, user_id
                )
                if project_obj and target.strip().casefold() in {
                    "",
                    "project",
                    "案件",
                    project_obj.name.casefold(),
                    (project_obj.slug or "").casefold(),
                }:
                    if not project_obj.knowledge_node_id:
                        raise ValueError("project does not have a Docs information node yet")
                    root = await service.resolve_node(
                        workspace_id=workspace.id,
                        ref=str(project_obj.knowledge_node_id),
                        project_id=project_obj.id,
                    )
                else:
                    root = await service.resolve_node(
                        workspace_id=workspace.id,
                        ref=target,
                        project_id=project_obj.id if project_obj else None,
                    )

                if not await _email_node_allowed_in_turn(session, root):
                    raise PermissionError("project-bound Docs tools cannot access another project's email")

                ancestors = await service.ancestor_titles(root)
                fields = await service.get_node_field_values(root)
                backlinks = [
                    node
                    for node in await service.get_backlinks(root)
                    if await _email_node_allowed_in_turn(session, node)
                ]
                outline = await service.outline_lines(
                    root=root,
                    depth=depth,
                    node_filter=lambda node: _email_node_allowed_in_turn(session, node),
                )

                sections: list[str] = []
                header = f"# {root.title}  ({str(root.id)[:8]})"
                sections.append(header)
                if ancestors:
                    sections.append("path: " + " / ".join(t for t in ancestors if t))
                if root.project_id:
                    sections.append(f"project: {str(root.project_id)[:8]}")
                if root.description:
                    sections.append("\n## description\n" + str(root.description))
                if fields:
                    field_lines = "\n".join(f"- {name}: {value}" for name, value in fields.items())
                    sections.append("\n## fields\n" + field_lines)
                if backlinks:
                    link_lines = "\n".join(
                        f"- {str(node.id)[:8]} | {node.title}" for node in backlinks
                    )
                    sections.append("\n## backlinks\n" + link_lines)
                sections.append("\n## outline\n" + "\n".join(outline))
                return "\n".join(sections)
            finally:
                await session.close()

        try:
            return _run_async(_read())
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_query(
        tags: str = "",
        text: str = "",
        project: str = "",
        fields_json: str = "",
        group_by: str = "",
        limit: int = 20,
    ) -> str:
        """Structured Docs query. AND over comma-separated `tags`, optional field equality.

        `fields_json` is a JSON object of field name -> expected value (e.g.
        `{"status": "done"}`). `group_by` is a field name that buckets the rows and
        adds per-group counts. Returns a count header then compact rows.
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _query():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await _resolve_authorized_project(
                    session, service, project, user_id
                )
                field_filters = _parse_json_object(fields_json, field_name="fields_json")
                nodes = await service.query_nodes(
                    workspace_id=workspace.id,
                    tags=_parse_csv(tags),
                    text=text,
                    project_id=project_obj.id if project_obj else None,
                    field_filters={str(k): str(v) for k, v in field_filters.items()},
                    limit=limit,
                )
                nodes = [
                    node
                    for node in nodes
                    if await _email_node_allowed_in_turn(session, node)
                ]
                header = f"count={len(nodes)}"
                if not group_by.strip():
                    return header + "\n" + await service.format_search_results(nodes)

                # Group rows by the requested field's current value.
                groups: dict[str, list] = {}
                for node in nodes:
                    values = await service.get_node_field_values(node)
                    key = values.get(group_by.strip()) or "(none)"
                    groups.setdefault(key, []).append(node)
                blocks = [f"{header} group_by={group_by.strip()}"]
                for key in sorted(groups):
                    members = groups[key]
                    blocks.append(f"\n[{key}] count={len(members)}")
                    blocks.append(await service.format_search_results(members))
                return "\n".join(blocks)
            finally:
                await session.close()

        try:
            return _run_async(_query())
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_ensure_inbox() -> str:
        """Return the current user's root Docs Inbox, creating it when absent."""
        from ..memory.database import get_database_manager
        from ..memory.models import KnowledgeNode, KnowledgeWorkspace
        from ..services.docs_graph_service import DocsGraphService

        async def _ensure():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                await session.execute(
                    select(KnowledgeWorkspace)
                    .where(KnowledgeWorkspace.id == workspace.id)
                    .with_for_update()
                )
                result = await session.execute(
                    select(KnowledgeNode).where(
                        KnowledgeNode.workspace_id == workspace.id,
                        KnowledgeNode.parent_id.is_(None),
                        KnowledgeNode.project_id.is_(None),
                        KnowledgeNode.archived_at.is_(None),
                        KnowledgeNode.title.ilike("Inbox"),
                    ).limit(1)
                )
                node = result.scalar_one_or_none()
                created = False
                if node is None:
                    node = await service.create_node(
                        workspace_id=workspace.id,
                        user_id=user_id,
                        title="Inbox",
                    )
                    created = True
                await session.commit()
                return {
                    "success": True,
                    "created": created,
                    "id": str(node.id),
                    "short_id": str(node.id)[:8],
                    "title": node.title,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_ensure()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_create_nodes(parent: str, outline_text: str, project: str = "") -> str:
        """Create a subtree from indented outline text.

        One node per non-empty line; leading tabs (or 4 spaces) set depth. Inline
        `#tags` are attached and `Field:: value` tokens set fields. `parent` is a
        node id/prefix/title or 'today' (the default day node).
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _create():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await _resolve_authorized_project(
                    session, service, project, user_id
                )
                parent_ref = parent or "today"
                if (
                    project_obj
                    and parent_ref.strip().casefold() in {"project", "案件"}
                    and project_obj.knowledge_node_id
                ):
                    parent_ref = str(project_obj.knowledge_node_id)
                parent_node = await service.resolve_node(
                    workspace_id=workspace.id,
                    ref=parent_ref,
                    project_id=project_obj.id if project_obj else None,
                )
                nodes = await service.create_nodes_from_outline(
                    workspace_id=workspace.id,
                    user_id=user_id,
                    parent=parent_node,
                    outline_text=outline_text,
                    project_id=project_obj.id if project_obj else parent_node.project_id,
                )
                await session.commit()
                return {
                    "success": True,
                    "created": [
                        {"id": str(node.id), "short_id": str(node.id)[:8], "title": node.title}
                        for node in nodes
                    ],
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_create()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_update_node(
        node_id: str,
        title: str = "",
        description: str = "",
        fields_json: str = "",
        add_tags: str = "",
        remove_tags: str = "",
    ) -> str:
        """Update a Docs node in one call: title, description, fields, and tags.

        `fields_json` is a JSON object of field name -> value (fields must be
        defined by one of the node's tags; empty value clears). `add_tags` /
        `remove_tags` are comma-separated tag names; adding `#Task` binds a native
        task, removing it unlinks without deleting.
        """
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _update():
            values = _parse_json_object(fields_json, field_name="fields_json") if fields_json.strip() else {}
            add_list = _parse_csv(add_tags)
            remove_list = _parse_csv(remove_tags)
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                node = await service.resolve_node(workspace_id=workspace.id, ref=node_id)
                changed: dict[str, Any] = {}

                if title.strip() or description.strip():
                    await service.update_node(
                        node=node,
                        user_id=user_id,
                        title=title if title.strip() else None,
                        description=description if description.strip() else None,
                    )
                    if title.strip():
                        changed["title"] = node.title
                    if description.strip():
                        changed["description"] = "updated"

                added: list[str] = []
                for tag_name in add_list:
                    supertag = await service.resolve_supertag(
                        workspace_id=workspace.id, tag=tag_name, create=True
                    )
                    if await service.add_tag(node=node, tag=supertag, user_id=user_id):
                        added.append(supertag.name)
                if added:
                    changed["added_tags"] = added

                removed: list[str] = []
                for tag_name in remove_list:
                    supertag = await service.resolve_supertag(
                        workspace_id=workspace.id, tag=tag_name, create=False
                    )
                    if await service.remove_tag(node=node, tag=supertag, user_id=user_id):
                        removed.append(supertag.name)
                if removed:
                    changed["removed_tags"] = removed

                if values:
                    updated = await service.set_fields(node=node, values=values, user_id=user_id)
                    changed["fields"] = updated

                await session.commit()
                return {
                    "success": True,
                    "id": str(node.id),
                    "short_id": str(node.id)[:8],
                    "title": node.title,
                    "changed": changed,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_update()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_move_node(node_id: str, new_parent: str, leave_reference: bool = False) -> str:
        """Move a Docs node under another node, optionally leaving a placement reference at the old parent."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _move():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                node = await service.resolve_node(workspace_id=workspace.id, ref=node_id)
                parent = await service.resolve_node(workspace_id=workspace.id, ref=new_parent)
                await service.move_node(
                    node=node,
                    new_parent=parent,
                    user_id=user_id,
                    leave_reference=leave_reference,
                )
                await session.commit()
                return {"success": True, "id": str(node.id), "parent_id": str(parent.id)}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_move()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_archive_node(node_id: str) -> str:
        """Archive a Docs node without hard-deleting it."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _archive():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session, require_context=True)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                node = await service.resolve_node(workspace_id=workspace.id, ref=node_id)
                await service.archive_node(node=node, user_id=user_id)
                await session.commit()
                return {"success": True, "id": str(node.id), "archived_at": node.archived_at}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_archive()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    return [
        docs_search,
        docs_read,
        docs_query,
        docs_ensure_inbox,
        docs_create_nodes,
        docs_update_node,
        docs_move_node,
        docs_archive_node,
    ]
