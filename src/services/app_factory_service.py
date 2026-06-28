"""Generate and serve downloadable instant app artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import shutil
import uuid
import zipfile
from typing import Any


CHAT_PROXY_ARTIFACT_PREFIX = "/api/python-proxy/api/app-factory/artifacts"
SUPPORTED_KINDS = {"local_web", "hosted_web", "bat_macro"}

_KIND_ALIASES = {
    "app": "local_web",
    "web": "local_web",
    "web_app": "local_web",
    "webui": "local_web",
    "webui_app": "local_web",
    "local_webui": "local_web",
    "local_webui_app": "local_web",
    "downloadable_web": "local_web",
    "aoitalk_web": "hosted_web",
    "aoitalk_webui": "hosted_web",
    "hosted": "hosted_web",
    "hosted_webui": "hosted_web",
    "macro": "bat_macro",
    "bat": "bat_macro",
    ".bat": "bat_macro",
    "batch": "bat_macro",
    "batch_macro": "bat_macro",
}

_RISKY_BATCH_PATTERNS = (
    "del ",
    "erase ",
    "rmdir ",
    "rd ",
    "format ",
    "shutdown ",
    "reg ",
    "powershell",
    "invoke-webrequest",
    "curl ",
    "bitsadmin",
    "certutil ",
)

MAX_EXTRA_FILE_COUNT = 80
MAX_EXTRA_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class AppFactoryArtifact:
    artifact_id: str
    kind: str
    title: str
    slug: str
    root_dir: Path
    package_dir: Path
    zip_path: Path
    zip_filename: str
    created_at: str
    files: list[str]
    warnings: list[str]

    @property
    def download_url(self) -> str:
        return f"{CHAT_PROXY_ARTIFACT_PREFIX}/{self.artifact_id}/download"

    @property
    def preview_url(self) -> str | None:
        if self.kind not in {"local_web", "hosted_web"}:
            return None
        return f"{CHAT_PROXY_ARTIFACT_PREFIX}/{self.artifact_id}/preview"

    def to_manifest(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "title": self.title,
            "slug": self.slug,
            "created_at": self.created_at,
            "files": self.files,
            "warnings": self.warnings,
            "download_filename": self.zip_filename,
            "download_url": self.download_url,
            "preview_url": self.preview_url,
            "runtime": _runtime_description(self.kind),
        }


def normalize_app_kind(kind: str) -> str:
    key = (kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _KIND_ALIASES.get(key, key)
    if normalized not in SUPPORTED_KINDS:
        allowed = ", ".join(sorted(SUPPORTED_KINDS))
        raise ValueError(f"Unsupported app kind '{kind}'. Use one of: {allowed}.")
    return normalized


def get_app_factory_root(config: Any | None = None) -> Path:
    configured = os.environ.get("AOITALK_APP_FACTORY_DIR") or _config_get(
        config,
        "app_factory.artifact_dir",
        "cache/app_factory",
    )
    root = Path(str(configured)).expanduser()
    if not root.is_absolute():
        root = _repo_root() / root
    return root.resolve()


def create_app_factory_artifact(
    *,
    kind: str,
    title: str,
    description: str = "",
    requirements: str = "",
    app_html: str = "",
    batch_script: str = "",
    extra_files: dict[str, str] | None = None,
    config: Any | None = None,
) -> AppFactoryArtifact:
    normalized_kind = normalize_app_kind(kind)
    clean_title = (title or "").strip() or "Instant App"
    slug = _slugify(clean_title)
    artifact_id = f"{slug}-{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(timezone.utc).isoformat()
    root_dir = get_app_factory_root(config) / artifact_id
    package_dir = root_dir / "package"
    zip_filename = f"{slug}.zip"
    zip_path = root_dir / zip_filename

    if root_dir.exists():
        shutil.rmtree(root_dir)
    package_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    if normalized_kind in {"local_web", "hosted_web"}:
        files = _build_web_package_files(
            kind=normalized_kind,
            title=clean_title,
            description=description,
            requirements=requirements,
            app_html=app_html,
        )
    else:
        warnings.extend(_batch_warnings(batch_script))
        files = _build_batch_package_files(
            title=clean_title,
            description=description,
            requirements=requirements,
            batch_script=batch_script,
        )

    if extra_files:
        files.update(_normalize_extra_files(extra_files))
        warnings.extend(_scan_package_warnings(files))

    artifact = AppFactoryArtifact(
        artifact_id=artifact_id,
        kind=normalized_kind,
        title=clean_title,
        slug=slug,
        root_dir=root_dir,
        package_dir=package_dir,
        zip_path=zip_path,
        zip_filename=zip_filename,
        created_at=created_at,
        files=sorted(list(files.keys()) + ["manifest.json"]),
        warnings=warnings,
    )
    manifest = artifact.to_manifest()
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    _write_package_files(package_dir, files)
    (root_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _zip_package(package_dir, zip_path)
    return artifact


def load_artifact_manifest(artifact_id: str, config: Any | None = None) -> dict[str, Any]:
    manifest_path = _artifact_dir(artifact_id, config) / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Artifact not found: {artifact_id}")
    return _manifest_with_runtime_status(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        manifest_path.parent,
    )


def resolve_artifact_download(
    artifact_id: str,
    config: Any | None = None,
) -> tuple[Path, str]:
    manifest = load_artifact_manifest(artifact_id, config)
    filename = str(manifest.get("download_filename") or f"{artifact_id}.zip")
    zip_path = _artifact_dir(artifact_id, config) / filename
    if not zip_path.exists() or not zip_path.is_file():
        raise FileNotFoundError(f"Artifact ZIP not found: {artifact_id}")
    return zip_path, filename


def resolve_artifact_preview(artifact_id: str, config: Any | None = None) -> Path:
    manifest = load_artifact_manifest(artifact_id, config)
    if manifest.get("kind") not in {"local_web", "hosted_web"}:
        raise FileNotFoundError(f"Artifact has no preview: {artifact_id}")
    preview = _artifact_dir(artifact_id, config) / "package" / "app" / "index.html"
    if not preview.exists() or not preview.is_file():
        raise FileNotFoundError(f"Preview not found: {artifact_id}")
    return preview


def _build_web_package_files(
    *,
    kind: str,
    title: str,
    description: str,
    requirements: str,
    app_html: str,
) -> dict[str, str]:
    index_html = app_html.strip() or _default_web_index_html(
        title=title,
        description=description,
        requirements=requirements,
    )
    return {
        "README.md": _web_readme(kind, title, description, requirements),
        "run.bat": _web_run_bat(),
        "app/index.html": index_html + ("\n" if not index_html.endswith("\n") else ""),
    }


def _build_batch_package_files(
    *,
    title: str,
    description: str,
    requirements: str,
    batch_script: str,
) -> dict[str, str]:
    script = batch_script.strip() or _default_batch_script(title)
    return {
        "README.md": _batch_readme(title, description, requirements),
        "run.bat": '@echo off\r\ncall "%~dp0scripts\\macro.bat" %*\r\n',
        "scripts/macro.bat": script + ("\r\n" if not script.endswith(("\n", "\r\n")) else ""),
        "input/README.txt": "Put input files for the macro in this folder.\r\n",
        "output/README.txt": "Macro output files can be written to this folder.\r\n",
    }


def _normalize_extra_files(extra_files: dict[str, str]) -> dict[str, str]:
    if len(extra_files) > MAX_EXTRA_FILE_COUNT:
        raise ValueError(f"Too many generated files; maximum is {MAX_EXTRA_FILE_COUNT}.")

    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_path, raw_content in extra_files.items():
        path = _normalize_package_path(raw_path)
        content = "" if raw_content is None else str(raw_content)
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > MAX_EXTRA_FILE_BYTES:
            raise ValueError(
                f"Generated files are too large; maximum is {MAX_EXTRA_FILE_BYTES} bytes."
            )
        normalized[path] = content
    return normalized


def _normalize_package_path(raw_path: str) -> str:
    value = str(raw_path or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    if not value or value.startswith("/") or ":" in value:
        raise ValueError(f"Unsafe package path: {raw_path}")
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError(f"Unsafe package path: {raw_path}")
    if len(parts) > 8:
        raise ValueError(f"Package path is too deep: {raw_path}")
    return "/".join(parts)


def _scan_package_warnings(files: dict[str, str]) -> list[str]:
    warnings: list[str] = []
    for path, content in files.items():
        suffix = Path(path).suffix.lower()
        if suffix not in {".bat", ".cmd", ".ps1"}:
            continue
        if suffix == ".ps1":
            warnings.append(f"{path} is a PowerShell script; review before running.")
        lower = f" {content.lower()} "
        for pattern in _RISKY_BATCH_PATTERNS:
            if pattern in lower:
                warnings.append(
                    f"{path} contains '{pattern.strip()}'; review before running."
                )
    return sorted(set(warnings))


def _default_web_index_html(*, title: str, description: str, requirements: str) -> str:
    safe_title = html.escape(title)
    safe_description = html.escape(description or "Instant WebUI app")
    safe_requirements = html.escape(requirements or "No detailed requirements were provided yet.")
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #5b6578;
      --line: #d8deea;
      --accent: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      width: min(960px, calc(100vw - 32px));
      margin: 32px auto;
    }}
    header {{
      margin-bottom: 18px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p {{ color: var(--muted); line-height: 1.7; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin: 14px 0;
    }}
    textarea {{
      width: 100%;
      min-height: 180px;
      resize: vertical;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: inherit;
    }}
    button {{
      margin-top: 10px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: white;
      padding: 10px 14px;
      font-weight: 600;
      cursor: pointer;
    }}
    code {{
      display: block;
      white-space: pre-wrap;
      background: #eef3f7;
      border-radius: 6px;
      padding: 12px;
      color: #1f2937;
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{safe_title}</h1>
      <p>{safe_description}</p>
    </header>
    <section>
      <h2>Requirements</h2>
      <code>{safe_requirements}</code>
    </section>
    <section>
      <h2>Workspace</h2>
      <textarea id="notes" placeholder="Use this area for temporary input or notes."></textarea>
      <button id="copy">Copy notes</button>
    </section>
  </main>
  <script>
    document.getElementById("copy").addEventListener("click", async () => {{
      const value = document.getElementById("notes").value;
      await navigator.clipboard.writeText(value);
    }});
  </script>
</body>
</html>"""


