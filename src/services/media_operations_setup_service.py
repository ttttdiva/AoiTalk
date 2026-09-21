"""MediaOps WS2 service.

Adds:
- fixed nine-slot Persona draft import/review/correction/atomic apply
- PlatformAccount stable identity + immutable state revisions

No provider calls, credentials, secret references, or filesystem paths are
accepted by this service.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    ExternalConnection,
    PLATFORM_CAPABILITY_STATUS_VALUES,
    PLATFORM_CREDENTIAL_STATUS_VALUES,
    Persona,
    PersonaBulkDraft,
    PersonaBulkDraftSlot,
    PersonaIntakeSlot,
    PlatformAccount,
    PlatformAccountRevision,
)
from .media_operations_service import (
    MediaOperationsAuthorizationError,
    MediaOperationsConflictError,
    MediaOperationsService,
    MediaOperationsValidationError,
    _actor_id,
    _actor_field,
    _as_uuid,
    _bounded_page,
    _idempotency_key,
    _intake_slot,
    _normalize_content_pillars,
    _normalize_platforms,
    _normalize_revision_input,
    _optional_text,
    _required_text,
    _validated_resource_url,
    sha256_json,
)


_FACT_FIELDS = (
    "display_name",
    "summary",
    "voice",
    "audience",
    "platforms",
    "content_pillars",
)

_PROTECTED_ACCOUNT_METADATA_TOKENS = frozenset(
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

_PLATFORM_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("x", "platform_x"),
    ("pixiv", "platform_pixiv"),
    ("dlsite", "platform_dlsite"),
    ("patreon", "platform_patreon"),
    ("youtube", "platform_youtube"),
    ("instagram", "platform_instagram"),
)


def _fact_state(value: Any) -> str:
    rendered = str(
        getattr(value, "value", value)
    ).strip().lower()
    if rendered not in {
        "explicit",
        "inferred",
        "unknown",
    }:
        raise MediaOperationsValidationError(
            "fact state must be explicit, inferred, or unknown"
        )
    return rendered


def _normalize_fact(
    raw: Any,
    *,
    label: str,
    normalize_value,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise MediaOperationsValidationError(
            f"{label} must be a typed fact object"
        )

    keys = {
        str(key)
        for key in raw.keys()
    }
    if keys != {
        "state",
        "value",
        "evidence",
    }:
        raise MediaOperationsValidationError(
            f"{label} accepts only state, value, and evidence"
        )

    state = _fact_state(
        raw.get("state")
    )
    evidence = _optional_text(
        raw.get("evidence"),
        f"{label}.evidence",
        1000,
    )

    if state == "unknown":
        if raw.get("value") is not None:
            raise MediaOperationsValidationError(
                f"{label}.value must be null when state is unknown"
            )
        if evidence is not None:
            raise MediaOperationsValidationError(
                f"{label}.evidence must be null when state is unknown"
            )
        return {
            "state": "unknown",
            "value": None,
            "evidence": None,
        }

    value = normalize_value(
        raw.get("value")
    )

    if (
        state == "inferred"
        and evidence is None
    ):
        raise MediaOperationsValidationError(
            f"{label}.evidence is required when state is inferred"
        )

    return {
        "state": state,
        "value": value,
        "evidence": evidence,
    }


def _normalize_text_fact(
    raw: Any,
    *,
    label: str,
    max_length: int,
) -> dict[str, Any]:
    return _normalize_fact(
        raw,
        label=label,
        normalize_value=lambda value: _required_text(
            value,
            f"{label}.value",
            max_length,
        ),
    )


def _normalize_platform_fact(
    raw: Any,
) -> dict[str, Any]:
    return _normalize_fact(
        raw,
        label="platforms",
        normalize_value=_normalize_platforms,
    )


def _normalize_pillar_fact(
    raw: Any,
) -> dict[str, Any]:
    return _normalize_fact(
        raw,
        label="content_pillars",
        normalize_value=_normalize_content_pillars,
    )


def _normalize_bulk_slot(
    raw: Any,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise MediaOperationsValidationError(
            "each Persona draft slot must be an object"
        )

    expected = {
        "slot",
        "display_name",
        "summary",
        "voice",
        "audience",
        "platforms",
        "content_pillars",
    }
    if {
        str(key)
        for key in raw.keys()
    } != expected:
        raise MediaOperationsValidationError(
            "Persona draft slot has an invalid field set"
        )

    slot = _intake_slot(
        raw.get("slot")
    )

    normalized = {
        "slot": slot,
        "display_name": _normalize_text_fact(
            raw.get("display_name"),
            label="display_name",
            max_length=120,
        ),
        "summary": _normalize_text_fact(
            raw.get("summary"),
            label="summary",
            max_length=4000,
        ),
        "voice": _normalize_text_fact(
            raw.get("voice"),
            label="voice",
            max_length=4000,
        ),
        "audience": _normalize_text_fact(
            raw.get("audience"),
            label="audience",
            max_length=4000,
        ),
        "platforms": _normalize_platform_fact(
            raw.get("platforms")
        ),
        "content_pillars": _normalize_pillar_fact(
            raw.get("content_pillars")
        ),
    }

    normalized["slot_hash"] = sha256_json(
        {
            key: value
            for key, value in normalized.items()
            if key != "slot_hash"
        }
    )
    return normalized


def _normalize_bulk_slots(
    values: Any,
) -> list[dict[str, Any]]:
    if (
        isinstance(values, (str, bytes))
        or not isinstance(values, Sequence)
    ):
        raise MediaOperationsValidationError(
            "slots must be a list"
        )

    if len(values) != 9:
        raise MediaOperationsValidationError(
            "bulk Persona draft must contain exactly nine slots"
        )

    normalized = [
        _normalize_bulk_slot(value)
        for value in values
    ]

    slot_numbers = [
        int(value["slot"])
        for value in normalized
    ]
    if len(set(slot_numbers)) != 9:
        raise MediaOperationsValidationError(
            "bulk Persona draft contains duplicate slots"
        )
    if set(slot_numbers) != set(
        range(1, 10)
    ):
        raise MediaOperationsValidationError(
            "bulk Persona draft must contain slots 1 through 9 exactly once"
        )

    return sorted(
        normalized,
        key=lambda item: int(
            item["slot"]
        ),
    )


_BULK_SOURCE_MAX_BYTES = 256_000
_BULK_SOURCE_FORMATS = frozenset({"auto", "json", "yaml", "markdown", "text"})
_BULK_FIELD_ALIASES = {
    "name": "display_name",
    "display": "display_name",
    "display_name": "display_name",
    "persona": "display_name",
    "summary": "summary",
    "description": "summary",
    "voice": "voice",
    "tone": "voice",
    "audience": "audience",
    "target_audience": "audience",
    "platform": "platforms",
    "platforms": "platforms",
    "content_pillar": "content_pillars",
    "content_pillars": "content_pillars",
    "pillars": "content_pillars",
}


def _source_fact(value: Any, field: str) -> dict[str, Any]:
    """Convert a source value into the explicit/inferred/unknown fact shape.

    Structured imports may already carry a typed fact.  Shorthand JSON/YAML
    and Markdown values are source facts (never model-inferred facts); missing
    values remain unknown so the apply gate can request human correction.
    """

    if isinstance(value, Mapping) and (
        "state" in value or "value" in value or "evidence" in value
    ):
        return {
            "state": value.get("state", "unknown"),
            "value": value.get("value"),
            "evidence": value.get("evidence"),
        }
    if value in (None, ""):
        return {"state": "unknown", "value": None, "evidence": None}
    if field in {"platforms", "content_pillars"} and isinstance(value, str):
        value = [part.strip() for part in re.split(r"[,、\n]", value) if part.strip()]
    return {"state": "explicit", "value": value, "evidence": None}


def _source_slot(raw: Any, number: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        # A scalar source entry is still useful as an explicit display name;
        # all other facts stay unknown rather than being invented.
        raw = {"display_name": raw}
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        normalized = _BULK_FIELD_ALIASES.get(str(key).strip().casefold().replace("-", "_"))
        if normalized is not None:
            fields[normalized] = value
    raw_slot = raw.get("slot", raw.get("index", raw.get("position", number)))
    try:
        slot = int(raw_slot)
    except (TypeError, ValueError):
        slot = number
    return {
        "slot": slot,
        "display_name": _source_fact(fields.get("display_name"), "display_name"),
        "summary": _source_fact(fields.get("summary"), "summary"),
        "voice": _source_fact(fields.get("voice"), "voice"),
        "audience": _source_fact(fields.get("audience"), "audience"),
        "platforms": _source_fact(fields.get("platforms"), "platforms"),
        "content_pillars": _source_fact(fields.get("content_pillars"), "content_pillars"),
    }


def _structured_bulk_source(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, Mapping):
        entries: Any = None
        for key in ("slots", "personas", "items", "drafts"):
            if key in value:
                entries = value[key]
                break
        if entries is None:
            # Numeric keys and a single persona object are both accepted.  A
            # source field that is not recognized is ignored deliberately.
            numeric = [item for key, item in value.items() if str(key).strip().isdigit()]
            if numeric:
                entries = numeric
            elif any(
                _BULK_FIELD_ALIASES.get(
                    str(key).strip().casefold().replace("-", "_")
                )
                for key in value
            ):
                entries = [value]
            else:
                # YAML can parse a Markdown outline as an incidental mapping
                # (for example, a labelled line with a colon).  Let the
                # conservative Markdown parser handle that text instead of
                # silently producing an all-unknown Persona.
                return None
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        entries = value
    else:
        return None
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
        raise MediaOperationsValidationError("Persona source slots/personas must be a list")
    if len(entries) > 9:
        raise MediaOperationsValidationError("Persona source contains more than nine slots")
    result = [_source_slot(item, index) for index, item in enumerate(entries, 1)]
    present = {int(item["slot"]) for item in result if isinstance(item.get("slot"), int)}
    # Missing positions are structural unknown drafts, not inferred content.
    for index in range(1, 10):
        if index not in present:
            result.append(_source_slot({}, index))
    return result


def _markdown_bulk_source(source: str) -> list[dict[str, Any]]:
    """Parse a conservative Markdown/free-form Persona outline.

    Only labelled fields are extracted.  Arbitrary prose is never evaluated
    as instructions and unknown fields are intentionally discarded.
    """

    header = re.compile(
        r"^\s{0,3}(?:#{1,6}\s*)?(?:persona|ペルソナ)\s*(?:#|№|no\.?|number)?\s*([0-9]{1,2}|[A-Za-z])?\s*(?:[-:：|]\s*(.*))?$",
        re.IGNORECASE,
    )
    field_re = re.compile(r"^\s*(?:[-*+]\s*)?([A-Za-z_][A-Za-z0-9 _-]{0,40}|名前|表示名|要約|概要|声|口調|対象読者|読者|媒体|プラットフォーム|柱|コンテンツ柱)\s*[:：]\s*(.*?)\s*$", re.IGNORECASE)
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            blocks.append(current)
        current = None

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = header.match(line)
        if match:
            flush()
            token = (match.group(1) or "").strip()
            number = int(token) if token.isdigit() else len(blocks) + 1
            title = (match.group(2) or "").strip() or None
            current = {"slot": number}
            if title:
                current["display_name"] = title
            elif token and not token.isdigit():
                current["display_name"] = f"Persona {token}"
            elif not token:
                current["display_name"] = line.lstrip("# ").strip()
            continue
        if line.startswith("---") or line.startswith("***"):
            flush()
            continue
        if current is None:
            # A free-form first line can establish one source-backed draft;
            # later labelled lines are still parsed into the same block.
            current = {"slot": len(blocks) + 1}
        match = field_re.match(line)
        if not match:
            continue
        raw_field = match.group(1).strip().casefold().replace("-", "_").replace(" ", "_")
        field = {
            "名前": "display_name",
            "表示名": "display_name",
            "要約": "summary",
            "概要": "summary",
            "声": "voice",
            "口調": "voice",
            "対象読者": "audience",
            "読者": "audience",
            "媒体": "platforms",
            "プラットフォーム": "platforms",
            "柱": "content_pillars",
            "コンテンツ柱": "content_pillars",
        }.get(raw_field, _BULK_FIELD_ALIASES.get(raw_field))
        if field:
            value = match.group(2).strip()
            current[field] = value
    flush()
    if len(blocks) > 9:
        raise MediaOperationsValidationError("Persona source contains more than nine slots")
    result = [_source_slot(item, index) for index, item in enumerate(blocks, 1)]
    present = {int(item["slot"]) for item in result if isinstance(item.get("slot"), int)}
    for index in range(1, 10):
        if index not in present:
            result.append(_source_slot({}, index))
    return result


def _parse_bulk_source(source: Any, source_format: Any = "auto") -> list[dict[str, Any]]:
    text = _required_text(source, "source", _BULK_SOURCE_MAX_BYTES)
    if len(text.encode("utf-8")) > _BULK_SOURCE_MAX_BYTES:
        raise MediaOperationsValidationError("source exceeds maximum size")
    format_value = str(source_format or "auto").strip().lower()
    if format_value not in _BULK_SOURCE_FORMATS:
        raise MediaOperationsValidationError("source_format is invalid")
    parsed: Any = None
    if format_value in {"auto", "json"}:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            if format_value == "json":
                raise MediaOperationsValidationError("source is not valid JSON") from exc
    if parsed is None and format_value in {"auto", "yaml"}:
        try:
            import yaml  # type: ignore[import-not-found]

            parsed = yaml.safe_load(text)
        except ImportError as exc:
            raise MediaOperationsValidationError("YAML import support is unavailable") from exc
        except Exception as exc:
            raise MediaOperationsValidationError("source is not valid YAML") from exc
    structured = _structured_bulk_source(parsed) if parsed is not None else None
    if structured is not None:
        return _normalize_bulk_slots(structured)
    if format_value in {"json", "yaml"}:
        raise MediaOperationsValidationError("source must contain a slots/personas list")
    return _normalize_bulk_slots(_markdown_bulk_source(text))


def _draft_hash(
    slots: Sequence[Mapping[str, Any]],
) -> str:
    return sha256_json(
        [
            {
                key: value
                for key, value in slot.items()
                if key != "slot_hash"
            }
            for slot in slots
        ]
    )


def _semantic_issues(
    slots: Sequence[Mapping[str, Any]],
    *,
    occupied_slots: set[int],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    for slot in slots:
        slot_number = int(
            slot["slot"]
        )
        display_name = slot[
            "display_name"
        ]

        for field in _FACT_FIELDS:
            fact = slot[field]
            if fact["state"] == "inferred":
                issues.append(
                    {
                        "code": (
                            "inferred_fact_requires_correction"
                        ),
                        "slot": slot_number,
                        "field": field,
                        "message": (
                            "inferred facts cannot be applied; "
                            "correct this fact to explicit or unknown"
                        ),
                    }
                )

        if (
            display_name["state"]
            == "unknown"
        ):
            non_unknown = [
                field
                for field in _FACT_FIELDS
                if (
                    field != "display_name"
                    and slot[field]["state"]
                    != "unknown"
                )
            ]
            if non_unknown:
                issues.append(
                    {
                        "code": (
                            "slot_identity_unknown"
                        ),
                        "slot": slot_number,
                        "field": "display_name",
                        "message": (
                            "a slot without an explicit Persona identity "
                            "cannot contain other facts"
                        ),
                    }
                )
            continue

        if slot_number in occupied_slots:
            issues.append(
                {
                    "code": (
                        "slot_already_occupied"
                    ),
                    "slot": slot_number,
                    "field": "slot",
                    "message": (
                        "target Persona intake slot is already occupied"
                    ),
                }
            )

    return issues


def _capability_status(
    value: Any,
    label: str,
) -> str:
    rendered = str(
        getattr(value, "value", value)
    ).strip().lower()
    if rendered not in (
        PLATFORM_CAPABILITY_STATUS_VALUES
    ):
        raise MediaOperationsValidationError(
            f"{label} must be unknown, available, or unsupported"
        )
    return rendered


def _credential_status(
    value: Any,
) -> str:
    rendered = str(
        getattr(value, "value", value)
    ).strip().lower()
    if rendered not in (
        PLATFORM_CREDENTIAL_STATUS_VALUES
    ):
        raise MediaOperationsValidationError(
            "credential_status must be unknown, "
            "not_configured, configured, or invalid"
        )
    return rendered


def _normalize_account_revision(
    *,
    display_name: Any,
    publish_capability: Any,
    media_capability: Any,
    analytics_capability: Any,
    credential_status: Any,
    remote_url: Any = None,
    locale: Any = None,
    timezone: Any = None,
    supported_content_modes: Any = None,
    disclosure_defaults: Any = None,
    rating_defaults: Any = None,
    adapter_ref: Any = None,
) -> dict[str, Any]:
    if supported_content_modes is None:
        supported_modes: list[str] = []
    elif isinstance(supported_content_modes, Sequence) and not isinstance(supported_content_modes, (str, bytes)):
        if len(supported_content_modes) > 20:
            raise MediaOperationsValidationError("supported_content_modes exceeds 20 items")
        supported_modes = []
        for raw in supported_content_modes:
            value = _required_text(raw, "supported_content_mode", 64)
            if value not in supported_modes:
                supported_modes.append(value)
    else:
        raise MediaOperationsValidationError("supported_content_modes must be a list")

    def bounded_object(value: Any, label: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise MediaOperationsValidationError(f"{label} must be an object")
        if len(value) > 20:
            raise MediaOperationsValidationError(f"{label} contains too many fields")
        result: dict[str, Any] = {}
        for key, raw in value.items():
            rendered_key = _required_text(key, f"{label} key", 64)
            normalized_key = re.sub(
                r"[^a-z0-9]+",
                "_",
                rendered_key.casefold(),
            ).strip("_")
            if (
                not normalized_key
                or normalized_key in _PROTECTED_ACCOUNT_METADATA_TOKENS
                or any(
                    token in normalized_key.split("_")
                    for token in _PROTECTED_ACCOUNT_METADATA_TOKENS
                )
            ):
                raise MediaOperationsValidationError(
                    f"{label} contains a protected field"
                )
            if isinstance(raw, (str, int, float, bool)) or raw is None:
                if isinstance(raw, str) and len(raw) > 500:
                    raise MediaOperationsValidationError(f"{label} value exceeds maximum length")
                if isinstance(raw, str) and any(
                    ord(character) < 0x20 and character not in "\t\n"
                    for character in raw
                ):
                    raise MediaOperationsValidationError(
                        f"{label} value contains control characters"
                    )
                if isinstance(raw, float) and not math.isfinite(raw):
                    raise MediaOperationsValidationError(
                        f"{label} values must be finite"
                    )
                result[rendered_key] = raw
            else:
                raise MediaOperationsValidationError(f"{label} values must be scalar")
        return result

    remote_url_value = None
    if remote_url not in (None, ""):
        remote_url_value = _validated_resource_url(remote_url)
    return {
        "display_name": _required_text(
            display_name,
            "display_name",
            255,
        ),
        "publish_capability": (
            _capability_status(
                publish_capability,
                "publish_capability",
            )
        ),
        "media_capability": (
            _capability_status(
                media_capability,
                "media_capability",
            )
        ),
        "analytics_capability": (
            _capability_status(
                analytics_capability,
                "analytics_capability",
            )
        ),
        "credential_status": (
            _credential_status(
                credential_status
            )
        ),
        "remote_url": remote_url_value,
        "locale": _optional_text(locale, "locale", 64),
        "timezone": _optional_text(timezone, "timezone", 64),
        "supported_content_modes": supported_modes,
        "disclosure_defaults": bounded_object(disclosure_defaults, "disclosure_defaults"),
        "rating_defaults": bounded_object(rating_defaults, "rating_defaults"),
        "adapter_ref": _optional_text(adapter_ref, "adapter_ref", 164),
    }


class MediaOperationsSetupService(
    MediaOperationsService
):
    """WS2 setup service built on WS1 ACL/session primitives."""

    async def _scope_condition(
        self,
        session: Any,
        actor: Any,
        model: Any,
        *,
        project_id: UUID | None,
    ) -> Any:
        if project_id is not None:
            await self._assert_access(
                session,
                actor,
                project_id=project_id,
                permission="read",
            )
            return (
                model.project_id
                == project_id
            )

        actor_id = _actor_id(actor)
        project_ids = (
            await self._authorized_project_ids(
                session,
                actor,
            )
        )
        role = str(_actor_field(actor, "role", "") or "").strip().lower()

        autonomous_discovery = bool(
            _actor_field(actor, "_autonomous_discovery", False)
        ) and role == "admin"
        personal = (
            model.project_id.is_(None)
            if autonomous_discovery
            else and_(
                model.project_id.is_(None),
                model.owner_user_id == actor_id,
            )
        )
        if not project_ids:
            return personal

        return or_(
            personal,
            model.project_id.in_(
                project_ids
            ),
        )

    async def _get_bulk_draft_row(
        self,
        session: Any,
        draft_id: UUID | str,
        *,
        for_update: bool = False,
    ) -> PersonaBulkDraft:
        parsed = _as_uuid(
            draft_id,
            "draft_id",
        )
        assert parsed is not None

        statement = (
            select(PersonaBulkDraft)
            .where(
                PersonaBulkDraft.id
                == parsed
            )
            .limit(1)
        )
        if for_update:
            statement = (
                statement.with_for_update()
            )

        draft = await self._scalar(
            session,
            statement,
        )
        if draft is None:
            from .media_operations_service import (
                MediaOperationsNotFoundError,
            )

            raise MediaOperationsNotFoundError(
                "Persona bulk draft not found"
            )
        return draft

    async def _bulk_draft_rows(
        self,
        session: Any,
        draft_id: UUID,
        *,
        for_update: bool = False,
    ) -> list[PersonaBulkDraftSlot]:
        statement = (
            select(PersonaBulkDraftSlot)
            .where(
                PersonaBulkDraftSlot.draft_id
                == draft_id
            )
            .order_by(
                PersonaBulkDraftSlot.slot.asc()
            )
        )
        if for_update:
            statement = (
                statement.with_for_update()
            )

        rows = await self._scalars(
            session,
            statement,
        )
        if len(rows) != 9:
            raise MediaOperationsConflictError(
                "Persona bulk draft does not contain exactly nine durable slots"
            )
        return rows

    async def _occupied_slots(
        self,
        session: Any,
        *,
        actor: Any,
        project_id: UUID | None,
    ) -> set[int]:
        actor_id = _actor_id(actor)

        if project_id is None:
            conditions = [
                PersonaIntakeSlot.project_id.is_(
                    None
                ),
                PersonaIntakeSlot.owner_user_id
                == actor_id,
            ]
        else:
            conditions = [
                PersonaIntakeSlot.project_id
                == project_id,
            ]

        return {
            int(value)
            for value in await self._scalars(
                session,
                select(
                    PersonaIntakeSlot.slot
                ).where(
                    *conditions
                ),
            )
        }

    def _write_draft_slot(
        self,
        row: PersonaBulkDraftSlot,
        normalized: Mapping[str, Any],
    ) -> None:
        row.slot = int(
            normalized["slot"]
        )

        for field in (
            "display_name",
            "summary",
            "voice",
            "audience",
        ):
            fact = normalized[field]
            setattr(
                row,
                f"{field}_state",
                fact["state"],
            )
            setattr(
                row,
                f"{field}_value",
                fact["value"],
            )
            setattr(
                row,
                f"{field}_evidence",
                fact["evidence"],
            )

        platform_fact = normalized[
            "platforms"
        ]
        row.platforms_state = (
            platform_fact["state"]
        )
        row.platforms_evidence = (
            platform_fact["evidence"]
        )

        if (
            platform_fact["state"]
            == "unknown"
        ):
            for _, attribute in (
                _PLATFORM_ATTRIBUTES
            ):
                setattr(
                    row,
                    attribute,
                    None,
                )
        else:
            selected = set(
                platform_fact["value"]
            )
            for platform, attribute in (
                _PLATFORM_ATTRIBUTES
            ):
                setattr(
                    row,
                    attribute,
                    platform in selected,
                )

        pillar_fact = normalized[
            "content_pillars"
        ]
        row.content_pillars_state = (
            pillar_fact["state"]
        )
        row.content_pillars_evidence = (
            pillar_fact["evidence"]
        )
        row.content_pillars_json = (
            []
            if pillar_fact["state"]
            == "unknown"
            else list(
                pillar_fact["value"]
            )
        )

        row.slot_hash = str(
            normalized["slot_hash"]
        )

    def _new_draft_slot(
        self,
        *,
        draft_id: UUID,
        normalized: Mapping[str, Any],
    ) -> PersonaBulkDraftSlot:
        row = PersonaBulkDraftSlot(
            id=uuid4(),
            draft_id=draft_id,
            slot=int(
                normalized["slot"]
            ),
            display_name_state="unknown",
            summary_state="unknown",
            voice_state="unknown",
            audience_state="unknown",
            platforms_state="unknown",
            content_pillars_state="unknown",
            content_pillars_json=[],
            slot_hash=str(
                normalized["slot_hash"]
            ),
        )
        self._write_draft_slot(
            row,
            normalized,
        )
        return row

    async def _find_bulk_draft_by_idempotency(
        self,
        session: Any,
        *,
        owner_user_id: UUID,
        project_id: UUID | None,
        idempotency_key: str,
    ) -> PersonaBulkDraft | None:
        conditions = [
            PersonaBulkDraft.idempotency_key
            == idempotency_key,
        ]

        if project_id is None:
            conditions.extend(
                [
                    PersonaBulkDraft.project_id.is_(
                        None
                    ),
                    PersonaBulkDraft.owner_user_id
                    == owner_user_id,
                ]
            )
        else:
            conditions.append(
                PersonaBulkDraft.project_id
                == project_id
            )

        return await self._scalar(
            session,
            select(PersonaBulkDraft)
            .where(*conditions)
            .limit(1),
        )

    async def _bulk_preview(
        self,
        session: Any,
        actor: Any,
        draft: PersonaBulkDraft,
    ) -> dict[str, Any]:
        await self._assert_entity_access(
            session,
            actor,
            draft,
            permission="read",
        )
        rows = await self._bulk_draft_rows(
            session,
            draft.id,
        )
        slots = [
            row.to_safe_dict()
            for row in rows
        ]
        occupied = await self._occupied_slots(
            session,
            actor=actor,
            project_id=draft.project_id,
        )
        issues = _semantic_issues(
            slots,
            occupied_slots=occupied,
        )

        return {
            **draft.to_safe_dict(),
            "applyable": (
                draft.status == "draft"
                and not issues
            ),
            "issues": issues,
            "slots": slots,
        }

    async def import_persona_bulk_draft(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        slots: Any = None,
        source: Any = None,
        source_format: Any = "auto",
        project_id: UUID | str | None = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(
            session
        )
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
        key = _idempotency_key(
            idempotency_key
        )
        if source not in (None, ""):
            if slots not in (None, ""):
                raise MediaOperationsValidationError(
                    "provide either slots or source, not both"
                )
            normalized = _parse_bulk_source(source, source_format)
        else:
            normalized = _normalize_bulk_slots(slots)
        source_hash = _draft_hash(
            normalized
        )

        existing = (
            await self._find_bulk_draft_by_idempotency(
                session,
                owner_user_id=actor_id,
                project_id=project_uuid,
                idempotency_key=key,
            )
        )
        if existing is not None:
            if (
                existing.source_hash
                != source_hash
            ):
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different Persona bulk draft"
                )
            return await self._bulk_preview(
                session,
                actor,
                existing,
            )

        draft = PersonaBulkDraft(
            id=uuid4(),
            owner_user_id=actor_id,
            project_id=project_uuid,
            source_hash=source_hash,
            draft_hash=source_hash,
            version=1,
            status="draft",
            idempotency_key=key,
            created_by=actor_id,
        )

        session.add(draft)
        for slot in normalized:
            session.add(
                self._new_draft_slot(
                    draft_id=draft.id,
                    normalized=slot,
                )
            )

        try:
            await self._flush_commit(
                session
            )
        except IntegrityError as exc:
            await self._rollback(
                session
            )
            recovered = (
                await self._find_bulk_draft_by_idempotency(
                    session,
                    owner_user_id=actor_id,
                    project_id=project_uuid,
                    idempotency_key=key,
                )
            )
            if (
                recovered is not None
                and recovered.source_hash
                == source_hash
            ):
                return await self._bulk_preview(
                    session,
                    actor,
                    recovered,
                )
            raise MediaOperationsConflictError(
                "Persona bulk draft conflicts "
                "with an existing record"
            ) from exc

        return await self._bulk_preview(
            session,
            actor,
            draft,
        )

    async def get_persona_bulk_draft(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        draft_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(
            session
        )
        if (
            actor is None
            or draft_id is None
        ):
            raise MediaOperationsValidationError(
                "actor and draft_id are required"
            )

        draft = await self._get_bulk_draft_row(
            session,
            draft_id,
        )
        return await self._bulk_preview(
            session,
            actor,
            draft,
        )

    async def correct_persona_bulk_draft(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        draft_id: UUID | str | None = None,
        *,
        expected_version: Any,
        slots: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(
            session
        )
        if (
            actor is None
            or draft_id is None
        ):
            raise MediaOperationsValidationError(
                "actor and draft_id are required"
            )

        draft = await self._get_bulk_draft_row(
            session,
            draft_id,
            for_update=True,
        )
        await self._assert_entity_access(
            session,
            actor,
            draft,
            permission="write",
        )

        if draft.status != "draft":
            raise MediaOperationsConflictError(
                "applied Persona bulk draft cannot be corrected"
            )

        try:
            expected = int(
                expected_version
            )
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "expected_version must be an integer"
            ) from exc

        if expected != int(
            draft.version
        ):
            raise MediaOperationsConflictError(
                "stale Persona bulk draft version"
            )

        normalized = _normalize_bulk_slots(
            slots
        )
        next_hash = _draft_hash(
            normalized
        )

        if next_hash == draft.draft_hash:
            return await self._bulk_preview(
                session,
                actor,
                draft,
            )

        rows = await self._bulk_draft_rows(
            session,
            draft.id,
            for_update=True,
        )
        rows_by_slot = {
            int(row.slot): row
            for row in rows
        }

        for value in normalized:
            self._write_draft_slot(
                rows_by_slot[
                    int(value["slot"])
                ],
                value,
            )

        draft.draft_hash = next_hash
        draft.version = (
            int(draft.version)
            + 1
        )
        draft.updated_at = (
            datetime.utcnow()
        )

        await self._flush_commit(
            session
        )

        return await self._bulk_preview(
            session,
            actor,
            draft,
        )

    async def _bulk_apply_result(
        self,
        session: Any,
        actor: Any,
        draft: PersonaBulkDraft,
    ) -> dict[str, Any]:
        rows = await self._bulk_draft_rows(
            session,
            draft.id,
        )
        candidate_slots = {
            int(row.slot)
            for row in rows
            if (
                row.display_name_state
                != "unknown"
            )
        }

        intake = await self.get_persona_intake(
            session,
            actor,
            project_id=(
                str(draft.project_id)
                if draft.project_id
                is not None
                else None
            ),
        )

        created_ids = [
            str(slot["persona_id"])
            for slot in intake["slots"]
            if (
                int(slot["slot"])
                in candidate_slots
                and slot["persona_id"]
                is not None
            )
        ]

        return {
            "draft_id": str(draft.id),
            "draft_hash": draft.draft_hash,
            "applied_at": (
                draft.applied_at.isoformat()
                if draft.applied_at
                is not None
                else None
            ),
            "created_persona_ids": (
                created_ids
            ),
            "intake": intake,
        }

    async def apply_persona_bulk_draft(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        draft_id: UUID | str | None = None,
        *,
        expected_version: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(
            session
        )
        if (
            actor is None
            or draft_id is None
        ):
            raise MediaOperationsValidationError(
                "actor and draft_id are required"
            )

        key = _idempotency_key(
            idempotency_key
        )
        draft = await self._get_bulk_draft_row(
            session,
            draft_id,
            for_update=True,
        )
        actor_id = await self._assert_entity_access(
            session,
            actor,
            draft,
            permission="write",
        )

        apply_hash = sha256_json(
            {
                "draft_id": str(
                    draft.id
                ),
                "draft_hash": (
                    draft.draft_hash
                ),
            }
        )

        if draft.status == "applied":
            if (
                draft.apply_idempotency_key
                == key
                and draft.apply_hash
                == apply_hash
            ):
                return await self._bulk_apply_result(
                    session,
                    actor,
                    draft,
                )
            raise MediaOperationsConflictError(
                "Persona bulk draft has already been applied"
            )

        try:
            expected = int(
                expected_version
            )
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "expected_version must be an integer"
            ) from exc

        if expected != int(
            draft.version
        ):
            raise MediaOperationsConflictError(
                "stale Persona bulk draft version"
            )

        rows = await self._bulk_draft_rows(
            session,
            draft.id,
            for_update=True,
        )
        slots = [
            row.to_safe_dict()
            for row in rows
        ]

        occupied = await self._occupied_slots(
            session,
            actor=actor,
            project_id=draft.project_id,
        )
        issues = _semantic_issues(
            slots,
            occupied_slots=occupied,
        )

        if issues:
            if any(
                issue["code"]
                == "slot_already_occupied"
                for issue in issues
            ):
                raise MediaOperationsConflictError(
                    "one or more Persona intake slots are already occupied"
                )

            raise MediaOperationsValidationError(
                "Persona bulk draft is not applyable; "
                "resolve inferred or identity-unknown facts first"
            )

        for slot in slots:
            display_fact = slot[
                "display_name"
            ]
            if (
                display_fact["state"]
                == "unknown"
            ):
                continue

            revision_content = (
                _normalize_revision_input(
                    display_name=(
                        display_fact["value"]
                    ),
                    summary=(
                        slot["summary"][
                            "value"
                        ]
                        if (
                            slot["summary"][
                                "state"
                            ]
                            == "explicit"
                        )
                        else None
                    ),
                    voice=(
                        slot["voice"][
                            "value"
                        ]
                        if (
                            slot["voice"][
                                "state"
                            ]
                            == "explicit"
                        )
                        else None
                    ),
                    audience=(
                        slot["audience"][
                            "value"
                        ]
                        if (
                            slot["audience"][
                                "state"
                            ]
                            == "explicit"
                        )
                        else None
                    ),
                    platforms=(
                        slot["platforms"][
                            "value"
                        ]
                        if (
                            slot["platforms"][
                                "state"
                            ]
                            == "explicit"
                        )
                        else []
                    ),
                    content_pillars=(
                        slot[
                            "content_pillars"
                        ]["value"]
                        if (
                            slot[
                                "content_pillars"
                            ]["state"]
                            == "explicit"
                        )
                        else []
                    ),
                )
            )

            slot_number = int(
                slot["slot"]
            )

            create_hash = sha256_json(
                {
                    "project_id": (
                        str(
                            draft.project_id
                        )
                        if draft.project_id
                        is not None
                        else None
                    ),
                    "intake_slot": (
                        slot_number
                    ),
                    "revision": (
                        revision_content
                    ),
                }
            )

            persona = Persona(
                id=uuid4(),
                owner_user_id=actor_id,
                project_id=draft.project_id,
                create_hash=create_hash,
                idempotency_key=(
                    f"bulk:{draft.id}:{slot_number}"
                ),
                created_by=actor_id,
            )
            revision = self._build_revision(
                persona=persona,
                version=1,
                content=revision_content,
                content_hash=sha256_json(
                    revision_content
                ),
                created_by=actor_id,
                idempotency_key=None,
            )
            intake = PersonaIntakeSlot(
                id=uuid4(),
                owner_user_id=actor_id,
                project_id=draft.project_id,
                slot=slot_number,
                persona_id=persona.id,
                created_by=actor_id,
            )

            session.add(persona)
            session.add(revision)
            session.add(intake)

        draft.status = "applied"
        draft.apply_idempotency_key = key
        draft.apply_hash = apply_hash
        draft.applied_at = (
            datetime.utcnow()
        )
        draft.updated_at = (
            datetime.utcnow()
        )

        try:
            await self._flush_commit(
                session
            )
        except IntegrityError as exc:
            await self._rollback(
                session
            )

            recovered = await self._get_bulk_draft_row(
                session,
                draft_id,
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered,
                permission="read",
            )

            if (
                recovered.status
                == "applied"
                and recovered.apply_idempotency_key
                == key
                and recovered.apply_hash
                == apply_hash
            ):
                return await self._bulk_apply_result(
                    session,
                    actor,
                    recovered,
                )

            raise MediaOperationsConflictError(
                "atomic Persona bulk apply conflicted with current intake state"
            ) from exc

        return await self._bulk_apply_result(
            session,
            actor,
            draft,
        )

    async def _get_platform_account_row(
        self,
        session: Any,
        account_id: UUID | str,
        *,
        for_update: bool = False,
    ) -> PlatformAccount:
        from .media_operations_service import (
            MediaOperationsNotFoundError,
        )

        parsed = _as_uuid(
            account_id,
            "platform_account_id",
        )
        assert parsed is not None

        statement = (
            select(PlatformAccount)
            .where(
                PlatformAccount.id
                == parsed
            )
            .limit(1)
        )
        if for_update:
            statement = (
                statement.with_for_update()
            )

        account = await self._scalar(
            session,
            statement,
        )
        if account is None:
            raise MediaOperationsNotFoundError(
                "PlatformAccount not found"
            )
        return account

    async def _find_platform_account_by_idempotency(
        self,
        session: Any,
        *,
        owner_user_id: UUID,
        project_id: UUID | None,
        idempotency_key: str,
    ) -> PlatformAccount | None:
        conditions = [
            PlatformAccount.idempotency_key
            == idempotency_key
        ]

        if project_id is None:
            conditions.extend(
                [
                    PlatformAccount.project_id.is_(
                        None
                    ),
                    PlatformAccount.owner_user_id
                    == owner_user_id,
                ]
            )
        else:
            conditions.append(
                PlatformAccount.project_id
                == project_id
            )

        return await self._scalar(
            session,
            select(PlatformAccount)
            .where(*conditions)
            .limit(1),
        )

    async def _find_platform_account_identity(
        self,
        session: Any,
        *,
        owner_user_id: UUID,
        project_id: UUID | None,
        platform: str,
        account_ref: str,
    ) -> PlatformAccount | None:
        conditions = [
            PlatformAccount.platform
            == platform,
            PlatformAccount.account_ref
            == account_ref,
        ]

        if project_id is None:
            conditions.extend(
                [
                    PlatformAccount.project_id.is_(
                        None
                    ),
                    PlatformAccount.owner_user_id
                    == owner_user_id,
                ]
            )
        else:
            conditions.append(
                PlatformAccount.project_id
                == project_id
            )

        return await self._scalar(
            session,
            select(PlatformAccount)
            .where(*conditions)
            .limit(1),
        )

    async def _find_platform_revision_by_idempotency(
        self,
        session: Any,
        *,
        account_id: UUID,
        idempotency_key: str,
    ) -> PlatformAccountRevision | None:
        return await self._scalar(
            session,
            select(
                PlatformAccountRevision
            )
            .where(
                PlatformAccountRevision.platform_account_id
                == account_id,
                PlatformAccountRevision.idempotency_key
                == idempotency_key,
            )
            .limit(1),
        )

    def _build_platform_revision(
        self,
        *,
        account: PlatformAccount,
        version: int,
        content: Mapping[str, Any],
        created_by: UUID,
        idempotency_key: str | None,
    ) -> PlatformAccountRevision:
        return PlatformAccountRevision(
            id=uuid4(),
            platform_account_id=account.id,
            owner_user_id=account.owner_user_id,
            project_id=account.project_id,
            version=version,
            display_name=content[
                "display_name"
            ],
            publish_capability=content[
                "publish_capability"
            ],
            media_capability=content[
                "media_capability"
            ],
            analytics_capability=content[
                "analytics_capability"
            ],
            credential_status=content[
                "credential_status"
            ],
            remote_url=content.get("remote_url"),
            locale=content.get("locale"),
            timezone=content.get("timezone"),
            supported_content_modes_json=content.get("supported_content_modes", []),
            disclosure_defaults_json=content.get("disclosure_defaults", {}),
            rating_defaults_json=content.get("rating_defaults", {}),
            adapter_ref=content.get("adapter_ref"),
            content_hash=sha256_json(
                content
            ),
            idempotency_key=idempotency_key,
            created_by=created_by,
        )

    async def _platform_summary_map(
        self,
        session: Any,
        accounts: Sequence[
            PlatformAccount
        ],
    ) -> dict[UUID, dict[str, Any]]:
        if not accounts:
            return {}

        account_ids = [
            account.id
            for account in accounts
        ]
        revisions = await self._scalars(
            session,
            select(
                PlatformAccountRevision
            )
            .where(
                PlatformAccountRevision.platform_account_id.in_(
                    account_ids
                )
            )
            .order_by(
                PlatformAccountRevision.platform_account_id.asc(),
                PlatformAccountRevision.version.desc(),
            ),
        )

        latest: dict[
            UUID,
            PlatformAccountRevision,
        ] = {}
        for revision in revisions:
            latest.setdefault(
                revision.platform_account_id,
                revision,
            )

        result: dict[
            UUID,
            dict[str, Any],
        ] = {}

        for account in accounts:
            revision = latest.get(
                account.id
            )
            if revision is None:
                raise MediaOperationsConflictError(
                    "PlatformAccount revision history is incomplete"
                )

            result[account.id] = {
                **account.to_safe_dict(),
                "current_revision": (
                    revision.to_safe_dict()
                ),
            }

        return result

    async def _platform_detail(
        self,
        session: Any,
        account: PlatformAccount,
    ) -> dict[str, Any]:
        revisions = await self._scalars(
            session,
            select(
                PlatformAccountRevision
            )
            .where(
                PlatformAccountRevision.platform_account_id
                == account.id
            )
            .order_by(
                PlatformAccountRevision.version.desc(),
                PlatformAccountRevision.id.desc(),
            )
            .limit(101),
        )

        if not revisions:
            raise MediaOperationsConflictError(
                "PlatformAccount revision history is incomplete"
            )

        visible = revisions[:100]

        return {
            **account.to_safe_dict(),
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

    async def list_platform_accounts(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        project_id: UUID | str | None = None,
        persona_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._resolve_session(
            session
        )
        if actor is None:
            raise MediaOperationsValidationError(
                "actor is required"
            )

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        persona_uuid = _as_uuid(persona_id, "persona_id", required=False)
        if persona_uuid is not None:
            persona = await self._get_persona_row(session, persona_uuid)
            await self._assert_entity_access(session, actor, persona, permission="read")
            if project_uuid is not None and persona.project_id != project_uuid:
                raise MediaOperationsValidationError(
                    "persona and platform account query must share project scope"
                )
            project_uuid = persona.project_id

        page_limit, page_offset = (
            _bounded_page(
                limit,
                offset,
            )
        )

        condition = await self._scope_condition(
            session,
            actor,
            PlatformAccount,
            project_id=project_uuid,
        )
        if persona_uuid is not None:
            condition = and_(condition, PlatformAccount.persona_id == persona_uuid)

        accounts = await self._scalars(
            session,
            select(PlatformAccount)
            .where(condition)
            .order_by(
                PlatformAccount.created_at.desc(),
                PlatformAccount.id.desc(),
            )
            .limit(page_limit)
            .offset(page_offset),
        )

        summaries = (
            await self._platform_summary_map(
                session,
                accounts,
            )
        )

        return [
            summaries[
                account.id
            ]
            for account in accounts
        ]

    async def get_platform_account(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        account_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        session = self._resolve_session(
            session
        )
        if (
            actor is None
            or account_id is None
        ):
            raise MediaOperationsValidationError(
                "actor and platform_account_id are required"
            )

        account = await self._get_platform_account_row(
            session,
            account_id,
        )
        await self._assert_entity_access(
            session,
            actor,
            account,
            permission="read",
        )
        return await self._platform_detail(
            session,
            account,
        )

    async def create_platform_account(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        *,
        platform: Any,
        account_ref: Any,
        display_name: Any,
        publish_capability: Any = "unknown",
        media_capability: Any = "unknown",
        analytics_capability: Any = "unknown",
        credential_status: Any = "unknown",
        account_type: Any = "profile",
        remote_url: Any = None,
        locale: Any = None,
        timezone: Any = None,
        supported_content_modes: Any = None,
        disclosure_defaults: Any = None,
        rating_defaults: Any = None,
        adapter_ref: Any = None,
        connection_id: UUID | str | None = None,
        status: Any = "active",
        project_id: UUID | str | None = None,
        persona_id: UUID | str | None = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(
            session
        )
        if actor is None:
            raise MediaOperationsValidationError(
                "actor is required"
            )

        project_uuid = _as_uuid(
            project_id,
            "project_id",
            required=False,
        )
        persona_uuid = _as_uuid(persona_id, "persona_id", required=False)
        connection_uuid = _as_uuid(connection_id, "connection_id", required=False)
        platform_value = _normalize_platforms([platform])[0]
        persona = None
        if persona_uuid is not None:
            persona = await self._get_persona_row(session, persona_uuid)
            await self._assert_entity_access(
                session, actor, persona, permission="write"
            )
            if project_uuid is not None and persona.project_id != project_uuid:
                raise MediaOperationsValidationError(
                    "persona and platform account must share project scope"
                )
            project_uuid = persona.project_id
        connection = None
        if connection_uuid is not None:
            connection = await self._scalar(
                session,
                select(ExternalConnection)
                .where(ExternalConnection.id == connection_uuid)
                .limit(1),
            )
            if connection is None:
                raise MediaOperationsValidationError("connection_id not found")
            await self._assert_entity_access(
                session,
                actor,
                connection,
                permission="write",
            )
            if connection.project_id != project_uuid:
                raise MediaOperationsValidationError(
                    "connection and platform account must share project scope"
                )
            if str(connection.provider_key or "").strip().lower() != platform_value:
                raise MediaOperationsValidationError(
                    "connection provider does not match platform"
                )
        actor_id = await self._assert_create_scope(
            session,
            actor,
            project_uuid,
        )
        # A personal Character remains owned by the Character owner even when
        # an administrator performs the setup action. Otherwise the account
        # would disappear from the owner's personal ACL after creation.
        account_owner_id = (
            persona.owner_user_id
            if persona is not None and project_uuid is None
            else actor_id
        )
        # Keep the stable account owner aligned with a supplied connection in
        # every scope.  Publication/content and vault ACLs both use the
        # account owner as an execution boundary; silently attaching another
        # member's connection would create an account that can never publish
        # or accept a credential safely.
        if (
            connection is not None
            and connection.owner_user_id != account_owner_id
        ):
            raise MediaOperationsAuthorizationError(
                "platform account connection owner does not match"
            )
        key = _idempotency_key(
            idempotency_key
        )
        account_ref_value = (
            _required_text(
                account_ref,
                "account_ref",
                255,
            )
        )
        content = (
            _normalize_account_revision(
                display_name=display_name,
                # Capability and credential state are server-owned.  The
                # legacy arguments remain accepted for source compatibility,
                # but caller-provided "available/configured" values must not
                # manufacture provider readiness.
                publish_capability="unknown",
                media_capability="unknown",
                analytics_capability="unknown",
                credential_status="unknown",
                remote_url=remote_url,
                locale=locale,
                timezone=timezone,
                supported_content_modes=supported_content_modes,
                disclosure_defaults=disclosure_defaults,
                rating_defaults=rating_defaults,
                adapter_ref=adapter_ref,
            )
        )
        account_type_value = _required_text(account_type or "profile", "account_type", 32).lower()
        if any(character.isspace() for character in account_type_value):
            raise MediaOperationsValidationError("account_type must not contain whitespace")
        status_value = _required_text(status or "active", "status", 16).lower()
        if status_value not in {"active", "paused"}:
            raise MediaOperationsValidationError("status must be active or paused")

        create_hash = sha256_json(
            {
                "project_id": (
                    str(project_uuid)
                    if project_uuid
                    is not None
                    else None
                ),
                "persona_id": str(persona_uuid) if persona_uuid is not None else None,
                "connection_id": str(connection_uuid) if connection_uuid is not None else None,
                "account_type": account_type_value,
                "status": status_value,
                "platform": (
                    platform_value
                ),
                "account_ref": (
                    account_ref_value
                ),
                "revision": content,
            }
        )

        existing = (
            await self._find_platform_account_by_idempotency(
                session,
                owner_user_id=account_owner_id,
                project_id=project_uuid,
                idempotency_key=key,
            )
        )
        if existing is not None:
            if (
                existing.create_hash
                != create_hash
            ):
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with a different PlatformAccount payload"
                )
            return await self._platform_detail(
                session,
                existing,
            )

        identity = (
            await self._find_platform_account_identity(
                session,
                owner_user_id=account_owner_id,
                project_id=project_uuid,
                platform=platform_value,
                account_ref=account_ref_value,
            )
        )
        if identity is not None:
            raise MediaOperationsConflictError(
                "PlatformAccount identity already exists"
            )

        account = PlatformAccount(
            id=uuid4(),
            owner_user_id=account_owner_id,
            project_id=project_uuid,
            persona_id=persona_uuid,
            connection_id=connection_uuid,
            account_type=account_type_value,
            remote_url=content["remote_url"],
            status=status_value,
            platform=platform_value,
            account_ref=account_ref_value,
            create_hash=create_hash,
            idempotency_key=key,
            created_by=actor_id,
        )
        revision = (
            self._build_platform_revision(
                account=account,
                version=1,
                content=content,
                created_by=actor_id,
                idempotency_key=None,
            )
        )

        session.add(account)
        session.add(revision)

        try:
            await self._flush_commit(
                session
            )
        except IntegrityError as exc:
            await self._rollback(
                session
            )

            recovered = (
                await self._find_platform_account_by_idempotency(
                    session,
                    owner_user_id=account_owner_id,
                    project_id=project_uuid,
                    idempotency_key=key,
                )
            )
            if (
                recovered is not None
                and recovered.create_hash
                == create_hash
            ):
                return await self._platform_detail(
                    session,
                    recovered,
                )

            raise MediaOperationsConflictError(
                "PlatformAccount conflicts with an existing record"
            ) from exc

        return await self._platform_detail(
            session,
            account,
        )

    async def append_platform_account_revision(
        self,
        session: Any | None = None,
        actor: Any | None = None,
        account_id: UUID | str | None = None,
        *,
        expected_version: Any,
        display_name: Any,
        publish_capability: Any = "unknown",
        media_capability: Any = "unknown",
        analytics_capability: Any = "unknown",
        credential_status: Any = "unknown",
        remote_url: Any = None,
        locale: Any = None,
        timezone: Any = None,
        supported_content_modes: Any = None,
        disclosure_defaults: Any = None,
        rating_defaults: Any = None,
        adapter_ref: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        session = self._resolve_session(
            session
        )
        if (
            actor is None
            or account_id is None
        ):
            raise MediaOperationsValidationError(
                "actor and platform_account_id are required"
            )

        account = (
            await self._get_platform_account_row(
                session,
                account_id,
                for_update=True,
            )
        )
        actor_id = (
            await self._assert_entity_access(
                session,
                actor,
                account,
                permission="write",
            )
        )
        key = _idempotency_key(
            idempotency_key
        )
        latest_revision = await self._scalar(
            session,
            select(PlatformAccountRevision)
            .where(
                PlatformAccountRevision.platform_account_id == account.id,
            )
            .order_by(
                PlatformAccountRevision.version.desc(),
                PlatformAccountRevision.id.desc(),
            )
            .limit(1),
        )
        if latest_revision is None:
            raise MediaOperationsConflictError("PlatformAccount revision history is incomplete")
        if remote_url is None:
            remote_url = latest_revision.remote_url
        if locale is None:
            locale = latest_revision.locale
        if timezone is None:
            timezone = latest_revision.timezone
        if supported_content_modes is None:
            supported_content_modes = list(latest_revision.supported_content_modes_json or [])
        if disclosure_defaults is None:
            disclosure_defaults = dict(latest_revision.disclosure_defaults_json or {})
        if rating_defaults is None:
            rating_defaults = dict(latest_revision.rating_defaults_json or {})
        if adapter_ref is None:
            adapter_ref = latest_revision.adapter_ref
        content = (
            _normalize_account_revision(
                display_name=display_name,
                # Preserve the latest server-observed capability state.  A
                # revision command is not a provider verification boundary.
                publish_capability=latest_revision.publish_capability,
                media_capability=latest_revision.media_capability,
                analytics_capability=latest_revision.analytics_capability,
                credential_status=latest_revision.credential_status,
                remote_url=remote_url,
                locale=locale,
                timezone=timezone,
                supported_content_modes=supported_content_modes,
                disclosure_defaults=disclosure_defaults,
                rating_defaults=rating_defaults,
                adapter_ref=adapter_ref,
            )
        )
        content_hash = sha256_json(
            content
        )

        existing = (
            await self._find_platform_revision_by_idempotency(
                session,
                account_id=account.id,
                idempotency_key=key,
            )
        )
        if existing is not None:
            if (
                existing.content_hash
                != content_hash
            ):
                raise MediaOperationsConflictError(
                    "idempotency key was already used "
                    "with different PlatformAccount revision content"
                )
            return existing.to_safe_dict()

        current_version = await self._scalar(
            session,
            select(
                func.max(
                    PlatformAccountRevision.version
                )
            ).where(
                PlatformAccountRevision.platform_account_id
                == account.id
            ),
        )
        current = int(
            current_version or 0
        )

        try:
            expected = int(
                expected_version
            )
        except (TypeError, ValueError) as exc:
            raise MediaOperationsValidationError(
                "expected_version must be an integer"
            ) from exc

        if expected != current:
            raise MediaOperationsConflictError(
                "stale PlatformAccount version"
            )

        revision = (
            self._build_platform_revision(
                account=account,
                version=current + 1,
                content=content,
                created_by=actor_id,
                idempotency_key=key,
            )
        )
        session.add(revision)

        try:
            await self._flush_commit(
                session
            )
        except IntegrityError as exc:
            await self._rollback(
                session
            )

            recovered_account = (
                await self._get_platform_account_row(
                    session,
                    account_id,
                )
            )
            await self._assert_entity_access(
                session,
                actor,
                recovered_account,
                permission="write",
            )

            recovered = (
                await self._find_platform_revision_by_idempotency(
                    session,
                    account_id=recovered_account.id,
                    idempotency_key=key,
                )
            )
            if (
                recovered is not None
                and recovered.content_hash
                == content_hash
            ):
                return recovered.to_safe_dict()

            raise MediaOperationsConflictError(
                "PlatformAccount revision changed concurrently"
            ) from exc

        return revision.to_safe_dict()
