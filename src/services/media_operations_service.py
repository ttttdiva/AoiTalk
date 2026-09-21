"""Typed Media Operations service for Persona Core.

This service owns every MediaOps mutation and applies the same owner/project ACL
model as the existing Operations service.  It performs no provider calls and
does not accept credentials, filesystem paths, or unbounded metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from inspect import isawaitable
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    MEDIA_PLATFORM_VALUES,
    ArtifactVersion,
    Persona,
    PersonaIntakeSlot,
    PersonaResource,
    PersonaRevision,
    Project,
    ProjectMember,
)
from .project_context import has_project_read_access
from .project_permissions import has_effective_project_permission


class MediaOperationsError(RuntimeError):
    status_code = 400


class MediaOperationsNotFoundError(MediaOperationsError):
    status_code = 404


class MediaOperationsAuthorizationError(MediaOperationsError, PermissionError):
    status_code = 403


class MediaOperationsConflictError(MediaOperationsError):
    status_code = 409


class MediaOperationsValidationError(MediaOperationsError, ValueError):
    status_code = 422


class MediaCredentialVaultUnavailableError(MediaOperationsError):
    """The credential vault cannot complete a command because key material is unavailable."""

    status_code = 503


_RESOURCE_KINDS = frozenset(
    {
        "profile",
        "reference",
        "asset",
        "persona_bible",
        "character_bible",
        "world_bible",
        "visual_style_reference",
        "reference_image",
        "posting_rule",
        "platform_rule",
        "sensitive_rule",
        "forbidden_content_rule",
        "ip_rights_rule",
        "monetization_rule",
        "kpi_definition",
        "experiment_policy",
        "topic_source",
        "idea_bank",
        "high_performing_content",
        "supporting_document",
    }
)
_PERSONA_STATES = frozenset({"draft", "active", "paused", "archived"})
_POLICY_SECRET_TOKENS = frozenset(
    {
        "password",
        "secret",
        "token",
        "credential",
        "cookie",
        "api_key",
        "apikey",
        "key",
        "auth",
        "authorization",
        "bearer",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "client_secret",
        "clientsecret",
    }
)
_MAX_QUERY_DECODE_ROUNDS = 8
_MAX_NESTED_URL_DEPTH = 3

# ``PersonaRevision.to_safe_dict`` is intentionally a richer DTO than the
# editable content accepted by the service.  Keep this allow-list explicit so
# PATCH cannot accidentally copy identity/metadata fields into a revision.
_REVISION_CONTENT_FIELDS = (
    "display_name",
    "summary",
    "voice",
    "audience",
    "platforms",
    "content_pillars",
    "public_aliases",
    "niche",
    "positioning",
    "visual_identity",
    "creative_direction",
    "allowed_subjects",
    "prohibited_subjects",
    "adult_policy",
    "sensitive_policy",
    "ip_policy",
    "disclosure_policy",
    "monetization_policy",
    "kpi_objectives",
    "default_language",
    "locale",
    "timezone",
    "research_policy",
    "image_production_policy",
    "video_production_policy",
)

_SENSITIVE_QUERY_EXACT = frozenset(
    {
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "auth_code",
        "authcode",
        "bearer",
        "client_secret",
        "clientsecret",
        "code",
        "cookie",
        "credential",
        "jwt",
        "key",
        "nonce",
        "oauth_code",
        "oauthcode",
        "oauth_state",
        "oauthstate",
        "password",
        "passphrase",
        "refresh_token",
        "refreshtoken",
        "relay_state",
        "relaystate",
        "secret",
        "session",
        "session_id",
        "sessionid",
        "session_key",
        "sessionkey",
        "sid",
        "sig",
        "signature",
        "state",
        "ticket",
        "token",
    }
)

_SENSITIVE_QUERY_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "secret",
        "session",
        "signature",
        "token",
    }
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _as_uuid(
    value: UUID | str | None,
    label: str,
    *,
    required: bool = True,
) -> UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise MediaOperationsValidationError(f"{label} is required")
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise MediaOperationsValidationError(
            f"{label} is not a valid UUID"
        ) from exc


def _actor_field(actor: Any, name: str, default: Any = None) -> Any:
    if isinstance(actor, Mapping):
        return actor.get(name, default)
    return getattr(actor, name, default)


def _actor_id(actor: Any) -> UUID:
    raw = _actor_field(actor, "id") or _actor_field(actor, "user_id")
    value = _as_uuid(raw, "actor id")
    assert value is not None
    return value


def _required_text(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise MediaOperationsValidationError(f"{label} must be a string")
    rendered = value.strip()
    if not rendered:
        raise MediaOperationsValidationError(f"{label} must not be empty")
    if len(rendered) > max_length:
        raise MediaOperationsValidationError(
            f"{label} exceeds maximum length"
        )
    return rendered


def _optional_text(value: Any, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MediaOperationsValidationError(f"{label} must be a string")
    rendered = value.strip()
    if not rendered:
        return None
    if len(rendered) > max_length:
        raise MediaOperationsValidationError(
            f"{label} exceeds maximum length"
        )
    return rendered


def _idempotency_key(value: Any) -> str:
    return _required_text(value, "idempotency_key", 255)


def _intake_slot(value: Any) -> int | None:
    # Slot-free Characters intentionally bypass the legacy intake table.  The
    # compatibility Persona API still accepts the historic 1..9 slots.
    if value is None:
        return None
    if isinstance(value, bool):
        raise MediaOperationsValidationError(
            "intake_slot must be an integer between 1 and 9"
        )
    try:
        slot = int(value)
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError(
            "intake_slot must be an integer between 1 and 9"
        ) from exc
    if slot < 1 or slot > 9:
        raise MediaOperationsValidationError(
            "intake_slot must be between 1 and 9"
        )
    return slot


def _bounded_page(
    limit: Any,
    offset: Any,
    *,
    maximum: int = 100,
) -> tuple[int, int]:
    if isinstance(limit, bool) or isinstance(offset, bool):
        raise MediaOperationsValidationError(
            "limit and offset must be integers"
        )
    try:
        page_limit = int(limit)
        page_offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError(
            "limit and offset must be integers"
        ) from exc
    if page_limit < 1 or page_limit > maximum:
        raise MediaOperationsValidationError(
            f"limit must be between 1 and {maximum}"
        )
    if page_offset < 0:
        raise MediaOperationsValidationError(
            "offset must be a non-negative integer"
        )
    return page_limit, page_offset


def _normalize_platforms(values: Sequence[Any] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError("platforms must be a list")
    if len(values) > len(MEDIA_PLATFORM_VALUES):
        raise MediaOperationsValidationError(
            "platforms exceeds the closed platform set"
        )

    selected: set[str] = set()
    for raw in values:
        value = getattr(raw, "value", raw)
        rendered = str(value).strip().lower()
        if rendered not in MEDIA_PLATFORM_VALUES:
            raise MediaOperationsValidationError(
                "platform must be one of "
                "x, pixiv, dlsite, patreon, youtube, instagram"
            )
        selected.add(rendered)

    return [
        platform
        for platform in MEDIA_PLATFORM_VALUES
        if platform in selected
    ]


def _normalize_content_pillars(
    values: Sequence[Any] | None,
) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError(
            "content_pillars must be a list"
        )
    if len(values) > 20:
        raise MediaOperationsValidationError(
            "content_pillars exceeds 20 items"
        )

    result: list[str] = []
    for raw in values:
        pillar = _required_text(raw, "content_pillar", 200)
        if pillar not in result:
            result.append(pillar)
    return result


def _normalize_bounded_string_list(
    values: Sequence[Any] | None,
    label: str,
    *,
    maximum: int = 20,
    item_length: int = 255,
) -> list[str]:
    """Normalize optional policy lists without accepting arbitrary blobs."""

    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MediaOperationsValidationError(f"{label} must be a list")
    if len(values) > maximum:
        raise MediaOperationsValidationError(f"{label} exceeds {maximum} items")
    result: list[str] = []
    for raw in values:
        value = _required_text(raw, label.rstrip("s"), item_length)
        if value not in result:
            result.append(value)
    return result


def _normalize_policy_object(
    value: Any,
    label: str,
    *,
    maximum_keys: int = 24,
    value_length: int = 1000,
) -> dict[str, Any]:
    """Accept a small, scalar policy object while rejecting secret-shaped data."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MediaOperationsValidationError(f"{label} must be an object")
    if len(value) > maximum_keys:
        raise MediaOperationsValidationError(f"{label} contains too many fields")

    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, f"{label} key", 64)
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
        if not normalized_key or normalized_key in _POLICY_SECRET_TOKENS or any(
            token in normalized_key.split("_") for token in _POLICY_SECRET_TOKENS
        ):
            raise MediaOperationsValidationError(f"{label} contains a protected field")
        if isinstance(raw_value, bool) or raw_value is None:
            result[key] = raw_value
            continue
        if isinstance(raw_value, (int, float)):
            if isinstance(raw_value, float) and (raw_value != raw_value or raw_value in (float("inf"), float("-inf"))):
                raise MediaOperationsValidationError(f"{label} values must be finite")
            result[key] = raw_value
            continue
        if isinstance(raw_value, str):
            rendered = raw_value.strip()
            if len(rendered) > value_length:
                raise MediaOperationsValidationError(f"{label} value exceeds maximum length")
            if any(ord(character) < 0x20 and character not in "\t\n" for character in rendered):
                raise MediaOperationsValidationError(f"{label} value contains control characters")
            result[key] = rendered
            continue
        if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes, bytearray)):
            result[key] = _normalize_bounded_string_list(
                raw_value,
                f"{label}.{key}",
                maximum=20,
                item_length=value_length,
            )
            continue
        raise MediaOperationsValidationError(f"{label} values must be scalar or string lists")
    return result


