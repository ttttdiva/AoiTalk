"""Playback settings contract consumed from the immutable 87 runtime snapshot.

The schema, defaults, limits and cross-field rules live in 87. This module only
validates/persists that data; it does not implement motion or physics.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Literal

from pydantic import ConfigDict, Field, create_model, field_validator

from .spec import Record

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_RELATIVE_PATH = Path("resources/seethrough-rig/runtime/web/js/runtime/playback-settings.schema.json")
_RUNTIME_ENV_PATH = _PROJECT_ROOT.parent / "85_CaddyGateway/config/private/runtime.env"


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _registered_release_root(project_root: Path) -> Path | None:
    """Read only the registered release path; never inspect runtime secrets."""
    bindings: list[str] = []
    try:
        with _RUNTIME_ENV_PATH.open(encoding="utf-8-sig") as stream:
            for line in stream:
                key, separator, value = line.strip().partition("=")
                if separator and key.strip().upper() == "GATEWAY_AOITALK_ROOT":
                    value = value.strip()
                    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
                        value = value[1:-1]
                    bindings.append(value)
        if len(bindings) != 1:
            return None
        candidate = Path(bindings[0])
        if not candidate.is_absolute():
            return None
        root = project_root.resolve(strict=True)
        candidate = Path(os.path.abspath(candidate))
        releases = root / "artifacts" / "releases"
        relative = candidate.relative_to(releases)
        if not relative.parts:
            return None
        current = root
        for part in (candidate / "main.py").relative_to(root).parts:
            current /= part
            if _is_reparse_point(current):
                return None
        if not (candidate / "main.py").is_file():
            return None
        return candidate
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return None


def resolve_schema_path(
    project_root: Path | None = None,
    *,
    registered_release_root: Path | None = None,
) -> Path:
    """Use the source asset, or its checksum-verified registered-release copy.

    Some worktrees intentionally omit the generated third-party runtime copy.
    Its validated, immutable release may supply the exact schema without
    restoring files into or changing the worktree.
    """
    root = (project_root or _PROJECT_ROOT).resolve()
    source_schema = root / _SCHEMA_RELATIVE_PATH
    if source_schema.is_file():
        return source_schema

    release_root = registered_release_root or _registered_release_root(root)
    if release_root is None:
        return source_schema
    try:
        releases = root / "artifacts" / "releases"
        release_root = Path(os.path.abspath(release_root))
        release_root.relative_to(releases)
        runtime_root = release_root / "resources" / "seethrough-rig" / "runtime"
        candidate = runtime_root / _SCHEMA_RELATIVE_PATH.relative_to("resources/seethrough-rig/runtime")
        manifest_path = runtime_root / "runtime-provenance.json"
        for path in (release_root, runtime_root, candidate, manifest_path):
            resolved = path
            chain = [resolved]
            chain.extend(resolved.parents)
            if any(_is_reparse_point(part) for part in reversed(chain) if part.exists()):
                return source_schema
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("package") != "@aoitalk/seethrough-rig-runtime":
            return source_schema
        entries = [
            entry for entry in manifest.get("files", [])
            if isinstance(entry, dict)
            and entry.get("path") == "web/js/runtime/playback-settings.schema.json"
        ]
        if len(entries) != 1 or not candidate.is_file():
            return source_schema
        expected = entries[0].get("sha256")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or actual != expected.lower()
            or candidate.stat().st_size != entries[0].get("size")
        ):
            return source_schema
        return candidate
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return source_schema


SCHEMA_PATH = resolve_schema_path()
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VERSION = SCHEMA["version"]
GROUPS = {group["id"]: {field["id"]: field for field in group["fields"]} for group in SCHEMA["groups"]}


class PlaybackSettingsError(ValueError):
    pass


def validate_block(value, *, full=False, require_version=False):
    if not isinstance(value, dict):
        raise PlaybackSettingsError("再生設定にはobjectが必要です")
    if set(value) - {"version", *GROUPS}:
        raise PlaybackSettingsError("未対応の再生設定フィールドです")
    if "version" in value:
        if type(value["version"]) is not int or value["version"] != VERSION:
            raise PlaybackSettingsError("未対応の再生設定versionです")
    elif require_version or full:
        raise PlaybackSettingsError("再生設定versionが必要です")
    if full and not set(GROUPS) <= set(value):
        raise PlaybackSettingsError("再生設定groupが不足しています")
    for group_id, fields in GROUPS.items():
        if group_id not in value:
            continue
        group = value[group_id]
        if not isinstance(group, dict) or set(group) - set(fields) or (full and set(group) != set(fields)):
            raise PlaybackSettingsError(f"再生設定 {group_id} のフィールドが不正です")
        for field_id, actual in group.items():
            field = fields[field_id]
            valid = False
            if field["type"] == "boolean":
                valid = type(actual) is bool
            elif field["type"] == "enum":
                valid = isinstance(actual, str) and actual in {option["value"] for option in field["options"]}
            elif field["type"] == "number":
                valid = (type(actual) in (int, float) and field["min"] <= actual <= field["max"]
                         and math.isfinite(actual))
            if not valid:
                raise PlaybackSettingsError(f"再生設定 {group_id}.{field_id} の型または範囲が不正です")
    validate_constraints(value)
    return copy.deepcopy(value)


def validate_constraints(value):
    def at(path):
        group, field = path.split(".")
        return value.get(group, {}).get(field)
    for constraint in SCHEMA.get("constraints", []):
        # Partial patches inherit their condition from the eventual merged
        # settings, not necessarily from canonical defaults.
        if any(at(path) is None or at(path) != expected
               for path, expected in constraint.get("when", {}).items()):
            continue
        if constraint["kind"] != "ordered":
            raise RuntimeError("Unsupported playback settings constraint")
        paths = constraint["paths"]
        if any(at(a) is not None and at(b) is not None and at(a) > at(b) for a, b in zip(paths, paths[1:])):
            raise PlaybackSettingsError(f"再生設定 {paths[0]} は {paths[-1]} 以下にしてください")


def canonical_defaults():
    return {"version": VERSION, **{group: {key: copy.deepcopy(field["default"]) for key, field in fields.items()} for group, fields in GROUPS.items()}}


def merge_settings(base, *patches):
    result = validate_block(base, full=True)
    for patch in patches:
        patch = validate_block(patch)
        for group, fields in patch.items():
            if group != "version":
                result[group].update(fields)
    validate_constraints(result)
    return result


def legacy_profile_settings(profile):
    """Preserve old exported flags when no explicit shared settings exist."""
    return {"version": VERSION,
            "wind": {"wind": profile.get("wind", 0), "windBlow": profile.get("windBlow", 0)},
            "blink": {"enabled": profile.get("blink", True)},
            "breath": {"enabled": profile.get("breath", True)},
            "idle": {"enabled": profile.get("idle", True)},
            "gaze": {"enabled": profile.get("saccade", True), "tracking": profile.get("lookAt", False), "follow": profile.get("follow", .6)}}


def package_defaults(manifest, model, settings=None):
    # Each layer is validated even when comparison/baseline playback ignores it.
    # The manifest snapshot intentionally may override authored payload settings.
    blocks = [legacy_profile_settings(manifest.get("profile", {}))]
    for container in (model, settings or {}, manifest):
        if "playbackSettings" in container:
            block = container["playbackSettings"]
            validate_block(block, require_version=True, full=container is manifest)
            blocks.append(block)
    return merge_settings(canonical_defaults(), *blocks)


class StrictPlaybackRecord(Record):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != VERSION:
            raise ValueError("未対応の再生設定versionです")
        return value


def no_schema_default(schema):
    # Missing patch values must stay optional in generated Web/Native types.
    schema.pop("default", None)


def settings_model(*, partial):
    fields = {"version": (Literal[VERSION], Field(default=VERSION if partial else ..., json_schema_extra=no_schema_default))}
    suffix = "Patch" if partial else ""
    for group, definitions in GROUPS.items():
        group_fields = {}
        for name, definition in definitions.items():
            kind = definition["type"]
            annotation = bool if kind == "boolean" else Literal[tuple(option["value"] for option in definition["options"])] if kind == "enum" else float
            options = {"ge": definition["min"], "le": definition["max"]} if kind == "number" else {}
            # A missing partial field is permitted; explicit null is rejected.
            group_fields[name] = (annotation, Field(default=None if partial else ..., json_schema_extra=no_schema_default, **options))
        group_type = create_model(f"Playback{group.title()}{suffix}", __base__=StrictPlaybackRecord, **group_fields)
        fields[group] = (group_type, Field(default=None if partial else ..., json_schema_extra=no_schema_default))
    return create_model(f"PlaybackSettings{suffix}", __base__=StrictPlaybackRecord, **fields)


PlaybackSettings = settings_model(partial=False)
PlaybackSettingsPatch = settings_model(partial=True)


class PlaybackSettingsReset(StrictPlaybackRecord):
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)


class PlaybackSettingsUpdate(PlaybackSettingsReset):
    settings: PlaybackSettingsPatch


class PlaybackSettingsView(StrictPlaybackRecord):
    character_id: str
    build_id: str
    manifest_sha256: str
    mode: Literal["conversation-v1", "comparison-v1"]
    editable: bool
    revision: int
    schema_version: int
    defaults: PlaybackSettings
    overrides: PlaybackSettingsPatch
    effective: PlaybackSettings
    updated_at: str | None
