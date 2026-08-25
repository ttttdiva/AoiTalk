"""Codex CLI based image generation helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .outbound_privacy_service import OutboundPrivacyGateway

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("temp/generated_images")
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_TIMEOUT_SECONDS = 900


class CodexImageGenerationError(Exception):
    """Raised when Codex CLI cannot produce a usable image file."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _safe_context(value: str, limit: int = 6000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...（省略）"


def _is_valid_image(path: Path) -> bool:
    if not path.exists() or not path.is_file() or path.stat().st_size < 512:
        return False
    head = path.read_bytes()[:16]
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"RIFF") and b"WEBP" in head[:16]
    )


def _extract_json(text: str) -> Dict[str, Any]:
    stripped = (text or "").strip()
    if not stripped:
        return {}
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def _build_prompt(
    *,
    output_path: Path,
    visual_request: str,
    scene_context: str,
    fixed_scene_tags: str,
    model: str,
    reasoning_effort: str,
) -> str:
    fixed_part = (
        f"\nFixed visual tags or scene image prompt:\n{fixed_scene_tags.strip()}\n"
        if fixed_scene_tags and fixed_scene_tags.strip()
        else ""
    )
    return f"""You are generating a TRPG scene image for AoiTalk.

Create exactly one AI-generated raster illustration and save it to this exact path:
{output_path}

Hard requirements:
- Use Codex CLI model {model} with reasoning effort {reasoning_effort}.
- Use an actual image generation capability if available.
- Save a PNG, JPEG, or WebP image file. PNG is preferred.
- Do not create SVG, HTML, text-only files, diagrams, placeholders, screenshots of text, or source-code artifacts.
- Do not modify any repository source files or configuration files.
- The image should depict the current in-session situation, not UI chrome.
- Favor cinematic TRPG scene composition, readable silhouettes, coherent lighting, and no embedded text.

Visual request:
{visual_request.strip() or "Generate the current TRPG scene."}
{fixed_part}
Current TRPG context:
{_safe_context(scene_context)}

After saving the image, reply with one JSON object only:
{{"image_path":"{output_path}","prompt":"<final visual prompt>","engine":"codex-cli","model":"{model}","reasoning_effort":"{reasoning_effort}"}}
"""


def _run_codex(
    *,
    prompt: str,
    output_path: Path,
    response_path: Path,
    model: str,
    reasoning_effort: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    bin_path = os.getenv("CODEX_BIN", "codex")
    resolved = shutil.which(bin_path) or bin_path
    root = _repo_root()
    cmd = [
        resolved,
        "exec",
        "--ephemeral",
        "--ignore-rules",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "-s",
        "workspace-write",
        "-C",
        str(root),
        "--output-last-message",
        str(response_path),
        "-",
    ]
    logger.info("Codex CLI image generation start: %s", output_path)
    return subprocess.run(
        cmd,
        input=prompt,
        cwd=str(root),
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        timeout=timeout_seconds,
    )


async def generate_codex_image(
    *,
    visual_request: str,
    scene_context: str,
    fixed_scene_tags: str = "",
    output_dir: Optional[Path] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    config: Any | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    session_context: Dict[str, Any] | None = None,
    project_metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Generate a TRPG image through Codex CLI and return metadata for logs."""

    # Codex CLI owns its own external model/workspace transport and cannot be
    # safely wrapped by the normal payload gateway.  It is therefore a direct
    # only route; protected/local_only policy must stop before creating files
    # or spawning the subprocess.  Gateway construction inherits the current
    # request context when callers omit explicit metadata.
    privacy_gateway = OutboundPrivacyGateway(
        config,
        session_id=session_id,
        user_id=user_id,
        session_context=session_context,
        project_metadata=project_metadata,
    )
    if privacy_gateway.mode != "direct":
        raise CodexImageGenerationError(
            "保護クラウド / ローカル限定モードではCodex CLI画像生成を使用できません。"
        )

    root = _repo_root()
    target_dir = (root / (output_dir or OUTPUT_DIR)).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"codex_trpg_{int(time.time())}_{random.randint(1000, 9999)}.png"
    output_path = target_dir / filename
    response_path = target_dir / f"{filename}.response.json"
    effective_model = (model or os.getenv("AOTALK_CODEX_IMAGE_MODEL") or DEFAULT_MODEL).strip()
    effective_effort = (
        reasoning_effort
        or os.getenv("AOTALK_CODEX_IMAGE_REASONING_EFFORT")
        or DEFAULT_REASONING_EFFORT
    ).strip()
    effective_timeout = int(
        timeout_seconds
        or os.getenv("AOTALK_CODEX_IMAGE_TIMEOUT_SECONDS")
        or DEFAULT_TIMEOUT_SECONDS
    )

    prompt = _build_prompt(
        output_path=output_path,
        visual_request=visual_request,
        scene_context=scene_context,
        fixed_scene_tags=fixed_scene_tags,
        model=effective_model,
        reasoning_effort=effective_effort,
    )

    try:
        result = await asyncio.to_thread(
            _run_codex,
            prompt=prompt,
            output_path=output_path,
            response_path=response_path,
            model=effective_model,
            reasoning_effort=effective_effort,
            timeout_seconds=effective_timeout,
        )
    except FileNotFoundError as exc:
        raise CodexImageGenerationError("Codex CLIが見つかりません") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodexImageGenerationError("Codex CLI画像生成がタイムアウトしました") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CodexImageGenerationError(
            f"Codex CLI画像生成に失敗しました: {detail[:600]}"
        )
    if not _is_valid_image(output_path):
        detail = ""
        if response_path.exists():
            detail = response_path.read_text(encoding="utf-8", errors="ignore").strip()
        raise CodexImageGenerationError(
            "Codex CLIは有効な画像ファイルを生成しませんでした"
            + (f": {detail[:400]}" if detail else "")
        )

    response_text = ""
    if response_path.exists():
        response_text = response_path.read_text(encoding="utf-8", errors="ignore")
    response = _extract_json(response_text)
    final_prompt = str(response.get("prompt") or visual_request or "").strip()

    logger.info("Codex CLI image generation complete: %s", output_path)
    return {
        "success": True,
        "image_path": str(output_path),
        "image_url": f"/api/generated-images/{filename}",
        "filename": filename,
        "prompt": final_prompt,
        "engine": "codex-cli",
        "model": effective_model,
        "reasoning_effort": effective_effort,
    }
