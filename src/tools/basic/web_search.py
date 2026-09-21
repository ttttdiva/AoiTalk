"""
Web search tool for normal chat agents.

The backend can use either OpenAI's hosted web search or the local
lightweight search service, depending on `search.provider`.
"""
import os
import asyncio
import contextvars
import inspect
import time
import warnings
from collections.abc import Mapping
from types import SimpleNamespace

import httpx
from ..core import tool

from ...llm.conversation_context import normalize_usage, persist_usage_sync

from ..external_llm_permission import check_permission_sync
from ...services.quick_search_service import (
    SEARCH_PROVIDER_LOCAL,
    SearchProviderConfigError,
    get_search_provider,
    local_web_search,
)
from ...services.deep_research_service import (
    DeepResearchProviderError,
    DeepResearchTransportError,
)
from ...services.search_egress_policy import (
    SearchEgressPreconditionError,
    assert_openai_hosted_search_ready,
    assert_public_search_egress_approved,
    is_enterprise_profile,
    search_egress_error_message,
)
from ...services.outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
from .x_search import (
    format_yahoo_x_results,
    is_x_url,
    looks_like_x_search_request,
    search_yahoo_realtime_sync,
    yahoo_result_has_results,
)


def persist_usage_sync(*args, **kwargs):
    """Lazy usage persistence keeps the search tool import-safe."""

    from ...llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))

_RECORDED_SEARCH_RESPONSES: list[object] = []
SEARCH_OPENAI_MODEL_KEY = "search.openai_model"


def _config_value(config, key: str, default=None):
    """Read a dotted config value from mappings and Config-like objects."""

    if config is None:
        return default
    if isinstance(config, Mapping):
        value = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        except Exception:
            value = default
        if value is not None:
            return value
    return default


def _enterprise_profile() -> bool:
    return is_enterprise_profile()


def _hosted_search_precondition(config) -> str | None:
    """Return a sanitized precondition error before any OpenAI transport."""

    try:
        assert_openai_hosted_search_ready(config)
    except SearchEgressPreconditionError as exc:
        return search_egress_error_message(exc)
    return None


def _configured_openai_api_key(config) -> str:
    value = _config_value(config, "search.openai_api_key", None)
    if value is None:
        value = _config_value(config, "openai_api_key", None)
    if value is None:
        value = os.getenv("AOITALK_SEARCH_OPENAI_API_KEY")
    if value is None:
        value = os.getenv("OPENAI_API_KEY")
    return str(value or "").strip()


def _sanitized_search_error(exc: Exception, *, hosted: bool = False) -> str:
    if isinstance(exc, SearchEgressPreconditionError):
        return search_egress_error_message(exc)
    if isinstance(exc, DeepResearchTransportError):
        if exc.code == "engine_timeout":
            return "検索エンジンが制限時間を超えました（engine_timeout）。ネットワーク設定を確認してください。"
        return "検索エグレスに到達できませんでした（egress_unreachable）。承認済みネットワーク経路を確認してください。"
    if isinstance(exc, DeepResearchProviderError):
        return "検索プロバイダが利用できませんでした（provider_failed）。設定とサービス状態を確認してください。"
    if isinstance(exc, ExternalProviderBlocked):
        return "検索はプライバシーポリシーにより停止しました（privacy_protection_failed）。"
    if isinstance(exc, PrivacyError):
        return "検索はプライバシー保護に失敗したため停止しました（privacy_protection_failed）。"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "検索エグレスに到達できませんでした（egress_unreachable）。ネットワーク設定を確認してください。"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "検索エグレスに到達できませんでした（egress_unreachable）。承認済みネットワーク経路を確認してください。"
    if isinstance(exc, httpx.HTTPStatusError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in {401, 403}:
            return "検索の認証情報が無効です（credential_invalid）。"
        return "検索エグレスが要求を拒否しました（egress_unreachable）。承認済みネットワーク経路を確認してください。"
    return "Hosted Web検索に失敗しました（hosted_search_failed）。設定と承認済みエグレスを確認してください。" if hosted else "Web検索に失敗しました。"


def get_openai_search_model(config) -> str:
    """Resolve the explicitly configured Hosted Web Search model.

    Hosted Search must not silently select a different model when the setting
    is absent or blank.  Callers should surface the resulting ``ValueError``
    instead of attempting an outbound request.
    """

    value = _config_value(config, SEARCH_OPENAI_MODEL_KEY)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{SEARCH_OPENAI_MODEL_KEY} must be configured as a non-empty string"
        )
    return value.strip()