def _normalize_persona_state(value: Any) -> str:
    rendered = str(getattr(value, "value", value) or "draft").strip().lower()
    if rendered not in _PERSONA_STATES:
        raise MediaOperationsValidationError("state must be draft, active, paused, or archived")
    return rendered


def _normalize_revision_input(
    *,
    display_name: Any,
    summary: Any = None,
    voice: Any = None,
    audience: Any = None,
    platforms: Sequence[Any] | None = None,
    content_pillars: Sequence[Any] | None = None,
    public_aliases: Sequence[Any] | None = None,
    niche: Any = None,
    positioning: Any = None,
    visual_identity: Any = None,
    creative_direction: Any = None,
    allowed_subjects: Sequence[Any] | None = None,
    prohibited_subjects: Sequence[Any] | None = None,
    adult_policy: Any = None,
    sensitive_policy: Any = None,
    ip_policy: Any = None,
    disclosure_policy: Any = None,
    monetization_policy: Any = None,
    kpi_objectives: Sequence[Any] | None = None,
    default_language: Any = None,
    locale: Any = None,
    timezone: Any = None,
    research_policy: Any = None,
    image_production_policy: Any = None,
    video_production_policy: Any = None,
) -> dict[str, Any]:
    return {
        "display_name": _required_text(
            display_name,
            "display_name",
            120,
        ),
        "summary": _optional_text(summary, "summary", 4000),
        "voice": _optional_text(voice, "voice", 4000),
        "audience": _optional_text(audience, "audience", 4000),
        "platforms": _normalize_platforms(platforms),
        "content_pillars": _normalize_content_pillars(
            content_pillars
        ),
        "public_aliases": _normalize_bounded_string_list(public_aliases, "public_aliases", maximum=20, item_length=120),
        "niche": _optional_text(niche, "niche", 1000),
        "positioning": _optional_text(positioning, "positioning", 2000),
        "visual_identity": _normalize_policy_object(visual_identity, "visual_identity"),
        "creative_direction": _optional_text(creative_direction, "creative_direction", 4000),
        "allowed_subjects": _normalize_bounded_string_list(allowed_subjects, "allowed_subjects", maximum=40, item_length=255),
        "prohibited_subjects": _normalize_bounded_string_list(prohibited_subjects, "prohibited_subjects", maximum=40, item_length=255),
        "adult_policy": _optional_text(adult_policy, "adult_policy", 32),
        "sensitive_policy": _optional_text(sensitive_policy, "sensitive_policy", 32),
        "ip_policy": _optional_text(ip_policy, "ip_policy", 32),
        "disclosure_policy": _optional_text(disclosure_policy, "disclosure_policy", 32),
        "monetization_policy": _normalize_policy_object(monetization_policy, "monetization_policy"),
        "kpi_objectives": _normalize_bounded_string_list(kpi_objectives, "kpi_objectives", maximum=20, item_length=255),
        "default_language": _optional_text(default_language, "default_language", 16),
        "locale": _optional_text(locale, "locale", 64),
        "timezone": _optional_text(timezone, "timezone", 64),
        "research_policy": _normalize_policy_object(research_policy, "research_policy"),
        "image_production_policy": _normalize_policy_object(image_production_policy, "image_production_policy"),
        "video_production_policy": _normalize_policy_object(video_production_policy, "video_production_policy"),
    }


def _decode_component(value: str, *, plus_as_space: bool = False) -> str | None:
    current = value.replace("+", " ") if plus_as_space else value
    for _ in range(_MAX_QUERY_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            return current
        current = decoded
    return None if unquote(current) != current else current


def _normalized_security_key(value: str) -> tuple[str, str]:
    rendered = str(value).strip()
    rendered = re.sub(
        r"([A-Z]+)([A-Z][a-z])",
        r"\1_\2",
        rendered,
    )
    rendered = re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1_\2",
        rendered,
    )
    rendered = rendered.casefold()
    separated = re.sub(
        r"[^a-z0-9]+",
        "_",
        rendered,
    ).strip("_")
    return separated, separated.replace("_", "")


def _is_sensitive_query_key(value: str) -> bool:
    decoded = _decode_component(value, plus_as_space=True)
    if decoded is None:
        return True
    separated, compact = _normalized_security_key(decoded)
    if separated in _SENSITIVE_QUERY_EXACT:
        return True
    if compact in _SENSITIVE_QUERY_EXACT:
        return True
    return bool(
        set(separated.split("_")).intersection(
            _SENSITIVE_QUERY_TOKENS
        )
    )


