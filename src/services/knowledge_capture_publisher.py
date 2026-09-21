"""Server-side publication of validated Resolution Knowledge drafts.

The curator only proposes JSON.  This module is the trust boundary that
re-checks Project/Docs state and performs the canonical Docs mutation.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    AgentRun,
    ConversationMessage,
    ConversationSession,
    ContextMemory,
    KnowledgeCaptureCandidate,
    KnowledgeCaptureQuestion,
    KnowledgeNode,
    KnowledgeRevision,
    Project,
    Task,
    TaskActivity,
    TaskAttachment,
    TaskComment,
    TaskReference,
)
from ..memory.project_repository import ProjectRepository
from .docs_acl import can_read_node
from .docs_graph_service import DocsGraphService
from .docs_workspace import get_canonical_project_information_node
from .knowledge_capture_contract import contains_secret_like_text, normalize_semantic_key
from .knowledge_capture_evidence import (
    _clip,
    _iso,
    _task_text,
    _value,
    agent_run_evidence_text,
    conversation_session_evidence_text,
    content_hash,
    docs_node_evidence_text,
    docs_revision_evidence_text,
    project_memory_evidence_text,
    read_bound_project_file,
    task_activity_evidence_text,
    task_attachment_evidence_text,
    workspace_file_version,
)
from .knowledge_capture_publication_guard import (
    KnowledgeCapturePublicationGuardError,
    MANAGED_SECTION_LABELS,
    assert_publication_subtree_unchanged,
    capture_publication_subtree_guard,
    extract_publication_subtree_guard,
    validate_publication_subtree_guard,
)
from .privacy_masking_projection import is_privacy_masking_source
from .task_reference_service import conversation_reference_visible


MAX_PUBLICATION_SOURCE_REFS = 64
MAX_CANDIDATE_EVIDENCE_REFS = MAX_PUBLICATION_SOURCE_REFS - 1
MAX_SECTION_CHARS = 4_000
MAX_SECTION_ITEMS = 32
_SECRET_RE = re.compile(r"(?i)(?:api[_ -]?key|password|secret|token|credential)\s*[:=]")


class KnowledgeCapturePublishError(RuntimeError):
    """Publication was rejected or could not be completed safely."""


class KnowledgeCapturePublishConflict(KnowledgeCapturePublishError):
    """The candidate or Docs revision changed while publishing."""


def _uuid(value: Any, field: str, *, required: bool = True) -> UUID | None:
    if value in (None, ""):
        if required:
            raise KnowledgeCapturePublishError(f"{field} is required")
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise KnowledgeCapturePublishError(f"invalid {field}") from exc


def _text_value(value: Any, field: str, limit: int = MAX_SECTION_CHARS) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).replace("\x00", "").strip()
    if not value:
        return ""
    if len(value) > limit:
        raise KnowledgeCapturePublishError(f"{field} is too long")
    if contains_secret_like_text(value) or _SECRET_RE.search(value):
        raise KnowledgeCapturePublishError(f"{field} contains sensitive material")
    return value


def _semantic_key(candidate: KnowledgeCaptureCandidate, draft: Mapping[str, Any]) -> str:
    raw = draft.get("semantic_key") or candidate.knowledge_semantic_key
    try:
        key = normalize_semantic_key(raw)
    except Exception as exc:
        raise KnowledgeCapturePublishError("knowledge semantic key is invalid") from exc
    if not key:
        raise KnowledgeCapturePublishError("knowledge semantic key is required")
    return key


def _list_text(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise KnowledgeCapturePublishError(f"{field} must be a list")
    return [_text_value(item, f"{field}[{index}]") for index, item in enumerate(list(value)[:MAX_SECTION_ITEMS]) if _text_value(item, f"{field}[{index}]")]


def _bounded_candidate_evidence_refs(candidate: KnowledgeCaptureCandidate) -> list[Mapping[str, Any]]:
    refs: list[Mapping[str, Any]] = []
    for item in list(candidate.evidence_refs or [])[:MAX_CANDIDATE_EVIDENCE_REFS]:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or item.get("kind") or "").strip()
        source = str(item.get("id") or item.get("source_id") or "").strip()
        if kind and source:
            refs.append(item)
    return refs


def _evidence_ids(candidate: KnowledgeCaptureCandidate) -> set[str]:
    ids: set[str] = set()
    for item in _bounded_candidate_evidence_refs(candidate):
        kind = str(item.get("type") or item.get("kind") or "").strip()
        source = str(item.get("id") or item.get("source_id") or "").strip()
        ids.add(source)
        ids.add(f"{kind}:{source}")
    return ids


def _source_refs(candidate: KnowledgeCaptureCandidate) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = [
        {"type": "knowledge_capture_candidate", "id": str(candidate.id)},
    ]
    for item in _bounded_candidate_evidence_refs(candidate):
        kind = str(item.get("type") or item.get("kind") or "").strip()[:64]
        source = str(item.get("id") or item.get("source_id") or "").strip()[:240]
        ref: dict[str, Any] = {"type": kind, "id": source}
        if item.get("content_hash") or item.get("evidence_sha256"):
            ref["sha256"] = str(item.get("content_hash") or item.get("evidence_sha256"))[:80]
        if item.get("version"):
            ref["version"] = str(item.get("version"))[:80]
        if item.get("source_path"):
            ref["path"] = str(item["source_path"])[:500]
        refs.append(ref)
    return refs


def _normalize_draft(candidate: KnowledgeCaptureCandidate) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    raw = candidate.draft_json
    if not isinstance(raw, Mapping):
        raise KnowledgeCapturePublishError("candidate draft is unavailable")
    draft = dict(raw)
    key = _semantic_key(candidate, draft)
    title = _text_value(draft.get("title"), "draft.title", 240)
    problem = _text_value(draft.get("problem"), "draft.problem")
    resolution = _text_value(draft.get("resolution"), "draft.resolution")
    if not title or not problem or not resolution:
        raise KnowledgeCapturePublishError("draft requires title, problem, and resolution")

    evidence = _evidence_ids(candidate)
    for field in ("procedure", "verification", "pitfalls"):
        values = draft.get(field) or []
        if not isinstance(values, list) or len(values) > MAX_SECTION_ITEMS:
            raise KnowledgeCapturePublishError(f"draft.{field} is invalid")
        for index, item in enumerate(values):
            if not isinstance(item, Mapping):
                raise KnowledgeCapturePublishError(f"draft.{field}[{index}] is invalid")
            claim = _text_value(item.get("text"), f"draft.{field}[{index}].text")
            if not claim:
                raise KnowledgeCapturePublishError(f"draft.{field}[{index}] is empty")
            cited = item.get("evidence_ids") or []
            if not isinstance(cited, list) or not cited or any(str(ref) not in evidence for ref in cited):
                raise KnowledgeCapturePublishError(f"draft.{field}[{index}] has invalid evidence")

    normalized = {
        "schema_version": str(draft.get("schema_version") or "resolution-knowledge-v1"),
        "title": title,
        "semantic_key": key,
        "knowledge_kind": _text_value(draft.get("knowledge_kind") or candidate.knowledge_semantic_key or "procedure", "draft.knowledge_kind", 64),
        "problem": problem,
        "preconditions": _list_text(draft.get("preconditions"), "draft.preconditions"),
        "symptoms": _list_text(draft.get("symptoms"), "draft.symptoms"),
        "root_cause": _text_value(draft.get("root_cause"), "draft.root_cause") if draft.get("root_cause") else None,
        "resolution": resolution,
        "procedure": list(draft.get("procedure") or [])[:MAX_SECTION_ITEMS],
        "verification": list(draft.get("verification") or [])[:MAX_SECTION_ITEMS],
        "pitfalls": list(draft.get("pitfalls") or [])[:MAX_SECTION_ITEMS],
        "environment_constraints": _list_text(draft.get("environment_constraints"), "draft.environment_constraints"),
        "known_uncertainty": _list_text(draft.get("known_uncertainty"), "draft.known_uncertainty"),
        "source_evidence_ids": list(draft.get("source_evidence_ids") or [])[:MAX_CANDIDATE_EVIDENCE_REFS],
    }
    if not normalized["source_evidence_ids"] or any(str(ref) not in evidence for ref in normalized["source_evidence_ids"]):
        raise KnowledgeCapturePublishError("draft source evidence is invalid")
    return normalized, key, _source_refs(candidate)


async def _lock_project(session: AsyncSession, project_id: UUID) -> Project:
    result = await session.execute(
        select(Project).where(Project.id == project_id, Project.deleted_at.is_(None)).with_for_update()
    )
    project = result.scalar_one_or_none()
    if project is None or bool(project.is_completed):
        raise KnowledgeCapturePublishError("Project is unavailable for publication")
    try:
        bind = getattr(session, "bind", None)
        if bind is not None and getattr(bind.dialect, "name", "") == "postgresql":
            await session.execute(text("select pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": f"knowledge-capture-publication:{project_id}"})
    except Exception as exc:
        raise KnowledgeCapturePublishError("Project publication lock unavailable") from exc
    return project


async def _latest_revision(session: AsyncSession, node_id: UUID) -> KnowledgeRevision | None:
    result = await session.execute(
        select(KnowledgeRevision).where(KnowledgeRevision.node_id == node_id).order_by(KnowledgeRevision.created_at.desc(), KnowledgeRevision.id.desc()).limit(1)
    )
    return result.scalar_one_or_none()


def _parse_cited_ref(item: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(item, str):
        raw = item.strip()
        kind, source = (raw.split(":", 1) + [raw])[:2] if ":" in raw else ("", raw)
        return {"type": kind, "id": source, "citation": raw}
    kind = str(item.get("type") or item.get("kind") or "").strip()
    source = str(item.get("id") or item.get("source_id") or "").strip()
    citation = source
    if ":" in source and kind and source.split(":", 1)[0].casefold() == kind.casefold():
        source = source.split(":", 1)[1]
    elif not kind and ":" in source:
        kind, source = source.split(":", 1)
    projected = dict(item)
    projected["type"] = kind
    projected["id"] = source
    projected["citation"] = citation or f"{kind}:{source}"
    return projected


def _cited_draft_ids(draft: Mapping[str, Any]) -> list[str]:
    cited: list[str] = []
    for field in ("procedure", "verification", "pitfalls"):
        for item in draft.get(field) or []:
            if isinstance(item, Mapping):
                cited.extend(str(value) for value in (item.get("evidence_ids") or []) if str(value).strip())
    cited.extend(str(value) for value in (draft.get("source_evidence_ids") or []) if str(value).strip())
    return cited


def _hash_matches(stored: Any, live: Any) -> bool:
    if stored in (None, ""):
        return False
    left = str(stored).removeprefix("sha256:")
    right = str(live or "").removeprefix("sha256:")
    return bool(left) and bool(right) and left == right


def _projection_hash(text: Any) -> str:
    return content_hash(_clip(text))


def _version_matches(stored: Any, live: Any) -> bool:
    stored_text = "" if stored in (None, "") else str(stored)
    if live in (None, ""):
        live_text = ""
    elif isinstance(live, str):
        live_text = live
    else:
        live_text = _iso(live) or ""
    if not stored_text and not live_text:
        return True
    return bool(stored_text) and bool(live_text) and (stored_text == live_text or stored_text == str(live))


async def _assert_live_cited_source(
    session: AsyncSession,
    ref: Mapping[str, Any],
    *,
    project_id: UUID,
    actor_id: UUID,
    candidate_id: UUID | None = None,
) -> None:
    kind = str(ref.get("type") or ref.get("kind") or "").casefold()
    source = str(ref.get("id") or ref.get("source_id") or "").strip()
    stored_hash = ref.get("content_hash") or ref.get("sha256") or ref.get("evidence_sha256")
    stored_version = ref.get("version")
    if kind in {"url", "web"}:
        return
    source_uuid: UUID | None
    try:
        source_uuid = UUID(str(source))
    except (TypeError, ValueError, AttributeError):
        source_uuid = None
    live_hash = None
    live_version = None
    if kind == "task" and source_uuid is not None:
        row = await session.get(Task, source_uuid)
        if row is None or row.deleted_at is not None or row.project_id != project_id:
            raise KnowledgeCapturePublishError("cited Task evidence is stale")
        live_hash = _projection_hash(_task_text(row))
        live_version = _iso(_value(row, "updated_at") or _value(row, "completed_at"))
    elif kind == "task_activity" and source_uuid is not None:
        row = await session.get(TaskActivity, source_uuid)
        if row is None:
            raise KnowledgeCapturePublishError("cited Task activity evidence is stale")
        task = await session.get(Task, row.task_id)
        if task is None or task.deleted_at is not None or task.project_id != project_id:
            raise KnowledgeCapturePublishError("cited Task activity evidence is stale")
        live_hash = _projection_hash(task_activity_evidence_text(row))
        live_version = _iso(_value(row, "created_at"))
    elif kind == "task_comment" and source_uuid is not None:
        row = await session.get(TaskComment, source_uuid)
        if row is None:
            raise KnowledgeCapturePublishError("cited Task comment evidence is stale")
        task = await session.get(Task, row.task_id)
        if task is None or task.deleted_at is not None or task.project_id != project_id:
            raise KnowledgeCapturePublishError("cited Task comment evidence is stale")
        live_hash = _projection_hash(_value(row, "content", ""))
        live_version = _iso(_value(row, "updated_at") or _value(row, "created_at"))
    elif kind == "task_attachment" and source_uuid is not None:
        row = await session.get(TaskAttachment, source_uuid)
        if row is None or row.project_id != project_id:
            raise KnowledgeCapturePublishError("cited Task attachment evidence is stale")
        live_hash = _projection_hash(task_attachment_evidence_text(row))
        live_version = _iso(_value(row, "created_at"))
    elif kind == "task_reference" and source_uuid is not None:
        row = await session.get(TaskReference, source_uuid)
        if row is None or row.project_id != project_id:
            raise KnowledgeCapturePublishError("cited Task reference evidence is stale")
        live_hash = _projection_hash(_value(row, "display_name", ""))
        live_version = _iso(_value(row, "created_at"))
    elif kind == "agent_run" and source_uuid is not None:
        row = await session.get(AgentRun, source_uuid)
        if row is None or row.project_id != project_id:
            raise KnowledgeCapturePublishError("cited agent run evidence is stale")
        live_hash = _projection_hash(agent_run_evidence_text(row))
        live_version = _iso(_value(row, "updated_at") or _value(row, "created_at"))
    elif kind == "conversation_session" and source_uuid is not None:
        row = await session.get(ConversationSession, source_uuid)
        if row is None or row.deleted_at is not None or row.project_id != project_id:
            raise KnowledgeCapturePublishError("cited chat evidence is stale")
        try:
            visible = await conversation_reference_visible(
                session, row, user_id=actor_id, project_id=project_id
            )
        except Exception as exc:
            raise KnowledgeCapturePublishError("cited chat evidence is stale") from exc
        if not visible:
            raise KnowledgeCapturePublishError("cited chat evidence is stale")
        live_hash = _projection_hash(conversation_session_evidence_text(row))
        live_version = _iso(_value(row, "last_activity") or _value(row, "session_start"))
    elif kind == "conversation_message" and source_uuid is not None:
        row = await session.get(ConversationMessage, source_uuid)
        if row is None or row.deleted_at is not None or is_privacy_masking_source(row):
            raise KnowledgeCapturePublishError("cited chat evidence is stale")
        chat_session = await session.get(ConversationSession, row.session_id)
        if (
            chat_session is None
            or chat_session.deleted_at is not None
            or chat_session.project_id != project_id
        ):
            raise KnowledgeCapturePublishError("cited chat evidence is stale")
        try:
            visible = await conversation_reference_visible(
                session, chat_session, user_id=actor_id, project_id=project_id
            )
        except Exception as exc:
            raise KnowledgeCapturePublishError("cited chat evidence is stale") from exc
        if not visible:
            raise KnowledgeCapturePublishError("cited chat evidence is stale")
        live_hash = _projection_hash(_value(row, "content", ""))
        live_version = _iso(_value(row, "updated_at") or _value(row, "created_at"))
    elif kind == "docs_node" and source_uuid is not None:
        row = await session.get(KnowledgeNode, source_uuid)
        if row is None or row.archived_at is not None or row.project_id != project_id:
            raise KnowledgeCapturePublishError("cited Docs evidence is stale")
        try:
            readable = await can_read_node(session, row, actor_id)
        except Exception as exc:
            raise KnowledgeCapturePublishError("cited Docs evidence is stale") from exc
        if not readable:
            raise KnowledgeCapturePublishError("cited Docs evidence is stale")
        live_hash = _projection_hash(docs_node_evidence_text(row))
        live_version = _iso(_value(row, "updated_at") or _value(row, "created_at"))
    elif kind == "docs_revision" and source_uuid is not None:
        row = await session.get(KnowledgeRevision, source_uuid)
        if row is None:
            raise KnowledgeCapturePublishError("cited Docs revision evidence is stale")
        node = await session.get(KnowledgeNode, row.node_id)
        if node is None or node.archived_at is not None or node.project_id != project_id:
            raise KnowledgeCapturePublishError("cited Docs revision evidence is stale")
        try:
            readable = await can_read_node(session, node, actor_id)
        except Exception as exc:
            raise KnowledgeCapturePublishError("cited Docs revision evidence is stale") from exc
        if not readable:
            raise KnowledgeCapturePublishError("cited Docs revision evidence is stale")
        live_hash = _projection_hash(docs_revision_evidence_text(row))
        live_version = _iso(_value(row, "created_at"))
    elif kind == "project_memory" and source_uuid is not None:
        row = await session.get(ContextMemory, source_uuid)
        if row is None or str(row.project_id) != str(project_id) or str(row.status or "") != "active":
            raise KnowledgeCapturePublishError("cited Memory evidence is stale")
        live_hash = _projection_hash(project_memory_evidence_text(row))
        live_version = _iso(_value(row, "updated_at") or _value(row, "created_at"))
    elif kind == "workspace_file":
        path = str(ref.get("source_path") or ref.get("path") or source)
        try:
            info = read_bound_project_file(project_id, path)
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise KnowledgeCapturePublishError("cited workspace file evidence is stale") from exc
        live_hash = _projection_hash(info.get("excerpt", ""))
        live_version = workspace_file_version(info)
    elif kind == "user_confirmation":
        question_id = source.split(":")[-1] if ":" in source else source
        try:
            question_uuid = source_uuid or UUID(str(question_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise KnowledgeCapturePublishError("cited user confirmation is stale") from exc
        row = await session.get(KnowledgeCaptureQuestion, question_uuid)
        if (
            row is None
            or str(row.status) != "answered"
            or row.project_id != project_id
            or candidate_id is None
            or row.candidate_id != candidate_id
            or not str(row.answer or "").strip()
        ):
            raise KnowledgeCapturePublishError("cited user confirmation is stale")
        live_hash = _projection_hash(_value(row, "answer", ""))
        live_version = _iso(_value(row, "answered_at"))
    else:
        raise KnowledgeCapturePublishError("cited evidence cannot be revalidated")
    if not _hash_matches(stored_hash, live_hash):
        raise KnowledgeCapturePublishError("cited evidence hash is stale")
    if not _version_matches(stored_version, live_version):
        raise KnowledgeCapturePublishError("cited evidence version is stale")


async def _revalidate_cited_sources(
    session: AsyncSession,
    candidate: KnowledgeCaptureCandidate,
    draft: Mapping[str, Any],
    *,
    project_id: UUID,
    actor_id: UUID,
) -> None:
    refs_by_citation: dict[str, dict[str, Any]] = {}
    for item in _bounded_candidate_evidence_refs(candidate):
        parsed = _parse_cited_ref(item)
        refs_by_citation[str(item.get("id") or "")] = parsed
        refs_by_citation[f"{parsed['type']}:{parsed['id']}"] = parsed
        refs_by_citation[parsed.get("citation") or ""] = parsed
    for citation in _cited_draft_ids(draft):
        ref = refs_by_citation.get(citation) or _parse_cited_ref(citation)
        await _assert_live_cited_source(
            session,
            ref,
            project_id=project_id,
            actor_id=actor_id,
            candidate_id=candidate.id,
        )


async def _revalidate_seed(session: AsyncSession, candidate: KnowledgeCaptureCandidate, project_id: UUID) -> Task | None:
    if candidate.seed_task_id is None:
        return None
    task = await session.get(Task, candidate.seed_task_id)
    if task is None or task.deleted_at is not None or _uuid(task.project_id, "task.project_id") != project_id:
        raise KnowledgeCapturePublishError("seed Task evidence is stale")
    if str(task.status).casefold() not in {"closed", "done"}:
        raise KnowledgeCapturePublishError("seed Task is no longer completed")
    return task


async def _ensure_task_reference(session: AsyncSession, task: Task | None, node: KnowledgeNode, candidate: KnowledgeCaptureCandidate, actor_id: UUID) -> None:
    if task is None:
        return
    existing = await session.scalar(
        select(TaskReference).where(
            TaskReference.task_id == task.id,
            TaskReference.reference_type == "docs_node",
            TaskReference.relation_type == "related",
            TaskReference.target_id == str(node.id),
        ).limit(1)
    )
    if existing is not None:
        return
    session.add(TaskReference(
        task_id=task.id,
        project_id=task.project_id,
        reference_type="docs_node",
        relation_type="related",
        target_id=str(node.id),
        display_name=str(node.title or "Resolution Knowledge")[:500],
        dedupe_key=f"knowledge_capture:{candidate.id}:{node.id}"[:1200],
        reference_metadata={"knowledge_capture_candidate_id": str(candidate.id)},
        created_by=actor_id,
    ))
    await session.flush()


SECTION_LABELS = (
    ("Problem", "problem"),
    ("Preconditions", "preconditions"),
    ("Symptoms", "symptoms"),
    ("Root cause", "root_cause"),
    ("Resolution", "resolution"),
    ("Procedure", "procedure"),
    ("Verification", "verification"),
    ("Important pitfalls", "pitfalls"),
    ("Environment / version constraints", "environment_constraints"),
    ("Known uncertainty", "known_uncertainty"),
)


def _managed_section_body_json(
    child: KnowledgeNode,
    body: str,
) -> dict[str, Any]:
    """Copy managed-section metadata while projecting its reviewed body."""

    body_json = dict(child.body_json) if isinstance(child.body_json, dict) else {}
    if body:
        body_json["knowledge_capture_text"] = body
    else:
        body_json.pop("knowledge_capture_text", None)
    return body_json


async def _write_subtree(session: AsyncSession, *, parent: KnowledgeNode, project: Project, user_id: UUID, draft: Mapping[str, Any], source_refs: list[dict[str, Any]], existing: KnowledgeNode | None = None) -> tuple[KnowledgeNode, KnowledgeRevision | None]:
    docs = DocsGraphService(session)
    root = existing
    if root is None:
        root = await docs.create_node(docs_library_id=parent.docs_library_id, user_id=user_id, title=str(draft["title"]), parent=parent, project_id=project.id, source_refs=source_refs)
    else:
        if root.project_id != project.id or root.parent_id != parent.id:
            raise KnowledgeCapturePublishError("publication target is outside Project Docs")
        await docs.update_node(node=root, user_id=user_id, title=str(draft["title"]), source_refs=source_refs, change_summary="Resolution Knowledge Captureを更新")

    existing_children = list((await session.execute(select(KnowledgeNode).where(KnowledgeNode.parent_id == root.id, KnowledgeNode.archived_at.is_(None)).order_by(KnowledgeNode.sort_order, KnowledgeNode.id))).scalars().all())
    by_title = {str(child.title): child for child in existing_children}
    for label, key in SECTION_LABELS:
        value = draft.get(key)
        if isinstance(value, list):
            lines = []
            for item in value:
                if isinstance(item, Mapping):
                    lines.append(str(item.get("text") or "").strip())
                else:
                    lines.append(str(item or "").strip())
            body = "\n".join(line for line in lines if line)
        else:
            body = str(value or "").strip()
        child = by_title.get(label)
        if child is None:
            child = await docs.create_node(docs_library_id=root.docs_library_id, user_id=user_id, title=label, parent=root, project_id=project.id, source_refs=source_refs)
            by_title[label] = child
        elif str(child.title) != label:
            raise KnowledgeCapturePublishError("publication section identity changed")
        if body:
            body = _text_value(body, f"{key}.body")
        body_json = _managed_section_body_json(child, body)
        if not body:
            # Every managed section has a durable node identity, even when a
            # reviewed draft has no content for that section yet.  This keeps
            # the publication guard complete without including unmanaged
            # children in the guarded set.
            current_body_json = child.body_json if isinstance(child.body_json, dict) else {}
            if "knowledge_capture_text" in current_body_json:
                await docs.update_node(
                    node=child,
                    user_id=user_id,
                    body_json=body_json,
                    source_refs=source_refs,
                    change_summary="Resolution Knowledge Captureセクションをクリア",
                )
            continue
        # Docs stores content as child nodes.  Keep the section title as the
        # canonical node mirror and put a bounded representation in the
        # revision provenance/body metadata rather than violating that invariant.
        await docs.update_node(node=child, user_id=user_id, body_json=body_json, source_refs=source_refs, change_summary="Resolution Knowledge Captureセクションを保存")
    revision = await _latest_revision(session, root.id)
    return root, revision


async def _prepare_legacy_publication_subtree(
    session: AsyncSession,
    *,
    node_id: UUID,
    project_id: UUID,
    actor: UUID,
    source_refs: list[dict[str, Any]],
) -> KnowledgeNode:
    """Lock and complete missing managed identities before guard capture.

    Existing and unmanaged children are read/locked only; adoption never
    rewrites them.  Missing managed labels are materialized through the
    normal Docs service so its ACL, revision, and scope checks remain active.
    """

    root_result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.id == node_id,
            KnowledgeNode.archived_at.is_(None),
        )
        .with_for_update()
    )
    root = root_result.scalar_one_or_none()
    if (
        root is None
        or str(getattr(root, "project_id", None)) != str(project_id)
    ):
        raise KnowledgeCapturePublishConflict(
            "published Docs node is unavailable for this Project"
        )

    children_result = await session.execute(
        select(KnowledgeNode)
        .where(KnowledgeNode.parent_id == root.id)
        .order_by(KnowledgeNode.id)
        .with_for_update()
    )
    children = list(children_result.scalars().all())
    existing_managed: dict[str, KnowledgeNode] = {}
    for child in children:
        label = str(getattr(child, "title", "") or "")
        if label not in MANAGED_SECTION_LABELS:
            # Legacy/unmanaged children remain untouched and are intentionally
            # outside the strict managed publication guard.
            continue
        if (
            str(getattr(child, "project_id", None)) != str(project_id)
            or str(getattr(child, "docs_library_id", None))
            != str(getattr(root, "docs_library_id", None))
        ):
            raise KnowledgeCapturePublishConflict(
                "published Docs child is outside the Project Docs scope"
            )
        if getattr(child, "archived_at", None) is not None:
            raise KnowledgeCapturePublishConflict(
                "published Docs subtree contains an archived managed section"
            )
        if label in existing_managed:
            raise KnowledgeCapturePublishConflict(
                "published Docs subtree contains duplicate managed sections"
            )
        existing_managed[label] = child

    missing_labels = [
        label
        for label, _key in SECTION_LABELS
        if label not in existing_managed
    ]
    if missing_labels:
        docs = DocsGraphService(session)
        for label in missing_labels:
            await docs.create_node(
                docs_library_id=root.docs_library_id,
                user_id=actor,
                title=label,
                parent=root,
                project_id=project_id,
                source_refs=source_refs,
            )
    return root


async def adopt_publication_subtree_guard(
    session: AsyncSession,
    candidate_id: UUID | str,
    user_id: UUID | str,
    expected_version: int,
    *,
    actor_user_id: UUID | str | None = None,
    **_: Any,
) -> KnowledgeCaptureCandidate:
    """Explicitly adopt the current live Docs subtree for a legacy publication.

    This is a human-reviewed trust-boundary operation.  It may materialize
    missing managed section nodes through the normal Docs service, but never
    updates existing or unmanaged Docs children.  It records a fresh guard on
    a published candidate after re-checking the candidate CAS, Project ACL,
    and current Docs readability while holding the same Project publication
    lock used by normal publication.
    """

    actor = _uuid(actor_user_id or user_id, "user_id")
    candidate_uuid = _uuid(candidate_id, "candidate_id")
    result = await session.execute(
        select(KnowledgeCaptureCandidate)
        .where(KnowledgeCaptureCandidate.id == candidate_uuid)
        .with_for_update()
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise KnowledgeCapturePublishError("Knowledge Capture candidate not found")
    if int(candidate.version or 1) != int(expected_version):
        raise KnowledgeCapturePublishConflict("candidate version conflict")
    if str(candidate.status).casefold() != "published":
        raise KnowledgeCapturePublishConflict(
            "candidate publication guard adoption requires a published candidate"
        )

    project_id = _uuid(candidate.project_id, "project_id")
    await _lock_project(session, project_id)
    if not await ProjectRepository.has_permission(
        session,
        project_id=project_id,
        user_id=actor,
        permission="manage_settings",
    ):
        raise PermissionError("Project publication guard adoption permission denied")

    node_id = candidate.published_node_id
    if node_id is None:
        raise KnowledgeCapturePublishConflict(
            "published candidate has no publication node binding"
        )
    if candidate.target_node_id is not None and candidate.target_node_id != node_id:
        raise KnowledgeCapturePublishConflict("publication node binding changed")
    node = await session.get(KnowledgeNode, node_id)
    if (
        node is None
        or node.archived_at is not None
        or node.project_id != project_id
    ):
        raise KnowledgeCapturePublishConflict(
            "published Docs node is unavailable for this Project"
        )
    if not await can_read_node(session, node, actor):
        raise PermissionError("Docs publication target read permission denied")

    raw_draft = candidate.draft_json
    draft = dict(raw_draft) if isinstance(raw_draft, Mapping) else {}
    raw_publication = draft.get("publication")
    publication = dict(raw_publication) if isinstance(raw_publication, Mapping) else {}
    if validate_publication_subtree_guard(
        publication.get("subtree_guard"),
        node.id,
    ):
        raise KnowledgeCapturePublishConflict(
            "publication subtree guard already exists; review is not required"
        )

    try:
        node = await _prepare_legacy_publication_subtree(
            session,
            node_id=node.id,
            project_id=project_id,
            actor=actor,
            source_refs=_source_refs(candidate),
        )
        subtree_guard = await capture_publication_subtree_guard(
            session,
            node.id,
            lock=True,
            adoption_actor_id=actor,
        )
        if not validate_publication_subtree_guard(subtree_guard, node.id):
            raise KnowledgeCapturePublicationGuardError(
                "captured publication subtree guard is invalid"
            )
    except KnowledgeCapturePublicationGuardError as exc:
        raise KnowledgeCapturePublishConflict(
            "Docs publication subtree is incomplete; review is required"
        ) from exc

    now = datetime.utcnow()
    publication.update(
        {
            "subtree_guard": subtree_guard,
            "adopted_by": str(actor)[:64],
            "adopted_at": (_iso(now) or "")[:64],
        }
    )
    draft["publication"] = publication
    candidate.draft_json = draft
    candidate.version = int(candidate.version or 1) + 1
    candidate.updated_at = now
    await session.flush()
    return candidate


async def publish_candidate(
    session: AsyncSession,
    candidate_id: UUID | str,
    user_id: UUID | str,
    expected_version: int,
    *,
    actor_user_id: UUID | str | None = None,
    **_: Any,
) -> KnowledgeCaptureCandidate:
    """Publish one reviewed candidate using current ACL and Docs invariants."""

    actor = _uuid(actor_user_id or user_id, "user_id")
    candidate_uuid = _uuid(candidate_id, "candidate_id")
    result = await session.execute(select(KnowledgeCaptureCandidate).where(KnowledgeCaptureCandidate.id == candidate_uuid).with_for_update())
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise KnowledgeCapturePublishError("Knowledge Capture candidate not found")
    if int(candidate.version or 1) != int(expected_version):
        raise KnowledgeCapturePublishConflict("candidate version conflict")
    if str(candidate.status).casefold() not in {"draft_ready", "approved"}:
        raise KnowledgeCapturePublishConflict("candidate is not ready for publication")
    project_id = _uuid(candidate.project_id, "project_id")
    project = await _lock_project(session, project_id)
    if not await ProjectRepository.has_permission(session, project_id=project_id, user_id=actor, permission="write"):
        raise PermissionError("Project Docs publication permission denied")
    task = await _revalidate_seed(session, candidate, project_id)
    draft, semantic_key, source_refs = _normalize_draft(candidate)
    await _revalidate_cited_sources(
        session,
        candidate,
        draft,
        project_id=project_id,
        actor_id=actor,
    )
    canonical = await get_canonical_project_information_node(session, project_id=project_id, actor_user_id=actor)
    if canonical is None:
        # Owner-side bootstrap is an existing canonical Docs operation; it is
        # never replaced by a raw node insert here.
        from .project_information_docs import ensure_project_information_doc
        canonical = await ensure_project_information_doc(session, project=project, user_id=actor)
    candidate.knowledge_semantic_key = semantic_key

    existing_result = await session.execute(
        select(KnowledgeCaptureCandidate).where(
            KnowledgeCaptureCandidate.project_id == project_id,
            KnowledgeCaptureCandidate.knowledge_semantic_key == semantic_key,
            KnowledgeCaptureCandidate.status == "published",
            KnowledgeCaptureCandidate.published_node_id.is_not(None),
            KnowledgeCaptureCandidate.id != candidate.id,
        ).order_by(KnowledgeCaptureCandidate.updated_at.desc()).limit(1).with_for_update()
    )
    existing_candidate = existing_result.scalar_one_or_none()
    target_node: KnowledgeNode | None = None
    action = "create"
    if existing_candidate is not None and existing_candidate.published_node_id is not None:
        target_node = await session.get(KnowledgeNode, existing_candidate.published_node_id)
        if target_node is None:
            raise KnowledgeCapturePublishConflict("existing publication target is unavailable")
        action = "update"
        subtree_guard = extract_publication_subtree_guard(existing_candidate)
        if subtree_guard is None:
            raise KnowledgeCapturePublishConflict(
                "Docs target subtree guard is unavailable; review is required"
            )
        try:
            await assert_publication_subtree_unchanged(session, target_node.id, subtree_guard)
        except KnowledgeCapturePublicationGuardError as exc:
            raise KnowledgeCapturePublishConflict(
                "Docs target subtree changed; review is required"
            ) from exc
        current_revision = await _latest_revision(session, target_node.id)
        expected_revision = candidate.target_revision_id or existing_candidate.published_revision_id
        if expected_revision is not None and (current_revision is None or current_revision.id != expected_revision):
            raise KnowledgeCapturePublishConflict("Docs target changed; review is required")
    elif candidate.target_node_id is not None:
        target_node = await session.get(KnowledgeNode, candidate.target_node_id)
        if target_node is None or target_node.project_id != project_id:
            raise KnowledgeCapturePublishConflict("publication target is unavailable")
        action = "update"
        subtree_guard = extract_publication_subtree_guard(candidate)
        if subtree_guard is None:
            raise KnowledgeCapturePublishConflict(
                "Docs target subtree guard is unavailable; review is required"
            )
        try:
            await assert_publication_subtree_unchanged(session, target_node.id, subtree_guard)
        except KnowledgeCapturePublicationGuardError as exc:
            raise KnowledgeCapturePublishConflict(
                "Docs target subtree changed; review is required"
            ) from exc
        current_revision = await _latest_revision(session, target_node.id)
        if candidate.target_revision_id is not None and (current_revision is None or current_revision.id != candidate.target_revision_id):
            raise KnowledgeCapturePublishConflict("Docs target changed; review is required")

    no_change = bool(
        existing_candidate is not None
        and target_node is not None
        and candidate.evidence_digest
        and existing_candidate.evidence_digest == candidate.evidence_digest
    )
    if no_change:
        node = target_node
        revision = await _latest_revision(session, node.id)
        action = "no_change"
    else:
        node, revision = await _write_subtree(
            session,
            parent=canonical,
            project=project,
            user_id=actor,
            draft=draft,
            source_refs=source_refs,
            existing=target_node,
        )
    await _ensure_task_reference(session, task, node, candidate, actor)
    try:
        subtree_guard = await capture_publication_subtree_guard(session, node.id, lock=True)
    except KnowledgeCapturePublicationGuardError as exc:
        raise KnowledgeCapturePublishError(
            "Docs publication subtree guard could not be recorded"
        ) from exc
    candidate.target_node_id = node.id
    candidate.published_node_id = node.id
    candidate.target_revision_id = revision.id if revision is not None else None
    candidate.published_revision_id = revision.id if revision is not None else None
    candidate.draft_json = {
        **dict(candidate.draft_json or {}),
        "publication": {
            "action": action,
            "target_node_id": str(node.id),
            "target_revision_id": str(revision.id) if revision else None,
            "subtree_guard": subtree_guard,
        },
    }
    candidate.status = "published"
    candidate.version = int(candidate.version or 1) + 1
    candidate.completed_at = datetime.utcnow()
    candidate.updated_at = datetime.utcnow()
    candidate.lease_owner = None
    candidate.lease_token = None
    candidate.lease_expires_at = None
    candidate.heartbeat_at = None
    await session.flush()
    return candidate


publish = publish_candidate
publish_knowledge_capture = publish_candidate


class KnowledgeCapturePublisher:
    adopt_publication_subtree_guard = staticmethod(adopt_publication_subtree_guard)
    publish_candidate = staticmethod(publish_candidate)
    publish = staticmethod(publish_candidate)


__all__ = [
    "KnowledgeCapturePublishConflict",
    "KnowledgeCapturePublishError",
    "KnowledgeCapturePublisher",
    "MAX_CANDIDATE_EVIDENCE_REFS",
    "MAX_PUBLICATION_SOURCE_REFS",
    "adopt_publication_subtree_guard",
    "publish",
    "publish_candidate",
    "publish_knowledge_capture",
]