# Keep the descriptive name available for callers/tests that used the initial
# implementation while the canonical public resolver remains explicit.
resolve_search_openai_model = get_openai_search_model


def _usage_client(context=None):
    """Return a usage context preserving the active turn identity."""

    if context is not None and (
        hasattr(context, "current_session_id")
        or hasattr(context, "current_project_id")
        or callable(getattr(context, "_get_session_user_id", None))
    ):
        return context
    try:
        from ...services.turn_context import get_turn_context

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
    model: str | None = None,
    usage_context=None,
    started: float | None = None,
) -> bool:
    raw_usage = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
    if raw_usage is None:
        return False
    response_model = (
        response.get("model") if isinstance(response, Mapping) else getattr(response, "model", None)
    )
    requested_model = str(model or "").strip() or str(response_model or "").strip()
    if not requested_model:
        return False
    usage = normalize_usage(
        raw_usage,
        provider="openai",
        resolved_model=response_model,
    )
    if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
        return False
    if _mark_response_recorded(response):
        return False
    try:
        persist_usage_sync(
            _usage_client(usage_context),
            provider="openai",
            model=requested_model,
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
        # Search output remains usable even when persistence is unavailable.
        return False


def _load_default_config():
    try:
        from ...config import Config

        return Config()
    except Exception:
        return None


def _privacy_gateway_for_config(config, usage_context=None) -> OutboundPrivacyGateway:
    """Resolve config and request-local identity/policy for hosted search."""

    try:
        from ...services.turn_context import get_turn_context

        turn = get_turn_context()
    except Exception:
        turn = None
    inherited = get_privacy_policy_context()
    def _value(*names):
        if isinstance(usage_context, Mapping):
            for name in names:
                value = usage_context.get(name)
                if value is not None:
                    return value
        for name in names:
            value = getattr(usage_context, name, None)
            if value is not None:
                return value
        return None

    user_id = _value("session_user_id", "user_id") or getattr(turn, "user_id", None) or ""
    session_id = (
        _value("current_session_id", "session_id")
        or getattr(turn, "session_id", None)
        or ""
    )
    return OutboundPrivacyGateway(
        config,
        user_id=str(user_id),
        session_id=str(session_id),
        session_context=inherited.session_context,
        project_metadata=inherited.project_metadata,
    )


def _web_search_egress_descriptor(model: str, *, destination: str = "https://api.openai.com/v1/responses"):
    """Build the canonical descriptor without making import-time assumptions.

    ``EgressDescriptor`` is supplied by the privacy boundary.  Keeping this
    tiny resolver lazy preserves importability for stripped deployments that
    do not ship the new boundary implementation yet; production always gets
    the real dataclass instance.
    """

    try:
        from ...services.outbound_privacy_service import EgressDescriptor

        return EgressDescriptor(
            action="web_search",
            transport="openai.responses.create",
            destination=destination,
            provider="openai",
            tool="web_search",
            model=str(model or ""),
        )
    except ImportError:  # pragma: no cover - compatibility with old embeds
        return SimpleNamespace(
            action="web_search",
            transport="openai.responses.create",
            destination=destination,
            provider="openai",
            tool="web_search",
            model=str(model or ""),
        )


def _execute_search_sync(
    gateway: OutboundPrivacyGateway,
    payload,
    *,
    model: str,
    sender,
    destination: str = "https://api.openai.com/v1/responses",
):
    """Cross the privacy boundary exactly once before Hosted Search transport.

    The production gateway path invokes ``execute_sync`` and lets that method
    perform masking, review, and the single sender call as one transaction.
    """

    descriptor = _web_search_egress_descriptor(model, destination=destination)
    execute = getattr(gateway, "execute_sync", None)
    if callable(execute):
        return execute(
            payload,
            provider="openai",
            descriptor=descriptor,
            sender=sender,
            base_url="https://api.openai.com/v1",
            source_kind="web_search",
            model=model,
        )

    # A few embedded/test gateways from before the transaction API only expose
    # ``protect_sync``.  Keep a deliberately narrow compatibility bridge while
    # deployments roll forward: the old protector must return an explicit
    # ``final_payload`` or ``payload`` value, and that value alone is passed to
    # the sender.  There is never a raw-payload fallback, and production's
    # OutboundPrivacyGateway always takes the execute path above.
    protect = getattr(gateway, "protect_sync", None)
    if not callable(protect):
        raise PrivacyError("outbound privacy gateway does not support execution")
    warnings.warn(
        "protect_sync-only outbound privacy gateways are deprecated; implement execute_sync",
        DeprecationWarning,
        stacklevel=2,
    )
    kwargs = {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "source_kind": "web_search",
        "model": model,
        "descriptor": descriptor,
    }
    try:
        protected = protect(payload, **kwargs)
    except TypeError as exc:
        # Preserve compatibility with an older positional-only protector, but
        # do not retry arbitrary provider TypeErrors that could duplicate work.
        if "unexpected keyword argument" not in str(exc):
            raise
        kwargs.pop("descriptor", None)
        protected = protect(payload, **kwargs)

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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(sent)
        # This helper is used from a synchronous adapter and normally runs on
        # a worker thread.  If an embed invokes it on an active loop, refuse to
        # nest the loop rather than risking an unbounded/raw send.
        raise PrivacyError("legacy outbound sender cannot await on an active loop")
    return sent


def _run_async(coro_factory, timeout: int = 45):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    import concurrent.futures

    # This helper is used from synchronous tool adapters that may themselves
    # run inside an active event loop.  Do not use a ``with`` block here:
    # ``ThreadPoolExecutor.__exit__`` waits for a timed-out coroutine, turning
    # the advertised fail-fast timeout into an unbounded request (and keeping
    # the raw query alive in the caller).  A cancelled future cannot stop a
    # coroutine already executing, but ``shutdown(wait=False)`` lets the tool
    # return its sanitized error promptly while the provider's own finite
    # HTTP timeout settles in the background.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    # Preserve request-local privacy and permission scopes when synchronous
    # adapters run a coroutine on a helper thread.
    context = contextvars.copy_context()
    future = executor.submit(context.run, lambda: asyncio.run(coro_factory()))
    try:
        return future.result(timeout=timeout)
    finally:
        if not future.done():
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)


