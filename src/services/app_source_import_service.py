"""Generate App metadata when an existing Project source is registered.

An imported folder is often an old macro rather than an already-structured App.
The App workspace still needs a valid Manifest and a useful README so the App
overview can explain what the source is for immediately after registration.
This module deliberately uses deterministic file-type inspection; it never
executes or interprets imported source code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from .app_storage import normalize_app_relative_path, resolve_workspace_file


_RUNTIME_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "aoitalk",
        "extensions": {".html", ".htm"},
        "display_name": "AoiTalk",
        "surface": "embedded_web",
        "runtime": "static_web",
        "execution_host": "aoitalk",
        "input_label": "入力データ",
        "input_detail": "App画面から入力します",
        "process_label": "AoiTalk画面",
        "process_detail": "ブラウザ内でAppを表示します",
        "output_label": "画面・成果物",
        "output_detail": "App画面で結果を確認します",
    },
    {
        "key": "office",
        "extensions": {".xlsm", ".xlam", ".xlsx", ".xls", ".bas", ".cls", ".frm", ".vba"},
        "display_name": "Excel / VBA",
        "surface": "office",
        "runtime": "vba",
        "execution_host": "office",
        "input_label": "Excel・VBAファイル",
        "input_detail": "取り込んだOfficeファイルを利用します",
        "process_label": "VBA処理",
        "process_detail": "Excel / Office環境で実行します",
        "output_label": "Excel成果物",
        "output_detail": "編集・変換したファイルを出力します",
    },
    {
        "key": "powershell",
        "extensions": {".ps1"},
        "display_name": "PowerShell",
        "surface": "headless",
        "runtime": "powershell",
        "execution_host": "client",
        "command": "powershell -ExecutionPolicy Bypass -File",
        "input_label": "入力ファイル",
        "input_detail": "スクリプトへ入力データを渡します",
        "process_label": "PowerShell処理",
        "process_detail": "PowerShellスクリプトを実行します",
        "output_label": "実行結果",
        "output_detail": "標準出力・生成ファイルを確認します",
    },
    {
        "key": "batch",
        "extensions": {".bat", ".cmd"},
        "display_name": "Windows Script",
        "surface": "headless",
        "runtime": "batch",
        "execution_host": "client",
        "command": "cmd /c",
        "input_label": "入力ファイル",
        "input_detail": "バッチへ入力データを渡します",
        "process_label": "Windows Script処理",
        "process_detail": "BAT / CMDを実行します",
        "output_label": "実行結果",
        "output_detail": "標準出力・生成ファイルを確認します",
    },
    {
        "key": "python",
        "extensions": {".py"},
        "display_name": "Python",
        "surface": "headless",
        "runtime": "python",
        "execution_host": "client",
        "command": "python",
        "input_label": "入力データ",
        "input_detail": "Pythonスクリプトへ入力データを渡します",
        "process_label": "Python処理",
        "process_detail": "Pythonスクリプトを実行します",
        "output_label": "成果物",
        "output_detail": "生成ファイル・標準出力を確認します",
    },
    {
        "key": "node",
        "extensions": {".js", ".mjs", ".cjs"},
        "display_name": "Node.js",
        "surface": "headless",
        "runtime": "node",
        "execution_host": "client",
        "command": "node",
        "input_label": "入力データ",
        "input_detail": "Node.jsスクリプトへ入力データを渡します",
        "process_label": "Node.js処理",
        "process_detail": "Node.jsスクリプトを実行します",
        "output_label": "成果物",
        "output_detail": "生成ファイル・標準出力を確認します",
    },
)


def _as_posix_paths(imported_files: Iterable[str]) -> list[str]:
    paths: list[str] = []
    for value in imported_files:
        try:
            normalized = normalize_app_relative_path(str(value))
        except ValueError:
            continue
        if normalized not in paths:
            paths.append(normalized)
    return sorted(paths, key=lambda value: value.lower())


def _preferred_entrypoint(paths: list[str]) -> str:
    preferred_names = ("main", "run", "server", "app", "index")
    for stem in preferred_names:
        for path in paths:
            if Path(path).stem.lower() == stem:
                return path
    return paths[0]


def _input_schema_path(workspace: Path, imported_files: list[str]) -> str | None:
    root = workspace.resolve()
    for path in imported_files:
        lowered = path.lower()
        if not lowered.startswith("schemas/") or not lowered.endswith(".json"):
            continue
        candidate = resolve_workspace_file(workspace, path)
        try:
            candidate.relative_to(root)
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return path
    return None


def _command_for(definition: dict[str, Any], entrypoint: str) -> str | None:
    prefix = definition.get("command")
    if not isinstance(prefix, str):
        return None
    # Quotes keep paths containing Japanese characters or spaces as one argv
    # item. The runner invokes argv directly and never uses a shell.
    return f'{prefix} "{entrypoint}"'


def _generated_targets(workspace: Path, imported_files: list[str]) -> list[dict[str, Any]]:
    targets: list[tuple[str, dict[str, Any]]] = []
    schema_path = _input_schema_path(workspace, imported_files)
    for definition in _RUNTIME_DEFINITIONS:
        matches = [
            path
            for path in imported_files
            if Path(path).suffix.lower() in definition["extensions"]
        ]
        if not matches:
            continue
        entrypoint = _preferred_entrypoint(matches)
        target: dict[str, Any] = {
            "display_name": definition["display_name"],
            "surface": definition["surface"],
            "runtime": definition["runtime"],
            "execution_host": definition["execution_host"],
            "entrypoint": entrypoint,
            "input_label": definition["input_label"],
            "input_detail": definition["input_detail"],
            "process_label": definition["process_label"],
            "process_detail": definition["process_detail"],
            "output_label": definition["output_label"],
            "output_detail": definition["output_detail"],
            "capabilities": [],
        }
        command = _command_for(definition, entrypoint)
        if command:
            target["run"] = {"command": command}
        if schema_path and definition["surface"] == "headless":
            target["input_schema"] = schema_path
        targets.append((definition["key"], target))

    if not targets:
        candidates = [
            path
            for path in imported_files
            if Path(path).name.lower() not in {"readme.md", "aoitalk.app.yaml", ".gitignore"}
            and Path(path).suffix.lower() not in {".json", ".yaml", ".yml"}
        ] or imported_files
        entrypoint = _preferred_entrypoint(candidates)
        targets.append(
            (
                "source",
                {
                    "display_name": "Source Bundle",
                    "surface": "headless",
                    "runtime": "executable",
                    "execution_host": "download_only",
                    "entrypoint": entrypoint,
                    "input_label": "入力ファイル",
                    "input_detail": "Source Bundleに含まれるファイルを利用します",
                    "process_label": "登録済みソース",
                    "process_detail": "必要な環境でソースを実行・編集します",
                    "output_label": "成果物",
                    "output_detail": "Source Bundleまたは生成ファイルを利用します",
                    "capabilities": [],
                },
            )
        )
    return [{"key": key, **target} for key, target in targets]


def _manifest_targets_from_file(workspace: Path) -> list[dict[str, Any]]:
    try:
        raw = yaml.safe_load(resolve_workspace_file(workspace, "aoitalk.app.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    targets = raw.get("targets") if isinstance(raw, dict) else None
    if not isinstance(targets, dict):
        return []
    return [
        {
            "key": str(key),
            "display_name": str(value.get("display_name") or key),
            "entrypoint": str(value.get("entrypoint") or ""),
            "input_label": str(value.get("input_label") or "入力データ"),
            "input_detail": str(value.get("input_detail") or "Targetへ入力します"),
            "process_label": str(value.get("process_label") or value.get("display_name") or key),
            "process_detail": str(value.get("process_detail") or "Targetの処理を実行します"),
            "output_label": str(value.get("output_label") or "成果物"),
            "output_detail": str(value.get("output_detail") or "処理結果を確認します"),
        }
        for key, value in targets.items()
        if isinstance(value, dict)
    ]


def _generated_readme(
    *,
    name: str,
    description: str,
    source_path: str,
    targets: list[dict[str, Any]],
) -> str:
    target_lines = "\n".join(
        f"- {target['display_name']}（{target['key']}）: `{target['entrypoint']}`"
        for target in targets
    )
    usage_lines = []
    for index, target in enumerate(targets, start=1):
        action = (
            "入力フォームから値を指定して実行します"
            if "input_schema" in target
            else "Targetの実行環境で実行するか、成果物をダウンロードします"
        )
        usage_lines.append(f"{index}. 「{target['display_name']}」Targetを選択し、{action}。")
    return f"""# {name}

