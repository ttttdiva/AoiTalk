"""Shared helpers for resolving the canonical Docs workspace."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeField,
    KnowledgeSupertag,
    KnowledgeSupertagField,
    KnowledgeWorkspace,
)

DOCS_WORKSPACE_NAME = "Personal Docs"
DOCS_WORKSPACE_DESCRIPTION = "AoiTalk DBを正本にするDocsワークスペース"
DOCS_WORKSPACE_SETTINGS: dict[str, Any] = {
    "canonical_store": "postgresql",
    "derived_index": "qdrant",
}

DEFAULT_DOCS_SUPERTAGS: list[dict[str, Any]] = [
    {
        "name": "Task",
        "system_key": "task",
        "base_type": "task",
        "description": "次アクションや作業項目",
        "icon": "check-square",
        "color": "#22c55e",
        "pinned_field_names": ["状態", "期日"],
        "fields": [
            {"name": "状態", "system_key": "task_status", "field_type": "options", "options_json": {"values": ["todo", "doing", "done"]}, "default_value_json": "todo"},
            {"name": "開始", "system_key": "task_start", "field_type": "date"},
            {"name": "期日", "system_key": "task_due", "field_type": "date", "options_json": {"default": "today"}},
            {"name": "優先度", "system_key": "task_priority", "field_type": "options", "options_json": {"values": ["low", "normal", "high", "urgent"]}, "default_value_json": "normal"},
            {"name": "担当", "field_type": "text"},
            {"name": "案件", "system_key": "task_project", "field_type": "reference", "options_json": {"default": "ancestor_project"}},
        ],
        "ai_instructions": "作業項目として、状態・期日・担当・案件を明示して更新する。",
    },
    {
        "name": "Meeting",
        "system_key": "meeting",
        "base_type": "meeting",
        "description": "会議メモと議事",
        "icon": "calendar-days",
        "color": "#a855f7",
        "pinned_field_names": ["日時", "出席者"],
        "fields": [
            {"name": "日時", "system_key": "meeting_date", "field_type": "date", "options_json": {"default": "today"}},
            {"name": "出席者", "field_type": "text"},
            {"name": "案件", "field_type": "reference", "options_json": {"default": "ancestor_project"}},
        ],
        "ai_instructions": "会議メモは議題、決定、宿題、未確認事項を分ける。",
    },
    {
        "name": "Person",
        "system_key": "person",
        "base_type": "person",
        "description": "関係者",
        "icon": "user",
        "color": "#ec4899",
        "pinned_field_names": ["所属"],
        "fields": [
            {"name": "所属", "field_type": "text"},
            {"name": "連絡先", "field_type": "text"},
            {"name": "役割", "field_type": "text"},
        ],
        "ai_instructions": "人物情報は所属・連絡先・役割を区別して扱う。",
    },
    {
        "name": "案件情報",
        "system_key": "project_info",
        "base_type": "project_information",
        "description": "案件の正本ページ",
        "icon": "briefcase",
        "color": "#2563eb",
        "pinned_field_names": [],
        "fields": [],
        "ai_instructions": "案件の恒久情報を構造化して保つ。",
    },
    {
        "name": "メール",
        "system_key": "email",
        "base_type": "email",
        "description": "プロジェクトへ取り込んだメールの恒久記録",
        "icon": "mail",
        "color": "#0284c7",
        "pinned_field_names": ["メール日時", "From", "Message-ID"],
        "fields": [
            {"name": "件名", "system_key": "email_subject", "field_type": "text"},
            {"name": "メール日時", "system_key": "email_date", "field_type": "text"},
            {"name": "From", "system_key": "email_from", "field_type": "text"},
            {"name": "To", "system_key": "email_to", "field_type": "long_text"},
            {"name": "CC", "system_key": "email_cc", "field_type": "long_text"},
            {"name": "BCC", "system_key": "email_bcc", "field_type": "long_text"},
            {"name": "Message-ID", "system_key": "email_message_id", "field_type": "text"},
            {"name": "In-Reply-To", "system_key": "email_in_reply_to", "field_type": "text"},
            {"name": "References", "system_key": "email_references", "field_type": "long_text"},
            {"name": "本文", "system_key": "email_body", "field_type": "long_text"},
            {"name": "元ファイル名", "system_key": "email_source_filename", "field_type": "text"},
            {"name": "元ファイルのプロジェクト内パス", "system_key": "email_source_path", "field_type": "text"},
            {"name": "重複判定キー", "system_key": "email_dedupe_key", "field_type": "text"},
        ],
        "ai_instructions": "1メール=1ノード。ヘッダー、本文、原本パスを保持し、同一プロジェクト内の質問へ根拠として使う。",
    },
    {
        "name": "Day",
        "system_key": "day",
        "base_type": "day",
        "description": "日次ノート",
        "icon": "calendar",
        "color": "#0ea5e9",
        "pinned_field_names": ["日付"],
        "fields": [
            {"name": "日付", "system_key": "day_date", "field_type": "date", "options_json": {"default": "today"}},
        ],
        "ai_instructions": "その日の記録と後で移動するメモを保持する。",
    },
    {
        "name": "Decision",
        "base_type": "decision",
        "description": "判断と根拠の記録",
        "icon": "gavel",
        "color": "#f59e0b",
        "pinned_field_names": ["決定日"],
        "fields": [
            {"name": "決定日", "field_type": "date", "options_json": {"default": "today"}},
            {"name": "根拠", "field_type": "text"},
            {"name": "案件", "field_type": "reference", "options_json": {"default": "ancestor_project"}},
        ],
        "ai_instructions": "決定事項として、決定文・決定日・根拠を必ず残す。",
    },
    {
        "name": "Risk",
        "base_type": "risk",
        "description": "懸念、制約、互換性リスク",
        "icon": "alert-triangle",
        "color": "#ef4444",
        "pinned_field_names": ["深刻度", "状態"],
        "fields": [
            {"name": "深刻度", "field_type": "options", "options_json": {"values": ["low", "mid", "high"]}, "default_value_json": "mid"},
            {"name": "状態", "field_type": "options", "options_json": {"values": ["open", "watch", "closed"]}, "default_value_json": "open"},
            {"name": "対策", "field_type": "text"},
            {"name": "案件", "field_type": "reference", "options_json": {"default": "ancestor_project"}},
        ],
        "ai_instructions": "リスクは影響・状態・対策を分けて記録する。",
    },
]


async def seed_default_docs_supertags(session: AsyncSession, workspace: KnowledgeWorkspace) -> None:
    """Ensure Python-side Docs tools see the same system tags as the Next.js UI."""

    result = await session.execute(
        select(KnowledgeSupertag).where(KnowledgeSupertag.workspace_id == workspace.id)
    )
    existing_tags = list(result.scalars().all())
    tags_by_name = {tag.name: tag for tag in existing_tags}
    tags_by_system_key = {tag.system_key: tag for tag in existing_tags if tag.system_key}

    for spec in DEFAULT_DOCS_SUPERTAGS:
        system_key = spec.get("system_key")
        tag = tags_by_system_key.get(system_key) if system_key else None
        tag = tag or tags_by_name.get(spec["name"])
        if tag is None:
            tag = KnowledgeSupertag(
                workspace_id=workspace.id,
                system_key=system_key,
                name=spec["name"],
                base_type=spec["base_type"],
                description=spec.get("description"),
                icon=spec.get("icon"),
                color=spec.get("color"),
                template_json={},
                pinned_field_ids=[],
                config_json=spec.get("config_json") or {},
                ai_instructions=spec.get("ai_instructions"),
            )
            session.add(tag)
            await session.flush()
            tags_by_name[tag.name] = tag
            if tag.system_key:
                tags_by_system_key[tag.system_key] = tag
        else:
            changed = False
            for attr, key in [
                ("system_key", "system_key"),
                ("base_type", "base_type"),
                ("description", "description"),
                ("icon", "icon"),
                ("color", "color"),
                ("ai_instructions", "ai_instructions"),
            ]:
                value = spec.get(key)
                if value and getattr(tag, attr) != value:
                    setattr(tag, attr, value)
                    changed = True
            # config_json.tools を spec と同期する(既存キーは温存しマージ)。
            spec_config = spec.get("config_json")
            if spec_config is not None:
                current_config = tag.config_json if isinstance(tag.config_json, dict) else {}
                desired_config = {**current_config}
                spec_tools = spec_config.get("tools")
                if spec_tools is not None and current_config.get("tools") != spec_tools:
                    desired_config["tools"] = spec_tools
                if desired_config != current_config:
                    tag.config_json = desired_config
                    changed = True
            if changed:
                await session.flush()

        fields_result = await session.execute(
            select(KnowledgeField).where(KnowledgeField.supertag_id == tag.id)
        )
        fields = list(fields_result.scalars().all())
        fields_by_name = {field.name: field for field in fields}
        fields_by_system_key = {field.system_key: field for field in fields if field.system_key}
        for index, field_spec in enumerate(spec["fields"]):
            field_key = field_spec.get("system_key")
            field = fields_by_system_key.get(field_key) if field_key else None
            field = field or fields_by_name.get(field_spec["name"])
            if field is None:
                field = KnowledgeField(
                    workspace_id=workspace.id,
                    supertag_id=tag.id,
                    system_key=field_key,
                    name=field_spec["name"],
                    field_type=field_spec["field_type"],
                    required=bool(field_spec.get("required")),
                    options_json=field_spec.get("options_json", {}),
                    default_value_json=field_spec.get("default_value_json"),
                    sort_order=index,
                )
                session.add(field)
                await session.flush()
                fields.append(field)
                fields_by_name[field.name] = field
                if field.system_key:
                    fields_by_system_key[field.system_key] = field
            elif field_key and field.system_key != field_key:
                field.system_key = field_key
                await session.flush()

            link = await session.get(
                KnowledgeSupertagField,
                {"supertag_id": tag.id, "field_id": field.id},
            )
            if link is None:
                session.add(
                    KnowledgeSupertagField(
                        supertag_id=tag.id,
                        field_id=field.id,
                        sort_order=index,
                        required=bool(field_spec.get("required")),
                        show_in_template=True,
                        optional=False,
                    )
                )

        pinned_ids = [
            str(fields_by_name[name].id)
            for name in spec.get("pinned_field_names", [])
            if name in fields_by_name
        ]
        if tag.pinned_field_ids != pinned_ids:
            tag.pinned_field_ids = pinned_ids

    await session.flush()


async def ensure_docs_workspace(
    session: AsyncSession,
    *,
    owner_user_id: UUID | None,
) -> KnowledgeWorkspace:
    """Return the single canonical Docs workspace for a user."""

    workspace_result = await session.execute(
        select(KnowledgeWorkspace)
        .where(KnowledgeWorkspace.owner_user_id == owner_user_id)
        .order_by(
            (KnowledgeWorkspace.name == DOCS_WORKSPACE_NAME).desc(),
            KnowledgeWorkspace.created_at,
        )
        .limit(1)
    )
    workspace = workspace_result.scalar_one_or_none()
    if workspace is None:
        workspace = KnowledgeWorkspace(
            name=DOCS_WORKSPACE_NAME,
            description=DOCS_WORKSPACE_DESCRIPTION,
            owner_user_id=owner_user_id,
            settings_json=DOCS_WORKSPACE_SETTINGS,
        )
        session.add(workspace)
        await session.flush()
        await seed_default_docs_supertags(session, workspace)
        return workspace

    changed = False
    if workspace.name != DOCS_WORKSPACE_NAME:
        workspace.name = DOCS_WORKSPACE_NAME
        changed = True
    if not workspace.description:
        workspace.description = DOCS_WORKSPACE_DESCRIPTION
        changed = True
    current_settings = workspace.settings_json if isinstance(workspace.settings_json, dict) else {}
    merged_settings = {**DOCS_WORKSPACE_SETTINGS, **current_settings}
    if merged_settings != current_settings:
        workspace.settings_json = merged_settings
        changed = True
    if changed:
        await session.flush()
    await seed_default_docs_supertags(session, workspace)
    return workspace
