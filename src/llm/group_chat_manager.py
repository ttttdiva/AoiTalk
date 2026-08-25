"""グループチャット応答管理"""

import asyncio
import logging
import os
import time
from typing import List, Dict, Any, Optional

from ..config import Config
from .prompts import build_unified_instructions
from ..services.character_service import get_character_for_prompt, _run_sync
from ..services.turn_context import get_turn_context
from .conversation_context import normalize_usage, persist_usage_sync
from .openai_compatible_local_profiles import openai_compatible_local_base_url
from .sglang_url import resolve_sglang_base_url

logger = logging.getLogger(__name__)


class _GroupUsageClient:
    """Identity adapter for the shared synchronous usage persistence helper."""

    def __init__(
        self,
        *,
        user_id: str | None,
        session_id: str | None,
        project_id: str | None,
        agent_name: str | None,
    ) -> None:
        self._user_id = user_id
        self.current_session_id = session_id
        self.current_project_id = project_id
        self.character_name = agent_name

    def _get_session_user_id(self) -> str:
        return self._user_id or "default_user"


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        if key in config:
            return config.get(key, default)
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def _first_config_value(
    config: Any,
    keys: tuple[str, ...],
    *,
    env_keys: tuple[str, ...] = (),
    default: Any = None,
) -> Any:
    """Resolve a non-empty provider setting without exposing its value.

    ``Config.get`` already supports dotted paths, while test and legacy
    callers commonly pass a plain dictionary.  Keeping the lookup in this
    small helper makes the transport resolver below follow the same
    persisted-config/environment precedence used by the regular LLM factory.
    """

    for key in keys:
        value = _config_get(config, key)
        if value is not None and (
            not isinstance(value, str) or value.strip()
        ):
            return value
    for env_key in env_keys:
        value = os.getenv(env_key)
        if value is not None and (
            not isinstance(value, str) or value.strip()
        ):
            return value
    return default


def _ensure_v1_base_url(value: Any, default: str) -> str:
    clean = str(value or default).strip().rstrip("/")
    if not clean.endswith("/v1"):
        clean = f"{clean}/v1"
    return clean


def _provider_model(config: Any, provider: str, fallback: str) -> str:
    value = _first_config_value(
        config,
        (f"{provider}.model", f"{provider}_model"),
        default=fallback,
    )
    return str(value or fallback).strip() or fallback


def _normalized_model(value: Any) -> str:
    """Normalize Character/Main model strings before transport selection."""

    return str(value or "").strip()


