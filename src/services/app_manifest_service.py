"""AoiTalk App Manifest validation and DB target synchronization.

## Manifest の位置付け

``aoitalk.app.yaml`` が App workspace の正本である。``app_targets`` は表示・
検索用の派生スナップショットに過ぎないため、Manifest が壊れた状態で登録される
と Build / Release / Job 実行が実行時に初めて失敗する。そのため検証はここに
一元化し、API・Target 同期・Build・Release がすべて同じ関数を共有する。

## 検証モードの方針（最重要）

Manifest の検証項目は「パス形式・型・許容値」のように *いつでも* 判定できるもの
と、「entrypoint / build.output が実在するか」のように *workspace の状態* に
依存するものに分かれる。後者を常に必須にすると、まだ build していない開発中の
workspace では Manifest を保存すらできない。逆に常に任意にすると、実体の無い
entrypoint を持つ Manifest が有効として登録され、Release 作成時に初めて壊れる。

そこで実在チェックだけをモードで切り替える:

``ValidationMode.DRAFT``
    開発中・Manifest 保存時・``validate_manifest`` API・Target 同期で使う。
    形式・型・許容値・workspace 外参照はすべて **error**。entrypoint と
    build.output の実在は **warning** に留め、有効な Manifest として登録できる。

``ValidationMode.STRICT``
    Build / Run 実行前と Release 作成時に使う。DRAFT の全 error に加えて
    entrypoint と build.output の実在・種別（file / directory）と、
    build.output 配下に entrypoint があることを **error** にする。

検証基準そのものは :func:`validate_manifest` 1 つしか無く、呼び出し元は
``mode`` を切り替えるだけである。二重定義された基準は存在しない。

### モードで切り替えない実在チェック

``input_schema`` にパスを指定した場合の実ファイル存在・JSON 解析可否は
**両モードで error** とする。:func:`sync_manifest_targets_unlocked` が
``manifest_snapshot`` へ実スキーマを埋め込む際に必ずこのファイルを読むため、
DRAFT で通してしまうと Target 同期が別経路で失敗し状態が食い違う。

## 戻り値の互換

``validate_manifest`` / ``validate_manifest_text`` /
``validate_workspace_manifest`` は errors と warnings を分離した
:class:`ManifestValidationResult` を返す。既存の
``validate_app_manifest`` / ``parse_manifest_text`` / ``load_app_manifest`` /
``validate_manifest_workspace`` は従来どおり正規化済み dict を返し、error が
あれば ``AppManifestError`` を送出する薄いラッパとして残す（warnings は捨てる）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models.apps import (
    APP_EXECUTION_HOSTS,
    APP_TARGET_RUNTIMES,
    APP_TARGET_SURFACES,
    App,
    AppArtifact,
    AppTarget,
    TaskAppLink,
)
from .app_storage import (
    AppStorageError,
    normalize_app_relative_path,
    resolve_workspace_file,
)


MANIFEST_FILENAME = "aoitalk.app.yaml"
MANIFEST_SCHEMA_VERSION = 1

#: Manifest が定義できる job command。``app_job_service`` の job_type と一致する。
MANIFEST_COMMAND_KEYS = ("build", "test", "run", "package")

#: command object で意味を持つキー。``command`` は実行文字列、``output`` は
#: build 成果物ディレクトリ（``app_release_service`` が読む）。
MANIFEST_COMMAND_FIELDS = {"command", "output"}

#: 既知の top-level キー。未知キーは error にせず warning に留める。
MANIFEST_TOP_LEVEL_KEYS = {
    "schema_version",
    "name",
    "description",
    "overview",
    "targets",
}

#: 既知の target キー。``input_label`` 系は import / 業務分析サービスが書く。
MANIFEST_TARGET_KEYS = {
    "display_name",
    "description",
    "purpose",
    "surface",
    "runtime",
    "execution_host",
    "entrypoint",
    "input_schema",
    "capabilities",
    "input_label",
    "input_detail",
    "process_label",
    "process_detail",
    "output_label",
    "output_detail",
    *MANIFEST_COMMAND_KEYS,
}

#: 実際に AoiTalk 側で解釈できる capability。App Bridge の method 名と一致する
#: ものだけが実効的で、それ以外は宣言できても常に拒否される。未知でも形式が
#: 正しければ error にはせず warning に留める。
APP_KNOWN_CAPABILITIES = {
    "docs.read",
    "tasks.read",
    "project.files.read",
    "project.files.write",
}

#: JSON Schema の ``type`` に指定できる値。
JSON_SCHEMA_TYPES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}

MANIFEST_MAX_NAME_LENGTH = 255
MANIFEST_MAX_DISPLAY_NAME_LENGTH = 255
MANIFEST_MAX_TARGET_KEY_LENGTH = 80
MANIFEST_MAX_COMMAND_LENGTH = 2000
_JSON_SCHEMA_MAX_DEPTH = 12

_TARGET_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CAPABILITY_RE = re.compile(r"^(?:[a-z][a-z0-9_.-]*|network:[A-Za-z0-9_.-]+)$")
_COMMAND_FORBIDDEN_CHARS = set("\x00\r\n;&|<>`%")
_COMMAND_FORBIDDEN_SEQUENCES = ("$(", "${")


class AppManifestError(ValueError):
    """Manifest parsing or validation error."""


class ValidationMode(str, Enum):
    """Manifest 検証の厳格度。モジュール docstring の方針を参照。"""

    #: 開発中・保存時・validate API。実在チェックは warning。
    DRAFT = "draft"
    #: 実行 / Build / Release 作成時。実在チェックは error。
    STRICT = "strict"

    @classmethod
    def coerce(cls, value: "ValidationMode | str | None") -> "ValidationMode":
        if value is None:
            return cls.DRAFT
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise TypeError(f"未知の ValidationMode です: {value!r}")


@dataclass(frozen=True)
class ManifestValidationResult:
    """errors と warnings を分離した検証結果。

    ``errors`` が空なら Manifest は当該モードで有効であり、``manifest`` は
    正規化済みの dict である。``errors`` がある場合 ``manifest`` は途中まで
    正規化された参考値で、登録に使ってはならない。
    """

    manifest: dict[str, Any]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    mode: ValidationMode = ValidationMode.DRAFT

    @property
    def valid(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> dict[str, Any]:
        """error があれば ``AppManifestError`` を送出し、無ければ正規化 dict を返す。"""
        if self.errors:
            raise AppManifestError("; ".join(self.errors))
        return self.manifest

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "mode": self.mode.value,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "manifest": self.manifest,
        }


class _UniqueKeyManifestLoader(yaml.SafeLoader):
    """重複キーを黙って握り潰さない YAML loader。

    PyYAML の既定動作では ``targets`` に同じ target key を 2 回書くと後勝ちで
    静かに 1 件へ潰れる。Target が消えたのか元から無かったのか判別できなく
    なるため、Manifest では重複キー自体を error にする。
    """

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        if not isinstance(node, yaml.nodes.MappingNode):
            return super().construct_mapping(node, deep=deep)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicated = key in mapping
            except TypeError as exc:
                raise AppManifestError("Manifest のキーに使えない値があります") from exc
            if duplicated:
                raise AppManifestError(f"Manifest にキーが重複しています: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _existence_issue(
    message: str,
    *,
    mode: ValidationMode,
    errors: list[str],
    warnings: list[str],
) -> None:
    """実在依存の指摘を STRICT では error、DRAFT では warning として記録する。"""
    if mode is ValidationMode.STRICT:
        errors.append(message)
    else:
        warnings.append(message)


def _safe_manifest_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppManifestError(f"{label} は必須です")
    try:
        return normalize_app_relative_path(value)
    except AppStorageError as exc:
        raise AppManifestError(f"{label}: {exc}") from exc


def _resolve_in_workspace(
    workspace: Path,
    relative: str,
    label: str,
    errors: list[str],
) -> Path | None:
    """workspace 内の実パスを解決する。

    ``..`` / 絶対パス / ``.git`` / シンボリックリンク越えはモードに関係なく
    常に error である（実在の有無ではなく参照範囲の問題であるため）。
    """
    try:
        return resolve_workspace_file(workspace, relative)
    except AppStorageError as exc:
        errors.append(f"{label} は App workspace 外を参照しています: {exc}")
        return None


def _validate_command(
    value: Any,
    label: str,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        command = value.strip()
        config: dict[str, Any] = {"command": command}
    elif isinstance(value, dict):
        raw_command = value.get("command")
        if raw_command is not None and not isinstance(raw_command, str):
            errors.append(f"{label}.command は文字列で指定してください")
            command = ""
        else:
            command = str(raw_command or "").strip()
        config = dict(value)
        for unknown in sorted(set(config) - MANIFEST_COMMAND_FIELDS):
            warnings.append(f"{label}.{unknown} は AoiTalk が解釈しないキーです")
    else:
        errors.append(f"{label} は文字列または object で指定してください")
        return None
    if not command:
        errors.append(f"{label}.command は空にできません")
    elif len(command) > MANIFEST_MAX_COMMAND_LENGTH:
        errors.append(
            f"{label}.command は {MANIFEST_MAX_COMMAND_LENGTH} 文字以内で指定してください"
        )
    elif any(character in _COMMAND_FORBIDDEN_CHARS for character in command):
        errors.append(
            f"{label}.command にshell演算子・改行・環境変数展開を含めることはできません"
        )
    elif any(sequence in command for sequence in _COMMAND_FORBIDDEN_SEQUENCES):
        errors.append(f"{label}.command にコマンド置換・変数展開を含めることはできません")
    else:
        try:
            argv = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            argv = []
            errors.append(f"{label}.command の引用符が不正です: {exc}")
        if not argv:
            errors.append(f"{label}.command をargvへ分解できません")
        elif argv[0].startswith("/"):
            errors.append(f"{label}.command は絶対パスの実行ファイルを指定できません")
    # Command may contain POSIX separators (python scripts/run.py), but it may
    # not embed traversal, drive paths, shell home expansion or workspace roots.
    if (
        ".." in command
        or "~\\" in command
        or re.search(r"(?:^|[\s'\"])~/", command)
        or re.search(r"(?:^|[\s'\"])[A-Za-z]:[\\/]", command)
    ):
        errors.append(f"{label}.command は workspace 外を参照できません")
    if "output" in config and config["output"] is not None:
        try:
            config["output"] = _safe_manifest_path(config["output"], f"{label}.output")
        except AppManifestError as exc:
            errors.append(str(exc))
    return config


def _validate_json_schema(
    schema: Any,
    label: str,
    errors: list[str],
    warnings: list[str],
    *,
    depth: int = 0,
    top_level: bool = True,
) -> None:
    """JSON Schema として最低限の整合が取れているかを検証する。

    外部ライブラリに依存せず、AoiTalk が実際に使う範囲（``type`` /
    ``properties`` / ``required`` / ``items`` / ``enum``）の型と相互整合だけを
    見る。フル仕様の検証は行わない。
    """
    if depth > _JSON_SCHEMA_MAX_DEPTH:
        errors.append(f"{label} のネストが深すぎます")
        return
    if isinstance(schema, bool) and not top_level:
        return
    if not isinstance(schema, dict):
        errors.append(f"{label} はJSON objectでなければなりません")
        return

    declared_types: set[str] = set()
    if "type" in schema:
        raw_type = schema["type"]
        if isinstance(raw_type, str):
            candidates = [raw_type]
        elif isinstance(raw_type, list) and raw_type:
            candidates = raw_type
        else:
            candidates = []
            errors.append(f"{label}.type は文字列または非空の配列で指定してください")
        for item in candidates:
            if not isinstance(item, str) or item not in JSON_SCHEMA_TYPES:
                errors.append(f"{label}.type が不正です: {item!r}")
            else:
                declared_types.add(item)
    elif top_level:
        warnings.append(f"{label}.type が未指定です。object を明示してください")

    if top_level and declared_types and "object" not in declared_types:
        # App Job の入力は ``AOITALK_APP_INPUT_JSON`` の JSON object で渡るため、
        # object 以外を宣言した input_schema は決して成立しない。
        errors.append(f"{label}.type は object でなければなりません")

    properties = schema.get("properties")
    if "properties" in schema:
        if not isinstance(properties, dict):
            errors.append(f"{label}.properties はJSON objectでなければなりません")
            properties = None
        else:
            if not top_level and declared_types and "object" not in declared_types:
                errors.append(
                    f"{label}.properties を持つ場合 type は object でなければなりません"
                )
            for property_name, property_schema in properties.items():
                if not isinstance(property_name, str) or not property_name:
                    errors.append(f"{label}.properties のキーは非空の文字列です")
                    continue
                _validate_json_schema(
                    property_schema,
                    f"{label}.properties.{property_name}",
                    errors,
                    warnings,
                    depth=depth + 1,
                    top_level=False,
                )

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(
            isinstance(item, str) and item for item in required
        ):
            errors.append(f"{label}.required は非空の文字列配列でなければなりません")
        else:
            if len(set(required)) != len(required):
                errors.append(f"{label}.required に重複があります")
            if declared_types and "object" not in declared_types:
                errors.append(
                    f"{label}.required を持つ場合 type は object でなければなりません"
                )
            if isinstance(properties, dict):
                missing = [item for item in required if item not in properties]
                if missing:
                    errors.append(
                        f"{label}.required に properties へ無い項目があります: "
                        + ", ".join(sorted(missing))
                    )

    if "items" in schema:
        items = schema["items"]
        if declared_types and "array" not in declared_types:
            errors.append(f"{label}.items を持つ場合 type は array でなければなりません")
        if isinstance(items, list):
            for index, item in enumerate(items):
                _validate_json_schema(
                    item,
                    f"{label}.items[{index}]",
                    errors,
                    warnings,
                    depth=depth + 1,
                    top_level=False,
                )
        else:
            _validate_json_schema(
                items, f"{label}.items", errors, warnings, depth=depth + 1, top_level=False
            )

    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not schema["enum"]:
            errors.append(f"{label}.enum は非空の配列でなければなりません")

    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if not isinstance(additional, (bool, dict)):
            errors.append(f"{label}.additionalProperties は boolean または object です")
        elif isinstance(additional, dict):
            _validate_json_schema(
                additional,
                f"{label}.additionalProperties",
                errors,
                warnings,
                depth=depth + 1,
                top_level=False,
            )


def _check_input_schema(
    target: dict[str, Any],
    label: str,
    *,
    workspace: Path | None,
    errors: list[str],
    warnings: list[str],
) -> None:
    """``input_schema`` を検証する。

    パス指定の場合、実ファイルの存在と JSON 解析可否は **両モードで error**。
    :func:`sync_manifest_targets_unlocked` がこのファイルを必ず読んで
    ``manifest_snapshot`` へ展開するため、DRAFT で通すと Target 同期が別経路で
    失敗して状態が食い違う。
    """
    input_schema = target.get("input_schema")
    if input_schema is None:
        return
    if isinstance(input_schema, dict):
        _validate_json_schema(input_schema, f"{label}.input_schema", errors, warnings)
        return
    if not isinstance(input_schema, str):
        errors.append(f"{label}.input_schema は path または object で指定してください")
        return

    try:
        normalized = _safe_manifest_path(input_schema, f"{label}.input_schema")
    except AppManifestError as exc:
        errors.append(str(exc))
        return
    target["input_schema"] = normalized
    if workspace is None:
        return
    try:
        schema_path = resolve_workspace_file(workspace, normalized)
    except AppStorageError:
        errors.append(f"{label}.input_schema のパスが不正です")
        return
    if not schema_path.exists() or not schema_path.is_file():
        errors.append(f"{label}.input_schema のファイルがありません: {normalized}")
        return
    try:
        schema_value = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append(f"{label}.input_schema をJSONとして読み込めません")
        return
    if not isinstance(schema_value, dict):
        errors.append(f"{label}.input_schema はJSON objectでなければなりません")
        return
    _validate_json_schema(schema_value, f"{label}.input_schema", errors, warnings)


def _check_capabilities(
    target: dict[str, Any],
    label: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    capabilities = target.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        errors.append(f"{label}.capabilities は文字列配列でなければなりません")
        return
    normalized: list[str] = []
    for capability in capabilities:
        value = capability.strip()
        if not value:
            errors.append(f"{label}.capabilities に空の権限があります")
            continue
        if not _CAPABILITY_RE.fullmatch(value):
            errors.append(f"{label}.capabilities に不正な権限があります: {value}")
            continue
        if value in normalized:
            errors.append(f"{label}.capabilities に重複があります: {value}")
            continue
        if value not in APP_KNOWN_CAPABILITIES and not value.startswith("network:"):
            warnings.append(
                f"{label}.capabilities の {value} は AoiTalk が未対応のため常に拒否されます"
            )
        normalized.append(value)
    target["capabilities"] = normalized


def _check_target_paths(
    target: dict[str, Any],
    label: str,
    *,
    mode: ValidationMode,
    workspace: Path | None,
    errors: list[str],
    warnings: list[str],
) -> None:
    """entrypoint と build.output の参照範囲・実在を検証する。

    参照範囲（``..`` / 絶対パス / symlink 越え）はモードに関係なく error。
    実在は DRAFT で warning、STRICT で error。
    """
    entrypoint = target.get("entrypoint")
    build = target.get("build") if isinstance(target.get("build"), dict) else None
    output = build.get("output") if isinstance(build, dict) else None
    output = output if isinstance(output, str) and output else None

    if output and isinstance(entrypoint, str):
        try:
            PurePosixPath(entrypoint).relative_to(PurePosixPath(output))
        except ValueError:
            _existence_issue(
                f"{label}.entrypoint が build.output 配下にありません: {entrypoint}",
                mode=mode,
                errors=errors,
                warnings=warnings,
            )

    if workspace is None:
        return

    if output:
        output_path = _resolve_in_workspace(workspace, output, f"{label}.build.output", errors)
        if output_path is not None and not output_path.is_dir():
            _existence_issue(
                f"{label}.build.output が存在しないかディレクトリではありません: {output}",
                mode=mode,
                errors=errors,
                warnings=warnings,
            )

    if isinstance(entrypoint, str) and entrypoint:
        entrypoint_path = _resolve_in_workspace(
            workspace, entrypoint, f"{label}.entrypoint", errors
        )
        if entrypoint_path is not None and not entrypoint_path.is_file():
            _existence_issue(
                f"{label}.entrypoint のファイルがありません: {entrypoint}",
                mode=mode,
                errors=errors,
                warnings=warnings,
            )


def _validate_target(
    key: str,
    raw: Any,
    *,
    mode: ValidationMode,
    workspace: Path | None,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any] | None:
    label = f"targets.{key}"
    if not isinstance(raw, dict):
        errors.append(f"{label} は object でなければなりません")
        return None
    target = dict(raw)

    for unknown in sorted(set(target) - MANIFEST_TARGET_KEYS):
        warnings.append(f"{label}.{unknown} は AoiTalk が解釈しないキーです")

    for field_name in ("surface", "runtime", "execution_host"):
        value = target.get(field_name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}.{field_name} は必須です")
    if target.get("surface") not in APP_TARGET_SURFACES:
        errors.append(
            f"{label}.surface が不正です。許容値: " + ", ".join(sorted(APP_TARGET_SURFACES))
        )
    if target.get("runtime") not in APP_TARGET_RUNTIMES:
        errors.append(
            f"{label}.runtime が不正です。許容値: " + ", ".join(sorted(APP_TARGET_RUNTIMES))
        )
    if target.get("execution_host") not in APP_EXECUTION_HOSTS:
        errors.append(
            f"{label}.execution_host が不正です。許容値: "
            + ", ".join(sorted(APP_EXECUTION_HOSTS))
        )

    try:
        target["entrypoint"] = _safe_manifest_path(target.get("entrypoint"), f"{label}.entrypoint")
    except AppManifestError as exc:
        errors.append(str(exc))
        target["entrypoint"] = None

    for command_key in MANIFEST_COMMAND_KEYS:
        if command_key in target:
            target[command_key] = _validate_command(
                target.get(command_key), f"{label}.{command_key}", errors, warnings
            )

    _check_input_schema(target, label, workspace=workspace, errors=errors, warnings=warnings)
    _check_capabilities(target, label, errors, warnings)
    _check_target_paths(
        target,
        label,
        mode=mode,
        workspace=workspace,
        errors=errors,
        warnings=warnings,
    )

    display_name = target.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        errors.append(f"{label}.display_name は文字列で指定してください")
        display_name = None
    display_name = str(display_name or key)
    if len(display_name) > MANIFEST_MAX_DISPLAY_NAME_LENGTH:
        errors.append(
            f"{label}.display_name は {MANIFEST_MAX_DISPLAY_NAME_LENGTH} 文字以内で指定してください"
        )
    target["display_name"] = display_name
    return target


def validate_manifest(
    manifest: Any,
    *,
    mode: ValidationMode | str = ValidationMode.DRAFT,
    workspace: Path | None = None,
) -> ManifestValidationResult:
    """Manifest を検証して errors / warnings を分離した結果を返す。

    これが唯一の検証実装であり、validate API / Target 同期 / Build / Release は
    すべて ``mode`` だけを変えてこの関数を共有する。

    :param mode: :class:`ValidationMode`。既定は DRAFT。
    :param workspace: App workspace。``None`` の場合ファイル実在チェックは
        行わない（Release source bundle のように workspace が無い経路向け）。
    :raises TypeError: ``mode`` が STRICT なのに ``workspace`` が無い場合。
        実在チェックが不可能なまま STRICT を名乗るのを防ぐ、呼び出し側のバグ。
    """
    mode = ValidationMode.coerce(mode)
    if mode is ValidationMode.STRICT and workspace is None:
        raise TypeError("ValidationMode.STRICT には workspace が必要です")

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        return ManifestValidationResult(
            manifest={},
            errors=("Manifest は object でなければなりません",),
            warnings=(),
            mode=mode,
        )

    for unknown in sorted(set(map(str, manifest)) - MANIFEST_TOP_LEVEL_KEYS):
        warnings.append(f"{unknown} は AoiTalk が解釈しない top-level キーです")

    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version は {MANIFEST_SCHEMA_VERSION} でなければなりません")

    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name は必須です")
    elif len(name.strip()) > MANIFEST_MAX_NAME_LENGTH:
        errors.append(f"name は {MANIFEST_MAX_NAME_LENGTH} 文字以内で指定してください")

    description = manifest.get("description")
    if isinstance(description, (dict, list)):
        errors.append("description は文字列で指定してください")
        description = ""

    if "overview" in manifest and not isinstance(manifest.get("overview"), dict):
        warnings.append("overview は object で指定してください。無視されます")

    targets = manifest.get("targets")
    if not isinstance(targets, dict) or not targets:
        errors.append("targets は1件以上の object でなければなりません")
        targets = {}

    normalized_targets: dict[str, dict[str, Any]] = {}
    for target_key, raw in targets.items():
        key = str(target_key)
        if not _TARGET_KEY_RE.fullmatch(key):
            errors.append(f"target key が不正です: {key}")
        elif len(key) > MANIFEST_MAX_TARGET_KEY_LENGTH:
            errors.append(
                f"target key は {MANIFEST_MAX_TARGET_KEY_LENGTH} 文字以内で指定してください: {key}"
            )
        if key in normalized_targets:
            # YAML の literal 重複は loader が弾くが、``1`` と ``"1"`` のように
            # 文字列化して初めて衝突するキーはここでしか検出できない。
            errors.append(f"target key が重複しています: {key}")
            continue
        target = _validate_target(
            key,
            raw,
            mode=mode,
            workspace=workspace,
            errors=errors,
            warnings=warnings,
        )
        if target is None:
            continue
        normalized_targets[key] = target

    normalized = dict(manifest)
    normalized["schema_version"] = MANIFEST_SCHEMA_VERSION
    normalized["name"] = str(name).strip() if isinstance(name, str) else ""
    normalized["description"] = str(description or "")
    normalized["targets"] = normalized_targets
    return ManifestValidationResult(
        manifest=normalized,
        errors=tuple(errors),
        warnings=tuple(warnings),
        mode=mode,
    )


def validate_manifest_text(
    text: str,
    *,
    mode: ValidationMode | str = ValidationMode.DRAFT,
    workspace: Path | None = None,
) -> ManifestValidationResult:
    """YAML テキストを解析して :func:`validate_manifest` にかける。"""
    try:
        raw = yaml.load(text, Loader=_UniqueKeyManifestLoader)
    except yaml.YAMLError as exc:
        raise AppManifestError(f"YAML を解析できません: {exc}") from exc
    return validate_manifest(raw, mode=mode, workspace=workspace)


def validate_workspace_manifest(
    workspace: Path,
    *,
    mode: ValidationMode | str = ValidationMode.DRAFT,
) -> tuple[ManifestValidationResult, str, str]:
    """workspace の ``aoitalk.app.yaml`` を読み、結果・本文・sha256 を返す。"""
    try:
        path = resolve_workspace_file(workspace, MANIFEST_FILENAME)
    except AppStorageError as exc:
        raise AppManifestError(str(exc)) from exc
    if not path.exists() or not path.is_file():
        raise AppManifestError(f"{MANIFEST_FILENAME} がありません")
    text = path.read_text(encoding="utf-8")
    result = validate_manifest_text(text, mode=mode, workspace=Path(workspace))
    return result, text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 既存呼び出し元との互換 API（error で送出、warnings は破棄）
# ---------------------------------------------------------------------------


def validate_app_manifest(
    manifest: Any,
    *,
    mode: ValidationMode | str = ValidationMode.DRAFT,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Return normalized manifest or raise with all actionable errors."""
    return validate_manifest(manifest, mode=mode, workspace=workspace).raise_for_errors()