def _response_citation_urls(response) -> list[str]:
    """Responses APIの注釈にある引用URLを表示用テキストへ戻す。"""
    urls: list[str] = []
    for output in getattr(response, "output", None) or []:
        for content in getattr(output, "content", None) or []:
            for annotation in getattr(content, "annotations", None) or []:
                if isinstance(annotation, dict):
                    url = annotation.get("url")
                else:
                    url = getattr(annotation, "url", None)
                normalized = str(url or "").strip()
                if normalized.startswith(("http://", "https://")) and normalized not in urls:
                    urls.append(normalized)
    return urls


def openai_web_search_impl(
    query: str,
    usage_context=None,
    config=None,
    *,
    _privacy_gateway: OutboundPrivacyGateway | None = None,
    _resolved_model: str | None = None,
) -> str:
    """OpenAI APIのHosted Web Searchで検索します。

    Args:
        query: 検索クエリ

    Returns:
        検索結果のサマリー
    """
    print(f"[Tool] web_search が呼び出されました: query_chars={len(str(query or ''))}")

    started = time.monotonic()
    active_gateway = _privacy_gateway
    try:
        # Enterprise Hosted Search is an explicitly approved capability.  Do
        # not construct a privacy gateway or probe/retry a route that lacks a
        # valid provider credential or approved egress.
        try:
            assert_openai_hosted_search_ready(config)
        except SearchEgressPreconditionError as exc:
            return search_egress_error_message(exc)
        # Personal callers retain the historical direct transport path, but a
        # zero-byte key is still rejected before opening the HTTP client.
        api_key = _configured_openai_api_key(config)
        if not api_key:
            return "検索の前提条件を満たせません（credential_missing）。管理者に検索プロバイダの認証情報を設定してください。"

        # OpenAI SDKのResponses APIでHosted Web Searchを実行
        try:
            requested_model = (
                _resolved_model
                if _resolved_model is not None
                else get_openai_search_model(config)
            )
            from openai import OpenAI

            async def run_search():
                nonlocal active_gateway
                try:
                    client = OpenAI(
                        api_key=api_key,
                        timeout=httpx.Timeout(8.0, connect=2.0),
                        max_retries=0,
                    )
                except TypeError as exc:
                    # Small test/dry-run adapters may expose only ``api_key``;
                    # the production SDK receives the explicit timeout/retry
                    # contract above.  Do not swallow unrelated constructor
                    # TypeErrors.
                    if "unexpected keyword argument" not in str(exc):
                        raise
                    client = OpenAI(api_key=api_key)
                try:
                    request_input = (
                        "あなたはWeb検索アシスタントです。"
                        "与えられたクエリについて最新の情報を検索し、"
                        "簡潔で正確な回答を日本語で提供してください。\n\n"
                        f"検索クエリ: {query}"
                    )
                    if _privacy_gateway is not None:
                        gateway = _privacy_gateway
                    else:
                        try:
                            gateway = _privacy_gateway_for_config(
                                config, usage_context
                            )
                        except TypeError as exc:
                            # Legacy embedders sometimes monkeypatch the
                            # resolver with its original one-argument shape.
                            # Retry only that signature mismatch; unrelated
                            # TypeErrors must remain fail-closed.
                            if "positional" not in str(exc) and "argument" not in str(exc):
                                raise
                            gateway = _privacy_gateway_for_config(config)
                    active_gateway = gateway

                    expected_tools = [{"type": "web_search_preview"}]
                    request_payload = {
                        "model": requested_model,
                        "tools": expected_tools,
                        "input": request_input,
                    }

                    def send(protected_payload):
                        """Send the exact payload approved by the gateway once."""

                        if not isinstance(protected_payload, Mapping):
                            raise PrivacyError(
                                "privacy protection returned no protected payload"
                            )

                        # The production transaction reviews the complete
                        # Responses request.  Validate fixed route fields and
                        # forward the reviewed mapping unchanged so the final
                        # editor value is the exact SDK payload.
                        if set(protected_payload) == {"model", "tools", "input"}:
                            final_model = protected_payload.get("model")
                            final_tools = protected_payload.get("tools")
                            final_input = protected_payload.get("input")
                            if final_model != requested_model:
                                raise PrivacyError("web search model binding changed")
                            if final_tools != expected_tools:
                                raise PrivacyError("web search tool binding changed")
                            if (
                                not isinstance(final_input, str)
                                or not final_input.strip()
                            ):
                                raise PrivacyError(
                                    "privacy protection returned no protected input"
                                )
                            return client.responses.create(**dict(protected_payload))

                        # Compatibility-only branch for old protect-only
                        # embedding adapters.  Production always takes the
                        # full-wire execute path above.
                        if set(protected_payload) != {"input"}:
                            raise PrivacyError("web search outbound payload is malformed")
                        protected_input = protected_payload.get("input")
                        if (
                            not isinstance(protected_input, str)
                            or not protected_input.strip()
                        ):
                            raise PrivacyError(
                                "privacy protection returned no protected input"
                            )
                        return client.responses.create(
                            model=requested_model,
                            tools=expected_tools,
                            input=protected_input,
                        )

                    # This is the sole privacy/transport boundary.  In
                    # particular, no pre-protected payload is carried into the
                    # function and no second review is attempted here.
                    return _execute_search_sync(
                        gateway,
                        request_payload,
                        model=requested_model,
                        sender=send,
                    )
                finally:
                    # The OpenAI client owns an httpx transport.  Close it at
                    # the request boundary when the SDK/test adapter exposes
                    # a close hook; no transport object is retained globally.
                    close = getattr(client, "close", None)
                    if callable(close):
                        try:
                            closed = close()
                            if inspect.isawaitable(closed):
                                await closed
                        except Exception:
                            pass

            response = _run_async(run_search, timeout=45)
            if response:
                _record_search_usage(
                    response,
                    model=requested_model,
                    usage_context=usage_context,
                    started=started,
                )

            if response and hasattr(response, 'output_text'):
                result = response.output_text
                if active_gateway is not None:
                    result = str(active_gateway.restore_aliases(result))
                citations = _response_citation_urls(response)
                if citations:
                    result = "\n".join(
                        [str(result).strip(), "", "参照URL:", *citations]
                    ).strip()
                print(f"[Tool] web_search 結果: {len(result)}文字")
                return result
            elif response:
                result = str(response)
                if active_gateway is not None:
                    result = str(active_gateway.restore_aliases(result))
                print(f"[Tool] web_search 結果: {len(result)}文字")
                return result
            else:
                return "検索結果を取得できませんでした。"

        except Exception as e:
            error_msg = _sanitized_search_error(e, hosted=True)
            print(f"[Tool] web_search エラー: {error_msg}")
            return error_msg

    except Exception as e:
        error_msg = _sanitized_search_error(e, hosted=True)
        print(f"[Tool] web_search エラー: {error_msg}")
        return error_msg


