"""日次インテーク(Daily Intake): その日の作業メモを構造化し案件情報へ反映する。

ユーザーがその日やったことを雑な自由文で書く→LLMで8軸に構造化する。
不明点があれば逆質問(clarifying_questions)を返して保存しない。
情報が揃えば整理案(draft)を提示し、ユーザーOKで既存の
Docs正本 / Q&A / タスク / record table へ反映する。

雛形: project_information_organizer.py。LLM呼び出し・JSON抽出・Docs反映の
パターンをそのまま流用している。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.project_management.common import (
    _materialize_record_row,
    _normalize_task_schedule_inputs,
)
from ..memory.models import (
    Project,
    ProjectQaEntry,
    RecordField,
    RecordRow,
    RecordTable,
    RecordView,
)
from .project_information_docs import (
    PROJECT_INFORMATION_SECTIONS,
    ensure_project_information_doc,
    update_project_information_doc,
)
from .project_information_organizer import (
    LLMUsageContext,
    _apply_llm_usage_context,
    _parse_json_object,
)
from .project_qa_candidate_service import (
    _normalized_question_hash,
    find_existing_project_qa_entry,
    is_project_qa_entry_closed,
)
from .task_management_service import TaskManagementService

logger = logging.getLogger(__name__)

# record table「作業記録」の列定義。列=日付/種別/内容。
WORK_RECORD_TABLE_NAME = "作業記録"
WORK_RECORD_FIELDS: list[dict[str, Any]] = [
    {"key": "date", "label": "日付", "field_type": "date", "sort_order": 0, "is_due": True},
    {"key": "kind", "label": "種別", "field_type": "text", "sort_order": 1},
    {"key": "content", "label": "内容", "field_type": "long_text", "sort_order": 2, "is_title": True},
]

_PRIORITY_ALLOWED = {"low", "medium", "high", "urgent"}


def _clean_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else f"{value[:limit].rstrip()}..."


def _string_list(value: Any, *, limit: int = 40, item_limit: int = 2000) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text:
            out.append(_clip(text, item_limit))
        if len(out) >= limit:
            break
    return out


def _safe_section(value: Any, default: str = "概要") -> str:
    text = _clean_text(value, default)
    return text if text in PROJECT_INFORMATION_SECTIONS else default


def _safe_priority(value: Any, default: str = "medium") -> str:
    text = _clean_text(value, default).lower()
    return text if text in _PRIORITY_ALLOWED else default


@dataclass
class IntakeTask:
    title: str
    description: str = ""
    due_date: str = ""
    priority: str = "medium"


@dataclass
class IntakeDocUpdate:
    content: str
    section_heading: str = "概要"
    source_ref: str = ""


@dataclass
class DailyIntakeDraft:
    intake_date: str
    raw_input: str
    summary_md: str = ""
    done_items: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    confirmations: list[str] = field(default_factory=list)
    inquiries: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    task_candidates: list[IntakeTask] = field(default_factory=list)
    docs_updates: list[IntakeDocUpdate] = field(default_factory=list)
    clarifying_questions: list[str] = field(default_factory=list)
    generated_by: str = "llm"


def _draft_to_dict(draft: DailyIntakeDraft) -> dict[str, Any]:
    return {
        "intake_date": draft.intake_date,
        "raw_input": draft.raw_input,
        "summary_md": draft.summary_md,
        "done_items": list(draft.done_items),
        "decisions": list(draft.decisions),
        "confirmations": list(draft.confirmations),
        "inquiries": list(draft.inquiries),
        "issues": list(draft.issues),
        "task_candidates": [item.__dict__ for item in draft.task_candidates],
        "docs_updates": [item.__dict__ for item in draft.docs_updates],
        "clarifying_questions": list(draft.clarifying_questions),
        "generated_by": draft.generated_by,
    }


def _draft_from_llm_json(
    value: dict[str, Any],
    *,
    intake_date: str = "",
    raw_input: str = "",
) -> DailyIntakeDraft:
    """LLMまたはフロントから渡ったJSONを堅牢にdraftへ変換する。"""

    tasks: list[IntakeTask] = []
    for item in value.get("task_candidates", []):
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"))
        if not title:
            continue
        tasks.append(
            IntakeTask(
                title=_clip(title, 500),
                description=_clip(_clean_text(item.get("description")), 4000),
                due_date=_clean_text(item.get("due_date")),
                priority=_safe_priority(item.get("priority")),
            )
        )

    docs_updates: list[IntakeDocUpdate] = []
    for item in value.get("docs_updates", []):
        if not isinstance(item, dict):
            continue
        content = _clean_text(item.get("content"))
        if not content:
            continue
        docs_updates.append(
            IntakeDocUpdate(
                content=_clip(content, 8000),
                section_heading=_safe_section(item.get("section_heading")),
                source_ref=_clip(_clean_text(item.get("source_ref")), 1000),
            )
        )

    return DailyIntakeDraft(
        intake_date=_clean_text(value.get("intake_date"), intake_date),
        raw_input=_clean_text(value.get("raw_input"), raw_input),
        summary_md=_clip(_clean_text(value.get("summary_md")), 4000),
        done_items=_string_list(value.get("done_items")),
        decisions=_string_list(value.get("decisions")),
        confirmations=_string_list(value.get("confirmations")),
        inquiries=_string_list(value.get("inquiries")),
        issues=_string_list(value.get("issues")),
        task_candidates=tasks,
        docs_updates=docs_updates,
        clarifying_questions=_string_list(value.get("clarifying_questions")),
        generated_by=_clean_text(value.get("generated_by"), "llm"),
    )


def _build_prompt(project_name: str, raw_input: str, clarification_answers: str, intake_date: str) -> str:
    answers_block = (
        f"\n補足回答(前回の逆質問への回答):\n{clarification_answers.strip()}\n"
        if clarification_answers.strip()
        else ""
    )
    return f"""