def _resolve_native_transport(
    config: Any,
    provider: str,
    *,
    model: str,
    char_data: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve the exact OpenAI-compatible transport used by native runtime.

    ``run_native_agent_once`` defaults to the official OpenAI transport.  The
    group-chat path must therefore provide provider-specific credentials and
    endpoint explicitly; otherwise an OpenRouter/DeepSeek (or local) request
    would be sent to OpenAI while being billed as the configured provider.
    Returned credentials are consumed only by the SDK call and are never put
    in usage metadata or logs.
    """

    char_data = char_data or {}
    requested = str(provider or "openai").strip().lower() or "openai"
    # ``native_runtime`` implements chat-completions for these provider labels.
    # Unknown/CLI labels cannot be dispatched by this function, so use the
    # actual default OpenAI transport and persist that effective label instead
    # of claiming that a CLI/backend request was made.
    supported = {
        "openai",
        "gemini",
        "openrouter",
        "deepseek",
        "deepinfra",
        "kimi",
        "ollama",
        "sglang",
        "openai_compatible_local",
    }
    effective = requested if requested in supported else "openai"

    # Character-level endpoint/key overrides are accepted for deployments
    # that attach provider settings to the character record.  They are read
    # only for transport construction and never persisted.
    char_api_key = char_data.get("api_key")
    char_base_url = char_data.get("base_url")

    if effective == "openrouter":
        api_key = char_api_key or _first_config_value(
            config,
            ("openrouter_api_key",),
            env_keys=("OPENROUTER_API_KEY",),
        )
        base_url = char_base_url or _first_config_value(
            config,
            ("openrouter.base_url", "openrouter_base_url"),
            env_keys=("OPENROUTER_BASE_URL",),
            default="https://openrouter.ai/api/v1",
        )
        site_url = _first_config_value(
            config,
            ("openrouter.site_url", "openrouter_site_url"),
            env_keys=("OPENROUTER_SITE_URL", "OPENROUTER_HTTP_REFERER"),
        )
        app_name = _first_config_value(
            config,
            ("openrouter.app_name", "openrouter_app_name"),
            env_keys=("OPENROUTER_APP_NAME",),
            default="AoiTalk",
        )
        headers: dict[str, str] = {}
        if site_url:
            headers["HTTP-Referer"] = str(site_url)
        if app_name:
            headers["X-Title"] = str(app_name)
        return effective, {
            "api_key": api_key or "dummy",
            "base_url": str(base_url).rstrip("/"),
            "default_headers": headers or None,
        }

    if effective == "deepseek":
        base_url = char_base_url or _config_get(config, "deepseek_base_url")
        if not base_url:
            base_url = os.getenv("DEEPSEEK_BASE_URL")
        if not base_url:
            base_url = _config_get(config, "deepseek.base_url")
        return effective, {
            "api_key": char_api_key
            or _first_config_value(
                config,
                ("deepseek_api_key",),
                env_keys=("DEEPSEEK_API_KEY",),
                default="dummy",
            ),
            "base_url": str(base_url or "https://api.deepseek.com").rstrip("/"),
            "default_headers": None,
        }

    if effective == "deepinfra":
        return effective, {
            "api_key": char_api_key
            or _first_config_value(
                config,
                ("deepinfra_api_key",),
                env_keys=("DEEPINFRA_TOKEN",),
                default="dummy",
            ),
            "base_url": str(
                char_base_url
                or _first_config_value(
                    config,
                    ("deepinfra.base_url", "deepinfra_base_url"),
                    env_keys=("DEEPINFRA_BASE_URL",),
                    default="https://api.deepinfra.com/v1/openai",
                )
            ).rstrip("/"),
            "default_headers": None,
        }

    if effective == "kimi":
        base_url = char_base_url or _config_get(config, "kimi_base_url")
        if not base_url:
            base_url = os.getenv("MOONSHOT_BASE_URL")
        if not base_url:
            base_url = _config_get(config, "kimi.base_url")
        return effective, {
            "api_key": char_api_key
            or _first_config_value(
                config,
                ("kimi_api_key",),
                env_keys=("MOONSHOT_API_KEY",),
                default="dummy",
            ),
            "base_url": str(base_url or "https://api.moonshot.ai/v1").rstrip("/"),
            "default_headers": None,
        }

    if effective == "gemini":
        # Gemini's OpenAI-compatible endpoint keeps this native path on the
        # configured Gemini provider rather than silently falling back to
        # OPENAI_API_KEY/default OpenAI base URL.
        return effective, {
            "api_key": char_api_key
            or _first_config_value(
                config,
                ("gemini_api_key",),
                env_keys=("GEMINI_API_KEY",),
                default="dummy",
            ),
            "base_url": str(
                char_base_url
                or _first_config_value(
                    config,
                    ("gemini.base_url", "gemini_base_url"),
                    env_keys=("GEMINI_BASE_URL",),
                    default="https://generativelanguage.googleapis.com/v1beta/openai",
                )
            ).rstrip("/"),
            "default_headers": None,
        }

    if effective == "ollama":
        base_url = char_base_url or _config_get(config, "runtime.target_base_url")
        if not base_url:
            base_url = os.getenv("OLLAMA_BASE_URL")
        if not base_url:
            base_url = _config_get(config, "ollama_base_url")
        if not base_url:
            base_url = _config_get(config, "ollama.base_url")
        api_key = char_api_key or _config_get(config, "runtime.target_api_key")
        if not api_key:
            api_key = os.getenv("OLLAMA_API_KEY")
        if not api_key:
            api_key = _config_get(config, "ollama_api_key")
        if not api_key:
            api_key = _config_get(config, "ollama.api_key")
        return effective, {
            "api_key": api_key or "ollama",
            "base_url": _ensure_v1_base_url(
                base_url, "http://127.0.0.1:11434/v1"
            ),
            "default_headers": None,
        }

    if effective == "sglang":
        base_url = char_base_url or _first_config_value(
            config,
            ("runtime.target_base_url", "sglang_base_url", "sglang.base_url"),
            env_keys=("SGLANG_BASE_URL",),
        )
        return effective, {
            "api_key": char_api_key
            or _first_config_value(
                config,
                ("runtime.target_api_key", "sglang_api_key"),
                env_keys=("SGLANG_API_KEY",),
                default="dummy",
            ),
            "base_url": (
                _ensure_v1_base_url(base_url, "http://127.0.0.1:30000/v1")
                if base_url
                else resolve_sglang_base_url(config)
            ),
            "default_headers": None,
        }

    if effective == "openai_compatible_local":
        base_url = char_base_url or _config_get(config, "runtime.target_base_url")
        if not base_url:
            base_url = openai_compatible_local_base_url(config, model=model)
        return effective, {
            "api_key": char_api_key
            or _config_get(config, "runtime.target_api_key")
            or os.getenv("OPENAI_COMPATIBLE_LOCAL_API_KEY")
            or _config_get(config, "openai_compatible_local.api_key")
            or "dummy",
            "base_url": _ensure_v1_base_url(
                base_url, "http://127.0.0.1:8080/v1"
            ),
            "default_headers": None,
        }

    # Official OpenAI.  The SDK itself also understands OPENAI_API_KEY, but
    # passing the resolved value avoids accidentally using a different
    # provider key when a custom base URL is configured.
    return effective, {
        "api_key": char_api_key
        or _first_config_value(
            config,
            ("openai_api_key",),
            env_keys=("OPENAI_API_KEY",),
        ),
        "base_url": char_base_url
        or _first_config_value(
            config,
            ("openai.base_url", "openai_base_url"),
            env_keys=("OPENAI_BASE_URL",),
        ),
        "default_headers": None,
    }


class GroupChatManager:
    """複数キャラクターの同時会話を管理する"""

    def __init__(
        self,
        config: Config,
        character_slugs: List[str],
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ):
        self.config = config
        self.character_slugs = character_slugs
        self._character_cache = {}
        # API callers predating explicit identity arguments continue to work;
        # request-scoped turn context is used as the fallback below.
        self.user_id = user_id
        self.session_user_id = user_id
        self.session_id = session_id
        self.project_id = project_id
        self.current_session_id = session_id
        self.current_project_id = project_id
        # Additive per-character route evidence for diagnostics.  Group chat
        # keeps its existing TokenUsage rows; this compact projection is also
        # useful to callers that already collect runtime metadata.
        self.route_metadata: dict[str, Any] = {}

    def _usage_client(self, char_data: dict[str, Any]) -> _GroupUsageClient:
        turn_context = get_turn_context()
        user_id = (
            self.user_id
            or getattr(self, "session_user_id", None)
            or turn_context.user_id
            or _config_get(self.config, "user_id")
            or "default_user"
        )
        session_id = (
            self.current_session_id
            or getattr(self, "session_id", None)
            or turn_context.session_id
            or _config_get(self.config, "session_id")
        )
        project_id = (
            self.current_project_id
            or getattr(self, "project_id", None)
            or turn_context.project_id
            or _config_get(self.config, "project_id")
        )
        return _GroupUsageClient(
            user_id=str(user_id).strip() if user_id else None,
            session_id=str(session_id).strip() if session_id else None,
            project_id=str(project_id).strip() if project_id else None,
            agent_name=str(char_data.get("name") or char_data.get("slug") or "Character"),
        )

    def _persist_usage_records(
        self,
        char_data: dict[str, Any],
        usage_records: Any,
        *,
        provider: str,
        model: str,
        started_at: float,
    ) -> None:
        """Persist native runtime records one-for-one, without inventing usage."""

        for usage in usage_records or []:
            try:
                normalized = normalize_usage(
                    usage,
                    provider=provider,
                    resolved_model=(
                        usage.get("resolved_model")
                        if isinstance(usage, dict)
                        else getattr(usage, "resolved_model", None)
                    ),
                )
                if not normalized:
                    continue
                if (
                    normalized.get("input_tokens") is None
                    and normalized.get("output_tokens") is None
                ):
                    continue
                persist_usage_sync(
                    self._usage_client(char_data),
                    provider=provider,
                    model=model,
                    usage=normalized,
                    request_type="chat",
                    # NativeRunResult does not expose per-request timing; do
                    # not charge the whole multi-round elapsed time to every
                    # row.
                    latency_ms=0,
                    is_streaming=False,
                )
            except Exception:
                logger.debug("グループチャットusageの保存に失敗しました", exc_info=True)

    async def generate_responses(
        self,
        user_message: str,
        history: List[Dict[str, Any]],
        strategy: str = "round_robin",
    ) -> List[Dict[str, Any]]:
        """各キャラクターの応答を順番に生成する

        strategy: "round_robin" / "random"
        戻り値: [{"character_slug": "xxx", "character_name": "XXX", "content": "応答"}]
        """
        turn_order = self._get_turn_order(strategy)
        responses = []
        accumulated_context = list(history)

        for slug in turn_order:
            char_data = await self._get_character(slug)
            if not char_data:
                continue

            # このキャラ用のプロンプト構築
            prompt = self._build_character_prompt(
                char_data, accumulated_context, user_message, responses
            )

            # LLM呼び出し（native runtimeで直接API呼び出し）
            content = await self._call_llm(char_data, prompt)

            response = {
                "character_slug": slug,
                "character_name": char_data.get("name", slug),
                "content": content,
            }
            responses.append(response)
            # 次のキャラのコンテキストに含める
            accumulated_context.append(
                {
                    "role": "assistant",
                    "content": f"[{char_data.get('name', slug)}]: {content}",
                }
            )

        return responses

    def _get_turn_order(self, strategy: str) -> List[str]:
        if strategy == "random":
            import random

            order = list(self.character_slugs)
            random.shuffle(order)
            return order
        return list(self.character_slugs)  # round_robin

    async def _get_character(self, slug: str) -> Optional[Dict]:
        if slug not in self._character_cache:
            self._character_cache[slug] = await get_character_for_prompt(slug)
        return self._character_cache[slug]

    def _build_character_prompt(self, char_data, history, user_message, prev_responses):
        """グループチャット用のプロンプトを構築"""
        name = char_data.get("name", "")
        description = char_data.get("description", "")
        personality = char_data.get("personality_summary", "")
        scenario = char_data.get("scenario", "")
        example_messages = char_data.get("example_messages", "")
        system_prompt = char_data.get("system_prompt", "")

        sections = []
        sections.append(f"あなたは「{name}」として、グループチャットに参加しています。")
        sections.append(
            "他のキャラクターの発言も踏まえて、自分のキャラクターらしく応答してください。"
        )
        sections.append(
            "行動やナレーションは *アスタリスク* で囲み、台詞はそのまま記述してください。"
        )

        if description:
            sections.append(f"\n## キャラクター設定\n{description}")
        if personality:
            sections.append(f"\n## 性格\n{personality}")
        if scenario:
            sections.append(f"\n## シナリオ\n{scenario}")
        if example_messages:
            sections.append(f"\n## 会話例\n{example_messages}")
        if system_prompt:
            sections.append(f"\n## 追加指示\n{system_prompt}")

        # 他のキャラの直前の応答を含める
        if prev_responses:
            others = "\n".join(
                f"[{r['character_name']}]: {r['content']}" for r in prev_responses
            )
            sections.append(f"\n## 他のキャラクターの発言\n{others}")

        return "\n".join(sections)

    async def _call_llm(self, char_data, prompt):
        """LLM APIを直接呼び出して応答を生成"""
        try:
            from .native_runtime import (
                AgentDefinition,
                NativeModelSettings,
                Reasoning,
                run_native_agent_once,
            )
            from .deployment_resolver import (
                effective_config_overrides,
                preflight_deployment,
                resolve_llm_deployment,
            )

            persisted_provider = str(
                char_data.get("provider")
                or _config_get(self.config, "llm_provider", "openai")
                or "openai"
            ).strip().lower()
            # Keep non-OpenAI provider fallbacks unchanged, but never let a
            # missing OpenAI model setting select the retired mini model.
            provider_fallback_model = (
                "gpt-5.6-luna" if persisted_provider == "openai" else "gpt-4o-mini"
            )
            character_model = _normalized_model(char_data.get("model"))
            main_model = _normalized_model(_config_get(self.config, "llm_model"))
            provider_model = _provider_model(
                self.config,
                persisted_provider,
                provider_fallback_model,
            )
            persisted_model = character_model or main_model or provider_model
            provider = persisted_provider
            model = _normalized_model(persisted_model)
            route_source = (
                "explicit_character_model"
                if character_model
                else "main_inherit"
                if main_model
                else "provider_default"
            )
            runtime_config = self.config
            transport_char_data = dict(char_data or {})

            # Group chat historically called ``run_native_agent_once`` directly
            # and therefore bypassed the deployment resolver used by the normal
            # LLM factory.  A stale persisted SGLang selection could consequently
            # open an SGLang endpoint even when Enterprise was fixed to Gemma/vLLM
            # (or another local backend).  Resolve the deployment before any
            # transport is built; fixed deployments always use their effective
            # provider/model/endpoint and never trust character-level endpoint
            # data.  In a non-fixed external deployment an unavailable persisted
            # provider is treated the same way.  Explicit per-request target
            # markers are preflighted and fail closed.
            deployment = resolve_llm_deployment(self.config)
            if deployment is not None:
                char_provider = str(char_data.get("provider") or "").strip().lower()
                char_model = str(char_data.get("model") or "").strip()
                char_base_url = str(char_data.get("base_url") or "").strip()

                # Character rows are persisted state, not request-scoped
                # engine switches.  They may contain provider/model/base_url
                # fields from an older deployment and therefore must not be
                # preflighted as if they were explicit overrides: doing so
                # would reject a stale SGLang endpoint before the fixed Gemma
                # overlay has a chance to replace it.  A caller that really
                # supplies a per-request target can opt in with the private
                # marker below (the public group-chat API currently has no
                # engine override field); only that marker is preflighted.
                explicit_target = bool(
                    char_data.get("_deployment_target_explicit")
                    or char_data.get("deployment_target_explicit")
                )
                target_data = char_data.get("_deployment_target")
                if not isinstance(target_data, dict):
                    target_data = char_data
                if explicit_target:
                    preflight_deployment(
                        self.config,
                        provider=(
                            str(
                                target_data.get("provider")
                                or char_provider
                                or persisted_provider
                            )
                            .strip()
                            .lower()
                        ),
                        model=str(target_data.get("model") or char_model or model),
                        base_url=(
                            str(target_data.get("base_url") or char_base_url).strip()
                            or None
                        ),
                    )

                available, _ = deployment.provider_available(persisted_provider)
                if deployment.fixed or not available:
                    provider = deployment.effective_provider
                    model = deployment.effective_model
                    # Do not carry stale per-character credentials/endpoints into
                    # a fixed deployment.  ``runtime.target_*`` in the overlay
                    # supplies the effective endpoint and key instead.
                    transport_char_data.pop("provider", None)
                    transport_char_data.pop("model", None)
                    transport_char_data.pop("base_url", None)
                    transport_char_data.pop("api_key", None)
                    transport_char_data["base_url"] = deployment.effective_base_url

                overrides = effective_config_overrides(self.config)
                if overrides:
                    # TargetConfig is imported lazily to avoid making the group
                    # manager depend on the main factory during module import.
                    from .manager import TargetConfig

                    runtime_config = TargetConfig(self.config, overrides)

            effective_provider, transport = _resolve_native_transport(
                runtime_config,
                provider,
                model=str(model),
                char_data=transport_char_data,
            )
            self.route_metadata = {
                "provider": effective_provider,
                "model": str(model).strip() or None,
                "route_source": route_source,
            }
            logger.info(
                "[GroupChat] Character route: provider=%s model=%s source=%s",
                effective_provider,
                str(model).strip() or "(unset)",
                route_source,
            )
            model_settings = None
            if effective_provider == "openai":
                effort = str(
                    _config_get(runtime_config, "openai.reasoning_effort", "") or ""
                ).strip().lower()
                model_leaf = str(model).strip().lower().rsplit("/", 1)[-1]
                if not effort and model_leaf.startswith("gpt-5.6-luna"):
                    effort = "max"
                try:
                    from ..services.llm_model_catalog import (
                        reasoning_effort_options_for_model,
                    )

                    supported_efforts = reasoning_effort_options_for_model(
                        "openai", str(model)
                    )
                except Exception:
                    supported_efforts = []
                if effort and effort in supported_efforts:
                    model_settings = NativeModelSettings(
                        reasoning=Reasoning(effort=effort)
                    )
            agent = AgentDefinition(
                name=char_data.get("name", "Character"),
                instructions=prompt,
                model=str(model),
                **(
                    {"model_settings": model_settings}
                    if model_settings is not None
                    else {}
                ),
            )
            started_at = time.perf_counter()
            result = await run_native_agent_once(
                agent,
                "",
                api_key=transport.get("api_key"),
                base_url=transport.get("base_url"),
                default_headers=transport.get("default_headers"),
                provider_label=effective_provider,
                config=runtime_config,
            )
            # NativeRunResult currently carries the provider on the runner,
            # not on the result object.  Keep compatibility with adapters that
            # expose their actual label while retaining the transport label as
            # the authoritative fallback.
            actual_provider = (
                getattr(result, "provider_label", None)
                or getattr(result, "provider", None)
                or effective_provider
            )
            self._persist_usage_records(
                char_data,
                getattr(result, "usage_records", None),
                provider=str(actual_provider).strip().lower() or effective_provider,
                model=str(model),
                started_at=started_at,
            )
            return result.final_output or ""
        except Exception as e:
            logger.error(f"グループチャットLLM呼び出し失敗: {e}")
            return f"[{char_data.get('name', '???')}の応答生成に失敗しました]"
