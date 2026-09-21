"""Shared projections/markers for the local ``/masking`` operation.

The masking command deliberately keeps the original user turn in the
conversation database.  That row is useful for an audit trail, but must not
be copied into learning, retrieval, summarisation, title generation, or
group/session projections.  This module centralises the small, structural
metadata predicate used by those projections so each caller does not invent
its own (and potentially inconsistent) marker interpretation.

Only the boolean ``privacy_masking.source`` marker is an authority here.  The
marker is written by the trusted server command path; user text and rendered
prompt content are never parsed for a masking marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePath
import re
from typing import Any, Iterable


PRIVACY_MASKING_METADATA_KEY = "privacy_masking"
PRIVACY_MASKING_SOURCE_KEY = "source"


def _safe_output_name(value: Any) -> str:
    """Return a bounded basename for a generated masking artifact.

    ``MaskingResult`` implementations are intentionally duck-typed for
    rolling deployments and embedding fakes.  A malformed result must not be
    able to smuggle an absolute source path (or a path containing newlines)
    into assistant metadata, so normalize the display name at this final
    projection boundary as well as in the canonical file transformer.
    """

    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        return ""
    name = PurePath(raw).name.strip()
    name = re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", name).strip(" .")
    if not name or name in {".", ".."}:
        return ""
    return name[:255]


def _safe_extension(value: Any) -> str:
    """Keep only a conventional short file extension in public metadata."""

    extension = str(value or "").strip().casefold()
    if not extension or len(extension) > 16 or not re.fullmatch(
        r"\.[a-z0-9]{1,15}", extension
    ):
        return ""
    return extension


def privacy_masking_source_metadata(
    *,
    invocation_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build the minimal durable metadata for a masking source turn.

    ``invocation_id``/``status`` are optional non-sensitive correlation
    values.  Raw input, alias maps, and findings containing source text are
    intentionally not accepted here.
    """

    marker: dict[str, Any] = {PRIVACY_MASKING_SOURCE_KEY: True}
    if invocation_id:
        marker["invocation_id"] = str(invocation_id)
    if status:
        marker["status"] = str(status)
    return {PRIVACY_MASKING_METADATA_KEY: marker}


def is_privacy_masking_source(value: Any) -> bool:
    """Return whether a message/metadata mapping is a masking source row.

    Callers pass either a ConversationMessage-like object or its metadata
    mapping.  We require the nested marker to be an actual ``True`` boolean;
    arbitrary user text such as ``"privacy_masking.source=true"`` is never
    treated as authority.
    """

    metadata: Any = value
    if isinstance(value, Mapping):
        # Projection callers frequently pass ``ConversationMessage.to_dict``
        # records rather than ORM objects.  In that shape the authoritative
        # marker lives under ``metadata`` (or the internal
        # ``message_metadata`` key), while callers that already pass a
        # metadata mapping expose ``privacy_masking`` at the top level.
        if PRIVACY_MASKING_METADATA_KEY not in value:
            candidate = value.get("message_metadata")
            if not isinstance(candidate, Mapping):
                candidate = value.get("metadata")
            if isinstance(candidate, Mapping):
                metadata = candidate
    else:
        metadata = getattr(value, "message_metadata", None)
        # Some lightweight ORM/test doubles expose both names, with an empty
        # ``message_metadata`` placeholder and the actual payload under
        # ``metadata``.  Prefer the authoritative key when present, otherwise
        # fall back to the alias without treating arbitrary object attributes
        # as a marker.
        if not isinstance(metadata, Mapping) or PRIVACY_MASKING_METADATA_KEY not in metadata:
            metadata = getattr(value, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    marker = metadata.get(PRIVACY_MASKING_METADATA_KEY)
    if not isinstance(marker, Mapping):
        return False
    return marker.get(PRIVACY_MASKING_SOURCE_KEY) is True


def filter_privacy_masking_sources(values: Iterable[Any]) -> list[Any]:
    """Drop masking source rows while preserving order for projections."""

    return [value for value in values if not is_privacy_masking_source(value)]


def safe_masking_result_metadata(result: Any) -> dict[str, Any]:
    """Project a MaskingResult into persistence-safe metadata.

    The result object is intentionally duck-typed to keep this helper usable
    while the masking service is being upgraded.  Only bounded status,
    invocation, semantic state, finding *categories*, and generated output
    file metadata are copied.  Source text, aliases, and arbitrary provider
    payloads are never persisted.
    """

    metadata: dict[str, Any] = {"operation": "masking"}
    for key in ("status", "invocation_id", "semantic_status"):
        value = getattr(result, key, None)
        if value is None and isinstance(result, Mapping):
            value = result.get(key)
        if value not in (None, ""):
            if hasattr(value, "value"):
                value = value.value
            metadata[key] = str(value)[:200]

    findings = getattr(result, "findings", None)
    if findings is None and isinstance(result, Mapping):
        findings = result.get("findings")
    categories: list[str] = []
    for finding in findings or ():
        category = getattr(finding, "category", None)
        if category is None and isinstance(finding, Mapping):
            category = finding.get("category")
        if category:
            normalized = str(category).strip()[:80]
            if normalized and normalized not in categories:
                categories.append(normalized)
    if categories:
        metadata["finding_categories"] = categories[:64]

    files = getattr(result, "files", None)
    if files is None and isinstance(result, Mapping):
        files = result.get("files")
    outputs: list[dict[str, Any]] = []
    for item in files or ():
        if isinstance(item, Mapping):
            get = item.get
        else:
            get = lambda key, default=None: getattr(item, key, default)
        output_name = _safe_output_name(
            get("output_name", "")
            or get("filename", "")
            or get("name", "")
            or ""
        )
        # The output path is an internal delivery detail.  Persisting it in a
        # chat/AgentRun projection would disclose workspace topology; callers
        # may resolve/download the generated file from the service result but
        # durable metadata contains only its public name/hash/size.
        if not output_name:
            continue
        # The service has already authorised and materialised this path.  Do
        # not copy arbitrary source/path/exception fields from a provider
        # object.
        output: dict[str, Any] = {}
        output["name"] = output_name[:255]
        for source_key, target_key in (
            ("sha256", "sha256"),
            ("output_sha256", "sha256"),
            ("mime_type", "mime_type"),
            ("extension", "extension"),
            ("size", "size"),
            ("size_bytes", "size"),
        ):
            value = get(source_key)
            if value in (None, ""):
                continue
            if target_key == "size":
                try:
                    value = max(0, int(value))
                except (TypeError, ValueError, OverflowError):
                    continue
            elif target_key == "extension":
                value = _safe_extension(value)
                if not value:
                    continue
            elif target_key == "mime_type":
                value = str(value).strip().casefold()
                if not re.fullmatch(
                    r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}",
                    value,
                ):
                    continue
            else:
                value = str(value)[:200]
            output[target_key] = value
        outputs.append(output)
    if outputs:
        metadata["files"] = outputs[:32]
    return metadata