def local_web_search_impl(query: str, config=None) -> str:
    """AoiTalk側の汎用Web検索で検索します。"""
    print(f"[Tool] local web_search が呼び出されました: query_chars={len(str(query or ''))}")
    try:
        return local_web_search(query, config=config)
    except Exception as e:
        error_msg = _sanitized_search_error(e)
        print(f"[Tool] web_search エラー: {error_msg}")
        return error_msg


def _is_x_search_route(query: str) -> bool:
    """Recognize only explicit X URL/search intent for normal chat routing.

    The canonical Yahoo service owns the actual URL parser and intent
    vocabulary.  Keeping this tiny predicate here makes the hard-route easy to
    test and, importantly, avoids treating a bare ``X``/``DirectX`` mention as
    a social-search request.
    """

    value = str(query or "").strip()
    if not value:
        return False
    try:
        return bool(is_x_url(value) or looks_like_x_search_request(value))
    except Exception:
        # An optional Yahoo service must never make ordinary web search
        # unavailable.  Its absence simply disables the special route.
        return False


def _try_yahoo_x_search(
    query: str,
    *,
    max_results: int = 8,
    config=None,
    usage_context=None,
    privacy_gateway: OutboundPrivacyGateway | None = None,
) -> str | None:
    """Return a formatted Yahoo result, or ``None`` to activate fallback."""

    try:
        gateway = privacy_gateway
        if gateway is None and config is not None:
            gateway = (
                _privacy_gateway_for_config(config, usage_context)
                if usage_context is not None
                else _privacy_gateway_for_config(config)
            )
        result = search_yahoo_realtime_sync(
            query,
            max_results=max_results,
            timeout_seconds=45,
            privacy_gateway=gateway,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        # Personal compatibility keeps the historical fallback.  Enterprise
        # must not turn a Yahoo outage into an implicit second external
        # provider call (especially an approved Hosted OpenAI route).
        if _enterprise_profile():
            print("[Tool] Yahoo X検索を停止しました（egress_unreachable）")
            return "X検索（Yahooリアルタイム）に到達できませんでした（egress_unreachable）。承認済みネットワーク経路を確認してください。"
        print("[Tool] Yahoo X検索をスキップしました（provider_unreachable）")
        return None
    status = str(
        result.get("status", "")
        if isinstance(result, Mapping)
        else getattr(result, "status", "")
    ).strip().lower()
    if status in {"blocked", "privacy_blocked"}:
        if _enterprise_profile():
            return "X検索（Yahooリアルタイム）はプライバシーポリシーにより停止しました。"
        return None
    if status in {"timeout"}:
        if _enterprise_profile():
            return "X検索（Yahooリアルタイム）が制限時間を超えました（engine_timeout）。ネットワーク設定を確認してください。"
        return None
    if status in {
        "egress_unreachable",
        "network_error",
        "http_error",
        "redirect_rejected",
        "invalid_endpoint",
        "body_too_large",
        "parse_error",
    }:
        if _enterprise_profile():
            return "X検索（Yahooリアルタイム）に到達できませんでした（egress_unreachable）。承認済みネットワーク経路を確認してください。"
        return None
    if not yahoo_result_has_results(result):
        return None
    return format_yahoo_x_results(query, result, max_results=max_results)


def web_search_impl(query: str, config=None, *, usage_context=None) -> str:
    """設定された通常検索プロバイダでWeb検索を実行します。"""
    return web_search_with_config(
        query,
        config=config,
        usage_context=usage_context,
    )


def web_search_with_config(query: str, config=None, *, usage_context=None) -> str:
    """設定に応じてOpenAI Hosted Searchまたは汎用Web検索を実行します。"""
    # Loading configuration is local-only; the Yahoo request itself remains
    # ahead of provider selection and any legacy permission UI.
    active_config = config if config is not None else _load_default_config()
    if active_config is None:
        return "検索はプライバシー設定を解決できないため停止しました。"

    try:
        provider = get_search_provider(active_config)
    except SearchProviderConfigError:
        return "検索の前提条件を満たせません（provider_invalid）。検索プロバイダ設定を確認してください。"

    # Enterprise's explicit local provider is an operator-owned route.  Do
    # not let the X/Yahoo convenience shortcut silently turn it into a public
    # request; the local route must be resolved by the configured internal
    # engine instead.
    if provider == SEARCH_PROVIDER_LOCAL and _enterprise_profile():
        return local_web_search_impl(query, active_config)

    # Explicit X URLs and strong X-search wording are a hard route to the
    # canonical Yahoo backend.  Enterprise must pass the shared egress gate
    # before this route constructs a privacy gateway or starts Yahoo HTTP.
    # Personal deployments retain the historical Yahoo-first behaviour.
    if _is_x_search_route(query):
        if _enterprise_profile():
            try:
                endpoint = _config_value(
                    active_config,
                    "deep_research.yahoo_realtime_url",
                    _config_value(active_config, "search.yahoo_realtime_url", None),
                )
                assert_public_search_egress_approved(
                    active_config,
                    engine="yahoo_realtime",
                    endpoint=endpoint,
                )
            except SearchEgressPreconditionError as exc:
                return search_egress_error_message(exc)
        yahoo_result = _try_yahoo_x_search(
            query,
            config=active_config,
            usage_context=usage_context,
        )
        if yahoo_result:
            return yahoo_result

    if provider == SEARCH_PROVIDER_LOCAL:
        return local_web_search_impl(query, active_config)

    try:
        requested_model = get_openai_search_model(active_config)
    except ValueError as exc:
        return f"検索は設定不備により停止しました: {exc}"

    # Credential and approved-egress checks are payload-independent.  Perform
    # them before privacy review/permission UI so an unusable Hosted route
    # fails quickly without displaying or transporting a protected query.
    hosted_precondition = _hosted_search_precondition(active_config)
    if hosted_precondition:
        return hosted_precondition
    # The provider credential is a payload-independent prerequisite for both
    # Personal and Enterprise Hosted Search.  Check it before privacy
    # redaction, permission UI, and transport so a missing key cannot display
    # or retain a protected query and the tool fails fast consistently across
    # profiles.
    if not _configured_openai_api_key(active_config):
        return "検索の前提条件を満たせません（credential_missing）。管理者に検索プロバイダの認証情報を設定してください。"

    # Keep compatibility with pre-transaction embedders used by older
    # integrations/tests.  A current gateway is passed through untouched and
    # performs the sole masking/review transaction inside
    # ``openai_web_search_impl``.  A legacy protect-only gateway may provide a
    # display-safe permission query, but only after it returns an explicit
    # payload; malformed protectors fail closed before the permission UI.
    permission_query = query
    active_gateway = None
    try:
        try:
            active_gateway = _privacy_gateway_for_config(active_config, usage_context)
        except TypeError as exc:
            if "positional argument" not in str(exc) and "unexpected keyword argument" not in str(exc):
                raise
            active_gateway = _privacy_gateway_for_config(active_config)
        if not callable(getattr(active_gateway, "execute_sync", None)):
            protect = getattr(active_gateway, "protect_sync", None)
            if not callable(protect):
                raise PrivacyError("outbound privacy gateway does not support execution")
            preview = protect(
                {"input": (
                    "あなたはWeb検索アシスタントです。"
                    "与えられたクエリについて最新の情報を検索し、"
                    "簡潔で正確な回答を日本語で提供してください。\n\n"
                    f"検索クエリ: {query}"
                )},
                provider="openai",
                source_kind="web_search",
                model=requested_model,
            )
            preview_payload = getattr(preview, "payload", preview)
            if not isinstance(preview_payload, Mapping) or not isinstance(
                preview_payload.get("input"), str
            ) or not preview_payload["input"].strip():
                raise PrivacyError("privacy protection returned no protected input")
            permission_query = preview_payload["input"]
            if "検索クエリ:" in permission_query:
                permission_query = permission_query.split("検索クエリ:", 1)[1].strip()
        
    except Exception as exc:  # noqa: BLE001
        # Only legacy protect-only gateways use this preflight.  Current
        # gateways never see payload text until the permission check has
        # passed, preserving the canonical egress transaction ordering.
        if active_gateway is not None and not callable(
            getattr(active_gateway, "execute_sync", None)
        ):
            return _sanitized_search_error(exc, hosted=True)
        # A current gateway construction failure is also fail-closed, but do
        # not expose provider/credential details in the tool result.
        return _sanitized_search_error(exc, hosted=True)

    # The ordinary tool permission is a separate capability check.  It runs
    # before the privacy transaction, so cancellation never starts a sidecar,
    # review callback, or provider transport.  The gateway itself receives
    # the original request only inside ``openai_web_search_impl`` and decides
    # the final masked wire payload immediately before ``responses.create``.
    try:
        approved = check_permission_sync(
            tool_name="web_search",
            tool_args={"query": permission_query},
            description=f"OpenAI APIによるWeb検索: 「{permission_query}」",
        )
    except Exception as exc:
        return _sanitized_search_error(exc, hosted=True)

    if not approved:
        return "ユーザーによって検索がキャンセルされました。"

    return openai_web_search_impl(
        query,
        usage_context=usage_context or active_config,
        config=active_config,
        _privacy_gateway=active_gateway,
        _resolved_model=requested_model,
    )


def web_search_with_permission(query: str) -> str:
    """後方互換用: 設定された通常検索プロバイダで検索します。"""
    return web_search_with_config(query)


@tool
def web_search(query: str) -> str:
    """Web検索を実行します。

    Args:
        query: 検索クエリ

    Returns:
        検索結果のサマリー
    """
    return web_search_with_permission(query)