def _validated_resource_url(
    value: Any,
    *,
    depth: int = 0,
) -> str:
    rendered = _required_text(value, "provenance.url", 4000)
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in rendered
    ):
        raise MediaOperationsValidationError(
            "provenance.url contains control characters"
        )
    if any(character.isspace() for character in rendered):
        raise MediaOperationsValidationError(
            "provenance.url must not contain whitespace"
        )
    if "\\" in rendered:
        raise MediaOperationsValidationError(
            "provenance.url must be an HTTP(S) URL"
        )

    try:
        parsed = urlsplit(rendered)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            raise MediaOperationsValidationError(
                "provenance.url must be an HTTP(S) URL"
            )
        if not parsed.netloc or parsed.hostname is None:
            raise MediaOperationsValidationError(
                "provenance.url must be an absolute HTTP(S) URL"
            )
        if parsed.username is not None or parsed.password is not None:
            raise MediaOperationsValidationError(
                "provenance.url must not contain userinfo"
            )
        _ = parsed.port
    except MediaOperationsValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise MediaOperationsValidationError(
            "provenance.url is invalid"
        ) from exc

    for component in re.split(r"[&;]", parsed.query):
        if not component:
            continue
        raw_key, separator, raw_value = component.partition("=")
        if _is_sensitive_query_key(raw_key):
            raise MediaOperationsValidationError(
                "provenance.url contains a sensitive query parameter"
            )
        if separator and raw_value:
            decoded_value = _decode_component(raw_value)
            if decoded_value is None:
                raise MediaOperationsValidationError(
                    "provenance.url contains unstable encoded data"
                )
            candidate = decoded_value.strip()
            if candidate.lower().startswith(
                ("http://", "https://")
            ):
                if depth >= _MAX_NESTED_URL_DEPTH:
                    raise MediaOperationsValidationError(
                        "provenance.url nesting is too deep"
                    )
                _validated_resource_url(
                    candidate,
                    depth=depth + 1,
                )

    return urlunsplit(
        (
            scheme,
            parsed.netloc,
            parsed.path,
            parsed.query,
            "",
        )
    )


def _validated_sha256(value: Any, label: str) -> str:
    rendered = _required_text(value, label, 64).lower()
    if len(rendered) != 64 or any(
        character not in "0123456789abcdef"
        for character in rendered
    ):
        raise MediaOperationsValidationError(
            f"{label} must be a SHA-256 value"
        )
    return rendered


def _normalize_resource_input(
    *,
    resource_kind: Any,
    platform: Any = None,
    label: Any = None,
    provenance: Any,
) -> dict[str, Any]:
    kind = _required_text(
        resource_kind,
        "resource_kind",
        32,
    ).lower()
    if kind not in _RESOURCE_KINDS:
        raise MediaOperationsValidationError(
            "resource_kind is not supported"
        )

    platform_value: str | None = None
    if platform is not None:
        platform_value = _normalize_platforms([platform])[0]

    label_value = _optional_text(label, "label", 255)

    if not isinstance(provenance, Mapping):
        raise MediaOperationsValidationError(
            "provenance must be a typed object"
        )
    provenance_dict = {
        str(key): value
        for key, value in provenance.items()
    }
    provenance_type = str(
        provenance_dict.get("type") or ""
    ).strip().lower()

    source_url: str | None = None
    artifact_id: UUID | None = None
    artifact_sha256: str | None = None
    artifact_mime_type: str | None = None

    if provenance_type == "url":
        if set(provenance_dict) != {"type", "url"}:
            raise MediaOperationsValidationError(
                "url provenance accepts only type and url"
            )
        source_url = _validated_resource_url(
            provenance_dict.get("url")
        )
        normalized_provenance: dict[str, Any] = {
            "type": "url",
            "url": source_url,
        }
    elif provenance_type == "artifact":
        if set(provenance_dict) != {
            "type",
            "sha256",
            "mime_type",
        }:
            raise MediaOperationsValidationError(
                "artifact provenance accepts only "
                "type, sha256, and mime_type"
            )
        artifact_sha256 = _validated_sha256(
            provenance_dict.get("sha256"),
            "provenance.sha256",
        )
        artifact_mime_type = _required_text(
            provenance_dict.get("mime_type"),
            "provenance.mime_type",
            255,
        )
        normalized_provenance = {
            "type": "artifact",
            "sha256": artifact_sha256,
            "mime_type": artifact_mime_type,
        }
    elif provenance_type == "stored_artifact":
        if set(provenance_dict) != {"type", "artifact_id"}:
            raise MediaOperationsValidationError(
                "stored_artifact provenance accepts only "
                "type and artifact_id"
            )
        artifact_id = _as_uuid(
            provenance_dict.get("artifact_id"),
            "provenance.artifact_id",
        )
        assert artifact_id is not None
        normalized_provenance = {
            "type": "stored_artifact",
            "artifact_id": str(artifact_id),
        }
    else:
        raise MediaOperationsValidationError(
            "provenance.type must be url, artifact, or stored_artifact"
        )

    hash_payload = {
        "resource_kind": kind,
        "platform": platform_value,
        "label": label_value,
        "provenance": normalized_provenance,
    }

    return {
        "resource_kind": kind,
        "platform": platform_value,
        "label": label_value,
        "provenance_type": provenance_type,
        "source_url": source_url,
        "artifact_id": artifact_id,
        "artifact_sha256": artifact_sha256,
        "artifact_mime_type": artifact_mime_type,
        "resource_hash": sha256_json(hash_payload),
    }


def _escape_like(value: str) -> str:
    """Escape SQL LIKE metacharacters while retaining substring search."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _revision_content_from_safe_dict(
    revision: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a safe revision DTO down to editable content fields."""

    return {
        field: revision.get(field)
        for field in _REVISION_CONTENT_FIELDS
    }


