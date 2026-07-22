"""TRPG ruleset profile and unified reference document management.

This service keeps rule-system metadata separate from scenario metadata. A
scenario points at a ruleset key; profiles and reference documents define how
that ruleset behaves at runtime.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from ..memory.database import get_db_session
from ..models.ecc_models import TRPGReferenceDocument, TRPGRulesetProfile
from ..utils.uuid_utils import parse_uuid
from .trpg_rules import (
    get_builtin_ruleset_profile,
    list_builtin_ruleset_profiles,
    merge_ruleset_profile,
    normalize_ruleset_key,
)


class TRPGRulebookError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TRPGReferenceDocumentNotFoundError(TRPGRulebookError):
    def __init__(self, identifier: str):
        super().__init__(f"TRPG資料が見つかりません: {identifier}", 404)


def profile_model_to_runtime_dict(profile: Optional[TRPGRulesetProfile]) -> Dict[str, Any]:
    if profile is None:
        return {}
    data = profile.to_dict()
    data["metadata"] = data.pop("metadata", data.get("profile_metadata", {}))
    return data


def normalize_reference_structure(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    normalized = dict(value)
    try:
        normalized["version"] = int(normalized.get("version") or 1)
    except (TypeError, ValueError):
        normalized["version"] = 1
    nodes = normalized.get("nodes")
    normalized["nodes"] = [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []
    links = normalized.get("links")
    normalized["links"] = [link for link in links if isinstance(link, dict)] if isinstance(links, list) else []
    metadata = normalized.get("metadata")
    normalized["metadata"] = dict(metadata) if isinstance(metadata, dict) else {}
    return normalized


def _reference_document_payload(data: Dict[str, Any], ruleset_key: str) -> Dict[str, Any]:
    title = str(data.get("title") or "").strip()
    if not title:
        raise TRPGRulebookError("TRPG資料のタイトルは必須です")
    document_type = str(data.get("document_type") or "rulebook").strip() or "rulebook"
    return {
        "ruleset_key": normalize_ruleset_key(data.get("ruleset_key") or ruleset_key),
        "title": title,
        "source_label": str(data.get("source_label") or "").strip(),
        "source_text": str(data.get("source_text") or ""),
        "document_type": document_type,
        "supplement_kind": str(data.get("supplement_kind") or "general").strip() or "general",
        "structure": normalize_reference_structure(data.get("structure")),
        "priority": int(data.get("priority") or 0),
        "is_active": bool(data.get("is_active", True)),
        "document_metadata": dict(data.get("metadata") or data.get("document_metadata") or {}),
        "import_status": str(data.get("import_status") or "manual"),
    }


async def ensure_ruleset_profile(ruleset_key: str) -> Dict[str, Any]:
    key = normalize_ruleset_key(ruleset_key)
    async with await get_db_session() as session:
        profile = await session.get(TRPGRulesetProfile, key)
        if profile is None:
            built_in = get_builtin_ruleset_profile(key)
            profile = TRPGRulesetProfile(
                key=key,
                display_name=built_in["display_name"],
                edition=built_in.get("edition", ""),
                system_type=built_in.get("system_type", "generic"),
                description=built_in.get("description", ""),
                gm_rules_brief=built_in.get("gm_rules_brief", ""),
                character_sheet_schema=built_in.get("character_sheet_schema", {}),
                default_pc_state=built_in.get("default_pc_state", {}),
                resource_schema=built_in.get("resource_schema", {}),
                dice_rule_schema=built_in.get("dice_rule_schema", {}),
                skill_resolver=built_in.get("skill_resolver", {}),
                profile_metadata=built_in.get("metadata", {}),
                is_enabled=bool(built_in.get("is_enabled", True)),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(profile)
            await session.commit()
            await session.refresh(profile)
        return merge_ruleset_profile(key, profile_model_to_runtime_dict(profile))


async def list_ruleset_profiles(include_disabled: bool = False) -> List[Dict[str, Any]]:
    profiles_by_key = {
        profile["key"]: profile for profile in list_builtin_ruleset_profiles()
    }
    async with await get_db_session() as session:
        stmt = select(TRPGRulesetProfile)
        if not include_disabled:
            stmt = stmt.where(TRPGRulesetProfile.is_enabled.is_(True))
        result = await session.execute(stmt)
        for profile in result.scalars().all():
            profiles_by_key[profile.key] = merge_ruleset_profile(
                profile.key,
                profile_model_to_runtime_dict(profile),
            )
    return sorted(profiles_by_key.values(), key=lambda item: item["key"])


async def get_ruleset_runtime_profile(ruleset_key: str) -> Dict[str, Any]:
    key = normalize_ruleset_key(ruleset_key)
    async with await get_db_session() as session:
        profile = await session.get(TRPGRulesetProfile, key)
        return merge_ruleset_profile(key, profile_model_to_runtime_dict(profile))


async def list_reference_documents(
    ruleset_key: str,
    active_only: bool = True,
    document_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    key = normalize_ruleset_key(ruleset_key)
    async with await get_db_session() as session:
        stmt = select(TRPGReferenceDocument).where(
            TRPGReferenceDocument.ruleset_key == key
        )
        if active_only:
            stmt = stmt.where(TRPGReferenceDocument.is_active.is_(True))
        if document_type:
            stmt = stmt.where(TRPGReferenceDocument.document_type == document_type)
        stmt = stmt.order_by(TRPGReferenceDocument.priority.desc(), TRPGReferenceDocument.updated_at.desc())
        result = await session.execute(stmt)
        return [doc.to_dict() for doc in result.scalars().all()]


async def upsert_reference_document(
    ruleset_key: str,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    key = normalize_ruleset_key(ruleset_key)
    payload = _reference_document_payload(data, key)
    document_id = parse_uuid(data.get("id"))

    async with await get_db_session() as session:
        profile = await session.get(TRPGRulesetProfile, key)
        if profile is None:
            built_in = get_builtin_ruleset_profile(key)
            profile = TRPGRulesetProfile(
                key=key,
                display_name=built_in["display_name"],
                edition=built_in.get("edition", ""),
                system_type=built_in.get("system_type", "generic"),
                description=built_in.get("description", ""),
                gm_rules_brief=built_in.get("gm_rules_brief", ""),
                character_sheet_schema=built_in.get("character_sheet_schema", {}),
                default_pc_state=built_in.get("default_pc_state", {}),
                resource_schema=built_in.get("resource_schema", {}),
                dice_rule_schema=built_in.get("dice_rule_schema", {}),
                skill_resolver=built_in.get("skill_resolver", {}),
                profile_metadata=built_in.get("metadata", {}),
                is_enabled=True,
            )
            session.add(profile)

        document = None
        if document_id:
            document = await session.get(TRPGReferenceDocument, document_id)
            if document is not None and document.ruleset_key != key:
                raise TRPGReferenceDocumentNotFoundError(str(document_id))
        if document is None:
            document = TRPGReferenceDocument(id=document_id or uuid.uuid4(), ruleset_key=key)
            session.add(document)

        for field, value in payload.items():
            setattr(document, field, value)
        document.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(document)
        return document.to_dict()


async def delete_reference_document(ruleset_key: str, document_id: str) -> bool:
    key = normalize_ruleset_key(ruleset_key)
    uid = parse_uuid(document_id)
    if uid is None:
        raise TRPGReferenceDocumentNotFoundError(document_id)
    async with await get_db_session() as session:
        document = await session.get(TRPGReferenceDocument, uid)
        if document is None or document.ruleset_key != key:
            raise TRPGReferenceDocumentNotFoundError(document_id)
        await session.delete(document)
        await session.commit()
        return True


# Backward-compatible API names. These now operate on unified reference
# documents, filtered to document_type=rulebook only where the old name implies
# a rulebook.
normalize_rulebook_structure = normalize_reference_structure
_rulebook_payload = _reference_document_payload


async def list_rulebook_documents(ruleset_key: str, active_only: bool = True) -> List[Dict[str, Any]]:
    key = normalize_ruleset_key(ruleset_key)
    async with await get_db_session() as session:
        stmt = select(TRPGReferenceDocument).where(
            TRPGReferenceDocument.ruleset_key == key,
            TRPGReferenceDocument.document_type.in_(["rulebook", "rulebook_reference"]),
        )
        if active_only:
            stmt = stmt.where(TRPGReferenceDocument.is_active.is_(True))
        stmt = stmt.order_by(TRPGReferenceDocument.priority.desc(), TRPGReferenceDocument.updated_at.desc())
        result = await session.execute(stmt)
        return [doc.to_dict() for doc in result.scalars().all()]


async def upsert_rulebook_document(ruleset_key: str, data: Dict[str, Any]) -> Dict[str, Any]:
    payload = {**data, "document_type": data.get("document_type") or "rulebook"}
    return await upsert_reference_document(ruleset_key, payload)


async def delete_rulebook_document(ruleset_key: str, document_id: str) -> bool:
    return await delete_reference_document(ruleset_key, document_id)