def _default_batch_script(title: str) -> str:
    safe_title = title.replace('"', "'")
    return f"""@echo off
setlocal
set "INPUT_DIR=%~dp0..\\input"
set "OUTPUT_DIR=%~dp0..\\output"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
echo {safe_title}
echo.
echo Input folder:  %INPUT_DIR%
echo Output folder: %OUTPUT_DIR%
echo.
echo Add macro steps to scripts\\macro.bat.
pause
endlocal"""


def _web_readme(kind: str, title: str, description: str, requirements: str) -> str:
    runtime = "AoiTalk static preview and local browser execution"
    if kind == "local_web":
        runtime = "Local browser execution"
    return f"""# {title}

{description or "Instant WebUI app package."}

## Runtime

{runtime}

## Run locally

Double-click `run.bat`. If Python is available, it starts a local HTTP server for
`app/index.html`. Otherwise it opens the HTML file directly in the browser.

## Requirements

{requirements or "No detailed requirements were provided yet."}

## Notes

This package was generated from an AoiTalk chat request. Review the files before
sharing outside your team.
"""


def _batch_readme(title: str, description: str, requirements: str) -> str:
    return f"""# {title}

{description or "Instant batch macro package."}

## Run

Review `scripts/macro.bat`, then double-click `run.bat`.

## Requirements

{requirements or "No detailed requirements were provided yet."}

## Safety

Batch files can modify local files. Review the script and test with sample input
before running it against important data.
"""


