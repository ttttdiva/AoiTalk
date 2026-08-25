"""Web検索ツール（OpenAI Responses API Web Search使用）"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING

from .....llm.conversation_context import normalize_usage, persist_usage_sync
from .....services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    get_privacy_policy_context,
)

_RECORDED_SEARCH_RESPONSES: list[object] = []


def _web_privacy_gateway(config=None) -> OutboundPrivacyGateway:
    if config is None:
        try:
            from .....config import Config

            config = Config()
        except Exception as exc:
            raise RuntimeError("Web検索のプライバシー設定を解決できません") from exc
    try:
        from .....services.turn_context import get_turn_context

        turn = get_turn_context()
    except Exception:
        turn = None
    inherited = get_privacy_policy_context()
    return OutboundPrivacyGateway(
        config,
        user_id=str(getattr(turn, "user_id", None) or ""),
        session_id=str(getattr(turn, "session_id", None) or ""),
        session_context=inherited.session_context,
        project_metadata=inherited.project_metadata,
    )


def _resolve_openai_search_model(config) -> str:
    """Resolve the configured OpenAI search model through the shared search contract."""

    try:
        from .....tools.basic.web_search import get_openai_search_model

        model = get_openai_search_model(config)
    except Exception as exc:
        raise RuntimeError("Web検索モデル設定を解決できません") from exc

    resolved = str(model or "").strip()
    if not resolved:
        raise RuntimeError("Web検索モデルが設定されていません")
    return resolved


def _usage_client(context=None):
    if context is not None and (
        hasattr(context, "current_session_id")
        or hasattr(context, "current_project_id")
        or callable(getattr(context, "_get_session_user_id", None))
    ):
        return context
    try:
        from .....services.turn_context import get_turn_context

        turn = get_turn_context()
    except Exception:
        turn = None

    def _value(name, default=None):
        if isinstance(context, Mapping):
            value = context.get(name)
            if value is not None:
                return value
        value = getattr(context, name, None)
        if value is not None:
            return value
        return getattr(turn, name, default) if turn is not None else default

    user_id = _value("user_id")
    return SimpleNamespace(
        current_session_id=_value("current_session_id", _value("session_id")),
        current_project_id=_value("current_project_id", _value("project_id")),
        character_name=_value("character_name"),
        _get_session_user_id=lambda: user_id,
    )


def _mark_response_recorded(response: object) -> bool:
    try:
        if getattr(response, "_aoitalk_usage_recorded", False):
            return True
        object.__setattr__(response, "_aoitalk_usage_recorded", True)
        return False
    except Exception:
        if any(item is response for item in _RECORDED_SEARCH_RESPONSES):
            return True
        _RECORDED_SEARCH_RESPONSES.append(response)
        del _RECORDED_SEARCH_RESPONSES[:-16]
        return False


def _record_search_usage(
    response,
    *,
    model: str,
    usage_context=None,
    started: float | None = None,
) -> bool:
    raw_usage = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
    if raw_usage is None:
        return False
    usage = normalize_usage(
        raw_usage,
        provider="openai",
        resolved_model=(
            response.get("model") if isinstance(response, Mapping) else getattr(response, "model", None)
        ),
    )
    if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
        return False
    if _mark_response_recorded(response):
        return False
    try:
        persist_usage_sync(
            _usage_client(usage_context),
            provider="openai",
            model=model,
            usage=usage,
            request_type="search",
            latency_ms=(
                max(0, int((time.monotonic() - started) * 1000))
                if started is not None
                else 0
            ),
            is_streaming=False,
        )
        return True
    except Exception:
        return False

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def persist_usage_sync(*args, **kwargs):
    """Lazy usage persistence keeps the MCP server startup lightweight."""

    from .....llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))


def register(mcp: FastMCP):
    """Web検索ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def web_search(query: str) -> str:
        """Web検索を実行します（OpenAI proxy実装）

        Args:
            query: 検索クエリ
        """
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            return "Web検索を使用するにはOPENAI_API_KEYが必要です。"

        started = time.monotonic()
        client = None
        try:
            try:
                from .....config import Config

                config = Config()
            except Exception as exc:
                raise RuntimeError("Web検索のプライバシー設定を解決できません") from exc

            requested_model = _resolve_openai_search_model(config)
            gateway = _web_privacy_gateway(config)

            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)
            request_input = (
                "あなたはWeb検索アシスタントです。"
                "与えられたクエリについて最新の情報を検索し、"
                "簡潔で正確な回答を日本語で提供してください。\n\n"
                f"検索クエリ: {query}"
            )
            protected = await gateway.protect(
                {"input": request_input},
                provider="openai",
                source_kind="web_search_mcp",
            )
            request_input = str(
                protected.payload.get("input", request_input)
                if isinstance(protected.payload, Mapping)
                else request_input
            )
            response = await client.responses.create(
                model=requested_model,
                tools=[{"type": "web_search_preview"}],
                input=request_input,
            )
            _record_search_usage(response, model=requested_model, started=started)

            if response and hasattr(response, 'output_text'):
                return str(gateway.restore_aliases(response.output_text))
            elif response:
                return str(response)
            else:
                return "検索結果を取得できませんでした。"

        except Exception as e:
            return f"Web検索エラー: {str(e)}"
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