## 目的
{description or "Project workspaceから取り込んだ既存スクリプトを、AoiTalkのAppとして管理します。"}

## 利用者
このProjectのメンバーと、Appに権限を付与された利用者。

## Target一覧
{target_lines}

## 入力
Targetごとの入力仕様を使用します。input_schemaがあるTargetは「使い方」から入力フォームを表示できます。

## 出力
実行結果、生成ファイル、またはRelease Artifact。

## 実行方法
{chr(10).join(usage_lines)}

## Projectとの関係
Project workspaceの `{source_path}` からApp workspaceへ登録時点のソースをコピーしました。以後の正本は `workspaces/_apps` です。

## 必要権限
閲覧はviewer、実行はrunner、ソース変更はdeveloper、Release作成はmaintainer以上が必要です。

## 開発方法
App詳細の「開発」「ファイル」「Docs」から、ソース、Manifest、READMEを管理します。

## Build手順
登録時点では既存ソースをそのまま保持しています。Build commandが必要な場合はManifestへTargetごとに追加します。

## Test手順
Test commandが必要な場合はManifestへTargetごとに追加します。

## 制約
秘密情報はManifest、README、Source Bundleへ保存しないでください。Project workspace外のパスは取り込めません。

## 既知の問題
Office / VBAの実行はAoiTalkサーバー上で自動化せず、成果物のダウンロードとローカルOffice利用を基本とします。