def _web_run_bat() -> str:
    return """@echo off
setlocal
cd /d "%~dp0app"
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  start "" http://127.0.0.1:8765
  python -m http.server 8765 --bind 127.0.0.1
) else (
  start "" "%CD%\\index.html"
)
endlocal
"""


def _runtime_description(kind: str) -> dict[str, Any]:
    if kind == "bat_macro":
        return {"type": "windows_batch", "entrypoint": "run.bat"}
    if kind == "hosted_web":
        return {
            "type": "static_web",
            "entrypoint": "app/index.html",
            "hosted_preview": True,
        }
    return {
        "type": "static_web",
        "entrypoint": "run.bat",
        "hosted_preview": True,
    }


def _write_package_files(package_dir: Path, files: dict[str, str]) -> None:
    base = package_dir.resolve()
    for relative_path, content in files.items():
        target = (base / relative_path).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"Unsafe package path: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")


def _zip_package(package_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package_dir).as_posix())


def _manifest_with_runtime_status(
    manifest: dict[str, Any],
    artifact_dir: Path,
) -> dict[str, Any]:
    filename = str(manifest.get("download_filename") or "")
    zip_path = artifact_dir / filename if filename else None
    preview_path = artifact_dir / "package" / "app" / "index.html"
    enriched = dict(manifest)
    enriched["download_available"] = bool(zip_path and zip_path.is_file())
    enriched["preview_available"] = bool(preview_path.is_file())
    if zip_path and zip_path.is_file():
        enriched["download_size_bytes"] = zip_path.stat().st_size
    return enriched


def _artifact_dir(artifact_id: str, config: Any | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", artifact_id or ""):
        raise FileNotFoundError(f"Artifact not found: {artifact_id}")
    root = get_app_factory_root(config)
    target = (root / artifact_id).resolve()
    if not target.is_relative_to(root):
        raise FileNotFoundError(f"Artifact not found: {artifact_id}")
    return target


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:48].strip("-") or "instant-app"


def _batch_warnings(script: str) -> list[str]:
    if not script:
        return []
    lower = f" {script.lower()} "
    warnings = []
    for pattern in _RISKY_BATCH_PATTERNS:
        if pattern in lower:
            warnings.append(
                f"Batch script contains '{pattern.strip()}'; review before running."
            )
    return warnings


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _config_get(config: Any | None, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    return default