def parse_manifest_text(
    text: str,
    *,
    mode: ValidationMode | str = ValidationMode.DRAFT,
    workspace: Path | None = None,
) -> dict[str, Any]:
    return validate_manifest_text(text, mode=mode, workspace=workspace).raise_for_errors()


def load_app_manifest(
    workspace: Path,
    *,
    mode: ValidationMode | str = ValidationMode.DRAFT,
) -> tuple[dict[str, Any], str, str]:
    result, text, digest = validate_workspace_manifest(workspace, mode=mode)
    return result.raise_for_errors(), text, digest


def _validate_input_schema_files(manifest: dict[str, Any], workspace: Path) -> None:
    errors: list[str] = []
    warnings: list[str] = []
    raw_targets = manifest.get("targets") if isinstance(manifest, dict) else None
    for target_key, target in (raw_targets or {}).items():
        if not isinstance(target, dict):
            continue
        _check_input_schema(
            dict(target),
            f"targets.{target_key}",
            workspace=Path(workspace),
            errors=errors,
            warnings=warnings,
        )
    if errors:
        raise AppManifestError("; ".join(errors))


def validate_manifest_workspace(
    manifest: dict[str, Any],
    workspace: Path,
    *,
    mode: ValidationMode | str = ValidationMode.DRAFT,
) -> None:
    """Validate manifest references against the App workspace without writing it.

    既に :func:`parse_manifest_text` で形式検証済みの Manifest に対して、
    workspace 依存の検証だけを追加で行う。``mode`` を STRICT にすると
    entrypoint / build.output の実在も必須になる。
    """
    validate_manifest(manifest, mode=mode, workspace=Path(workspace)).raise_for_errors()


