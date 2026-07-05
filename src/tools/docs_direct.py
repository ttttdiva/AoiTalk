"""Direct runtime tools for AoiTalk Docs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select

from .core import tool


DOCS_READ_TOOL_NAMES = {
    "docs_search",
    "docs_outline",
    "docs_query",
}

DOCS_MUTATION_TOOL_NAMES = {
    "docs_create_nodes",
    "docs_update_node",
    "docs_set_fields",
    "docs_add_tag",
    "docs_remove_tag",
    "docs_move_node",
    "docs_archive_node",
}


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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


async def _resolve_operator_user_id(session) -> UUID:
    from ..memory.models import User

    admin_result = await session.execute(select(User).where(User.role == "admin").limit(1))
    admin_user = admin_result.scalar_one_or_none()
    if admin_user:
        return admin_user.id
    first_user_result = await session.execute(select(User).limit(1))
    first_user = first_user_result.scalar_one_or_none()
    if first_user:
        return first_user.id
    raise ValueError("No local user exists to act as the Docs operator.")


def build_docs_direct_tools() -> list:
    """Build root-level Docs tools used by chat agents."""

    @tool
    def docs_search(query: str, project: str = "", tag: str = "", limit: int = 20) -> str:
        """Search AoiTalk Docs nodes. Returns compact lines: short_id | title | tags."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _search():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await service.resolve_project(project) if project.strip() else None
                nodes = await service.search(
                    workspace_id=workspace.id,
                    query=query,
                    project_id=project_obj.id if project_obj else None,
                    tag=tag,
                    limit=limit,
                )
                return await service.format_search_results(nodes)
            finally:
                await session.close()

        try:
            return _run_async(_search())
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_outline(target: str, project: str = "", depth: int = 3) -> str:
        """Read a Docs outline by node id/prefix/title, 'today', or project name."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _outline():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await service.resolve_project(project or target)
                if project_obj and target.strip().casefold() in {"", "project", "案件", project_obj.name.casefold(), (project_obj.slug or "").casefold()}:
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
                return "\n".join(await service.outline_lines(root=root, depth=depth))
            finally:
                await session.close()

        try:
            return _run_async(_outline())
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_create_nodes(parent: str, outline_text: str, project: str = "") -> str:
        """Create a subtree from indented outline text. Inline #tags are attached and Field:: value tokens are set."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _create():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await service.resolve_project(project) if project.strip() else None
                parent_node = await service.resolve_node(
                    workspace_id=workspace.id,
                    ref=parent or "today",
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
    def docs_update_node(node_id: str, title: str = "", description: str = "") -> str:
        """Update a Docs node title and/or description."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _update():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                node = await service.resolve_node(workspace_id=workspace.id, ref=node_id)
                await service.update_node(
                    node=node,
                    user_id=user_id,
                    title=title if title.strip() else None,
                    description=description if description.strip() else None,
                )
                await session.commit()
                return {"success": True, "id": str(node.id), "short_id": str(node.id)[:8], "title": node.title}
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
    def docs_set_fields(node_id: str, fields_json: str) -> str:
        """Set Docs fields from a JSON object. Task system fields proxy to native tasks."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _set():
            values = _parse_json_object(fields_json, field_name="fields_json")
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                node = await service.resolve_node(workspace_id=workspace.id, ref=node_id)
                updated = await service.set_fields(node=node, values=values, user_id=user_id)
                await session.commit()
                return {"success": True, "id": str(node.id), "updated": updated}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_set()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_add_tag(node_id: str, tag: str) -> str:
        """Attach a Docs supertag to a node. Adding #Task creates/binds a native task."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _add():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                node = await service.resolve_node(workspace_id=workspace.id, ref=node_id)
                supertag = await service.resolve_supertag(workspace_id=workspace.id, tag=tag, create=True)
                changed = await service.add_tag(node=node, tag=supertag, user_id=user_id)
                await session.commit()
                return {"success": True, "changed": changed, "id": str(node.id), "tag": supertag.name}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_add()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def docs_remove_tag(node_id: str, tag: str) -> str:
        """Remove a Docs supertag from a node. Removing #Task unlinks the native task without deleting it."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _remove():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                node = await service.resolve_node(workspace_id=workspace.id, ref=node_id)
                supertag = await service.resolve_supertag(workspace_id=workspace.id, tag=tag, create=False)
                changed = await service.remove_tag(node=node, tag=supertag, user_id=user_id)
                await session.commit()
                return {"success": True, "changed": changed, "id": str(node.id), "tag": supertag.name}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_remove()))
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
                user_id = await _resolve_operator_user_id(session)
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
    def docs_query(
        tags: str = "",
        text: str = "",
        project: str = "",
        group_by: str = "",
        limit: int = 20,
    ) -> str:
        """Run a compact Docs query. Returns count and top rows; use tags for a single tag name."""
        from ..memory.database import get_database_manager
        from ..services.docs_graph_service import DocsGraphService

        async def _query():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id = await _resolve_operator_user_id(session)
                service = DocsGraphService(session)
                workspace = await service.ensure_workspace(user_id)
                project_obj = await service.resolve_project(project) if project.strip() else None
                tag = tags.split(",")[0].strip() if tags.strip() else ""
                nodes = await service.search(
                    workspace_id=workspace.id,
                    query=text,
                    project_id=project_obj.id if project_obj else None,
                    tag=tag,
                    limit=limit,
                )
                header = f"count={len(nodes)}"
                if group_by:
                    header += f" group_by={group_by}"
                return header + "\n" + await service.format_search_results(nodes)
            finally:
                await session.close()

        try:
            return _run_async(_query())
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
                user_id = await _resolve_operator_user_id(session)
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
        docs_outline,
        docs_create_nodes,
        docs_update_node,
        docs_set_fields,
        docs_add_tag,
        docs_remove_tag,
        docs_move_node,
        docs_query,
        docs_archive_node,
    ]