## リリース履歴
- App登録時にProject workspaceから初回ソースを取り込み。
"""


def generate_import_metadata(
    *,
    workspace: Path,
    app_name: str,
    description: str,
    source_path: str,
    imported_files: Iterable[str],
    replace_starter_metadata: bool = False,
    replace_starter_readme: bool | None = None,
) -> dict[str, Any]:
    """Create missing Manifest/README metadata and return the target summary.

    Existing root ``aoitalk.app.yaml`` and ``README.md`` files are treated as
    user-authored source of truth and are never overwritten, except when the
    caller explicitly marks the untouched starter metadata for replacement.
    """
    files = _as_posix_paths(imported_files)
    lower_files = {path.lower() for path in files}
    manifest_generated = "aoitalk.app.yaml" not in lower_files or replace_starter_metadata
    replace_readme = replace_starter_metadata if replace_starter_readme is None else replace_starter_readme
    readme_generated = "readme.md" not in lower_files or replace_readme

    if manifest_generated:
        targets = _generated_targets(workspace, files)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "name": app_name.strip() or "AoiTalk App",
            "description": description.strip() or "Project workspaceから取り込んだ既存ソースです。",
            "targets": {
                target["key"]: {key: value for key, value in target.items() if key != "key"}
                for target in targets
            },
        }
        resolve_workspace_file(workspace, "aoitalk.app.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
    else:
        targets = _manifest_targets_from_file(workspace)

    if readme_generated:
        resolve_workspace_file(workspace, "README.md").write_text(
            _generated_readme(
                name=app_name.strip() or "AoiTalk App",
                description=description.strip(),
                source_path=source_path,
                targets=targets,
            ),
            encoding="utf-8",
            newline="\n",
        )

    return {
        "manifest_generated": manifest_generated,
        "readme_generated": readme_generated,
        "targets": targets,
    }


__all__ = ["generate_import_metadata"]
