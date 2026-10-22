"""Regenerate an existing Inbox item's document and reply draft from kept sources.

Recovery path for Inbox items whose generation fell back to the fixed
``fallback_inbox_document`` / "（返信案を作成できませんでした）" because the model
output was empty or truncated.  ``source_key`` deduplication means a resend
cannot fix such an item, so this module rebuilds it in place:

* sources are read from what the item already keeps (instruction field,
  archived mail / mail-message nodes, inline-cited Docs nodes); none of them
  is ever modified,
* the model is called through ``generate`` and an empty or truncated body is
  a :class:`ModelOutputIncompleteError` (nothing is replaced),
* writes go through ``WorkIntakeDocsService.replace_document`` (Docs writer:
  search index, edges, revisions) and ``TaskManagementService.update_task``.

A reply draft is regenerated only when the bound Task description already has
a ``### 返信文ドラフト`` section; items without that section never gain one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable
from uuid import UUID

from sqlalchemy import Text, cast, select

from ..llm.generation_error import ModelOutputIncompleteError
from ..memory.models import (
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeRevision,
    Task,
    TaskReference,
)
from .inbox_document_service import (
    InboxDocument,
    InboxSourceMaterial,
    build_inbox_document_prompt,
    build_inbox_reply_draft_prompt,
    parse_inbox_document,
    parse_inbox_reply_draft,
)
from .work_intake_docs_service import INBOX_ITEM_SYSTEM_PREFIX, WorkIntakeDocsService

GenerateText = Callable[[str], Awaitable[str]]

REPLY_DRAFT_HEADING = "### 返信文ドラフト"
REPLY_DRAFT_PLACEHOLDER = "（返信案を作成できませんでした）"
_REPLY_SECTION_RE = re.compile(
    r"(?ms)^###[ \t]*返信文ドラフト[ \t]*\n(?P<body>.*?)(?=^#{1,6}[ \t]+\S|\Z)"
)
_NODE_LINK_RE = re.compile(r"\[\[node:([0-9a-fA-F-]{36})(?:\|[^\]]*)?\]\]")
_RESEARCH_CONTENT_LIMIT = 4000


class InboxRegenerationError(RuntimeError):
    """The target or its sources cannot be regenerated safely."""


@dataclass(frozen=True)
class InboxRegenerationContext:
    item_id: UUID
    project_id: UUID
    title: str
    instruction: str
    current_summary: str
    materials: tuple[InboxSourceMaterial, ...]
    expected_revision: str
    source_refs: tuple[dict[str, Any], ...] = ()
    task_id: UUID | None = None
    task_title: str = ""
    task_description: str = ""
    default_user_id: UUID | None = None

    @property
    def reply_draft(self) -> str | None:
        return reply_draft_section(self.task_description)

    @property
    def source_node_ids(self) -> set[UUID]:
        return {m.node_id for m in self.materials if m.node_id is not None}


@dataclass(frozen=True)
class InboxRegenerationResult:
    document: InboxDocument
    reply_draft: str | None = None
    reply_draft_skipped_reason: str = ""


@dataclass
class InboxRegenerationReport:
    context: InboxRegenerationContext
    result: InboxRegenerationResult | None = None
    applied: bool = False
    notes: list[str] = field(default_factory=list)


def reply_draft_section(description: str) -> str | None:
    """Return the current reply draft body, or None when the section is absent."""

    match = _REPLY_SECTION_RE.search(str(description or ""))
    if match is None:
        return None
    return match.group("body").strip()


def replace_reply_draft_section(description: str, draft: str) -> str | None:
    """Replace only the reply draft body; None when there is no such section."""

    text = str(description or "")
    match = _REPLY_SECTION_RE.search(text)
    if match is None:
        return None
    body = f"\n{str(draft).strip()}\n\n"
    if match.end("body") >= len(text):
        body = body.rstrip("\n") + "\n"
    return text[: match.start("body")] + body + text[match.end("body") :]


def build_regeneration_materials(
    *,
    instruction: str,
    conversation_key: str,
    mails: Iterable[tuple[KnowledgeNode, dict[str, str], list[tuple[KnowledgeNode, dict[str, str]]]]],
    cited_nodes: Iterable[KnowledgeNode] = (),
) -> list[InboxSourceMaterial]:
    """Rebuild the /inbox ``document_sources`` order from stored nodes.

    ``mails`` yields ``(raw_mail_node, raw_fields, [(message_node, fields)])``.
    Message nodes carry the original ``source_key`` in ``body_json`` so the
    regenerated citations resolve to the same nodes as the first run.
    """

    materials: list[InboxSourceMaterial] = []
    if str(instruction or "").strip():
        materials.append(
            InboxSourceMaterial(
                key=conversation_key,
                title="チャットでの受付指示",
                content=str(instruction).strip(),
                kind="conversation",
            )
        )
    used: set[UUID] = set()
    for raw_node, raw_fields, messages in mails:
        used.add(raw_node.id)
        ordered = sorted(
            messages,
            key=lambda pair: int((pair[0].body_json or {}).get("sequence") or 0),
        )
        for message_node, fields in ordered:
            used.add(message_node.id)
            body_json = message_node.body_json or {}
            materials.append(
                InboxSourceMaterial(
                    key=str(body_json.get("source_key") or message_node.id),
                    node_id=message_node.id,
                    title=message_node.title,
                    date=fields.get("email_message_date", ""),
                    sender=fields.get("email_message_from", ""),
                    content=fields.get("email_message_body", ""),
                    kind="email_message",
                )
            )
        if not ordered:
            materials.append(
                InboxSourceMaterial(
                    key=str(raw_node.id),
                    node_id=raw_node.id,
                    title=str(raw_fields.get("email_subject") or raw_node.title or "メール原本"),
                    date=raw_fields.get("email_date", ""),
                    sender=raw_fields.get("email_from", ""),
                    content=raw_fields.get("email_body", ""),
                    kind="email",
                )
            )
    for index, node in enumerate(cited_nodes):
        if node.id in used:
            continue
        used.add(node.id)
        materials.append(
            InboxSourceMaterial(
                key=f"research:{index}",
                node_id=node.id,
                title=f"DocsノードID: {node.id}",
                content=str(node.body_text or node.title or "")[:_RESEARCH_CONTENT_LIMIT],
                kind="research",
            )
        )
    return materials


async def _field_map(session, node_id: UUID) -> dict[str, str]:
    result = await session.execute(
        select(KnowledgeField.system_key, KnowledgeFieldValue.value_text)
        .join(KnowledgeFieldValue, KnowledgeFieldValue.field_id == KnowledgeField.id)
        .where(KnowledgeFieldValue.node_id == node_id)
    )
    return {str(key): str(value or "") for key, value in result.all() if key}


def _is_inbox_item_root(node: KnowledgeNode | None) -> bool:
    key = str(getattr(node, "system_key", "") or "")
    return key.startswith(f"{INBOX_ITEM_SYSTEM_PREFIX}:") and len(key.split(":")) == 3


async def find_inbox_item_ids(
    session,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
) -> list[UUID]:
    """Resolve Inbox item ids from the receipt ``source_refs`` of their revisions."""

    needles: list[str] = []
    for value in (session_id, run_id):
        text = str(value or "").strip().lower()
        if not text:
            continue
        # Full UUIDs or short prefixes (e.g. ``843f468b``) are matched as a
        # substring of the receipt's serialized source_refs.
        if len(text) < 8 or any(ch not in "0123456789abcdef-" for ch in text):
            raise InboxRegenerationError(f"IDは8文字以上のUUID(接頭辞)で指定してください: {value}")
        needles.append(text)
    if not needles:
        return []
    statement = (
        select(KnowledgeNode)
        .join(KnowledgeRevision, KnowledgeRevision.node_id == KnowledgeNode.id)
        .where(
            KnowledgeNode.archived_at.is_(None),
            KnowledgeNode.system_key.like(f"{INBOX_ITEM_SYSTEM_PREFIX}:%"),
        )
    )
    for needle in needles:
        statement = statement.where(
            cast(KnowledgeRevision.source_refs_json, Text).contains(needle)
        )
    nodes = (await session.execute(statement)).scalars().all()
    return list(dict.fromkeys(node.id for node in nodes if _is_inbox_item_root(node)))


async def _receipt_source_refs(session, item_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        select(KnowledgeRevision.source_refs_json)
        .where(KnowledgeRevision.node_id == item_id)
        .order_by(KnowledgeRevision.created_at)
    )
    for refs in result.scalars().all():
        if isinstance(refs, list) and refs:
            return [dict(ref) for ref in refs if isinstance(ref, dict)]
    return []


async def load_inbox_regeneration_context(
    session,
    *,
    item_id: UUID,
) -> InboxRegenerationContext:
    """Read the item and its kept sources.  Performs no writes."""

    item = await session.get(KnowledgeNode, item_id)
    if item is None or item.archived_at is not None or not _is_inbox_item_root(item):
        raise InboxRegenerationError(f"Inbox項目が見つかりません: {item_id}")
    project_id = item.project_id
    fields = await _field_map(session, item.id)
    source_refs = await _receipt_source_refs(session, item.id)
    message_ref = next(
        (str(ref.get("id")) for ref in source_refs if ref.get("type") == "conversation_message" and ref.get("id")),
        "",
    )
    conversation_key = f"conversation:{message_ref or 'current'}"

    # Every live node of the item subtree (generated document, inline
    # sources, attachment links).  Their cited targets are the kept sources.
    project_nodes = list(
        (
            await session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.project_id == project_id,
                    KnowledgeNode.archived_at.is_(None),
                )
            )
        ).scalars().all()
    )
    nodes_by_id = {node.id: node for node in project_nodes}
    children: dict[UUID | None, list[KnowledgeNode]] = {}
    for node in project_nodes:
        children.setdefault(node.parent_id, []).append(node)
    subtree: set[UUID] = set()
    frontier = [item.id]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child.id not in subtree:
                subtree.add(child.id)
                frontier.append(child.id)

    cited_ids: list[UUID] = []
    for node_id in subtree:
        node = nodes_by_id[node_id]
        if (node.body_json or {}).get("format") == "work_intake_inline_source":
            cited_ids.extend(UUID(match) for match in _NODE_LINK_RE.findall(node.title or ""))
    if subtree:
        edge_targets = (
            await session.execute(
                select(KnowledgeEdge.target_node_id).where(
                    KnowledgeEdge.source_node_id.in_(subtree),
                    KnowledgeEdge.relation_type == "inline_ref",
                )
            )
        ).scalars().all()
        cited_ids.extend(edge_targets)
    attachment_paths = {
        str(ref.get("path"))
        for ref in source_refs
        if ref.get("type") == "workspace_file" and ref.get("path")
    }

    task = (
        await session.execute(
            select(Task)
            .where(Task.knowledge_node_id == item.id, Task.deleted_at.is_(None))
            .order_by(Task.created_at)
        )
    ).scalars().first()
    if task is not None:
        refs = (
            await session.execute(
                select(TaskReference.target_id).where(
                    TaskReference.task_id == task.id,
                    TaskReference.reference_type == "docs_node",
                    TaskReference.relation_type == "source",
                )
            )
        ).scalars().all()
        for value in refs:
            try:
                cited_ids.append(UUID(str(value)))
            except (TypeError, ValueError):
                continue

    raw_mail_ids: list[UUID] = []
    other_ids: list[UUID] = []
    for node_id in dict.fromkeys(cited_ids):
        node = nodes_by_id.get(node_id)
        if node is None or node_id in subtree or node_id == item.id:
            continue
        key = str(node.system_key or "")
        if key.startswith("project_mail_message:"):
            if node.parent_id in nodes_by_id:
                raw_mail_ids.append(node.parent_id)
        elif key.startswith("project_mail:"):
            raw_mail_ids.append(node.id)
        elif not key.startswith(f"{INBOX_ITEM_SYSTEM_PREFIX}:"):
            other_ids.append(node.id)
    if attachment_paths:
        for node in project_nodes:
            if str(node.system_key or "").startswith("project_mail:") and node.id not in raw_mail_ids:
                raw_fields = await _field_map(session, node.id)
                if raw_fields.get("email_source_path") in attachment_paths:
                    raw_mail_ids.append(node.id)

    mails: list[tuple[KnowledgeNode, dict[str, str], list[tuple[KnowledgeNode, dict[str, str]]]]] = []
    for raw_id in dict.fromkeys(raw_mail_ids):
        raw = nodes_by_id[raw_id]
        messages = [
            (child, await _field_map(session, child.id))
            for child in children.get(raw.id, [])
            if str(child.system_key or "").startswith("project_mail_message:")
        ]
        mails.append((raw, await _field_map(session, raw.id), messages))

    materials = build_regeneration_materials(
        instruction=fields.get("inbox_instruction", ""),
        conversation_key=conversation_key,
        mails=mails,
        cited_nodes=[nodes_by_id[node_id] for node_id in other_ids],
    )
    if not materials:
        raise InboxRegenerationError("再生成に使える元資料がありません。")
    expected_revision = await WorkIntakeDocsService(session).document_revision(item)
    return InboxRegenerationContext(
        item_id=item.id,
        project_id=project_id,
        title=str(item.title or ""),
        instruction=fields.get("inbox_instruction", ""),
        current_summary=fields.get("inbox_summary", ""),
        materials=tuple(materials),
        expected_revision=expected_revision,
        source_refs=tuple(source_refs),
        task_id=task.id if task is not None else None,
        task_title=str(task.title or "") if task is not None else "",
        task_description=str(task.description or "") if task is not None else "",
        default_user_id=item.updated_by or item.created_by,
    )


async def _complete_text(generate: GenerateText, prompt: str, request_type: str) -> str:
    text = await generate(prompt)
    if not str(text or "").strip():
        # Defensive: a generator that does not enforce completeness itself
        # must still never let an empty body through as a document.
        raise ModelOutputIncompleteError(
            "empty_output", transport="inbox_regeneration", request_type=request_type
        )
    return str(text)


async def generate_inbox_regeneration(
    context: InboxRegenerationContext,
    generate: GenerateText,
    *,
    regenerate_draft: bool = True,
    replace_existing_draft: bool = False,
) -> InboxRegenerationResult:
    """Generate the replacement document (and draft).  No writes.

    ``ModelOutputIncompleteError`` and parse errors propagate: the caller must
    not replace anything when generation did not produce a complete artifact.
    """

    allowed_keys = [material.key for material in context.materials]
    # The Task summary (without the draft itself) is the original
    # ``action_result``; the stored overview may be the fixed fallback, so it
    # is deliberately not fed back as ``current_document``.
    task_context = re.split(
        r"(?m)^###[ \t]*返信文ドラフト", context.task_description, maxsplit=1
    )[0].strip()
    document = parse_inbox_document(
        await _complete_text(
            generate,
            build_inbox_document_prompt(
                instruction=context.instruction,
                sources=context.materials,
                current_document="",
                action_result=task_context,
            ),
            "inbox_regenerate_document",
        ),
        allowed_source_keys=allowed_keys,
    )
    current_draft = context.reply_draft
    if not regenerate_draft:
        return InboxRegenerationResult(document, None, "返信案の再生成は指定されていません。")
    if current_draft is None:
        return InboxRegenerationResult(document, None, "返信文ドラフト欄が無い項目のため追加しません。")
    if current_draft and current_draft != REPLY_DRAFT_PLACEHOLDER and not replace_existing_draft:
        return InboxRegenerationResult(document, None, "既存の返信案があるため保持します。")
    draft = parse_inbox_reply_draft(
        await _complete_text(
            generate,
            build_inbox_reply_draft_prompt(
                instruction=context.instruction,
                sources=context.materials,
                current_document=document.summary_text(),
                task_context=task_context,
            ),
            "inbox_regenerate_reply_draft",
        )
    )
    return InboxRegenerationResult(document, draft)


async def apply_inbox_regeneration(
    session,
    context: InboxRegenerationContext,
    result: InboxRegenerationResult,
    *,
    user_id: UUID,
    docs_service: Any = None,
    task_service: Any = None,
) -> None:
    """Write the regenerated document/draft in one transaction."""

    docs = docs_service or WorkIntakeDocsService(session)
    if task_service is None:
        from .task_management_service import TaskManagementService

        task_service = TaskManagementService()
    try:
        await docs.replace_document(
            item_id=context.item_id,
            project_id=context.project_id,
            user_id=user_id,
            document=result.document,
            source_nodes={
                material.key: material.node_id
                for material in context.materials
                if material.node_id is not None
            },
            status="",
            source_refs=list(context.source_refs),
            expected_revision=context.expected_revision,
        )
        if result.reply_draft is not None and context.task_id is not None:
            description = replace_reply_draft_section(
                context.task_description, result.reply_draft
            )
            if description is not None and description != context.task_description:
                await task_service.update_task(
                    session,
                    user_id=user_id,
                    task_id=context.task_id,
                    updates={"description": description},
                    commit=False,
                )
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def regenerate_inbox_item(
    session,
    *,
    item_id: UUID,
    generate: GenerateText | None,
    apply: bool = False,
    user_id: UUID | None = None,
    regenerate_draft: bool = True,
    replace_existing_draft: bool = False,
    context_loader: Callable[..., Awaitable[InboxRegenerationContext]] | None = None,
    docs_service: Any = None,
    task_service: Any = None,
) -> InboxRegenerationReport:
    """Dry-run by default.  ``generate=None`` skips the model call entirely."""

    loader = context_loader or load_inbox_regeneration_context
    context = await loader(session, item_id=item_id)
    report = InboxRegenerationReport(context=context)
    if generate is None:
        if apply:
            raise InboxRegenerationError("--apply にはモデル生成が必要です。")
        report.notes.append("モデル生成は行っていません(--generate で生成プレビュー)。")
        return report
    report.result = await generate_inbox_regeneration(
        context,
        generate,
        regenerate_draft=regenerate_draft,
        replace_existing_draft=replace_existing_draft,
    )
    if report.result.reply_draft_skipped_reason:
        report.notes.append(report.result.reply_draft_skipped_reason)
    if not apply:
        await session.rollback()
        report.notes.append("dry-run: 書込みは行っていません。")
        return report
    actor = user_id or context.default_user_id
    if actor is None:
        raise InboxRegenerationError("書込みユーザーを特定できません。--user-id を指定してください。")
    await apply_inbox_regeneration(
        session,
        context,
        report.result,
        user_id=actor,
        docs_service=docs_service,
        task_service=task_service,
    )
    report.applied = True
    return report
