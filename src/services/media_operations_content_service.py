"""MediaOps WS5 content variants, QA and rights service."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from ..memory.models.media_operations_content import (
    CONTENT_VARIANT_PLATFORM_VALUES, QA_ASSESSMENT_RESULT_VALUES,
    RIGHTS_ASSESSMENT_RESULT_VALUES, ContentVariant, ContentVariantRevision,
    QAAssessment, RightsAssessment,
)
from ..memory.models.media_operations_generation import GenerationOutput
from ..memory.models.media_operations import Persona, PersonaRevision
from ..memory.models.media_operations_research import ContentItem, EditorialProgram
from ..memory.models.media_operations_setup import PlatformAccount, PlatformAccountRevision
from .media_operations_research_service import MediaOperationsResearchService
from .media_operations_service import (
    MediaOperationsAuthorizationError, MediaOperationsConflictError,
    MediaOperationsNotFoundError, MediaOperationsValidationError,
    _actor_field, _actor_id, _as_uuid, _bounded_page, _idempotency_key,
    _optional_text, _required_text, _validated_resource_url, _validated_sha256,
    sha256_json,
)

_PAYLOAD_TYPES = {
    "x": {"x_post", "x_thread"},
    "pixiv": {"pixiv_work"},
    "dlsite": {"dlsite_release"},
    "patreon": {"patreon_post"},
    "youtube": {"youtube_video", "youtube_short"},
    "instagram": {"instagram_feed", "instagram_carousel", "instagram_reel"},
}
_PAYLOAD_PLATFORM = {kind: platform for platform, kinds in _PAYLOAD_TYPES.items() for kind in kinds}
_PAYLOAD_RULES = {
    "x_post": ({"type", "text"}, {"media", "alt_text", "links", "hashtags", "sensitive_content", "reply_to", "scheduled_at"}),
    "x_thread": ({"type", "posts"}, {"scheduled_at", "reply_to"}),
    "pixiv_work": ({"type", "title", "caption", "tags", "media"}, {"ai_generated", "rating", "r18", "r18g", "series_id"}),
    "dlsite_release": ({"type", "title", "description", "category", "age_rating", "price", "sales", "preview_assets", "deliverable_package_ref", "rights_checklist"}, {"thumbnail_assets"}),
    "patreon_post": ({"type", "audience", "title", "body"}, {"public_preview", "attachments", "tier_refs", "scheduled_at"}),
    "youtube_video": ({"type", "title", "description", "tags", "media_asset", "visibility"}, {"thumbnail", "captions", "scheduled_at", "audience", "disclosure"}),
    "youtube_short": ({"type", "title", "description", "tags", "media_asset", "visibility"}, {"thumbnail", "captions", "scheduled_at", "audience", "disclosure"}),
    "instagram_feed": ({"type", "caption", "media"}, {"alt_text", "scheduled_at"}),
    "instagram_carousel": ({"type", "caption", "media"}, {"alt_text", "scheduled_at"}),
    "instagram_reel": ({"type", "caption", "media"}, {"alt_text", "scheduled_at", "cover"}),
}
_BLOCKED_KEYS = frozenset({"provider", "provider_payload", "raw", "raw_payload", "graph", "workflow", "secret", "secrets", "credential", "credentials", "token", "access_token", "cookie", "cookies", "filesystem_path", "path"})
_TEXT_FIELDS = frozenset({"text", "title", "caption", "description", "body", "public_preview", "alt_text", "category", "age_rating", "rating", "series_id", "reply_to", "audience", "visibility", "disclosure", "scheduled_at", "deliverable_package_ref"})
_LIST_TEXT_FIELDS = frozenset({"links", "hashtags", "tags", "tier_refs", "captions"})
_BOOL_FIELDS = frozenset({"sensitive_content", "ai_generated", "r18", "r18g"})
_REF_FIELDS = frozenset({"media", "attachments", "preview_assets", "thumbnail_assets", "media_asset", "thumbnail", "cover"})


def _platform(value: Any) -> str:
    rendered = str(getattr(value, "value", value)).strip().lower()
    if rendered not in CONTENT_VARIANT_PLATFORM_VALUES:
        raise MediaOperationsValidationError("platform must be one of x, pixiv, dlsite, patreon, youtube, instagram")
    return rendered


def _uuid(value: Any, label: str) -> UUID:
    parsed = _as_uuid(value, label)
    assert parsed is not None
    return parsed


def _text_list(value: Any, label: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if required and not value:
        raise MediaOperationsValidationError(f"{label} must contain at least one item")
    if len(value) > 50:
        raise MediaOperationsValidationError(f"{label} exceeds 50 items")
    result: list[str] = []
    for item in value:
        rendered = _required_text(item, label, 2000)
        if rendered not in result:
            result.append(rendered)
    return result


def _ref_token(value: Any, label: str) -> str:
    if isinstance(value, Mapping):
        keys = {str(key) for key in value}
        if keys - {"generation_output_id", "id", "alt_text"}:
            raise MediaOperationsValidationError(f"{label} accepts only internal GenerationOutput references")
        value = value.get("generation_output_id", value.get("id"))
    return str(_uuid(value, f"{label}.generation_output_id"))


def _ref_list(value: Any, label: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(value) > 20:
        raise MediaOperationsValidationError(f"{label} exceeds 20 items")
    result: list[str] = []
    for item in value:
        ref = _ref_token(item, label)
        if ref not in result:
            result.append(ref)
    return result


def _normalize_payload(payload: Any, platform: str) -> tuple[dict[str, Any], list[str]]:
    platform = _platform(platform)
    if not isinstance(payload, Mapping):
        raise MediaOperationsValidationError("payload must be a typed object")
    data = {str(key): value for key, value in payload.items()}
    if set(data) & _BLOCKED_KEYS:
        raise MediaOperationsValidationError("payload contains a forbidden provider, credential, or filesystem field")
    kind = data.get("type")
    if not isinstance(kind, str) or kind.strip().lower() not in _PAYLOAD_PLATFORM:
        raise MediaOperationsValidationError("payload.type is required and must be a supported discriminator")
    kind = kind.strip().lower()
    if _PAYLOAD_PLATFORM[kind] != platform:
        raise MediaOperationsValidationError("payload.type does not match the requested platform")
    required, optional = _PAYLOAD_RULES[kind]
    if required - set(data):
        raise MediaOperationsValidationError("payload is missing required fields: " + ", ".join(sorted(required - set(data))))
    if set(data) - (required | optional):
        raise MediaOperationsValidationError("payload contains fields not allowed by its typed discriminator")
    result: dict[str, Any] = {"type": kind}
    refs: list[str] = []
    for key in sorted(set(data) - {"type"}):
        value = data[key]
        if key in _TEXT_FIELDS:
            if key in {"public_preview", "alt_text", "scheduled_at", "reply_to", "series_id", "rating", "disclosure"} and value is None:
                result[key] = None
            elif key in {"caption", "description"} and isinstance(value, str) and not value.strip():
                if key == "description" and kind == "dlsite_release":
                    raise MediaOperationsValidationError(f"payload.{key} must not be empty")
                result[key] = ""
            elif key in {"public_preview", "alt_text", "scheduled_at", "reply_to", "series_id", "rating", "disclosure"} and isinstance(value, str) and not value.strip():
                result[key] = None
            else:
                result[key] = _required_text(value, f"payload.{key}", 12000)
        elif key in _LIST_TEXT_FIELDS:
            result[key] = _text_list(value, f"payload.{key}", required=False)
        elif key in _BOOL_FIELDS:
            if not isinstance(value, bool):
                raise MediaOperationsValidationError(f"payload.{key} must be a boolean")
            result[key] = value
        elif key in _REF_FIELDS:
            values = _ref_list(value, f"payload.{key}")
            if key in required and not values:
                raise MediaOperationsValidationError(f"payload.{key} must contain at least one internal reference")
            result[key] = values
            refs.extend(item for item in values if item not in refs)
        elif key == "posts":
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value or len(value) > 20:
                raise MediaOperationsValidationError("payload.posts must contain 1 to 20 typed posts")
            posts = []
            for index, post in enumerate(value, 1):
                if not isinstance(post, Mapping) or set(post) - {"text", "media", "alt_text"} or "text" not in post:
                    raise MediaOperationsValidationError("payload.posts item has invalid fields")
                item = {"text": _required_text(post["text"], f"payload.posts[{index}].text", 12000)}
                if "media" in post:
                    item["media"] = _ref_list(post["media"], f"payload.posts[{index}].media")
                    refs.extend(ref for ref in item["media"] if ref not in refs)
                if "alt_text" in post:
                    item["alt_text"] = _required_text(post["alt_text"], f"payload.posts[{index}].alt_text", 2000)
                posts.append(item)
            result[key] = posts
        elif key == "price":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 10000000:
                raise MediaOperationsValidationError("payload.price must be a non-negative number")
            result[key] = float(value)
        elif key == "sales":
            if not isinstance(value, Mapping) or set(value) != {"currency", "tax_included", "distribution"} or not isinstance(value["tax_included"], bool):
                raise MediaOperationsValidationError("payload.sales accepts currency, tax_included, and distribution")
            result[key] = {"currency": _required_text(value["currency"], "payload.sales.currency", 8).upper(), "tax_included": value["tax_included"], "distribution": _required_text(value["distribution"], "payload.sales.distribution", 80)}
        elif key == "rights_checklist":
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value or len(value) > 50:
                raise MediaOperationsValidationError("payload.rights_checklist must contain 1 to 50 items")
            checks = []
            for index, check in enumerate(value, 1):
                if not isinstance(check, Mapping) or set(check) != {"code", "status", "note"}:
                    raise MediaOperationsValidationError("rights checklist items accept code, status, and note")
                status = _required_text(check["status"], f"payload.rights_checklist[{index}].status", 24).lower()
                if status not in {"passed", "failed", "not_run", "review_required"}:
                    raise MediaOperationsValidationError("rights checklist status is invalid")
                checks.append({"code": _required_text(check["code"], f"payload.rights_checklist[{index}].code", 120), "status": status, "note": _optional_text(check["note"], f"payload.rights_checklist[{index}].note", 2000)})
            result[key] = checks
        else:
            raise MediaOperationsValidationError(f"payload.{key} is unsupported")
    if kind == "instagram_carousel" and not 2 <= len(result.get("media", [])) <= 10:
        raise MediaOperationsValidationError("instagram_carousel requires between two and ten media references")
    if kind in {"instagram_feed", "instagram_reel"} and len(result.get("media", [])) != 1:
        raise MediaOperationsValidationError("instagram_feed and instagram_reel require exactly one media reference")
    if kind in {"youtube_video", "youtube_short"} and len(result.get("media_asset", [])) != 1:
        raise MediaOperationsValidationError("YouTube payload requires exactly one media reference")
    if kind == "x_post" and len(result.get("media", [])) > 4:
        raise MediaOperationsValidationError("x_post supports at most four media references")
    return result, refs

def _normalize_evidence(values: Any, label: str = "evidence") -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(values) > 50:
        raise MediaOperationsValidationError(f"{label} exceeds 50 items")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, raw in enumerate(values, 1):
        if not isinstance(raw, Mapping):
            raise MediaOperationsValidationError(f"{label} items must be typed")
        data = {str(k): v for k, v in raw.items()}
        kind = str(data.get("type") or "").strip().lower()
        if kind == "url":
            if set(data) != {"type", "url", "label", "note"}:
                raise MediaOperationsValidationError("url evidence has an invalid field set")
            normalized = {"ordinal": ordinal, "type": "url", "url": _validated_resource_url(data["url"]), "label": _optional_text(data["label"], f"{label}.label", 255), "note": _optional_text(data["note"], f"{label}.note", 2000)}
        elif kind == "artifact":
            if set(data) != {"type", "sha256", "mime_type", "label", "note"}:
                raise MediaOperationsValidationError("artifact evidence has an invalid field set")
            normalized = {"ordinal": ordinal, "type": "artifact", "sha256": _validated_sha256(data["sha256"], f"{label}.sha256"), "mime_type": _required_text(data["mime_type"], f"{label}.mime_type", 255), "label": _optional_text(data["label"], f"{label}.label", 255), "note": _optional_text(data["note"], f"{label}.note", 2000)}
        else:
            raise MediaOperationsValidationError(f"{label}.type must be url or artifact")
        evidence_hash = sha256_json({k: v for k, v in normalized.items() if k != "ordinal"})
        if evidence_hash in seen:
            raise MediaOperationsValidationError(f"duplicate {label} is not allowed")
        seen.add(evidence_hash)
        normalized["evidence_hash"] = evidence_hash
        out.append(normalized)
    return out


def _normalize_generation_refs(values: Any) -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError("generation_output_refs must be a list")
    if len(values) > 20:
        raise MediaOperationsValidationError("generation_output_refs exceeds 20 items")
    out: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    for ordinal, raw in enumerate(values, 1):
        expected_sha = None
        if isinstance(raw, Mapping):
            if set(raw) - {"generation_output_id", "id", "sha256"}:
                raise MediaOperationsValidationError("generation_output_refs accept only internal UUID and sha256")
            parsed = _uuid(raw.get("generation_output_id", raw.get("id")), "generation_output_refs.generation_output_id")
            if raw.get("sha256") is not None:
                expected_sha = _validated_sha256(raw["sha256"], "generation_output_refs.sha256")
        else:
            parsed = _uuid(raw, "generation_output_refs.generation_output_id")
        if parsed in seen:
            raise MediaOperationsValidationError("generation_output_refs cannot contain duplicates")
        seen.add(parsed)
        entry = {"ordinal": ordinal, "generation_output_id": str(parsed)}
        if expected_sha is not None:
            entry["sha256"] = expected_sha
        out.append(entry)
    return out


def _normalize_checks(values: Any, label: str = "checks") -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(values) > 50:
        raise MediaOperationsValidationError(f"{label} exceeds 50 items")
    out: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(values, 1):
        if not isinstance(raw, Mapping) or set(raw) != {"code", "status", "mandatory", "message"}:
            raise MediaOperationsValidationError(f"{label} items accept code, status, mandatory, and message")
        status = _required_text(raw["status"], f"{label}[{ordinal}].status", 24).lower()
        if status not in {"passed", "failed", "not_run"}:
            raise MediaOperationsValidationError(f"{label}[{ordinal}].status is invalid")
        if not isinstance(raw["mandatory"], bool):
            raise MediaOperationsValidationError(f"{label}[{ordinal}].mandatory must be a boolean")
        out.append({"code": _required_text(raw["code"], f"{label}[{ordinal}].code", 120), "status": status, "mandatory": raw["mandatory"], "message": _optional_text(raw["message"], f"{label}[{ordinal}].message", 2000)})
    return out


def _normalize_findings(values: Any, label: str = "findings") -> list[dict[str, Any]]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(values) > 50:
        raise MediaOperationsValidationError(f"{label} exceeds 50 items")
    out: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(values, 1):
        if not isinstance(raw, Mapping) or set(raw) != {"code", "severity", "message"}:
            raise MediaOperationsValidationError(f"{label} items accept code, severity, and message")
        severity = _required_text(raw["severity"], f"{label}[{ordinal}].severity", 24).lower()
        if severity not in {"info", "warning", "error"}:
            raise MediaOperationsValidationError(f"{label}[{ordinal}].severity is invalid")
        out.append({"code": _required_text(raw["code"], f"{label}[{ordinal}].code", 120), "severity": severity, "message": _required_text(raw["message"], f"{label}[{ordinal}].message", 2000)})
    return out


def _policy_revision(policy_revision_id: Any, policy_revision_hash: Any, kind: str) -> tuple[UUID | None, str]:
    parsed = _as_uuid(policy_revision_id, "policy_revision_id", required=False)
    if policy_revision_hash is None:
        policy_hash = sha256_json({"kind": kind, "policy_revision_id": str(parsed) if parsed else None, "version": "default-fail-closed"})
    else:
        policy_hash = _validated_sha256(policy_revision_hash, "policy_revision_hash")
    return parsed, policy_hash


def _nonhuman(actor: Any) -> bool:
    # PASS/CLEARED are human-review decisions.  Treat an absent, malformed or
    # future actor classification as non-human rather than silently granting
    # authority to an unclassified caller.
    actor_type = str(
        _actor_field(actor, "actor_type", _actor_field(actor, "type", "unknown"))
        or "unknown"
    ).strip().lower()
    return actor_type != "human"


def _revision_content_hash(revision: ContentVariantRevision) -> str:
    return sha256_json({
        "content_item_id": str(revision.content_item_id),
        "content_item_hash": revision.content_item_hash,
        "persona_revision_id": str(revision.persona_revision_id),
        "persona_revision_hash": revision.persona_revision_hash,
        "platform_account_id": str(revision.platform_account_id) if revision.platform_account_id else None,
        "platform_account_revision_id": str(revision.platform_account_revision_id) if revision.platform_account_revision_id else None,
        "platform_account_revision_hash": revision.platform_account_revision_hash,
        "platform": revision.platform,
        "payload": revision.payload_json,
        "generation_output_refs": revision.generation_output_refs_json,
        "source_evidence": revision.source_evidence_json,
    })


def _assessment_content_hash(assessment: Any, revision: ContentVariantRevision) -> str:
    """Recompute the append-only QA/Rights digest for readiness projections."""

    policy_id = getattr(assessment, "policy_revision_id", None)
    return sha256_json(
        {
            "revision_id": str(revision.id),
            "revision_hash": revision.content_hash,
            "policy_revision_id": str(policy_id) if policy_id else None,
            "policy_revision_hash": assessment.policy_revision_hash,
            "result": assessment.result,
            "checks": list(getattr(assessment, "checks_json", None) or []),
            "findings": list(getattr(assessment, "findings_json", None) or []),
            "evidence": list(getattr(assessment, "evidence_json", None) or []),
        }
    )


class MediaOperationsContentService(MediaOperationsResearchService):
    """Append-only ContentVariant/QA/Rights service with shared ACL."""

    async def _get_row(self, session: Any, model: Any, entity_id: UUID | str, label: str, *, for_update: bool = False) -> Any:
        parsed = _as_uuid(entity_id, label)
        assert parsed is not None
        statement = select(model).where(model.id == parsed).limit(1)
        if for_update:
            statement = statement.with_for_update()
        row = await self._scalar(session, statement)
        if row is None:
            raise MediaOperationsNotFoundError(f"{label} not found")
        return row

    async def _dependencies(self, session: Any, actor: Any, item: ContentItem, persona_revision_id: UUID | str, platform: str, account_id: UUID | str | None, account_revision_id: UUID | str | None) -> dict[str, Any]:
        await self._assert_entity_access(session, actor, item, permission="write")
        program = await self._get_row(session, EditorialProgram, item.editorial_program_id, "editorial_program_id")
        persona_revision = await self._get_row(session, PersonaRevision, persona_revision_id, "persona_revision_id")
        await self._assert_entity_access(session, actor, persona_revision, permission="read")
        persona = await self._get_row(session, Persona, persona_revision.persona_id, "persona_id")
        if (
            persona.id != program.persona_id
            or persona.owner_user_id != item.owner_user_id
            or persona.project_id != item.project_id
            or persona_revision.owner_user_id != item.owner_user_id
            or persona_revision.project_id != item.project_id
        ):
            raise MediaOperationsValidationError("ContentItem, EditorialProgram, and PersonaRevision must share one Persona and scope")
        account = None
        account_revision = None
        if account_id is not None:
            account = await self._get_row(session, PlatformAccount, account_id, "platform_account_id")
            await self._assert_entity_access(session, actor, account, permission="read")
            if account.platform != platform or account.owner_user_id != item.owner_user_id or account.project_id != item.project_id:
                raise MediaOperationsValidationError("PlatformAccount platform or scope does not match ContentItem")
        if account_revision_id is not None:
            if account is None:
                raise MediaOperationsValidationError("platform_account_id is required with an account revision")
            account_revision = await self._get_row(session, PlatformAccountRevision, account_revision_id, "platform_account_revision_id")
            await self._assert_entity_access(session, actor, account_revision, permission="read")
            if (
                account_revision.platform_account_id != account.id
                or account_revision.owner_user_id != item.owner_user_id
                or account_revision.project_id != item.project_id
            ):
                raise MediaOperationsValidationError("PlatformAccountRevision does not belong to PlatformAccount")
        return {"program": program, "persona": persona, "persona_revision": persona_revision, "account": account, "account_revision": account_revision}

    async def _normalized_revision(self, session: Any, actor: Any, *, item_id: UUID | str, persona_revision_id: UUID | str, platform: Any, account_id: UUID | str | None, account_revision_id: UUID | str | None, payload: Any, generation_output_refs: Any, source_evidence: Any, expected_content_item_hash: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
        platform_value = _platform(platform)
        item = await self._get_row(session, ContentItem, item_id, "content_item_id")
        if expected_content_item_hash is not None:
            expected_hash = _validated_sha256(expected_content_item_hash, "expected_content_item_hash")
            if expected_hash != str(item.content_hash).lower():
                raise MediaOperationsConflictError("ContentItem hash changed since the draft was prepared")
        deps = await self._dependencies(session, actor, item, persona_revision_id, platform_value, account_id, account_revision_id)
        normalized_payload, payload_refs = _normalize_payload(payload, platform_value)
        refs = _normalize_generation_refs(generation_output_refs)
        known = {entry["generation_output_id"] for entry in refs}
        for ref in payload_refs:
            if ref not in known:
                refs.append({"ordinal": len(refs) + 1, "generation_output_id": ref})
                known.add(ref)
        if refs:
            ids = [_uuid(entry["generation_output_id"], "generation_output_id") for entry in refs]
            rows = await self._scalars(session, select(GenerationOutput).where(GenerationOutput.id.in_(ids)))
            by_id = {str(row.id): row for row in rows}
            if len(by_id) != len(ids):
                raise MediaOperationsValidationError("generation_output_refs must point to internal GenerationOutput rows")
            for entry in refs:
                row = by_id[entry["generation_output_id"]]
                if row.owner_user_id != item.owner_user_id or row.project_id != item.project_id:
                    raise MediaOperationsValidationError("GenerationOutput scope does not match ContentItem")
                if entry.get("sha256") is not None and entry["sha256"] != row.sha256:
                    raise MediaOperationsConflictError("GenerationOutput sha256 does not match pinned output")
                entry["sha256"] = row.sha256
        normalized = {
            "content_item_id": item.id,
            "content_item_hash": str(item.content_hash),
            "persona_revision_id": deps["persona_revision"].id,
            "persona_revision_hash": str(deps["persona_revision"].content_hash),
            "platform_account_id": deps["account"].id if deps["account"] else None,
            "platform_account_revision_id": deps["account_revision"].id if deps["account_revision"] else None,
            "platform_account_revision_hash": str(deps["account_revision"].content_hash) if deps["account_revision"] else None,
            "platform": platform_value,
            "payload": normalized_payload,
            "generation_output_refs": refs,
            "source_evidence": _normalize_evidence(source_evidence, "source_evidence"),
        }
        normalized["content_hash"] = sha256_json({k: (str(v) if isinstance(v, UUID) else v) for k, v in normalized.items()})
        return normalized, {"item": item, **deps}

    async def _revision_rows(self, session: Any, variant: ContentVariant) -> list[ContentVariantRevision]:
        return await self._scalars(session, select(ContentVariantRevision).where(ContentVariantRevision.content_variant_id == variant.id).order_by(ContentVariantRevision.version.desc(), ContentVariantRevision.id.desc()).limit(101))

    async def _latest_assessment(self, session: Any, model: Any, revision_id: UUID) -> Any | None:
        return await self._scalar(session, select(model).where(model.content_variant_revision_id == revision_id).order_by(model.created_at.desc(), model.id.desc()).limit(1))

    async def get_readiness(self, session: Any | None = None, actor: Any | None = None, variant_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or variant_id is None:
            raise MediaOperationsValidationError("actor and variant_id are required")
        variant = await self._get_row(session, ContentVariant, variant_id, "variant_id")
        await self._assert_entity_access(session, actor, variant, permission="read")
        revisions = await self._revision_rows(session, variant)
        if not revisions:
            return {"content_variant_id": str(variant.id), "revision_id": None, "revision_hash": None, "ready": False, "blocking_reasons": ["revision_missing"], "qa": None, "rights": None}
        revision = revisions[0]
        qa = await self._latest_assessment(session, QAAssessment, revision.id)
        rights = await self._latest_assessment(session, RightsAssessment, revision.id)
        reasons: list[str] = []
        if qa is not None:
            if qa.content_variant_id != variant.id:
                reasons.append("qa_variant_mismatch")
            elif qa.revision_hash != revision.content_hash:
                reasons.append("qa_revision_mismatch")
            elif qa.assessment_hash != _assessment_content_hash(qa, revision):
                reasons.append("qa_hash_invalid")
        if rights is not None:
            if rights.content_variant_id != variant.id:
                reasons.append("rights_variant_mismatch")
            elif rights.revision_hash != revision.content_hash:
                reasons.append("rights_revision_mismatch")
            elif rights.assessment_hash != _assessment_content_hash(rights, revision):
                reasons.append("rights_hash_invalid")
        if revision.content_hash != _revision_content_hash(revision):
            reasons.append("revision_hash_invalid")
        if revision.platform_account_id is None:
            reasons.append("account_binding_missing")
        if qa is None:
            reasons.append("qa_missing")
        elif qa.result != "passed":
            reasons.append(f"qa_{qa.result}")
        if rights is None:
            reasons.append("rights_missing")
        elif rights.result != "cleared":
            reasons.append(f"rights_{rights.result}")
        return {"content_variant_id": str(variant.id), "variant_id": str(variant.id), "revision_id": str(revision.id), "revision_hash": revision.content_hash, "ready": not reasons, "publication_allowed": not reasons, "status": "ready" if not reasons else "blocked", "qa_result": qa.result if qa else None, "rights_result": rights.result if rights else None, "blockers": list(reasons), "blocking_reasons": reasons, "qa": qa.to_safe_dict() if qa else None, "rights": rights.to_safe_dict() if rights else None}

    async def _variant_detail(self, session: Any, actor: Any, variant: ContentVariant) -> dict[str, Any]:
        await self._assert_entity_access(session, actor, variant, permission="read")
        revisions = await self._revision_rows(session, variant)
        current = revisions[0].to_safe_dict() if revisions else None
        detail = {**variant.to_safe_dict(), "current_revision": current, "revisions": [row.to_safe_dict() for row in revisions[:100]], "revision_history_truncated": len(revisions) > 100, "readiness": await self.get_readiness(session, actor, variant.id)}
        if current:
            for key in ("content_item_hash", "persona_revision_id", "persona_revision_hash", "platform_account_id", "platform_account_revision_id", "platform_account_revision_hash"):
                detail[key] = current.get(key)
        detail["status"] = "ready" if detail["readiness"].get("ready") else "blocked"
        return detail

    async def create_variant(self, session: Any | None = None, actor: Any | None = None, *, content_item_id: UUID | str, platform: Any, persona_revision_id: UUID | str, platform_account_id: UUID | str | None = None, platform_account_revision_id: UUID | str | None = None, payload: Any, generation_output_refs: Any = None, source_evidence: Any = None, expected_content_item_hash: Any = None, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        key = _idempotency_key(idempotency_key)
        normalized, deps = await self._normalized_revision(session, actor, item_id=content_item_id, persona_revision_id=persona_revision_id, platform=platform, account_id=platform_account_id, account_revision_id=platform_account_revision_id, payload=payload, generation_output_refs=generation_output_refs, source_evidence=source_evidence, expected_content_item_hash=expected_content_item_hash)
        create_hash = sha256_json({"content_item_id": str(normalized["content_item_id"]), "platform": normalized["platform"], "persona_revision_id": str(normalized["persona_revision_id"]), "payload": normalized["payload"], "generation_output_refs": normalized["generation_output_refs"], "source_evidence": normalized["source_evidence"]})
        item = deps["item"]
        existing = await self._scalar(session, select(ContentVariant).where(ContentVariant.owner_user_id == item.owner_user_id, ContentVariant.project_id == item.project_id, ContentVariant.content_item_id == item.id, ContentVariant.platform == normalized["platform"]).limit(1))
        if existing is not None:
            if existing.create_hash != create_hash:
                raise MediaOperationsConflictError("ContentVariant already exists with different immutable content")
            return await self._variant_detail(session, actor, existing)
        idem_existing = await self._scalar(session, select(ContentVariant).where(ContentVariant.owner_user_id == item.owner_user_id, ContentVariant.project_id == item.project_id, ContentVariant.idempotency_key == key).limit(1))
        if idem_existing is not None:
            if idem_existing.create_hash != create_hash:
                raise MediaOperationsConflictError("idempotency key was already used with a different ContentVariant payload")
            return await self._variant_detail(session, actor, idem_existing)
        variant = ContentVariant(id=uuid4(), owner_user_id=item.owner_user_id, project_id=item.project_id, content_item_id=item.id, platform=normalized["platform"], create_hash=create_hash, idempotency_key=key, created_by=_actor_id(actor))
        revision = ContentVariantRevision(id=uuid4(), content_variant_id=variant.id, owner_user_id=variant.owner_user_id, project_id=variant.project_id, version=1, content_item_id=normalized["content_item_id"], content_item_hash=normalized["content_item_hash"], persona_revision_id=normalized["persona_revision_id"], persona_revision_hash=normalized["persona_revision_hash"], platform_account_id=normalized["platform_account_id"], platform_account_revision_id=normalized["platform_account_revision_id"], platform_account_revision_hash=normalized["platform_account_revision_hash"], platform=normalized["platform"], payload_json=normalized["payload"], generation_output_refs_json=normalized["generation_output_refs"], source_evidence_json=normalized["source_evidence"], content_hash=normalized["content_hash"], created_by=_actor_id(actor))
        session.add(variant)
        session.add(revision)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(session, select(ContentVariant).where(ContentVariant.owner_user_id == item.owner_user_id, ContentVariant.project_id == item.project_id, ContentVariant.content_item_id == item.id, ContentVariant.platform == normalized["platform"]).limit(1))
            if recovered is not None and recovered.create_hash == create_hash:
                return await self._variant_detail(session, actor, recovered)
            raise MediaOperationsConflictError("ContentVariant conflicts with an existing record") from exc
        return await self._variant_detail(session, actor, variant)

    async def list_variants(self, session: Any | None = None, actor: Any | None = None, *, project_id: UUID | str | None = None, content_item_id: UUID | str | None = None, platform: Any | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        project_uuid = _as_uuid(project_id, "project_id", required=False)
        page_limit, page_offset = _bounded_page(limit, offset)
        conditions = [await self._scope_condition(session, actor, ContentVariant, project_id=project_uuid)]
        if content_item_id is not None:
            conditions.append(ContentVariant.content_item_id == _uuid(content_item_id, "content_item_id"))
        if platform is not None:
            conditions.append(ContentVariant.platform == _platform(platform))
        rows = await self._scalars(session, select(ContentVariant).where(*conditions).order_by(ContentVariant.created_at.desc(), ContentVariant.id.desc()).limit(page_limit).offset(page_offset))
        return [await self._variant_detail(session, actor, row) for row in rows]

    async def get_variant(self, session: Any | None = None, actor: Any | None = None, variant_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or variant_id is None:
            raise MediaOperationsValidationError("actor and variant_id are required")
        return await self._variant_detail(session, actor, await self._get_row(session, ContentVariant, variant_id, "variant_id"))

    async def append_variant_revision(self, session: Any | None = None, actor: Any | None = None, variant_id: UUID | str | None = None, *, expected_version: Any, persona_revision_id: UUID | str, platform_account_id: UUID | str | None = None, platform_account_revision_id: UUID | str | None = None, payload: Any, generation_output_refs: Any = None, source_evidence: Any = None, expected_content_item_hash: Any = None, idempotency_key: Any) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or variant_id is None:
            raise MediaOperationsValidationError("actor and variant_id are required")
        variant = await self._get_row(session, ContentVariant, variant_id, "variant_id", for_update=True)
        await self._assert_entity_access(session, actor, variant, permission="write")
        normalized, deps = await self._normalized_revision(session, actor, item_id=variant.content_item_id, persona_revision_id=persona_revision_id, platform=variant.platform, account_id=platform_account_id, account_revision_id=platform_account_revision_id, payload=payload, generation_output_refs=generation_output_refs, source_evidence=source_evidence, expected_content_item_hash=expected_content_item_hash)
        if deps["item"].project_id != variant.project_id or deps["item"].owner_user_id != variant.owner_user_id:
            raise MediaOperationsValidationError("ContentItem scope does not match ContentVariant")
        key = _idempotency_key(idempotency_key)
        historical_rows = await self._revision_rows(session, variant)
        if historical_rows and normalized["content_item_hash"] != historical_rows[0].content_item_hash:
            raise MediaOperationsConflictError("ContentItem hash changed; create a new ContentVariant for the new source")
        existing = await self._scalar(session, select(ContentVariantRevision).where(ContentVariantRevision.content_variant_id == variant.id, ContentVariantRevision.idempotency_key == key).limit(1))
        if existing is not None:
            if existing.content_hash != normalized["content_hash"]:
                raise MediaOperationsConflictError("idempotency key was already used with different revision content")
            return existing.to_safe_dict()
        current = int(await self._scalar(session, select(func.max(ContentVariantRevision.version)).where(ContentVariantRevision.content_variant_id == variant.id)) or 0)
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError("expected_version must be an integer") from exc
        if expected != current:
            raise MediaOperationsConflictError("stale ContentVariant revision")
        revision = ContentVariantRevision(id=uuid4(), content_variant_id=variant.id, owner_user_id=variant.owner_user_id, project_id=variant.project_id, version=current + 1, content_item_id=normalized["content_item_id"], content_item_hash=normalized["content_item_hash"], persona_revision_id=normalized["persona_revision_id"], persona_revision_hash=normalized["persona_revision_hash"], platform_account_id=normalized["platform_account_id"], platform_account_revision_id=normalized["platform_account_revision_id"], platform_account_revision_hash=normalized["platform_account_revision_hash"], platform=variant.platform, payload_json=normalized["payload"], generation_output_refs_json=normalized["generation_output_refs"], source_evidence_json=normalized["source_evidence"], content_hash=normalized["content_hash"], idempotency_key=key, created_by=_actor_id(actor))
        session.add(revision)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(session, select(ContentVariantRevision).where(ContentVariantRevision.content_variant_id == variant.id, ContentVariantRevision.idempotency_key == key).limit(1))
            if recovered is not None and recovered.content_hash == normalized["content_hash"]:
                return recovered.to_safe_dict()
            raise MediaOperationsConflictError("ContentVariant revision changed concurrently") from exc
        return revision.to_safe_dict()

    async def list_variant_revisions(self, session: Any | None = None, actor: Any | None = None, variant_id: UUID | str | None = None, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None or variant_id is None:
            raise MediaOperationsValidationError("actor and variant_id are required")
        variant = await self._get_row(session, ContentVariant, variant_id, "variant_id")
        await self._assert_entity_access(session, actor, variant, permission="read")
        page_limit, page_offset = _bounded_page(limit, offset)
        rows = await self._scalars(session, select(ContentVariantRevision).where(ContentVariantRevision.content_variant_id == variant.id).order_by(ContentVariantRevision.version.desc(), ContentVariantRevision.id.desc()).limit(page_limit).offset(page_offset))
        return [row.to_safe_dict() for row in rows]

    async def get_revision(self, session: Any | None = None, actor: Any | None = None, revision_id: UUID | str | None = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or revision_id is None:
            raise MediaOperationsValidationError("actor and revision_id are required")
        revision = await self._get_row(session, ContentVariantRevision, revision_id, "revision_id")
        variant = await self._get_row(session, ContentVariant, revision.content_variant_id, "variant_id")
        await self._assert_entity_access(session, actor, variant, permission="read")
        return revision.to_safe_dict()

    async def _assessment_revision(self, session: Any, actor: Any, revision_id: UUID | str) -> tuple[ContentVariant, ContentVariantRevision]:
        revision = await self._get_row(session, ContentVariantRevision, revision_id, "content_variant_revision_id")
        variant = await self._get_row(session, ContentVariant, revision.content_variant_id, "variant_id")
        await self._assert_entity_access(session, actor, variant, permission="write")
        computed = _revision_content_hash(revision)
        if revision.content_hash != computed:
            raise MediaOperationsConflictError("ContentVariant revision hash is invalid")
        return variant, revision

    @staticmethod
    def _assessment_result(value: Any, allowed: tuple[str, ...], label: str) -> str:
        rendered = str(getattr(value, "value", value)).strip().lower()
        if rendered not in allowed:
            raise MediaOperationsValidationError(f"{label} is invalid")
        return rendered

    async def _record_assessment(self, session: Any, actor: Any, *, revision_id: UUID | str, result: Any, checks: Any, findings: Any, evidence: Any, policy_revision_id: UUID | str | None, policy_revision_hash: Any, idempotency_key: Any, kind: str) -> dict[str, Any]:
        variant, revision = await self._assessment_revision(session, actor, revision_id)
        allowed = QA_ASSESSMENT_RESULT_VALUES if kind == "qa" else RIGHTS_ASSESSMENT_RESULT_VALUES
        rendered = self._assessment_result(result, allowed, f"{kind} result")
        normalized_checks = _normalize_checks(checks)
        normalized_findings = _normalize_findings(findings)
        normalized_evidence = _normalize_evidence(evidence)
        mandatory_failures = [item for item in normalized_checks if item["mandatory"] and item["status"] == "failed"]
        passed_value = "passed" if kind == "qa" else "cleared"
        if rendered == passed_value:
            if _nonhuman(actor):
                raise MediaOperationsAuthorizationError(f"only an authorized human may record a {kind} {passed_value} assessment")
            if not normalized_checks:
                raise MediaOperationsValidationError(f"a {kind} {passed_value} assessment requires checks")
            if any(item["status"] != "passed" for item in normalized_checks):
                raise MediaOperationsValidationError(f"all {kind} checks must pass before {passed_value}")
            if mandatory_failures or any(item["severity"] == "error" for item in normalized_findings):
                raise MediaOperationsValidationError(f"mandatory {kind} checks/findings must be resolved before {passed_value}")
        policy_id, policy_hash = _policy_revision(policy_revision_id, policy_revision_hash, kind)
        payload = {"revision_id": str(revision.id), "revision_hash": revision.content_hash, "policy_revision_id": str(policy_id) if policy_id else None, "policy_revision_hash": policy_hash, "result": rendered, "checks": normalized_checks, "findings": normalized_findings, "evidence": normalized_evidence}
        assessment_hash = sha256_json(payload)
        model = QAAssessment if kind == "qa" else RightsAssessment
        existing = await self._scalar(session, select(model).where(model.content_variant_revision_id == revision.id, model.assessment_hash == assessment_hash).limit(1))
        if existing is not None:
            return existing.to_safe_dict()
        key = _idempotency_key(idempotency_key) if idempotency_key is not None else None
        if key is not None:
            idem_existing = await self._scalar(session, select(model).where(model.content_variant_revision_id == revision.id, model.idempotency_key == key).limit(1))
            if idem_existing is not None:
                if idem_existing.assessment_hash != assessment_hash:
                    raise MediaOperationsConflictError("idempotency key was already used with a different assessment")
                return idem_existing.to_safe_dict()
        row = model(id=uuid4(), content_variant_id=variant.id, content_variant_revision_id=revision.id, owner_user_id=variant.owner_user_id, project_id=variant.project_id, revision_hash=revision.content_hash, policy_revision_id=policy_id, policy_revision_hash=policy_hash, result=rendered, checks_json=normalized_checks, findings_json=normalized_findings, evidence_json=normalized_evidence, assessment_hash=assessment_hash, idempotency_key=key, created_by=_actor_id(actor))
        session.add(row)
        try:
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered = await self._scalar(session, select(model).where(model.content_variant_revision_id == revision.id, model.assessment_hash == assessment_hash).limit(1))
            if recovered is not None:
                return recovered.to_safe_dict()
            if key is not None:
                recovered = await self._scalar(session, select(model).where(model.content_variant_revision_id == revision.id, model.idempotency_key == key).limit(1))
                if recovered is not None and recovered.assessment_hash == assessment_hash:
                    return recovered.to_safe_dict()
            raise MediaOperationsConflictError(f"{kind} assessment conflicts with an existing record") from exc
        return row.to_safe_dict()

    async def record_qa(self, session: Any | None = None, actor: Any | None = None, *, content_variant_revision_id: UUID | str, result: Any, checks: Any = None, findings: Any = None, evidence: Any = None, policy_revision_id: UUID | str | None = None, policy_revision_hash: Any = None, idempotency_key: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        return await self._record_assessment(session, actor, revision_id=content_variant_revision_id, result=result, checks=checks, findings=findings, evidence=evidence, policy_revision_id=policy_revision_id, policy_revision_hash=policy_revision_hash, idempotency_key=idempotency_key, kind="qa")

    async def record_rights(self, session: Any | None = None, actor: Any | None = None, *, content_variant_revision_id: UUID | str, result: Any, checks: Any = None, findings: Any = None, evidence: Any = None, policy_revision_id: UUID | str | None = None, policy_revision_hash: Any = None, idempotency_key: Any = None) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        return await self._record_assessment(session, actor, revision_id=content_variant_revision_id, result=result, checks=checks, findings=findings, evidence=evidence, policy_revision_id=policy_revision_id, policy_revision_hash=policy_revision_hash, idempotency_key=idempotency_key, kind="rights")

    async def list_qa_assessments(self, session: Any | None = None, actor: Any | None = None, *, content_variant_revision_id: UUID | str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        revision = await self._get_row(session, ContentVariantRevision, content_variant_revision_id, "content_variant_revision_id")
        variant = await self._get_row(session, ContentVariant, revision.content_variant_id, "variant_id")
        await self._assert_entity_access(session, actor, variant, permission="read")
        page_limit, page_offset = _bounded_page(limit, offset)
        rows = await self._scalars(session, select(QAAssessment).where(QAAssessment.content_variant_revision_id == revision.id).order_by(QAAssessment.created_at.desc(), QAAssessment.id.desc()).limit(page_limit).offset(page_offset))
        return [row.to_safe_dict() for row in rows]

    async def list_rights_assessments(self, session: Any | None = None, actor: Any | None = None, *, content_variant_revision_id: UUID | str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError("actor is required")
        revision = await self._get_row(session, ContentVariantRevision, content_variant_revision_id, "content_variant_revision_id")
        variant = await self._get_row(session, ContentVariant, revision.content_variant_id, "variant_id")
        await self._assert_entity_access(session, actor, variant, permission="read")
        page_limit, page_offset = _bounded_page(limit, offset)
        rows = await self._scalars(session, select(RightsAssessment).where(RightsAssessment.content_variant_revision_id == revision.id).order_by(RightsAssessment.created_at.desc(), RightsAssessment.id.desc()).limit(page_limit).offset(page_offset))
        return [row.to_safe_dict() for row in rows]

    # Compatibility aliases used by route slices and callers that spell out
    # the domain object in the method name.
    create_content_variant = create_variant
    list_content_variants = list_variants
    get_content_variant = get_variant
    append_content_variant_revision = append_variant_revision
    get_content_variant_revision = get_revision
    list_content_variant_revisions = list_variant_revisions
    record_qa_assessment = record_qa
    record_rights_assessment = record_rights
    get_variant_readiness = get_readiness


__all__ = ["MediaOperationsContentService", "_normalize_payload", "_normalize_generation_refs"]