あなたは案件運営を支援する日次インテーク整理エージェントです。
プロジェクト「{project_name}」について、ユーザーがその日やったことを雑に書いた自由文を読み、
8軸に構造化してください。

対象日: {intake_date}

自由文:
{raw_input.strip()}
{answers_block}
重要:
- 事実として確定していないこと、担当・期日・対象が曖昧でDocs正本やタスクに書くと誤りになるものは、
  勝手に補完せず clarifying_questions に日本語の質問として並べてください。
- clarifying_questions が非空のときは保存しません。逆に情報が十分なら clarifying_questions は空にしてください。
- decisions は案件として決まったこと、confirmations は相手に確認して確定した事項、
  issues は課題・懸念、inquiries は自分から相手へ問い合わせて回答待ちの事項です。
- docs_updates は案件情報Docs正本へ追記すべき恒久的な事実です。section_heading は次のいずれか:
  {"/".join(PROJECT_INFORMATION_SECTIONS)}
- task_candidates は今後やるべき作業。due_date は分かる場合のみ YYYY-MM-DD。priority は low/medium/high/urgent。
- done_items はその日実施した作業の記録(作業ログ)です。

返答はJSONのみ:
{{
  "summary_md": "その日の短い要約",
  "done_items": ["実施した作業"],
  "decisions": ["決定事項"],
  "confirmations": ["確認して確定した事項"],
  "inquiries": ["相手へ問い合わせ中で回答待ちの事項"],
  "issues": ["課題・懸念"],
  "task_candidates": [
    {{"title": "作業名", "description": "詳細", "due_date": "YYYY-MM-DD", "priority": "medium"}}
  ],
  "docs_updates": [
    {{"content": "Docs正本へ追記する恒久的事実", "section_heading": "概要", "source_ref": ""}}
  ],
  "clarifying_questions": ["曖昧で確認が必要な点の質問"]
}}
""".strip()


async def _generate_draft_with_llm(
    config: Any,
    project_name: str,
    *,
    raw_input: str,
    clarification_answers: str,
    intake_date: str,
    usage_context: LLMUsageContext | Any = None,
) -> DailyIntakeDraft | None:
    prompt = _build_prompt(project_name, raw_input, clarification_answers, intake_date)

    def _generate() -> str:
        from ..llm.manager import create_llm_client

        client = create_llm_client(config)
        _apply_llm_usage_context(client, usage_context)
        return client.generate_response(prompt, stream=False)

    try:
        response = await asyncio.to_thread(_generate)
    except Exception as exc:
        logger.warning("Daily intake LLM generation failed: %s", exc)
        return None

    parsed = _parse_json_object(response)
    if not parsed:
        return None
    return _draft_from_llm_json(parsed, intake_date=intake_date, raw_input=raw_input)


async def _ensure_work_record_table(
    session: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
) -> tuple[RecordTable, list[RecordField]]:
    """作業記録テーブル(列=日付/種別/内容)を取得、無ければ作成する。"""

    table_result = await session.execute(
        select(RecordTable)
        .where(
            RecordTable.project_id == project_id,
            RecordTable.name == WORK_RECORD_TABLE_NAME,
            RecordTable.deleted_at.is_(None),
        )
        .limit(1)
    )
    table = table_result.scalar_one_or_none()
    if table is None:
        table = RecordTable(
            project_id=project_id,
            name=WORK_RECORD_TABLE_NAME,
            description="日次インテークで記録した作業ログ。",
            sort_order=0,
            memory_policy="project_only",
            default_sensitivity="normal",
            created_by=user_id,
            table_metadata={"source": "daily_intake"},
        )
        session.add(table)
        await session.flush()
        session.add(
            RecordView(
                table_id=table.id,
                name="Grid",
                view_type="grid",
                config={},
                sort_order=0,
                created_by=user_id,
            )
        )

    fields_result = await session.execute(
        select(RecordField)
        .where(
            RecordField.table_id == table.id,
            RecordField.deleted_at.is_(None),
        )
        .order_by(RecordField.sort_order, RecordField.created_at)
    )
    fields = list(fields_result.scalars().all())
    existing_keys = {item.key for item in fields}
    missing = [item for item in WORK_RECORD_FIELDS if item["key"] not in existing_keys]
    if missing:
        for field_def in missing:
            record_field = RecordField(
                table_id=table.id,
                key=field_def["key"],
                label=field_def["label"],
                field_type=field_def["field_type"],
                sort_order=field_def["sort_order"],
                is_title=bool(field_def.get("is_title")),
                is_due=bool(field_def.get("is_due")),
                options={},
            )
            session.add(record_field)
            fields.append(record_field)
        await session.flush()
    return table, fields


async def apply_daily_intake(
    session: AsyncSession,
    *,
    project_id: UUID,
    project_name: str,
    user_id: UUID,
    draft: DailyIntakeDraft,
) -> dict[str, Any]:
    """draftを既存の Docs正本 / Q&A / タスク / record table へ反映する。"""

    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError("Project not found.")

    intake_date = draft.intake_date
    source_refs = [{"type": "daily_intake", "date": intake_date}]
    change_summary = f"日次インテーク({intake_date})を案件情報へ反映"

    node = await ensure_project_information_doc(session, project=project, user_id=user_id)

    # 決定事項 / 確認事項 / 課題管理 → Docs正本の該当セクションへ追記
    section_axis = [
        ("決定事項", draft.decisions),
        ("確認事項", draft.confirmations),
        ("課題管理", draft.issues),
    ]
    for section_heading, items in section_axis:
        if not items:
            continue
        append_text = "\n".join(f"- {item}" for item in items)
        node = await update_project_information_doc(
            session,
            project=project,
            user_id=user_id,
            section_heading=section_heading,
            append_text=append_text,
            operation="append",
            change_summary=change_summary,
            source_refs=source_refs,
        )

    # Docs反映候補 → 指定セクションへ追記
    for item in draft.docs_updates:
        node = await update_project_information_doc(
            session,
            project=project,
            user_id=user_id,
            section_heading=item.section_heading,
            append_text=item.content,
            operation="append",
            change_summary=change_summary,
            source_refs=source_refs,
        )

    # 問い合わせ → ProjectQaEntry(status=unanswered, review_state=candidate) として登録
    now = datetime.utcnow()
    inquiries_created = 0
    for question in draft.inquiries:
        question_hash = _normalized_question_hash(question)
        entry = await find_existing_project_qa_entry(
            session,
            project_id=project_id,
            question=question,
            question_hash=question_hash,
        )
        if entry is None:
            session.add(
                ProjectQaEntry(
                    project_id=project_id,
                    knowledge_node_id=node.id,
                    question=question,
                    answer=None,
                    normalized_question_hash=question_hash,
                    status="unanswered",
                    review_state="candidate",
                    confidence=0.65,
                    asked_count=1,
                    source_message_ids=[],
                    created_by=user_id,
                    updated_by=user_id,
                    created_by_agent=True,
                )
            )
            inquiries_created += 1
        elif is_project_qa_entry_closed(entry):
            continue
        else:
            entry.asked_count = int(entry.asked_count or 0) + 1
            entry.last_asked_at = now
            entry.updated_at = now
            entry.updated_by = user_id

    # タスク候補 → TaskManagementService.create_task
    task_service = TaskManagementService()
    tasks_created = 0
    task_payloads: list[dict[str, Any]] = []
    for task in draft.task_candidates:
        start_at, end_at, all_day = _normalize_task_schedule_inputs(due_date=task.due_date)
        task_payload = await task_service.create_task(
            session,
            user_id=user_id,
            project_id=project_id,
            title=task.title,
            description=task.description or None,
            priority=task.priority,
            start_at=start_at,
            end_at=end_at,
            all_day=all_day,
            source="daily_intake",
            commit=False,
        )
        task_payloads.append(task_payload)
        tasks_created += 1

    # 実施事項 → record table「作業記録」へ行追記
    record_rows_created = 0
    if draft.done_items:
        table, fields = await _ensure_work_record_table(
            session,
            project_id=project_id,
            user_id=user_id,
        )
        for content in draft.done_items:
            values = {"date": intake_date, "kind": "作業", "content": content}
            materialized = _materialize_record_row(values, fields)
            session.add(
                RecordRow(
                    table_id=table.id,
                    project_id=project_id,
                    created_by=user_id,
                    values=values,
                    title=materialized["title"],
                    search_text=materialized["search_text"],
                    sensitivity=table.default_sensitivity or "normal",
                    row_metadata={"source": "daily_intake", "date": intake_date},
                )
            )
            record_rows_created += 1

    await session.commit()
    # Daily intake mutates the canonical Project Information Docs in the
    # transaction above.  Rebuild scheduling happens only after that commit;
    # a queue outage must not turn a successful intake into an API failure.
    from .project_context_pack_job_service import enqueue_project_context_pack_rebuild

    try:
        await enqueue_project_context_pack_rebuild(
            project_id,
            user_id,
            "daily_intake_applied",
        )
    except Exception:
        logger.exception("Failed to enqueue ProjectContextPack rebuild after daily intake")
    for task_payload in task_payloads:
        await task_service._broadcast("task_created", task_payload)

    return {
        "decisions": len(draft.decisions),
        "confirmations": len(draft.confirmations),
        "issues": len(draft.issues),
        "inquiries": inquiries_created,
        "tasks": tasks_created,
        "record_rows": record_rows_created,
        "docs_updates": len(draft.docs_updates),
        "knowledge_node_id": str(node.id),
    }


async def run_daily_intake(  # noqa: PLR0913
    session: AsyncSession,
    *,
    project_id: UUID,
    project_name: str,
    user_id: UUID,
    raw_input: str,
    intake_date: str,
    clarification_answers: str = "",
    apply: bool = False,
    use_llm: bool = True,
    config: Any = None,
    draft_override: dict[str, Any] | None = None,
    usage_context: LLMUsageContext | Any = None,
) -> dict[str, Any]:
    """日次インテークの制御フロー(preview / clarify / apply)。"""

    # apply確定時にフロントから渡されたdraftを正とする
    if apply and draft_override:
        draft = _draft_from_llm_json(
            draft_override,
            intake_date=intake_date,
            raw_input=raw_input,
        )
    else:
        draft = None
        if use_llm and config:
            draft = await _generate_draft_with_llm(
                config,
                project_name,
                raw_input=raw_input,
                clarification_answers=clarification_answers,
                intake_date=intake_date,
                usage_context=usage_context,
            )
        if draft is None:
            draft = DailyIntakeDraft(intake_date=intake_date, raw_input=raw_input)

    # 逆質問がある場合は保存を禁止し、副作用ゼロで返す
    if draft.clarifying_questions:
        return {
            "success": True,
            "needs_clarification": True,
            "draft": _draft_to_dict(draft),
            "clarifying_questions": list(draft.clarifying_questions),
        }

    if apply:
        result = await apply_daily_intake(
            session,
            project_id=project_id,
            project_name=project_name,
            user_id=user_id,
            draft=draft,
        )
        return {"success": True, "applied": True, "result": result}

    return {
        "success": True,
        "needs_clarification": False,
        "draft": _draft_to_dict(draft),
        "clarifying_questions": [],
    }