class MediaOperationsService:
    """Application service for typed MediaOps Persona state."""

    def __init__(self, session: Any | None = None):
        self.session = session

    def _resolve_session(self, session: Any | None) -> Any:
        resolved = (
            session
            if session is not None
            else self.session
        )
        if resolved is None:
            raise MediaOperationsValidationError(
                "database session is required"
            )
        return resolved

    async def _execute(
        self,
        session: Any,
        statement: Any,
    ) -> Any:
        result = session.execute(statement)
        if isawaitable(result):
            result = await result
        return result

    async def _scalar(
        self,
        session: Any,
        statement: Any,
    ) -> Any:
        result = await self._execute(session, statement)
        scalar = getattr(result, "scalar", None)
        if callable(scalar):
            return scalar()
        scalar_one_or_none = getattr(
            result,
            "scalar_one_or_none",
            None,
        )
        if callable(scalar_one_or_none):
            return scalar_one_or_none()
        return result

    async def _scalars(
        self,
        session: Any,
        statement: Any,
    ) -> list[Any]:
        result = await self._execute(session, statement)
        scalars = getattr(result, "scalars", None)
        if callable(scalars):
            return list(scalars().all())
        return list(result or [])

    async def _flush_only(self, session: Any) -> None:
        flush = getattr(session, "flush", None)
        if callable(flush):
            result = flush()
            if isawaitable(result):
                await result

    async def _commit(self, session: Any) -> None:
        commit = getattr(session, "commit", None)
        if callable(commit):
            result = commit()
            if isawaitable(result):
                await result

    async def _flush_commit(self, session: Any) -> None:
        await self._flush_only(session)
        await self._commit(session)

    async def _rollback(self, session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            result = rollback()
            if isawaitable(result):
                await result

    async def _get_project(
        self,
        session: Any,
        project_id: UUID | None,
    ) -> Project | None:
        if project_id is None:
            return None
        return await self._scalar(
            session,
            select(Project)
            .where(Project.id == project_id)
            .limit(1),
        )

    async def _assert_access(
        self,
        session: Any,
        actor: Any,
        *,
        project_id: UUID | None,
        owner_user_id: UUID | None = None,
        permission: str = "read",
    ) -> UUID:
        actor_id = _actor_id(actor)
        role = str(
            _actor_field(actor, "role", "") or ""
        ).strip().lower()

        if project_id is None:
            if (
                owner_user_id is not None
                and actor_id == owner_user_id
            ):
                return actor_id
            if role == "admin":
                return actor_id
            raise MediaOperationsAuthorizationError(
                "media operation access denied"
            )

        project = await self._get_project(
            session,
            project_id,
        )
        if (
            project is None
            or getattr(project, "deleted_at", None)
            is not None
        ):
            raise MediaOperationsNotFoundError(
                "project not found"
            )
        if role == "admin":
            return actor_id

        if permission == "read":
            allowed = await has_project_read_access(
                session,
                project,
                user_id=str(actor_id),
                user_role=role or None,
            )
        else:
            member = await self._scalar(
                session,
                select(ProjectMember)
                .where(
                    ProjectMember.project_id
                    == project_id,
                    ProjectMember.user_id
                    == actor_id,
                )
                .limit(1),
            )
            allowed = has_effective_project_permission(
                user_id=actor_id,
                user_role=role,
                project_owner_id=getattr(
                    project,
                    "owner_id",
                    None,
                ),
                member_permissions=getattr(
                    member,
                    "permissions",
                    None,
                ),
                permission=permission,
            )

        if not allowed:
            raise MediaOperationsAuthorizationError(
                "media operation access denied"
            )
        return actor_id

    async def _assert_create_scope(
        self,
        session: Any,
        actor: Any,
        project_id: UUID | None,
    ) -> UUID:
        actor_id = _actor_id(actor)
        if project_id is None:
            return actor_id
        await self._assert_access(
            session,
            actor,
            project_id=project_id,
            permission="write",
        )
        return actor_id

    async def _assert_entity_access(
        self,
        session: Any,
        actor: Any,
        entity: Any,
        *,
        permission: str,
    ) -> UUID:
        return await self._assert_access(
            session,
            actor,
            project_id=getattr(
                entity,
                "project_id",
                None,
            ),
            owner_user_id=getattr(
                entity,
                "owner_user_id",
                None,
            ),
            permission=permission,
        )

    async def _authorized_project_ids(
        self,
        session: Any,
        actor: Any,
    ) -> list[UUID]:
        actor_id = _actor_id(actor)
        role = str(
            _actor_field(actor, "role", "") or ""
        ).strip().lower() or None

        if role == "admin":
            return await self._scalars(
                session,
                select(Project.id)
                .where(Project.deleted_at.is_(None))
                .order_by(
                    Project.created_at.asc(),
                    Project.id.asc(),
                ),
            )

        result = await self._execute(
            session,
            select(
                Project.id,
                Project.owner_id,
                ProjectMember.permissions,
            )
            .outerjoin(
                ProjectMember,
                and_(
                    ProjectMember.project_id
                    == Project.id,
                    ProjectMember.user_id
                    == actor_id,
                ),
            )
            .where(
                Project.deleted_at.is_(None),
                or_(
                    Project.owner_id == actor_id,
                    ProjectMember.user_id == actor_id,
                ),
            )
            .order_by(
                Project.created_at.asc(),
                Project.id.asc(),
            ),
        )
        rows = (
            result.all()
            if callable(getattr(result, "all", None))
            else list(result or [])
        )
        return [
            project_id
            for project_id, owner_id, permissions
            in rows
            if has_effective_project_permission(
                user_id=actor_id,
                user_role=role,
                project_owner_id=owner_id,
                member_permissions=permissions,
                permission="read",
            )
        ]

    async def _scope_less_condition(
        self,
        session: Any,
        actor: Any,
    ) -> Any:
        actor_id = _actor_id(actor)
        project_ids = await self._authorized_project_ids(
            session,
            actor,
        )
        personal = and_(
            Persona.project_id.is_(None),
            Persona.owner_user_id == actor_id,
        )
        if not project_ids:
            return personal
        return or_(
            personal,
            Persona.project_id.in_(project_ids),
        )

    async def _get_persona_row(
        self,
        session: Any,
        persona_id: UUID | str,
        *,
        for_update: bool = False,
    ) -> Persona:
        parsed = _as_uuid(
            persona_id,
            "persona_id",
        )
        assert parsed is not None
        statement = (
            select(Persona)
            .where(Persona.id == parsed)
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        persona = await self._scalar(
            session,
            statement,
        )
        if persona is None:
            raise MediaOperationsNotFoundError(
                "persona not found"
            )
        return persona

    async def _find_persona_by_idempotency(
        self,
        session: Any,
        *,
        owner_user_id: UUID,
        project_id: UUID | None,
        idempotency_key: str,
    ) -> Persona | None:
        conditions = [
            Persona.idempotency_key
            == idempotency_key,
        ]
        if project_id is None:
            conditions.extend(
                [
                    Persona.project_id.is_(None),
                    Persona.owner_user_id
                    == owner_user_id,
                ]
            )
        else:
            conditions.append(
                Persona.project_id == project_id
            )
        return await self._scalar(
            session,
            select(Persona)
            .where(*conditions)
            .limit(1),
        )

    async def _find_slot(
        self,
        session: Any,
        *,
        owner_user_id: UUID,
        project_id: UUID | None,
        slot: int,
    ) -> PersonaIntakeSlot | None:
        conditions = [
            PersonaIntakeSlot.slot == slot,
        ]
        if project_id is None:
            conditions.extend(
                [
                    PersonaIntakeSlot.project_id.is_(None),
                    PersonaIntakeSlot.owner_user_id
                    == owner_user_id,
                ]
            )
        else:
            conditions.append(
                PersonaIntakeSlot.project_id
                == project_id
            )
        return await self._scalar(
            session,
            select(PersonaIntakeSlot)
            .where(*conditions)
            .limit(1),
        )

    async def _find_revision_by_idempotency(
        self,
        session: Any,
        *,
        persona_id: UUID,
        idempotency_key: str,
    ) -> PersonaRevision | None:
        return await self._scalar(
            session,
            select(PersonaRevision)
            .where(
                PersonaRevision.persona_id
                == persona_id,
                PersonaRevision.idempotency_key
                == idempotency_key,
            )
            .limit(1),
        )

    async def _latest_revision(
        self,
        session: Any,
        persona_id: UUID,
    ) -> PersonaRevision | None:
        return await self._scalar(
            session,
            select(PersonaRevision)
            .where(PersonaRevision.persona_id == persona_id)
            .order_by(
                PersonaRevision.version.desc(),
                PersonaRevision.id.desc(),
            )
            .limit(1),
        )

    async def _find_resource_by_idempotency(
        self,
        session: Any,
        *,
        persona_id: UUID,
        idempotency_key: str,
    ) -> PersonaResource | None:
        return await self._scalar(
            session,
            select(PersonaResource)
            .where(
                PersonaResource.persona_id
                == persona_id,
                PersonaResource.idempotency_key
                == idempotency_key,
            )
            .limit(1),
        )

    def _build_revision(
        self,
        *,
        persona: Persona,
        version: int,
        content: Mapping[str, Any],
        content_hash: str,
        created_by: UUID,
        idempotency_key: str | None,
    ) -> PersonaRevision:
        platforms = set(
            content["platforms"]
        )
        return PersonaRevision(
            persona_id=persona.id,
            owner_user_id=persona.owner_user_id,
            project_id=persona.project_id,
            version=version,
            display_name=content["display_name"],
            summary=content["summary"],
            voice=content["voice"],
            audience=content["audience"],
            niche=content.get("niche"),
            positioning=content.get("positioning"),
            visual_identity_json=dict(content.get("visual_identity") or {}),
            creative_direction=content.get("creative_direction"),
            allowed_subjects_json=list(content.get("allowed_subjects") or []),
            prohibited_subjects_json=list(content.get("prohibited_subjects") or []),
            adult_policy=content.get("adult_policy"),
            sensitive_policy=content.get("sensitive_policy"),
            ip_policy=content.get("ip_policy"),
            disclosure_policy=content.get("disclosure_policy"),
            monetization_policy_json=dict(content.get("monetization_policy") or {}),
            kpi_objectives_json=list(content.get("kpi_objectives") or []),
            default_language=content.get("default_language"),
            locale=content.get("locale"),
            timezone=content.get("timezone"),
            research_policy_json=dict(content.get("research_policy") or {}),
            image_production_policy_json=dict(content.get("image_production_policy") or {}),
            video_production_policy_json=dict(content.get("video_production_policy") or {}),
            public_aliases_json=list(content.get("public_aliases") or []),
            platform_x="x" in platforms,
            platform_pixiv="pixiv" in platforms,
            platform_dlsite="dlsite" in platforms,
            platform_patreon="patreon" in platforms,
            platform_youtube="youtube" in platforms,
            platform_instagram="instagram"
            in platforms,
            content_pillars_json=list(
                content["content_pillars"]
            ),
            content_hash=content_hash,
            idempotency_key=idempotency_key,
            created_by=created_by,
        )

    async def _summary_map(
        self,
        session: Any,
        personas: Sequence[Persona],
    ) -> dict[UUID, dict[str, Any]]:
        if not personas:
            return {}

        persona_ids = [
            persona.id
            for persona in personas
        ]
        revisions = await self._scalars(
            session,
            select(PersonaRevision)
            .where(
                PersonaRevision.persona_id.in_(
                    persona_ids
                )
            )
            .order_by(
                PersonaRevision.persona_id.asc(),
                PersonaRevision.version.desc(),
            ),
        )
        latest: dict[UUID, PersonaRevision] = {}
        for revision in revisions:
            if revision.persona_id not in latest:
                latest[revision.persona_id] = revision

        result: dict[UUID, dict[str, Any]] = {}
        for persona in personas:
            revision = latest.get(persona.id)
            if revision is None:
                raise MediaOperationsConflictError(
                    "persona revision history is incomplete"
                )
            result[persona.id] = {
                **persona.to_safe_dict(),
                "current_revision": (
                    revision.to_safe_dict()
                ),
            }
        return result

    async def _detail_for_row(
        self,
        session: Any,
        persona: Persona,
    ) -> dict[str, Any]:
        revisions = await self._scalars(
            session,
            select(PersonaRevision)
            .where(
                PersonaRevision.persona_id
                == persona.id
            )
            .order_by(
                PersonaRevision.version.desc(),
                PersonaRevision.id.desc(),
            )
            .limit(101),
        )
        if not revisions:
            raise MediaOperationsConflictError(
                "persona revision history is incomplete"
            )
        visible = revisions[:100]
        return {
            **persona.to_safe_dict(),
            "current_revision": (
                visible[0].to_safe_dict()
            ),
            "revisions": [
                revision.to_safe_dict()
                for revision in visible
            ],
            "revision_history_truncated": (
                len(revisions) > 100
            ),
        }

    async def get_persona_intake(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError(
                "actor is required"
            )

        actor_id = _actor_id(actor)
        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )

        if project_uuid is None:
            conditions = [
                PersonaIntakeSlot.project_id.is_(None),
                PersonaIntakeSlot.owner_user_id
                == actor_id,
            ]
        else:
            await self._assert_access(
                session,
                actor,
                project_id=project_uuid,
                permission="read",
            )
            conditions = [
                PersonaIntakeSlot.project_id
                == project_uuid,
            ]

        slots = await self._scalars(
            session,
            select(PersonaIntakeSlot)
            .where(*conditions)
            .order_by(
                PersonaIntakeSlot.slot.asc()
            ),
        )
        by_number = {
            int(slot.slot): slot
            for slot in slots
        }

        persona_ids = [
            slot.persona_id
            for slot in slots
        ]
        personas = (
            await self._scalars(
                session,
                select(Persona).where(
                    Persona.id.in_(persona_ids)
                ),
            )
            if persona_ids
            else []
        )

        for persona in personas:
            if project_uuid is None:
                if (
                    persona.project_id is not None
                    or persona.owner_user_id
                    != actor_id
                ):
                    raise MediaOperationsConflictError(
                        "persona intake scope mismatch"
                    )
            elif persona.project_id != project_uuid:
                raise MediaOperationsConflictError(
                    "persona intake scope mismatch"
                )

        summaries = await self._summary_map(
            session,
            personas,
        )

        result_slots: list[dict[str, Any]] = []
        for number in range(1, 10):
            row = by_number.get(number)
            if row is None:
                result_slots.append(
                    {
                        "slot": number,
                        "persona_id": None,
                        "persona": None,
                    }
                )
                continue
            result_slots.append(
                {
                    "slot": number,
                    "persona_id": str(
                        row.persona_id
                    ),
                    "persona": summaries.get(
                        row.persona_id
                    ),
                }
            )

        return {
            "project_id": (
                str(project_uuid)
                if project_uuid is not None
                else None
            ),
            "slots": result_slots,
        }

    async def list_personas(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
        search: Any = None,
        characters_only: bool = False,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError(
                "actor is required"
            )

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        page_limit, page_offset = _bounded_page(
            limit,
            offset,
        )
        search_value = _optional_text(search, "search", 200)

        if project_uuid is not None:
            await self._assert_access(
                session,
                actor,
                project_id=project_uuid,
                permission="read",
            )
            condition = (
                Persona.project_id
                == project_uuid
            )
        else:
            condition = (
                await self._scope_less_condition(
                    session,
                    actor,
                )
            )

        statement = select(Persona)
        if search_value is not None or characters_only:
            latest_versions = (
                select(
                    PersonaRevision.persona_id.label("persona_id"),
                    func.max(PersonaRevision.version).label("version"),
                )
                .group_by(PersonaRevision.persona_id)
                .subquery()
            )
            statement = statement.join(
                latest_versions,
                latest_versions.c.persona_id == Persona.id,
            ).join(
                PersonaRevision,
                and_(
                    PersonaRevision.persona_id == Persona.id,
                    PersonaRevision.version == latest_versions.c.version,
                ),
            )
            if characters_only:
                statement = statement.where(
                    ~select(PersonaIntakeSlot.id)
                    .where(PersonaIntakeSlot.persona_id == Persona.id)
                    .exists()
                )
            if search_value is not None:
                pattern = f"%{_escape_like(search_value.casefold())}%"
                statement = statement.where(
                    or_(
                        func.lower(PersonaRevision.display_name).like(
                            pattern,
                            escape="\\",
                        ),
                        func.lower(PersonaRevision.summary).like(
                            pattern,
                            escape="\\",
                        ),
                        func.lower(PersonaRevision.niche).like(
                            pattern,
                            escape="\\",
                        ),
                        func.lower(PersonaRevision.positioning).like(
                            pattern,
                            escape="\\",
                        ),
                    )
                )
        statement = (
            statement.where(condition)
            .order_by(
                Persona.created_at.desc(),
                Persona.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset)
        )
        personas = await self._scalars(session, statement)
        summaries = await self._summary_map(
            session,
            personas,
        )
        return [
            summaries[persona.id]
            for persona in personas
        ]

    async def get_persona(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        persona_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or persona_id is None:
            raise MediaOperationsValidationError(
                "actor and persona_id are required"
            )
        persona = await self._get_persona_row(
            session,
            persona_id,
        )
        await self._assert_entity_access(
            session,
            actor,
            persona,
            permission="read",
        )
        return await self._detail_for_row(
            session,
            persona,
        )

    async def create_persona(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        intake_slot: Any = None,
        display_name: Any,
        summary: Any = None,
        voice: Any = None,
        audience: Any = None,
        platforms: Sequence[Any] | None = None,
        content_pillars: Sequence[Any] | None = None,
        public_aliases: Sequence[Any] | None = None,
        niche: Any = None,
        positioning: Any = None,
        visual_identity: Any = None,
        creative_direction: Any = None,
        allowed_subjects: Sequence[Any] | None = None,
        prohibited_subjects: Sequence[Any] | None = None,
        adult_policy: Any = None,
        sensitive_policy: Any = None,
        ip_policy: Any = None,
        disclosure_policy: Any = None,
        monetization_policy: Any = None,
        kpi_objectives: Sequence[Any] | None = None,
        default_language: Any = None,
        locale: Any = None,
        timezone: Any = None,
        research_policy: Any = None,
        image_production_policy: Any = None,
        video_production_policy: Any = None,
        state: Any = "draft",
        parent_brand_ref: Any = None,
        project_id: UUID | str | None = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None:
            raise MediaOperationsValidationError(
                "actor is required"
            )

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        actor_id = await self._assert_create_scope(
            session,
            actor,
            project_uuid,
        )
        slot = _intake_slot(intake_slot)
        key = _idempotency_key(
            idempotency_key
        )
        revision_content = _normalize_revision_input(
            display_name=display_name,
            summary=summary,
            voice=voice,
            audience=audience,
            platforms=platforms,
            content_pillars=content_pillars,
            public_aliases=public_aliases,
            niche=niche,
            positioning=positioning,
            visual_identity=visual_identity,
            creative_direction=creative_direction,
            allowed_subjects=allowed_subjects,
            prohibited_subjects=prohibited_subjects,
            adult_policy=adult_policy,
            sensitive_policy=sensitive_policy,
            ip_policy=ip_policy,
            disclosure_policy=disclosure_policy,
            monetization_policy=monetization_policy,
            kpi_objectives=kpi_objectives,
            default_language=default_language,
            locale=locale,
            timezone=timezone,
            research_policy=research_policy,
            image_production_policy=image_production_policy,
            video_production_policy=video_production_policy,
        )
        persona_state = _normalize_persona_state(state)
        parent_brand_value = _optional_text(parent_brand_ref, "parent_brand_ref", 164)
        content_hash = sha256_json(
            revision_content
        )
        create_hash = sha256_json(
            {
                "project_id": (
                    str(project_uuid)
                    if project_uuid is not None
                    else None
                ),
                "intake_slot": slot,
                "state": persona_state,
                "parent_brand_ref": parent_brand_value,
                "revision": revision_content,
            }
        )

        existing = (
            await self._find_persona_by_idempotency(
                session,
                owner_user_id=actor_id,
                project_id=project_uuid,
                idempotency_key=key,
            )
        )
        if existing is not None:
            if existing.create_hash != create_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different Persona payload"
                )
            await self._assert_entity_access(
                session,
                actor,
                existing,
                permission="read",
            )
            return await self._detail_for_row(
                session,
                existing,
            )

        if slot is not None:
            occupied = await self._find_slot(
                session,
                owner_user_id=actor_id,
                project_id=project_uuid,
                slot=slot,
            )
            if occupied is not None:
                raise MediaOperationsConflictError(
                    "persona intake slot is already occupied"
                )

        persona = Persona(
            owner_user_id=actor_id,
            project_id=project_uuid,
            state=persona_state,
            parent_brand_ref=parent_brand_value,
            create_hash=create_hash,
            idempotency_key=key,
            created_by=actor_id,
        )

        try:
            session.add(persona)
            await self._flush_only(session)

            revision = self._build_revision(
                persona=persona,
                version=1,
                content=revision_content,
                content_hash=content_hash,
                created_by=actor_id,
                idempotency_key=None,
            )
            session.add(revision)
            if slot is not None:
                session.add(
                    PersonaIntakeSlot(
                        owner_user_id=actor_id,
                        project_id=project_uuid,
                        slot=slot,
                        persona_id=persona.id,
                        created_by=actor_id,
                    )
                )
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)

            recovered = (
                await self._find_persona_by_idempotency(
                    session,
                    owner_user_id=actor_id,
                    project_id=project_uuid,
                    idempotency_key=key,
                )
            )
            if recovered is not None:
                if (
                    recovered.create_hash
                    == create_hash
                ):
                    await self._assert_entity_access(
                        session,
                        actor,
                        recovered,
                        permission="read",
                    )
                    return await self._detail_for_row(
                        session,
                        recovered,
                    )
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different Persona payload"
                ) from exc

            if slot is not None:
                occupied = await self._find_slot(
                    session,
                    owner_user_id=actor_id,
                    project_id=project_uuid,
                    slot=slot,
                )
                if occupied is not None:
                    raise MediaOperationsConflictError(
                        "persona intake slot is already occupied"
                    ) from exc

            raise MediaOperationsConflictError(
                "persona creation conflicts with "
                "an existing record"
            ) from exc

        return await self.get_persona(
            session,
            actor,
            persona.id,
        )

    async def list_characters(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
        search: Any = None,
    ) -> list[dict[str, Any]]:
        """List slot-free Character identities through the Persona ACL."""

        return await self.list_personas(
            session,
            actor,
            project_id=project_id,
            limit=limit,
            offset=offset,
            search=search,
            characters_only=True,
        )

    async def create_character(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        display_name: Any,
        summary: Any = None,
        voice: Any = None,
        audience: Any = None,
        platforms: Sequence[Any] | None = None,
        content_pillars: Sequence[Any] | None = None,
        public_aliases: Sequence[Any] | None = None,
        niche: Any = None,
        positioning: Any = None,
        visual_identity: Any = None,
        creative_direction: Any = None,
        allowed_subjects: Sequence[Any] | None = None,
        prohibited_subjects: Sequence[Any] | None = None,
        adult_policy: Any = None,
        sensitive_policy: Any = None,
        ip_policy: Any = None,
        disclosure_policy: Any = None,
        monetization_policy: Any = None,
        kpi_objectives: Sequence[Any] | None = None,
        default_language: Any = None,
        locale: Any = None,
        timezone: Any = None,
        research_policy: Any = None,
        image_production_policy: Any = None,
        video_production_policy: Any = None,
        state: Any = "draft",
        parent_brand_ref: Any = None,
        project_id: UUID | str | None = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        """Create a Character without occupying one of the legacy slots."""

        return await self.create_persona(
            session,
            actor,
            intake_slot=None,
            display_name=display_name,
            summary=summary,
            voice=voice,
            audience=audience,
            platforms=platforms,
            content_pillars=content_pillars,
            public_aliases=public_aliases,
            niche=niche,
            positioning=positioning,
            visual_identity=visual_identity,
            creative_direction=creative_direction,
            allowed_subjects=allowed_subjects,
            prohibited_subjects=prohibited_subjects,
            adult_policy=adult_policy,
            sensitive_policy=sensitive_policy,
            ip_policy=ip_policy,
            disclosure_policy=disclosure_policy,
            monetization_policy=monetization_policy,
            kpi_objectives=kpi_objectives,
            default_language=default_language,
            locale=locale,
            timezone=timezone,
            research_policy=research_policy,
            image_production_policy=image_production_policy,
            video_production_policy=video_production_policy,
            state=state,
            parent_brand_ref=parent_brand_ref,
            project_id=project_id,
            idempotency_key=idempotency_key,
        )

    async def get_character(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        character_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Read a Character through the same Persona ACL as legacy reads."""

        return await self.get_persona(session, actor, character_id)

    async def patch_character(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        character_id: UUID | str | None = None,
        *,
        expected_revision_id: Any,
        expected_revision_version: Any,
        expected_revision_content_hash: Any,
        idempotency_key: Any,
        **changes: Any,
    ) -> dict[str, Any]:
        """Apply an optimistic, immutable PATCH to a Character.

        The expected revision triple is checked against the exact immutable
        row and the latest revision while the stable Persona row is locked.
        Only fields explicitly supplied by the caller are merged; omitted
        fields are copied from the expected revision and explicit ``None`` or
        empty collections retain their clear semantics through the normalizer.
        """

        session = self._resolve_session(session)
        if actor is None or character_id is None:
            raise MediaOperationsValidationError(
                "actor and character_id are required"
            )

        persona = await self._get_persona_row(
            session,
            character_id,
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            persona,
            permission="write",
        )

        expected_uuid = _as_uuid(
            expected_revision_id,
            "expected_revision_id",
        )
        assert expected_uuid is not None
        if isinstance(expected_revision_version, bool):
            raise MediaOperationsValidationError(
                "expected_revision_version must be an integer"
            )
        try:
            expected_version = int(expected_revision_version)
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "expected_revision_version must be an integer"
            ) from exc
        if expected_version < 1:
            raise MediaOperationsValidationError(
                "expected_revision_version must be at least 1"
            )
        expected_hash = _validated_sha256(
            expected_revision_content_hash,
            "expected_revision_content_hash",
        )
        key = _idempotency_key(idempotency_key)

        unknown_fields = set(changes).difference(
            _REVISION_CONTENT_FIELDS
        )
        if unknown_fields:
            raise MediaOperationsValidationError(
                "unsupported character revision field"
            )
        if not changes:
            raise MediaOperationsValidationError(
                "at least one character revision field is required"
            )

        expected_revision = await self._scalar(
            session,
            select(PersonaRevision)
            .where(
                PersonaRevision.persona_id == persona.id,
                PersonaRevision.id == expected_uuid,
            )
            .limit(1),
        )
        if (
            expected_revision is None
            or int(expected_revision.version or 0) != expected_version
            or str(expected_revision.content_hash or "").lower()
            != expected_hash
        ):
            raise MediaOperationsConflictError(
                "stale character revision"
            )

        source_content = _revision_content_from_safe_dict(
            expected_revision.to_safe_dict()
        )
        merged_content = dict(source_content)
        merged_content.update(changes)
        content = _normalize_revision_input(
            **merged_content,
        )
        content_hash = sha256_json(content)

        # Resolve idempotency before the latest gate.  A retry after another
        # successful request must return the original result even when the
        # caller still presents the now-stale expected revision triple.
        existing = await self._find_revision_by_idempotency(
            session,
            persona_id=persona.id,
            idempotency_key=key,
        )
        if existing is not None:
            if existing.content_hash != content_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with different revision content"
                )
            return await self._detail_for_row(session, persona)

        latest = await self._latest_revision(session, persona.id)
        if (
            latest is None
            or latest.id != expected_uuid
            or int(latest.version or 0) != expected_version
            or str(latest.content_hash or "").lower() != expected_hash
        ):
            raise MediaOperationsConflictError(
                "stale character revision"
            )

        revision = self._build_revision(
            persona=persona,
            version=expected_version + 1,
            content=content,
            content_hash=content_hash,
            created_by=actor_id,
            idempotency_key=key,
        )

        try:
            session.add(revision)
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)
            recovered_persona = await self._get_persona_row(
                session,
                persona.id,
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_persona,
                permission="write",
            )
            recovered = await self._find_revision_by_idempotency(
                session,
                persona_id=recovered_persona.id,
                idempotency_key=key,
            )
            if recovered is not None:
                if recovered.content_hash == content_hash:
                    return await self._detail_for_row(
                        session,
                        recovered_persona,
                    )
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with different revision content"
                ) from exc
            raise MediaOperationsConflictError(
                "character revision changed concurrently; "
                "retry with the same idempotency key"
            ) from exc

        return await self._detail_for_row(session, persona)

    async def update_character(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        character_id: UUID | str | None = None,
        *,
        expected_revision_id: Any,
        expected_revision_version: Any,
        expected_revision_content_hash: Any,
        idempotency_key: Any,
        **changes: Any,
    ) -> dict[str, Any]:
        """Compatibility PUT routed through the optimistic PATCH semantics."""

        return await self.patch_character(
            session,
            actor,
            character_id,
            expected_revision_id=expected_revision_id,
            expected_revision_version=expected_revision_version,
            expected_revision_content_hash=expected_revision_content_hash,
            idempotency_key=idempotency_key,
            **changes,
        )

    async def append_persona_revision(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        persona_id: UUID | str | None = None,
        *,
        display_name: Any,
        summary: Any = None,
        voice: Any = None,
        audience: Any = None,
        platforms: Sequence[Any] | None = None,
        content_pillars: Sequence[Any] | None = None,
        public_aliases: Sequence[Any] | None = None,
        niche: Any = None,
        positioning: Any = None,
        visual_identity: Any = None,
        creative_direction: Any = None,
        allowed_subjects: Sequence[Any] | None = None,
        prohibited_subjects: Sequence[Any] | None = None,
        adult_policy: Any = None,
        sensitive_policy: Any = None,
        ip_policy: Any = None,
        disclosure_policy: Any = None,
        monetization_policy: Any = None,
        kpi_objectives: Sequence[Any] | None = None,
        default_language: Any = None,
        locale: Any = None,
        timezone: Any = None,
        research_policy: Any = None,
        image_production_policy: Any = None,
        video_production_policy: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or persona_id is None:
            raise MediaOperationsValidationError(
                "actor and persona_id are required"
            )

        persona = await self._get_persona_row(
            session,
            persona_id,
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            persona,
            permission="write",
        )
        key = _idempotency_key(
            idempotency_key
        )
        content = _normalize_revision_input(
            display_name=display_name,
            summary=summary,
            voice=voice,
            audience=audience,
            platforms=platforms,
            content_pillars=content_pillars,
            public_aliases=public_aliases,
            niche=niche,
            positioning=positioning,
            visual_identity=visual_identity,
            creative_direction=creative_direction,
            allowed_subjects=allowed_subjects,
            prohibited_subjects=prohibited_subjects,
            adult_policy=adult_policy,
            sensitive_policy=sensitive_policy,
            ip_policy=ip_policy,
            disclosure_policy=disclosure_policy,
            monetization_policy=monetization_policy,
            kpi_objectives=kpi_objectives,
            default_language=default_language,
            locale=locale,
            timezone=timezone,
            research_policy=research_policy,
            image_production_policy=image_production_policy,
            video_production_policy=video_production_policy,
        )
        content_hash = sha256_json(content)

        existing = (
            await self._find_revision_by_idempotency(
                session,
                persona_id=persona.id,
                idempotency_key=key,
            )
        )
        if existing is not None:
            if existing.content_hash != content_hash:
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with different revision content"
                )
            return existing.to_safe_dict()

        current_version = await self._scalar(
            session,
            select(
                func.max(
                    PersonaRevision.version
                )
            ).where(
                PersonaRevision.persona_id
                == persona.id
            ),
        )
        next_version = int(
            current_version or 0
        ) + 1
        revision = self._build_revision(
            persona=persona,
            version=next_version,
            content=content,
            content_hash=content_hash,
            created_by=actor_id,
            idempotency_key=key,
        )

        try:
            session.add(revision)
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)

            recovered_persona = (
                await self._get_persona_row(
                    session,
                    persona_id,
                )
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_persona,
                permission="write",
            )
            recovered = (
                await self._find_revision_by_idempotency(
                    session,
                    persona_id=recovered_persona.id,
                    idempotency_key=key,
                )
            )
            if recovered is not None:
                if recovered.content_hash == content_hash:
                    return recovered.to_safe_dict()
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with different revision content"
                ) from exc
            raise MediaOperationsConflictError(
                "persona revision changed concurrently; "
                "retry with the same idempotency key"
            ) from exc

        return revision.to_safe_dict()

    async def attach_persona_resource(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        persona_id: UUID | str | None = None,
        *,
        resource_kind: Any,
        platform: Any = None,
        label: Any = None,
        provenance: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(session)
        if actor is None or persona_id is None:
            raise MediaOperationsValidationError(
                "actor and persona_id are required"
            )

        persona = await self._get_persona_row(
            session,
            persona_id,
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            persona,
            permission="write",
        )
        key = _idempotency_key(
            idempotency_key
        )
        normalized = _normalize_resource_input(
            resource_kind=resource_kind,
            platform=platform,
            label=label,
            provenance=provenance,
        )

        if (
            normalized["resource_kind"] == "reference_image"
            and normalized["provenance_type"] == "artifact"
        ):
            raise MediaOperationsValidationError(
                "reference_image requires stored_artifact provenance"
            )

        if normalized["provenance_type"] == "stored_artifact":
            if normalized["resource_kind"] != "reference_image":
                raise MediaOperationsValidationError(
                    "stored_artifact provenance is only supported "
                    "for reference_image"
                )
            artifact = await self._scalar(
                session,
                select(ArtifactVersion)
                .where(
                    ArtifactVersion.id == normalized["artifact_id"]
                )
                .limit(1),
            )
            if artifact is None:
                # Keep artifact identifiers and storage details out of all
                # error text; callers learn only that the reference is absent.
                raise MediaOperationsNotFoundError(
                    "artifact version not found"
                )
            # A stored artifact may be referenced only inside its own scope.
            # Project members can share project artifacts via the normal ACL,
            # while personal artifacts remain owner-bound.
            if artifact.project_id != persona.project_id:
                raise MediaOperationsAuthorizationError(
                    "artifact access denied"
                )
            try:
                await self._assert_entity_access(
                    session,
                    actor,
                    artifact,
                    permission="read",
                )
            except MediaOperationsError:
                raise MediaOperationsAuthorizationError(
                    "artifact access denied"
                ) from None
            mime_type = str(
                getattr(artifact, "mime_type", "") or ""
            ).split(";", 1)[0].strip().lower()
            if not mime_type.startswith("image/"):
                raise MediaOperationsValidationError(
                    "reference_image artifact must be an image"
                )
            normalized["provenance_type"] = "artifact"
            normalized["artifact_sha256"] = _validated_sha256(
                getattr(artifact, "sha256", None),
                "artifact.sha256",
            )
            normalized["artifact_mime_type"] = _required_text(
                mime_type,
                "artifact.mime_type",
                255,
            )

        existing = (
            await self._find_resource_by_idempotency(
                session,
                persona_id=persona.id,
                idempotency_key=key,
            )
        )
        if existing is not None:
            if (
                existing.resource_hash
                != normalized["resource_hash"]
            ):
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different resource"
                )
            return existing.to_safe_dict()

        resource = PersonaResource(
            persona_id=persona.id,
            owner_user_id=persona.owner_user_id,
            project_id=persona.project_id,
            resource_kind=normalized[
                "resource_kind"
            ],
            platform=normalized["platform"],
            label=normalized["label"],
            provenance_type=normalized[
                "provenance_type"
            ],
            source_url=normalized["source_url"],
            artifact_sha256=normalized[
                "artifact_sha256"
            ],
            artifact_mime_type=normalized[
                "artifact_mime_type"
            ],
            resource_hash=normalized[
                "resource_hash"
            ],
            idempotency_key=key,
            created_by=actor_id,
        )

        try:
            session.add(resource)
            await self._flush_commit(session)
        except IntegrityError as exc:
            await self._rollback(session)

            recovered_persona = (
                await self._get_persona_row(
                    session,
                    persona_id,
                )
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_persona,
                permission="write",
            )
            recovered = (
                await self._find_resource_by_idempotency(
                    session,
                    persona_id=recovered_persona.id,
                    idempotency_key=key,
                )
            )
            if recovered is not None:
                if (
                    recovered.resource_hash
                    == normalized["resource_hash"]
                ):
                    return recovered.to_safe_dict()
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different resource"
                ) from exc
            raise MediaOperationsConflictError(
                "resource attachment conflicts "
                "with an existing record"
            ) from exc

        return resource.to_safe_dict()

    async def list_persona_resources(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        persona_id: UUID | str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(session)
        if actor is None or persona_id is None:
            raise MediaOperationsValidationError(
                "actor and persona_id are required"
            )

        persona = await self._get_persona_row(
            session,
            persona_id,
        )
        await self._assert_entity_access(
            session,
            actor,
            persona,
            permission="read",
        )
        page_limit, page_offset = _bounded_page(
            limit,
            offset,
        )
        resources = await self._scalars(
            session,
            select(PersonaResource)
            .where(
                PersonaResource.persona_id
                == persona.id
            )
            .order_by(
                PersonaResource.created_at.desc(),
                PersonaResource.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset),
        )
        return [
            resource.to_safe_dict()
            for resource in resources
        ]
