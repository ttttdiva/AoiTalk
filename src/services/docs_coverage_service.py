"""Resumable, version-bound enumeration and evidence delivery for Docs overview.

Coverage describes source records delivered by this protocol, not a claim that
an LLM understood every fact. A corpus change invalidates the run rather than
mixing pages from different versions or guessing a historical snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select

from ..memory.models import KnowledgeNode
from ..memory.models.docs_agent import DocsCoverageRun
from .docs_consistency import (
    DocsConflict,
    DocsContractUnavailable,
    contract_available,
    require_active_actor,
    revision,
)
from .docs_graph_service import DocsGraphService
from .docs_read_projection import build_docs_read_projection
from .docs_scope import DocsScopeMode, resolve_docs_scope
from .docs_corpus_snapshot import corpus_fingerprint


def _scope_hash(scope):
    ids = sorted({str(value) for value in (*scope.canonical_node_ids, *scope.related_node_ids)})
    return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


class DocsCoverageService:
    def __init__(self, session):
        self.session = session
        self.docs = DocsGraphService(session)

    async def _enumerate(self, scope, actor_id, filters, max_nodes):
        ids, offset, total = [], 0, 0
        while len(ids) < max_nodes:
            page = await self.docs.query_with_scope_result(docs_scope=scope, user_id=actor_id,
                turn_project_id=scope.project_id, offset=offset, limit=min(100, max_nodes - len(ids)), **filters)
            total = page.total_matches
            ids.extend(str(node.id) for node in page.nodes)
            if not page.has_more:
                break
            if not page.nodes:
                raise DocsConflict("Overview enumeration stopped without a continuation")
            offset += len(page.nodes)
        return ids, total

    async def start(self, *, actor_id, scope, binding, filters=None, max_nodes=10000):
        filters = filters or {}
        if not isinstance(filters, dict) or set(filters) - {"tags", "text", "field_filters", "date_from", "date_to", "order_by", "order"}:
            raise ValueError("Use supported structured Docs filters for overview")
        if type(max_nodes) is not int or not 1 <= max_nodes <= 50000:
            raise ValueError("max_nodes must be between 1 and 50000")
        # This is a read-only schema/trigger check.  It deliberately does not
        # enter the Agent advisory critical section, so overview enumeration
        # cannot starve or be starved by an Agent mutation.
        if not await contract_available(self.session):
            raise DocsContractUnavailable("Docs Agent consistency migration is required")
        await require_active_actor(self.session, actor_id)
        before_versions = {
            str(lib): list(await revision(self.session, lib))
            for lib in scope.allowed_library_ids
        }
        ids, total = await self._enumerate(scope, actor_id, filters, max_nodes)
        after_versions = {
            str(lib): list(await revision(self.session, lib))
            for lib in scope.allowed_library_ids
        }
        if after_versions != before_versions:
            raise DocsConflict("Docs changed during overview enumeration; retry from current sources")
        versions = after_versions
        corpus = await corpus_fingerprint(self.session, ids)
        fingerprint_versions = {
            str(lib): list(await revision(self.session, lib))
            for lib in scope.allowed_library_ids
        }
        if fingerprint_versions != versions:
            raise DocsConflict("Docs changed while capturing overview sources; retry from current sources")
        now = datetime.utcnow()
        await self.session.execute(delete(DocsCoverageRun).where(DocsCoverageRun.expires_at < now))
        run = DocsCoverageRun(id=uuid4(), actor_id=actor_id, scope_binding=binding,
            expires_at=now + timedelta(hours=2), state={
                "ids": ids, "versions": versions, "source_total": total, "filters": filters,
                "max_nodes": max_nodes, "corpus_fingerprint": corpus,
                "project_id": str(scope.project_id) if scope.project_id else None,
                "scope_mode": scope.mode.value, "scope_hash": _scope_hash(scope), "actor_id": str(actor_id),
                "position": 0, "record_cursor": "", "incomplete_ids": [],
                "next_cursor": str(uuid4()), "last_cursor": None, "last_reply": None,
            })
        self.session.add(run)
        await self.session.flush()
        return run

    async def _validate_versions(self, state):
        await require_active_actor(self.session, UUID(state["actor_id"]))
        scope = await resolve_docs_scope(session=self.session, actor_user_id=UUID(state["actor_id"]),
            project_id=UUID(state["project_id"]) if state["project_id"] else None,
            mode=DocsScopeMode(state["scope_mode"]))
        current_versions = {str(lib): list(await revision(self.session, lib)) for lib in scope.allowed_library_ids}
        for library, expected in state["versions"].items():
            if list(await revision(self.session, UUID(library)))[1] != expected[1]:
                raise DocsConflict("Docs permissions changed; restart overview with current sources")
        if _scope_hash(scope) == state["scope_hash"] and current_versions == state["versions"]:
            return False
        ids, total = await self._enumerate(scope, UUID(state["actor_id"]), state["filters"], state["max_nodes"])
        if ids != state["ids"] or total != state["source_total"]:
            raise DocsConflict("Docs corpus membership changed; restart overview with current sources")
        if await corpus_fingerprint(self.session, ids) != state["corpus_fingerprint"]:
            raise DocsConflict("Selected Docs content changed; restart overview with current sources")
        # Unrelated edits outside a filtered historical corpus must not starve
        # long overview runs. Advance only the observation watermark.
        state["versions"] = current_versions
        state["scope_hash"] = _scope_hash(scope)
        return True

    async def read_page(self, *, run_id, actor_id, binding, cursor, page_chars=12000):
        if not await contract_available(self.session):
            raise DocsContractUnavailable("Docs Agent consistency migration is required")
        await require_active_actor(self.session, actor_id)
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("A coverage continuation cursor is required")
        UUID(cursor)
        if type(page_chars) is not int or not 6000 <= page_chars <= 32000:
            raise ValueError("overview page_chars must be between 6000 and 32000")
        # Serialize requests to the same run.  Overview projection is a
        # read-oriented, potentially long operation and must not acquire the
        # Agent mutation advisory lock or a global writer boundary.
        run = (await self.session.execute(select(DocsCoverageRun).where(
            DocsCoverageRun.id == run_id, DocsCoverageRun.actor_id == actor_id,
        ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if run is None or run.scope_binding != binding or run.expires_at <= datetime.utcnow():
            raise PermissionError("Overview run is unavailable in this scope")
        state = copy.deepcopy(run.state)
        await self._validate_versions(state)
        if cursor == state["last_cursor"]:
            await self._validate_versions(state)
            if page_chars == state.get("last_page_chars"):
                run.state = state
                await self.session.flush()
                return state["last_reply"]
            # A model-context budget may require replaying the same source
            # page at a smaller size. Restore its checkpoint, never skip it.
            state.update(copy.deepcopy(state["checkpoint"]))
        elif cursor != state["next_cursor"]:
            raise DocsConflict("Overview cursor is stale; use the last returned continuation")
        checkpoint = {key: copy.deepcopy(state[key]) for key in ("position", "record_cursor", "incomplete_ids")}
        pages, used = [], 1800
        while state["position"] < len(state["ids"]) and len(pages) < 10 and page_chars - used >= 4096:
            node_id = UUID(state["ids"][state["position"]])
            node = await self.session.get(KnowledgeNode, node_id)
            if node is None:
                raise DocsConflict("Overview source disappeared; restart overview")
            page = await build_docs_read_projection(
                self.docs, node, actor_id, record_only=True,
                allowed_node_ids={UUID(value) for value in state["ids"]},
                turn_project_id=UUID(state["project_id"]) if state["project_id"] else None,
                cursor=state["record_cursor"], page_chars=min(12000, page_chars - used),
            )
            pages.append(page)
            used += len(json.dumps(page, ensure_ascii=False, separators=(",", ":")))
            if page["has_more"]:
                state["record_cursor"] = page["next_cursor"]
                break
            if not page["coverage_complete"]:
                state["incomplete_ids"].append(str(node_id))
            state["position"] += 1
            state["record_cursor"] = ""
        revalidated = await self._validate_versions(state)
        if revalidated:
            # A selected row could have changed and reverted while the page
            # was being built. Validate actual delivered projections too.
            for page in pages:
                node = await self.session.get(KnowledgeNode, UUID(page["root_id"]))
                current = await build_docs_read_projection(self.docs, node, actor_id, record_only=True,
                    allowed_node_ids={UUID(value) for value in state["ids"]},
                    turn_project_id=UUID(state["project_id"]) if state["project_id"] else None, page_chars=4096)
                if current["read_fingerprint"] != page["read_fingerprint"]:
                    raise DocsConflict("Docs changed during page delivery; retry from current sources")
        has_more = state["position"] < len(state["ids"])
        next_cursor = str(uuid4()) if has_more else None
        complete = not has_more and len(state["ids"]) == state["source_total"] and not state["incomplete_ids"]
        reply = {
            "schema": "docs_coverage.v1", "run_id": str(run.id), "request_cursor": cursor,
            "source_total": state["source_total"], "selected_records": len(state["ids"]),
            "delivered_records": state["position"], "incomplete_records": len(state["incomplete_ids"]),
            "coverage_complete": complete, "has_more": has_more, "next_cursor": next_cursor,
            "coverage_basis": "authorized source records delivered; not proof of model understanding",
            "budget_limited": len(state["ids"]) < state["source_total"],
            "date_basis": state["filters"].get("order_by", "updated_at"),
            "date_warning": "Node dates are not business event dates; verify events in source content",
            "records": pages,
        }
        state.update(last_cursor=cursor, next_cursor=next_cursor, last_reply=reply,
                     last_page_chars=page_chars, checkpoint=checkpoint)
        run.state = state
        await self.session.flush()
        return reply
