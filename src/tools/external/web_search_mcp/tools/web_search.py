"""Web検索ツール（OpenAI Responses API Web Search使用）"""

from __future__ import annotations

import os
import time
import inspect
import warnings
from collections.abc import Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx

from .....llm.conversation_context import normalize_usage, persist_usage_sync
from .....services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    ExternalProviderBlocked,
    PrivacyError,
    get_privacy_policy_context,
)
from .....services.search_egress_policy import (
    SearchEgressPreconditionError,
    assert_openai_hosted_search_ready,
    configured_openai_api_key,
    search_egress_error_message,
)

_RECORDED_SEARCH_RESPONSES: list[object] = []


def _web_search_descriptor(model: str):
    try:
        from .....services.outbound_privacy_service import EgressDescriptor

        return EgressDescriptor(
            action="web_search",
            transport="openai.responses.create",
            destination="https://api.openai.com/v1/responses",
            provider="openai",
            tool="web_search",
            model=model,
        )
    except ImportError:  # pragma: no cover - old stripped embeds
        return SimpleNamespace(
            action="web_search",
            transport="openai.responses.create",
            destination="https://api.openai.com/v1/responses",
            provider="openai",
            tool="web_search",
            model=model,
        )


async def _execute_web_search(
    gateway: OutboundPrivacyGateway,
    payload: Mapping[str, object],
    *,
    model: str,
    sender,
) -> object:
    """Execute one Hosted Search request, with a bounded legacy bridge.

    Current gateways expose ``execute`` and own the complete review/send
    transaction.  Older embedders may provide only ``protect``; in that case
    we accept an explicit protected ``final_payload``/``payload`` and invoke
    the sender exactly once, never falling back to the raw request.
    """

    descriptor = _web_search_descriptor(model)
    execute = getattr(gateway, "execute", None)
    if callable(execute):
        return await execute(
            dict(payload),
            provider="openai",
            descriptor=descriptor,
            sender=sender,
            base_url="https://api.openai.com/v1",
            source_kind="web_search_mcp",
            model=model,
        )

    protect = getattr(gateway, "protect", None)
    if not callable(protect):
        raise PrivacyError("outbound privacy gateway does not support execution")
    warnings.warn(
        "protect-only MCP web-search gateways are deprecated; implement execute",
        DeprecationWarning,
        stacklevel=2,
    )
    # Keep the compatibility call's historical kwargs narrow.  Descriptor and
    # model binding are enforced by the production execute path; old
    # protectors cannot safely claim those transaction fields.
    protected = protect(
        dict(payload),
        provider="openai",
        source_kind="web_search_mcp",
    )
    if inspect.isawaitable(protected):
        protected = await protected
    marker = object()
    final_payload = marker
    if isinstance(protected, Mapping):
        final_payload = protected.get("final_payload", marker)
        if final_payload is marker:
            final_payload = protected.get("payload", marker)
    else:
        final_payload = getattr(protected, "final_payload", marker)
        if final_payload is marker:
            final_payload = getattr(protected, "payload", marker)
    if final_payload is marker or final_payload is None:
        raise PrivacyError("legacy privacy protector returned no explicit final payload")
    sent = sender(final_payload)
    if inspect.isawaitable(sent):
        return await sent
    return sent


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
        started = time.monotonic()
        client = None
        try:
            try:
                from .....config import Config

                config = Config()
            except Exception as exc:
                raise RuntimeError("Web検索のプライバシー設定を解決できません") from exc

            # Egress and credential checks are payload-independent.  Perform
            # them before constructing a privacy gateway or an OpenAI client
            # so an Enterprise deployment with no approved route cannot
            # expose the query to a sidecar/permission path first.
            try:
                assert_openai_hosted_search_ready(config)
            except SearchEgressPreconditionError as exc:
                return search_egress_error_message(exc)
            api_key = configured_openai_api_key(config)
            if not api_key:
                return "検索の前提条件を満たせません（credential_missing）。管理者に検索プロバイダの認証情報を設定してください。"

            requested_model = _resolve_openai_search_model(config)
            gateway = _web_privacy_gateway(config)

            from openai import AsyncOpenAI

            try:
                client = AsyncOpenAI(
                    api_key=api_key,
                    timeout=httpx.Timeout(8.0, connect=2.0),
                    max_retries=0,
                )
            except TypeError as exc:
                # Small OpenAI-compatible test/embedding adapters may expose
                # only ``api_key``.  Keep the production SDK's explicit
                # timeout/retry contract without hiding unrelated constructor
                # errors from those adapters.
                if "unexpected keyword argument" not in str(exc):
                    raise
                client = AsyncOpenAI(api_key=api_key)
            request_input = (
                "あなたはWeb検索アシスタントです。"
                "与えられたクエリについて最新の情報を検索し、"
                "簡潔で正確な回答を日本語で提供してください。\n\n"
                f"検索クエリ: {query}"
            )
            expected_tools = [{"type": "web_search_preview"}]
            request_payload = {
                "model": requested_model,
                "tools": expected_tools,
                "input": request_input,
            }

            async def send(protected_payload):
                if not isinstance(protected_payload, Mapping):
                    raise PrivacyError(
                        "privacy protection returned no protected payload"
                    )

                # The execute path reviews the complete provider wire.  Fixed
                # model/tool fields are route-bound and the final mapping is
                # forwarded unchanged, preserving the exact editor payload.
                if set(protected_payload) == {"model", "tools", "input"}:
                    final_model = protected_payload.get("model")
                    final_tools = protected_payload.get("tools")
                    final_input = protected_payload.get("input")
                    if final_model != requested_model:
                        raise PrivacyError("web search model binding changed")
                    if final_tools != expected_tools:
                        raise PrivacyError("web search tool binding changed")
                    if not isinstance(final_input, str) or not final_input.strip():
                        raise PrivacyError(
                            "privacy protection returned no protected input"
                        )
                    return await client.responses.create(**dict(protected_payload))

                # Compatibility-only branch for old protect-only embedding
                # gateways.  Production always uses the full-wire branch.
                if set(protected_payload) != {"input"}:
                    raise PrivacyError("web search outbound payload is malformed")
                protected_input = protected_payload.get("input")
                if not isinstance(protected_input, str) or not protected_input.strip():
                    raise PrivacyError(
                        "privacy protection returned no protected input"
                    )
                return await client.responses.create(
                    model=requested_model,
                    tools=expected_tools,
                    input=protected_input,
                )

            response = await _execute_web_search(
                gateway,
                request_payload,
                model=requested_model,
                sender=send,
            )
            _record_search_usage(response, model=requested_model, started=started)

            if response and hasattr(response, 'output_text'):
                return str(gateway.restore_aliases(response.output_text))
            elif response:
                return str(response)
            else:
                return "検索結果を取得できませんでした。"

        except SearchEgressPreconditionError as exc:
            return search_egress_error_message(exc)
        except (ExternalProviderBlocked, PrivacyError):
            return "検索はプライバシー保護に失敗したため停止しました（privacy_protection_failed）。"
        except (httpx.TimeoutException, TimeoutError):
            return "検索エグレスに到達できませんでした（egress_unreachable）。ネットワーク設定を確認してください。"
        except RuntimeError as exc:
            # Preserve the existing, sanitized configuration diagnostics for
            # model/config resolution while suppressing arbitrary provider
            # exception text.
            message = str(exc)
            if message in {
                "Web検索モデルが設定されていません",
                "Web検索モデル設定を解決できません",
                "Web検索のプライバシー設定を解決できません",
            }:
                return message
            return "Web検索に失敗しました。設定と承認済みエグレスを確認してください。"
        except Exception:
            # Provider exception text may contain URLs, headers, or other
            # sensitive deployment details.  Keep the MCP surface sanitized.
            return "Web検索に失敗しました。設定と承認済みエグレスを確認してください。"
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
