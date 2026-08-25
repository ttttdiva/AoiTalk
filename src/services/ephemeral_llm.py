"""履歴やツールを変更しない一時的なLLMテキスト生成。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_EPHEMERAL_SYSTEM_PROMPT = (
    "You are a concise assistant. Follow the instruction exactly. "
    "Do not call tools or modify files. Return only the requested text."
)

_EPHEMERAL_METHOD_NAMES = (
    "generate_plain_text_async",
    "generate_memory_extraction_async",
)


def _get_explicit_method(llm_client: Any, name: str) -> Any:
    """``__getattr__``だけが作る動的属性を対応APIとみなさない。"""
    try:
        inspect.getattr_static(llm_client, name)
    except AttributeError:
        return None
    method = getattr(llm_client, name, None)
    return method if callable(method) else None


def supports_ephemeral_text(llm_client: Any) -> bool:
    """履歴非保存のテキスト生成APIを持つか判定する。"""

    if llm_client is None:
        return False
    return any(
        _get_explicit_method(llm_client, name) is not None
        for name in _EPHEMERAL_METHOD_NAMES
    )


def _method_accepts_keyword(method: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == name
        for parameter in parameters
    )


async def _invoke_ephemeral_method(
    method: Any,
    prompt: str,
    *,
    system_prompt: str,
) -> Optional[str]:
    kwargs = {}
    if _method_accepts_keyword(method, "system_prompt"):
        kwargs["system_prompt"] = system_prompt

    response = method(prompt, **kwargs)
    if inspect.isawaitable(response):
        response = await response
    return str(response) if response else None


async def generate_ephemeral_text_with_llm_client(
    llm_client: Any,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    timeout_seconds: Optional[float] = 20.0,
) -> Optional[str]:
    """生成するが、通常の会話履歴へ入力・出力を保存しない。

    通常の ``generate_async`` / ``generate_response_async`` は履歴保存を
    伴うため、ここでは明示的に副作用なしと契約したメソッドだけを呼ぶ。
    対応APIがない場合も、通常生成へフォールバックせず ``None`` を返す。
    """

    if llm_client is None:
        return None

    effective_system_prompt = system_prompt or DEFAULT_EPHEMERAL_SYSTEM_PROMPT

    async def _generate() -> Optional[str]:
        for method_name in _EPHEMERAL_METHOD_NAMES:
            method = _get_explicit_method(llm_client, method_name)
            if method is None:
                continue
            try:
                return await _invoke_ephemeral_method(
                    method,
                    prompt,
                    system_prompt=effective_system_prompt,
                )
            except Exception as exc:
                logger.warning(
                    "履歴非保存LLM呼び出しに失敗しました (%s): %s",
                    method_name,
                    exc,
                )
                return None
        logger.warning(
            "LLMクライアントが履歴非保存テキスト生成APIに対応していません: %s",
            type(llm_client).__name__,
        )
        return None

    try:
        if timeout_seconds is None:
            return await _generate()
        return await asyncio.wait_for(_generate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "履歴非保存LLM呼び出しが %.1f 秒でタイムアウトしました",
            timeout_seconds,
        )
    except Exception as exc:
        logger.warning("履歴非保存LLM呼び出しに失敗しました: %s", exc)
    return None
