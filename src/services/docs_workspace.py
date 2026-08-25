"""Shared helpers for resolving the canonical Docs library."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeField,
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
    KnowledgeSupertagField,
    DocsLibrary,
    Project,
)
from ..memory.project_repository import ProjectRepository

DOCS_LIBRARY_NAME = "Personal Docs"
DOCS_LIBRARY_DESCRIPTION = "AoiTalk DBを正本にするDocsライブラリ"
DOCS_LIBRARY_SETTINGS: dict[str, Any] = {
    "canonical_store": "postgresql",
    "derived_index": "qdrant",
}

# Legacy constant aliases for extensions that still import the old names.
DOCS_WORKSPACE_NAME = DOCS_LIBRARY_NAME
DOCS_WORKSPACE_DESCRIPTION = DOCS_LIBRARY_DESCRIPTION
DOCS_WORKSPACE_SETTINGS = DOCS_LIBRARY_SETTINGS

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
        "name": "メールメッセージ",
        "system_key": "email_message",
        "base_type": "email",
        "description": "メール原本の引用チェーンから復元した、個別に参照できる1通",
        "icon": "mail-open",
        "color": "#0ea5e9",
        "pinned_field_names": ["送信日時", "From"],
        "fields": [
            {"name": "件名", "system_key": "email_message_subject", "field_type": "text"},
            {"name": "送信日時", "system_key": "email_message_date", "field_type": "text"},
            {"name": "From", "system_key": "email_message_from", "field_type": "text"},
            {"name": "To", "system_key": "email_message_to", "field_type": "long_text"},
            {"name": "CC", "system_key": "email_message_cc", "field_type": "long_text"},
            {"name": "本文", "system_key": "email_message_body", "field_type": "long_text"},
            {"name": "原本", "system_key": "email_message_source", "field_type": "reference"},
            {"name": "復元キー", "system_key": "email_message_source_key", "field_type": "text"},
        ],
        "ai_instructions": "メールの経緯を裏付ける個別メッセージ。要約の事実は該当メッセージへ直接リンクする。",
    },
    {
        "name": "Inbox項目",
        "system_key": "work_intake",
        "base_type": "record",
        "description": "/inboxで受け付けた問い合わせ・依頼・情報共有の管理単位",
        "icon": "inbox",
        "color": "#6366f1",
        "pinned_field_names": ["Inbox ID", "対応状態", "受付日時"],
        "fields": [
            {"name": "Inbox ID", "system_key": "inbox_item_id", "field_type": "text"},
            {
                "name": "分類",
                "system_key": "inbox_classification",
                "field_type": "options",
                "options_json": {"values": ["質問", "依頼", "情報共有"]},
            },
            {
                "name": "対応状態",
                "system_key": "inbox_status",
                "field_type": "options",
                "options_json": {
                    "values": ["受付", "対応中", "確認待ち", "レビュー待ち", "完了", "保存のみ"]
                },
                "default_value_json": "受付",
            },
            {
                "name": "受付元",
                "system_key": "inbox_source_type",
                "field_type": "options",
                "options_json": {"values": ["チャット", "メール", "複合"]},
            },
            {"name": "受付日時", "system_key": "inbox_received_at", "field_type": "date"},
            {"name": "最終更新", "system_key": "inbox_last_updated_at", "field_type": "date"},
            {"name": "受付内容", "system_key": "inbox_instruction", "field_type": "long_text"},
            {"name": "取りまとめ", "system_key": "inbox_summary", "field_type": "long_text"},
        ],
        "ai_instructions": (
            "1回の/inbox受付を1つのInbox項目として扱う。"
            "概要を最優先し、内容に必要な章だけを作る。複数回の応酬は経緯を意味的に要約し、"
            "各事実の直下へ根拠をリンクする。確認事項・次の対応・参考資料・更新履歴を固定で作らない。"
            "追加情報は同じUUIDの文書全体へ統合し、新しい項目を推測で作らない。"
        ),
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


async def seed_default_docs_supertags(session: AsyncSession, library: DocsLibrary) -> None:
    """Ensure Python-side Docs tools see the same system tags as the Next.js UI."""

    result = await session.execute(
        select(KnowledgeSupertag).where(KnowledgeSupertag.docs_library_id == library.id)
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
                docs_library_id=library.id,
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
            desired_name = spec.get("name")
            name_conflict = (
                next(
                    (
                        other
                        for other in existing_tags
                        if other.id != tag.id and other.name == desired_name
                    ),
                    None,
                )
                if desired_name
                else None
            )
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
            if desired_name and tag.name != desired_name and name_conflict is None:
                tag.name = desired_name
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
                    docs_library_id=library.id,
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


async def ensure_docs_library(
    session: AsyncSession,
    *,
    owner_user_id: UUID | None,
) -> DocsLibrary:
    """Return the single canonical Docs library for a user."""

    library_result = await session.execute(
        select(DocsLibrary)
        .where(DocsLibrary.owner_user_id == owner_user_id)
        .order_by(
            (DocsLibrary.name == DOCS_LIBRARY_NAME).desc(),
            DocsLibrary.created_at,
        )
        .limit(1)
    )
    library = library_result.scalar_one_or_none()
    if library is None:
        kwargs: dict[str, Any] = {
            "name": DOCS_LIBRARY_NAME,
            "description": DOCS_LIBRARY_DESCRIPTION,
            "owner_user_id": owner_user_id,
            "settings_json": DOCS_LIBRARY_SETTINGS,
        }
        kwargs["library_type"] = "personal"
        library = DocsLibrary(**kwargs)
        session.add(library)
        await session.flush()
        await seed_default_docs_supertags(session, library)
        return library

    changed = False
    if getattr(library, "library_type", "personal") != "personal":
        # A legacy row keyed only by owner_user_id is necessarily personal;
        # do not let stale settings classify it as a shared Project library.
        library.library_type = "personal"
        changed = True
    if library.name != DOCS_LIBRARY_NAME:
        library.name = DOCS_LIBRARY_NAME
        changed = True
    if not library.description:
        library.description = DOCS_LIBRARY_DESCRIPTION
        changed = True
    current_settings = library.settings_json if isinstance(library.settings_json, dict) else {}
    merged_settings = {**DOCS_LIBRARY_SETTINGS, **current_settings}
    if merged_settings != current_settings:
        library.settings_json = merged_settings
        changed = True
    if changed:
        await session.flush()
    await seed_default_docs_supertags(session, library)
    return library


async def get_project_docs_library(
    session: AsyncSession,
    *,
    project_id: UUID,
    actor_user_id: UUID | None = None,
) -> DocsLibrary | None:
    """Read the project owner's canonical Personal Docs Library.

    This resolver is intentionally SELECT-only: it never creates, repairs or
    seeds a library/supertag.  Callers that only have Project ``read``
    permission must use it so a GET/sync request cannot mutate the database.
    ``None`` means the Project or canonical library is missing/invalid and
    callers should map it to a uniform 404 response.
    """

    try:
        normalized_project_id = UUID(str(project_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("Projectが見つかりません") from exc

    project = await session.get(Project, normalized_project_id)
    if project is None or project.deleted_at is not None:
        return None
    if actor_user_id is not None:
        try:
            actor = UUID(str(actor_user_id))
        except (TypeError, ValueError) as exc:
            raise PermissionError("Projectへの読み取り権限がありません") from exc
        allowed = await ProjectRepository.has_permission(
            session,
            project_id=normalized_project_id,
            user_id=actor,
            permission="read",
        )
        if not allowed:
            raise PermissionError("Projectへの読み取り権限がありません")

    # Project Docs are no longer stored in a project-scoped library. Resolve
    # the pointer node and return its owner's Personal Docs Library only when
    # the pointer is already a valid project root. Read paths never create or
    # repair a missing pointer; the write/bootstrap helper below does that.
    pointer_id = getattr(project, "knowledge_node_id", None)
    if pointer_id is None:
        return None
    node = await session.get(KnowledgeNode, pointer_id)
    if node is None or node.project_id != normalized_project_id:
        return None
    # Runtime reads must validate the complete canonical pointer contract;
    # accepting an arbitrary project-tagged node would let a stale pointer
    # expose unrelated Personal metadata.  Do not repair anything here.
    library = await session.get(DocsLibrary, node.docs_library_id)
    if library is None:
        return None
    if (
        str(getattr(library, "library_type", "personal") or "personal").lower()
        != "personal"
        or getattr(library, "owner_user_id", None) != project.owner_id
        or getattr(node, "archived_at", None) is not None
        or str(getattr(node, "system_key", "") or "")
        != f"project_information:{normalized_project_id}"
        or getattr(node, "parent_id", None) is None
    ):
        return None

    hub = await session.get(KnowledgeNode, node.parent_id)
    if (
        hub is None
        or hub.id != node.parent_id
        or hub.docs_library_id != library.id
        or getattr(hub, "archived_at", None) is not None
        or str(getattr(hub, "system_key", "") or "")
        != "project_information_root"
        or getattr(hub, "parent_id", None) is not None
        or getattr(hub, "project_id", None) is not None
        or getattr(hub, "root_page_id", None) not in (None, hub.id)
        or getattr(node, "root_page_id", None) != hub.id
    ):
        return None

    # The canonical Project Information supertag is part of the pointer
    # identity.  A node merely carrying an arbitrary tag must not be adopted
    # as the project root.
    tag_result = await session.execute(
        select(KnowledgeNodeSupertag.node_id)
        .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
        .where(
            KnowledgeNodeSupertag.node_id == node.id,
            KnowledgeSupertag.docs_library_id == library.id,
            KnowledgeSupertag.system_key == "project_info",
        )
        .limit(1)
    )
    if tag_result.scalar_one_or_none() is None:
        return None
    return library


async def ensure_project_docs_library(
    session: AsyncSession,
    *,
    project_id: UUID,
    actor_user_id: UUID | None = None,
) -> DocsLibrary:
    """Return the owner's Personal Docs Library for one Project.

    Project identity remains on the canonical root/descendant nodes.  This
    deprecated resolver name is retained for callers during the rolling rename;
    it never creates a project-scoped library row.
    """

    normalized_project_id = UUID(str(project_id))
    project = await session.get(Project, normalized_project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError("Projectが見つかりません")
    if actor_user_id is None:
        raise PermissionError("Project Docsへの書き込み権限がありません")
    try:
        actor = UUID(str(actor_user_id))
    except (TypeError, ValueError) as exc:
        raise PermissionError("Project Docsへの書き込み権限がありません") from exc
    allowed = await ProjectRepository.has_permission(
        session,
        project_id=normalized_project_id,
        user_id=actor,
        permission="write",
    )
    if not allowed:
        raise PermissionError("Project Docsへの書き込み権限がありません")

    # The canonical scope is always the project owner's Personal Docs Library.
    # Only the owner may bootstrap/repair that library or seed default
    # metadata.  A ProjectMember writer must consume the already validated
    # owner library and canonical hub/project node without any write side
    # effects (including name/settings/default-tag repair).
    if actor == project.owner_id:
        return await ensure_docs_library(
            session,
            owner_user_id=project.owner_id,
        )

    library = await get_project_docs_library(
        session,
        project_id=normalized_project_id,
        actor_user_id=actor,
    )
    if library is None:
        raise PermissionError(
            "Project Docsのcanonical libraryまたは正本nodeが未初期化または不正です"
        )
    return library


    # Deprecated Python aliases kept for integrations during the rolling rename.
# New code should import the ``*_library`` names above.
ensure_docs_workspace = ensure_docs_library
get_project_docs_workspace = get_project_docs_library
ensure_project_docs_workspace = ensure_project_docs_library
