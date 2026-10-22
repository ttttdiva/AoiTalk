"""Manifest owned App Action and declarative UI contract.

The App workspace is editable, while the values exposed by this module are
copied into an immutable Release.  Keep this module independent from the
Manifest service so it can also be used by the runtime/API layer when it
reads a manifest from a Release source bundle.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker, SchemaError


ACTION_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EXECUTION_TARGET_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
VALUE_PATH_RE = re.compile(r"^run\.(?:result|status)(?:\.[a-zA-Z][a-zA-Z0-9_-]*)*$")

UI_SCHEMA_VERSION = 1
UI_KINDS = frozenset(
    {
        "stack",
        "card",
        "text",
        "button",
        "form",
        "select",
        "switch",
        "list",
        "table",
        "detail",
        "progress",
        "log",
        "result",
        "file",
    }
)
UI_FIELD_KINDS = frozenset({"text", "number", "select", "switch", "file"})
ACTION_EXECUTION_KINDS = frozenset({"target_command", "pc_saved_action"})
ACTION_EXECUTION_MODES = frozenset({"finite", "service", "desktop"})
ACTION_COMMAND_KEYS = frozenset({"build", "test", "run", "package"})
ACTION_RUN_STATUSES = frozenset(
    {
        "unknown",
        "queued",
        "dispatching",
        "accepted",
        "starting",
        "running",
        "stopping",
        "succeeded",
        "failed",
        "cancelled",
        "blocked",
    }
)
ACTION_READINESS_STATUSES = frozenset({"not_required", "pending", "ready", "failed", "unknown"})
ACTION_PROCESS_STATUSES = frozenset(
    {"unknown", "not_started", "starting", "running", "stopped", "succeeded", "failed", "cancelled"}
)
JSON_SCHEMA_FORMATS = frozenset(
    {
        "date",
        "date-time",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "iri",
        "iri-reference",
        "regex",
        "time",
        "uri",
        "uri-reference",
        "uuid",
    }
)

# This is deliberately a closed subset.  jsonschema itself ignores unknown
# vocabulary keywords, which would otherwise make an unsupported schema look
# accepted while the worker validates a different contract.
JSON_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "default",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "uniqueItems",
        "pattern",
        "format",
    }
)
JSON_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)
JSON_SCHEMA_MAX_DEPTH = 16
UI_MAX_NODES = 256
UI_MAX_DEPTH = 16
UI_MAX_FIELDS = 64
UI_MAX_OPTIONS = 128
FILE_METADATA_KEYS = frozenset(
    {"file_id", "filename", "content_type", "size_bytes", "sha256"}
)

_SECRET_KEY_RE = re.compile(
    r"(?:pass(?:word)?|secret|token|credential|api[_-]?key|client[_-]?secret|private[_-]?key)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?:https?|javascript|data):", re.IGNORECASE)


class AppActionContractError(ValueError):
    """Manifest Action/UI contract error."""


@dataclass(frozen=True)
class ActionContractValidationResult:
    """Normalized Action/UI projection and diagnostics."""

    ui: dict[str, Any] | None
    actions: dict[str, dict[str, Any]]
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def _bounded_string(value: Any, label: str, *, max_length: int = 255) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppActionContractError(f"{label} は空でない文字列で指定してください")
    value = value.strip()
    if len(value) > max_length:
        raise AppActionContractError(f"{label} は {max_length} 文字以内で指定してください")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise AppActionContractError(f"{label} に制御文字を含めることはできません")
    return value


def _safe_text(value: Any, label: str, *, max_length: int = 255) -> str:
    text = _bounded_string(value, label, max_length=max_length)
    if _URL_RE.search(text) or "<" in text or ">" in text:
        raise AppActionContractError(f"{label} にHTML/URL/scriptを含めることはできません")
    return text


def _reject_secret_key(key: Any, label: str) -> None:
    if _SECRET_KEY_RE.search(str(key or "")):
        raise AppActionContractError(f"{label} に秘密情報を表すキーを指定できません")


def _validate_json_value(value: Any, label: str, *, depth: int = 0) -> Any:
    if depth > JSON_SCHEMA_MAX_DEPTH:
        raise AppActionContractError(f"{label} のネストが深すぎます")
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AppActionContractError(f"{label} のキーは文字列でなければなりません")
            _reject_secret_key(key, f"{label}.{key}")
            normalized[key] = _validate_json_value(item, f"{label}.{key}", depth=depth + 1)
        return normalized
    if isinstance(value, list):
        if len(value) > 256:
            raise AppActionContractError(f"{label} の配列が大きすぎます")
        return [
            _validate_json_value(item, f"{label}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        if len(value) > 16_384:
            raise AppActionContractError(f"{label} の文字列が長すぎます")
        if _URL_RE.search(value) or "<script" in value.lower():
            raise AppActionContractError(f"{label} にHTML/URL/scriptを含めることはできません")
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise AppActionContractError(f"{label} にJSONでない浮動小数点値があります")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise AppActionContractError(f"{label} にJSONでない値があります")


def _validate_finite_json(value: Any, label: str, *, depth: int = 0) -> None:
    """Reject values that Python can hold but JSONResponse cannot encode."""

    if depth > JSON_SCHEMA_MAX_DEPTH:
        raise AppActionContractError(f"{label} のネストが深すぎます")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise AppActionContractError(f"{label} のキーは文字列でなければなりません")
            _validate_finite_json(child, f"{label}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_finite_json(child, f"{label}[{index}]", depth=depth + 1)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise AppActionContractError(f"{label} にJSONでない浮動小数点値があります")
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    raise AppActionContractError(f"{label} にJSONでない値があります")


def _validate_schema_keywords(schema: Any, label: str, *, depth: int = 0) -> None:
    if depth > JSON_SCHEMA_MAX_DEPTH:
        raise AppActionContractError(f"{label} のネストが深すぎます")
    if not isinstance(schema, dict):
        raise AppActionContractError(f"{label} はJSON Schema objectでなければなりません")
    _validate_finite_json(schema, label, depth=depth)
    unknown = sorted(set(schema) - JSON_SCHEMA_KEYS)
    if unknown:
        raise AppActionContractError(
            f"{label} に未対応のJSON Schemaキーワードがあります: {', '.join(unknown)}"
        )
    if "$schema" in schema:
        schema_id = schema["$schema"]
        if schema_id != "https://json-schema.org/draft/2020-12/schema":
            raise AppActionContractError(f"{label}.$schema は Draft 2020-12 のみ対応します")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not types or any(item not in JSON_SCHEMA_TYPES for item in types):
            raise AppActionContractError(f"{label}.type が不正です")
    if "format" in schema and schema["format"] not in JSON_SCHEMA_FORMATS:
        raise AppActionContractError(f"{label}.format が未対応です")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise AppActionContractError(f"{label}.required は文字列配列でなければなりません")
        if len(required) > 128:
            raise AppActionContractError(f"{label}.required が多すぎます")
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            raise AppActionContractError(f"{label}.properties は object でなければなりません")
        for key, child in properties.items():
            if not isinstance(key, str):
                raise AppActionContractError(f"{label}.properties のキーは文字列でなければなりません")
            _reject_secret_key(key, f"{label}.properties.{key}")
            _validate_schema_keywords(child, f"{label}.properties.{key}", depth=depth + 1)
    if "items" in schema:
        _validate_schema_keywords(schema["items"], f"{label}.items", depth=depth + 1)
    if isinstance(schema.get("additionalProperties"), dict):
        _validate_schema_keywords(
            schema["additionalProperties"],
            f"{label}.additionalProperties",
            depth=depth + 1,
        )
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum or len(enum) > 128:
            raise AppActionContractError(f"{label}.enum は1〜128件の配列でなければなりません")
        _validate_json_value(enum, f"{label}.enum")
    if "default" in schema:
        # Defaults are useful for forms, but must never carry credentials.
        _validate_json_value(schema["default"], f"{label}.default")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise AppActionContractError(f"{label} が不正なJSON Schemaです") from exc


def validate_json_schema_definition(
    schema: Any,
    label: str = "schema",
    *,
    require_object: bool = False,
) -> dict[str, Any]:
    """Validate and return a closed, supported JSON Schema definition.

    Action inputs are object payloads.  Outputs may be any JSON value, so the
    caller can opt into the input restriction without weakening the schema
    vocabulary or runtime validator.
    """
    _validate_schema_keywords(schema, label)
    normalized = copy.deepcopy(schema)
    declared_type = normalized.get("type")
    if require_object and declared_type is not None and (
        declared_type != "object" and declared_type != ["object"]
    ):
        raise AppActionContractError(f"{label}.type は object でなければなりません")
    if require_object and declared_type is None:
        raise AppActionContractError(f"{label}.type はobjectを明示してください")
    return normalized


def validate_json_schema_value(
    value: Any,
    schema: Mapping[str, Any],
    *,
    label: str = "input",
) -> None:
    """Validate a runtime JSON value against a previously validated schema."""
    try:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    except SchemaError as exc:
        raise AppActionContractError(f"{label} のSchemaが不正です") from exc
    if errors:
        first = errors[0]
        path = ".".join(str(item) for item in first.path)
        suffix = f" ({path})" if path else ""
        raise AppActionContractError(f"{label} がSchemaに一致しません{suffix}: {first.message}")


def _validate_execution(
    raw: Any,
    *,
    label: str,
    target: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AppActionContractError(f"{label} は object でなければなりません")
    allowed = {"kind", "mode", "command_key", "saved_action_id", "sha256", "contract_hash"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AppActionContractError(f"{label} に未対応のキーがあります: {', '.join(unknown)}")
    kind = raw.get("kind")
    mode = raw.get("mode")
    if kind not in ACTION_EXECUTION_KINDS:
        raise AppActionContractError(f"{label}.kind が不正です")
    if mode not in ACTION_EXECUTION_MODES:
        raise AppActionContractError(f"{label}.mode が不正です")
    normalized: dict[str, Any] = {"kind": kind, "mode": mode}
    if kind == "target_command":
        command_key = raw.get("command_key")
        if command_key not in ACTION_COMMAND_KEYS:
            raise AppActionContractError(f"{label}.command_key が不正です")
        if command_key not in target:
            raise AppActionContractError(
                f"{label}.command_key={command_key} はTargetに定義されていません"
            )
        normalized["command_key"] = command_key
        if (
            raw.get("saved_action_id") is not None
            or raw.get("sha256") is not None
            or raw.get("contract_hash") is not None
        ):
            raise AppActionContractError(f"{label} にpc_saved_action用の値があります")
    else:
        saved_action_id = _safe_text(raw.get("saved_action_id"), f"{label}.saved_action_id", max_length=255)
        if _URL_RE.search(saved_action_id) or "/" in saved_action_id or "\\" in saved_action_id:
            raise AppActionContractError(f"{label}.saved_action_id はopaque IDで指定してください")
        digest = raw.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise AppActionContractError(f"{label}.sha256 は64文字のSHA-256で指定してください")
        contract_hash = raw.get("contract_hash")
        if contract_hash is not None and (
            not isinstance(contract_hash, str) or not SHA256_RE.fullmatch(contract_hash)
        ):
            raise AppActionContractError(
                f"{label}.contract_hash は64文字のSHA-256で指定してください"
            )
        normalized.update({"saved_action_id": saved_action_id, "sha256": digest.lower()})
        if contract_hash is not None:
            normalized["contract_hash"] = contract_hash.lower()
        if raw.get("command_key") is not None:
            raise AppActionContractError(f"{label} にtarget_command用の値があります")
    return normalized


def _validate_field(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AppActionContractError(f"{label} は object でなければなりません")
    allowed = {"name", "label", "kind", "required", "options"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AppActionContractError(f"{label} に未対応のキーがあります: {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not FIELD_NAME_RE.fullmatch(name):
        raise AppActionContractError(f"{label}.name が不正です")
    result = {
        "name": name,
        "label": _safe_text(raw.get("label"), f"{label}.label"),
        "kind": raw.get("kind"),
        "required": raw.get("required", False),
    }
    if result["kind"] not in UI_FIELD_KINDS:
        raise AppActionContractError(f"{label}.kind が不正です")
    if not isinstance(result["required"], bool):
        raise AppActionContractError(f"{label}.required はbooleanで指定してください")
    options = raw.get("options")
    if result["kind"] == "select":
        if not isinstance(options, list) or not options or len(options) > UI_MAX_OPTIONS:
            raise AppActionContractError(f"{label}.options は1〜{UI_MAX_OPTIONS}件必要です")
        normalized_options: list[dict[str, str]] = []
        for index, option in enumerate(options):
            option_label = f"{label}.options[{index}]"
            if not isinstance(option, dict) or set(option) != {"label", "value"}:
                raise AppActionContractError(f"{option_label} は label/value object で指定してください")
            normalized_options.append(
                {
                    "label": _safe_text(option.get("label"), f"{option_label}.label"),
                    "value": _safe_text(option.get("value"), f"{option_label}.value"),
                }
            )
        result["options"] = normalized_options
    elif options is not None:
        raise AppActionContractError(f"{label}.options はselectでのみ指定できます")
    return result


def _schema_types(schema: Mapping[str, Any]) -> set[str]:
    """Return the explicitly declared primitive types for a field schema."""

    value = schema.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _validate_file_field_schema(
    schema: Mapping[str, Any],
    *,
    field_label: str,
    action_key: str,
    field_name: str,
) -> None:
    """Require the canonical uploaded-file metadata object on file fields."""

    properties = schema.get("properties")
    required = schema.get("required")
    if (
        schema.get("type") != "object"
        or not isinstance(properties, Mapping)
        or set(properties) != FILE_METADATA_KEYS
        or not isinstance(required, list)
        or set(required) != FILE_METADATA_KEYS
        or schema.get("additionalProperties") is not False
    ):
        raise AppActionContractError(
            f"{field_label} はcanonical file metadata objectのみ対応します "
            f"(actions.{action_key}.input_schema.properties.{field_name})"
        )
    for key in FILE_METADATA_KEYS:
        child = properties.get(key)
        if not isinstance(child, Mapping):
            raise AppActionContractError(
                f"{field_label} のfile metadata property {key} が不正です"
            )
        expected_type = "integer" if key == "size_bytes" else "string"
        if child.get("type") != expected_type:
            raise AppActionContractError(
                f"{field_label} のfile metadata property {key}.type が不正です"
            )


def _validate_form_field_binding(
    field: Mapping[str, Any],
    *,
    field_label: str,
    action_key: str,
    action: Mapping[str, Any],
) -> None:
    """Prove that one declarative form field can satisfy its Action schema."""

    schema = action.get("input_schema")
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise AppActionContractError(
            f"actions.{action_key}.input_schema はform配線に使えるobjectでなければなりません"
        )
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or field["name"] not in properties:
        raise AppActionContractError(
            f"{field_label}.name={field['name']} が actions.{action_key}.input_schema.properties にありません"
        )
    property_schema = properties[field["name"]]
    if not isinstance(property_schema, Mapping):
        raise AppActionContractError(
            f"actions.{action_key}.input_schema.properties.{field['name']} が不正です"
        )
    actual_types = _schema_types(property_schema)
    if field["kind"] == "file":
        _validate_file_field_schema(
            property_schema,
            field_label=field_label,
            action_key=action_key,
            field_name=str(field["name"]),
        )
        actual_types = {"object"}
    expected_types = {
        "text": {"string"},
        # Browser file inputs are uploaded first and the Action receives the
        # immutable canonical metadata object.
        "file": {"object"},
        "number": {"number", "integer"},
        "switch": {"boolean"},
        "select": {"string"},
    }[str(field["kind"])]
    if not actual_types or not actual_types.intersection(expected_types):
        raise AppActionContractError(
            f"{field_label} のkind={field['kind']} と actions.{action_key}.input_schema.properties.{field['name']}.type が一致しません"
        )

    required = schema.get("required") or []
    required_set = {item for item in required if isinstance(item, str)}
    is_required = field.get("required") is True
    if (field["name"] in required_set) != is_required:
        expected = "required=true" if field["name"] in required_set else "required=false"
        raise AppActionContractError(
            f"{field_label} は actions.{action_key}.input_schema.required と一致させてください ({expected})"
        )

    if field["kind"] == "select":
        enum = property_schema.get("enum")
        if not isinstance(enum, list) or not enum:
            raise AppActionContractError(
                f"{field_label} はselectのため input_schema.properties.{field['name']}.enum が必要です"
            )
        enum_values = {str(value) for value in enum}
        options = field.get("options") or []
        if any(str(option.get("value")) not in enum_values for option in options):
            raise AppActionContractError(
                f"{field_label}.options のvalueが actions.{action_key}.input_schema.properties.{field['name']}.enum にありません"
            )


def _ui_form_action_candidates(
    raw: Mapping[str, Any],
    *,
    actions: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    action_ref = raw.get("action_ref")
    # The renderer submits a form as one Action payload.  A neighbouring
    # button, target_key, or single-action default is not a safe binding: the
    # form must state the exact Action it feeds.
    if isinstance(action_ref, str) and action_ref in actions:
        return [action_ref]
    return []


def _validate_ui_node(
    raw: Any,
    *,
    label: str,
    action_keys: set[str],
    target_keys: set[str],
    actions: Mapping[str, Mapping[str, Any]],
    form_fields: dict[str, list[dict[str, Any]]],
    ids: set[str],
    counter: list[int],
    depth: int,
) -> dict[str, Any]:
    if depth > UI_MAX_DEPTH:
        raise AppActionContractError(f"{label} のUIネストが深すぎます")
    if not isinstance(raw, dict):
        raise AppActionContractError(f"{label} は object でなければなりません")
    allowed = {
        "id",
        "kind",
        "children",
        "fields",
        "columns",
        "label",
        "text",
        "data",
        "value",
        "value_path",
        "action_ref",
        "target_key",
        "name",
        "options",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AppActionContractError(f"{label} に未対応のキーがあります: {', '.join(unknown)}")
    counter[0] += 1
    if counter[0] > UI_MAX_NODES:
        raise AppActionContractError(f"UI node は {UI_MAX_NODES} 件以内で指定してください")
    node_id = raw.get("id")
    if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
        raise AppActionContractError(f"{label}.id が不正です")
    if node_id in ids:
        raise AppActionContractError(f"UI node id が重複しています: {node_id}")
    ids.add(node_id)
    kind = raw.get("kind")
    if kind not in UI_KINDS:
        raise AppActionContractError(f"{label}.kind が不正です")
    if kind in {"select", "switch"} and "name" not in raw:
        raise AppActionContractError(f"{label}.name は{kind}で必須です")
    if kind == "select" and "options" not in raw:
        raise AppActionContractError(f"{label}.options はselectで必須です")
    normalized: dict[str, Any] = {"id": node_id, "kind": kind}
    for text_key in ("label", "text"):
        if text_key in raw:
            normalized[text_key] = _safe_text(raw[text_key], f"{label}.{text_key}", max_length=16_384)
    if "target_key" in raw:
        if raw["target_key"] not in target_keys:
            raise AppActionContractError(f"{label}.target_key が存在しません")
        normalized["target_key"] = raw["target_key"]
    if "action_ref" in raw:
        action_ref = raw["action_ref"]
        if not isinstance(action_ref, str) or action_ref not in action_keys:
            raise AppActionContractError(f"{label}.action_ref が存在しません")
        normalized["action_ref"] = action_ref
    if "name" in raw:
        name = raw["name"]
        if not isinstance(name, str) or not FIELD_NAME_RE.fullmatch(name):
            raise AppActionContractError(f"{label}.name が不正です")
        normalized["name"] = name
    if "options" in raw:
        if kind != "select":
            raise AppActionContractError(f"{label}.options はselectでのみ指定できます")
        options = raw["options"]
        if not isinstance(options, list) or not options or len(options) > UI_MAX_OPTIONS:
            raise AppActionContractError(f"{label}.options は1〜{UI_MAX_OPTIONS}件必要です")
        normalized_options: list[dict[str, str]] = []
        for index, option in enumerate(options):
            option_label = f"{label}.options[{index}]"
            if not isinstance(option, dict) or set(option) != {"label", "value"}:
                raise AppActionContractError(f"{option_label} はlabel/value objectで指定してください")
            normalized_options.append(
                {
                    "label": _safe_text(option.get("label"), f"{option_label}.label"),
                    "value": _safe_text(option.get("value"), f"{option_label}.value"),
                }
            )
        normalized["options"] = normalized_options
    if "columns" in raw:
        columns = raw["columns"]
        if kind != "table":
            raise AppActionContractError(f"{label}.columns はtableでのみ指定できます")
        if not isinstance(columns, list) or not columns or len(columns) > UI_MAX_FIELDS:
            raise AppActionContractError(f"{label}.columns は1〜{UI_MAX_FIELDS}件必要です")
        normalized_columns: list[dict[str, str]] = []
        for index, column in enumerate(columns):
            column_label = f"{label}.columns[{index}]"
            if not isinstance(column, dict) or set(column) != {"name", "label"}:
                raise AppActionContractError(f"{column_label} はname/label objectで指定してください")
            column_name = column.get("name")
            if not isinstance(column_name, str) or not FIELD_NAME_RE.fullmatch(column_name):
                raise AppActionContractError(f"{column_label}.name が不正です")
            normalized_columns.append(
                {
                    "name": column_name,
                    "label": _safe_text(column.get("label"), f"{column_label}.label"),
                }
            )
        normalized["columns"] = normalized_columns
    if "value_path" in raw:
        value_path = raw["value_path"]
        if not isinstance(value_path, str) or not VALUE_PATH_RE.fullmatch(value_path):
            raise AppActionContractError(f"{label}.value_path はrun.result/run.status配下に限定されます")
        normalized["value_path"] = value_path
    for value_key in ("data", "value"):
        if value_key in raw:
            normalized[value_key] = _validate_json_value(raw[value_key], f"{label}.{value_key}")
    if "fields" in raw:
        fields = raw["fields"]
        if not isinstance(fields, list) or len(fields) > UI_MAX_FIELDS:
            raise AppActionContractError(f"{label}.fields は最大 {UI_MAX_FIELDS} 件です")
        normalized_fields = [
            _validate_field(field, f"{label}.fields[{index}]")
            for index, field in enumerate(fields)
        ]
        normalized["fields"] = normalized_fields
        if kind == "form" and normalized_fields:
            candidates = _ui_form_action_candidates(raw, actions=actions)
            if not candidates:
                raise AppActionContractError(
                    f"{label}.fields はaction_refまたは一意のtarget_keyでActionへ接続してください"
                )
            action_key = candidates[0]
            action = actions[action_key]
            for index, field in enumerate(normalized_fields):
                _validate_form_field_binding(
                    field,
                    field_label=f"{label}.fields[{index}]",
                    action_key=action_key,
                    action=action,
                )
            form_fields.setdefault(action_key, []).extend(normalized_fields)
    if "children" in raw:
        children = raw["children"]
        if not isinstance(children, list) or len(children) > UI_MAX_NODES:
            raise AppActionContractError(f"{label}.children が不正です")
        normalized["children"] = [
            _validate_ui_node(
                child,
                label=f"{label}.children[{index}]",
                action_keys=action_keys,
                target_keys=target_keys,
                actions=actions,
                form_fields=form_fields,
                ids=ids,
                counter=counter,
                depth=depth + 1,
            )
            for index, child in enumerate(children)
        ]
    return normalized


def _validate_ui(
    raw: Any,
    *,
    action_keys: set[str],
    target_keys: set[str],
    actions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AppActionContractError("ui は object でなければなりません")
    allowed = {"schema_version", "target_key", "root"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AppActionContractError(f"ui に未対応のキーがあります: {', '.join(unknown)}")
    if raw.get("schema_version") != UI_SCHEMA_VERSION:
        raise AppActionContractError(f"ui.schema_version は {UI_SCHEMA_VERSION} でなければなりません")
    target_key = raw.get("target_key")
    if target_key is not None and target_key not in target_keys:
        raise AppActionContractError("ui.target_key が存在しません")
    ids: set[str] = set()
    form_fields: dict[str, list[dict[str, Any]]] = {}
    root = _validate_ui_node(
        raw.get("root"),
        label="ui.root",
        action_keys=action_keys,
        target_keys=target_keys,
        actions=actions,
        form_fields=form_fields,
        ids=ids,
        counter=[0],
        depth=0,
    )
    for action_key, action in actions.items():
        schema = action.get("input_schema")
        required = schema.get("required") if isinstance(schema, Mapping) else []
        required_names = {item for item in (required or []) if isinstance(item, str)}
        if not required_names:
            continue
        field_names = {str(field.get("name")) for field in form_fields.get(action_key, [])}
        missing = sorted(required_names - field_names)
        if missing:
            raise AppActionContractError(
                f"ui form が actions.{action_key}.input_schema.required を入力可能にしていません: {', '.join(missing)}"
            )
    result: dict[str, Any] = {"schema_version": UI_SCHEMA_VERSION, "root": root}
    if target_key is not None:
        result["target_key"] = target_key
    return result


def validate_action_contract(
    manifest: Mapping[str, Any],
    *,
    workspace: Path | None = None,
    strict: bool = False,
) -> ActionContractValidationResult:
    """Validate top-level ``ui`` and ``actions`` and normalize both.

    ``strict`` requires path-based schemas to resolve inside ``workspace``;
    draft validation still rejects malformed/unsupported declarations.
    """
    errors: list[str] = []
    warnings: list[str] = []
    targets = manifest.get("targets")
    target_map = targets if isinstance(targets, dict) else {}
    target_keys = {str(key) for key in target_map}
    raw_actions = manifest.get("actions", {})
    actions: dict[str, dict[str, Any]] = {}
    if raw_actions is None:
        raw_actions = {}
    if not isinstance(raw_actions, dict):
        errors.append("actions は object でなければなりません")
        raw_actions = {}
    if len(raw_actions) > 256:
        errors.append("actions は256件以内で指定してください")
    for raw_key, raw_action in raw_actions.items():
        key = str(raw_key)
        label = f"actions.{key}"
        if not ACTION_KEY_RE.fullmatch(key):
            errors.append(f"Action key が不正です: {key}")
            continue
        if not isinstance(raw_action, dict):
            errors.append(f"{label} は object でなければなりません")
            continue
        try:
            allowed = {
                "label",
                "description",
                "target_key",
                "input_schema",
                "output_schema",
                "execution_target_id",
                "execution",
            }
            unknown = sorted(set(raw_action) - allowed)
            if unknown:
                raise AppActionContractError(
                    f"{label} に未対応のキーがあります: {', '.join(unknown)}"
                )
            target_key = raw_action.get("target_key")
            if not isinstance(target_key, str) or target_key not in target_map:
                raise AppActionContractError(f"{label}.target_key が存在しません")
            target_snapshot = target_map[target_key]
            if not isinstance(target_snapshot, dict):
                raise AppActionContractError(f"targets.{target_key} が不正です")
            normalized_action: dict[str, Any] = {
                "label": _safe_text(raw_action.get("label"), f"{label}.label"),
                "target_key": target_key,
            }
            if "description" in raw_action:
                normalized_action["description"] = _safe_text(
                    raw_action["description"], f"{label}.description", max_length=2_000
                )
            execution_target_id = raw_action.get("execution_target_id")
            if not isinstance(execution_target_id, str) or not EXECUTION_TARGET_RE.fullmatch(execution_target_id):
                raise AppActionContractError(f"{label}.execution_target_id が不正です")
            normalized_action["execution_target_id"] = execution_target_id
            normalized_action["execution"] = _validate_execution(
                raw_action.get("execution"), label=f"{label}.execution", target=target_snapshot
            )
            for schema_key in ("input_schema", "output_schema"):
                schema = raw_action.get(schema_key)
                if schema is None:
                    # An Action may inherit a Target input schema.  Preserve
                    # path references here so the same workspace/archive
                    # hydrator resolves them before the schema is consumed;
                    # silently replacing a path with an empty object would
                    # make a malformed UI appear valid until runtime.
                    if schema_key == "input_schema" and target_snapshot.get("input_schema") is not None:
                        schema = target_snapshot["input_schema"]
                    else:
                        schema = {"type": "object", "properties": {}, "additionalProperties": False}
                if isinstance(schema, str):
                    # Path references are resolved and embedded by the existing
                    # target sync/release source bundle flow.
                    try:
                        schema = schema.replace("\\", "/")
                        if not schema or schema.startswith("/") or ".." in Path(schema).parts:
                            raise ValueError
                        if workspace is not None:
                            candidate = (Path(workspace) / Path(schema)).resolve()
                            candidate.relative_to(Path(workspace).resolve())
                            if not candidate.is_file():
                                raise ValueError
                            schema = json.loads(candidate.read_text(encoding="utf-8"))
                        else:
                            normalized_action[schema_key] = schema
                            continue
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        if strict or schema_key == "input_schema":
                            raise AppActionContractError(f"{label}.{schema_key} の参照先が不正です") from exc
                        warnings.append(f"{label}.{schema_key} の参照先をworkspaceなしでは確認できません")
                        normalized_action[schema_key] = schema
                        continue
                normalized_action[schema_key] = validate_json_schema_definition(
                    schema,
                    f"{label}.{schema_key}",
                    require_object=schema_key == "input_schema",
                )
            actions[key] = normalized_action
        except AppActionContractError as exc:
            errors.append(str(exc))
    ui: dict[str, Any] | None = None
    if manifest.get("ui") is not None:
        try:
            ui = _validate_ui(
                manifest["ui"],
                action_keys=set(actions),
                target_keys=target_keys,
                actions=actions,
            )
        except AppActionContractError as exc:
            errors.append(str(exc))
    return ActionContractValidationResult(
        ui=ui,
        actions=actions,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def make_release_action_id(release_id: UUID | str, action_key: str) -> str:
    """Create the opaque, Release-bound Action ID exposed to clients."""
    try:
        release_uuid = UUID(str(release_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AppActionContractError("release_id が不正です") from exc
    if not isinstance(action_key, str) or not ACTION_KEY_RE.fullmatch(action_key):
        raise AppActionContractError("action_key が不正です")
    return f"{release_uuid}:{action_key}"


def parse_release_action_id(action_id: str) -> tuple[UUID, str]:
    """Parse and validate an opaque ``<release_uuid>:<action_key>`` ID."""
    if not isinstance(action_id, str) or ":" not in action_id:
        raise AppActionContractError("action_id が不正です")
    release_text, action_key = action_id.split(":", 1)
    try:
        release_id = UUID(release_text)
    except (TypeError, ValueError, AttributeError) as exc:
        raise AppActionContractError("action_id のRelease IDが不正です") from exc
    if not ACTION_KEY_RE.fullmatch(action_key):
        raise AppActionContractError("action_id のAction keyが不正です")
    return release_id, action_key


def build_runtime_action_descriptors(
    actions: Mapping[str, Mapping[str, Any]],
    *,
    release_id: UUID | str,
) -> list[dict[str, Any]]:
    """Return the public Action catalog without commands or local paths."""
    result: list[dict[str, Any]] = []
    for action_key, action in actions.items():
        descriptor = {
            "action_id": make_release_action_id(release_id, str(action_key)),
            "label": action.get("label", str(action_key)),
            "description": action.get("description"),
            "target_key": action.get("target_key"),
            "input_schema": copy.deepcopy(action.get("input_schema") or {}),
            "output_schema": copy.deepcopy(action.get("output_schema") or {}),
            "execution_target_id": action.get("execution_target_id"),
            "execution": {
                "kind": (action.get("execution") or {}).get("kind"),
                "mode": (action.get("execution") or {}).get("mode"),
                **(
                    {"command_key": (action.get("execution") or {}).get("command_key")}
                    if (action.get("execution") or {}).get("command_key") is not None
                    else {}
                ),
                **(
                    {
                        "saved_action_id": (action.get("execution") or {}).get("saved_action_id"),
                        "sha256": (action.get("execution") or {}).get("sha256"),
                    }
                    if (action.get("execution") or {}).get("saved_action_id") is not None
                    else {}
                ),
            },
            "available": True,
            "unavailable_reason": None,
        }
        result.append(descriptor)
    return result


def materialize_runtime_ui(ui: Mapping[str, Any] | None, *, release_id: UUID | str) -> dict[str, Any] | None:
    """Convert manifest ``action_ref`` values to opaque runtime Action IDs."""
    if ui is None:
        return None
    result = copy.deepcopy(dict(ui))

    def visit(node: dict[str, Any]) -> None:
        action_ref = node.pop("action_ref", None)
        if action_ref is not None:
            node["action_id"] = make_release_action_id(release_id, str(action_ref))
        for child in node.get("children", []):
            if isinstance(child, dict):
                visit(child)

    root = result.get("root")
    if isinstance(root, dict):
        visit(root)
    return result


__all__ = [
    "ACTION_COMMAND_KEYS",
    "ACTION_EXECUTION_KINDS",
    "ACTION_EXECUTION_MODES",
    "ACTION_KEY_RE",
    "ACTION_PROCESS_STATUSES",
    "ACTION_READINESS_STATUSES",
    "ACTION_RUN_STATUSES",
    "ActionContractValidationResult",
    "AppActionContractError",
    "JSON_SCHEMA_FORMATS",
    "JSON_SCHEMA_KEYS",
    "UI_FIELD_KINDS",
    "UI_KINDS",
    "build_runtime_action_descriptors",
    "make_release_action_id",
    "materialize_runtime_ui",
    "parse_release_action_id",
    "validate_action_contract",
    "validate_json_schema_definition",
    "validate_json_schema_value",
]
