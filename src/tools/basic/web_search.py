"""
Web search tool for normal chat agents.

The backend can use either OpenAI's hosted web search or the local
lightweight search service, depending on `search.provider`.
"""
import os
import asyncio
import time
from collections.abc import Mapping
from types import SimpleNamespace
from ..core import tool

from ...llm.conversation_context import normalize_usage, persist_usage_sync

from ..external_llm_permission import check_permission_sync
from ...services.quick_search_service import (
    SEARCH_PROVIDER_LOCAL,
    get_search_provider,
    local_web_search,
)
from ...services.outbound_privacy_service import (
    OutboundPrivacyGateway,
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


def _run_async(coro_factory, timeout: int = 45):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(coro_factory()))
        return future.result(timeout=timeout)


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
    _preprotected_payload=None,
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
        # OpenAI APIキーを確認
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            return "Web検索を使用するにはOPENAI_API_KEYが必要です。"

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
                client = OpenAI(api_key=api_key)
                request_input = (
                    "あなたはWeb検索アシスタントです。"
                    "与えられたクエリについて最新の情報を検索し、"
                    "簡潔で正確な回答を日本語で提供してください。\n\n"
                    f"検索クエリ: {query}"
                )
                if _preprotected_payload is None:
                    gateway = _privacy_gateway or _privacy_gateway_for_config(
                        config, usage_context
                    )
                    protected = gateway.protect_sync(
                        {"input": request_input},
                        provider="openai",
                        source_kind="web_search",
                        model=requested_model,
                    )
                    _payload = protected.payload
                    active_gateway = gateway
                else:
                    _payload = _preprotected_payload
                request_input = str(
                    _payload.get("input", request_input)
                    if isinstance(_payload, Mapping)
                    else request_input
                )
                return client.responses.create(
                    model=requested_model,
                    tools=[{"type": "web_search_preview"}],
                    input=request_input,
                )

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
            error_msg = f"OpenAI Web検索エラー: {str(e)}"
            print(f"[Tool] web_search エラー: {error_msg}")
            return error_msg

    except Exception as e:
        error_msg = f"Web検索エラー: {str(e)}"
        print(f"[Tool] web_search エラー: {error_msg}")
        return error_msg


def local_web_search_impl(query: str, config=None) -> str:
    """AoiTalk側の汎用Web検索で検索します。"""
    print(f"[Tool] local web_search が呼び出されました: query_chars={len(str(query or ''))}")
    try:
        return local_web_search(query, config=config)
    except Exception as e:
        error_msg = f"汎用Web検索エラー: {str(e)}"
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
        )
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        print(f"[Tool] Yahoo X検索をスキップしました: {exc}")
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

    # Explicit X URLs and strong X-search wording are a hard route to the
    # canonical Yahoo backend.  This intentionally runs before provider
    # selection and before the OpenAI/local permission seams.  Only an empty
    # or failed Yahoo response reaches the existing provider fallback.
    if _is_x_search_route(query):
        yahoo_result = _try_yahoo_x_search(
            query,
            config=active_config,
            usage_context=usage_context,
        )
        if yahoo_result:
            return yahoo_result

    provider = get_search_provider(active_config)

    if provider == SEARCH_PROVIDER_LOCAL:
        return local_web_search_impl(query, active_config)

    try:
        requested_model = get_openai_search_model(active_config)
    except ValueError as exc:
        return f"検索は設定不備により停止しました: {exc}"

    # Redact/protect before opening the tool permission UI.  This keeps the
    # UI from displaying raw secrets while retaining the legacy confirmation
    # seam and preventing a second gateway review at transport time.
    request_input = (
        "あなたはWeb検索アシスタントです。"
        "与えられたクエリについて最新の情報を検索し、"
        "簡潔で正確な回答を日本語で提供してください。\n\n"
        f"検索クエリ: {query}"
    )
    try:
        gateway = _privacy_gateway_for_config(active_config)
        protected = gateway.protect_sync(
            {"input": request_input},
            provider="openai",
            source_kind="web_search",
            model=requested_model,
        )
        protected_payload = protected.payload
        protected_input = str(
            protected_payload.get("input", request_input)
            if isinstance(protected_payload, Mapping)
            else request_input
        )
    except Exception as exc:  # noqa: BLE001
        return f"検索はプライバシーポリシーにより停止しました: {exc}"

    # Keep the historical tool argument shape (the query only) but source its
    # value from the already-protected request body.
    permission_query = protected_input
    if "検索クエリ:" in permission_query:
        permission_query = permission_query.split("検索クエリ:", 1)[1].strip()
    approved = check_permission_sync(
        tool_name="web_search",
        tool_args={"query": permission_query},
        description=f"OpenAI APIによるWeb検索: 「{permission_query}」",
    )

    if not approved:
        return "ユーザーによって検索がキャンセルされました。"

    return openai_web_search_impl(
        permission_query,
        usage_context=usage_context or active_config,
        config=active_config,
        _preprotected_payload=protected_payload,
        _privacy_gateway=gateway,
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