def _is_masking_result_metadata(value: Mapping[str, Any]) -> bool:
    marker = value.get(PRIVACY_MASKING_METADATA_KEY)
    return isinstance(marker, Mapping) and marker.get("result") is True


def _model_masking_file_metadata(value: Any, index: int) -> dict[str, Any]:
    """Return a generic artifact descriptor for model-facing session tools.

    Durable/UI metadata may retain the generated filename and download path,
    but those values can contain customer or workspace names.  A model-facing
    tool only needs to know that a masked artifact exists, its type, and small
    integrity/size facts; it must not receive the source-derived name/path.
    """

    if isinstance(value, Mapping):
        get = value.get
    else:
        get = lambda key, default=None: getattr(value, key, default)
    extension = _safe_extension(get("extension"))
    generic_name = f"masked_file_{index}{extension}"
    output: dict[str, Any] = {
        "name": generic_name,
        "filename": generic_name,
        "kind": "masked_file",
    }
    for source_key, target_key in (
        ("sha256", "sha256"),
        ("output_sha256", "sha256"),
        ("mime_type", "mime_type"),
        ("size", "size"),
        ("size_bytes", "size_bytes"),
        ("extension", "extension"),
    ):
        raw = get(source_key)
        if raw in (None, "") or target_key in output:
            continue
        if target_key in {"size", "size_bytes"}:
            try:
                output[target_key] = max(0, int(raw))
            except (TypeError, ValueError, OverflowError):
                continue
        elif target_key == "extension":
            if extension:
                output[target_key] = extension
        elif target_key == "mime_type":
            normalized = str(raw).strip().casefold()
            if re.fullmatch(
                r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}",
                normalized,
            ):
                output[target_key] = normalized
        else:
            output[target_key] = str(raw)[:200]
    return output


def public_model_message_metadata(value: Any) -> Any:
    """Project persisted metadata for model-facing conversation tools.

    Normal conversation APIs intentionally keep the durable/UI metadata
    projection unchanged.  This stricter projection is used only by tools
    that expose old messages back to a cloud model.  Masking result artifacts
    are genericized and all path/source-derived names are removed.
    """

    if isinstance(value, list):
        return [public_model_message_metadata(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    if _is_masking_result_metadata(value):
        projected: dict[str, Any] = {}
        for key in (
            "operation",
            "status",
            "invocation_id",
            "semantic_status",
            "finding_categories",
            "source_message_id",
            PRIVACY_MASKING_METADATA_KEY,
        ):
            if key in value:
                projected[key] = public_model_message_metadata(value[key])
        for key in ("files", "attachments"):
            items = value.get(key)
            if isinstance(items, (list, tuple)):
                projected[key] = [
                    _model_masking_file_metadata(item, index)
                    for index, item in enumerate(items, start=1)
                ]
        return projected
    return {
        str(key): public_model_message_metadata(item)
        for key, item in value.items()
        if key != "reasoning_content"
    }


__all__ = [
    "PRIVACY_MASKING_METADATA_KEY",
    "PRIVACY_MASKING_SOURCE_KEY",
    "filter_privacy_masking_sources",
    "is_privacy_masking_source",
    "public_model_message_metadata",
    "privacy_masking_source_metadata",
    "safe_masking_result_metadata",
]
