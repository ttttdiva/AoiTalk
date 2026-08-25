
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import google.generativeai as genai

from .core import tool
from ..services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    get_privacy_policy_context,
)

logger = logging.getLogger(__name__)

_RECORDED_USAGE_RESPONSES: list[Any] = []


def persist_usage_sync(*args: Any, **kwargs: Any) -> bool:
    """Lazy import to keep ``src.tools`` optional-import safe."""
    from ..llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))


def _usage_client() -> Any:
    """Resolve task-local user/session context for a tool invocation."""
    try:
        from types import SimpleNamespace

        from ..services.turn_context import get_turn_context

        turn = get_turn_context()
        return SimpleNamespace(
            current_session_id=turn.session_id,
            current_project_id=turn.project_id,
            character_name=None,
            _get_session_user_id=lambda: turn.user_id,
        )
    except Exception:
        return None


def _field(value: Any, name: str) -> Any:
    result = getattr(value, name, None)
    if result is None and isinstance(value, dict):
        result = value.get(name)
    return result


def _gemini_usage_payload(response: Any) -> Optional[Dict[str, Any]]:
    """Return reported Gemini image-generation token usage, if available."""
    usage = _field(response, "usage_metadata")
    if usage is None:
        return None

    def count(name: str) -> Optional[int]:
        raw = _field(usage, name)
        if raw is None:
            return None
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return None

    input_tokens = count("prompt_token_count")
    output_tokens = count("candidates_token_count")
    # A successful image response without token counts must remain unmetered;
    # token estimates would make the dashboard misleading.
    if input_tokens is None and output_tokens is None:
        return None

    cached_tokens = count("cached_content_token_count") or 0
    payload: Dict[str, Any] = {
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "cached_tokens": cached_tokens,
        "cache_read_tokens": cached_tokens,
        "reasoning_tokens": count("thoughts_token_count") or 0,
        "cache_provider": "gemini",
        "metrics_source": "gemini.usage_metadata",
    }
    resolved_model = _field(response, "model_version")
    if resolved_model:
        payload["resolved_model"] = str(resolved_model)
    return payload


def _mark_usage_recorded(response: Any) -> bool:
    """Prevent duplicate rows when a tool wrapper retries the same response."""
    try:
        if getattr(response, "_aoitalk_usage_recorded", False):
            return True
        setattr(response, "_aoitalk_usage_recorded", True)
        return False
    except Exception:
        if any(item is response for item in _RECORDED_USAGE_RESPONSES):
            return True
        _RECORDED_USAGE_RESPONSES.append(response)
        del _RECORDED_USAGE_RESPONSES[:-8]
        return False


def _record_image_usage(response: Any, model_name: str, latency_ms: int) -> bool:
    """Persist one successful Gemini image response when usage is reported."""
    payload = _gemini_usage_payload(response)
    if not payload:
        logger.info(
            "[ImageGeneration] Gemini image response has no usage_metadata; "
            "token usage is left unmetered rather than estimated"
        )
        return False
    if _mark_usage_recorded(response):
        return False
    try:
        persist_usage_sync(
            _usage_client(),
            provider="gemini",
            model=str(model_name),
            usage=payload,
            request_type="image",
            latency_ms=max(int(latency_ms or 0), 0),
        )
        return True
    except Exception:  # pragma: no cover - telemetry must not break generation
        logger.debug("[ImageGeneration] usage persistence failed", exc_info=True)
        return False

@tool
async def generate_image(prompt: str) -> str:
    """Generate an image based on the prompt using Gemini 3 Pro.
    
    Args:
        prompt: Description of the image to generate. Please be descriptive.
        
    Returns:
        String containing the path to the generated image in a special tag format [GENERATED_IMAGE:<path>]
    """
    started = time.monotonic()
    try:
        # Prompt text may contain secrets; keep logs metadata-only and redact
        # only at the provider boundary below.
        print(f"[ImageGeneration] Compiling prompt: chars={len(str(prompt or ''))}")
        
        # Ensure API key is set
        api_key = os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY')
        if not api_key:
            return "エラー: Google APIキーが設定されていません。"
            
        genai.configure(api_key=api_key)

        try:
            from ..config import Config

            privacy_config = Config()
        except Exception as exc:
            # Do not let a configuration-store failure silently select the
            # gateway's historical ``direct`` default for an external image
            # request.  The tool remains available once policy can be loaded.
            logger.warning("[ImageGeneration] privacy configuration unavailable: %s", exc)
            return "エラー: プライバシーポリシーを解決できないため画像生成を停止しました。"

        try:
            from ..services.turn_context import get_turn_context

            turn = get_turn_context()
            session_id = turn.session_id
            user_id = turn.user_id
        except Exception:
            session_id = None
            user_id = None
        inherited = get_privacy_policy_context()
        privacy_gateway = OutboundPrivacyGateway(
            privacy_config,
            session_id=str(session_id or ""),
            user_id=str(user_id or ""),
            session_context=inherited.session_context,
            project_metadata=inherited.project_metadata,
        )
        protected_prompt = privacy_gateway.protect_sync(
            {"prompt": prompt},
            provider="gemini",
            source_kind="image_generation",
        )
        if isinstance(protected_prompt.payload, dict):
            prompt = str(protected_prompt.payload.get("prompt") or prompt)
        
        # Initialize model
        # Using the specific model version requested by user
        model_name = "gemini-3-pro-image-preview" 
        try:
            model = genai.GenerativeModel(model_name)
        except Exception as e:
            return f"エラー: モデル {model_name} の初期化に失敗しました: {e}"
            
        # Generate content
        print(f"[ImageGeneration] Generating image with model {model_name}...")
        
        # Run blocking generation in a thread to avoid blocking the event loop
        import asyncio
        import functools
        
        # Check if there is a running loop
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, 
                functools.partial(model.generate_content, prompt)
            )
        except RuntimeError:
            # Fallback for sync execution (e.g. in tests)
            response = model.generate_content(prompt)

        
        # Check if generation was successful
        parts = getattr(response, "parts", None)
        if not parts:
            return "エラー: 画像生成に失敗しました（レスポンスが空です）。"
            
        # Find image part
        image_part = None
        for part in parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                image_part = part
                break
                
        if not image_part:
            return "エラー: 生成されたレスポンスに画像データが含まれていませんでした。"

        # Only successful image responses are recorded.  The provider may omit
        # usage metadata for image generation; in that case we intentionally do
        # not infer tokens from prompt length or image bytes.
        _record_image_usage(
            response,
            model_name=model_name,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
            
        # Save image
        # Create temp directory if it doesn't exist
        output_dir = Path("temp/generated_images")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(time.time())
        filename = f"gen_{timestamp}.jpg" # Defaulting to jpg, though mime_type might vary
        
        # Check mime type to be sure
        mime_type = image_part.inline_data.mime_type
        if "png" in mime_type:
            filename = f"gen_{timestamp}.png"
            
        output_path = output_dir / filename
        
        with open(output_path, "wb") as f:
            f.write(image_part.inline_data.data)
            
        abs_path = output_path.resolve()
        print(f"[ImageGeneration] Image saved to {abs_path}")
        
        # Return special tag for Discord bot to pick up
        return f"[GENERATED_IMAGE:{str(abs_path)}]"
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"エラー: 画像生成中に問題が発生しました: {str(e)}"