async def sync_manifest_targets(session: AsyncSession, app: App, workspace: Path) -> list[AppTarget]:
    """Synchronize derived app_targets from the workspace manifest."""
    # Target rows are also referenced by TaskAppLink and AppArtifact.  Keep
    # this operation serialized with release/job operations so a manifest
    # edit cannot delete a target between artifact validation and flush.
    from .app_operation_lock import app_operation_lock

    async with app_operation_lock(app.id, workspace_root=workspace.parent.parent):
        return await sync_manifest_targets_unlocked(session, app, workspace)


async def sync_manifest_targets_unlocked(
    session: AsyncSession,
    app: App,
    workspace: Path,
) -> list[AppTarget]:
    """Synchronize targets when the caller already owns the App lock.

    Target 同期は開発中の workspace でも走るため DRAFT で検証する。実在必須の
    判定は Build / Release 側が STRICT で行う。
    """
    if not callable(getattr(session, "add", None)):
        # A filesystem-only source import test may intentionally provide a
        # session stub without ORM mutation methods.  There are no derived DB
        # rows to synchronize in that mode.
        return []

    async def _scalars(statement):
        # Keep the service usable with the lightweight FakeSession used by
        # source-import tests while using AsyncSession.scalars in production.
        method = getattr(session, "scalars", None)
        if callable(method):
            return (await method(statement)).all()
        execute = getattr(session, "execute", None)
        if not callable(execute):
            return []
        return (await execute(statement)).scalars().all()

    async def _run() -> list[AppTarget]:
        manifest, _text, _hash = load_app_manifest(workspace)
        existing = {
            target.target_key: target
            for target in await _scalars(select(AppTarget).where(AppTarget.app_id == app.id))
        }
        stale_targets = list(
            target for target_key, target in existing.items()
            if target_key not in manifest["targets"]
        )
        stale_target_ids = {target.id for target in stale_targets}
        if stale_target_ids:
            artifact = await session.scalar(
                select(AppArtifact).where(AppArtifact.target_id.in_(stale_target_ids)).limit(1)
            )
            if artifact is not None:
                raise AppManifestError(
                    "Release Artifactが参照中のTargetは削除できません。"
                    "新しいTargetを追加するか、該当Releaseを先に整理してください。"
                )

            # ON DELETE SET NULL can collide with the partial unique key for
            # links without a target.  Collapse each affected logical link
            # before deleting its Target: keep an existing NULL link when it
            # exists, otherwise retain exactly one relation as NULL.
            stale_links = await _scalars(
                select(TaskAppLink)
                .where(
                    TaskAppLink.app_id == app.id,
                    TaskAppLink.target_id.in_(stale_target_ids),
                )
                .order_by(TaskAppLink.created_at, TaskAppLink.id)
            )
            groups = {(link.task_id, link.relation_type) for link in stale_links}
            for task_id, relation_type in groups:
                links = await _scalars(
                    select(TaskAppLink)
                    .where(
                        TaskAppLink.task_id == task_id,
                        TaskAppLink.app_id == app.id,
                        TaskAppLink.relation_type == relation_type,
                    )
                    .order_by(TaskAppLink.created_at, TaskAppLink.id)
                )
                stale_group = [link for link in links if link.target_id in stale_target_ids]
                null_link = next((link for link in links if link.target_id is None), None)
                if null_link is not None:
                    for link in stale_group:
                        await session.delete(link)
                    continue
                keep = stale_group[0]
                keep.target_id = None
                for link in stale_group[1:]:
                    await session.delete(link)

        targets: list[AppTarget] = []
        for target_key, snapshot in manifest["targets"].items():
            target = existing.pop(target_key, None)
            if target is None:
                target = AppTarget(app_id=app.id, target_key=target_key)
                session.add(target)
            target.display_name = snapshot.get("display_name") or target_key
            target.surface = snapshot["surface"]
            target.runtime = snapshot["runtime"]
            target.execution_host = snapshot["execution_host"]
            target.entrypoint = snapshot["entrypoint"]
            derived_snapshot = dict(snapshot)
            input_schema_ref = snapshot.get("input_schema")
            if isinstance(input_schema_ref, str):
                schema_path = workspace / input_schema_ref
                _validate_input_schema_files({"targets": {target_key: snapshot}}, workspace)
                schema_value = json.loads(schema_path.read_text(encoding="utf-8"))
                derived_snapshot["input_schema_path"] = input_schema_ref
                derived_snapshot["input_schema"] = schema_value
            target.manifest_snapshot = derived_snapshot
            targets.append(target)
        for stale in existing.values():
            await session.delete(stale)
        app.default_target_key = app.default_target_key if app.default_target_key in manifest["targets"] else next(iter(manifest["targets"]), None)
        await session.flush()
        return targets

    return await _run()


__all__ = [
    "APP_KNOWN_CAPABILITIES",
    "AppManifestError",
    "JSON_SCHEMA_TYPES",
    "MANIFEST_COMMAND_KEYS",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_TARGET_KEYS",
    "MANIFEST_TOP_LEVEL_KEYS",
    "ManifestValidationResult",
    "ValidationMode",
    "load_app_manifest",
    "parse_manifest_text",
    "sync_manifest_targets",
    "sync_manifest_targets_unlocked",
    "validate_manifest",
    "validate_manifest_text",
    "validate_manifest_workspace",
    "validate_app_manifest",
    "validate_workspace_manifest",
]
